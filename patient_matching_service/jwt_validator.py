"""JWT bearer-token validation against a JWKS endpoint.

Ported from person-matching-service's jwt_validator.py, using PyJWT instead
of python-jose -- cms-hte-patient-matching already depends on PyJWT (for its
own IAL2 token verification), so this avoids adding a second JWT library.
"""

import asyncio
import logging
import time
from typing import Any

import httpx
import jwt
from fastapi import HTTPException, Request

logger = logging.getLogger(__name__)

_AUTH_HEADERS = {"WWW-Authenticate": "Bearer"}


class JWTValidator:
    """JWT token validator using JWKs from a JWK URL.

    This instance is stored on ``app.state`` and shared across all requests.
    The only mutable state is the JWKS cache (``_jwk_keys``), which contains
    **public** key material fetched from the IdP.

    * JWKS are public -- no per-user/per-request data leakage risk.
    * An ``asyncio.Lock`` serialises cache refreshes so concurrent requests
      don't trigger duplicate fetches.
    * FastAPI runs on a single-threaded asyncio event loop, so no threading
      races are possible.
    """

    def __init__(
        self,
        jwk_urls: list[str],
        cid_check_issuer: str | None = None,
        expected_cids: list[str] | None = None,
        cache_ttl: int = 300,
    ):
        if not jwk_urls:
            raise ValueError("jwk_urls is required to create a JWTValidator")
        self.jwk_urls = jwk_urls
        self.cid_check_issuer = cid_check_issuer
        self.expected_cids = expected_cids  # supports multiple CIDs
        self.cache_ttl = cache_ttl

        self._jwk_keys: list[dict[str, Any]] | None = None
        self._jwks_cached_at: float | None = None
        self._jwks_lock = asyncio.Lock()

    async def get_jwk_keys(self) -> list[dict[str, Any]]:
        """Fetch JWKs from the JWK URL(s) with time-based caching."""
        current_time = time.time()

        if (
            self._jwk_keys is not None
            and self._jwks_cached_at is not None
            and (current_time - self._jwks_cached_at) <= self.cache_ttl
        ):
            logger.debug("Using cached JWK Keys")
            return self._jwk_keys

        async with self._jwks_lock:
            current_time = time.time()
            if (
                self._jwk_keys is not None
                and self._jwks_cached_at is not None
                and (current_time - self._jwks_cached_at) <= self.cache_ttl
            ):
                logger.debug("Using cached JWK Keys (refreshed by another coroutine)")
                return self._jwk_keys

            logger.info("Fetching JWK Keys from %s", self.jwk_urls)
            jwk_keys: list[dict[str, Any]] = []
            async with httpx.AsyncClient() as client:
                for url in self.jwk_urls:
                    response = await client.get(url)
                    response.raise_for_status()
                    jwk_keys.extend(response.json().get("keys", []))

            self._jwk_keys = jwk_keys
            self._jwks_cached_at = current_time

        return self._jwk_keys

    async def verify_token(self, token: str) -> dict[str, Any]:
        """Verify JWT token and return the payload."""
        try:
            jwk_keys = await self.get_jwk_keys()

            unverified_header = jwt.get_unverified_header(token)
            token_kid = unverified_header.get("kid")
            if not token_kid:
                logger.error("JWT header missing 'kid' claim")
                raise HTTPException(
                    status_code=401,
                    detail="JWT header missing 'kid'",
                    headers=_AUTH_HEADERS,
                )
            raw_jwk = next(
                (key for key in jwk_keys if key.get("kid") == token_kid),
                None,
            )

            if not raw_jwk:
                logger.error("No matching JWK found for kid: %s", token_kid)
                raise HTTPException(
                    status_code=401,
                    detail="No matching signing key found",
                    headers=_AUTH_HEADERS,
                )

            signing_key = jwt.PyJWK.from_dict(raw_jwk).key
            payload = jwt.decode(
                token,
                signing_key,
                algorithms=["RS256"],
                options={"verify_aud": False},
            )

        except jwt.PyJWTError as e:
            logger.error("JWT verification failed: %s", e)
            raise HTTPException(
                status_code=401,
                detail="JWT verification failed",
                headers=_AUTH_HEADERS,
            ) from e
        except HTTPException:
            raise
        except Exception as e:
            logger.error("Unexpected error during JWT verification: %s", e)
            raise HTTPException(
                status_code=401,
                detail="Authentication error",
                headers=_AUTH_HEADERS,
            ) from e

        if self.cid_check_issuer and payload.get("iss") == self.cid_check_issuer:
            self.validate_cid(payload)

        return dict(payload)

    def validate_cid(self, payload: dict[str, Any]) -> None:
        """Validate the cid claim against one or more expected values."""
        if not self.expected_cids:
            return
        cid = payload.get("cid", None)
        if not cid:
            logger.error("Token missing required claim: cid")
            raise HTTPException(
                status_code=401,
                detail="Token missing required claim: cid",
                headers=_AUTH_HEADERS,
            )
        if cid not in self.expected_cids:
            logger.error(
                "Invalid cid in token: %s (expected one of %s)", cid, self.expected_cids
            )
            raise HTTPException(
                status_code=401,
                detail="Invalid client ID",
                headers=_AUTH_HEADERS,
            )


def get_jwt_validator(request: Request) -> JWTValidator:
    """Get JWT validator from the app state."""
    jwt_validator = getattr(request.app.state, "jwt_validator", None)
    if jwt_validator is None:
        logger.error("JWT authentication is not configured")
        raise HTTPException(
            status_code=401,
            detail="JWT authentication is not configured",
            headers=_AUTH_HEADERS,
        )
    if not isinstance(jwt_validator, JWTValidator):
        raise HTTPException(
            status_code=401,
            detail="Invalid JWT validator configuration",
            headers=_AUTH_HEADERS,
        )
    return jwt_validator
