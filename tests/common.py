from contextlib import asynccontextmanager
from typing import AsyncGenerator, Any

import httpx
from asgi_lifespan import LifespanManager
from httpx import AsyncClient

from patient_matching_service.api import app


@asynccontextmanager
async def create_async_client_unopened() -> AsyncGenerator[AsyncClient, Any]:
    async with LifespanManager(app) as manager:
        yield httpx.AsyncClient(
            transport=httpx.ASGITransport(app=manager.app), base_url="http://test"
        )
