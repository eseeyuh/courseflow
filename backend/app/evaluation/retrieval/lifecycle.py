# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Seed an evaluation database with a frozen dataset through the production
lifecycle, and describe the state that results.

Superseded versions are made stale the way production makes them stale, not
by inserting rows: ``seed_and_index`` works in rounds. Round n ingests
version n of every resource that has one (so it becomes current), then
indexes every course with the production ``IndexingService``. Before the
next round, every version that a later round will supersede must be fully
indexed (each chunk linked to an embedding). After the last round the stale
versions keep their chunks, embeddings and links but are no longer current:
an otherwise searchable version that a search must exclude because it is
not the current ResourceVersion.

Every step commits, as production does, so this runs only against a
dedicated database (the guarded evaluation database, or a test database).
Query embeddings are never stored: search embeds queries ephemerally.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai.embeddings import DOCUMENT_INPUT_FORMAT, EmbeddingProvider, EmbeddingSpace
from app.db.models import (
    Chunk,
    ChunkEmbedding,
    Course,
    Embedding,
    Resource,
    ResourceVersion,
)
from app.domain.enums import WorkflowRunStatus
from app.evaluation.retrieval.dataset import Dataset
from app.evaluation.retrieval.resolve import (
    IngestedVersion,
    VersionKey,
    create_courses,
    ingest_version,
    production_limits,
)
from app.evaluation.retrieval.snapshot import ExpectedInputs
from app.ingestion.storage import RawObjectStore
from app.retrieval import store
from app.retrieval.chunking import SPAN_PACK_V1
from app.retrieval.indexing import IndexingService, IndexRunSummary


class LifecycleError(RuntimeError):
    """Seeding did not reach the state the lifecycle requires."""


@dataclass(frozen=True, slots=True)
class VersionState:
    key: VersionKey
    current: bool
    chunks: int
    linked_chunks: int  # chunks with a link in the space and document format

    @property
    def fully_linked(self) -> bool:
        return self.chunks > 0 and self.linked_chunks == self.chunks


@dataclass(frozen=True, slots=True)
class SeedRound:
    round: int
    ingested: tuple[VersionKey, ...]
    index_runs: tuple[IndexRunSummary, ...]
    # Versions a later round supersedes, as they were after this round's indexing.
    to_be_superseded: tuple[VersionState, ...]


@dataclass(frozen=True, slots=True)
class SeedResult:
    versions: dict[VersionKey, IngestedVersion]
    rounds: tuple[SeedRound, ...]


async def seed_and_index(
    session_factory: async_sessionmaker[AsyncSession],
    raw_store: RawObjectStore,
    dataset: Dataset,
    provider: EmbeddingProvider,
    *,
    batch_max_items: int,
    batch_max_estimated_tokens: int,
) -> SeedResult:
    indexer = IndexingService(
        session_factory,
        provider,
        batch_max_items=batch_max_items,
        batch_max_estimated_tokens=batch_max_estimated_tokens,
        params=SPAN_PACK_V1,
    )
    limits = production_limits()
    async with session_factory() as session, session.begin():
        courses = await create_courses(session, dataset)

    versions: dict[VersionKey, IngestedVersion] = {}
    rounds = []
    last_round = max(len(r.versions) for c in dataset.manifest.courses for r in c.resources)
    for number in range(1, last_round + 1):
        keys = [
            (course.key, resource.key, number)
            for course in dataset.manifest.courses
            for resource in course.resources
            if number <= resource.latest
        ]
        async with session_factory() as session, session.begin():
            for key in keys:
                versions[key] = await ingest_version(
                    session, raw_store, dataset, key, course_id=courses[key[0]], limits=limits
                )
        summaries = []
        for course_key, course_id in courses.items():
            result = await indexer.index_course(course_id)
            if result.status is not WorkflowRunStatus.SUCCEEDED or result.summary.failures:
                raise LifecycleError(
                    f"round {number}: indexing {course_key} ended {result.status.value}"
                )
            summaries.append(result.summary)
        superseded_later = [k for k in keys if not versions[k].current]
        async with session_factory() as session:
            states = await version_states(
                session, [versions[k] for k in superseded_later], provider.space
            )
        not_ready = [s for s in states if not (s.current and s.fully_linked)]
        if not_ready:
            raise LifecycleError(
                f"round {number}: {[s.key for s in not_ready]} must be current and fully "
                "indexed before a later version supersedes them"
            )
        rounds.append(SeedRound(number, tuple(keys), tuple(summaries), tuple(states)))
    return SeedResult(versions, tuple(rounds))


