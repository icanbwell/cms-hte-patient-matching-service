from typing import Any

from fastmcp import FastMCP
from fastmcp.server.http import StarletteWithLifespan
from starlette.requests import Request
from starlette.responses import PlainTextResponse


class MathServerMCP:
    path: str = "/math_server"

    @classmethod
    def get_app(cls) -> StarletteWithLifespan:
        """
        Get the FastMCP application instance.

        Returns:
            FastMCP[Any]: The FastMCP application instance.
        """
        mcp: FastMCP[Any] = cls.get_mcp()
        return mcp.http_app(path="/")

    @classmethod
    def get_mcp(cls) -> FastMCP[Any]:
        """
        Get the FastMCP instance with defined tools and prompts.
        This method defines two tools: `add` and `multiply`, along with a prompt
        to configure the assistant's behavior. It also includes a custom health check route.
        The `add` tool performs addition, while the `multiply` tool intentionally
        returns an incorrect result to demonstrate the assistant's use of tools.
        The `configure_assistant` prompt sets the assistant's skills and instructs it
        to use only one tool at a time.
        The health check route returns a simple "OK" response to verify the server's status.

        :return: FastMCP instance with defined tools and prompts.
        """

        # If you want to do Auth
        # well_known_url = os.getenv("AUTH_WELL_KNOWN_URI")
        # token_verifier: TokenVerifier | None = (
        #     BearerAuthManager.get_mcp_token_verifier()
        #     if well_known_url or os.getenv("AUTH_JWKS_URI")
        #     else None
        # )
        # auth: AuthProvider | None = (
        #     RemoteAuthProvider(
        #         token_verifier=token_verifier,
        #         authorization_servers=[AnyHttpUrl(well_known_url)],
        #         resource_server_url="https://test/google_drive",
        #     )
        #     if token_verifier and well_known_url
        #     else None
        # )
        # mcp: FastMCP[Any] = FastMCP("GoogleDrive", auth=auth)
        mcp: FastMCP[Any] = FastMCP("Math")

        @mcp.tool()
        def add(a: int, b: int) -> int:
            """Add two numbers"""
            print(f"Adding numbers {a} + {b}")
            return a + b

        @mcp.tool()
        def multiply(a: int, b: int) -> int:
            """Multiply two numbers"""
            # Give wrong answer to ensure that the assistant uses the tool
            print(f"Multiplying numbers {a} * {b}")
            print(f"Multiplying numbers {a} * {b}")
            return a * b

        @mcp.prompt()
        def configure_assistant(skills: str) -> list[dict[str, str]]:
            return [
                {
                    "role": "assistant",
                    "content": f"You are a helpful assistant. You have the following skills: {skills}. Always use only one tool at a time.",
                },
            ]

        @mcp.custom_route("/health", methods=["GET"])
        async def health_check(request: Request) -> PlainTextResponse:
            return PlainTextResponse("OK")

        return mcp
