# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""SQL for indexing: spaces, the embedding cache, versions, chunk and link state.

Every function runs inside a session the caller (the indexing service) owns;
none of them commits. Cache identity is always the full key
(space, input format, input hash): an input hash alone is not enough.
"""

from __future__ import annotations

import enum
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.embeddings import EmbeddingSpace
from app.db.models import (
    Chunk,
    ChunkEmbedding,
    Embedding,
    EmbeddingSpaceRecord,
    Resource,
    ResourceVersion,
    SourceSpan,
)
from app.retrieval.chunking import ChunkSpan, chunk_spans
from app.retrieval.provenance import ChunkDraft


@dataclass(frozen=True, slots=True, order=True)
class CacheKey:
    """The identity of one cached vector."""

    space_id: uuid.UUID
    input_format: str
    input_hash: str


@dataclass(frozen=True, slots=True)
class SpaceRef:
    id: uuid.UUID
    dimensions: int


@dataclass(frozen=True, slots=True)
class VersionSource:
    version_id: uuid.UUID
    extracted_text: str
    spans: list[ChunkSpan]


@dataclass(frozen=True, slots=True)
class StoredChunk:
    id: uuid.UUID
    ordinal: int
    start_offset: int
    end_offset: int
    first_span_ordinal: int
    last_span_ordinal: int
    text_hash: str
    estimated_tokens: int


class ChunkState(enum.Enum):
    NONE = "none"  # no chunks for (version, chunker version)
    EXACT = "exact"  # exactly the deterministic drafts
    MISMATCH = "mismatch"  # anything else: incomplete, extra or different


class LinkState(enum.Enum):
    ZERO = "zero"  # no link in the active space and format
    COMPLETE = "complete"  # one correct link per chunk
    INCONSISTENT = "inconsistent"  # anything else


# --- spaces and the embedding cache ---------------------------------------------------


async def ensure_space(session: AsyncSession, space: EmbeddingSpace) -> SpaceRef:
    """The space's row, created if needed (safe under concurrent creation)."""
    await session.execute(
        insert(EmbeddingSpaceRecord)
        .values(
            provider=space.provider,
            model=space.model,
            dimensions=space.dimensions,
            space_version=space.space_version,
        )
        .on_conflict_do_nothing(constraint="uq_embedding_spaces_identity")
    )
    row = (
        await session.execute(
            select(EmbeddingSpaceRecord.id, EmbeddingSpaceRecord.dimensions).where(
                EmbeddingSpaceRecord.provider == space.provider,
                EmbeddingSpaceRecord.model == space.model,
                EmbeddingSpaceRecord.dimensions == space.dimensions,
                EmbeddingSpaceRecord.space_version == space.space_version,
            )
        )
    ).one()
    return SpaceRef(row.id, row.dimensions)


async def find_cached(session: AsyncSession, keys: Iterable[CacheKey]) -> dict[CacheKey, uuid.UUID]:
    """Embedding IDs for the keys that are already cached."""
    found: dict[CacheKey, uuid.UUID] = {}
    by_partition: dict[tuple[uuid.UUID, str], set[str]] = {}
    for key in keys:
        by_partition.setdefault((key.space_id, key.input_format), set()).add(key.input_hash)
    for (space_id, input_format), hashes in sorted(by_partition.items()):
        rows = await session.execute(
            select(Embedding.input_hash, Embedding.id).where(
                Embedding.space_id == space_id,
                Embedding.input_format == input_format,
                Embedding.input_hash.in_(sorted(hashes)),
            )
        )
        for input_hash, embedding_id in rows:
            found[CacheKey(space_id, input_format, input_hash)] = embedding_id
    return found


@dataclass(frozen=True, slots=True)
class StoredVectors:
    ids: dict[CacheKey, uuid.UUID]
    # Keys this transaction inserted; the others were won by another writer.
    created: frozenset[CacheKey]


