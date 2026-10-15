# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Index a course: chunk its current versions, embed what is not cached, and
publish each version's chunks and links atomically.

Transactions (the service owns every one; none is open during a provider call):

A  run start    WorkflowRun(running), its input versions (the current-version
                snapshot) and the embedding space row
B  per version  read the version, its spans, chunks, links and cached vectors
C  per batch    a successful embedding ModelCall + the vectors it produced
                (see app.ai.model_calls.call_embeddings_recorded)
D  per version  lock the version, insert the chunk set if absent, insert the
                link set if absent, verify, commit: all or nothing
E  run end      status, finished_at and summary

Vectors committed in C survive a rolled-back D as reusable cache rows.

Concurrency: persistence is strictly idempotent (unique keys, ON CONFLICT,
the per-version lock in D). Concurrent cache misses may make duplicate paid
provider calls; each one is recorded, and the database keeps one canonical
vector per cache key. No lock is held across a network call.

Failures confined to one version (chunker error, inconsistent existing
chunk or link state, an integrity error while publishing that version) fail
that version and the run continues. Shared or systemic failures (the
provider, storing vectors, database connectivity, a bug) abort the run; the
remaining versions are not attempted.

Logs and the summary carry IDs and counts only, never text or text hashes.
"""

from __future__ import annotations

import enum
import functools
import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict
from sqlalchemy import exists, insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai.embeddings import (
    DOCUMENT_INPUT_FORMAT,
    EmbeddingInput,
    EmbeddingProvider,
    EmbeddingResult,
    EmbeddingSpace,
    document_input,
)
from app.ai.model_calls import RecordedCallError, VectorStorageError, call_embeddings_recorded
from app.db.models import Course, WorkflowRun, workflow_run_inputs
from app.domain.enums import WorkflowRunStatus
from app.retrieval import store
from app.retrieval.batching import plan_batches
from app.retrieval.chunking import (
    SPAN_PACK_V1,
    ChunkingError,
    ChunkingParams,
    check_spans,
    chunk_version,
    has_visible_text,
)
from app.retrieval.provenance import ChunkDraft
from app.retrieval.store import CacheKey, ChunkState, LinkState, SpaceRef, StoredChunk

logger = logging.getLogger(__name__)

INDEX_WORKFLOW_VERSION = "index/v1"


class CourseNotFoundError(LookupError):
    pass


class FailureStage(enum.StrEnum):
    CHUNK = "chunk"  # the version cannot be chunked (no visible text, bad spans)
    CHUNK_STATE = "chunk_state"  # existing chunks differ from the deterministic set
    LINK_STATE = "link_state"  # existing active-space links are partial or wrong
    EMBED = "embed"  # the provider call failed
    STORE_VECTORS = "store_vectors"  # the provider succeeded; storing vectors failed
    PERSIST_VERSION = "persist_version"  # publishing the version's chunks/links failed
    UNEXPECTED = "unexpected"  # a bug or a database outage


class VersionFailure(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    resource_version_id: uuid.UUID
    stage: FailureStage
    # A model-call error category or an exception type: never a message.
    error: str | None = None


class SpaceSummary(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: str
    model: str
    dimensions: int
    space_version: str


class IndexRunSummary(BaseModel):
    """The ``summary`` of a WorkflowRun with workflow_version "index/v1".

    Counters (all unique, never double-counted through shared rows):
    - chunks_*: Chunk rows; links_*: ChunkEmbedding rows.
    - embeddings_created: cache keys whose Embedding row this run inserted.
    - embeddings_reused: cache keys this run used whose row it did not insert
      (cached before, or won by a concurrent writer).
    - embedding_inputs_deduplicated: chunk inputs that needed no vector of their
      own because another chunk in this run needed the same cache key.
    - provider_calls: real external requests; failed_provider_calls: those that
      failed. prompt_tokens: sum over successful requests, or None if any did
      not report usage. The ModelCall rows remain the source of truth.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    workflow_version: Literal["index/v1"] = INDEX_WORKFLOW_VERSION
    chunker_version: str
    embedding_space: SpaceSummary
    input_format: str
    versions_considered: int = 0
    versions_indexed: int = 0
    versions_already_indexed: int = 0
    versions_failed: int = 0
    versions_not_attempted: int = 0
    versions_superseded: int = 0
    chunks_created: int = 0
    chunks_reused: int = 0
    links_created: int = 0
    links_reused: int = 0
    embeddings_created: int = 0
    embeddings_reused: int = 0
    embedding_inputs_deduplicated: int = 0
    provider_calls: int = 0
    failed_provider_calls: int = 0
    prompt_tokens: int | None = 0
    failures: list[VersionFailure] = []


