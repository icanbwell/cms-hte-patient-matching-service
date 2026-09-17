"""End-to-end tests for the /testing-ui manual-testing page and its
/testing-ui-api/* endpoints (see patient_matching_service/testing_ui/).

ENABLE_TESTING_UI is checked per-request (see
testing_ui.routes._require_testing_ui_enabled), so it can be toggled per
test via monkeypatch without needing to reimport patient_matching_service.api.
"""

from __future__ import annotations

import time
from collections.abc import Generator
from typing import Any

import jwt
import pytest
from cmshteial2reader import IAL2Extractor, TokenVerificationError
from fastapi.testclient import TestClient

from patient_matching_service.api import app

_SIMPLE_PATIENT: dict[str, Any] = {
    "resourceType": "Patient",
    "name": [{"family": "Smith", "given": ["John"]}],
    "birthDate": "1990-01-15",
    "address": [{"line": ["123 Main St"], "postalCode": "12345"}],
}

_DIFFERENT_PATIENT: dict[str, Any] = {
    "resourceType": "Patient",
    "name": [{"family": "Jones", "given": ["Robert"]}],
    "birthDate": "1975-06-20",
    "address": [{"line": ["999 Oak Ave"], "postalCode": "99999"}],
}


class _StubIAL2Verifier:
    """A TokenVerifierProtocol stub returning canned claims (or raising)
    instead of doing real JWKS/signature verification -- see
    test_match.py's identical stub; duplicated here rather than shared to
    keep this file's fixtures self-contained."""

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


def _token_with_issuer(issuer: str) -> str:
    """Build a JWT-shaped (but unsigned-key-irrelevant) token carrying the
    given `iss` claim, so _peek_issuer can read it via an unverified
    decode -- the stub verifier below ignores the token's actual contents
    anyway, so any signing key works."""
    return jwt.encode(
        {"iss": issuer},
        key="unused-test-signing-key-not-verified-below",
        algorithm="HS256",
    )


_IAL2_CLAIMS: dict[str, Any] = {
    "iss": "https://csp.example.com",
    "sub": "csp-user-1",
    "aud": "my-client",
    "exp": int(time.time()) + 3600,
    "iat": int(time.time()),
    "jti": "jti-1",
    "identity_assurance_level": "ial2",
    "name_first": "john",
    "name_last": "smith",
    "birth_date": "1990-01-15",
}


@pytest.fixture
def enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENABLE_TESTING_UI", "true")


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    with TestClient(app) as test_client:
        yield test_client


def test_testing_ui_page_is_404_by_default(client: TestClient) -> None:
    response = client.get("/testing-ui")
    assert response.status_code == 404


def test_decode_endpoint_is_404_by_default(client: TestClient) -> None:
    response = client.post("/testing-ui-api/decode-ial2-token", json={"token": "x"})
    assert response.status_code == 404


def test_match_pair_endpoint_is_404_by_default(client: TestClient) -> None:
    response = client.post(
        "/testing-ui-api/match-pair",
        json={"patient_a": _SIMPLE_PATIENT, "patient_b": _SIMPLE_PATIENT},
    )
    assert response.status_code == 404


def test_testing_ui_page_served_when_enabled(enabled: None, client: TestClient) -> None:
    response = client.get("/testing-ui")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "Testing UI" in response.text


def test_decode_ial2_token_without_extractor_configured_returns_503(
    enabled: None, client: TestClient
) -> None:
    """The default `client` fixture's app.state.ial2_extractor is None
    (IAL2_ALLOWED_JWKS_URLS/IAL2_AUDIENCE unset in the test environment)."""
    response = client.post("/testing-ui-api/decode-ial2-token", json={"token": "x"})
    assert response.status_code == 503


def test_decode_ial2_token_invalid_body_returns_400(
    enabled: None, client: TestClient
) -> None:
    app.state.ial2_extractor = IAL2Extractor(verifier=_StubIAL2Verifier(_IAL2_CLAIMS))
    try:
        response = client.post("/testing-ui-api/decode-ial2-token", json={})
    finally:
        app.state.ial2_extractor = None
    assert response.status_code == 400


