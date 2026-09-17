"""End-to-end tests for POST /Patient/$match.

Auth is bypassed via FastAPI's own dependency_overrides mechanism (the
documented, supported way to test a route protected by Depends(verify_jwt)
without standing up a real IdP/JWKS endpoint) -- this is not a workaround,
it's how FastAPI itself recommends testing auth-gated routes. Full JWT
verification is covered separately by tests/unit tests on JWTValidator.

The candidate cache is seeded directly via app.state after the lifespan
starts (which otherwise leaves the cache empty, since no FHIR_BASE_URL is
set in the test environment) -- app is a module-level singleton, so the
override is undone in a finally block to avoid leaking into other tests.
"""

import time
from collections.abc import AsyncGenerator
from typing import Any

import pytest
from cmshteial2reader import IAL2Extractor, TokenVerificationError
from fastapi.testclient import TestClient
from patient_matching.api.service import PatientMatcherService
from patient_matching.cache.cache_backend import CachedPatient
from patient_matching.cache.duckdb_cache import DuckDBCache

from patient_matching_service.api import app
from patient_matching_service.deps import verify_jwt
from patient_matching_service.service.match_controller import MatchController

_KNOWN_PATIENT: dict[str, Any] = {
    "resourceType": "Patient",
    "id": "p1",
    "name": [{"family": "smith", "given": ["john"]}],
    "birthDate": "1990-01-15",
    "telecom": [
        {"system": "phone", "value": "+12125551234"},
        {"system": "email", "value": "john@gmail.com"},
    ],
    "address": [{"line": ["123 main st"]}],
    "identifier": [
        {"system": "http://hl7.org/fhir/sid/us-ssn", "value": "xxx-xx-6789"}
    ],
}


async def _seeded_cache() -> DuckDBCache:
    cache = DuckDBCache(database=":memory:")
    await cache.upsert_patients(
        [
            CachedPatient(
                patient_id="p1",
                first_names={"john"},
                last_names={"smith"},
                dob={"1990-01-15"},
                phones={"+12125551234"},
                emails={"john@gmail.com"},
                ssn_last4={"6789"},
                street_lines={"123 main st"},
                fhir_resource=_KNOWN_PATIENT,
            )
        ]
    )
    return cache


@pytest.fixture
async def client() -> AsyncGenerator[TestClient, None]:
    # NOT `= dict`: FastAPI's dependency-override resolution introspects the
    # override callable's own signature (to resolve *its* sub-dependencies),
    # and inspect.signature() raises ValueError on the builtin `dict` type.
    # ruff's PIE807 suggests `dict` as "equivalent" to `lambda: {}` in general
    # Python, but that equivalence breaks here -- ignore that suggestion.
    app.dependency_overrides[verify_jwt] = lambda: {}  # noqa: PIE807
    with TestClient(app) as test_client:
        cache = await _seeded_cache()
        app.state.match_controller = MatchController(
            service=PatientMatcherService(cache=cache)
        )
        try:
            yield test_client
        finally:
            await cache.close()
    del app.dependency_overrides[verify_jwt]


def _parameters(patient: dict[str, Any]) -> dict[str, Any]:
    return {
        "resourceType": "Parameters",
        "parameter": [{"name": "resource", "resource": patient}],
    }


def _cms_smart_claims(id_token: str) -> dict[str, Any]:
    return {"extensions": {"cms_smart": {"id_token": id_token}}}


class _StubIAL2Verifier:
    """A TokenVerifierProtocol stub returning canned claims (or raising)
    instead of doing real JWKS/signature verification -- lets tests
    exercise the real IAL2Extractor/IAL2ToFhirConverter pipeline without a
    live CSP."""

    def __init__(
        self,
        claims: dict[str, Any] | None = None,
        *,
        raises: Exception | None = None,
    ) -> None:
        self._claims = claims
        self._raises = raises

    async def verify(self, token: str) -> dict[str, Any]:
        if self._raises is not None:
            raise self._raises
        assert self._claims is not None
        return self._claims


_IAL2_CLAIMS: dict[str, Any] = {
    "iss": "https://csp.example.com",
    "sub": "csp-user-1",
    "aud": "my-client",
    "exp": int(time.time()) + 3600,
    "iat": int(time.time()),
    "jti": "jti-1",
    "name_first": "john",
    "name_last": "smith",
    "birth_date": "1990-01-15",
    "email": "john@gmail.com",
    "phone_number": "+12125551234",
    "address_line1": "123 main st",
}


def _make_ial2_client(verifier: _StubIAL2Verifier) -> AsyncGenerator[TestClient, None]:
    """Shared body for the ial2_client-style fixtures below, parameterized
    on the stub verifier so failure-path fixtures can inject a verifier
    that raises instead of one that succeeds."""

    async def _generator() -> AsyncGenerator[TestClient, None]:
        app.dependency_overrides[verify_jwt] = lambda: _cms_smart_claims(
            "the-nested-jwt"
        )
        with TestClient(app) as test_client:
            cache = await _seeded_cache()
            app.state.match_controller = MatchController(
                service=PatientMatcherService(cache=cache),
                ial2_extractor=IAL2Extractor(verifier=verifier),
            )
            try:
                yield test_client
            finally:
                await cache.close()
        del app.dependency_overrides[verify_jwt]

    return _generator()


@pytest.fixture
async def ial2_client() -> AsyncGenerator[TestClient, None]:
    """Like `client`, but the auth token carries a cms_smart identity
    (extensions.cms_smart.id_token) and the controller is wired with a
    real IAL2Extractor backed by a stub verifier."""
    async for c in _make_ial2_client(_StubIAL2Verifier(claims=_IAL2_CLAIMS)):
        yield c


