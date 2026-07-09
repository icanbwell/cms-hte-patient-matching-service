from typing import Any, List
from urllib.parse import urljoin

import httpx
from fastmcp import Client
from fastmcp.client import StreamableHttpTransport
from fastmcp.client.client import CallToolResult
from mcp.types import (
    Prompt,
    Resource,
    Tool,
    TextContent,
    ImageContent,
    AudioContent,
    ResourceLink,
    EmbeddedResource,
)

from patient_matching_service.mcp_servers.math_server.math_server import MathServerMCP


async def test_math_mcp_agent_via_fastmcp(
    async_client_unopened: httpx.AsyncClient,
) -> None:
    print()
    url: str = urljoin("http://test", MathServerMCP.path + "/")

    def create_mcp_http_client(
        headers: dict[str, str] | None = None,
        timeout: httpx.Timeout | None = None,
        auth: httpx.Auth | None = None,
        **kwargs: Any,
    ) -> httpx.AsyncClient:
        if headers:
            async_client_unopened.headers.update(headers)
        if auth:
            async_client_unopened.auth = auth
        if timeout:
            async_client_unopened.timeout = timeout
        return async_client_unopened

    transport: StreamableHttpTransport = StreamableHttpTransport(
        url=url,
        httpx_client_factory=create_mcp_http_client,
    )

    # HTTP server
    client: Client[Any] = Client(transport=transport)
    async with client:
        # Basic server interaction
        await client.ping()

        # List available operations
        tools: List[Tool] = await client.list_tools()
        assert tools is not None
        resources: List[Resource] = await client.list_resources()
        assert resources is not None
        prompts: List[Prompt] = await client.list_prompts()
        assert prompts is not None

        # Execute operations
        result: CallToolResult = await client.call_tool("add", {"a": 12, "b": 8})
        print(result)
        content: (
            TextContent | ImageContent | AudioContent | ResourceLink | EmbeddedResource
        ) = result.content[0]
        assert content is not None
        assert isinstance(content, TextContent)
        assert content.text == "20"
