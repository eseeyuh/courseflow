# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Course-scoped search over complete, verified index states only.

Two modes rank the same verified corpus (``SearchMode``):

- ``vector``: exact cosine similarity between the query/v1 embedding and the
  chunks' document/v1 embeddings in the active space;
- ``lexical``: PostgreSQL full-text match of the query's own normalised
  lexemes against ``chunks.text_tsv`` (``rankers.LEXICAL_VERSION``).

The corpus is the same in both: the course's CURRENT versions classified
SEARCHABLE for the active chunker and embedding space, with the same derived
index state indexing uses (re-chunked and compared, links checked):

- searchable: exact chunk set, complete and correct links;
- unindexed: no chunks, or exact chunks with no links in this space, or
  valid spans with no visible text (nothing to retrieve);
- corrupt: anything else, including spans the chunker cannot safely use. One
  corrupt current version fails the search (IndexStateError): a partial or
  inconsistent index is never searched.

Lexical search could technically match any chunk with a ``text_tsv``; it is
deliberately limited to that verified corpus so it is never a way around the
index-state checks. It needs only the space's identity: no provider, no
credentials, no network, no ModelCall.

Vector search:
1. First verification, in its own read-only snapshot, before any paid call.
   Nothing searchable: an empty result and no provider call.
2. Embed the question as query/v1: one standalone ModelCall. Query vectors
   are never stored or cached: every search pays. No transaction is open.
3. One read-only REPEATABLE READ snapshot holds the final classification,
   the ranking (only the versions classified searchable in that snapshot)
   and the evidence for the hits.

Lexical search: step 3 only, with the lexical ranker.

Evidence: each hit's SourceSpans with the exact overlap, read in the ranking
snapshot. Any provenance inconsistency fails the whole search
(ProvenanceError). Chunk text is retrieval context; a SourceSpan plus its
sub-range is evidence.