@pytest.fixture
async def ial2_client_with_invalid_token() -> AsyncGenerator[TestClient, None]:
    """Like `ial2_client`, but the verifier rejects the token outright
    (bad signature/audience/issuer)."""
    verifier = _StubIAL2Verifier(
        raises=TokenVerificationError(
            "Issuer 'https://169.254.169.254' resolves to a non-public "
            "address (169.254.169.254); rejected"
        )
    )
    async for c in _make_ial2_client(verifier):
        yield c


@pytest.fixture
async def ial2_client_with_malformed_claims() -> AsyncGenerator[TestClient, None]:
    """Like `ial2_client`, but the token verifies successfully yet its
    claims produce a FHIR-schema-invalid Patient (wrong-typed field)."""
    claims = {**_IAL2_CLAIMS, "name_first": 12345}
    async for c in _make_ial2_client(_StubIAL2Verifier(claims=claims)):
        yield c


def test_match_returns_known_patient(client: TestClient) -> None:
    query = {
        "resourceType": "Patient",
        "name": [{"family": "smith", "given": ["john"]}],
        "birthDate": "1990-01-15",
        "telecom": [
            {"system": "phone", "value": "+12125551234"},
            {"system": "email", "value": "john@gmail.com"},
        ],
        "address": [{"line": ["123 main st"]}],
        "identifier": [
            {"system": "http://hl7.org/fhir/sid/us-ssn", "value": "xxx-xx-6789"}
        ],
    }

    response = client.post("/Patient/$match", json=_parameters(query))

    assert response.status_code == 200
    bundle = response.json()
    assert bundle["resourceType"] == "Bundle"
    assert bundle["total"] == 1
    assert bundle["entry"][0]["resource"]["id"] == "p1"


def test_match_with_no_candidates_returns_empty_bundle(client: TestClient) -> None:
    query = {
        "resourceType": "Patient",
        "name": [{"family": "nobody", "given": ["stranger"]}],
        "birthDate": "1901-01-01",
    }

    response = client.post("/Patient/$match", json=_parameters(query))

    assert response.status_code == 200
    bundle = response.json()
    assert bundle["total"] == 0


def test_invalid_request_returns_400(client: TestClient) -> None:
    response = client.post("/Patient/$match", json={"resourceType": "Patient"})
    assert response.status_code == 400


def test_malformed_json_body_returns_400(client: TestClient) -> None:
    response = client.post(
        "/Patient/$match",
        content=b"not json",
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 400


def test_match_without_auth_override_is_rejected() -> None:
    """Sanity check that the route really is auth-gated (not just that our
    override happens to work) -- run without the client fixture's override."""
    with TestClient(app) as test_client:
        response = test_client.post(
            "/Patient/$match", json=_parameters({"resourceType": "Patient"})
        )
    assert response.status_code == 401


def test_match_with_cms_smart_identity_token_and_no_body(
    ial2_client: TestClient,
) -> None:
    """A caller presenting a CMS Blue Button cms_smart identity needs no
    request body -- the query Patient comes from the nested IAL2 token."""
    response = ial2_client.post("/Patient/$match", content=b"")

    assert response.status_code == 200
    bundle = response.json()
    assert bundle["resourceType"] == "Bundle"
    assert bundle["total"] == 1
    assert bundle["entry"][0]["resource"]["id"] == "p1"


def test_match_with_cms_smart_identity_token_ignores_body(
    ial2_client: TestClient,
) -> None:
    response = ial2_client.post(
        "/Patient/$match",
        json=_parameters({"resourceType": "Patient", "id": "should-be-ignored"}),
    )

    assert response.status_code == 200
    assert response.json()["total"] == 1


def test_match_with_cms_smart_claim_but_no_ial2_extractor_configured(
    client: TestClient,
) -> None:
    """The default `client` fixture's controller has no ial2_extractor --
    a cms_smart identity token must be rejected, not silently ignored."""
    app.dependency_overrides[verify_jwt] = lambda: _cms_smart_claims("the-nested-jwt")
    try:
        response = client.post("/Patient/$match", content=b"")
    finally:
        app.dependency_overrides[verify_jwt] = lambda: {}  # noqa: PIE807

    assert response.status_code == 400


def test_match_with_invalid_ial2_token_returns_generic_401(
    ial2_client_with_invalid_token: TestClient,
) -> None:
    """A token that fails verification must be rejected with a generic
    401 -- not one that echoes the verifier's internal error detail (which
    can include the token's own, not-yet-trusted `iss` claim, e.g. an
    internal address an SSRF-protection check rejected)."""
    response = ial2_client_with_invalid_token.post("/Patient/$match", content=b"")

    assert response.status_code == 401
    detail = response.json()["detail"]
    assert detail == "IAL2 identity token verification failed"
    assert "169.254.169.254" not in detail


def test_match_with_malformed_ial2_claims_returns_generic_422(
    ial2_client_with_malformed_claims: TestClient,
) -> None:
    """Claims that verify successfully but produce a FHIR-schema-invalid
    Patient must be rejected with a generic 422 -- not one that echoes
    pydantic's validation message, which includes the offending claim
    value (here, patient demographic data)."""
    response = ial2_client_with_malformed_claims.post("/Patient/$match", content=b"")

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail == "IAL2 identity token contains invalid demographic data"
    assert "12345" not in detail
