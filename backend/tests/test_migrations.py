# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""The migration history works from an empty database, in both directions."""

import asyncio
import hashlib
from collections.abc import Iterator
from dataclasses import dataclass

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import URL, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from app.db import models  # noqa: F401  (registers every table on Base.metadata)
from app.db.base import Base
from app.db.models import CHUNK_TEXT_TSV_EXPRESSION
from tests.db_utils import (
    alembic_config,
    downgrade,
    drop_database,
    migrate,
    recreate_database,
)

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
    "chunks",
    "embedding_spaces",
    "embeddings",
    "chunk_embeddings",
    "assessments",
    "assessment_deadlines",
    "evidence_links",
    "assessment_requirements",
    "obligations",
    "material_mappings",
    "dependency_edges",
    "conflicts",
    "conflict_sides",
    "study_states",
    "study_plans",
    "study_tasks",
    "change_events",
}
HEAD = "0012"

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
        assert state.version == HEAD

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
        assert state.version == HEAD
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
HASH_A = "a" * 64

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


def test_0003_refuses_offline_sql_mode(test_database_url: URL) -> None:
    """--sql renders without a connection, so the data checks cannot run."""
    url = test_database_url.set(database=MIGRATIONS_DATABASE)
    with pytest.raises(RuntimeError, match="preconditions need a live database"):
        command.upgrade(alembic_config(url), "0002:0003", sql=True)


# --- 0004: retrieval layer and ModelCall.call_kind -----------------------------------

RETRIEVAL_TABLES = {"chunks", "embedding_spaces", "embeddings", "chunk_embeddings"}
EXPECTED_0004_CHECKS = {
    "ck_model_calls_model_call_kind",
    "ck_model_calls_embedding_has_no_role",
    "ck_chunks_chunker_version_nonblank",
    "ck_chunks_ordinal_nonnegative",
    "ck_chunks_offsets_ordered",
    "ck_chunks_span_range_ordered",
    "ck_chunks_text_matches_offsets",
    "ck_chunks_text_hash_matches_text",
    "ck_chunks_estimated_tokens_positive",
    "ck_embedding_spaces_provider_valid",
    "ck_embedding_spaces_model_valid",
    "ck_embedding_spaces_space_version_valid",
    "ck_embedding_spaces_dimensions_in_range",
    "ck_embeddings_input_format_versioned",
    "ck_embeddings_input_hash_sha256_hex",
    "ck_embeddings_vector_dims_match",
    "ck_embeddings_vector_nonzero",
    "ck_chunk_embeddings_document_format_only",
}
EXPECTED_0004_UNIQUES = {
    "uq_chunks_version_chunker_ordinal",
    "uq_embedding_spaces_identity",
    "uq_embedding_spaces_id_dimensions",
    "uq_embeddings_cache_key",
    "uq_embeddings_id_space_format",
    "uq_chunk_embeddings_chunk_space_format",
}
EXPECTED_0004_FKS = {
    "fk_chunks_resource_version_id_resource_versions",
    "fk_chunks_first_span",
    "fk_chunks_last_span",
    "fk_embeddings_space",
    "fk_embeddings_model_call_id_model_calls",
    "fk_chunk_embeddings_chunk_id_chunks",
    "fk_chunk_embeddings_embedding",
}

# Pre-existing ingestion data written with the 0003 schema: a current version with two spans
# and a recorded chat call (no call_kind column yet).
EXISTING_TEXT = "Submit by Friday.\n\nWorth 40%."
EXISTING_HASH = hashlib.sha256(EXISTING_TEXT.encode()).hexdigest()
EXISTING_ROWS = [
    *BASE_ROWS,
    A_RESOURCE,
    (
        "INSERT INTO resource_versions (id, resource_id, version_number, content_hash, "
        "raw_object_ref, media_type, byte_size, display_name, parser_name, parser_version, "
        "extracted_text, text_hash) VALUES "
        f"('{VERSION}', '{RESOURCE}', 1, '{HASH_A}', 'sha256:{HASH_A}', 'application/pdf', "
        f"10, 'brief.pdf', 'pdf', '1', E'Submit by Friday.\\n\\nWorth 40%.', '{EXISTING_HASH}')"
    ),
    f"UPDATE resources SET current_version_id = '{VERSION}' WHERE id = '{RESOURCE}'",
    (
        "INSERT INTO source_spans (resource_version_id, ordinal, block_kind, page_number, "
        f"start_offset, end_offset, excerpt) VALUES ('{VERSION}', 0, 'page', 1, 0, 17, "
        f"'Submit by Friday.'), ('{VERSION}', 1, 'page', 2, 19, 29, 'Worth 40%.')"
    ),
    (
        "INSERT INTO model_calls (provider, model, model_role, prompt_version, output_schema, "
        "status, attempts, latency_ms, output_summary, started_at, finished_at) VALUES "
        "('p', 'm', 'fast', 'v1', 'CourseFact', 'succeeded', 1, 5, '{}', now(), now())"
    ),
]
EXISTING_COUNTS = {"resources": 1, "resource_versions": 1, "source_spans": 2, "model_calls": 1}