``score`` is retriever-specific (``SearchResult.score_kind``): ``1 - cosine
distance`` in vector mode, ``ts_rank_cd`` in lexical mode. It is a ranking
score: never a confidence or probability, and not comparable across modes.
Logs carry IDs and counts only: never query text, lexemes, chunk text,
excerpts or URIs.
"""

from __future__ import annotations

import enum
import logging
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai.embeddings import (
    DOCUMENT_INPUT_FORMAT,
    QUERY_V1_MAX_CHARS,
    EmbeddingProvider,
    EmbeddingSpace,
    query_input,
)
from app.ai.errors import AIConfigurationError
from app.ai.model_calls import RecordedCallError, VectorStorageError, call_embeddings_recorded
from app.db.models import Chunk, Resource, ResourceVersion, SourceSpan
from app.db.session import read_only_snapshot
from app.domain.enums import BlockKind, ModelCallErrorCategory, ResourceType
from app.retrieval import store
from app.retrieval.chunking import SPAN_PACK_V1, ChunkingParams
from app.retrieval.indexing import (
    SourceContent,
    SpaceSummary,
    VersionIndexReport,
    inspect_course_in_session,
)
from app.retrieval.rankers import (
    LEXICAL_VERSION,
    RankedCandidate,
    SearchScope,
    rank_lexical,
    rank_vector,
)
from app.retrieval.store import ChunkState, LinkState

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


class SearchMode(enum.StrEnum):
    """Which ranker orders the verified corpus."""

    VECTOR = "vector"
    LEXICAL = "lexical"


# What ``SearchHit.score`` means in each mode: retriever-specific, never a
# confidence, never comparable across modes.
SCORE_KINDS: dict[SearchMode, str] = {
    SearchMode.VECTOR: "cosine_similarity",  # 1 - cosine distance
    SearchMode.LEXICAL: "ts_rank_cd",
}


class SearchRequest(BaseModel):
    # hide_input_in_errors: a validation error must never echo the query.
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    course_id: uuid.UUID
    query: str
    k: int = Field(default=DEFAULT_K, ge=1, le=MAX_K)
    resource_types: frozenset[ResourceType] | None = None
    mode: SearchMode = SearchMode.VECTOR

    @field_validator("query")
    @classmethod
    def _valid_query(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("query is empty")
        if len(value) > QUERY_V1_MAX_CHARS:
            raise ValueError(f"query is longer than {QUERY_V1_MAX_CHARS} characters")
        if chr(0) in value:  # PostgreSQL text cannot hold NUL: lexical search would fail
            raise ValueError("query contains a NUL character")
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
    # Retriever-specific ranking score (see SearchResult.score_kind): never a
    # confidence or probability, not comparable across modes.
    score: float
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
    mode: SearchMode
    score_kind: str  # what SearchHit.score means in this mode
    chunker_version: str
    # The active space: it defines the searchable corpus in every mode.
    space: SpaceSummary
    query_format: str | None  # None in lexical mode: the query is not embedded
    document_format: str
    lexical_version: str | None  # set in lexical mode only
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


async def classify_course_in_session(
    session: AsyncSession,
    course_id: uuid.UUID,
    space: EmbeddingSpace,
    *,
    params: ChunkingParams = SPAN_PACK_V1,
) -> list[tuple[uuid.UUID, VersionSearchState]]:
    """Each current version's search state, read in the caller's session and
    snapshot (``inspect_course_in_session`` + ``classify``). No commit, no
    other session, no provider call."""
    reports = await inspect_course_in_session(session, course_id, space, params=params)
    return [(report.resource_version_id, classify(report)) for report in reports]


@dataclass(frozen=True, slots=True)
class _Corpus:
    """The verified search corpus in one snapshot: current versions and the
    subset that is searchable."""

    current: int
    searchable: tuple[uuid.UUID, ...]


# --- the service ----------------------------------------------------------------------------


class SearchService:
    """Searches one course. ``space`` is the active embedding space: it
    defines the searchable corpus in every mode. ``provider`` is needed only
    by vector search and must embed in that same space."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        space: EmbeddingSpace,
        provider: EmbeddingProvider | None = None,
        *,
        params: ChunkingParams = SPAN_PACK_V1,
    ) -> None:
        if provider is not None and provider.space != space:
            raise ValueError("the provider does not embed in the search's embedding space")
        self._sessions = session_factory
        self._space = space
        self._provider = provider
        self._params = params

    async def search_course(self, request: SearchRequest) -> SearchResult:
        started = time.perf_counter()
        if request.mode is SearchMode.LEXICAL:
            result = await self._search_lexical(request)
        else:
            result = await self._search_vector(request)
        self._log(request, result, started)
        return result

    async def _search_lexical(self, request: SearchRequest) -> SearchResult:
        """No paid call to protect, so one snapshot does everything."""
        async with read_only_snapshot(self._sessions) as session:
            corpus = await self._verified_corpus(session, request.course_id, None)
            candidates = await rank_lexical(
                session, self._scope(request, corpus), request.query, request.k
            )
            hits = await _assemble(session, candidates, _lexical_score)
        return self._result(request, corpus, hits, query_format=None, query_model_call_id=None)

    async def _search_vector(self, request: SearchRequest) -> SearchResult:
        """The first verification avoids paying for a search that cannot run
        (nothing searchable, or corrupt state). No transaction is held across
        the provider call. The final classification, the ranking and the
        evidence then share one snapshot: only versions verified there rank."""
        if self._provider is None:
            raise AIConfigurationError("vector search needs an embedding provider")
        query = query_input(request.query)  # the exact query/v1 provider input

        async with read_only_snapshot(self._sessions) as session:
            corpus = await self._verified_corpus(session, request.course_id, None)
        if not corpus.searchable:
            return self._result(
                request, corpus, (), query_format=query.input_format, query_model_call_id=None
            )

        try:
            recorded = await call_embeddings_recorded(self._provider, self._sessions, [query])
        except RecordedCallError as failed:
            raise SearchProviderError(failed.error.category, failed.model_call_id) from failed
        except VectorStorageError as failed:
            raise QueryCallNotRecordedError(failed.model_call_id) from failed
        vector = recorded.result.vectors[0]
        call_id = recorded.model_call_id

        async with read_only_snapshot(self._sessions) as session:
            corpus = await self._verified_corpus(session, request.course_id, call_id)
            hits: tuple[SearchHit, ...] = ()
            if corpus.searchable:
                space_ref = await store.find_space(session, self._space)
                if space_ref is None:  # pragma: no cover - searchable implies the space row
                    raise RuntimeError("searchable versions without an embedding space row")
                if len(vector) != space_ref.dimensions:  # pragma: no cover - recorder rejects it
                    raise RuntimeError("query vector does not match the space's dimensions")
                candidates = await rank_vector(
                    session, self._scope(request, corpus), space_ref, vector, request.k
                )
                hits = await _assemble(session, candidates, _cosine_similarity)
        return self._result(
            request, corpus, hits, query_format=query.input_format, query_model_call_id=call_id
        )

    async def _verified_corpus(
        self,
        session: AsyncSession,
        course_id: uuid.UUID,
        query_model_call_id: uuid.UUID | None,
    ) -> _Corpus:
        """Classify every current version in ``session``'s snapshot; fail
        closed on any corrupt one."""
        states = await classify_course_in_session(
            session, course_id, self._space, params=self._params
        )
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

    def _scope(self, request: SearchRequest, corpus: _Corpus) -> SearchScope:
        return SearchScope(
            course_id=request.course_id,
            version_ids=corpus.searchable,
            chunker_version=self._params.version,
            resource_types=request.resource_types,
        )

    def _result(
        self,
        request: SearchRequest,
        corpus: _Corpus,
        hits: tuple[SearchHit, ...],
        *,
        query_format: str | None,
        query_model_call_id: uuid.UUID | None,
    ) -> SearchResult:
        space = self._space
        return SearchResult(
            course_id=request.course_id,
            mode=request.mode,
            score_kind=SCORE_KINDS[request.mode],
            chunker_version=self._params.version,
            space=SpaceSummary(
                provider=space.provider,
                model=space.model,
                dimensions=space.dimensions,
                space_version=space.space_version,
            ),
            query_format=query_format,
            document_format=DOCUMENT_INPUT_FORMAT,
            lexical_version=LEXICAL_VERSION if request.mode is SearchMode.LEXICAL else None,
            query_model_call_id=query_model_call_id,
            current_versions=corpus.current,
            searchable_versions=len(corpus.searchable),
            unindexed_versions=corpus.current - len(corpus.searchable),
            hits=hits,
        )

    def _log(self, request: SearchRequest, result: SearchResult, started: float) -> None:
        logger.info(
            "retrieval.search_completed",
            extra={
                "course_id": str(request.course_id),
                "mode": request.mode.value,
                "k": request.k,
                "resource_types": sorted(t.value for t in request.resource_types or ()),
                "hits": len(result.hits),
                "current_versions": result.current_versions,
                "searchable_versions": result.searchable_versions,
                "model_call_id": result.query_model_call_id and str(result.query_model_call_id),
                "latency_ms": round((time.perf_counter() - started) * 1000),
            },
        )