async def version_states(
    session: AsyncSession, versions: Sequence[IngestedVersion], space: EmbeddingSpace
) -> list[VersionState]:
    space_ref = await store.find_space(session, space)
    states = []
    for version in versions:
        current_id = await session.scalar(
            select(Resource.current_version_id).where(Resource.id == version.resource_id)
        )
        chunk_ids = select(Chunk.id).where(
            Chunk.resource_version_id == version.version_id,
            Chunk.chunker_version == SPAN_PACK_V1.version,
        )
        chunks = await session.scalar(select(func.count()).select_from(chunk_ids.subquery()))
        linked = 0
        if space_ref is not None:
            linked = await session.scalar(
                select(func.count(func.distinct(ChunkEmbedding.chunk_id))).where(
                    ChunkEmbedding.chunk_id.in_(chunk_ids),
                    ChunkEmbedding.space_id == space_ref.id,
                    ChunkEmbedding.input_format == DOCUMENT_INPUT_FORMAT,
                )
            )
        states.append(
            VersionState(
                key=version.key,
                current=current_id == version.version_id,
                chunks=int(chunks or 0),
                linked_chunks=int(linked or 0),
            )
        )
    return states


# --- the expected state -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CorpusState:
    courses: int
    resources: int
    resource_versions: int
    chunks: int
    current_version_chunks: int
    document_embeddings: int  # in the space, document format
    other_embeddings: int  # any other space or format (query vectors included)
    document_input_hashes: frozenset[str]
    versions: tuple[VersionState, ...]


async def _count(session: AsyncSession, statement: object) -> int:
    return int(await session.scalar(statement) or 0)  # type: ignore[call-overload]


async def corpus_state(
    session: AsyncSession, seeded: SeedResult, space: EmbeddingSpace
) -> CorpusState:
    space_ref = await store.find_space(session, space)
    space_id = space_ref.id if space_ref is not None else uuid.UUID(int=0)
    in_space = (Embedding.space_id == space_id) & (Embedding.input_format == DOCUMENT_INPUT_FORMAT)
    current_chunks = (
        select(func.count())
        .select_from(Chunk)
        .join(Resource, Resource.current_version_id == Chunk.resource_version_id)
    )
    hashes = await session.scalars(select(Embedding.input_hash).where(in_space))
    return CorpusState(
        courses=await _count(session, select(func.count()).select_from(Course)),
        resources=await _count(session, select(func.count()).select_from(Resource)),
        resource_versions=await _count(session, select(func.count()).select_from(ResourceVersion)),
        chunks=await _count(session, select(func.count()).select_from(Chunk)),
        current_version_chunks=await _count(session, current_chunks),
        document_embeddings=await _count(
            session, select(func.count()).select_from(Embedding).where(in_space)
        ),
        other_embeddings=await _count(
            session, select(func.count()).select_from(Embedding).where(~in_space)
        ),
        document_input_hashes=frozenset(hashes),
        versions=tuple(await version_states(session, list(seeded.versions.values()), space)),
    )


def expected_state_problems(
    state: CorpusState, dataset: Dataset, inputs: ExpectedInputs
) -> list[str]:
    """Every way the database differs from exactly the frozen dataset, fully
    indexed, with each superseded version stale but still indexed."""
    manifest = dataset.manifest
    resources = [r for c in manifest.courses for r in c.resources]
    expected = {
        "courses": len(manifest.courses),
        "resources": len(resources),
        "resource_versions": sum(len(r.versions) for r in resources),
        "chunks": manifest.expected_chunks.all_versions,
        "current_version_chunks": manifest.expected_chunks.current_versions,
        "document_embeddings": len(inputs.documents),
        "other_embeddings": 0,
    }
    problems = [
        f"{name}: {getattr(state, name)} != expected {value}"
        for name, value in expected.items()
        if getattr(state, name) != value
    ]
    if state.document_input_hashes != {item.input_hash for item in inputs.documents}:
        problems.append("stored document input hashes != the snapshot's expected document keys")
    for version in state.versions:
        _, resource_key, number = version.key
        latest = dataset.resource(version.key[0], resource_key).latest
        where = "/".join(map(str, version.key))
        if version.current != (number == latest):
            problems.append(f"{where}: current={version.current}, expected {number == latest}")
        if not version.fully_linked:
            problems.append(
                f"{where}: {version.linked_chunks}/{version.chunks} chunks have embedding links"
            )
    return problems