@dataclass(frozen=True, slots=True)
class IndexRunResult:
    run_id: uuid.UUID
    status: WorkflowRunStatus
    summary: IndexRunSummary


class SourceContent(enum.StrEnum):
    """What deterministic chunking can make of a version's stored source."""

    CHUNKABLE = "chunkable"
    # Valid spans, but none holds visible text (e.g. NBSP-only): nothing to retrieve.
    NO_VISIBLE_TEXT = "no_visible_text"
    # Spans out of order or outside the text, or the chunker refused its own
    # output: the expected state cannot be derived safely.
    INVALID = "invalid"


def derive_chunks(
    source: store.VersionSource | None, params: ChunkingParams
) -> tuple[SourceContent, list[ChunkDraft]]:
    """The deterministic chunk set, or why there is none. Only a version with
    no visible text yields an empty set without being invalid."""
    if source is None or source.spans is None:
        return SourceContent.INVALID, []
    try:
        check_spans(source.extracted_text, source.spans)
        if not has_visible_text(source.extracted_text, source.spans):
            return SourceContent.NO_VISIBLE_TEXT, []
        return SourceContent.CHUNKABLE, chunk_version(source.extracted_text, source.spans, params)
    except ChunkingError:
        return SourceContent.INVALID, []


@dataclass(frozen=True, slots=True)
class VersionIndexReport:
    """Derived, never stored. A version is indexed for (chunker version, space,
    document format) when its stored chunks are exactly the deterministic set
    and every chunk has exactly one correct link in that space and format."""

    resource_version_id: uuid.UUID
    content: SourceContent
    chunk_state: ChunkState | None  # None: not chunkable (see content)
    link_state: LinkState | None  # None: not examined (chunks are not exact)
    chunks: int  # stored chunks for the chunker version

    @property
    def indexed(self) -> bool:
        return self.chunk_state is ChunkState.EXACT and self.link_state is LinkState.COMPLETE


class _VersionFailed(Exception):
    """A failure confined to one version: the run continues."""

    def __init__(self, stage: FailureStage, error: str | None = None) -> None:
        super().__init__(stage.value)
        self.stage = stage
        self.error = error


class _RunAborted(Exception):
    """A shared or systemic failure: the remaining versions are not attempted."""

    def __init__(self, stage: FailureStage, error: str | None = None) -> None:
        super().__init__(stage.value)
        self.stage = stage
        self.error = error


@dataclass
class _RunState:
    run_id: uuid.UUID
    space: SpaceRef
    # Canonical embedding per cache key, learned during this run (any space or
    # format a key names; this run only ever produces one of each).
    cache: dict[CacheKey, uuid.UUID] = field(default_factory=dict)
    # Keys whose Embedding row this run inserted, and keys it resolved to a row
    # it did not insert (cached before, or won by a concurrent writer).
    created: set[CacheKey] = field(default_factory=set)
    reused: set[CacheKey] = field(default_factory=set)
    counts: dict[str, int] = field(default_factory=dict)
    prompt_tokens: int | None = 0
    failures: list[VersionFailure] = field(default_factory=list)
    processed: list[uuid.UUID] = field(default_factory=list)

    def add(self, counter: str, amount: int = 1) -> None:
        self.counts[counter] = self.counts.get(counter, 0) + amount


