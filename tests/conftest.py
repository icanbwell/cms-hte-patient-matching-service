from typing import AsyncGenerator, Generator, Any

import httpx
from asgi_lifespan import LifespanManager
from httpx import AsyncClient

from tests.common import create_async_client_unopened

import logging
import os

import pytest
from fastapi.testclient import TestClient

from patient_matching_service.api import app


@pytest.fixture
def graphql_client() -> Generator[TestClient, None, None]:
    # app.config["TESTING"] = True

    # Get log level from environment variable
    log_level = os.getenv("LOG_LEVEL", "INFO").upper()

    # Set up basic configuration for logging
    logging.basicConfig(level=getattr(logging, log_level))

    client = TestClient(app)
    yield client  # Use `yield` to ensure any teardown can happen after the test runs


@pytest.fixture
async def async_client() -> AsyncGenerator[httpx.AsyncClient, None]:
    async with LifespanManager(app) as manager:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=manager.app), base_url="http://test"
        ) as client:
            yield client


@pytest.fixture(scope="function")
async def async_client_unopened() -> AsyncGenerator[AsyncClient, Any]:
    async with create_async_client_unopened() as client:
        yield client


@pytest.fixture
def sync_client() -> Generator[TestClient, None, None]:
    client = TestClient(app)
    yield client  # Use `yield` to ensure any teardown can happen after the test runs
