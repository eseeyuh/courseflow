# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Course-scoped vector search over complete, verified index states only.

1. Validate the request (no network).
2. First verification, before any paid call: the course exists, and every
   CURRENT version is classified with the same derived index state indexing
   uses (re-chunked and compared, links checked):
   - searchable: exact chunk set, complete and correct links;
   - unindexed: no chunks, or exact chunks with no links in this space, or
     valid spans with no visible text (nothing to retrieve);
   - corrupt: anything else, including spans the chunker cannot safely use.
     One corrupt current version fails the search (IndexStateError): a
     partial or inconsistent index is never searched.
   No searchable version: an empty result, and no provider call. This check
   exists to avoid paying for a search that cannot run.
3. Embed the question as query/v1 in the active space: one standalone
   ModelCall. Query vectors are never stored or cached: every search pays.
4. Second verification, with the same rules, after the external call: it
   makes sure the corpus actually ranked is still trusted and current
   (no lock is held across the call; no second provider request is made).
5. Exact cosine scan restricted to the versions verified the second time,
   the course, current versions, the active chunker, space and document format.
6. Evidence: each hit's SourceSpans with the exact overlap. Any provenance
   inconsistency fails the whole search (ProvenanceError).

Chunk text is retrieval context; a SourceSpan plus its sub-range is evidence.
``score`` is ``1 - cosine distance``: a ranking similarity, never a confidence.
Logs carry IDs and counts only: never query text, chunk text, excerpts or URIs.
"""

from __future__ import annotations

import dataclasses
import enum
import logging
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai.embeddings import (
    DOCUMENT_INPUT_FORMAT,
    QUERY_V1_MAX_CHARS,
    EmbeddingProvider,
    query_input,
)
from app.ai.model_calls import RecordedCallError, VectorStorageError, call_embeddings_recorded
from app.db.models import Chunk, ChunkEmbedding, Embedding, Resource, ResourceVersion, SourceSpan
from app.domain.enums import BlockKind, ModelCallErrorCategory, ResourceType
from app.retrieval import store
from app.retrieval.chunking import SPAN_PACK_V1, ChunkingParams
from app.retrieval.indexing import (
    SourceContent,
    SpaceSummary,
    VersionIndexReport,
    inspect_course,
)
from app.retrieval.store import ChunkState, LinkState, SpaceRef

logger = logging.getLogger(__name__)

DEFAULT_K = 5
MAX_K = 50


# --- errors ---------------------------------------------------------------------------------


class InvalidSearchRequest(ValueError):
    """The request is invalid. Names the offending fields, never their values."""

    def __init__(self, fields: Sequence[str]) -> None:
        super().__init__(f"invalid search request: {', '.join(fields)}")
        self.fields = tuple(fields)


class IndexStateError(RuntimeError):
    """A current version's derived index state is inconsistent: search fails
    closed rather than search a partial or corrupt index."""

    def __init__(
        self,
        course_id: uuid.UUID,
        corrupt_version_ids: Sequence[uuid.UUID],
        current_versions: int,
        query_model_call_id: uuid.UUID | None = None,
    ) -> None:
        super().__init__(
            f"{len(corrupt_version_ids)} of {current_versions} current version(s) of course "
            f"{course_id} have inconsistent index state"
        )
        self.course_id = course_id
        self.corrupt_version_ids = tuple(corrupt_version_ids)
        self.current_versions = current_versions
        # Set when the state was found inconsistent after the query was embedded
        # (that successful call stays recorded as it was).
        self.query_model_call_id = query_model_call_id


class SearchProviderError(RuntimeError):
    """Embedding the query failed (recorded as a failed ModelCall)."""

    def __init__(self, category: ModelCallErrorCategory, model_call_id: uuid.UUID | None) -> None:
        super().__init__(f"query embedding failed: {category.value}")
        self.category = category
        self.model_call_id = model_call_id


class QueryCallNotRecordedError(RuntimeError):
    """The query was embedded, but its ModelCall could not be stored, so the
    search stops: storage is failing. ``model_call_id`` is the best-effort
    fallback row, or None if that failed too."""

    def __init__(self, model_call_id: uuid.UUID | None) -> None:
        super().__init__("the query embedding call could not be recorded")
        self.model_call_id = model_call_id


class ProvenanceError(RuntimeError):
    """A hit's chunk does not resolve exactly to its source spans."""

    def __init__(self, chunk_id: uuid.UUID, reason: str) -> None:
        super().__init__(f"chunk {chunk_id}: {reason}")
        self.chunk_id = chunk_id
        self.reason = reason


# --- request and result ---------------------------------------------------------------------


