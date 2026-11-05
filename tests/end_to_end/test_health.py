from fastapi.testclient import TestClient

from patient_matching_service.api import app


def test_health_does_not_require_auth() -> None:
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    # Present whenever the matching engine (and so the cache) is installed. With no
    # FHIR_BASE_URL in the test environment the cache is empty.
    if "cache_patients" in body:
        assert body["cache_patients"] == 0
