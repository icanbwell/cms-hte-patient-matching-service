import logging
import os
from typing import List, Dict, Any, cast

import httpx
from fastmcp.server.auth import TokenVerifier
from fastmcp.server.auth.providers.jwt import RSAKeyPair
from fastmcp.server.dependencies import AccessToken
from httpx import ConnectError

from patient_matching_service.mcp_servers.auth.jwt_verifier_with_logging import (
    JwtVerifierWithLogging,
)

logger = logging.getLogger(__name__)


class BearerAuthManager:
    """
    Manages bearer token authentication for MCP servers.
    """

    JWKS_URI: str | None = os.getenv("AUTH_JWKS_URI")
    WELL_KNOWN_URI: str | None = os.getenv("AUTH_WELL_KNOWN_URI")
    AUDIENCE: str | None = os.getenv("AUTH_AUDIENCE")
    ISSUER: str | None = os.getenv("AUTH_ISSUER")

    @classmethod
    def get_mcp_token_verifier(
        cls, *, retries: int = 10, delay: float = 10.0
    ) -> TokenVerifier:
        """
        Returns a BearerAuthProvider instance configured with the bearer token.
        https://gofastmcp.com/servers/auth/token-verification

        :return: An instance of TokenVerifier.
        """

        assert cls.JWKS_URI is not None or cls.WELL_KNOWN_URI, (
            "AUTH_JWKS_URI or AUTH_WELL_KNOWN_URI environment variable must be set"
        )

        if cls.WELL_KNOWN_URI and not cls.JWKS_URI:
            cls.JWKS_URI, cls.ISSUER = cls.get_jwks_uri_and_issuer(
                retries=retries,
                delay=delay,
            )

        assert cls.JWKS_URI is not None, "AUTH_JWKS_URI must be set"

        auth = cls.create_mcp_token_verifier(
            jwks_uri=cls.JWKS_URI,
            issuer=cls.ISSUER,
            audience=cls.AUDIENCE,
            algorithm=None,  # Use default algorithm
        )
        return auth

    @classmethod
    def create_mcp_token_verifier(
        cls,
        *,
        jwks_uri: str,
        issuer: str | None,
        algorithm: str | None,
        audience: str | None,
    ) -> TokenVerifier:
        """
        Creates a TokenVerifier instance with the specified parameters.

        :param jwks_uri: The URI to fetch the JSON Web Key Set (JWKS).
        :param issuer: The issuer of the JWT.
        :param algorithm: The algorithm used for signing the JWT.
        :param audience: The intended audience of the JWT.
        :return: An instance of TokenVerifier.
        """
        return JwtVerifierWithLogging(
            jwks_uri=jwks_uri,
            issuer=issuer,
            algorithm=algorithm,
            audience=audience,
        )

    @classmethod
    def create_test_token(
        cls,
        *,
        subject: str,
        key_pair: RSAKeyPair,
        kid: str,
        issuer: str | None,
        audience: str | None,
        scopes: List[str],
    ) -> str:
        """
        This method is used to create a JWT token for testing purposes.


        :param subject: The subject of the token, typically the user ID or email.
        :param key_pair: The RSAKeyPair used to sign the token.
        :param kid: The key ID for the token.
        :param issuer: The issuer of the token.
        :param audience: The intended audience of the token.
        :param scopes: A list of scopes for the token.
        :raises ValueError: If the key_pair is not provided.
        :raises TypeError: If the subject, issuer, audience, or kid is not a string.
        :return: A JWT token as a string.
        """
        assert issuer is not None, "Issuer must be provided."
        # Generate a token for testing
        test_token = key_pair.create_token(
            subject=subject,
            issuer=issuer,
            audience=audience,
            scopes=scopes,
            kid=kid,
        )
        return test_token

    @classmethod
    def get_test_token(
        cls, *, subject: str, key_pair: RSAKeyPair, kid: str, scopes: List[str]
    ) -> str:
        """
        Generates a test bearer token for authentication.
        This method is a convenience wrapper around `create_test_token`.
        It uses the default issuer and audience defined in the class.

        :param subject: The subject of the token, typically the user ID or email.
        :param key_pair: The RSAKeyPair used to sign the token.
        :param kid: The key ID for the token.
        :param scopes: A list of scopes for the token.
        :return: A JWT token as a string.
        """
        assert cls.JWKS_URI is not None, (
            "AUTH_JWKS_URI environment variable must be set"
        )

        return cls.create_test_token(
            subject=subject,
            key_pair=key_pair,
            kid=kid,
            issuer=cls.ISSUER,
            audience=cls.AUDIENCE,
            scopes=scopes,
        )

    @classmethod
    def fetch_well_known_config(cls, *, retries: int, delay: float) -> Dict[str, Any]:
        """
        Fetches the OpenID Connect discovery document and returns its contents as a dict.
        Retries the request up to `retries` times, waiting `delay` seconds between attempts.
        Args:
            retries (int): Number of times to retry on failure.
            delay (float): Seconds to wait between retries.
        Returns:
            dict: The parsed discovery document.
        Raises:
            ValueError: If the document cannot be fetched or parsed after all retries.
        """
        import time

        if not cls.WELL_KNOWN_URI:
            raise ValueError("well_known_uri is not set")
        last_exception: Exception | None = None
        for attempt in range(1, retries + 1):
            with httpx.Client() as client:
                try:
                    logger.info(
                        f"Fetching OIDC discovery document from {cls.WELL_KNOWN_URI} (attempt {attempt}/{retries})"
                    )
                    response = client.get(cls.WELL_KNOWN_URI)
                    response.raise_for_status()
                    return cast(Dict[str, Any], response.json())
                except httpx.HTTPStatusError as e:
                    last_exception = ValueError(
                        f"Failed to fetch OIDC discovery document from {cls.WELL_KNOWN_URI} with status {e.response.status_code} : {e}"
                    )
                except ConnectError as e:
                    last_exception = ConnectionError(
                        f"Failed to connect to OIDC discovery document: {cls.WELL_KNOWN_URI}: {e}"
                    )
            if attempt < retries:
                logger.warning(f"Retrying in {delay} seconds...")
                time.sleep(delay)
        # If we get here, all retries failed
        raise (
            last_exception
            if last_exception
            else ValueError(
                f"Error fetching OIDC discovery document after {retries} retries"
            )
        ) from last_exception

    @classmethod
    def get_jwks_uri_and_issuer(cls, *, retries: int, delay: float) -> tuple[str, str]:
        """
        Retrieves the JWKS URI and issuer from the well-known OpenID Connect configuration.
        Returns:
            tuple: (jwks_uri, issuer)
        Raises:
            ValueError: If required fields are missing.
        """
        config = cls.fetch_well_known_config(
            retries=retries,
            delay=delay,
        )
        jwks_uri = config.get("jwks_uri")
        issuer = config.get("issuer")
        if not jwks_uri or not issuer:
            raise ValueError("jwks_uri or issuer not found in well-known configuration")
        return jwks_uri, issuer

    @classmethod
    def extract_user_email(cls, access_token: AccessToken | None) -> str | None:
        """
        Extract the user email from the access token in the context.
        """
        if access_token is None:
            raise ValueError("Access token is required to extract user email.")
        # override for testing purposes
        email_override = os.getenv("TEST_GOOGLE_DRIVE_EMAIL")
        if email_override:
            return email_override
        return cast(str | None, access_token.client_id)
