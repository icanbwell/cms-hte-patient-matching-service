import logging
import os
from typing import Optional

# Since graphql_sync is used, assuming a similar synchronous approach is acceptable
from ariadne import graphql
from ariadne.explorer import ExplorerPlayground
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, HTMLResponse
from prometheus_fastapi_instrumentator import Instrumentator
from contextlib import asynccontextmanager, AbstractAsyncContextManager
from typing import AsyncGenerator, AsyncContextManager, Callable, Mapping, Any

from patient_matching_service.api_schema import ApiSchema
from patient_matching_service.filters.endpoint_filter import EndpointFilter
from patient_matching_service.mcp_servers.math_server.math_server import MathServerMCP
from patient_matching_service.mcp_servers.middleware.fastapi_logging_middleware import (
    FastApiLoggingMiddleware,
)

# Get log level from environment variable
log_level = os.getenv("LOG_LEVEL", "INFO").upper()

# Set up basic configuration for logging
logging.basicConfig(level=getattr(logging, log_level))
logger = logging.getLogger(__name__)

# disable logging calls to /health endpoint
uvicorn_logger = logging.getLogger("uvicorn.access")
uvicorn_logger.addFilter(EndpointFilter(path="/health"))


# Your existing lifespan
@asynccontextmanager
async def app_lifespan(app1: FastAPI) -> AsyncGenerator[None, None]:
    # Startup
    logger.info("Starting up the app...")
    logger.info(f"Log level set to: {log_level}")
    # Initialize database, cache, etc.

    yield
    # Shutdown
    logger.info("Shutting down the app...")


def create_composite_lifespan(
    *lifespan_funcs: Callable[[Any], AbstractAsyncContextManager[None, bool | None]]
    | Callable[[Any], AbstractAsyncContextManager[Mapping[str, Any], bool | None]],
) -> Callable[[FastAPI], AsyncContextManager[None]]:
    @asynccontextmanager
    async def composite_lifespan(app1: FastAPI) -> AsyncGenerator[None, None]:
        # Create a list to track active context managers
        context_managers = []

        try:
            # Enter all lifespan context managers
            for lifespan_func in lifespan_funcs:
                context_manager = lifespan_func(app1)
                await context_manager.__aenter__()
                context_managers.append(context_manager)

            yield

        finally:
            # Exit all context managers in reverse order
            for context_manager in reversed(context_managers):
                await context_manager.__aexit__(None, None, None)

    return composite_lifespan


math_server_mcp_app = MathServerMCP.get_app()

app = FastAPI(
    lifespan=create_composite_lifespan(
        math_server_mcp_app.lifespan,
        app_lifespan,
    )
)

app.mount(MathServerMCP.path, math_server_mcp_app)

app.add_middleware(FastApiLoggingMiddleware)


PLAYGROUND_HTML: Optional[str] = ExplorerPlayground(
    title="patient_matching_service"
).html(None)

# Set up CORS middleware; adjust parameters as needed
# noinspection PyTypeChecker
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# noinspection SpellCheckingInspection
instrumentator = Instrumentator()
instrumentator.instrument(app).expose(app)


@app.get("/", response_class=HTMLResponse)
def hello() -> str:
    return "Use /graphql endpoint to test"


@app.get("/health")
def health() -> str:
    return "OK"


@app.get("/graphql", response_class=HTMLResponse)
def graphql_playground() -> str:
    assert PLAYGROUND_HTML is not None
    return PLAYGROUND_HTML


@app.post("/graphql")
async def graphql_server(request: Request) -> JSONResponse:
    data = await request.json()
    logger.info(
        f"API call [{request.client.host if request.client else None}] {data!r}"
    )

    success, result = await graphql(
        ApiSchema.schema, data, context_value=request, debug=app.debug
    )

    status_code = 200 if success else 400
    return JSONResponse(result, status_code=status_code)


logger.info("FastAPI app created")