# --- evidence -------------------------------------------------------------------------------


def _cosine_similarity(candidate: RankedCandidate) -> float:
    assert candidate.vector_distance is not None
    return 1.0 - candidate.vector_distance


def _lexical_score(candidate: RankedCandidate) -> float:
    assert candidate.lexical_score is not None
    return candidate.lexical_score


async def _assemble(
    session: AsyncSession,
    candidates: Sequence[RankedCandidate],
    score_of: Callable[[RankedCandidate], float],
) -> tuple[SearchHit, ...]:
    """Hits with their SourceSpan evidence, read in the ranking snapshot."""
    if not candidates:
        return ()
    rows = {
        row.id: row
        for row in (
            await session.execute(
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
                )
                .join(ResourceVersion, ResourceVersion.id == Chunk.resource_version_id)
                .join(Resource, Resource.id == ResourceVersion.resource_id)
                .where(Chunk.id.in_([c.chunk_id for c in candidates]))
            )
        ).all()
    }
    for candidate in candidates:
        if candidate.chunk_id not in rows:  # pragma: no cover - same snapshot as the ranking
            raise ProvenanceError(candidate.chunk_id, "the ranked chunk is missing")
    wanted = or_(
        *(
            and_(
                SourceSpan.resource_version_id == row.version_id,
                SourceSpan.ordinal.between(row.first_span_ordinal, row.last_span_ordinal),
            )
            for row in rows.values()
        )
    )
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
    hits = []
    for candidate in candidates:
        row = rows[candidate.chunk_id]
        version_spans = by_version.get(row.version_id, [])
        hits.append(_hit(candidate.rank, row, score_of(candidate), version_spans))
    return tuple(hits)


def _hit(rank: int, row: Any, score: float, version_spans: Sequence[Any]) -> SearchHit:
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
        score=score,
        chunk_id=row.id,
        chunk_ordinal=row.ordinal,
        start_offset=row.start_offset,
        end_offset=row.end_offset,
        text=row.text,
        resource=ResourceRef(row.resource_id, row.title, row.type, row.source_uri),
        version=VersionRef(row.version_id, row.version_number),
        spans=tuple(evidence),
    )
