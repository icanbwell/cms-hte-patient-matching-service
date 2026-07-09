import logging
import time
from typing import Any, override

from authlib.jose.errors import JoseError
from fastmcp.server.auth import AccessToken
from fastmcp.server.auth.providers.jwt import JWTVerifier

logger = logging.getLogger(__name__)
# enable debug logging for this module
# logger.setLevel(logging.DEBUG)


class JwtVerifierWithLogging(JWTVerifier):
    """
    JWT Verifier with logging capabilities.
    This class extends the JWTVerifier to add logging for token verification.
    """

    @override
    async def verify_token(self, token: str) -> AccessToken | None:
        """
        Verify a bearer token and return access info if valid.

        This method implements the TokenVerifier protocol by delegating
        to our existing load_access_token method.

        Args:
            token: The token string to validate

        Returns:
            AccessToken object if valid, None if invalid or expired
        """
        logger.debug(f"Verifying token: {token}")
        access_token: AccessToken | None = await super().verify_token(token)
        if access_token:
            logger.debug(f"Token verified successfully: {access_token}")
        else:
            logger.debug(f"Token verification failed or token is expired: {token}")
        return access_token

    @override
    async def load_access_token(self, token: str) -> AccessToken | None:
        """
        Validates the provided JWT bearer token.

        Args:
            token: The JWT token string to validate

        Returns:
            AccessToken object if valid, None if invalid or expired
        """
        try:
            # Use the parent class for JWT decoding
            claims = await super().load_access_token(token)
            if claims is None:
                return None

            # Extract client ID for logging
            client_id = (
                claims.client_id
                or claims.claims.get("client_id")
                or claims.claims.get("email")
                or claims.claims.get("sub")
                or "unknown"
            )
            logger.debug(f"Client ID: {client_id}")

            # Validate expiration
            exp = claims.expires_at
            now = time.time()
            # convert exp to string for logging
            exp_str = (
                time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(exp))
                if exp
                else "None"
            )
            now_str = (
                time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))
                if exp
                else "None"
            )
            if exp is None:
                logger.warning(
                    f"Token validation failed: missing 'exp' claim for client {client_id}"
                )
                logger.info(
                    f"Bearer token rejected for client {client_id}: missing expiration claim."
                )
                return None
            if exp < now:
                # Log the expiration details
                logger.warning(
                    f"Token validation failed: expired token for client {client_id}. Exp: {exp_str}, Now: {now_str}"
                )
                logger.info(
                    f"Bearer token rejected for client {client_id}: token expired."
                )
                return None

            # Validate issuer - note we use issuer instead of issuer_url here because
            # issuer is optional, allowing users to make this check optional
            if self.issuer:
                claim_issuer = claims.claims.get("iss")
                if claim_issuer != self.issuer:
                    logger.warning(
                        f"Token validation failed: issuer mismatch for client {client_id}. Claim issuer: {claim_issuer}, Expected: {self.issuer}"
                    )
                    logger.info(
                        f"Bearer token rejected for client {client_id}: issuer mismatch."
                    )
                    return None

            # Validate audience if configured
            if self.audience:
                aud = claims.claims.get("aud")

                # Handle different combinations of audience types
                audience_valid = False
                if isinstance(self.audience, list):
                    # self.audience is a list - check if any expected audience is present
                    if isinstance(aud, list):
                        # Both are lists - check for intersection
                        audience_valid = any(
                            expected in aud for expected in self.audience
                        )
                    else:
                        # aud is a string - check if it's in our expected list
                        audience_valid = aud in self.audience
                else:
                    # self.audience is a string - use original logic
                    if isinstance(aud, list):
                        audience_valid = self.audience in aud
                    else:
                        audience_valid = aud == self.audience

                if not audience_valid:
                    logger.warning(
                        f"Token validation failed: audience mismatch for client {client_id}. Claim audience: {aud}, Expected: {self.audience}"
                    )
                    logger.info(
                        f"Bearer token rejected for client {client_id}: audience mismatch."
                    )
                    return None

            # Extract scopes
            scopes = claims.scopes
            logger.debug(f"Scopes extracted: {scopes}")
            logger.info(
                f"Token validated successfully for client {client_id} with scopes: {scopes} exp: {exp_str}, now: {now_str}"
            )
            return AccessToken(
                token=token,
                client_id=str(client_id),
                scopes=scopes,
                expires_at=exp,
            )

        except JoseError as je:
            logger.error(
                f"Token validation failed: JWT signature/format invalid for token {token}. Error: {je}"
            )
            logger.info(f"Bearer token rejected: JWT error for token {token}")
            return None
        except Exception as e:
            logger.error(
                f"Token validation failed: Unexpected error for token {token}. Error: {type(e).__name__}: {e}"
            )
            logger.info(f"Bearer token rejected: unexpected error for token {token}")
            return None
