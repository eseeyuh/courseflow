# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Regression tests for confirmed review findings on the retrieval stack:
invalid span rows, ordinal gaps, cancellation, paid-token accounting, query
telemetry failures, fallback row identity and CLI cleanup."""

import asyncio
import json
import uuid
from collections.abc import Sequence
from typing import Any

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai.embeddings import EmbeddingInput, EmbeddingResult, document_input
from app.ai.model_calls import VectorStorageError, call_embeddings_recorded
from app.core.config import Settings
from app.db.models import ModelCall, SourceSpan, WorkflowRun
from app.domain.enums import ModelCallStatus, WorkflowRunStatus
from app.retrieval import cli, search, store
from app.retrieval.chunking import ChunkingParams
from app.retrieval.indexing import (
    FailureStage,
    IndexingService,
    SourceContent,
    inspect_course,
)
from app.retrieval.search import (
    IndexStateError,
    QueryCallNotRecordedError,
    SearchRequest,
    SearchService,
)
from app.retrieval.stats import StatsIntegrityError, course_stats
from tests.embedding_fakes import FakeEmbeddingProvider
from tests.retrieval_seed import paragraphs, seed_course, seed_version

pytestmark = pytest.mark.anyio

SMALL = ChunkingParams("test/small", 40, 50, 6, 44, 10, 20)


@pytest.fixture
def session_factory(committed_session: AsyncSession) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(committed_session.bind, expire_on_commit=False)


def _indexer(
    sf: async_sessionmaker[AsyncSession], provider: FakeEmbeddingProvider
) -> IndexingService:
    return IndexingService(
        sf, provider, batch_max_items=8, batch_max_estimated_tokens=400, params=SMALL
    )


async def _damage(
    sf: async_sessionmaker[AsyncSession], version: uuid.UUID, span: int, **values: Any
) -> None:
    async with sf() as session, session.begin():
        await session.execute(
            update(SourceSpan)
            .where(SourceSpan.resource_version_id == version, SourceSpan.ordinal == span)
            .values(**values)
        )


DAMAGE = {
    # The schema allows spans without offsets (both or neither).
    "span-without-offsets": (1, {"start_offset": None, "end_offset": None}),
    # Increasing but not contiguous: a chunk's span range could not be resolved.
    "ordinal-gap": (2, {"ordinal": 5}),
}


@pytest.mark.parametrize("damage", DAMAGE.values(), ids=DAMAGE.keys())
async def test_invalid_span_rows_fail_one_version_and_never_reach_search(
    session_factory: async_sessionmaker[AsyncSession], damage: tuple[int, dict[str, Any]]
) -> None:
    course = await seed_course(session_factory)
    broken = await seed_version(
        session_factory, course, paragraphs(0, 3), source_uri="upload:a.pdf"
    )
    good = await seed_version(session_factory, course, paragraphs(10, 3), source_uri="upload:b.pdf")
    ordinal, values = damage
    await _damage(session_factory, broken, ordinal, **values)
    provider = FakeEmbeddingProvider()

    result = await _indexer(session_factory, provider).index_course(course)
    reports = {
        r.resource_version_id: r
        for r in await inspect_course(session_factory, course, provider.space, params=SMALL)
    }

    # Version-local: the run continues and the sibling is indexed.
    (failure,) = result.summary.failures
    assert (failure.resource_version_id, failure.stage) == (broken, FailureStage.CHUNK)
    assert result.summary.versions_indexed == 1 and result.summary.versions_not_attempted == 0
    assert reports[broken].content is SourceContent.INVALID
    assert reports[good].indexed
    # Search fails closed and names the version, before any paid call.
    calls = len(provider.calls)
    with pytest.raises(IndexStateError) as caught:
        await SearchService(session_factory, provider.space, provider, params=SMALL).search_course(
            SearchRequest.build(course_id=course, query="topic")
        )
    assert caught.value.corrupt_version_ids == (broken,)
    assert len(provider.calls) == calls
    with pytest.raises(StatsIntegrityError):
        await course_stats(session_factory, course, params=SMALL)


async def test_a_cancelled_run_is_marked_failed_not_left_running(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    course = await seed_course(session_factory)
    await seed_version(session_factory, course, paragraphs(0, 3))

    async def cancel(_inputs: Sequence[EmbeddingInput]) -> None:
        raise asyncio.CancelledError  # as Ctrl+C or a stopped container would

    with pytest.raises(asyncio.CancelledError):
        await _indexer(session_factory, FakeEmbeddingProvider(before_embed=cancel)).index_course(
            course
        )

    async with session_factory() as session:
        run = await session.scalar(select(WorkflowRun))
    assert run is not None and run.status is WorkflowRunStatus.FAILED
    assert run.summary is not None and run.summary["failures"][0]["error"] == "CancelledError"


async def test_tokens_of_a_paid_call_whose_storage_failed_are_still_counted(
    session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    course = await seed_course(session_factory)
    await seed_version(session_factory, course, paragraphs(0, 3))

    async def failing(*args: object) -> None:
        raise OperationalError("INSERT", {}, Exception("disk full"))

    monkeypatch.setattr(store, "insert_vectors", failing)
    result = await _indexer(session_factory, FakeEmbeddingProvider()).index_course(course)

    async with session_factory() as session:
        (call,) = (await session.scalars(select(ModelCall))).all()
    assert call.status is ModelCallStatus.SUCCEEDED
    assert result.summary.prompt_tokens == call.prompt_tokens and call.prompt_tokens


async def test_the_fallback_row_reuses_the_call_id_so_an_ambiguous_commit_cannot_duplicate_it(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    seen: list[uuid.UUID] = []

    async def store_then_fail(
        _session: AsyncSession, _result: EmbeddingResult, call_id: uuid.UUID
    ) -> None:
        seen.append(call_id)
        raise OperationalError("INSERT", {}, Exception("connection lost at commit"))

    with pytest.raises(VectorStorageError) as caught:
        await call_embeddings_recorded(
            FakeEmbeddingProvider(), session_factory, [document_input("a")], store=store_then_fail
        )

    assert caught.value.model_call_id == seen[0]
    assert caught.value.usage.prompt_tokens == 1
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(ModelCall)) == 1


async def test_a_query_call_that_cannot_be_recorded_stops_the_search_with_a_typed_error(
    session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    course = await seed_course(session_factory)
    await seed_version(session_factory, course, paragraphs(0, 3))
    provider = FakeEmbeddingProvider()
    await _indexer(session_factory, provider).index_course(course)
    fallback = uuid.uuid4()

    async def unrecorded(*args: object, **kwargs: object) -> None:
        raise VectorStorageError(fallback, None)

    monkeypatch.setattr(search, "call_embeddings_recorded", unrecorded)

    with pytest.raises(QueryCallNotRecordedError) as caught:
        await SearchService(session_factory, provider.space, provider, params=SMALL).search_course(
            SearchRequest.build(course_id=course, query="topic")
        )

    assert caught.value.model_call_id == fallback


async def test_a_cleanup_error_never_replaces_the_cli_result(
    session_factory: async_sessionmaker[AsyncSession],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
) -> None:
    course = await seed_course(session_factory)
    await seed_version(session_factory, course, paragraphs(0, 2))
    provider = FakeEmbeddingProvider()

    async def broken_close() -> None:
        raise RuntimeError("close failed")

    provider.aclose = broken_close  # type: ignore[method-assign]
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(cli, "_provider", lambda _settings: provider)

    code = await asyncio.to_thread(cli.main, ["index", "--course-id", str(course)])

    out, err = capsys.readouterr()
    lines = out.strip().splitlines()
    assert code == cli.EXIT_OK and len(lines) == 1
    assert json.loads(lines[0])["status"] == "succeeded"
    assert "cli.cleanup_failed" in err


async def test_status_reports_the_search_classification_of_each_version(
    session_factory: async_sessionmaker[AsyncSession],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
) -> None:
    course = await seed_course(session_factory)
    broken = await seed_version(
        session_factory, course, paragraphs(0, 3), source_uri="upload:a.pdf"
    )
    await seed_version(session_factory, course, paragraphs(10, 3), source_uri="upload:b.pdf")
    await _damage(session_factory, broken, 2, ordinal=5)
    monkeypatch.setattr(
        cli, "get_settings", lambda: settings.model_copy(update={"embedding_model": "m"})
    )

    code = await asyncio.to_thread(cli.main, ["status", "--course-id", str(course)])

    result = json.loads(capsys.readouterr().out)
    assert code == cli.EXIT_OK  # status reports; search is what refuses
    assert result["search_states"] == {"searchable": 0, "unindexed": 1, "corrupt": 1}
    states = {v["resource_version_id"]: v["search_state"] for v in result["versions"]}
    assert states[str(broken)] == "corrupt"
