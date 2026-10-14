# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""IndexingService against PostgreSQL with real commits: transactions, reuse,
failure isolation, paid-vector durability and concurrency."""

import asyncio
import json
import logging
import uuid
from collections.abc import Sequence
from typing import Any

import pytest
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai import model_calls
from app.ai.embeddings import EmbeddingInput
from app.ai.errors import AIProviderError
from app.ai.provider import TokenUsage
from app.db.models import (
    Chunk,
    ChunkEmbedding,
    Embedding,
    ModelCall,
    WorkflowRun,
    workflow_run_inputs,
)
from app.domain.enums import (
    BlockKind,
    ModelCallErrorCategory,
    ModelCallStatus,
    WorkflowRunStatus,
)
from app.retrieval import store
from app.retrieval.chunking import ChunkingParams
from app.retrieval.indexing import (
    INDEX_WORKFLOW_VERSION,
    CourseNotFoundError,
    FailureStage,
    IndexingService,
    IndexRunResult,
    VersionIndexReport,
)
from app.retrieval.store import ChunkState, LinkState
from tests.chunking_cases import sentences
from tests.embedding_fakes import FakeEmbeddingProvider
from tests.retrieval_seed import paragraphs as _paragraphs
from tests.retrieval_seed import seed_course as _course
from tests.retrieval_seed import seed_version as _version

pytestmark = pytest.mark.anyio

SMALL = ChunkingParams(
    version="test/small",
    target_estimated_tokens=40,
    hard_max_estimated_tokens=50,
    overlap_estimated_tokens=6,
    split_above_estimated_tokens=44,
    min_estimated_tokens=10,
    split_piece_min_estimated_tokens=20,
)
P, H, NBSP = BlockKind.PAGE, BlockKind.HEADING, "\u00a0"


@pytest.fixture
def session_factory(committed_session: AsyncSession) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(committed_session.bind, expire_on_commit=False)


# --- builders and probes ------------------------------------------------------------------


def _service(
    sf: async_sessionmaker[AsyncSession], provider: FakeEmbeddingProvider, **kwargs: Any
) -> IndexingService:
    return IndexingService(
        sf,
        provider,
        batch_max_items=kwargs.pop("batch_max_items", 4),
        batch_max_estimated_tokens=kwargs.pop("batch_max_estimated_tokens", 200),
        params=kwargs.pop("params", SMALL),
    )


async def _counts(sf: async_sessionmaker[AsyncSession]) -> dict[str, int]:
    async with sf() as session:
        return {
            model.__tablename__: await session.scalar(select(func.count()).select_from(model))
            for model in (Chunk, ChunkEmbedding, Embedding, ModelCall, WorkflowRun)
        }


async def _state(
    sf: async_sessionmaker[AsyncSession], course_id: uuid.UUID, provider: FakeEmbeddingProvider
) -> list[VersionIndexReport]:
    return await _service(sf, provider).inspect_course(course_id)


async def _run(sf: async_sessionmaker[AsyncSession], run_id: uuid.UUID) -> WorkflowRun:
    async with sf() as session:
        return await session.get_one(WorkflowRun, run_id)


async def _chunk_texts(sf: async_sessionmaker[AsyncSession], version_id: uuid.UUID) -> list[str]:
    async with sf() as session:
        return list(
            await session.scalars(
                select(Chunk.text)
                .where(Chunk.resource_version_id == version_id)
                .order_by(Chunk.ordinal)
            )
        )


def _embedded(provider: FakeEmbeddingProvider) -> list[str]:
    return provider.embedded_texts


# --- first run, rerun, versions, courses, spaces -------------------------------------------


async def test_first_index_publishes_chunks_links_and_vectors_and_records_each_call(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    course = await _course(session_factory)
    version = await _version(session_factory, course, _paragraphs(0, 6))
    provider = FakeEmbeddingProvider()

    result = await _service(session_factory, provider).index_course(course)

    texts = await _chunk_texts(session_factory, version)
    chunks = len(texts)
    batches = -(-chunks // 4)
    assert result.status is WorkflowRunStatus.SUCCEEDED
    summary = result.summary
    assert (summary.versions_considered, summary.versions_indexed) == (1, 1)
    assert (summary.chunks_created, summary.links_created) == (chunks, chunks)
    assert (summary.embeddings_created, summary.embeddings_reused) == (len(set(texts)), 0)
    assert (summary.provider_calls, summary.failed_provider_calls) == (batches, 0)
    assert summary.prompt_tokens is not None and summary.prompt_tokens > 0
    assert sorted(_embedded(provider)) == sorted(set(texts))
    counts = await _counts(session_factory)
    assert counts == {
        "chunks": chunks,
        "chunk_embeddings": chunks,
        "embeddings": len(set(texts)),
        "model_calls": batches,
        "workflow_runs": 1,
    }
    run = await _run(session_factory, result.run_id)
    assert run.workflow_version == INDEX_WORKFLOW_VERSION == "index/v1"
    assert run.status is WorkflowRunStatus.SUCCEEDED and run.finished_at is not None
    assert run.summary == summary.model_dump(mode="json")
    async with session_factory() as session:
        inputs = list(await session.scalars(select(workflow_run_inputs.c.resource_version_id)))
        calls = list(await session.scalars(select(ModelCall)))
    assert inputs == [version]
    assert all(c.workflow_run_id == result.run_id and c.model_role is None for c in calls)
    assert [s.indexed for s in await _state(session_factory, course, provider)] == [True]


async def test_an_immediate_rerun_makes_no_provider_call_and_writes_nothing(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    course = await _course(session_factory)
    await _version(session_factory, course, _paragraphs(0, 6))
    provider = FakeEmbeddingProvider()
    first = await _service(session_factory, provider).index_course(course)
    before = await _counts(session_factory)
    calls_before = len(provider.calls)

    second = await _service(session_factory, provider).index_course(course)

    assert len(provider.calls) == calls_before
    after = await _counts(session_factory)
    assert after == before | {"workflow_runs": 2}
    summary = second.summary
    assert second.status is WorkflowRunStatus.SUCCEEDED
    assert (summary.versions_indexed, summary.versions_already_indexed) == (0, 1)
    assert summary.chunks_reused == summary.links_reused == first.summary.chunks_created
    assert summary.chunks_created == summary.links_created == summary.embeddings_created == 0
    assert summary.provider_calls == 0


async def test_v2_creates_only_v2_chunks_and_reuses_vectors_for_unchanged_text(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    course = await _course(session_factory)
    v1 = await _version(session_factory, course, _paragraphs(0, 6))
    provider = FakeEmbeddingProvider()
    await _service(session_factory, provider).index_course(course)
    v1_texts = await _chunk_texts(session_factory, v1)
    embedded_before = len(_embedded(provider))

    v2 = await _version(session_factory, course, _paragraphs(0, 7))
    result = await _service(session_factory, provider).index_course(course)

    v2_texts = await _chunk_texts(session_factory, v2)
    new_texts = set(v2_texts) - set(v1_texts)
    assert await _chunk_texts(session_factory, v1) == v1_texts  # v1 untouched
    assert sorted(_embedded(provider)[embedded_before:]) == sorted(new_texts)
    assert result.summary.chunks_created == len(v2_texts)
    assert result.summary.embeddings_created == len(new_texts)
    assert result.summary.embeddings_reused == len(set(v2_texts) - new_texts) > 0
    states = await _state(session_factory, course, provider)
    assert [(s.resource_version_id, s.indexed) for s in states] == [(v2, True)]


async def test_identical_text_in_another_course_reuses_the_same_vectors(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    course_a, course_b = await _course(session_factory, "A"), await _course(session_factory, "B")
    await _version(session_factory, course_a, _paragraphs(0, 5))
    await _version(session_factory, course_b, _paragraphs(0, 5))
    provider = FakeEmbeddingProvider()
    first = await _service(session_factory, provider).index_course(course_a)
    calls = len(provider.calls)

    second = await _service(session_factory, provider).index_course(course_b)

    assert len(provider.calls) == calls
    assert second.summary.embeddings_created == 0
    assert second.summary.embeddings_reused == first.summary.embeddings_created
    counts = await _counts(session_factory)
    assert counts["embeddings"] == first.summary.embeddings_created
    assert counts["chunk_embeddings"] == 2 * first.summary.links_created


async def test_a_new_embedding_space_adds_vectors_and_links_without_rechunking(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    course = await _course(session_factory)
    await _version(session_factory, course, _paragraphs(0, 5))
    first = await _service(session_factory, FakeEmbeddingProvider()).index_course(course)
    chunks = first.summary.chunks_created
    v2_space = FakeEmbeddingProvider(space_version="v2")

    result = await _service(session_factory, v2_space).index_course(course)

    summary = result.summary
    assert (summary.chunks_created, summary.chunks_reused) == (0, chunks)
    assert summary.links_created == chunks
    assert summary.embeddings_created == first.summary.embeddings_created
    counts = await _counts(session_factory)
    assert counts["chunks"] == chunks
    assert counts["chunk_embeddings"] == 2 * chunks  # the v1-space links are kept
    assert [s.indexed for s in await _state(session_factory, course, v2_space)] == [True]


async def test_duplicate_chunk_texts_share_one_vector_but_keep_one_link_each(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    body = sentences(0, 3)  # ~34 tokens: heading + body cannot share a chunk twice
    course = await _course(session_factory)
    await _version(
        session_factory, course, [(H, "Notes", None), (BlockKind.PARAGRAPH, body, None)] * 2
    )
    provider = FakeEmbeddingProvider()

    result = await _service(session_factory, provider).index_course(course)

    summary = result.summary
    assert (summary.chunks_created, summary.links_created) == (2, 2)
    assert summary.embeddings_created == 1
    assert summary.embedding_inputs_deduplicated == 1
    assert _embedded(provider) == ["Notes\n\n" + body]


async def test_a_course_without_current_versions_is_a_successful_no_op_run(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    provider = FakeEmbeddingProvider()

    result = await _service(session_factory, provider).index_course(await _course(session_factory))

    assert result.status is WorkflowRunStatus.SUCCEEDED
    assert result.summary.versions_considered == result.summary.provider_calls == 0
    assert provider.calls == []
    assert await _counts(session_factory) == {
        "chunks": 0,
        "chunk_embeddings": 0,
        "embeddings": 0,
        "model_calls": 0,
        "workflow_runs": 1,
    }
    run = await _run(session_factory, result.run_id)
    assert run.status is WorkflowRunStatus.SUCCEEDED
    assert run.summary == result.summary.model_dump(mode="json")


async def test_an_unknown_course_fails_clearly_without_a_run_or_a_provider_call(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    provider = FakeEmbeddingProvider()
    service = _service(session_factory, provider)

    with pytest.raises(CourseNotFoundError):
        await service.index_course(uuid.uuid4())
    with pytest.raises(CourseNotFoundError):
        await service.inspect_course(uuid.uuid4())

    assert provider.calls == []
    assert (await _counts(session_factory))["workflow_runs"] == 0


# --- failures --------------------------------------------------------------------------------


def _rate_limited() -> AIProviderError:
    return AIProviderError(
        ModelCallErrorCategory.RATE_LIMITED,
        "HTTP 429",
        model="fake",
        attempts=3,
        usage=TokenUsage(),
    )


async def test_a_provider_failure_aborts_the_run_and_leaves_versions_unsearchable(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    course = await _course(session_factory)
    await _version(session_factory, course, _paragraphs(0, 3), source_uri="upload:a.pdf")
    await _version(session_factory, course, _paragraphs(10, 3), source_uri="upload:b.pdf")
    provider = FakeEmbeddingProvider(failures=[_rate_limited()])

    result = await _service(session_factory, provider).index_course(course)

    summary = result.summary
    assert result.status is WorkflowRunStatus.FAILED
    assert (summary.versions_failed, summary.versions_not_attempted) == (1, 1)
    assert summary.failures[0].stage is FailureStage.EMBED
    assert summary.failures[0].error == "rate_limited"
    assert (summary.provider_calls, summary.failed_provider_calls) == (1, 1)
    assert summary.embeddings_created == summary.embeddings_reused == 0  # nothing resolved
    assert len(provider.calls) == 1  # the second version was never tried
    counts = await _counts(session_factory)
    assert counts["chunks"] == counts["chunk_embeddings"] == counts["embeddings"] == 0
    async with session_factory() as session:
        (call,) = (await session.scalars(select(ModelCall))).all()
    assert call.status is ModelCallStatus.FAILED
    assert call.error_category is ModelCallErrorCategory.RATE_LIMITED
    assert not any(s.indexed for s in await _state(session_factory, course, provider))
    assert (await _run(session_factory, result.run_id)).status is WorkflowRunStatus.FAILED


async def test_invalid_provider_output_is_a_typed_failure_that_aborts_the_run(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    course = await _course(session_factory)
    await _version(session_factory, course, _paragraphs(0, 3), source_uri="upload:a.pdf")
    await _version(session_factory, course, _paragraphs(10, 3), source_uri="upload:b.pdf")
    provider = FakeEmbeddingProvider(truncate_results=True)  # one vector short

    result = await _service(session_factory, provider).index_course(course)

    summary = result.summary
    assert result.status is WorkflowRunStatus.FAILED
    (failure,) = summary.failures
    assert (failure.stage, failure.error) == (FailureStage.EMBED, "invalid_output")
    assert (summary.versions_failed, summary.versions_not_attempted) == (1, 1)
    assert (summary.provider_calls, summary.failed_provider_calls) == (1, 1)
    async with session_factory() as session:
        (call,) = (await session.scalars(select(ModelCall))).all()
    assert call.status is ModelCallStatus.FAILED
    assert call.error_category is ModelCallErrorCategory.INVALID_OUTPUT  # not "unexpected"
    counts = await _counts(session_factory)
    assert counts["chunks"] == counts["chunk_embeddings"] == counts["embeddings"] == 0
    assert not any(r.indexed for r in await _state(session_factory, course, provider))


async def test_a_version_local_failure_does_not_stop_a_valid_sibling(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    course = await _course(session_factory)
    blank = await _version(session_factory, course, [(P, NBSP, None)], source_uri="upload:a.pdf")
    good = await _version(session_factory, course, _paragraphs(0, 3), source_uri="upload:b.pdf")

    result = await _service(session_factory, FakeEmbeddingProvider()).index_course(course)

    summary = result.summary
    assert result.status is WorkflowRunStatus.FAILED  # one snapshot input failed
    assert (summary.versions_failed, summary.versions_indexed) == (1, 1)
    (failure,) = summary.failures
    assert (failure.resource_version_id, failure.stage) == (blank, FailureStage.CHUNK)
    assert failure.error == "NoVisibleText"  # valid but empty, not a chunker failure
    states = {
        s.resource_version_id: s.indexed
        for s in await _state(session_factory, course, FakeEmbeddingProvider())
    }
    assert states == {blank: False, good: True}


async def test_paid_vectors_survive_a_failed_version_publication_and_are_reused(
    session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    course = await _course(session_factory)
    await _version(session_factory, course, _paragraphs(0, 5))
    provider = FakeEmbeddingProvider()

    async def failing_links(*args: object) -> None:
        raise IntegrityError("INSERT INTO chunk_embeddings", {}, Exception("simulated"))

    monkeypatch.setattr(store, "insert_links", failing_links)
    failed = await _service(session_factory, provider).index_course(course)
    monkeypatch.undo()

    assert failed.summary.failures[0].stage is FailureStage.PERSIST_VERSION
    after_failure = await _counts(session_factory)
    assert after_failure["chunks"] == after_failure["chunk_embeddings"] == 0  # D rolled back
    assert after_failure["embeddings"] == failed.summary.embeddings_created > 0  # C kept
    calls = len(provider.calls)

    retried = await _service(session_factory, provider).index_course(course)

    assert len(provider.calls) == calls  # paid vectors reused, nothing re-embedded
    assert retried.status is WorkflowRunStatus.SUCCEEDED
    assert retried.summary.embeddings_reused == failed.summary.embeddings_created
    assert retried.summary.embeddings_created == 0


async def test_vector_storage_failure_keeps_the_successful_call_and_aborts_the_run(
    session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    course = await _course(session_factory)
    await _version(session_factory, course, _paragraphs(0, 3), source_uri="upload:a.pdf")
    await _version(session_factory, course, _paragraphs(10, 3), source_uri="upload:b.pdf")

    async def failing_vectors(*args: object) -> None:
        raise OperationalError("INSERT INTO embeddings", {}, Exception("disk full"))

    monkeypatch.setattr(store, "insert_vectors", failing_vectors)
    result = await _service(session_factory, FakeEmbeddingProvider()).index_course(course)

    summary = result.summary
    assert result.status is WorkflowRunStatus.FAILED
    assert summary.failures[0].stage is FailureStage.STORE_VECTORS
    assert (summary.versions_failed, summary.versions_not_attempted) == (1, 1)
    assert (summary.provider_calls, summary.failed_provider_calls) == (1, 0)
    async with session_factory() as session:
        (call,) = (await session.scalars(select(ModelCall))).all()
    assert call.status is ModelCallStatus.SUCCEEDED  # never a fake provider failure
    counts = await _counts(session_factory)
    assert counts["embeddings"] == counts["chunks"] == 0


async def test_when_telemetry_cannot_be_stored_either_the_run_aborts_cleanly_and_safely(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    secret = "SENSITIVE-NOTES-TEXT"
    course = await _course(session_factory)
    await _version(session_factory, course, [(P, f"{secret} {sentences(0, 2)}", None)])

    async def failing(*args: object) -> None:
        raise OperationalError("INSERT", {}, Exception("connection lost"))

    monkeypatch.setattr(store, "insert_vectors", failing)
    monkeypatch.setattr(model_calls, "_persist", failing)
    result = await _service(session_factory, FakeEmbeddingProvider()).index_course(course)

    assert result.status is WorkflowRunStatus.FAILED
    assert result.summary.failures[0].stage is FailureStage.STORE_VECTORS
    assert (await _counts(session_factory))["model_calls"] == 0
    assert secret not in caplog.text
    assert secret not in json.dumps(result.summary.model_dump(mode="json"))


async def test_incomplete_existing_chunks_are_reported_not_repaired(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    course = await _course(session_factory)
    damaged = await _version(session_factory, course, _paragraphs(0, 5), source_uri="upload:a.pdf")
    provider = FakeEmbeddingProvider()
    await _service(session_factory, provider).index_course(course)
    async with session_factory() as session, session.begin():
        last = await session.scalar(
            select(func.max(Chunk.ordinal)).where(Chunk.resource_version_id == damaged)
        )
        await session.execute(
            delete(Chunk).where(Chunk.resource_version_id == damaged, Chunk.ordinal == last)
        )
    sibling = await _version(session_factory, course, _paragraphs(20, 2), source_uri="upload:b.pdf")
    before = await _counts(session_factory)

    result = await _service(session_factory, provider).index_course(course)

    (failure,) = result.summary.failures
    assert (failure.resource_version_id, failure.stage) == (damaged, FailureStage.CHUNK_STATE)
    after = await _counts(session_factory)
    assert after["chunks"] - before["chunks"] == result.summary.chunks_created  # sibling only
    states = {
        s.resource_version_id: s.indexed for s in await _state(session_factory, course, provider)
    }
    assert states[sibling] is True
    # Its remaining chunks are contiguous and linked, but not the deterministic set.
    (report,) = [
        r
        for r in await _state(session_factory, course, provider)
        if r.resource_version_id == damaged
    ]
    assert (report.chunk_state, report.indexed) == (ChunkState.MISMATCH, False)


async def test_a_partial_active_space_link_set_is_reported_not_repaired(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    course = await _course(session_factory)
    version = await _version(session_factory, course, _paragraphs(0, 5))
    provider = FakeEmbeddingProvider()
    await _service(session_factory, provider).index_course(course)
    async with session_factory() as session, session.begin():
        first = await session.scalar(
            select(Chunk.id).where(Chunk.resource_version_id == version, Chunk.ordinal == 0)
        )
        await session.execute(delete(ChunkEmbedding).where(ChunkEmbedding.chunk_id == first))
    before = await _counts(session_factory)

    result = await _service(session_factory, provider).index_course(course)

    assert result.summary.failures[0].stage is FailureStage.LINK_STATE
    assert await _counts(session_factory) == before | {"workflow_runs": 2}  # no silent repair
    (report,) = await _state(session_factory, course, provider)
    assert (report.chunk_state, report.link_state) == (ChunkState.EXACT, LinkState.INCONSISTENT)
    assert not report.indexed


async def test_a_complete_link_set_pointing_at_the_wrong_vector_is_reported(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    course = await _course(session_factory)
    version = await _version(session_factory, course, _paragraphs(0, 5))
    provider = FakeEmbeddingProvider()
    await _service(session_factory, provider).index_course(course)
    async with session_factory() as session, session.begin():
        chunk_0, chunk_1 = (
            await session.scalars(
                select(Chunk.id)
                .where(Chunk.resource_version_id == version)
                .order_by(Chunk.ordinal)
                .limit(2)
            )
        ).all()
        other = await session.scalar(
            select(ChunkEmbedding.embedding_id).where(ChunkEmbedding.chunk_id == chunk_1)
        )
        await session.execute(
            update(ChunkEmbedding)
            .where(ChunkEmbedding.chunk_id == chunk_0)
            .values(embedding_id=other)
        )

    result = await _service(session_factory, provider).index_course(course)

    assert result.summary.failures[0].stage is FailureStage.LINK_STATE


# --- stale versions and concurrency -------------------------------------------------------


async def test_a_version_superseded_during_the_run_is_still_a_successful_input(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    course = await _course(session_factory)
    v1 = await _version(session_factory, course, _paragraphs(0, 3))
    created: list[uuid.UUID] = []

    async def ingest_v2_mid_run(inputs: Sequence[EmbeddingInput]) -> None:
        if not created:  # no indexing transaction is open during the provider call
            created.append(await _version(session_factory, course, _paragraphs(0, 4)))

    provider = FakeEmbeddingProvider(before_embed=ingest_v2_mid_run)

    result = await _service(session_factory, provider).index_course(course)

    assert result.status is WorkflowRunStatus.SUCCEEDED
    assert (result.summary.versions_indexed, result.summary.versions_superseded) == (1, 1)
    assert await _chunk_texts(session_factory, v1)  # derived rows for the stale version exist
    states = await _state(session_factory, course, provider)
    assert [(s.resource_version_id, s.indexed) for s in states] == [(created[0], False)]


async def test_concurrent_runs_on_the_same_course_end_with_one_canonical_state(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    course = await _course(session_factory)
    version = await _version(session_factory, course, _paragraphs(0, 6))
    barrier = asyncio.Barrier(2)

    async def meet_once(provider: FakeEmbeddingProvider) -> None:
        if len(provider.calls) == 1:  # both runs have missed the cache before either stores
            await asyncio.wait_for(barrier.wait(), timeout=10)

    providers = [FakeEmbeddingProvider(), FakeEmbeddingProvider()]
    for provider in providers:
        provider.before_embed = lambda _inputs, p=provider: meet_once(p)

    results: list[IndexRunResult] = await asyncio.gather(
        *(_service(session_factory, provider).index_course(course) for provider in providers)
    )

    texts = await _chunk_texts(session_factory, version)
    keys = len(set(texts))
    assert all(r.status is WorkflowRunStatus.SUCCEEDED for r in results)
    # Duplicate paid calls really happened, and each one is recorded ...
    assert len(providers[0].calls) == len(providers[1].calls) == -(-keys // 4)
    counts = await _counts(session_factory)
    assert counts["model_calls"] == 2 * -(-keys // 4)
    # ... but persistence is idempotent: one vector per key, one chunk set, one link each.
    assert counts["embeddings"] == keys
    assert counts["chunks"] == counts["chunk_embeddings"] == len(texts)
    assert sum(r.summary.embeddings_created for r in results) == keys
    assert sum(r.summary.embeddings_reused for r in results) == keys  # losers reuse
    assert sorted(r.summary.versions_indexed for r in results) == [0, 1]
    # The losing inserts reused the canonical row; their ModelCalls still exist.
    async with session_factory() as session:
        referenced = func.count(Embedding.id)
        rows = (
            await session.execute(
                select(ModelCall.id, referenced)
                .outerjoin(Embedding, Embedding.model_call_id == ModelCall.id)
                .group_by(ModelCall.id)
            )
        ).all()
    assert sum(n for _, n in rows) == keys
    assert any(n == 0 for _, n in rows)
