# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""The retrieval CLI, driven through main() against the test database with the
offline fake embedding provider."""

import asyncio
import json
import logging
import uuid
from collections.abc import Iterator
from typing import Any

import openai
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai.errors import AIConfigurationError, AIProviderError
from app.ai.nebius_embeddings import embedding_space_from_settings
from app.ai.provider import TokenUsage
from app.core.config import Settings
from app.db.models import Chunk, ChunkEmbedding, Embedding, ModelCall, WorkflowRun
from app.domain.enums import ModelCallErrorCategory
from app.retrieval import cli
from tests.embedding_fakes import FakeEmbeddingProvider
from tests.retrieval_seed import paragraphs, seed_course, seed_version

pytestmark = pytest.mark.anyio

SECRET = "SENSITIVE-COURSE-TEXT"


@pytest.fixture(autouse=True)
def _restore_root_logging() -> Iterator[None]:
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)


@pytest.fixture
def session_factory(committed_session: AsyncSession) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(committed_session.bind, expire_on_commit=False)


@pytest.fixture
def cli_settings(settings: Settings) -> Settings:
    """Embedding space configured; no API key (status must not need one)."""
    configured = settings.model_copy(
        update={"embedding_model": "vendor/embedding-model", "embedding_dimensions": 64}
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
