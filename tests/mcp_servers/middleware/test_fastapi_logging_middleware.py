from typing import AsyncIterator

import pytest
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse

from patient_matching_service.mcp_servers.middleware.fastapi_logging_middleware import (
    FastApiLoggingMiddleware,
)


def _build_request(path: str = "/stream") -> Request:
    """Build a minimal ASGI Request for exercising dispatch() directly."""
    scope = {
        "type": "http",
        "method": "GET",
        "path": path,
        "headers": [],
        "query_string": b"",
        "server": ("test", 80),
        "client": ("test", 123),
        "scheme": "http",
    }
    return Request(scope)


@pytest.mark.asyncio
async def test_dispatch_only_pulls_one_chunk_and_preserves_the_rest() -> None:
    """
    Regression test for the streaming-buffer bug: the middleware used to fully
    drain body_iterator (via a list comprehension) before dispatch() returned,
    which defeats streaming responses (e.g. SSE) by buffering the entire
    response in memory up front.

    This test asserts that:
    1. Only ONE chunk is pulled from the underlying generator during dispatch().
    2. The remaining chunks are still available afterward via the rechained
       body_iterator on the returned response.
    """
    pulled_chunks: list[str] = []

    async def source() -> AsyncIterator[str]:
        for chunk in ("first-chunk", "second-chunk", "third-chunk"):
            pulled_chunks.append(chunk)
            yield chunk

    streaming_response = StreamingResponse(source(), media_type="text/event-stream")

    async def call_next(_: Request) -> Response:
        return streaming_response

    middleware = FastApiLoggingMiddleware(app=None)  # type: ignore[arg-type]
    request = _build_request()

    result = await middleware.dispatch(request, call_next)

    # Only the first chunk should have been pulled while dispatch() ran.
    assert pulled_chunks == ["first-chunk"]

    assert isinstance(result, StreamingResponse)
    remaining_chunks = [section async for section in result.body_iterator]

    # The peeked first chunk plus the untouched remainder must all still be
    # available for the actual ASGI response cycle to consume.
    assert remaining_chunks == ["first-chunk", "second-chunk", "third-chunk"]
    assert pulled_chunks == ["first-chunk", "second-chunk", "third-chunk"]


@pytest.mark.asyncio
async def test_dispatch_handles_empty_stream() -> None:
    """An empty streaming response must not raise and must yield no chunks."""

    async def source() -> AsyncIterator[str]:
        return
        yield  # pragma: no cover - makes this an async generator

    streaming_response = StreamingResponse(source(), media_type="text/event-stream")

    async def call_next(_: Request) -> Response:
        return streaming_response

    middleware = FastApiLoggingMiddleware(app=None)  # type: ignore[arg-type]
    request = _build_request()

    result = await middleware.dispatch(request, call_next)

    assert isinstance(result, StreamingResponse)
    remaining_chunks = [section async for section in result.body_iterator]
    assert remaining_chunks == []


@pytest.mark.asyncio
async def test_dispatch_health_check_bypasses_logging_and_streaming_logic() -> None:
    """The /health path should short-circuit before touching body_iterator."""

    async def source() -> AsyncIterator[str]:
        yield "ok"

    streaming_response = StreamingResponse(source(), media_type="text/plain")

    async def call_next(_: Request) -> Response:
        return streaming_response

    middleware = FastApiLoggingMiddleware(app=None)  # type: ignore[arg-type]
    request = _build_request(path="/health")

    result = await middleware.dispatch(request, call_next)

    assert result is streaming_response
