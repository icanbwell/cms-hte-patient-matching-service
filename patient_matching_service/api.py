"""FastAPI app wrapping cms-hte-patient-matching's PatientMatcherService.

See docs/TECH_DESIGN.md for the full design and open questions this
implementation depends on. Notably (Open Question 4, unresolved as of this
writing): which FHIR server backs the candidate cache per environment. If
FHIR_BASE_URL isn't set, the cache starts empty and every $match request
returns no_match -- this is a deliberate degrade-don't-crash choice, not a
bug, so the service stays deployable/testable while that's decided.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

from cmshteial2reader import (
    IAL2Extractor,
    MultiIssuerTokenVerifier,
    TokenVerificationError,
)
from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from patient_matching.api.service import PatientMatcherService, ServiceConfig
from patient_matching.cache.cache_backend import CacheBackend
from patient_matching.cache.cache_manager import CacheManager, CacheManagerConfig
from patient_matching.cache.duckdb_cache import DuckDBCache
from patient_matching.fhir_client.auth import ClientCredentialsAuth
from patient_matching.fhir_client.client import FhirClient, FhirClientConfig
from prometheus_fastapi_instrumentator import Instrumentator

from patient_matching_service.deps import verify_jwt
from patient_matching_service.filters.endpoint_filter import EndpointFilter
from patient_matching_service.jwt_validator import JWTValidator
from patient_matching_service.observability.logging import configure_logging
from patient_matching_service.service.match_controller import (
    InvalidMatchRequest,
    MatchController,
    get_match_controller,
)

configure_logging()
logger = logging.getLogger(__name__)

logging.getLogger("uvicorn.access").addFilter(EndpointFilter(path="/health"))


async def _build_cache() -> CacheBackend:
    """Build the DuckDB candidate cache, populated from a FHIR server if configured."""
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

    auth = None
    token_url = os.getenv("FHIR_TOKEN_URL")
    client_id = os.getenv("FHIR_CLIENT_ID")
    client_secret = os.getenv("FHIR_CLIENT_SECRET")
    if token_url and client_id and client_secret:
        auth = ClientCredentialsAuth(
            token_url=token_url, client_id=client_id, client_secret=client_secret
        )

    fhir_client = FhirClient(FhirClientConfig(base_url=fhir_base_url, auth=auth))
    cache_manager = CacheManager(
        fhir_client=fhir_client,
        cache=cache,
        config=CacheManagerConfig(
            refresh_interval_minutes=int(
                os.getenv("CACHE_REFRESH_INTERVAL_MINUTES", "60")
            )
        ),
    )
    logger.info("Building patient cache from FHIR server %s", fhir_base_url)
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

    cache = await _build_cache()
    service = PatientMatcherService(cache=cache, config=ServiceConfig())
    app.state.match_controller = MatchController(
        service=service, ial2_extractor=_build_ial2_extractor()
    )

    app.state.jwt_validator = _build_jwt_validator()

    yield

    logger.info("Shutting down patient_matching_service...")
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


@app.exception_handler(TokenVerificationError)
async def _ial2_token_verification_error_handler(
    request: Request, exc: TokenVerificationError
) -> JSONResponse:
    return JSONResponse(
        {"detail": f"IAL2 identity token verification failed: {exc}"},
        status_code=401,
        headers={"WWW-Authenticate": "Bearer"},
    )


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


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
            raise InvalidMatchRequest(
                f"Request body is not valid JSON: {exc}"
            ) from exc
    bundle = await controller.match(data, jwt_claims=jwt_claims)
    return JSONResponse(bundle, status_code=200, media_type="application/fhir+json")


app.include_router(protected_router, dependencies=[Depends(verify_jwt)])

logger.info("patient_matching_service FastAPI app created")