@pytest.fixture
def db_at_0003(test_database_url: URL) -> Iterator[URL]:
    """A fresh database at 0003 holding pre-existing ingestion data."""
    url = test_database_url.set(database=MIGRATIONS_DATABASE)
    recreate_database(url)
    migrate(url, "0003")
    _execute(url, EXISTING_ROWS)
    try:
        yield url
    finally:
        drop_database(url)


def _retrieval_objects(url: URL) -> set[str]:
    """Which of the trigger, its function and model_calls.call_kind exist."""
    found = _scalar(
        url,
        "SELECT array_remove(ARRAY["
        "(SELECT 'trigger' FROM pg_trigger WHERE tgname = 'chunks_provenance'),"
        "(SELECT 'function' FROM pg_proc WHERE proname = 'chunks_check_provenance'),"
        "(SELECT 'call_kind' FROM information_schema.columns "
        " WHERE table_name = 'model_calls' AND column_name = 'call_kind')"
        "], NULL)",
    )
    return set(found)  # type: ignore[call-overload]


def _assert_existing_data_intact(url: URL) -> None:
    for table, expected in EXISTING_COUNTS.items():
        assert _scalar(url, f"SELECT count(*) FROM {table}") == expected, table
    assert _scalar(url, "SELECT extracted_text FROM resource_versions") == EXISTING_TEXT


def _assert_at_0003_with_existing_data(url: URL) -> None:
    state = _state(url)
    assert state.version == "0003"
    assert not RETRIEVAL_TABLES & state.tables
    assert not EXPECTED_0004_CHECKS & state.checks
    assert not EXPECTED_0004_FKS & state.foreign_keys
    assert _retrieval_objects(url) == set()
    _assert_existing_data_intact(url)


def _assert_at_0004_with_existing_data(url: URL) -> None:
    state = _state(url)
    assert state.version == "0004"
    assert RETRIEVAL_TABLES <= state.tables
    assert EXPECTED_0004_CHECKS <= state.checks
    assert EXPECTED_0004_UNIQUES <= state.uniques
    assert EXPECTED_0004_FKS <= state.foreign_keys
    assert {"ix_embeddings_model_call_id", "ix_chunk_embeddings_embedding_id"} <= state.indexes
    assert _retrieval_objects(url) == {"trigger", "function", "call_kind"}
    _assert_existing_data_intact(url)
    # Backfilled, with no default left behind: new writers must state the kind.
    assert _scalar(url, "SELECT string_agg(DISTINCT call_kind, ',') FROM model_calls") == (
        "chat_structured"
    )
    default = _scalar(
        url,
        "SELECT column_default FROM information_schema.columns "
        "WHERE table_name = 'model_calls' AND column_name = 'call_kind'",
    )
    assert default is None


def test_0004_upgrade_downgrade_upgrade_keeps_existing_data(db_at_0003: URL) -> None:
    _assert_at_0003_with_existing_data(db_at_0003)

    migrate(db_at_0003, "0004")
    _assert_at_0004_with_existing_data(db_at_0003)

    downgrade(db_at_0003, "0003")
    _assert_at_0003_with_existing_data(db_at_0003)

    migrate(db_at_0003, "0004")
    _assert_at_0004_with_existing_data(db_at_0003)


CALL_WITHOUT_KIND = (
    "INSERT INTO model_calls (provider, model, prompt_version, output_schema, "
    "status, attempts, latency_ms, output_summary, started_at, finished_at) VALUES "
    "('p', 'm', 'v1', 'S', 'succeeded', 1, 5, '{}', now(), now())"
)
EMBEDDING_CALL = (
    "INSERT INTO model_calls (call_kind, provider, model, prompt_version, "
    "output_schema, status, attempts, latency_ms, output_summary, started_at, "
    "finished_at) VALUES ('embedding', 'p', 'e', 'document/v1', 'vector[8]', "
    "'succeeded', 1, 5, '{}', now(), now())"
)


