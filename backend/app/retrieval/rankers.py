# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Candidate rankers over one verified search scope.

Each ranker runs inside the caller's session, so it reads the same snapshot
in which the caller classified the corpus, and returns ranked chunk
identities only: never text, never SourceSpan evidence (assembled later by
the search service). Both rankers apply the same scope predicates: the
course, current versions only, the versions verified searchable in that
snapshot, the active chunker and optional resource types.

Ranks start at 1 and follow a total order ending in the chunk ID, so equal
scores never make the order depend on the query plan. The raw score each
ranker keeps (cosine distance, ``ts_rank_cd``) is retriever-specific
diagnostic data, not a confidence, and never comparable across rankers.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Row, Select, and_, bindparam, cast, func, select, text
from sqlalchemy.dialects.postgresql import TSQUERY
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.embeddings import DOCUMENT_INPUT_FORMAT
from app.db.models import TEXT_SEARCH_CONFIG, Chunk, ChunkEmbedding, Embedding, Resource
from app.db.models import ResourceVersion as Version
from app.domain.enums import ResourceType
from app.retrieval.store import SpaceRef

# The lexical retrieval contract: chunks.text_tsv (english, migration 0006),
# an OR of the query's own normalised lexemes, ts_rank_cd without length
# normalisation. Changing any part of it is a new version.
LEXICAL_VERSION = "pg-fts-english/v1"
LEXICAL_RANK_NORMALIZATION = 0


@dataclass(frozen=True, slots=True)
class SearchScope:
    """What a ranker may rank. ``version_ids`` are the versions classified
    searchable in the caller's own snapshot, never an earlier one."""

    course_id: uuid.UUID
    version_ids: tuple[uuid.UUID, ...]
    chunker_version: str
    resource_types: frozenset[ResourceType] | None = None


@dataclass(frozen=True, slots=True)
class RankedCandidate:
    """One ranked chunk. Exactly one of the raw scores is set, by the ranker
    that produced it."""

    chunk_id: uuid.UUID
    resource_version_id: uuid.UUID
    chunk_ordinal: int
    rank: int  # 1-based, within its ranker
    vector_distance: float | None = None  # cosine distance: lower is closer
    lexical_score: float | None = None  # ts_rank_cd: higher is better


# Every ranker selects (chunk id, version id, chunk ordinal, raw score).
_Ranked = Select[tuple[uuid.UUID, uuid.UUID, int, float]]


def _scoped(statement: _Ranked, scope: SearchScope) -> _Ranked:
    statement = (
        statement.join(Version, Version.id == Chunk.resource_version_id)
        .join(
            Resource,
            and_(
                Resource.id == Version.resource_id,
                Resource.current_version_id == Chunk.resource_version_id,
            ),
        )
        .where(
            Resource.course_id == scope.course_id,
            Chunk.resource_version_id.in_(scope.version_ids),
            Chunk.chunker_version == scope.chunker_version,
        )
    )
    if scope.resource_types:
        statement = statement.where(Resource.type.in_(sorted(scope.resource_types)))
    return statement


def _candidates(rows: Sequence[Row[Any]], *, vector: bool) -> list[RankedCandidate]:
    return [
        RankedCandidate(
            chunk_id=chunk_id,
            resource_version_id=version_id,
            chunk_ordinal=ordinal,
            rank=rank,
            vector_distance=float(score) if vector else None,
            lexical_score=None if vector else float(score),
        )
        for rank, (chunk_id, version_id, ordinal, score) in enumerate(rows, start=1)
    ]


async def rank_vector(
    session: AsyncSession,
    scope: SearchScope,
    space: SpaceRef,
    vector: Sequence[float],
    depth: int,
) -> list[RankedCandidate]:
    """Exact cosine scan (no approximate index). The space and dimension
    filters are on the embedding row itself, so its scan qualifies every row
    before the distance is computed above it."""
    if not scope.version_ids:
        return []
    distance = Embedding.vector.cosine_distance(list(vector)).label("distance")
    statement = _scoped(
        select(Chunk.id, Chunk.resource_version_id, Chunk.ordinal, distance).select_from(Chunk),
        scope,
    )
    statement = (
        statement.join(ChunkEmbedding, ChunkEmbedding.chunk_id == Chunk.id)
        .join(Embedding, Embedding.id == ChunkEmbedding.embedding_id)
        .where(
            ChunkEmbedding.space_id == space.id,
            ChunkEmbedding.input_format == DOCUMENT_INPUT_FORMAT,
            Embedding.space_id == space.id,
            Embedding.dimensions == space.dimensions,
        )
        .order_by(distance, Chunk.resource_version_id, Chunk.ordinal, Chunk.id)
        .limit(depth)
    )
    rows = (await session.execute(statement)).all()
    return _candidates(rows, vector=True)


# PostgreSQL normalises the query itself, with the same explicit configuration
# as chunks.text_tsv. The query text is a bound parameter; tsvector lexemes come
# back sorted, and the C collation pins that order.
_QUERY_LEXEMES = text(
    "SELECT lexeme FROM unnest(tsvector_to_array("
    "to_tsvector(CAST(:config AS regconfig), :query))) AS lexeme "
    'ORDER BY lexeme COLLATE "C"'
)


async def query_lexemes(session: AsyncSession, query: str) -> list[str]:
    """The query's distinct normalised lexemes; empty for stop words or
    punctuation only."""
    rows = await session.scalars(_QUERY_LEXEMES, {"config": TEXT_SEARCH_CONFIG, "query": query})
    return list(rows)


def quote_lexeme(lexeme: str) -> str:
    """One lexeme as a quoted tsquery operand: inside quotes, tsquery syntax
    (``| & ! : * ( )``, spaces) is literal; quotes are doubled and
    backslashes escaped, as tsquery input requires."""
    return "'" + lexeme.replace("\\", "\\\\").replace("'", "''") + "'"


def or_tsquery(lexemes: Sequence[str]) -> str:
    """The OR of the given lexemes, as tsquery input text. Sent to PostgreSQL
    as a bound parameter, so it can never change SQL structure."""
    if not lexemes:
        raise ValueError("an OR query needs at least one lexeme")
    return " | ".join(quote_lexeme(lexeme) for lexeme in lexemes)


async def rank_lexical(
    session: AsyncSession,
    scope: SearchScope,
    query: str,
    depth: int,
) -> list[RankedCandidate]:
    """Chunks matching at least one of the query's lexemes, by ``ts_rank_cd``.
    A query without lexemes has no candidates (and sends no empty tsquery)."""
    if not scope.version_ids:
        return []
    lexemes = await query_lexemes(session, query)
    if not lexemes:
        return []
    tsquery = cast(bindparam("lexical_query", or_tsquery(lexemes)), TSQUERY)
    score = func.ts_rank_cd(Chunk.text_tsv, tsquery, LEXICAL_RANK_NORMALIZATION).label("score")
    statement = _scoped(
        select(Chunk.id, Chunk.resource_version_id, Chunk.ordinal, score).select_from(Chunk),
        scope,
    )
    statement = (
        statement.where(Chunk.text_tsv.bool_op("@@")(tsquery))
        .order_by(score.desc(), Chunk.resource_version_id, Chunk.ordinal, Chunk.id)
        .limit(depth)
    )
    rows = (await session.execute(statement)).all()
    return _candidates(rows, vector=False)
