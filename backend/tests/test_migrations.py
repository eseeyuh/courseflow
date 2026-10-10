# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""The migration history works from an empty database, in both directions."""

import asyncio
from collections.abc import Iterator
from dataclasses import dataclass

import pytest
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
    "model_calls",
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
    "ck_source_spans_excerpt_matches_offsets",
    "ck_source_spans_locator_fits_kind",
    "ck_source_spans_ordinal_nonnegative",
    "ck_source_spans_block_kind",
}

# Added by 0003 on the other two provenance tables.
EXPECTED_0003_CHECKS = {
    "ck_resources_source_uri_nonblank",
    "ck_resource_versions_raw_object_ref_matches_hash",
    "ck_resource_versions_media_type",
    "ck_resource_versions_byte_size_nonnegative",
    "ck_resource_versions_display_name_nonblank",
    "ck_resource_versions_parser_name_nonblank",
    "ck_resource_versions_parser_version_nonblank",
    "ck_resource_versions_extracted_text_nonempty",
    "ck_resource_versions_text_hash_matches_text",
}
EXPECTED_0003_UNIQUES = {"uq_resources_course_source_uri", "uq_source_spans_version_ordinal"}


def _public_constraints(contype: str) -> str:
    return (
        "SELECT c.conname FROM pg_constraint c "
        "JOIN pg_namespace n ON n.oid = c.connamespace "
        f"WHERE n.nspname = 'public' AND c.contype = '{contype}'"
    )