def test_0004_new_rows_must_state_call_kind(db_at_0003: URL) -> None:
    migrate(db_at_0003, "0004")

    with pytest.raises(IntegrityError, match="call_kind"):
        _execute(db_at_0003, [CALL_WITHOUT_KIND])


def test_0004_downgrade_refuses_while_embedding_calls_exist(db_at_0003: URL) -> None:
    migrate(db_at_0003, "0004")
    _execute(db_at_0003, [EMBEDDING_CALL])

    with pytest.raises(RuntimeError, match=r"model_calls has 1 embedding call\(s\)"):
        downgrade(db_at_0003, "0003")

    # Refused before any DDL: still a complete 0004 schema.
    assert _state(db_at_0003).version == "0004"
    assert _retrieval_objects(db_at_0003) == {"trigger", "function", "call_kind"}


def test_0004_downgrade_refuses_offline_sql_mode(test_database_url: URL) -> None:
    url = test_database_url.set(database=MIGRATIONS_DATABASE)
    with pytest.raises(RuntimeError, match="preconditions need a live database"):
        command.downgrade(alembic_config(url), "0004:0003", sql=True)


# --- 0005: workflow_runs.summary ------------------------------------------------------

RUN = "00000000-0000-0000-0000-0000000000d1"
A_RUN = (
    "INSERT INTO workflow_runs (id, workflow_version, course_id, status) "
    f"VALUES ('{RUN}', 'import/v1', '{COURSE_1}', 'succeeded')"
)


@pytest.fixture
def db_at_0004(test_database_url: URL) -> Iterator[URL]:
    """A fresh database at 0004 holding pre-existing ingestion data and one workflow run."""
    url = test_database_url.set(database=MIGRATIONS_DATABASE)
    recreate_database(url)
    migrate(url, "0003")
    _execute(url, EXISTING_ROWS)
    migrate(url, "0004")
    _execute(url, [A_RUN])
    try:
        yield url
    finally:
        drop_database(url)


def _summary_column(url: URL) -> bool:
    return bool(
        _scalar(
            url,
            "SELECT count(*) FROM information_schema.columns "
            "WHERE table_name = 'workflow_runs' AND column_name = 'summary'",
        )
    )


def _assert_at_0004_with_run(url: URL) -> None:
    state = _state(url)
    assert state.version == "0004"
    assert not _summary_column(url)
    assert "ck_workflow_runs_summary_is_object" not in state.checks
    assert _scalar(url, "SELECT count(*) FROM workflow_runs") == 1
    _assert_existing_data_intact(url)


def _assert_at_0005_with_run(url: URL) -> None:
    state = _state(url)
    assert state.version == "0005"
    assert _summary_column(url)
    assert "ck_workflow_runs_summary_is_object" in state.checks
    assert _scalar(url, "SELECT summary IS NULL FROM workflow_runs") is True  # left NULL
    _assert_existing_data_intact(url)


def test_0005_upgrade_downgrade_upgrade_keeps_existing_runs(db_at_0004: URL) -> None:
    _assert_at_0004_with_run(db_at_0004)

    migrate(db_at_0004, "0005")
    _assert_at_0005_with_run(db_at_0004)

    downgrade(db_at_0004, "0004")
    _assert_at_0004_with_run(db_at_0004)

    migrate(db_at_0004, "0005")
    _assert_at_0005_with_run(db_at_0004)


@pytest.mark.parametrize("summary", ["'[]'", "'3'", "'\"text\"'", "'null'"])
def test_0005_summary_must_be_a_json_object(db_at_0004: URL, summary: str) -> None:
    migrate(db_at_0004, "0005")
    _execute(db_at_0004, [f"UPDATE workflow_runs SET summary = '{{}}' WHERE id = '{RUN}'"])

    with pytest.raises(IntegrityError, match="ck_workflow_runs_summary_is_object"):
        _execute(db_at_0004, [f"UPDATE workflow_runs SET summary = {summary}::jsonb"])


# --- 0006: chunks.text_tsv -------------------------------------------------------------

