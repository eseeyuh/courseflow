# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""The migration history works from an empty database, in both directions."""

import asyncio
from dataclasses import dataclass

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import URL, text
from sqlalchemy.ext.asyncio import create_async_engine

from app.db import models  # noqa: F401  (registers every table on Base.metadata)
from app.db.base import Base
from tests.db_utils import downgrade, drop_database, migrate, recreate_database

MIGRATIONS_DATABASE = "courseflow_migrations_test"

EXPECTED_TABLES = {
    "alembic_version",
    "institutions",
    "courses",
    "resources",
    "resource_versions",
    "source_spans",
    "workflow_runs",
    "workflow_run_inputs",
}

EXPECTED_SOURCE_SPAN_CHECKS = {
    "ck_source_spans_locator_present",
    "ck_source_spans_page_positive",
    "ck_source_spans_slide_positive",
    "ck_source_spans_section_nonblank",
    "ck_source_spans_timestamp_nonnegative",
    "ck_source_spans_offsets_paired",
    "ck_source_spans_offsets_ordered",
    "ck_source_spans_excerpt_nonempty",
}


@dataclass(frozen=True)
class SchemaState:
    tables: set[str]
    extensions: set[str]
    source_span_checks: set[str]
    foreign_keys: set[str]
    version: str | None


async def _inspect(url: URL) -> SchemaState:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            tables = set(
                await conn.scalars(
                    text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
                )
            )
            extensions = set(await conn.scalars(text("SELECT extname FROM pg_extension")))
            checks = set(
                await conn.scalars(
                    text(
                        "SELECT conname FROM pg_constraint "
                        "WHERE conrelid = to_regclass('public.source_spans') AND contype = 'c'"
                    )
                )
            )
            fks = set(
                await conn.scalars(
                    text(
                        "SELECT c.conname FROM pg_constraint c "
                        "JOIN pg_namespace n ON n.oid = c.connamespace "
                        "WHERE n.nspname = 'public' AND c.contype = 'f'"
                    )
                )
            )
            version = None
            if "alembic_version" in tables:
                version = await conn.scalar(text("SELECT version_num FROM alembic_version"))
            return SchemaState(tables, extensions, checks, fks, version)
    finally:
        await engine.dispose()


def _state(url: URL) -> SchemaState:
    return asyncio.run(_inspect(url))


async def _schema_drift(url: URL) -> list[object]:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            return await conn.run_sync(
                lambda sync_conn: compare_metadata(
                    MigrationContext.configure(sync_conn), Base.metadata
                )
            )
    finally:
        await engine.dispose()


def test_models_match_migrated_schema(test_database_url: URL) -> None:
    """A model change without a matching migration fails here. (Alembic does not
    compare CHECK constraints; their names are asserted in the test below.)"""
    assert asyncio.run(_schema_drift(test_database_url)) == []


def test_upgrade_downgrade_upgrade_from_empty_database(test_database_url: URL) -> None:
    url = test_database_url.set(database=MIGRATIONS_DATABASE)
    recreate_database(url)
    try:
        assert _state(url).tables == set()

        migrate(url, "head")
        state = _state(url)
        assert state.tables == EXPECTED_TABLES
        assert "vector" in state.extensions
        assert state.source_span_checks == EXPECTED_SOURCE_SPAN_CHECKS
        assert "fk_resources_current_version" in state.foreign_keys
        assert state.version == "0001"

        downgrade(url, "base")
        state = _state(url)
        assert state.tables == {"alembic_version"}  # Alembic keeps its (empty) bookkeeping table
        assert state.version is None
        assert "vector" not in state.extensions
        assert state.foreign_keys == set()

        migrate(url, "head")
        state = _state(url)
        assert state.tables == EXPECTED_TABLES
        assert "vector" in state.extensions
        assert state.version == "0001"
    finally:
        drop_database(url)