def test_decode_ial2_token_success(enabled: None, client: TestClient) -> None:
    app.state.ial2_extractor = IAL2Extractor(
        verifier=_StubIAL2Verifier(claims=_IAL2_CLAIMS)
    )
    try:
        response = client.post(
            "/testing-ui-api/decode-ial2-token",
            json={"token": _token_with_issuer("https://csp.example.com")},
        )
    finally:
        app.state.ial2_extractor = None

    assert response.status_code == 200
    body = response.json()
    assert body["accepted"] is True
    assert body["issuer"] == "https://csp.example.com"
    patient = body["patient"]
    assert patient["resourceType"] == "Patient"
    assert patient["name"][0]["family"] == "smith"
    assert patient["birthDate"] == "1990-01-15"


def test_decode_ial2_token_rejected_issuer_shows_issuer_and_detail(
    enabled: None, client: TestClient
) -> None:
    """A JWKS URL that isn't whitelisted must surface the real detail (which
    issuer/JWKS URL was rejected and why) -- unlike POST /Patient/$match,
    which deliberately hides this from untrusted callers."""
    rejection = TokenVerificationError(
        "Issuer 'https://evil.example.com' resolved to JWKS URL "
        "'https://evil.example.com/jwks', which is not whitelisted"
    )
    app.state.ial2_extractor = IAL2Extractor(
        verifier=_StubIAL2Verifier(raises=rejection)
    )
    try:
        response = client.post(
            "/testing-ui-api/decode-ial2-token",
            json={"token": _token_with_issuer("https://evil.example.com")},
        )
    finally:
        app.state.ial2_extractor = None

    assert response.status_code == 401
    body = response.json()
    assert body["accepted"] is False
    assert body["issuer"] == "https://evil.example.com"
    assert body["error_type"] == "TokenVerificationError"
    assert "not whitelisted" in body["detail"]


def test_decode_ial2_token_malformed_claims_returns_422_with_detail(
    enabled: None, client: TestClient
) -> None:
    claims = {**_IAL2_CLAIMS, "name_first": 12345}
    app.state.ial2_extractor = IAL2Extractor(verifier=_StubIAL2Verifier(claims=claims))
    try:
        response = client.post(
            "/testing-ui-api/decode-ial2-token",
            json={"token": _token_with_issuer("https://csp.example.com")},
        )
    finally:
        app.state.ial2_extractor = None

    assert response.status_code == 422
    body = response.json()
    assert body["accepted"] is False
    assert body["error_type"] == "ValidationError"


def test_match_pair_invalid_body_returns_400(enabled: None, client: TestClient) -> None:
    response = client.post(
        "/testing-ui-api/match-pair", json={"patient_a": _SIMPLE_PATIENT}
    )
    assert response.status_code == 400


def test_match_pair_matching_patients_reports_matched_rule(
    enabled: None, client: TestClient
) -> None:
    response = client.post(
        "/testing-ui-api/match-pair",
        json={"patient_a": _SIMPLE_PATIENT, "patient_b": _SIMPLE_PATIENT},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["outcome"] == "match"
    assert body["matched"] is True
    assert body["matched_rule_id"] == "01"

    rule_01 = next(r for r in body["rules"] if r["rule_id"] == "01")
    assert rule_01["evaluated"] is True
    assert rule_01["matched"] is True
    assert rule_01["field_outcomes"]["first_name"] == "exact"


def test_match_pair_different_patients_reports_no_match_with_reason(
    enabled: None, client: TestClient
) -> None:
    response = client.post(
        "/testing-ui-api/match-pair",
        json={"patient_a": _SIMPLE_PATIENT, "patient_b": _DIFFERENT_PATIENT},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["outcome"] == "no_match"
    assert body["matched"] is False

    rule_01 = next(r for r in body["rules"] if r["rule_id"] == "01")
    assert rule_01["evaluated"] is False
    assert rule_01["matched"] is False
    assert rule_01["reason"]


def test_match_pair_missing_fields_reports_which_fields_are_missing(
    enabled: None, client: TestClient
) -> None:
    bare_patient = {"resourceType": "Patient", "name": [{"family": "Smith"}]}
    response = client.post(
        "/testing-ui-api/match-pair",
        json={"patient_a": bare_patient, "patient_b": bare_patient},
    )

    assert response.status_code == 200
    body = response.json()
    rule_08 = next(r for r in body["rules"] if r["rule_id"] == "08")
    assert rule_08["evaluated"] is False
    assert "dob" in rule_08["reason"]