class SearchRequest(BaseModel):
    # hide_input_in_errors: a validation error must never echo the query.
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    course_id: uuid.UUID
    query: str
    k: int = Field(default=DEFAULT_K, ge=1, le=MAX_K)
    resource_types: frozenset[ResourceType] | None = None

    @field_validator("query")
    @classmethod
    def _valid_query(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("query is empty")
        if len(value) > QUERY_V1_MAX_CHARS:
            raise ValueError(f"query is longer than {QUERY_V1_MAX_CHARS} characters")
        return value

    @field_validator("resource_types")
    @classmethod
    def _types_not_empty(cls, value: frozenset[ResourceType] | None) -> Any:
        if value is not None and not value:
            raise ValueError("resource_types, when given, must not be empty")
        return value

    @classmethod
    def build(cls, **values: Any) -> SearchRequest:
        """Validate, raising InvalidSearchRequest (field names only)."""
        try:
            return cls(**values)
        except ValidationError as exc:
            raise InvalidSearchRequest(
                sorted({str(error["loc"][0]) for error in exc.errors()})
            ) from None


@dataclass(frozen=True, slots=True)
class ResourceRef:
    id: uuid.UUID
    title: str
    type: ResourceType
    source_uri: str  # the logical identity; output only, never logged


@dataclass(frozen=True, slots=True)
class VersionRef:
    id: uuid.UUID
    version_number: int


@dataclass(frozen=True, slots=True)
class SpanEvidence:
    span_id: uuid.UUID
    ordinal: int
    block_kind: BlockKind
    page_number: int | None
    slide_number: int | None
    section_path: str | None
    span_start: int
    span_end: int
    # The part of the span inside the chunk: absolute offsets in extracted_text,
    # and the same range as code-point offsets into ``excerpt``.
    overlap_start: int
    overlap_end: int
    relative_start: int
    relative_end: int
    coverage: Literal["full", "partial"]
    excerpt: str


@dataclass(frozen=True, slots=True)
class SearchHit:
    rank: int
    score: float  # 1 - cosine distance: ranking similarity, never a confidence
    chunk_id: uuid.UUID
    chunk_ordinal: int
    start_offset: int
    end_offset: int
    text: str  # retrieval context; the evidence is ``spans``
    resource: ResourceRef
    version: VersionRef
    spans: tuple[SpanEvidence, ...]


@dataclass(frozen=True, slots=True)
class SearchResult:
    course_id: uuid.UUID
    chunker_version: str
    space: SpaceSummary
    query_format: str
    document_format: str
    # None when no provider call was needed (nothing searchable).
    query_model_call_id: uuid.UUID | None
    current_versions: int
    searchable_versions: int
    unindexed_versions: int
    hits: tuple[SearchHit, ...]


# --- index state preflight -------------------------------------------------------------------


class VersionSearchState(enum.StrEnum):
    SEARCHABLE = "searchable"
    UNINDEXED = "unindexed"
    CORRUPT = "corrupt"


def classify(report: VersionIndexReport) -> VersionSearchState:
    """The same derived state indexing and ``status`` use, read for search.

    A version with valid spans but no visible text has nothing to retrieve:
    unindexed (it never blocks its siblings), unless chunks exist for it.
    Spans the chunker cannot safely use are corrupt, never merely unindexed.
    """
    if report.indexed:
        return VersionSearchState.SEARCHABLE
    if report.content is SourceContent.NO_VISIBLE_TEXT:
        return VersionSearchState.UNINDEXED if report.chunks == 0 else VersionSearchState.CORRUPT
    if report.content is SourceContent.INVALID:
        return VersionSearchState.CORRUPT
    if report.chunk_state is ChunkState.NONE:
        return VersionSearchState.UNINDEXED
    if report.chunk_state is ChunkState.EXACT and report.link_state is LinkState.ZERO:
        return VersionSearchState.UNINDEXED
    return VersionSearchState.CORRUPT


@dataclass(frozen=True, slots=True)
class _Corpus:
    """The verified search corpus at one moment: current versions and the
    subset that is searchable."""

    current: int
    searchable: tuple[uuid.UUID, ...]


# --- the service ----------------------------------------------------------------------------


class SearchService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        provider: EmbeddingProvider,
        *,
        params: ChunkingParams = SPAN_PACK_V1,
    ) -> None:
        self._sessions = session_factory
        self._provider = provider
        self._params = params

    async def search_course(self, request: SearchRequest) -> SearchResult:
        """Two read-only verifications of the corpus, neither held across the
        provider call: the first avoids paying for a search that cannot run
        (nothing searchable, or corrupt state); the second, after the query is
        embedded, makes sure the corpus actually ranked is still trusted and
        current. Only the versions verified the second time are ranked."""
        started = time.perf_counter()
        space = self._provider.space
        query = query_input(request.query)  # the exact query/v1 provider input

        corpus = await self._verified_corpus(request.course_id, query_model_call_id=None)
        result = SearchResult(
            course_id=request.course_id,
            chunker_version=self._params.version,
            space=SpaceSummary(
                provider=space.provider,
                model=space.model,
                dimensions=space.dimensions,
                space_version=space.space_version,
            ),
            query_format=query.input_format,
            document_format=DOCUMENT_INPUT_FORMAT,
            query_model_call_id=None,
            current_versions=corpus.current,
            searchable_versions=len(corpus.searchable),
            unindexed_versions=corpus.current - len(corpus.searchable),
            hits=(),
        )
        if not corpus.searchable:
            self._log(request, result, started)
            return result

        async with self._sessions() as session:
            space_ref = await store.find_space(session, space)
        if space_ref is None:  # pragma: no cover - a searchable version implies the space row
            raise RuntimeError("searchable versions without an embedding space row")
        try:
            recorded = await call_embeddings_recorded(self._provider, self._sessions, [query])
        except RecordedCallError as failed:
            raise SearchProviderError(failed.error.category, failed.model_call_id) from failed
        except VectorStorageError as failed:
            raise QueryCallNotRecordedError(failed.model_call_id) from failed
        vector = recorded.result.vectors[0]
        if len(vector) != space_ref.dimensions:  # pragma: no cover - the recorder rejects it
            raise RuntimeError("query vector does not match the space's dimensions")

        # Re-verify after the external call: never a second provider request.
        corpus = await self._verified_corpus(
            request.course_id, query_model_call_id=recorded.model_call_id
        )
        result = dataclasses.replace(
            result,
            query_model_call_id=recorded.model_call_id,
            current_versions=corpus.current,
            searchable_versions=len(corpus.searchable),
            unindexed_versions=corpus.current - len(corpus.searchable),
        )
        if corpus.searchable:
            rows = await self._rank(request, space_ref, corpus.searchable, vector)
            result = dataclasses.replace(result, hits=await self._assemble(rows))
        self._log(request, result, started)
        return result

    async def _verified_corpus(
        self, course_id: uuid.UUID, *, query_model_call_id: uuid.UUID | None
    ) -> _Corpus:
        """Classify every current version; fail closed on any corrupt one."""
        reports = await inspect_course(
            self._sessions, course_id, self._provider.space, params=self._params
        )
        states = [(report.resource_version_id, classify(report)) for report in reports]
        corrupt = [v for v, state in states if state is VersionSearchState.CORRUPT]
        if corrupt:
            logger.warning(
                "retrieval.search_refused",
                extra={
                    "course_id": str(course_id),
                    "current_versions": len(states),
                    "corrupt_versions": len(corrupt),
                    "model_call_id": query_model_call_id and str(query_model_call_id),
                },
            )
            raise IndexStateError(course_id, corrupt, len(states), query_model_call_id)
        searchable = tuple(v for v, state in states if state is VersionSearchState.SEARCHABLE)
        return _Corpus(current=len(states), searchable=searchable)

    async def _rank(
        self,
        request: SearchRequest,
        space: SpaceRef,
        searchable: Sequence[uuid.UUID],
        vector: Sequence[float],
    ) -> list[Any]:
        """Exact cosine scan. The space and dimension filters are on the
        embedding row itself, so its scan qualifies every row before the
        distance is computed above it."""
        distance = Embedding.vector.cosine_distance(list(vector)).label("distance")
        statement = (
            select(
                Chunk.id,
                Chunk.ordinal,
                Chunk.start_offset,
                Chunk.end_offset,
                Chunk.first_span_ordinal,
                Chunk.last_span_ordinal,
                Chunk.text,
                ResourceVersion.id.label("version_id"),
                ResourceVersion.version_number,
                Resource.id.label("resource_id"),
                Resource.title,
                Resource.type,
                Resource.source_uri,
                distance,
            )
            .select_from(Chunk)
            .join(ResourceVersion, ResourceVersion.id == Chunk.resource_version_id)
            .join(
                Resource,
                and_(
                    Resource.id == ResourceVersion.resource_id,
                    Resource.current_version_id == Chunk.resource_version_id,
                ),
            )
            .join(ChunkEmbedding, ChunkEmbedding.chunk_id == Chunk.id)
            .join(Embedding, Embedding.id == ChunkEmbedding.embedding_id)
            .where(
                Resource.course_id == request.course_id,
                Chunk.resource_version_id.in_(searchable),
                Chunk.chunker_version == self._params.version,
                ChunkEmbedding.space_id == space.id,
                ChunkEmbedding.input_format == DOCUMENT_INPUT_FORMAT,
                Embedding.space_id == space.id,
                Embedding.dimensions == space.dimensions,
            )
            .order_by(distance, ResourceVersion.id, Chunk.ordinal)
            .limit(request.k)
        )
        if request.resource_types:
            statement = statement.where(Resource.type.in_(sorted(request.resource_types)))
        async with self._sessions() as session:
            return list((await session.execute(statement)).all())

    async def _assemble(self, rows: Sequence[Any]) -> tuple[SearchHit, ...]:
        if not rows:
            return ()
        wanted = or_(
            *(
                and_(
                    SourceSpan.resource_version_id == row.version_id,
                    SourceSpan.ordinal.between(row.first_span_ordinal, row.last_span_ordinal),
                )
                for row in rows
            )
        )
        async with self._sessions() as session:
            spans = (
                await session.execute(
                    select(
                        SourceSpan.id,
                        SourceSpan.resource_version_id,
                        SourceSpan.ordinal,
                        SourceSpan.block_kind,
                        SourceSpan.page_number,
                        SourceSpan.slide_number,
                        SourceSpan.section_path,
                        SourceSpan.start_offset,
                        SourceSpan.end_offset,
                        SourceSpan.excerpt,
                    )
                    .where(wanted)
                    .order_by(SourceSpan.resource_version_id, SourceSpan.ordinal)
                )
            ).all()
        by_version: dict[uuid.UUID, list[Any]] = {}
        for span in spans:
            by_version.setdefault(span.resource_version_id, []).append(span)
        return tuple(
            _hit(rank, row, by_version.get(row.version_id, []))
            for rank, row in enumerate(rows, start=1)
        )

    def _log(self, request: SearchRequest, result: SearchResult, started: float) -> None:
        logger.info(
            "retrieval.search_completed",
            extra={
                "course_id": str(request.course_id),
                "k": request.k,
                "resource_types": sorted(t.value for t in request.resource_types or ()),
                "hits": len(result.hits),
                "current_versions": result.current_versions,
                "searchable_versions": result.searchable_versions,
                "model_call_id": result.query_model_call_id and str(result.query_model_call_id),
                "latency_ms": round((time.perf_counter() - started) * 1000),
            },
        )


