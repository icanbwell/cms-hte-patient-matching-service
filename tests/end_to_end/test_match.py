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

from collections.abc import AsyncGenerator
from typing import Any

import pytest
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