async def insert_vectors(
    session: AsyncSession,
    space: SpaceRef,
    vectors: Mapping[CacheKey, Sequence[float]],
    model_call_id: uuid.UUID,
) -> StoredVectors:
    """Insert vectors for keys not cached yet; return the canonical ID of every key.

    A key another writer inserted first keeps that writer's row (and its
    model_call_id): this call's vector for it is simply not stored.
    """
    if not vectors:
        return StoredVectors({}, frozenset())
    keys = sorted(vectors)
    for key in keys:
        if key.space_id != space.id:
            raise ValueError("vectors must belong to the given space")
    inserted = await session.execute(
        insert(Embedding)
        .values(
            [
                {
                    "space_id": key.space_id,
                    "dimensions": space.dimensions,
                    "input_format": key.input_format,
                    "input_hash": key.input_hash,
                    "vector": list(vectors[key]),
                    "model_call_id": model_call_id,
                }
                for key in keys
            ]
        )
        .on_conflict_do_nothing(constraint="uq_embeddings_cache_key")
        .returning(Embedding.input_format, Embedding.input_hash)
    )
    created = frozenset(CacheKey(space.id, fmt, h) for fmt, h in inserted)
    ids = await find_cached(session, keys)
    if len(ids) != len(keys):  # pragma: no cover - inserted or conflicted, so present
        raise RuntimeError("a cached vector vanished after insert")
    return StoredVectors(ids, created)


# --- versions and chunks -----------------------------------------------------------------


async def current_versions(session: AsyncSession, course_id: uuid.UUID) -> list[uuid.UUID]:
    """The course's current resource versions, in a stable order."""
    rows = await session.scalars(
        select(Resource.current_version_id)
        .where(Resource.course_id == course_id, Resource.current_version_id.is_not(None))
        .order_by(Resource.id)
    )
    return [version_id for version_id in rows if version_id is not None]


async def still_current(session: AsyncSession, version_ids: Sequence[uuid.UUID]) -> set[uuid.UUID]:
    rows = await session.scalars(
        select(Resource.current_version_id).where(Resource.current_version_id.in_(version_ids))
    )
    return {version_id for version_id in rows if version_id is not None}


async def load_version(session: AsyncSession, version_id: uuid.UUID) -> VersionSource | None:
    text = await session.scalar(
        select(ResourceVersion.extracted_text).where(ResourceVersion.id == version_id)
    )
    if text is None:
        return None
    spans = await session.scalars(
        select(SourceSpan)
        .where(SourceSpan.resource_version_id == version_id)
        .order_by(SourceSpan.ordinal)
    )
    return VersionSource(version_id, text, chunk_spans(spans))


async def lock_version(session: AsyncSession, version_id: uuid.UUID) -> None:
    """Serialise publication of one version's chunks and links.

    FOR NO KEY UPDATE conflicts only with another lock of the same kind (or
    stronger): readers, ingestion and rows that merely reference the version
    are not blocked.
    """
    await session.execute(
        select(ResourceVersion.id)
        .where(ResourceVersion.id == version_id)
        .with_for_update(key_share=True)  # renders FOR NO KEY UPDATE on PostgreSQL
    )


async def existing_chunks(
    session: AsyncSession, version_id: uuid.UUID, chunker_version: str
) -> list[StoredChunk]:
    rows = await session.execute(
        select(
            Chunk.id,
            Chunk.ordinal,
            Chunk.start_offset,
            Chunk.end_offset,
            Chunk.first_span_ordinal,
            Chunk.last_span_ordinal,
            Chunk.text_hash,
            Chunk.estimated_tokens,
        )
        .where(Chunk.resource_version_id == version_id, Chunk.chunker_version == chunker_version)
        .order_by(Chunk.ordinal)
    )
    return [StoredChunk(*row) for row in rows]


def compare_chunks(existing: Sequence[StoredChunk], drafts: Sequence[ChunkDraft]) -> ChunkState:
    if not existing:
        return ChunkState.NONE
    stored = [
        (
            c.ordinal,
            c.start_offset,
            c.end_offset,
            c.first_span_ordinal,
            c.last_span_ordinal,
            c.text_hash,
            c.estimated_tokens,
        )
        for c in existing
    ]
    expected = [
        (
            d.ordinal,
            d.start_offset,
            d.end_offset,
            d.first_span_ordinal,
            d.last_span_ordinal,
            d.text_hash,
            d.estimated_tokens,
        )
        for d in drafts
    ]
    return ChunkState.EXACT if stored == expected else ChunkState.MISMATCH


