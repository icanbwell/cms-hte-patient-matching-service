from collections.abc import AsyncGenerator, Generator
from typing import Any

import httpx
import pytest
from asgi_lifespan import LifespanManager
from fastapi.testclient import TestClient
from httpx import AsyncClient

from patient_matching_service.api import app
from tests.common import create_async_client_unopened


@pytest.fixture
async def async_client() -> AsyncGenerator[httpx.AsyncClient, None]:
    async with (
        LifespanManager(app) as manager,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=manager.app), base_url="http://test"
        ) as client,
    ):
        yield client


@pytest.fixture(scope="function")
async def async_client_unopened() -> AsyncGenerator[AsyncClient, Any]:
    async with create_async_client_unopened() as client:
        yield client


@pytest.fixture
def sync_client() -> Generator[TestClient, None, None]:
    client = TestClient(app)
    yield client  # Use `yield` to ensure any teardown can happen after the test runs
