from fastapi.testclient import TestClient

from patient_matching_service.api import app
from patient_matching_service.service.match_controller import MATCHING_ENGINE_AVAILABLE


def test_health_does_not_require_auth() -> None:
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    if MATCHING_ENGINE_AVAILABLE:
        # With the engine installed the cache exists, so the count must be reported.
        # With no FHIR_BASE_URL in the test environment the cache is empty.
        assert body["cache_patients"] == 0
    else:
        assert "cache_patients" not in body
