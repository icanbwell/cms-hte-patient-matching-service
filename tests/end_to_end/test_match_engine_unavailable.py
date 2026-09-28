"""POST /Patient/$match when the `matching` extra isn't installed.

Only relevant to the default build (INSTALL_MATCHING_ENGINE=false, no
cms-hte-patient-matching) -- see patient_matching_service/service/match_controller.py's
module-level comment. tests/end_to_end/test_match.py covers every other $match behavior,
but is skipped entirely in that build (it needs the real engine), so without this file
the 503 path this whole opt-in-extra design exists to guarantee would have zero coverage
in the one build where it's actually exercised.
"""

from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient

from patient_matching_service.api import app
from patient_matching_service.deps import verify_jwt
from patient_matching_service.service.match_controller import MATCHING_ENGINE_AVAILABLE

pytestmark = pytest.mark.skipif(
    MATCHING_ENGINE_AVAILABLE,
    reason="only exercises the no-`matching`-extra path",
)


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    app.dependency_overrides[verify_jwt] = lambda: {}  # noqa: PIE807
    with TestClient(app) as test_client:
        yield test_client
    del app.dependency_overrides[verify_jwt]


def test_match_returns_503_when_matching_engine_unavailable(client: TestClient) -> None:
    response = client.post(
        "/Patient/$match",
        json={
            "resourceType": "Parameters",
            "parameter": [
                {"name": "resource", "resource": {"resourceType": "Patient"}}
            ],
        },
    )
    assert response.status_code == 503
    assert "matching" in response.json()["detail"].lower()