def _hit(rank: int, row: Any, version_spans: Sequence[Any]) -> SearchHit:
    """One ranked chunk with its evidence, or ProvenanceError."""
    first, last = row.first_span_ordinal, row.last_span_ordinal
    spans = [span for span in version_spans if first <= span.ordinal <= last]
    if [span.ordinal for span in spans] != list(range(first, last + 1)):
        raise ProvenanceError(row.id, "a span in its range is missing")
    evidence = []
    for span in spans:
        if span.start_offset is None or span.end_offset is None:
            raise ProvenanceError(row.id, f"span {span.ordinal} has no offsets")
        overlap_start = max(row.start_offset, span.start_offset)
        overlap_end = min(row.end_offset, span.end_offset)
        if overlap_end <= overlap_start:
            raise ProvenanceError(row.id, f"span {span.ordinal} does not overlap the chunk")
        relative_start = overlap_start - span.start_offset
        relative_end = overlap_end - span.start_offset
        chunk_slice = row.text[overlap_start - row.start_offset : overlap_end - row.start_offset]
        if span.excerpt[relative_start:relative_end] != chunk_slice:
            raise ProvenanceError(row.id, f"span {span.ordinal} text differs from the chunk")
        full = (overlap_start, overlap_end) == (span.start_offset, span.end_offset)
        evidence.append(
            SpanEvidence(
                span_id=span.id,
                ordinal=span.ordinal,
                block_kind=span.block_kind,
                page_number=span.page_number,
                slide_number=span.slide_number,
                section_path=span.section_path,
                span_start=span.start_offset,
                span_end=span.end_offset,
                overlap_start=overlap_start,
                overlap_end=overlap_end,
                relative_start=relative_start,
                relative_end=relative_end,
                coverage="full" if full else "partial",
                excerpt=span.excerpt,
            )
        )
    return SearchHit(
        rank=rank,
        score=1.0 - float(row.distance),
        chunk_id=row.id,
        chunk_ordinal=row.ordinal,
        start_offset=row.start_offset,
        end_offset=row.end_offset,
        text=row.text,
        resource=ResourceRef(row.resource_id, row.title, row.type, row.source_uri),
        version=VersionRef(row.version_id, row.version_number),
        spans=tuple(evidence),
    )