EXISTING_CHUNK_TEXT = "Submit by Friday."
EXISTING_CHUNK = (
    "INSERT INTO chunks (resource_version_id, chunker_version, ordinal, start_offset, "
    "end_offset, first_span_ordinal, last_span_ordinal, text, text_hash, estimated_tokens) "
    f"VALUES ('{VERSION}', 'span-pack/v1', 0, 0, 17, 0, 0, '{EXISTING_CHUNK_TEXT}', "
    f"'{hashlib.sha256(EXISTING_CHUNK_TEXT.encode()).hexdigest()}', 5)"
)
TEXT_TSV_COLUMN = (
    "SELECT a.attgenerated::text || ':' || a.attnotnull::text || ':' || pg_get_expr(d.adbin, d.adrelid) "
    "FROM pg_attribute a JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum "
    "WHERE a.attrelid = 'public.chunks'::regclass AND a.attname = 'text_tsv' "
    "AND NOT a.attisdropped"
)
TEXT_TSV_INDEX_METHOD = (
    "SELECT am.amname FROM pg_class c JOIN pg_am am ON am.oid = c.relam "
    "WHERE c.relname = 'ix_chunks_text_tsv'"
)


@pytest.fixture
def db_at_0005(test_database_url: URL) -> Iterator[URL]:
    """A fresh database at 0005 holding pre-existing ingestion data and one chunk."""
    url = test_database_url.set(database=MIGRATIONS_DATABASE)
    recreate_database(url)
    migrate(url, "0003")
    _execute(url, EXISTING_ROWS)
    migrate(url, "0005")
    _execute(url, [EXISTING_CHUNK])
    try:
        yield url
    finally:
        drop_database(url)


def _assert_at_0005_with_chunk(url: URL) -> None:
    state = _state(url)
    assert state.version == "0005"
    assert _scalar(url, TEXT_TSV_COLUMN) is None
    assert "ix_chunks_text_tsv" not in state.indexes
    assert _scalar(url, "SELECT text FROM chunks") == EXISTING_CHUNK_TEXT
    _assert_existing_data_intact(url)


def _assert_at_0006_with_chunk(url: URL) -> None:
    state = _state(url)
    assert state.version == "0006"
    # Stored generated ('s'), NOT NULL, and exactly the model's expression.
    assert _scalar(url, TEXT_TSV_COLUMN) == f"s:true:{CHUNK_TEXT_TSV_EXPRESSION}"
    assert "ix_chunks_text_tsv" in state.indexes
    assert _scalar(url, TEXT_TSV_INDEX_METHOD) == "gin"
    # The pre-existing chunk was backfilled by the table rewrite.
    assert _scalar(url, "SELECT text_tsv::text FROM chunks") == "'friday':3 'submit':1"
    assert _scalar(url, "SELECT text FROM chunks") == EXISTING_CHUNK_TEXT
    _assert_existing_data_intact(url)


def test_0006_upgrade_downgrade_upgrade_backfills_existing_chunks(db_at_0005: URL) -> None:
    _assert_at_0005_with_chunk(db_at_0005)

    migrate(db_at_0005, "0006")
    _assert_at_0006_with_chunk(db_at_0005)

    downgrade(db_at_0005, "0005")
    _assert_at_0005_with_chunk(db_at_0005)

    migrate(db_at_0005, "0006")
    _assert_at_0006_with_chunk(db_at_0005)


# --- 0007: assessments, assessment deadlines, evidence links -------------------------

ACADEMIC_TABLES = {"assessments", "assessment_deadlines", "evidence_links"}
INSTITUTION_TIMEZONE = (
    "SELECT count(*) FROM information_schema.columns "
    "WHERE table_name = 'institutions' AND column_name = 'timezone'"
)


@pytest.fixture
def db_at_0006(test_database_url: URL) -> Iterator[URL]:
    """A fresh database at 0006 holding pre-existing ingestion data."""
    url = test_database_url.set(database=MIGRATIONS_DATABASE)
    recreate_database(url)
    migrate(url, "0003")
    _execute(url, EXISTING_ROWS)
    migrate(url, "0006")
    try:
        yield url
    finally:
        drop_database(url)


def _assert_at_0006_without_academic_layer(url: URL) -> None:
    state = _state(url)
    assert state.version == "0006"
    assert not ACADEMIC_TABLES & state.tables
    assert _scalar(url, INSTITUTION_TIMEZONE) == 0
    _assert_existing_data_intact(url)