class IndexingService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        provider: EmbeddingProvider,
        *,
        batch_max_items: int,
        batch_max_estimated_tokens: int,
        params: ChunkingParams = SPAN_PACK_V1,
    ) -> None:
        if params.hard_max_estimated_tokens > batch_max_estimated_tokens:
            raise ValueError("a single chunk must fit the embedding batch token budget")
        self._sessions = session_factory
        self._provider = provider
        self._params = params
        self._max_items = min(batch_max_items, provider.max_batch_items)
        self._max_tokens = batch_max_estimated_tokens
        self._format = DOCUMENT_INPUT_FORMAT

    # --- the run -------------------------------------------------------------------------

    async def index_course(self, course_id: uuid.UUID) -> IndexRunResult:
        run, snapshot = await self._start(course_id)  # A
        logger.info(
            "retrieval.index_started",
            extra={
                "run_id": str(run.run_id),
                "course_id": str(course_id),
                "versions": len(snapshot),
            },
        )
        for position, version_id in enumerate(snapshot):
            remaining = len(snapshot) - position - 1
            try:
                outcome = await self._index_version(run, version_id)
            except _VersionFailed as failed:
                self._fail(run, version_id, failed.stage, failed.error)
                continue
            except _RunAborted as aborted:
                self._fail(run, version_id, aborted.stage, aborted.error)
                run.add("versions_not_attempted", remaining)
                break
            except BaseException as exc:
                # A bug, a database outage, or cancellation (Ctrl+C, a stopped
                # container): record what we can so the run is not left running,
                # then re-raise.
                self._fail(run, version_id, FailureStage.UNEXPECTED, type(exc).__name__)
                run.add("versions_not_attempted", remaining)
                await self._finish_best_effort(run)
                raise
            run.processed.append(version_id)
            run.add(outcome)
        return await self._finish(run)  # E

    async def _start(self, course_id: uuid.UUID) -> tuple[_RunState, list[uuid.UUID]]:
        async with self._sessions() as session, session.begin():
            course = await session.scalar(select(exists().where(Course.id == course_id)))
            if not course:
                raise CourseNotFoundError(f"course {course_id} does not exist")
            version_ids = await store.current_versions(session, course_id)
            space = await store.ensure_space(session, self._provider.space)
            run = WorkflowRun(
                workflow_version=INDEX_WORKFLOW_VERSION,
                course_id=course_id,
                status=WorkflowRunStatus.RUNNING,
                started_at=datetime.now(UTC),
            )
            session.add(run)
            await session.flush()
            if version_ids:
                await session.execute(
                    insert(workflow_run_inputs),
                    [{"run_id": run.id, "resource_version_id": v} for v in version_ids],
                )
        state = _RunState(run_id=run.id, space=space)
        state.add("versions_considered", len(version_ids))
        return state, version_ids

    def _fail(
        self,
        run: _RunState,
        version_id: uuid.UUID,
        stage: FailureStage,
        error: str | None,
    ) -> None:
        run.add("versions_failed")
        run.failures.append(
            VersionFailure(resource_version_id=version_id, stage=stage, error=error)
        )
        logger.warning(
            "retrieval.version_failed",
            extra={
                "run_id": str(run.run_id),
                "resource_version_id": str(version_id),
                "stage": stage.value,
                "error": error,
            },
        )

    def _summary(self, run: _RunState, superseded: int) -> IndexRunSummary:
        space = self._provider.space
        counts = run.counts
        return IndexRunSummary(
            chunker_version=self._params.version,
            embedding_space=SpaceSummary(
                provider=space.provider,
                model=space.model,
                dimensions=space.dimensions,
                space_version=space.space_version,
            ),
            input_format=self._format,
            versions_superseded=superseded,
            embeddings_created=len(run.created),
            embeddings_reused=len(run.reused),
            prompt_tokens=run.prompt_tokens,
            failures=run.failures,
            **{
                name: counts.get(name, 0)
                for name in (
                    "versions_considered",
                    "versions_indexed",
                    "versions_already_indexed",
                    "versions_failed",
                    "versions_not_attempted",
                    "chunks_created",
                    "chunks_reused",
                    "links_created",
                    "links_reused",
                    "embedding_inputs_deduplicated",
                    "provider_calls",
                    "failed_provider_calls",
                )
            },
        )

    async def _finish(self, run: _RunState) -> IndexRunResult:
        superseded = 0
        if run.processed:
            async with self._sessions() as session:
                current = await store.still_current(session, run.processed)
            superseded = len([v for v in run.processed if v not in current])
        summary = self._summary(run, superseded)
        failed = summary.versions_failed or summary.versions_not_attempted
        status = WorkflowRunStatus.FAILED if failed else WorkflowRunStatus.SUCCEEDED
        await self._write_finish(run.run_id, status, summary)
        logger.info(
            "retrieval.index_finished",
            extra={"run_id": str(run.run_id), "status": status.value}
            | summary.model_dump(mode="json", exclude={"failures", "embedding_space"}),
        )
        return IndexRunResult(run_id=run.run_id, status=status, summary=summary)

    async def _finish_best_effort(self, run: _RunState) -> None:
        try:
            await self._write_finish(run.run_id, WorkflowRunStatus.FAILED, self._summary(run, 0))
        except Exception as exc:  # noqa: BLE001 - the original error is re-raised by the caller
            logger.error(
                "retrieval.run_finish_failed",
                extra={"run_id": str(run.run_id), "error_type": type(exc).__name__},
            )

    async def _write_finish(
        self, run_id: uuid.UUID, status: WorkflowRunStatus, summary: IndexRunSummary
    ) -> None:
        async with self._sessions() as session, session.begin():
            run = await session.get_one(WorkflowRun, run_id)
            run.status = status
            run.finished_at = max(datetime.now(UTC), run.started_at or datetime.now(UTC))
            run.summary = summary.model_dump(mode="json")

    # --- read-only inspection -------------------------------------------------------------

    async def inspect_course(self, course_id: uuid.UUID) -> list[VersionIndexReport]:
        return await inspect_course(
            self._sessions, course_id, self._provider.space, params=self._params
        )

    # --- one version ---------------------------------------------------------------------

    async def _index_version(self, run: _RunState, version_id: uuid.UUID) -> str:
        """Returns the outcome counter: versions_indexed or versions_already_indexed."""
        chunker = self._params.version
        async with self._sessions() as session:  # B
            source = await store.load_version(session, version_id)
            existing = await store.existing_chunks(session, version_id, chunker)
        content, drafts = derive_chunks(source, self._params)
        if content is SourceContent.NO_VISIBLE_TEXT:
            raise _VersionFailed(FailureStage.CHUNK, "NoVisibleText")
        if content is SourceContent.INVALID:
            raise _VersionFailed(FailureStage.CHUNK, "ChunkingError")

        inputs = [document_input(draft.text) for draft in drafts]
        keys = [_key(run.space, item) for item in inputs]
        state = store.compare_chunks(existing, drafts)
        if state is ChunkState.MISMATCH:
            raise _VersionFailed(FailureStage.CHUNK_STATE)
        if state is ChunkState.EXACT:
            expected = {chunk.id: key for chunk, key in zip(existing, keys, strict=True)}
            async with self._sessions() as session:
                links = await store.link_state(session, existing, expected)
            if links is LinkState.INCONSISTENT:
                raise _VersionFailed(FailureStage.LINK_STATE)
            if links is LinkState.COMPLETE:
                run.add("chunks_reused", len(existing))
                run.add("links_reused", len(existing))
                return "versions_already_indexed"

        await self._ensure_vectors(run, inputs, keys)
        return await self._publish(run, version_id, drafts, keys)  # D

    async def _ensure_vectors(
        self, run: _RunState, inputs: Sequence[EmbeddingInput], keys: Sequence[CacheKey]
    ) -> None:
        """Make sure every key has a canonical embedding: in-run first, then the
        database cache, then the provider for whatever is still missing."""
        unique: dict[CacheKey, EmbeddingInput] = {}
        for item, key in zip(inputs, keys, strict=True):
            if key in unique or key in run.cache:
                run.add("embedding_inputs_deduplicated")
            unique.setdefault(key, item)
        lookup = [key for key in unique if key not in run.cache]
        if lookup:
            async with self._sessions() as session:
                found = await store.find_cached(session, lookup)
            run.cache.update(found)
            run.reused.update(found)
        missing = [unique[key] for key in unique if key not in run.cache]
        batches = plan_batches(
            missing, max_items=self._max_items, max_estimated_tokens=self._max_tokens
        )
        for batch in batches:
            await self._embed_batch(run, batch)

    async def _embed_batch(self, run: _RunState, batch: Sequence[EmbeddingInput]) -> None:
        keys = [_key(run.space, item) for item in batch]
        try:
            recorded = await call_embeddings_recorded(
                self._provider,
                self._sessions,
                batch,
                workflow_run_id=run.run_id,
                store=functools.partial(self._store_vectors, run.space, keys),
            )
        except RecordedCallError as failed:
            run.add("provider_calls")
            run.add("failed_provider_calls")
            raise _RunAborted(FailureStage.EMBED, failed.error.category.value) from failed
        except VectorStorageError as failed:
            run.add("provider_calls")  # the paid request itself succeeded
            _add_prompt_tokens(run, failed.usage.prompt_tokens)
            raise _RunAborted(FailureStage.STORE_VECTORS, type(failed.__cause__).__name__) from (
                failed
            )
        run.add("provider_calls")
        _add_prompt_tokens(run, recorded.result.usage.prompt_tokens)
        stored = recorded.stored
        if stored is None:  # pragma: no cover - a store function was always given
            raise RuntimeError("embedding call returned without stored vectors")
        run.cache.update(stored.ids)
        run.created.update(stored.created)
        run.reused.update(key for key in stored.ids if key not in stored.created)

    @staticmethod
    async def _store_vectors(
        space: SpaceRef,
        keys: Sequence[CacheKey],
        session: AsyncSession,
        result: EmbeddingResult,
        model_call_id: uuid.UUID,
    ) -> store.StoredVectors:
        vectors = dict(zip(keys, result.vectors, strict=True))
        return await store.insert_vectors(session, space, vectors, model_call_id)

    async def _publish(
        self,
        run: _RunState,
        version_id: uuid.UUID,
        drafts: Sequence[ChunkDraft],
        keys: Sequence[CacheKey],
    ) -> str:
        """Transaction D: the version's chunk and link sets become visible together."""
        chunker = self._params.version
        writes = {"chunks_created": 0, "chunks_reused": 0, "links_created": 0, "links_reused": 0}
        try:
            async with self._sessions() as session, session.begin():
                await store.lock_version(session, version_id)
                existing = await store.existing_chunks(session, version_id, chunker)
                state = store.compare_chunks(existing, drafts)
                if state is ChunkState.MISMATCH:
                    raise _VersionFailed(FailureStage.CHUNK_STATE)
                if state is ChunkState.NONE:
                    chunks = await store.insert_chunks(session, version_id, chunker, drafts)
                    writes["chunks_created"] = len(chunks)
                else:
                    chunks = existing
                    writes["chunks_reused"] = len(chunks)
                expected = self._expected(chunks, keys)
                links = await store.link_state(session, chunks, expected)
                if links is LinkState.INCONSISTENT:
                    raise _VersionFailed(FailureStage.LINK_STATE)
                if links is LinkState.ZERO:
                    await store.insert_links(session, chunks, expected, run.cache)
                    writes["links_created"] = len(chunks)
                else:
                    writes["links_reused"] = len(chunks)
                await self._verify(session, version_id, drafts, expected)
        except IntegrityError as exc:
            # Raised inside D, which writes only this version's chunks and links.
            raise _VersionFailed(FailureStage.PERSIST_VERSION, type(exc).__name__) from None
        for counter, amount in writes.items():
            run.add(counter, amount)
        wrote = writes["chunks_created"] or writes["links_created"]
        return "versions_indexed" if wrote else "versions_already_indexed"

    def _expected(
        self, chunks: Sequence[StoredChunk], keys: Sequence[CacheKey]
    ) -> dict[uuid.UUID, CacheKey]:
        return {chunk.id: key for chunk, key in zip(chunks, keys, strict=True)}

    async def _verify(
        self,
        session: AsyncSession,
        version_id: uuid.UUID,
        drafts: Sequence[ChunkDraft],
        expected: dict[uuid.UUID, CacheKey],
    ) -> None:
        """Inside D, before commit: the published state is exactly what is
        required, or the whole version rolls back."""
        chunks = await store.existing_chunks(session, version_id, self._params.version)
        if store.compare_chunks(chunks, drafts) is not ChunkState.EXACT:
            raise _VersionFailed(FailureStage.PERSIST_VERSION, "ChunkVerification")
        if [c.ordinal for c in chunks] != list(range(len(drafts))):
            raise _VersionFailed(FailureStage.PERSIST_VERSION, "ChunkVerification")
        # document/v1 embeds the chunk text unchanged: the key's hash is its text hash.
        if any(expected[c.id].input_hash != c.text_hash for c in chunks):
            raise _VersionFailed(FailureStage.PERSIST_VERSION, "LinkVerification")
        if await store.link_state(session, chunks, expected) is not LinkState.COMPLETE:
            raise _VersionFailed(FailureStage.PERSIST_VERSION, "LinkVerification")


