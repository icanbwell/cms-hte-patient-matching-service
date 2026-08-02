import logging
import time
from typing import override, Callable, Awaitable, cast, AsyncIterator

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse

logger = logging.getLogger(__name__)


class FastApiLoggingMiddleware(BaseHTTPMiddleware):
    """
    Middleware for logging requests and responses in FastAPI applications.
    """

    @override
    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        """
        Log the request and response details.

        Args:
            request (Request): The incoming request.
            call_next (Callable): The next middleware or endpoint to call.

        Returns:
            Response: The response from the endpoint.
        """

        # don't log health check requests
        if request.url.path == "/health":
            return await call_next(request)

        try:
            req_body = await request.json()
        except Exception:
            req_body = None

        start_time = time.perf_counter()
        response: Response = await call_next(request)
        process_time = time.perf_counter() - start_time
        # convert process_time to seconds
        process_time_in_secs = f"{process_time:.4f} secs"

        res_body_text: str = "No body"
        # if response is StreamingResponse, we need to read the body
        if "body_iterator" in response.__dict__:
            response1: StreamingResponse = cast(StreamingResponse, response)
            # body_iterator is declared as AsyncIterable, not AsyncIterator, so we
            # must call __aiter__() ourselves before we can pull items from it.
            original_iterator = response1.body_iterator.__aiter__()
            try:
                first_chunk: (
                    str | bytes | memoryview | None
                ) = await original_iterator.__anext__()
            except StopAsyncIteration:
                first_chunk = None

            # Only peek at the first chunk for logging purposes. Draining the
            # whole stream here (as the previous implementation did) would
            # buffer the entire response in memory before dispatch() returns,
            # defeating streaming (e.g. SSE) and risking large memory usage
            # for big responses. Instead, lazily rechain the peeked chunk with
            # the remainder of the original iterator so the client still
            # receives the response as a genuine stream.
            async def _rechain() -> AsyncIterator[str | bytes | memoryview]:
                if first_chunk is not None:
                    yield first_chunk
                async for section in original_iterator:
                    yield section

            response1.body_iterator = _rechain()

            if first_chunk is not None:
                # Turn response body object to string
                res_body_ = first_chunk
                res_body_text = (
                    res_body_.decode()
                    if isinstance(res_body_, bytes)
                    else str(res_body_)
                )
        else:
            if response.body:
                # For regular responses, we can access the body directly
                res_body2: list[bytes | memoryview] = [response.body]
                # Turn response body object to string
                if len(res_body2) > 0:
                    res_body_2 = res_body2[0]
                    res_body_text = (
                        res_body_2.decode()
                        if isinstance(res_body_2, bytes)
                        else str(res_body_2)
                    )

        if response.status_code >= 300:
            logger.error(
                f"Request: {request.method} | url: {request.url} | Headers: {request.headers} | Body: {req_body}"
            )
            logger.error(
                f"Response: {response.status_code} | time: {process_time_in_secs} | Body: {res_body_text} "
            )
        else:
            logger.debug(
                f"Request: {request.method} | url: {request.url} | Headers: {request.headers} | Body: {req_body}"
            )
            logger.debug(
                f"Response: {response.status_code} | time: {process_time_in_secs} | Body: {res_body_text} "
            )
        return response