def _assert_at_0007(url: URL) -> None:
    state = _state(url)
    assert state.version == "0007"
    assert ACADEMIC_TABLES <= state.tables
    assert "uq_assessment_deadlines_value" in state.uniques
    assert "uq_evidence_links_fact_quote" in state.uniques
    assert {
        "ck_evidence_links_one_subject",
        "ck_evidence_links_field_fits_subject",
        "ck_assessment_deadlines_time_fits_precision",
        "ck_assessment_deadlines_verified_datetime_has_instant",
        "ck_institutions_timezone_nonblank",
    } <= state.checks
    assert _scalar(url, "SELECT timezone IS NULL FROM institutions") is True  # left unknown
    _assert_existing_data_intact(url)


def test_0007_upgrade_downgrade_upgrade_keeps_existing_data(db_at_0006: URL) -> None:
    _assert_at_0006_without_academic_layer(db_at_0006)

    migrate(db_at_0006, "0007")
    _assert_at_0007(db_at_0006)

    downgrade(db_at_0006, "0006")
    _assert_at_0006_without_academic_layer(db_at_0006)

    migrate(db_at_0006, "0007")
    _assert_at_0007(db_at_0006)


# --- 0008: assessment requirements and obligations ------------------------------------

REQUIREMENT_OBLIGATION_TABLES = {"assessment_requirements", "obligations"}
ASSESSMENT = "00000000-0000-0000-0000-0000000000e1"
REQUIREMENT = "00000000-0000-0000-0000-0000000000e2"
OBLIGATION = "00000000-0000-0000-0000-0000000000e3"
KEY = "f" * 64
FIRST_SPAN = "(SELECT id FROM source_spans WHERE ordinal = 0)"
# Written with the 0007 schema: an assessment with one piece of title evidence.
ASSESSMENT_ROWS = [
    (
        "INSERT INTO assessments (id, course_id, canonical_key, title, status) VALUES "
        f"('{ASSESSMENT}', (SELECT course_id FROM resources), 'coursework 1', 'Coursework 1', "
        "'verified')"
    ),
    (
        "INSERT INTO evidence_links (subject_type, assessment_id, field, source_span_id, "
        "quote_start, quote_end, verification_status) VALUES "
        f"('assessment', '{ASSESSMENT}', 'title', {FIRST_SPAN}, 0, 6, 'verified')"
    ),
]
REQUIREMENT_OBLIGATION_ROWS = [
    (
        "INSERT INTO assessment_requirements (id, assessment_id, canonical_key, text, status) "
        f"VALUES ('{REQUIREMENT}', '{ASSESSMENT}', '{KEY}', 'Submit by Friday.', 'verified')"
    ),
    (
        "INSERT INTO obligations (id, course_id, assessment_id, canonical_key, canonical_action, "
        "category, modality, status) VALUES "
        f"('{OBLIGATION}', (SELECT course_id FROM resources), '{ASSESSMENT}', '{KEY}', "
        "'Submit by Friday', 'other', 'mandatory', 'verified')"
    ),
    (
        "INSERT INTO evidence_links (subject_type, assessment_requirement_id, field, "
        "source_span_id, quote_start, quote_end, verification_status) VALUES "
        f"('assessment_requirement', '{REQUIREMENT}', 'requirement', {FIRST_SPAN}, 0, 17, "
        "'verified')"
    ),
    (
        "INSERT INTO evidence_links (subject_type, obligation_id, field, source_span_id, "
        "quote_start, quote_end, verification_status) VALUES "
        f"('obligation', '{OBLIGATION}', 'obligation', {FIRST_SPAN}, 0, 17, 'verified')"
    ),
]


@pytest.fixture
def db_at_0007(test_database_url: URL) -> Iterator[URL]:
    """A fresh database at 0007 holding ingestion data and an evidenced assessment."""
    url = test_database_url.set(database=MIGRATIONS_DATABASE)
    recreate_database(url)
    migrate(url, "0003")
    _execute(url, EXISTING_ROWS)
    migrate(url, "0007")
    _execute(url, ASSESSMENT_ROWS)
    try:
        yield url
    finally:
        drop_database(url)


def _assert_at_0007_with_assessment(url: URL) -> None:
    state = _state(url)
    assert state.version == "0007"
    assert not REQUIREMENT_OBLIGATION_TABLES & state.tables
    assert "uq_assessments_id_course" not in state.uniques
    assert _scalar(url, "SELECT count(*) FROM evidence_links") == 1
    assert _scalar(url, "SELECT count(*) FROM assessments") == 1
    _assert_existing_data_intact(url)