async def inspect_course(
    session_factory: async_sessionmaker[AsyncSession],
    course_id: uuid.UUID,
    space: EmbeddingSpace,
    *,
    params: ChunkingParams = SPAN_PACK_V1,
) -> list[VersionIndexReport]:
    """Index state of each CURRENT version of the course, re-derived exactly as
    indexing would see it. Needs only the space's identity: no provider, no
    credentials, no network. Reads only, in one session of its own."""
    async with session_factory() as session:
        return await inspect_course_in_session(session, course_id, space, params=params)


async def inspect_course_in_session(
    session: AsyncSession,
    course_id: uuid.UUID,
    space: EmbeddingSpace,
    *,
    params: ChunkingParams = SPAN_PACK_V1,
) -> list[VersionIndexReport]:
    """``inspect_course`` inside the caller's session and transaction: every
    read sees the caller's snapshot. Never commits, never opens another
    session, never calls a provider. The one implementation of index state."""
    if not await session.scalar(select(exists().where(Course.id == course_id))):
        raise CourseNotFoundError(f"course {course_id} does not exist")
    version_ids = await store.current_versions(session, course_id)
    space_ref = await store.find_space(session, space)
    return [
        await _inspect_version(session, version_id, space_ref, params) for version_id in version_ids
    ]


