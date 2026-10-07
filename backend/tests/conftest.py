# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Test fixtures.

Database-backed tests run against a dedicated PostgreSQL database
(``courseflow_test`` by default) on the same server as development. It is
dropped, recreated from zero and migrated to ``head`` with Alembic once per
session, exactly as production databases are migrated. Start the server first:
``docker compose up -d db``.

Each test builds its own app/engine, bound to that test's own event loop.
"""

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest
from fastapi import FastAPI
from pydantic import ValidationError
from sqlalchemy import URL, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from app.core.config import Settings
from app.db import models  # noqa: F401  (registers every table on Base.metadata)
from app.db.base import Base
from app.main import create_app
from tests.db_utils import migrate, recreate_database, require_test_database_name

TEST_DATABASE_NAME = "courseflow_test"
# Nothing listens on port 1: a real "database is down".
UNREACHABLE_DATABASE_URL = "postgresql+asyncpg://courseflow:unused@127.0.0.1:1/courseflow"


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
    try:
        require_test_database_name(url)
    except ValueError as exc:
        pytest.fail(str(exc))
    return url


@pytest.fixture(scope="session")
def test_database_url() -> URL:
    """Dedicated test database, dropped and recreated from zero, then migrated to head
    once per session: schema tests always run against the CURRENT migrations."""
    url = _resolve_test_database_url()
    where = url.render_as_string(hide_password=True)
    try:
        recreate_database(url)
    except OSError as exc:
        pytest.fail(
            f"PostgreSQL not reachable at {where} ({exc}). Start it with: docker compose up -d db"
        )
    except DBAPIError as exc:
        pytest.fail(
            f"PostgreSQL at {where} rejected the connection ({exc.orig!r}). If the password "
            "is wrong: POSTGRES_* values only apply when the data volume is first created "
            "(see README, Troubleshooting)."
        )
    try:
        migrate(url)
    except Exception as exc:  # noqa: BLE001 - re-raised as a clear test-setup failure
        pytest.fail(f"Migrating the test database {where} to head failed: {exc!r}")
    return url


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def settings(test_database_url: URL) -> Settings:
    return _settings(database_url=test_database_url.render_as_string(hide_password=False))


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


async def _truncate_all_tables(engine: AsyncEngine, url: URL) -> None:
    require_test_database_name(url)  # never wipe a non-test database
    tables = ", ".join(f'"{table.name}"' for table in Base.metadata.sorted_tables)
    async with engine.begin() as connection:
        await connection.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))


@pytest.fixture
async def committed_session(test_database_url: URL) -> AsyncIterator[AsyncSession]:
    """A session whose commits are real, so data is visible to the app's own
    per-request sessions (HTTP round-trip tests). All tables are truncated
    before and after the test, so it starts empty and leaves nothing behind."""
    engine = create_async_engine(test_database_url)
    try:
        await _truncate_all_tables(engine, test_database_url)
        async with AsyncSession(engine, expire_on_commit=False) as session:
            yield session
    finally:
        await _truncate_all_tables(engine, test_database_url)
        await engine.dispose()


@pytest.fixture
async def db_session(test_database_url: URL) -> AsyncIterator[AsyncSession]:
    """A session inside an outer transaction that is always rolled back,
    so schema tests leave no rows behind. Commits become savepoints."""
    engine = create_async_engine(test_database_url)
    try:
        async with engine.connect() as connection:
            outer = await connection.begin()
            session = AsyncSession(
                bind=connection,
                expire_on_commit=False,
                join_transaction_mode="create_savepoint",
            )
            try:
                yield session
            finally:
                await session.close()
                await outer.rollback()
    finally:
        await engine.dispose()
