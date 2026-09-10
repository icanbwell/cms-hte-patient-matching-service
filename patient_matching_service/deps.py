from typing import Any

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from patient_matching_service.jwt_validator import JWTValidator, get_jwt_validator

# HTTP Bearer token extractor -- auto_error=False so we return 401 (not 403)
bearer_token = HTTPBearer(auto_error=False)

_AUTH_HEADERS = {"WWW-Authenticate": "Bearer"}


async def verify_jwt(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_token),
    jwt_validator: JWTValidator = Depends(get_jwt_validator),
) -> dict[str, Any]:
    """Verify the JWT token and return the payload."""
    if credentials is None:
        raise HTTPException(
            status_code=401,
            detail="Missing or invalid Authorization header",
            headers=_AUTH_HEADERS,
        )
    token = credentials.credentials
    payload = await jwt_validator.verify_token(token)
    return payload