async def insert_chunks(
    session: AsyncSession,
    version_id: uuid.UUID,
    chunker_version: str,
    drafts: Sequence[ChunkDraft],
) -> list[StoredChunk]:
    """Insert the whole deterministic chunk set (each row checked by the
    chunks_provenance trigger) and return it as stored."""
    rows = [
        {
            "resource_version_id": version_id,
            "chunker_version": chunker_version,
            "ordinal": d.ordinal,
            "start_offset": d.start_offset,
            "end_offset": d.end_offset,
            "first_span_ordinal": d.first_span_ordinal,
            "last_span_ordinal": d.last_span_ordinal,
            "text": d.text,
            "text_hash": d.text_hash,
            "estimated_tokens": d.estimated_tokens,
        }
        for d in drafts
    ]
    for page in _pages(rows):
        await session.execute(insert(Chunk).values(page))
    return await existing_chunks(session, version_id, chunker_version)


# --- links ----------------------------------------------------------------------------------


async def link_state(
    session: AsyncSession,
    chunks: Sequence[StoredChunk],
    expected: Mapping[uuid.UUID, CacheKey],
) -> LinkState:
    """Classify the links of ``chunks`` in the expected keys' space and format.

    COMPLETE means exactly one link per chunk, each to the embedding cached
    under that chunk's expected key. Links in other spaces or formats are not
    looked at: they may coexist (an earlier embedding space).
    """
    keys = set(expected.values())
    if len({(k.space_id, k.input_format) for k in keys}) > 1:
        raise ValueError("link state is classified for one space and input format")
    if not chunks:
        return LinkState.ZERO
    space_id, input_format = next(iter(keys)).space_id, next(iter(keys)).input_format
    rows = (
        await session.execute(
            select(ChunkEmbedding.chunk_id, Embedding.space_id, Embedding.input_format,
                   Embedding.input_hash)
            .join(Embedding, Embedding.id == ChunkEmbedding.embedding_id)
            .where(
                ChunkEmbedding.chunk_id.in_([c.id for c in chunks]),
                ChunkEmbedding.space_id == space_id,
                ChunkEmbedding.input_format == input_format,
            )
        )
    ).all()  # fmt: skip
    if not rows:
        return LinkState.ZERO
    linked: dict[uuid.UUID, CacheKey] = {}
    for chunk_id, row_space, row_format, row_hash in rows:
        if chunk_id in linked:  # two vectors for one chunk in this space and format
            return LinkState.INCONSISTENT
        linked[chunk_id] = CacheKey(row_space, row_format, row_hash)
    complete = len(linked) == len(chunks) and all(
        linked.get(chunk.id) == expected[chunk.id] for chunk in chunks
    )
    return LinkState.COMPLETE if complete else LinkState.INCONSISTENT


async def insert_links(
    session: AsyncSession,
    chunks: Sequence[StoredChunk],
    expected: Mapping[uuid.UUID, CacheKey],
    embedding_ids: Mapping[CacheKey, uuid.UUID],
) -> None:
    rows = [
        {
            "chunk_id": chunk.id,
            "embedding_id": embedding_ids[expected[chunk.id]],
            "space_id": expected[chunk.id].space_id,
            "input_format": expected[chunk.id].input_format,
        }
        for chunk in chunks
    ]
    for page in _pages(rows):
        await session.execute(insert(ChunkEmbedding).values(page))


# --- inspection -----------------------------------------------------------------------------


async def find_space(session: AsyncSession, space: EmbeddingSpace) -> SpaceRef | None:
    """The space's row if it exists (read only: never creates it)."""
    row = (
        await session.execute(
            select(EmbeddingSpaceRecord.id, EmbeddingSpaceRecord.dimensions).where(
                EmbeddingSpaceRecord.provider == space.provider,
                EmbeddingSpaceRecord.model == space.model,
                EmbeddingSpaceRecord.dimensions == space.dimensions,
                EmbeddingSpaceRecord.space_version == space.space_version,
            )
        )
    ).one_or_none()
    return None if row is None else SpaceRef(row.id, row.dimensions)


# asyncpg allows at most 32,767 parameters per statement.
_ROWS_PER_INSERT = 1000


def _pages[R](rows: Sequence[R]) -> Iterable[Sequence[R]]:
    for start in range(0, len(rows), _ROWS_PER_INSERT):
        yield rows[start : start + _ROWS_PER_INSERT]
