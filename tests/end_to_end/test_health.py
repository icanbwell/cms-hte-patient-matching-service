from fastapi.testclient import TestClient

from patient_matching_service.api import app


def test_health_does_not_require_auth() -> None:
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
