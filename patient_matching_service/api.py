"""FastAPI app wrapping cms-hte-patient-matching's PatientMatcherService.

See docs/TECH_DESIGN.md for the full design and open questions this
implementation depends on. Notably (Open Question 4, unresolved as of this
writing): which FHIR server backs the candidate cache per environment. If
FHIR_BASE_URL isn't set, the cache starts empty and every $match request
returns no_match -- this is a deliberate degrade-don't-crash choice, not a
bug, so the service stays deployable/testable while that's decided.

cms-hte-patient-matching itself is an optional `matching` extra (see
pyproject.toml, Dockerfile's INSTALL_MATCHING_ENGINE build arg, and
service/match_controller.py's MATCHING_ENGINE_AVAILABLE) -- it pulls in duckdb,
which has no public musllinux wheel, so a build without JFrog access (this
repo is temporarily public; BAI-894) can't install it. When it's absent,
/Patient/$match returns 503 (MatchingEngineUnavailableError below) instead of
the app failing to start.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.parse
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from cmshteial2reader import (
    IAL2Extractor,
    MultiIssuerTokenVerifier,
    TokenVerificationError,
)
from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from prometheus_fastapi_instrumentator import Instrumentator

from patient_matching_service.deps import verify_jwt
from patient_matching_service.filters.endpoint_filter import EndpointFilter
from patient_matching_service.jwt_validator import JWTValidator
from patient_matching_service.observability.logging import configure_logging
from patient_matching_service.service.match_controller import (
    MATCHING_ENGINE_AVAILABLE,
    Ial2PatientValidationError,
    InvalidMatchRequest,
    MatchController,
    MatchingEngineUnavailableError,
    get_match_controller,
)
from patient_matching_service.testing_ui.routes import is_testing_ui_enabled
from patient_matching_service.testing_ui.routes import router as testing_ui_router

# See match_controller.py's module-level comment: `patient_matching`-specific imports
# (PatientMatcherService, CacheBackend, etc.) are TYPE_CHECKING-only here for the same
# reason -- `from __future__ import annotations` makes every annotation below safe to
# reference even when this build doesn't have the `matching` extra installed. The real
# (runtime) imports happen inside _build_cache()/lifespan(), gated on MATCHING_ENGINE_AVAILABLE.
if TYPE_CHECKING:
    from patient_matching.cache.cache_backend import CacheBackend
    from patient_matching.fhir_client.auth import ClientCredentialsAuth

configure_logging()
logger = logging.getLogger(__name__)

logging.getLogger("uvicorn.access").addFilter(EndpointFilter(path="/health"))


def _parse_fhir_search_params(raw: str | None) -> dict[str, str] | None:
    """Parse FHIR_PATIENT_SEARCH_PARAMS into the dict the cache build sends to FHIR.

    The value is a URL query string such as ``_security=<system>|<code>``, and
    restricts which Patients the cache pulls (used to start an environment on a
    small, tagged test sample before pulling everyone). Unset or blank means no
    filter.

    Raises ValueError on a malformed string, an empty value or a repeated key: the cache build passes
    a flat dict, so a repeated key would silently keep only one value, and a filter
    that quietly widens to "everyone" is the failure we most want to avoid.
    """
    if raw is None or not raw.strip():
        return None
    params: dict[str, str] = {}
    for segment in raw.strip().split("&"):
        if not segment:
            continue  # tolerate a stray or trailing "&"
        key, separator, value = segment.partition("=")
        if not separator:
            raise ValueError(
                f"FHIR_PATIENT_SEARCH_PARAMS has a parameter with no '=': {segment!r}"
            )
        # unquote, not unquote_plus: a raw "+" (e.g. a dateTime offset) must reach
        # the FHIR server unchanged rather than become a space.
        key = urllib.parse.unquote(key)
        value = urllib.parse.unquote(value)
        if not key:
            raise ValueError("FHIR_PATIENT_SEARCH_PARAMS has a parameter with no name")
        if not value:
            raise ValueError(
                f"FHIR_PATIENT_SEARCH_PARAMS has an empty value for {key!r}; "
                "an empty value would drop the filter and widen the cache"
            )
        if key in params:
            raise ValueError(
                f"FHIR_PATIENT_SEARCH_PARAMS repeats the key {key!r}; "
                "repeated keys are not supported"
            )
        params[key] = value
    if not params:
        # Only separators (e.g. "&"): there is no parameter, but the value is not
        # blank either, so reading it as "no filter" would silently widen the cache.
        raise ValueError(
            "FHIR_PATIENT_SEARCH_PARAMS has no parameters; unset it for no filter"
        )
    return params


def _build_fhir_auth() -> ClientCredentialsAuth | None:
    """Build OAuth2 client-credentials auth for the FHIR server, or None if unset.

    FHIR_SCOPE is sent as the token request's ``scope`` (b.well's FHIR server
    enforces tenant isolation from ``access/<tenant>`` scopes, so this decides which
    patients the cache can see). FHIR_TOKEN_RESOURCE, if set, is sent as
    ``resource`` (Descope only adds that audience to the token when asked).
    """
    from patient_matching.fhir_client.auth import ClientCredentialsAuth

    token_url = os.getenv("FHIR_TOKEN_URL")
    client_id = os.getenv("FHIR_CLIENT_ID")
    client_secret = os.getenv("FHIR_CLIENT_SECRET")
    if token_url and client_id and client_secret:
        resource = os.getenv("FHIR_TOKEN_RESOURCE")
        return ClientCredentialsAuth(
            token_url=token_url,
            client_id=client_id,
            client_secret=client_secret,
            scope=os.getenv("FHIR_SCOPE", ""),
            extra_params={"resource": resource} if resource else None,
        )
    if token_url or client_id or client_secret:
        logger.warning(
            "FHIR client-credentials auth is partly configured -- FHIR_TOKEN_URL, "
            "FHIR_CLIENT_ID and FHIR_CLIENT_SECRET must all be set. Calling the FHIR "
            "server without auth."
        )
    return None


async def _build_cache() -> CacheBackend:
    """Build the DuckDB candidate cache, populated from a FHIR server if configured.

    Only called when MATCHING_ENGINE_AVAILABLE (see lifespan()) -- imports the
    `matching` extra's classes locally rather than at module scope so this module
    still imports cleanly in a build that doesn't have that extra installed.
    """
    from patient_matching.cache.cache_manager import CacheManager, CacheManagerConfig
    from patient_matching.cache.duckdb_cache import DuckDBCache
    from patient_matching.fhir_client.client import FhirClient, FhirClientConfig

    cache = DuckDBCache(database=os.getenv("CACHE_DATABASE_PATH", ":memory:"))

    fhir_base_url = os.getenv("FHIR_BASE_URL")
    if not fhir_base_url:
        logger.warning(
            "FHIR_BASE_URL is not set -- the candidate cache will stay empty "
            "and every $match request will return no_match. Set FHIR_BASE_URL "
            "(and FHIR_TOKEN_URL/FHIR_CLIENT_ID/FHIR_CLIENT_SECRET if the FHIR "
            "server requires OAuth2 client-credentials auth) to populate it."
        )
        return cache

    search_params = _parse_fhir_search_params(os.getenv("FHIR_PATIENT_SEARCH_PARAMS"))
    fhir_client = FhirClient(
        FhirClientConfig(base_url=fhir_base_url, auth=_build_fhir_auth())
    )
    cache_manager = CacheManager(
        fhir_client=fhir_client,
        cache=cache,
        config=CacheManagerConfig(
            refresh_interval_minutes=int(
                os.getenv("CACHE_REFRESH_INTERVAL_MINUTES", "60")
            ),
            search_params=search_params,
        ),
    )
    # Log the filter's keys, not its values: a value could name a tenant or person.
    logger.info(
        "Building patient cache from FHIR server %s (search filter keys: %s)",
        fhir_base_url,
        sorted(search_params) if search_params else "none",
    )
    counts = await cache_manager.build_cache()
    logger.info("Patient cache built: %s", counts)
    cache_manager.start_scheduled_refresh()
    return cache


def _build_jwt_validator() -> JWTValidator | None:
    """Build the JWT validator from AUTH_JWK_URLS, or None if unset (fail-closed)."""
    jwk_urls_raw = os.getenv("AUTH_JWK_URLS", "")
    jwk_urls = [u.strip() for u in jwk_urls_raw.split(",") if u.strip()]
    if not jwk_urls:
        logger.warning(
            "AUTH_JWK_URLS is not set -- every request to a protected route "
            "will be rejected with 401 (fail-closed, not open)."
        )
        return None

    expected_cids_raw = os.getenv("AUTH_EXPECTED_CIDS", "")
    expected_cids = [c.strip() for c in expected_cids_raw.split(",") if c.strip()]

    return JWTValidator(
        jwk_urls=jwk_urls,
        cid_check_issuer=os.getenv("AUTH_CID_CHECK_ISSUER") or None,
        expected_cids=expected_cids or None,
        cache_ttl=int(os.getenv("AUTH_JWKS_CACHE_TTL_SECONDS", "300")),
    )


def _build_ial2_extractor() -> IAL2Extractor | None:
    """Build the IAL2 extractor from IAL2_ALLOWED_JWKS_URLS/IAL2_AUDIENCE,
    or None if unset (IAL2 support disabled -- a cms_smart identity token
    on a request is then rejected, not silently ignored; see
    MatchController.match)."""
    jwks_urls = os.getenv("IAL2_ALLOWED_JWKS_URLS")
    audience = os.getenv("IAL2_AUDIENCE")
    if not jwks_urls or not audience:
        logger.warning(
            "IAL2_ALLOWED_JWKS_URLS/IAL2_AUDIENCE are not set -- IAL2 "
            "identity token support (the CMS Blue Button cms_smart "
            "extension) is disabled."
        )
        return None

    verifier = MultiIssuerTokenVerifier.from_env(audience=audience)
    return IAL2Extractor(verifier=verifier)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    logger.info("Starting up patient_matching_service...")

    if is_testing_ui_enabled():
        logger.warning(
            "ENABLE_TESTING_UI is set -- exposing the unauthenticated IAL2-decode "
            "and patient-matching testing UI at /testing-ui. Do not set this in a "
            "production or externally-reachable deployment."
        )

    cache = None
    if MATCHING_ENGINE_AVAILABLE:
        from patient_matching.api.service import PatientMatcherService, ServiceConfig

        cache = await _build_cache()
        service = PatientMatcherService(cache=cache, config=ServiceConfig())
    else:
        logger.warning(
            "This build does not include cms-hte-patient-matching (the matching "
            "extra) -- /Patient/$match will return 503 until it's installed. See "
            "INSTALL_MATCHING_ENGINE in the Dockerfile."
        )
        service = None
    app.state.cache = cache
    ial2_extractor = _build_ial2_extractor()
    app.state.match_controller = MatchController(
        service=service, ial2_extractor=ial2_extractor
    )
    app.state.ial2_extractor = ial2_extractor

    app.state.jwt_validator = _build_jwt_validator()

    yield

    logger.info("Shutting down patient_matching_service...")
    if cache is not None:
        await cache.close()


app = FastAPI(lifespan=lifespan)

# noinspection PyTypeChecker
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,  # never combine wildcard origins with credentials
    allow_methods=["*"],
    allow_headers=["*"],
)

instrumentator = Instrumentator()
instrumentator.instrument(app).expose(app)


@app.exception_handler(InvalidMatchRequest)
async def _invalid_match_request_handler(
    request: Request, exc: InvalidMatchRequest
) -> JSONResponse:
    return JSONResponse({"detail": str(exc)}, status_code=400)


@app.exception_handler(MatchingEngineUnavailableError)
async def _matching_engine_unavailable_handler(
    request: Request, exc: MatchingEngineUnavailableError
) -> JSONResponse:
    return JSONResponse({"detail": str(exc)}, status_code=503)


@app.exception_handler(TokenVerificationError)
async def _ial2_token_verification_error_handler(
    request: Request, exc: TokenVerificationError
) -> JSONResponse:
    # Detail is intentionally generic, not str(exc): TokenVerificationError
    # messages can include the token's own (unverified-at-that-point) `iss`
    # claim -- e.g. "resolves to a non-public address (10.x.x.x); rejected"
    # -- which would leak internal network details to whoever sent the
    # token. Same reasoning as jwt_validator.py's auth-failure messages.
    logger.warning("IAL2 identity token verification failed: %s", exc)
    return JSONResponse(
        {"detail": "IAL2 identity token verification failed"},
        status_code=401,
        headers={"WWW-Authenticate": "Bearer"},
    )


@app.exception_handler(Ial2PatientValidationError)
async def _ial2_patient_validation_error_handler(
    request: Request, exc: Ial2PatientValidationError
) -> JSONResponse:
    # Raised by MatchController.match() when a signature-valid CSP token's
    # demographic claims don't build a schema-valid FHIR Patient (e.g. an
    # unparseable birth_date). Deliberately its own exception type, not
    # pydantic's ValidationError directly: that's raised by any pydantic
    # model in the app, so handling it globally here would mislabel an
    # unrelated validation failure (e.g. in the FHIR client) as an IAL2
    # one. Detail is generic, not str(exc): pydantic validation messages
    # include the offending input value, which here would be a fragment of
    # the patient's demographic data. Same reasoning applies to the log
    # line below -- log only field paths/error codes (exc.original.errors()
    # minus 'input'/'msg'/'url'), never str(exc) or the raw errors() dicts,
    # which carry that same input_value.
    failed_fields = [".".join(str(p) for p in e["loc"]) for e in exc.original.errors()]
    logger.warning(
        "IAL2 token produced an invalid FHIR Patient; failed fields: %s",
        failed_fields,
    )
    return JSONResponse(
        {"detail": "IAL2 identity token contains invalid demographic data"},
        status_code=422,
    )


@app.get("/health")
async def health(request: Request) -> dict[str, Any]:
    body: dict[str, Any] = {"status": "ok"}
    # The cached patient count lets a deploy be checked from outside: an empty or
    # unloaded cache otherwise looks exactly like "patient not found". Probes only
    # look at the status code, so a failed count never fails the probe.
    cache = getattr(request.app.state, "cache", None)
    if cache is not None:
        try:
            body["cache_patients"] = await cache.count()
        except Exception:
            logger.exception("Could not count cached patients for /health")
    return body


protected_router = APIRouter()


@protected_router.post("/Patient/$match")
async def match(
    request: Request,
    controller: MatchController = Depends(get_match_controller),
    jwt_claims: dict[str, Any] = Depends(verify_jwt),
) -> JSONResponse:
    # Body is optional: a request whose auth token carries a cms_smart
    # identity (see MatchController.match) doesn't need one. FastAPI
    # caches this Depends(verify_jwt) call against the identical one on
    # protected_router below, so the token isn't verified twice.
    raw_body = await request.body()
    data: dict[str, Any] | None = None
    if raw_body:
        try:
            data = json.loads(raw_body)
        except json.JSONDecodeError as exc:
            raise InvalidMatchRequest(f"Request body is not valid JSON: {exc}") from exc
    bundle = await controller.match(data, jwt_claims=jwt_claims)
    return JSONResponse(bundle, status_code=200, media_type="application/fhir+json")


# Registered unconditionally -- each route in testing_ui_router gates
# itself per-request on ENABLE_TESTING_UI (404 if unset), rather than the
# router only existing when the env var happened to be set at import time.
# See testing_ui/routes.py's _require_testing_ui_enabled.
app.include_router(testing_ui_router)

app.include_router(protected_router, dependencies=[Depends(verify_jwt)])

logger.info("patient_matching_service FastAPI app created")