async def _inspect_version(
    session: AsyncSession,
    version_id: uuid.UUID,
    space: SpaceRef | None,
    params: ChunkingParams,
) -> VersionIndexReport:
    source = await store.load_version(session, version_id)
    existing = await store.existing_chunks(session, version_id, params.version)
    content, drafts = derive_chunks(source, params)
    chunkable = SourceContent.CHUNKABLE
    if content is not chunkable:
        return VersionIndexReport(version_id, content, None, None, len(existing))
    state = store.compare_chunks(existing, drafts)
    if state is not ChunkState.EXACT:
        return VersionIndexReport(version_id, chunkable, state, None, len(existing))
    if space is None:  # nothing was ever indexed in this space
        return VersionIndexReport(version_id, chunkable, state, LinkState.ZERO, len(existing))
    expected = {
        chunk.id: _key(space, document_input(draft.text))
        for chunk, draft in zip(existing, drafts, strict=True)
    }
    links = await store.link_state(session, existing, expected)
    return VersionIndexReport(version_id, chunkable, state, links, len(existing))


def _add_prompt_tokens(run: _RunState, tokens: int | None) -> None:
    """Sum over successful requests; unknown as soon as one did not report usage."""
    run.prompt_tokens = (
        None if tokens is None or run.prompt_tokens is None else (run.prompt_tokens + tokens)
    )


def _key(space: SpaceRef, item: EmbeddingInput) -> CacheKey:
    """The full cache key of an input in a space: never the hash alone."""
    return CacheKey(space.id, item.input_format, item.input_hash)
