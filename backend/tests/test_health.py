# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import NoReturn

import anyio
import httpx
import pytest
from fastapi import FastAPI

from app.api.deps import get_engine

pytestmark = pytest.mark.anyio


# --- database available -----------------------------------------------------


async def test_readiness_200_when_database_available(client: httpx.AsyncClient) -> None:
    response = await client.get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready", "checks": {"database": "ok"}}


async def test_api_health_200_when_database_available(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/v1/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["environment"] == "test"
    assert body["checks"] == {"database": "ok"}
    assert body["version"]


async def test_liveness_200(client: httpx.AsyncClient) -> None:
    response = await client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "alive"}


# --- database unavailable ---------------------------------------------------


async def test_liveness_200_when_database_unavailable(
    client_db_down: httpx.AsyncClient,
) -> None:
    response = await client_db_down.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "alive"}


async def test_readiness_503_when_database_unavailable(
    client_db_down: httpx.AsyncClient,
) -> None:
    started = time.perf_counter()
    response = await client_db_down.get("/health/ready")
    elapsed = time.perf_counter() - started

    assert response.status_code == 503
    assert response.json() == {"status": "not_ready", "checks": {"database": "unavailable"}}
    assert elapsed < 3.0  # health timeout is 1 s in these settings


async def test_api_health_503_when_database_unavailable(
    client_db_down: httpx.AsyncClient,
) -> None:
    response = await client_db_down.get("/api/v1/health")

    assert response.status_code == 503
    assert response.json()["status"] == "unavailable"
    assert response.json()["checks"] == {"database": "unavailable"}


class _HangingEngine:
    """Stands in for an AsyncEngine whose database accepts TCP but never answers."""

    def __init__(self) -> None:
        self.entered = False

    @asynccontextmanager
    async def connect(self) -> AsyncIterator[NoReturn]:
        self.entered = True
        await asyncio.sleep(3600)
        raise AssertionError("unreachable")
        yield  # pragma: no cover


async def test_readiness_503_promptly_when_database_hangs(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    # Start from a HEALTHY app: if the override were not applied, this would be 200.
    app.state.settings = app.state.settings.model_copy(update={"db_health_timeout_seconds": 1.0})
    hanging = _HangingEngine()
    # dependency_overrides: every route depending on get_engine (directly or
    # through get_readiness) now receives the hanging fake.
    app.dependency_overrides[get_engine] = lambda: hanging

    started = time.perf_counter()
    # Fail fast instead of hanging the suite if the timeout ever regresses.
    with anyio.fail_after(5):
        response = await client.get("/health/ready")
    elapsed = time.perf_counter() - started

    assert hanging.entered
    assert response.status_code == 503
    assert 0.9 < elapsed < 2.5  # bounded by db_health_timeout_seconds=1.0
