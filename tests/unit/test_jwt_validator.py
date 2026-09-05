from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from patient_matching_service.deps import verify_jwt
from patient_matching_service.jwt_validator import JWTValidator, get_jwt_validator


class TestJWTValidatorConstruction:
    def test_requires_at_least_one_jwk_url(self) -> None:
        with pytest.raises(ValueError):
            JWTValidator(jwk_urls=[])


class TestGetJwtValidator:
    def test_missing_validator_fails_closed(self) -> None:
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))
        with pytest.raises(HTTPException) as exc_info:
            get_jwt_validator(request)  # type: ignore[arg-type]
        assert exc_info.value.status_code == 401

    def test_configured_validator_is_returned(self) -> None:
        validator = JWTValidator(jwk_urls=["https://example.com/jwks.json"])
        request = SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(jwt_validator=validator))
        )
        assert get_jwt_validator(request) is validator  # type: ignore[arg-type]


class TestVerifyJwt:
    @pytest.mark.asyncio
    async def test_missing_credentials_returns_401(self) -> None:
        validator = JWTValidator(jwk_urls=["https://example.com/jwks.json"])
        with pytest.raises(HTTPException) as exc_info:
            await verify_jwt(credentials=None, jwt_validator=validator)
        assert exc_info.value.status_code == 401
