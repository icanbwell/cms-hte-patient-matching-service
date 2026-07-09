import logging
import time
from typing import override

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
            # Get verification key (static or from JWKS)
            verification_key = await self._get_verification_key(token)
            logger.debug(f"Received verification key: {verification_key}")

            # Decode and verify the JWT token
            claims = self.jwt.decode(token, verification_key)
            logger.debug(f"Decoded claims: {claims}")

            # Select the client identifier from JWT claims using the following precedence:
            # 1. Prefer "client_id" if present, as this is the canonical identifier for OAuth2 clients.
            # 2. If "client_id" is missing, use "email" if present, as it may identify the user in some systems.
            # 3. If both are missing, use "sub" (subject), which is a standard JWT claim for the principal.
            # 4. If none of these claims are present, default to "unknown".
            client_id = (
                claims.get("client_id")
                or claims.get("email")
                or claims.get("sub")
                or "unknown"
            )
            logger.debug(f"Client ID: {client_id}")

            # Validate expiration
            exp = claims.get("exp")
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
                claim_issuer = claims.get("iss")
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
                aud = claims.get("aud")

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
            scopes = self._extract_scopes(claims)
            logger.debug(f"Scopes extracted: {scopes}")
            logger.info(
                f"Token validated successfully for client {client_id} with scopes: {scopes} exp: {exp_str}, now: {now_str}"
            )
            return AccessToken(
                token=token,
                client_id=str(client_id),
                scopes=scopes,
                expires_at=int(exp) if exp else None,
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
