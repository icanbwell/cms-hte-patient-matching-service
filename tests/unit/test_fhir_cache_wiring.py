"""Behavior of the FHIR auth/cache wiring in api.py (BAI-1466).

Only the HTTP boundary (token endpoint) and the cache's FHIR fetch are faked; the
service's own helpers run for real. Needs the `matching` extra, like the service.
"""

from typing import Any, ClassVar
from urllib.parse import parse_qs

import httpx
import pytest

pytest.importorskip("patient_matching")

from patient_matching_service import api

AUTH_ENV = {
    "FHIR_TOKEN_URL": "https://idp.example.test/token",
    "FHIR_CLIENT_ID": "test-client",
    "FHIR_CLIENT_SECRET": "test-secret",  # pragma: allowlist secret
}
ALL_FHIR_ENV = (
    *AUTH_ENV,
    "FHIR_SCOPE",
    "FHIR_TOKEN_RESOURCE",
    "FHIR_BASE_URL",
    "FHIR_PATIENT_SEARCH_PARAMS",
)


@pytest.fixture(autouse=True)
def _clean_fhir_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ALL_FHIR_ENV:
        monkeypatch.delenv(name, raising=False)


async def _token_request_form(auth: Any) -> dict[str, list[str]]:
    """Run the auth's token request against a fake endpoint; return the form sent."""
    seen: dict[str, list[str]] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(parse_qs(request.content.decode()))
        return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await auth.get_access_token(client)
    return seen


def test_no_auth_when_nothing_is_configured() -> None:
    assert api._build_fhir_auth() is None


@pytest.mark.parametrize("missing", sorted(AUTH_ENV))
def test_no_auth_when_only_part_is_configured(
    monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    for name, value in AUTH_ENV.items():
        if name != missing:
            monkeypatch.setenv(name, value)
    assert api._build_fhir_auth() is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("extra_env", "expected_form"),
    [
        ({}, {}),
        ({"FHIR_SCOPE": "user/Patient.read"}, {"scope": ["user/Patient.read"]}),
        (
            {"FHIR_TOKEN_RESOURCE": "https://fhir.example.test"},
            {"resource": ["https://fhir.example.test"]},
        ),
        (
            {
                "FHIR_SCOPE": "user/Patient.read",
                "FHIR_TOKEN_RESOURCE": "https://fhir.example.test",
            },
            {
                "scope": ["user/Patient.read"],
                "resource": ["https://fhir.example.test"],
            },
        ),
    ],
)
async def test_scope_and_resource_reach_the_token_request(
    monkeypatch: pytest.MonkeyPatch,
    extra_env: dict[str, str],
    expected_form: dict[str, list[str]],
) -> None:
    for name, value in {**AUTH_ENV, **extra_env}.items():
        monkeypatch.setenv(name, value)

    auth = api._build_fhir_auth()
    assert auth is not None
    form = await _token_request_form(auth)

    assert form["grant_type"] == ["client_credentials"]
    assert form["client_id"] == [AUTH_ENV["FHIR_CLIENT_ID"]]
    for key in ("scope", "resource"):
        assert form.get(key) == expected_form.get(key)


class _FakeFhirClient:
    configs: ClassVar[list[Any]] = []

    def __init__(self, config: Any) -> None:
        type(self).configs.append(config)


class _FakeCacheManager:
    configs: ClassVar[list[Any]] = []

    def __init__(self, **kwargs: Any) -> None:
        type(self).configs.append(kwargs["config"])

    async def build_cache(self) -> dict[str, int]:
        return {"patients": 0}

    def start_scheduled_refresh(self) -> None:
        return None


@pytest.fixture
def fake_fhir(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fake only the FHIR fetch: the real FhirClient/CacheManager would call out."""
    _FakeFhirClient.configs = []
    _FakeCacheManager.configs = []
    monkeypatch.setattr(
        "patient_matching.fhir_client.client.FhirClient", _FakeFhirClient
    )
    monkeypatch.setattr(
        "patient_matching.cache.cache_manager.CacheManager", _FakeCacheManager
    )


@pytest.mark.asyncio
async def test_search_filter_and_auth_are_passed_to_the_cache_build(
    monkeypatch: pytest.MonkeyPatch, fake_fhir: None
) -> None:
    monkeypatch.setenv("FHIR_BASE_URL", "https://fhir.example.test/4_0_0")
    monkeypatch.setenv("FHIR_PATIENT_SEARCH_PARAMS", "_security=a|b")
    for name, value in AUTH_ENV.items():
        monkeypatch.setenv(name, value)

    await api._build_cache()

    assert _FakeCacheManager.configs[0].search_params == {"_security": "a|b"}
    fhir_config = _FakeFhirClient.configs[0]
    assert fhir_config.base_url == "https://fhir.example.test/4_0_0"
    assert fhir_config.auth is not None


@pytest.mark.asyncio
async def test_no_filter_and_no_auth_when_unset(
    monkeypatch: pytest.MonkeyPatch, fake_fhir: None
) -> None:
    monkeypatch.setenv("FHIR_BASE_URL", "https://fhir.example.test/4_0_0")

    await api._build_cache()

    assert _FakeCacheManager.configs[0].search_params is None
    assert _FakeFhirClient.configs[0].auth is None


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", ["&", "&&", " & ", "novalue"])
async def test_malformed_filter_stops_the_cache_build(
    monkeypatch: pytest.MonkeyPatch, fake_fhir: None, raw: str
) -> None:
    monkeypatch.setenv("FHIR_BASE_URL", "https://fhir.example.test/4_0_0")
    monkeypatch.setenv("FHIR_PATIENT_SEARCH_PARAMS", raw)

    with pytest.raises(ValueError):
        await api._build_cache()

    assert _FakeCacheManager.configs == []


@pytest.mark.asyncio
async def test_filter_and_token_reach_the_patient_search_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real FhirClient and CacheManager; only the HTTP transport is faked."""
    from patient_matching.cache.cache_manager import CacheManager
    from patient_matching.fhir_client.client import FhirClient

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/token":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        return httpx.Response(200, json={"resourceType": "Bundle", "entry": []})

    monkeypatch.setattr(
        FhirClient,
        "_create_http_client",
        lambda self: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    # The refresh loop would outlive the test.
    monkeypatch.setattr(CacheManager, "start_scheduled_refresh", lambda self: None)
    monkeypatch.setenv("FHIR_BASE_URL", "https://fhir.example.test/4_0_0")
    monkeypatch.setenv("FHIR_PATIENT_SEARCH_PARAMS", "_security=a|b")
    for name, value in {
        **AUTH_ENV,
        "FHIR_TOKEN_URL": "https://idp.example.test/token",
    }.items():
        monkeypatch.setenv(name, value)

    await api._build_cache()

    searches = [r for r in seen if r.url.path.endswith("/Patient")]
    assert len(searches) == 1
    assert searches[0].url.params["_security"] == "a|b"
    assert searches[0].headers["Authorization"] == "Bearer t"
