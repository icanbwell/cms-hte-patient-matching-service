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

