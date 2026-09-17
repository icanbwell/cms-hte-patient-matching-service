import pytest
from cmshteial2reader import IAL2Extractor

from patient_matching_service.api import _build_ial2_extractor


class TestBuildIal2Extractor:
    def test_returns_none_when_both_env_vars_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("IAL2_ALLOWED_JWKS_URLS", raising=False)
        monkeypatch.delenv("IAL2_AUDIENCE", raising=False)

        assert _build_ial2_extractor() is None

    def test_returns_none_when_only_jwks_urls_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("IAL2_ALLOWED_JWKS_URLS", "https://idp.example.com/jwks")
        monkeypatch.delenv("IAL2_AUDIENCE", raising=False)

        assert _build_ial2_extractor() is None

    def test_returns_none_when_only_audience_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("IAL2_ALLOWED_JWKS_URLS", raising=False)
        monkeypatch.setenv("IAL2_AUDIENCE", "my-client-id")

        assert _build_ial2_extractor() is None

    def test_builds_extractor_when_both_env_vars_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("IAL2_ALLOWED_JWKS_URLS", "https://idp.example.com/jwks")
        monkeypatch.setenv("IAL2_AUDIENCE", "my-client-id")

        extractor = _build_ial2_extractor()

        assert isinstance(extractor, IAL2Extractor)
