# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""The retrieval CLI, driven through main() against the test database with the
offline fake embedding provider."""

import asyncio
import io
import json
import uuid
from fractions import Fraction
from typing import Any

import openai
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai.errors import AIConfigurationError, AIProviderError
from app.ai.nebius_embeddings import embedding_space_from_settings
from app.ai.provider import TokenUsage
from app.core.config import Settings
from app.db.models import (
    Chunk,
    ChunkEmbedding,
    Embedding,
    ModelCall,
    SourceSpan,
    WorkflowRun,
)
from app.domain.enums import BlockKind, ModelCallErrorCategory
from app.retrieval import cli
from app.retrieval.search import SpanEvidence
from tests.embedding_fakes import FakeEmbeddingProvider
from tests.retrieval_seed import paragraphs, seed_course, seed_version

pytestmark = pytest.mark.anyio

SECRET = "SENSITIVE-COURSE-TEXT"


@pytest.fixture
def session_factory(committed_session: AsyncSession) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(committed_session.bind, expire_on_commit=False)


@pytest.fixture
def cli_settings(settings: Settings) -> Settings:
    """Embedding space configured; no API key (status must not need one)."""
    configured = settings.model_copy(
        update={"embedding_model": "vendor/embedding-model", "embedding_dimensions": 256}
    )
    assert configured.nebius_api_key is None
    return configured


@pytest.fixture
def provider(monkeypatch: pytest.MonkeyPatch, cli_settings: Settings) -> FakeEmbeddingProvider:
    """An offline stand-in for the real provider, in the space status derives."""
    fake = FakeEmbeddingProvider(space=embedding_space_from_settings(cli_settings))
    monkeypatch.setattr(cli, "get_settings", lambda: cli_settings)
    monkeypatch.setattr(cli, "_provider", lambda _settings: fake)
    return fake


async def _run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, dict[str, Any], str]:
    code = await asyncio.to_thread(cli.main, list(argv))
    out, err = capsys.readouterr()
    lines = out.strip().splitlines()
    assert len(lines) == 1, f"stdout must hold exactly one JSON result, got: {out!r}"
    return code, json.loads(lines[0]), err


async def test_index_then_status_reports_counts_and_no_text(
    provider: FakeEmbeddingProvider,
    session_factory: async_sessionmaker[AsyncSession],
    capsys: pytest.CaptureFixture[str],
) -> None:
    course = await seed_course(session_factory)
    blocks = [(b[0], f"{SECRET} {b[1]}", b[2]) for b in paragraphs(0, 3)]
    version = await seed_version(session_factory, course, blocks)

    code, result, err = await _run(capsys, "index", "--course-id", str(course))

    assert code == cli.EXIT_OK
    assert result["status"] == "succeeded"
    assert result["summary"]["workflow_version"] == "index/v1"
    assert result["summary"]["versions_indexed"] == 1
    assert SECRET not in json.dumps(result) and SECRET not in err
    events = [json.loads(line)["event"] for line in err.strip().splitlines()]
    assert {"retrieval.index_started", "retrieval.index_finished"} <= set(events)

    code, status, _ = await _run(capsys, "status", "--course-id", str(course))

    assert code == cli.EXIT_OK
    assert status["indexed"] == 1
    assert status["versions"] == [
        {
            "resource_version_id": str(version),
            "search_state": "searchable",
            "content": "chunkable",
            "chunk_state": "exact",
            "link_state": "complete",
            "chunks": status["versions"][0]["chunks"],
            "indexed": True,
        }
    ]
    calls = len(provider.calls)
    await _run(capsys, "status", "--course-id", str(course))
    assert len(provider.calls) == calls  # status never calls the provider


async def test_a_failed_run_exits_5_with_its_summary(
    provider: FakeEmbeddingProvider,
    session_factory: async_sessionmaker[AsyncSession],
    capsys: pytest.CaptureFixture[str],
) -> None:
    provider.failures = [
        AIProviderError(
            ModelCallErrorCategory.AUTHENTICATION,
            "HTTP 401",
            model="fake",
            attempts=1,
            usage=TokenUsage(),
        )
    ]
    course = await seed_course(session_factory)
    await seed_version(session_factory, course, paragraphs(0, 2))

    code, result, _ = await _run(capsys, "index", "--course-id", str(course))

    assert code == cli.EXIT_RUN_FAILED == 5
    assert result["status"] == "failed"
    assert result["summary"]["failures"][0]["stage"] == "embed"
    assert result["summary"]["failures"][0]["error"] == "authentication"


async def _rows(sf: async_sessionmaker[AsyncSession]) -> dict[str, int]:
    async with sf() as session:
        return {
            model.__tablename__: await session.scalar(select(func.count()).select_from(model))
            for model in (Chunk, ChunkEmbedding, Embedding, ModelCall, WorkflowRun)
        }


@pytest.mark.parametrize("command", ["index", "status"])
async def test_an_unknown_course_exits_3_without_a_run_or_a_provider_call(
    provider: FakeEmbeddingProvider,
    session_factory: async_sessionmaker[AsyncSession],
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    code, result, _ = await _run(capsys, command, "--course-id", str(uuid.uuid4()))

    assert code == cli.EXIT_NOT_FOUND == 3
    assert result == {"status": "error", "error": "course_not_found"}
    assert provider.calls == []
    assert (await _rows(session_factory))["workflow_runs"] == 0  # no orphan run


async def test_a_course_without_current_versions_is_a_successful_no_op_run(
    provider: FakeEmbeddingProvider,
    session_factory: async_sessionmaker[AsyncSession],
    capsys: pytest.CaptureFixture[str],
) -> None:
    course = await seed_course(session_factory)

    code, result, _ = await _run(capsys, "index", "--course-id", str(course))

    assert code == cli.EXIT_OK
    assert result["status"] == "succeeded"
    assert result["summary"]["versions_considered"] == 0
    assert result["summary"]["provider_calls"] == 0
    assert provider.calls == []
    assert await _rows(session_factory) == {
        "chunks": 0,
        "chunk_embeddings": 0,
        "embeddings": 0,
        "model_calls": 0,
        "workflow_runs": 1,
    }
    async with session_factory() as session:
        run = await session.scalar(select(WorkflowRun))
    assert run is not None and run.status.value == "succeeded"
    assert run.summary is not None and run.summary["versions_considered"] == 0


async def test_status_works_offline_without_credentials_or_a_provider_client(
    provider: FakeEmbeddingProvider,
    session_factory: async_sessionmaker[AsyncSession],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    course = await seed_course(session_factory)
    await seed_version(session_factory, course, paragraphs(0, 2))
    code, _, _ = await _run(capsys, "index", "--course-id", str(course))
    assert code == cli.EXIT_OK

    def no_provider(_settings: Settings) -> FakeEmbeddingProvider:
        raise AssertionError("status must not build an embedding provider")

    def no_client(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("status must not build an API client")

    monkeypatch.setattr(cli, "_provider", no_provider)
    monkeypatch.setattr(openai.AsyncOpenAI, "__init__", no_client)
    calls = len(provider.calls)

    code, status, _ = await _run(capsys, "status", "--course-id", str(course))

    assert code == cli.EXIT_OK
    assert status["indexed"] == 1
    assert len(provider.calls) == calls


async def test_status_needs_the_embedding_model_to_know_its_space(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "get_settings", lambda: settings)  # no EMBEDDING_MODEL

    code, result, _ = await _run(capsys, "status", "--course-id", str(uuid.uuid4()))

    assert code == cli.EXIT_NOT_CONFIGURED == 2
    assert "EMBEDDING_MODEL" in result["detail"]
    assert "NEBIUS_API_KEY" not in result["detail"]  # credentials are not required


async def test_an_unconfigured_provider_exits_2_before_any_work(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    def unconfigured(_settings: Settings) -> FakeEmbeddingProvider:
        raise AIConfigurationError("AI provider not configured; missing: NEBIUS_API_KEY")

    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(cli, "_provider", unconfigured)  # index needs the real provider

    code, result, _ = await _run(capsys, "index", "--course-id", str(uuid.uuid4()))

    assert code == cli.EXIT_NOT_CONFIGURED == 2
    assert result["error"] == "provider_not_configured"
    assert "NEBIUS_API_KEY" in result["detail"]


async def test_the_real_provider_factory_requires_embedding_configuration(
    settings: Settings,
) -> None:
    with pytest.raises(AIConfigurationError, match="EMBEDDING_MODEL"):
        cli._provider(settings.model_copy(update={"embedding_model": None}))


async def test_usage_errors_exit_64_with_a_json_result(capsys: pytest.CaptureFixture[str]) -> None:
    code, result, _ = await _run(capsys, "index", "--course-id", "not-a-uuid")

    assert code == cli.EXIT_USAGE == 64
    assert result["error"] == "usage"


async def test_unexpected_errors_report_only_the_exception_type(
    provider: FakeEmbeddingProvider,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def explode(*args: Any) -> int:
        raise ValueError(f"{SECRET} in a message")

    monkeypatch.setattr(cli, "_index", explode)

    code, result, err = await _run(capsys, "index", "--course-id", str(uuid.uuid4()))

    assert code == cli.EXIT_UNEXPECTED == 1
    assert result == {"status": "error", "error": "unexpected", "type": "ValueError"}
    assert SECRET not in err and "Traceback" not in err


# --- search ---------------------------------------------------------------------------------

MARKER = "zebra quokka narwhal"
MARKED = f"The {MARKER} rubric is assessed in week nine."


async def _raw(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = await asyncio.to_thread(cli.main, list(argv))
    out, err = capsys.readouterr()
    return code, out, err


async def _indexed_course(
    session_factory: async_sessionmaker[AsyncSession], capsys: pytest.CaptureFixture[str]
) -> uuid.UUID:
    course = await seed_course(session_factory)
    await seed_version(
        session_factory,
        course,
        # Enough text for several span-pack/v1 chunks (~1,200 estimated tokens).
        [*paragraphs(0, 50), (paragraphs(0, 1)[0][0], MARKED, None)],
        source_uri="upload:secret-brief.pdf",
    )
    code, _, _ = await _run(capsys, "index", "--course-id", str(course))
    assert code == cli.EXIT_OK
    return course


def _assert_safe_logs(err: str) -> None:
    for secret in (MARKER, "week nine", "secret-brief", "Instruct"):
        assert secret not in err
    for line in err.strip().splitlines():
        json.loads(line)  # every log line is structured JSON


async def test_search_prints_a_readable_ranking_with_evidence_and_safe_logs(
    provider: FakeEmbeddingProvider,
    session_factory: async_sessionmaker[AsyncSession],
    capsys: pytest.CaptureFixture[str],
) -> None:
    course = await _indexed_course(session_factory, capsys)

    code, out, err = await _raw(capsys, "search", "--course-id", str(course), "--query", MARKER)

    assert code == cli.EXIT_OK
    assert "Searched 1 of 1 current version(s)" in out
    assert "#1 " in out and "(lecture) v1 | chunk" in out
    async with session_factory() as session:
        chunk_ids = {str(c) for c in await session.scalars(select(Chunk.id))}
    assert any(chunk_id in out for chunk_id in chunk_ids)  # top-k chunk IDs are printed
    assert "upload:secret-brief.pdf" in out  # stdout may show the reference, deliberately
    assert "evidence: span" in out and "not a confidence" in out
    _assert_safe_logs(err)
    events = [json.loads(line)["event"] for line in err.strip().splitlines()]
    assert "retrieval.search_completed" in events


async def test_search_json_is_one_object_with_spans_and_the_query_call(
    provider: FakeEmbeddingProvider,
    session_factory: async_sessionmaker[AsyncSession],
    capsys: pytest.CaptureFixture[str],
) -> None:
    course = await _indexed_course(session_factory, capsys)

    code, result, err = await _run(
        capsys, "search", "--course-id", str(course), "--query", MARKER, "-k", "2", "--json"
    )

    assert code == cli.EXIT_OK
    assert result["status"] == "ok" and len(result["hits"]) == 2
    assert MARKER in result["hits"][0]["text"]
    assert result["hits"][0]["spans"] and result["hits"][0]["resource"]["type"] == "lecture"
    assert result["query_model_call_id"] is not None
    assert result["space"]["dimensions"] == 256
    _assert_safe_logs(err)


async def test_search_reads_the_query_from_stdin(
    provider: FakeEmbeddingProvider,
    session_factory: async_sessionmaker[AsyncSession],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    course = await _indexed_course(session_factory, capsys)
    monkeypatch.setattr("sys.stdin", io.StringIO("  " + MARKER + chr(10)))  # with a newline

    code, result, _ = await _run(
        capsys, "search", "--course-id", str(course), "--query", "-", "--json"
    )

    assert code == cli.EXIT_OK
    assert MARKER in result["hits"][0]["text"]


@pytest.mark.parametrize(
    ("extra", "fields"),
    [(["--query", "   "], ["query"]), (["--query", "ok", "-k", "0"], ["k"])],
)
async def test_invalid_search_requests_exit_4_before_any_work(
    provider: FakeEmbeddingProvider,
    capsys: pytest.CaptureFixture[str],
    extra: list[str],
    fields: list[str],
) -> None:
    code, result, _ = await _run(
        capsys, "search", "--course-id", str(uuid.uuid4()), *extra, "--json"
    )

    assert code == cli.EXIT_INVALID_REQUEST == 4
    assert result == {"status": "error", "error": "invalid_request", "fields": fields}
    assert provider.calls == []


async def test_a_non_integer_k_is_a_usage_error(
    provider: FakeEmbeddingProvider, capsys: pytest.CaptureFixture[str]
) -> None:
    code, result, _ = await _run(
        capsys, "search", "--course-id", str(uuid.uuid4()), "--query", "x", "-k", "many"
    )

    assert code == cli.EXIT_USAGE and result["error"] == "usage"


async def test_search_errors_are_one_readable_line_in_human_mode(
    provider: FakeEmbeddingProvider, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = await _raw(capsys, "search", "--course-id", str(uuid.uuid4()), "--query", MARKER)

    assert code == cli.EXIT_NOT_FOUND
    assert out.strip() == "error: course_not_found"


async def test_a_query_provider_failure_exits_5_with_its_category(
    provider: FakeEmbeddingProvider,
    session_factory: async_sessionmaker[AsyncSession],
    capsys: pytest.CaptureFixture[str],
) -> None:
    course = await _indexed_course(session_factory, capsys)
    provider.failures = [
        AIProviderError(
            ModelCallErrorCategory.RATE_LIMITED,
            "HTTP 429",
            model="m",
            attempts=3,
            usage=TokenUsage(),
        )
    ]

    code, result, err = await _run(
        capsys, "search", "--course-id", str(course), "--query", MARKER, "--json"
    )

    assert code == cli.EXIT_PROVIDER_FAILED == 5
    assert result["error"] == "query_embedding_failed"
    assert result["category"] == "rate_limited" and result["model_call_id"]
    _assert_safe_logs(err)


async def test_an_inconsistent_index_exits_6_without_a_query_call(
    provider: FakeEmbeddingProvider,
    session_factory: async_sessionmaker[AsyncSession],
    capsys: pytest.CaptureFixture[str],
) -> None:
    course = await _indexed_course(session_factory, capsys)
    async with session_factory() as session, session.begin():
        one = await session.scalar(select(ChunkEmbedding.chunk_id).limit(1))
        await session.execute(
            ChunkEmbedding.__table__.delete().where(ChunkEmbedding.chunk_id == one)
        )
    calls = len(provider.calls)

    code, result, _ = await _run(
        capsys, "search", "--course-id", str(course), "--query", MARKER, "--json"
    )

    assert code == cli.EXIT_INDEX_STATE == 6
    assert result["error"] == "index_state_inconsistent"
    assert len(result["corrupt_versions"]) == 1
    assert len(provider.calls) == calls


def _span(ordinal: int, coverage: str, page: int) -> SpanEvidence:
    return SpanEvidence(
        span_id=uuid.uuid4(),
        ordinal=ordinal,
        block_kind=BlockKind.PAGE,
        page_number=page,
        slide_number=None,
        section_path=None,
        span_start=0,
        span_end=100,
        overlap_start=0 if coverage == "full" else 40,
        overlap_end=100,
        relative_start=0 if coverage == "full" else 40,
        relative_end=100,
        coverage=coverage,  # type: ignore[arg-type]
        excerpt="x" * 100,
    )


def test_human_evidence_groups_whole_spans_and_keeps_partial_ranges_exact() -> None:
    spans = [_span(3, "partial", 4), _span(4, "full", 5), _span(5, "full", 6), _span(6, "full", 7)]

    assert cli._evidence_lines(spans) == [
        "span 3, page 4 (characters 40-100 of 100)",
        "spans 4-6: page 5 to page 7 (whole spans)",
    ]


# --- stats ------------------------------------------------------------------------------------


async def test_stats_is_offline_and_reports_estimates_in_text_and_json(
    session_factory: async_sessionmaker[AsyncSession],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
) -> None:
    course = await seed_course(session_factory)
    await seed_version(session_factory, course, paragraphs(0, 3))

    def no_provider(_settings: Settings) -> FakeEmbeddingProvider:
        raise AssertionError("stats must not build an embedding provider")

    monkeypatch.setattr(cli, "get_settings", lambda: settings)  # no embedding settings, no key
    monkeypatch.setattr(cli, "_provider", no_provider)

    def no_client(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("stats must not build an API client")

    monkeypatch.setattr(openai.AsyncOpenAI, "__init__", no_client)

    code, out, err = await _raw(capsys, "stats", "--course-id", str(course))
    json_code, result, _ = await _run(capsys, "stats", "--course-id", str(course), "--json")

    assert code == json_code == cli.EXIT_OK
    assert "course total:" in out and "duplication factor" in out
    assert "estimates (ceil(chars / 4)), not tokenizer counts" in out
    assert result["chunker_version"] == "span-pack/v1"
    (version,) = result["versions"]
    assert version["content"] == "chunkable" and version["stored_chunk_state"] == "none"
    assert version["stats"]["visible_coverage"] == 1.0
    assert "Sentence" not in err  # no source text in logs


async def test_stats_refuse_to_report_invalid_chunk_state(
    session_factory: async_sessionmaker[AsyncSession],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
) -> None:
    course = await seed_course(session_factory)
    version = await seed_version(session_factory, course, paragraphs(0, 3))
    async with session_factory() as session, session.begin():
        await session.execute(
            SourceSpan.__table__.update()
            .where(SourceSpan.resource_version_id == version, SourceSpan.ordinal == 1)
            .values(start_offset=SourceSpan.start_offset - 3, end_offset=SourceSpan.end_offset - 3)
        )
    monkeypatch.setattr(cli, "get_settings", lambda: settings)

    code, result, _ = await _run(capsys, "stats", "--course-id", str(course), "--json")

    assert code == cli.EXIT_INDEX_STATE == 6
    assert result == {
        "status": "error",
        "error": "chunk_integrity_failed",
        "versions": [str(version)],
    }


# --- search modes ---------------------------------------------------------------------------


async def test_lexical_search_runs_offline_without_a_key_a_client_or_a_model_call(
    session_factory: async_sessionmaker[AsyncSession],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    provider: FakeEmbeddingProvider,
    cli_settings: Settings,
) -> None:
    course = await _indexed_course(session_factory, capsys)  # indexed by the fake provider
    async with session_factory() as session:
        calls_before = await session.scalar(select(func.count()).select_from(ModelCall))
    requests_before = len(provider.calls)  # the indexing requests

    def no_provider(_settings: Settings) -> FakeEmbeddingProvider:
        raise AssertionError("lexical search must not build an embedding provider")

    def no_client(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("lexical search must not build an API client")

    assert cli_settings.nebius_api_key is None  # no credentials at all
    monkeypatch.setattr(cli, "_provider", no_provider)
    monkeypatch.setattr(openai.AsyncOpenAI, "__init__", no_client)

    code, result, err = await _run(
        capsys, "search", "--course-id", str(course), "--query", MARKER, "--mode", "lexical",
        "--json",
    )  # fmt: skip

    assert code == cli.EXIT_OK
    assert result["mode"] == "lexical" and result["score_kind"] == "ts_rank_cd"
    assert result["lexical_version"] == "pg-fts-english/v2"
    assert result["query_format"] is None and result["query_model_call_id"] is None
    assert result["hits"] and MARKER in result["hits"][0]["text"]
    assert len(provider.calls) == requests_before  # no query request
    async with session_factory() as session:
        calls_after = await session.scalar(select(func.count()).select_from(ModelCall))
    assert calls_after == calls_before  # no query ModelCall
    _assert_safe_logs(err)
    assert "'zebra'" not in json.dumps(result) and "'zebra'" not in err  # no tsquery shown


async def test_lexical_human_output_names_the_mode_and_its_score(
    provider: FakeEmbeddingProvider,
    session_factory: async_sessionmaker[AsyncSession],
    capsys: pytest.CaptureFixture[str],
) -> None:
    course = await _indexed_course(session_factory, capsys)

    code, out, err = await _raw(
        capsys, "search", "--course-id", str(course), "--query", MARKER, "--mode", "lexical"
    )

    assert code == cli.EXIT_OK
    assert "Mode lexical (pg-fts-english/v2)" in out
    assert "ts_rank_cd" in out and "not comparable with vector scores" in out
    assert "evidence: span" in out
    assert "'zebra'" not in out  # the generated tsquery is never printed
    _assert_safe_logs(err)


async def test_vector_remains_the_default_mode(
    provider: FakeEmbeddingProvider,
    session_factory: async_sessionmaker[AsyncSession],
    capsys: pytest.CaptureFixture[str],
) -> None:
    course = await _indexed_course(session_factory, capsys)

    code, result, _ = await _run(
        capsys, "search", "--course-id", str(course), "--query", MARKER, "--json"
    )

    assert code == cli.EXIT_OK
    assert result["mode"] == "vector" and result["score_kind"] == "cosine_similarity"
    assert result["lexical_version"] is None and result["query_format"] == "query/v1"
    assert result["query_model_call_id"] is not None


async def test_hybrid_json_reports_both_rankers_and_an_exact_fused_score(
    provider: FakeEmbeddingProvider,
    session_factory: async_sessionmaker[AsyncSession],
    capsys: pytest.CaptureFixture[str],
) -> None:
    course = await _indexed_course(session_factory, capsys)
    requests_before = len(provider.calls)

    code, result, err = await _run(
        capsys, "search", "--course-id", str(course), "--query", MARKER, "--mode", "hybrid",
        "--json",
    )  # fmt: skip

    assert code == cli.EXIT_OK
    assert result["mode"] == "hybrid" and result["score_kind"] == "rrf"
    assert result["fusion_version"] == "rrf-k60-depth50/v2"
    assert result["lexical_version"] == "pg-fts-english/v2"
    assert result["query_format"] == "query/v1" and result["query_model_call_id"] is not None
    assert len(provider.calls) == requests_before + 1  # one paid query call
    top = result["hits"][0]
    assert MARKER in top["text"] and top["spans"]
    assert (top["vector_rank"], top["lexical_rank"]) == (1, 1)
    # the exact rational score as text; the float score is its approximation
    assert Fraction(top["fused_score"]) == Fraction(2, 61) and top["fused_score"] == "2/61"
    assert top["score"] == float(Fraction(2, 61))
    assert top["vector_distance"] is not None and top["lexical_score"] is not None
    _assert_safe_logs(err)


async def test_hybrid_human_output_names_both_contracts_and_each_hit_s_ranks(
    provider: FakeEmbeddingProvider,
    session_factory: async_sessionmaker[AsyncSession],
    capsys: pytest.CaptureFixture[str],
) -> None:
    course = await _indexed_course(session_factory, capsys)

    code, out, err = await _raw(
        capsys, "search", "--course-id", str(course), "--query", MARKER, "--mode", "hybrid"
    )

    assert code == cli.EXIT_OK
    assert "Mode hybrid (pg-fts-english/v2 + rrf-k60-depth50/v2)" in out
    assert "reciprocal rank fusion" in out and "not a confidence or probability" in out
    assert "ranks: vector 1 | lexical 1 | rrf 2/61" in out
    assert "ranks: vector 2 | lexical - | rrf 1/62" in out  # a vector-only hit
    assert "evidence: span" in out
    _assert_safe_logs(err)


async def test_a_hybrid_provider_failure_exits_5_and_never_falls_back_to_lexical(
    provider: FakeEmbeddingProvider,
    session_factory: async_sessionmaker[AsyncSession],
    capsys: pytest.CaptureFixture[str],
) -> None:
    course = await _indexed_course(session_factory, capsys)
    provider.failures = [
        AIProviderError(
            ModelCallErrorCategory.TIMEOUT, "timed out", model="m", attempts=3, usage=TokenUsage()
        )
    ]

    code, result, err = await _run(
        capsys, "search", "--course-id", str(course), "--query", MARKER, "--mode", "hybrid",
        "--json",
    )  # fmt: skip

    assert code == cli.EXIT_PROVIDER_FAILED
    assert result["error"] == "query_embedding_failed" and "hits" not in result
    assert result["category"] == "timeout" and result["model_call_id"]
    _assert_safe_logs(err)


async def test_hybrid_search_needs_a_configured_provider(
    monkeypatch: pytest.MonkeyPatch,
    cli_settings: Settings,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def unconfigured(_settings: Settings) -> FakeEmbeddingProvider:
        raise AIConfigurationError("AI provider not configured; missing: NEBIUS_API_KEY")

    monkeypatch.setattr(cli, "get_settings", lambda: cli_settings)
    monkeypatch.setattr(cli, "_provider", unconfigured)

    code, result, _ = await _run(
        capsys, "search", "--course-id", str(uuid.uuid4()), "--query", "q", "--mode", "hybrid",
        "--json",
    )  # fmt: skip

    assert code == cli.EXIT_NOT_CONFIGURED
    assert result["error"] == "provider_not_configured"


@pytest.mark.parametrize("mode", ["rrf", "fused", ""])
async def test_unimplemented_modes_are_usage_errors(
    provider: FakeEmbeddingProvider, capsys: pytest.CaptureFixture[str], mode: str
) -> None:
    code, result, _ = await _run(
        capsys, "search", "--course-id", str(uuid.uuid4()), "--query", "q", "--mode", mode
    )

    assert code == cli.EXIT_USAGE
    assert result["error"] == "usage"


async def test_lexical_search_without_embedding_settings_is_not_configured(
    session_factory: async_sessionmaker[AsyncSession],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
) -> None:
    """The active space defines the searchable corpus even for lexical search."""
    course = await seed_course(session_factory)
    monkeypatch.setattr(cli, "get_settings", lambda: settings)  # no EMBEDDING_MODEL

    code, out, _ = await _raw(
        capsys, "search", "--course-id", str(course), "--query", "q", "--mode", "lexical"
    )

    assert code == cli.EXIT_NOT_CONFIGURED
    assert "provider_not_configured" in out