@dataclass(frozen=True)
class SchemaState:
    tables: set[str]
    extensions: set[str]
    source_span_checks: set[str]
    checks: set[str]
    uniques: set[str]
    indexes: set[str]
    version_columns: set[str]
    source_uri_nullable: bool | None
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
            span_checks = set(
                await conn.scalars(
                    text(
                        "SELECT conname FROM pg_constraint "
                        "WHERE conrelid = to_regclass('public.source_spans') AND contype = 'c'"
                    )
                )
            )
            checks = set(await conn.scalars(text(_public_constraints("c"))))
            uniques = set(await conn.scalars(text(_public_constraints("u"))))
            indexes = set(
                await conn.scalars(
                    text("SELECT indexname FROM pg_indexes WHERE schemaname = 'public'")
                )
            )
            version_columns = set(
                await conn.scalars(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema = 'public' AND table_name = 'resource_versions'"
                    )
                )
            )
            source_uri_nullable = await conn.scalar(
                text(
                    "SELECT is_nullable = 'YES' FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = 'resources' "
                    "AND column_name = 'source_uri'"
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
            return SchemaState(
                tables=tables,
                extensions=extensions,
                source_span_checks=span_checks,
                checks=checks,
                uniques=uniques,
                indexes=indexes,
                version_columns=version_columns,
                source_uri_nullable=source_uri_nullable,
                foreign_keys=fks,
                version=version,
            )
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
        assert state.version == "0003"

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
        assert state.version == "0003"
    finally:
        drop_database(url)


# --- 0003: ingestion provenance ---------------------------------------------------

NEW_VERSION_COLUMNS = {
    "raw_object_ref",
    "media_type",
    "byte_size",
    "display_name",
    "parser_name",
    "parser_version",
    "extracted_text",
    "text_hash",
}


@pytest.fixture
def db_at_0002(test_database_url: URL) -> Iterator[URL]:
    """A fresh database migrated only to 0002, for exercising 0003 on its own."""
    url = test_database_url.set(database=MIGRATIONS_DATABASE)
    recreate_database(url)
    migrate(url, "0002")
    try:
        yield url
    finally:
        drop_database(url)


def _execute(url: URL, statements: list[str]) -> None:
    async def run() -> None:
        engine = create_async_engine(url)
        try:
            async with engine.begin() as conn:
                for statement in statements:
                    await conn.execute(text(statement))
        finally:
            await engine.dispose()

    asyncio.run(run())


def _assert_at_0002(state: SchemaState) -> None:
    assert state.version == "0002"
    assert "raw_text_ref" in state.version_columns
    assert not NEW_VERSION_COLUMNS & state.version_columns
    assert not EXPECTED_0003_CHECKS & state.checks
    assert not EXPECTED_0003_UNIQUES & state.uniques
    assert "ix_source_spans_resource_version_id" in state.indexes
    assert state.source_uri_nullable is True


def _assert_at_0003(state: SchemaState) -> None:
    assert state.version == "0003"
    assert "raw_text_ref" not in state.version_columns
    assert NEW_VERSION_COLUMNS <= state.version_columns
    assert EXPECTED_0003_CHECKS <= state.checks
    assert state.source_span_checks == EXPECTED_SOURCE_SPAN_CHECKS
    assert EXPECTED_0003_UNIQUES <= state.uniques
    assert "ix_source_spans_resource_version_id" not in state.indexes
    assert state.source_uri_nullable is False


def test_0003_upgrade_downgrade_upgrade(db_at_0002: URL) -> None:
    _assert_at_0002(_state(db_at_0002))

    migrate(db_at_0002, "0003")
    _assert_at_0003(_state(db_at_0002))

    downgrade(db_at_0002, "0002")
    _assert_at_0002(_state(db_at_0002))

    migrate(db_at_0002, "0003")
    _assert_at_0003(_state(db_at_0002))


# Rows written with the 0002 schema; each refusal case adds what it needs.
INSTITUTION = "00000000-0000-0000-0000-000000000001"
COURSE_1 = "00000000-0000-0000-0000-0000000000c1"
COURSE_2 = "00000000-0000-0000-0000-0000000000c2"
RESOURCE = "00000000-0000-0000-0000-0000000000a1"
VERSION = "00000000-0000-0000-0000-0000000000b1"

BASE_ROWS = [
    f"INSERT INTO institutions (id, name, lms_type) VALUES ('{INSTITUTION}', 'N', 'demo')",
    (
        "INSERT INTO courses (id, institution_id, title) VALUES "
        f"('{COURSE_1}', '{INSTITUTION}', 'C1'), ('{COURSE_2}', '{INSTITUTION}', 'C2')"
    ),
]


def _resource(course: str, source_uri_sql: str, resource_id: str | None = None) -> str:
    """An INSERT for one resource; ``source_uri_sql`` is a SQL literal (e.g. NULL)."""
    id_column, id_value = ("id, ", f"'{resource_id}', ") if resource_id else ("", "")
    return (
        f"INSERT INTO resources ({id_column}course_id, type, title, source_uri) "
        f"VALUES ({id_value}'{course}', 'brief', 'Brief', {source_uri_sql})"
    )


A_RESOURCE = _resource(COURSE_1, "'upload:brief.pdf'", RESOURCE)
A_VERSION = (
    "INSERT INTO resource_versions (id, resource_id, version_number, content_hash) "
    f"VALUES ('{VERSION}', '{RESOURCE}', 1, '{'a' * 64}')"
)
A_SPAN = (
    "INSERT INTO source_spans (resource_version_id, page_number, excerpt) "
    f"VALUES ('{VERSION}', 1, 'Submit by Friday')"
)
UPLOAD_A = "'upload:a.pdf'"


@pytest.mark.parametrize(
    ("rows", "message"),
    [
        ([A_RESOURCE, A_VERSION], "resource_versions has 1 row"),
        ([A_RESOURCE, A_VERSION, A_SPAN], "source_spans has 1 row"),
        ([_resource(COURSE_1, "NULL")], "NULL or blank source_uri"),
        ([_resource(COURSE_1, "''")], "NULL or blank source_uri"),
        ([_resource(COURSE_1, "'   '")], "NULL or blank source_uri"),
        (
            [_resource(COURSE_1, UPLOAD_A), _resource(COURSE_1, UPLOAD_A)],
            r"1 \(course_id, source_uri\) pair\(s\) are shared",
        ),
    ],
    ids=["versions", "spans", "null-uri", "empty-uri", "blank-uri", "duplicate-uri"],
)
def test_0003_refuses_data_it_cannot_convert(
    db_at_0002: URL, rows: list[str], message: str
) -> None:
    _execute(db_at_0002, BASE_ROWS + rows)

    with pytest.raises(RuntimeError, match=message):
        migrate(db_at_0002, "0003")

    # Refused before any DDL: still a complete 0002 schema.
    _assert_at_0002(_state(db_at_0002))


def test_0003_keeps_existing_resources_with_valid_source_uris(db_at_0002: URL) -> None:
    _execute(
        db_at_0002,
        BASE_ROWS
        + [
            _resource(COURSE_1, UPLOAD_A),
            _resource(COURSE_1, "'upload:b.pdf'"),
            _resource(COURSE_2, UPLOAD_A),  # same URI in a different course
        ],
    )

    migrate(db_at_0002, "0003")

    _assert_at_0003(_state(db_at_0002))
    assert _scalar(db_at_0002, "SELECT count(*) FROM resources") == 3


def _scalar(url: URL, statement: str) -> object:
    async def run() -> object:
        engine = create_async_engine(url)
        try:
            async with engine.connect() as conn:
                return await conn.scalar(text(statement))
        finally:
            await engine.dispose()

    return asyncio.run(run())