def _assert_at_0008(url: URL) -> None:
    state = _state(url)
    assert state.version == "0008"
    assert REQUIREMENT_OBLIGATION_TABLES <= state.tables
    assert {
        "uq_assessments_id_course",
        "uq_assessment_requirements_assessment_key",
        "uq_obligations_course_key",
        "uq_evidence_links_fact_quote",
    } <= state.uniques
    assert {
        "ck_obligations_verified_has_modality",
        "ck_obligations_due_fields_need_date",
        "ck_obligations_verified_due_datetime_has_instant",
        "ck_assessment_requirements_canonical_key_sha256_hex",
        "ck_evidence_links_one_subject",
        "ck_evidence_links_field_fits_subject",
    } <= state.checks
    assert "fk_obligations_assessment_same_course" in state.foreign_keys
    # The 0007 evidence satisfies the rebuilt checks unchanged.
    assert (
        _scalar(url, "SELECT count(*) FROM evidence_links WHERE subject_type = 'assessment'") == 1
    )
    _assert_existing_data_intact(url)


def test_0008_upgrade_downgrade_upgrade_keeps_existing_claims(db_at_0007: URL) -> None:
    _assert_at_0007_with_assessment(db_at_0007)

    migrate(db_at_0007, "0008")
    _assert_at_0008(db_at_0007)
    _execute(db_at_0007, REQUIREMENT_OBLIGATION_ROWS)
    assert _scalar(db_at_0007, "SELECT count(*) FROM evidence_links") == 3

    # Requirements, obligations and their evidence go; the assessment's stays.
    downgrade(db_at_0007, "0007")
    _assert_at_0007_with_assessment(db_at_0007)

    migrate(db_at_0007, "0008")
    _assert_at_0008(db_at_0007)


def test_0008_downgrade_refuses_while_a_new_review_reason_is_in_use(db_at_0007: URL) -> None:
    migrate(db_at_0007, "0008")
    _execute(
        db_at_0007,
        [
            (
                "UPDATE assessments SET status = 'needs_review', "
                "review_reason = 'conflicting_assessment_link'"
            )
        ],
    )

    with pytest.raises(RuntimeError, match=r"1 assessment, deadline or evidence row\(s\)"):
        downgrade(db_at_0007, "0007")
    assert _state(db_at_0007).version == "0008"


def test_0008_downgrade_refuses_offline_sql_mode(test_database_url: URL) -> None:
    url = test_database_url.set(database=MIGRATIONS_DATABASE)
    with pytest.raises(RuntimeError, match="0008: downgrade preconditions need a live database"):
        command.downgrade(alembic_config(url), "0008:0007", sql=True)


# --- 0009: material mappings ----------------------------------------------------------

MAPPING_RUN = "00000000-0000-0000-0000-0000000000f1"
MAPPING_ROWS = [
    (
        "INSERT INTO workflow_runs (id, workflow_version, course_id, status) VALUES "
        f"('{MAPPING_RUN}', 'material-mapping/v1', (SELECT course_id FROM resources), "
        "'succeeded')"
    ),
    (
        "INSERT INTO material_mappings (requirement_id, relation, source_span_id, quote_start, "
        "quote_end, rationale, retrieval_mode, retrieval_rank, retrieval_score, "
        "query_model_call_id, verifier_model_call_id, workflow_run_id) VALUES "
        f"('{REQUIREMENT}', 'supported_by', {FIRST_SPAN}, 0, 5, 'Page 1 of \"Brief\"', "
        "'lexical', 1, 0.5, NULL, (SELECT id FROM model_calls LIMIT 1), "
        f"'{MAPPING_RUN}')"
    ),
]


def test_0009_upgrade_downgrade_upgrade_keeps_existing_claims(db_at_0007: URL) -> None:
    migrate(db_at_0007, "0008")
    _execute(db_at_0007, REQUIREMENT_OBLIGATION_ROWS)
    migrate(db_at_0007, "0009")
    state = _state(db_at_0007)
    assert "material_mappings" in state.tables
    assert "uq_material_mappings_target" in state.uniques
    assert {
        "ck_material_mappings_query_call_iff_embedded",
        "ck_material_mappings_rationale_nonblank_bounded",
    } <= state.checks
    _execute(db_at_0007, MAPPING_ROWS)
    assert _scalar(db_at_0007, "SELECT count(*) FROM material_mappings") == 1

    # Mappings go; the requirement and its evidence stay.
    downgrade(db_at_0007, "0008")
    _assert_at_0008(db_at_0007)
    assert "material_mappings" not in _state(db_at_0007).tables
    assert _scalar(db_at_0007, "SELECT count(*) FROM assessment_requirements") == 1
    assert _scalar(db_at_0007, "SELECT count(*) FROM evidence_links") == 3

    migrate(db_at_0007, "0009")
    assert _scalar(db_at_0007, "SELECT count(*) FROM material_mappings") == 0


