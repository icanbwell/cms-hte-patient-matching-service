import pytest
from fastapi.testclient import TestClient

from patient_matching_service.api import app
from patient_matching_service.service.match_controller import MATCHING_ENGINE_AVAILABLE


class _CountingCache:
    def __init__(self, count: int) -> None:
        self._count = count

    async def count(self) -> int:
        return self._count


class _FailingCache:
    async def count(self) -> int:
        raise RuntimeError("cache unavailable")


def test_health_does_not_require_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FHIR_BASE_URL", raising=False)
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    if MATCHING_ENGINE_AVAILABLE:
        # With the engine installed the cache exists, so the count must be reported.
        # With no FHIR_BASE_URL the cache stays empty.
        assert body["cache_patients"] == 0
    else:
        assert "cache_patients" not in body


def test_health_reports_the_cached_patient_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FHIR_BASE_URL", raising=False)
    with TestClient(app) as client:
        app.state.cache = _CountingCache(7)
        body = client.get("/health").json()
    assert body["cache_patients"] == 7


def test_health_still_ok_when_the_count_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Probes only read the status code, so a failed count must not fail the probe.
    monkeypatch.delenv("FHIR_BASE_URL", raising=False)
    with TestClient(app) as client:
        app.state.cache = _FailingCache()
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
