# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Test fixtures.

Database-backed tests run against a dedicated PostgreSQL database
(``courseflow_test`` by default) on the same server as development, created
on demand. Start the server first: ``docker compose up -d db``.

Each test builds its own app and runs the real lifespan, so every test gets
its own engine bound to its own event loop.
"""

import asyncio
import os
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest
from fastapi import FastAPI
from pydantic import ValidationError
from sqlalchemy import URL, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import Settings
from app.main import create_app

TEST_DATABASE_NAME = "courseflow_test"
# Nothing listens on port 1: a real, fast-failing "database is down".
UNREACHABLE_DATABASE_URL = "postgresql+asyncpg://courseflow:unused@127.0.0.1:1/courseflow"
_SAFE_DB_NAME = re.compile(r"^[a-z_][a-z0-9_]*_test$")


def _settings(**overrides: object) -> Settings:
    # _env_file=None: tests never read a developer's local .env implicitly.
    return Settings(_env_file=None, app_env="test", log_level="DEBUG", **overrides)  # type: ignore[arg-type]


def _resolve_test_database_url() -> URL:
    explicit = os.environ.get("TEST_DATABASE_URL")
    if explicit:
        url = make_url(explicit)
    else:
        try:
            configured = Settings()  # type: ignore[call-arg]  # env vars, then repo-root .env
        except ValidationError as exc:
            pytest.fail(
                "No database configuration for tests. Copy .env.example to .env "
                f"or set TEST_DATABASE_URL.\n{exc}"
            )
        url = make_url(configured.database_url.get_secret_value()).set(database=TEST_DATABASE_NAME)
    # Guard: tests wipe data, so never point them at a non-test database.
    if not url.database or not _SAFE_DB_NAME.match(url.database):
        pytest.fail(
            f"Test database name must match {_SAFE_DB_NAME.pattern!r}, got {url.database!r}"
        )
    return url


async def _ensure_database_exists(url: URL) -> None:
    admin = create_async_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as conn:
            exists = await conn.scalar(
                text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": url.database}
            )
            if not exists:
                # Identifier cannot be a bind parameter; it was validated above.
                await conn.execute(text(f'CREATE DATABASE "{url.database}"'))
    finally:
        await admin.dispose()


@pytest.fixture(scope="session")
def test_database_url() -> str:
    """Dedicated test database URL; the database is created if missing."""
    url = _resolve_test_database_url()
    try:
        asyncio.run(_ensure_database_exists(url))
    except OSError as exc:
        pytest.fail(
            f"PostgreSQL not reachable at {url.render_as_string(hide_password=True)} "
            f"({exc}). Start it with: docker compose up -d db"
        )
    return url.render_as_string(hide_password=False)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def settings(test_database_url: str) -> Settings:
    return _settings(database_url=test_database_url)


@pytest.fixture
def settings_db_down() -> Settings:
    return _settings(
        database_url=UNREACHABLE_DATABASE_URL,
        db_connect_timeout_seconds=1.0,
        db_health_timeout_seconds=1.0,
    )


@asynccontextmanager
async def _serve(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """Run the real lifespan (ASGITransport alone does not) and yield a client."""
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    return create_app(settings)


@pytest.fixture
def app_db_down(settings_db_down: Settings) -> FastAPI:
    return create_app(settings_db_down)


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with _serve(app) as client:
        yield client


@pytest.fixture
async def client_db_down(app_db_down: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with _serve(app_db_down) as client:
        yield client