# --- 0010: dependency edges and conflicts ---------------------------------------------

EDGE = "00000000-0000-0000-0000-0000000000f2"
REASONING_ROWS = [
    *MAPPING_ROWS[:1],
    (
        "INSERT INTO dependency_edges (id, course_id, relation, prerequisite_kind, "
        "prerequisite_obligation_id, dependent_kind, dependent_assessment_id, rule, "
        "derived_from_obligation_id, workflow_run_id) VALUES "
        f"('{EDGE}', (SELECT course_id FROM resources), 'required_for', 'obligation', "
        f"'{OBLIGATION}', 'assessment', '{ASSESSMENT}', 'dependency-rules/v1:before-submitting', "
        f"'{OBLIGATION}', '{MAPPING_RUN}')"
    ),
    (
        "INSERT INTO evidence_links (subject_type, dependency_edge_id, field, source_span_id, "
        "quote_start, quote_end, verification_status) VALUES "
        f"('dependency_edge', '{EDGE}', 'dependency', {FIRST_SPAN}, 0, 17, 'verified')"
    ),
    (
        "INSERT INTO conflicts (course_id, assessment_id, field, status, workflow_run_id) VALUES "
        f"((SELECT course_id FROM resources), '{ASSESSMENT}', 'deadline', 'open', '{MAPPING_RUN}')"
    ),
]


def test_0010_upgrade_downgrade_upgrade_keeps_existing_claims(db_at_0007: URL) -> None:
    migrate(db_at_0007, "0008")
    _execute(db_at_0007, REQUIREMENT_OBLIGATION_ROWS)
    migrate(db_at_0007, "0010")
    state = _state(db_at_0007)
    assert {"dependency_edges", "conflicts", "conflict_sides"} <= state.tables
    assert {"uq_dependency_edges_ends", "uq_conflicts_assessment_field"} <= state.uniques
    assert "fk_dependency_edges_dependent_same_course" in state.foreign_keys
    _execute(db_at_0007, REASONING_ROWS)
    assert _scalar(db_at_0007, "SELECT count(*) FROM evidence_links") == 4

    # Edges, their evidence and conflicts go; the claims and their evidence stay.
    downgrade(db_at_0007, "0009")
    state = _state(db_at_0007)
    assert not {"dependency_edges", "conflicts", "conflict_sides"} & state.tables
    assert _scalar(db_at_0007, "SELECT count(*) FROM evidence_links") == 3
    assert _scalar(db_at_0007, "SELECT count(*) FROM obligations") == 1

    migrate(db_at_0007, "0010")
    assert _scalar(db_at_0007, "SELECT count(*) FROM dependency_edges") == 0


def test_0011_upgrade_downgrade_upgrade(db_at_0007: URL) -> None:
    migrate(db_at_0007, "0011")
    assert {"study_states", "study_plans", "study_tasks"} <= _state(db_at_0007).tables
    _execute(
        db_at_0007,
        [
            (
                "INSERT INTO study_states (learner, source_span_id, state) VALUES "
                f"('demo-student', {FIRST_SPAN}, 'studying')"
            )
        ],
    )
    downgrade(db_at_0007, "0010")
    assert not {"study_states", "study_plans", "study_tasks"} & _state(db_at_0007).tables
    _assert_existing_data_intact(db_at_0007)
    migrate(db_at_0007, "0011")


def test_0012_upgrade_downgrade_upgrade(db_at_0007: URL) -> None:
    migrate(db_at_0007, "0012")
    assert "change_events" in _state(db_at_0007).tables
    assert "uq_change_events_new_version" in _state(db_at_0007).uniques
    downgrade(db_at_0007, "0011")
    assert "change_events" not in _state(db_at_0007).tables
    _assert_existing_data_intact(db_at_0007)
    migrate(db_at_0007, "0012")
