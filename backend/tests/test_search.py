# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""SearchService against PostgreSQL with real commits: verified-index preflight,
course/current/space scoping, ranking, span evidence and query telemetry."""

import dataclasses
import json
import logging
import math
import uuid
from collections.abc import Sequence
from typing import Any

import pytest
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai.embeddings import EmbeddingInput, EmbeddingResult
from app.ai.errors import AIProviderError
from app.ai.provider import TokenUsage
from app.db.models import Chunk, ChunkEmbedding, Embedding, ModelCall, SourceSpan
from app.domain.enums import (
    BlockKind,
    ModelCallErrorCategory,
    ModelCallKind,
    ModelCallStatus,
    ResourceType,
)
from app.retrieval import store
from app.retrieval.chunking import ChunkingParams
from app.retrieval.indexing import (
    CourseNotFoundError,
    IndexingService,
    SourceContent,
    VersionIndexReport,
    inspect_course,
)
from app.retrieval.search import (
    IndexStateError,
    InvalidSearchRequest,
    ProvenanceError,
    SearchProviderError,
    SearchRequest,
    SearchResult,
    SearchService,
    VersionSearchState,
    classify,
)
from tests.chunking_cases import sentences
from tests.embedding_fakes import FakeEmbeddingProvider
from tests.retrieval_seed import paragraphs, seed_course, seed_version

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
P = BlockKind.PAGE
DIMS = 256  # fewer hash collisions for the bag-of-words fake
MARKER = "zebra quokka narwhal"
MARKED = f"The {MARKER} rubric is assessed in week nine."
NBSP = "\u00a0"


@pytest.fixture
def session_factory(committed_session: AsyncSession) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(committed_session.bind, expire_on_commit=False)


def _provider(**kwargs: Any) -> FakeEmbeddingProvider:
    return FakeEmbeddingProvider(dimensions=kwargs.pop("dimensions", DIMS), **kwargs)


async def _index(
    sf: async_sessionmaker[AsyncSession], course: uuid.UUID, provider: FakeEmbeddingProvider
) -> None:
    service = IndexingService(
        sf, provider, batch_max_items=8, batch_max_estimated_tokens=400, params=SMALL
    )
    result = await service.index_course(course)
    assert result.summary.versions_failed == 0, result.summary.failures


async def _search(
    sf: async_sessionmaker[AsyncSession],
    provider: FakeEmbeddingProvider,
    course: uuid.UUID,
    query: str = MARKER,
    **kwargs: Any,
) -> SearchResult:
    request = SearchRequest.build(course_id=course, query=query, **kwargs)
    return await SearchService(sf, provider.space, provider, params=SMALL).search_course(request)


async def _model_calls(sf: async_sessionmaker[AsyncSession]) -> list[ModelCall]:
    async with sf() as session:
        return list((await session.scalars(select(ModelCall).order_by(ModelCall.started_at))).all())


async def _query_calls(sf: async_sessionmaker[AsyncSession]) -> list[ModelCall]:
    return [c for c in await _model_calls(sf) if c.prompt_version == "query/v1"]


def _blocks(*texts: str) -> list[tuple[BlockKind, str, None]]:
    return [(P, t, None) for t in texts]


async def _marked_course(
    sf: async_sessionmaker[AsyncSession], title: str = "Course A"
) -> tuple[uuid.UUID, uuid.UUID]:
    course = await seed_course(sf, title)
    version = await seed_version(
        sf, course, [*paragraphs(0, 3), (P, MARKED, None), *paragraphs(10, 2)]
    )
    return course, version


# --- ranking, scoping and evidence -------------------------------------------------------------


async def test_a_known_marker_query_ranks_its_chunk_first_with_span_evidence(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    course, version = await _marked_course(session_factory)
    provider = _provider()
    await _index(session_factory, course, provider)

    result = await _search(session_factory, provider, course)

    top = result.hits[0]
    assert MARKER in top.text
    assert top.rank == 1 and top.version.id == version
    assert result.current_versions == result.searchable_versions == 1
    assert result.query_format == "query/v1" and result.document_format == "document/v1"
    for hit in result.hits:
        assert hit.spans  # at least one SourceSpan, always
        assert [s.ordinal for s in hit.spans] == sorted(s.ordinal for s in hit.spans)
        for span in hit.spans:
            assert (
                span.excerpt[span.relative_start : span.relative_end]
                == hit.text[
                    span.overlap_start - hit.start_offset : span.overlap_end - hit.start_offset
                ]
            )
    scores = [hit.score for hit in result.hits]
    assert scores == sorted(scores, reverse=True)
    assert all(-1.0 <= s <= 1.0 + 1e-9 for s in scores)


async def test_another_course_is_never_returned_even_when_it_matches_better(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    course_a, _ = await _marked_course(session_factory, "Course A")
    course_b = await seed_course(session_factory, "Course B")
    await seed_version(session_factory, course_b, _blocks(MARKER))  # the exact query text
    provider = _provider()
    await _index(session_factory, course_a, provider)
    await _index(session_factory, course_b, provider)

    result = await _search(session_factory, provider, course_a, k=50)

    assert result.hits
    assert all(hit.text != MARKER for hit in result.hits)
    async with session_factory() as session:
        b_chunks = set(await session.scalars(select(Chunk.id).where(Chunk.text == MARKER)))
    assert b_chunks and not b_chunks & {hit.chunk_id for hit in result.hits}


async def test_a_stale_version_never_returns_after_a_new_version_is_current(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    course, v1 = await _marked_course(session_factory)
    provider = _provider()
    await _index(session_factory, course, provider)
    v2 = await seed_version(session_factory, course, paragraphs(0, 4))  # marker removed
    await _index(session_factory, course, provider)

    result = await _search(session_factory, provider, course, k=50)

    assert result.hits
    assert {hit.version.id for hit in result.hits} == {v2}
    assert all(MARKER not in hit.text for hit in result.hits)
    assert v1 not in {hit.version.id for hit in result.hits}


async def test_resource_type_filters_restrict_results(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    course = await seed_course(session_factory)
    await seed_version(
        session_factory,
        course,
        _blocks(MARKED),
        source_uri="upload:l.pdf",
        resource_type=ResourceType.LECTURE,
        title="Lecture",
    )
    await seed_version(
        session_factory,
        course,
        _blocks(MARKED),
        source_uri="upload:b.pdf",
        resource_type=ResourceType.BRIEF,
        title="Brief",
    )
    await seed_version(
        session_factory, course, _blocks(MARKED), source_uri="upload:r.pdf",
        resource_type=ResourceType.READING, title="Reading",
    )  # fmt: skip
    provider = _provider()
    await _index(session_factory, course, provider)

    briefs = await _search(session_factory, provider, course, resource_types={ResourceType.BRIEF})
    two = await _search(
        session_factory,
        provider,
        course,
        resource_types={ResourceType.BRIEF, ResourceType.LECTURE},
    )
    everything = await _search(session_factory, provider, course)

    assert {h.resource.type for h in briefs.hits} == {ResourceType.BRIEF}
    assert {h.resource.type for h in two.hits} == {ResourceType.BRIEF, ResourceType.LECTURE}
    assert len(everything.hits) == 3


async def test_partial_span_evidence_reports_exact_sub_ranges(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    course = await seed_course(session_factory)
    oversized = sentences(0, 12) + f" {MARKED} " + sentences(12, 12)  # split inside itself
    await seed_version(session_factory, course, _blocks(oversized))
    provider = _provider()
    await _index(session_factory, course, provider)

    result = await _search(session_factory, provider, course, k=50)

    partial = [s for hit in result.hits for s in hit.spans if s.coverage == "partial"]
    assert partial
    for hit in result.hits:
        (span,) = hit.spans  # every chunk lies inside the one oversized span
        assert span.relative_start == hit.start_offset - span.span_start
        assert span.relative_end == hit.end_offset - span.span_start
        assert span.excerpt[span.relative_start : span.relative_end] == hit.text


async def test_ties_break_deterministically_by_natural_identity(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    course = await seed_course(session_factory)
    for name in ("c", "a", "b"):  # seeded out of order: row IDs never decide
        await seed_version(
            session_factory, course, _blocks(MARKED), source_uri=f"upload:{name}.pdf"
        )
    provider = _provider()
    await _index(session_factory, course, provider)

    first = await _search(session_factory, provider, course)
    second = await _search(session_factory, provider, course)

    keys = [(h.resource.source_uri, h.version.version_number, h.chunk_ordinal) for h in first.hits]
    assert len({h.score for h in first.hits}) == 1  # identical text: a three-way tie
    assert keys == [("upload:a.pdf", 1, 0), ("upload:b.pdf", 1, 0), ("upload:c.pdf", 1, 0)]
    assert [h.chunk_id for h in first.hits] == [h.chunk_id for h in second.hits]


async def test_switching_space_searches_only_the_new_space(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    course, _ = await _marked_course(session_factory)
    old, new = _provider(), _provider(space_version="v2")
    await _index(session_factory, course, old)

    before = await _search(session_factory, new, course)  # nothing in the new space yet

    assert before.hits == () and before.searchable_versions == 0
    assert new.calls == [] and await _query_calls(session_factory) == []

    await _index(session_factory, course, new)
    after = await _search(session_factory, new, course, k=50)

    async with session_factory() as session:
        chunks = await session.scalar(select(func.count()).select_from(Chunk))
    assert len(after.hits) == chunks  # each chunk once, though it has two links now
    assert after.space.space_version == "v2"


async def test_spaces_of_different_dimensions_never_meet_in_the_distance(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    course, _ = await _marked_course(session_factory)
    wide, narrow = _provider(), _provider(dimensions=32, space_version="v-32")
    await _index(session_factory, course, wide)
    await _index(session_factory, course, narrow)

    for provider, dims in ((wide, DIMS), (narrow, 32)):
        result = await _search(session_factory, provider, course, k=50)
        assert result.hits and result.space.dimensions == dims


async def test_no_approximate_index_exists_on_vectors(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        ann = await session.scalar(
            text(
                "SELECT count(*) FROM pg_index i "
                "JOIN pg_class c ON c.oid = i.indexrelid "
                "JOIN pg_am a ON a.oid = c.relam WHERE a.amname IN ('hnsw', 'ivfflat')"
            )
        )
    assert ann == 0


# --- the integration proof ----------------------------------------------------------------------


async def test_only_the_course_current_version_active_space_and_dimension_are_returned(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Course A current v2 + a stale indexed A v1 + course B holding the exact
    query + course A also indexed in a 32-dimension space: a 256-dimension
    search returns only course A, v2, active-space vectors."""
    course_a, v1 = await _marked_course(session_factory, "Course A")
    active, other = _provider(), _provider(dimensions=32, space_version="v-32")
    await _index(session_factory, course_a, active)
    v2 = await seed_version(
        session_factory, course_a, [*paragraphs(0, 2), (P, MARKED + " Updated.", None)]
    )
    course_b = await seed_course(session_factory, "Course B")
    await seed_version(session_factory, course_b, _blocks(MARKER))
    for course in (course_a, course_b):
        await _index(session_factory, course, active)
        await _index(session_factory, course, other)

    result = await _search(session_factory, active, course_a, k=50)

    assert result.hits and result.space.dimensions == DIMS
    assert {hit.version.id for hit in result.hits} == {v2}  # never v1, never course B
    async with session_factory() as session:
        rows = (
            await session.execute(
                select(Chunk.resource_version_id, Embedding.dimensions, Embedding.space_id)
                .join(ChunkEmbedding, ChunkEmbedding.chunk_id == Chunk.id)
                .join(Embedding, Embedding.id == ChunkEmbedding.embedding_id)
                .where(
                    Chunk.id.in_([h.chunk_id for h in result.hits]),
                    Embedding.dimensions == DIMS,
                )
            )
        ).all()
    assert {(r.resource_version_id, r.dimensions) for r in rows} == {(v2, DIMS)}
    assert len(rows) == len(result.hits)  # exactly one active-space vector per hit
    assert v1 != v2


# --- verified-index preflight ----------------------------------------------------------------------


async def test_a_partial_link_set_fails_closed_before_any_provider_call(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    course, version = await _marked_course(session_factory)
    provider = _provider()
    await _index(session_factory, course, provider)
    calls = len(provider.calls)
    async with session_factory() as session, session.begin():
        first = await session.scalar(
            select(Chunk.id).where(Chunk.resource_version_id == version, Chunk.ordinal == 0)
        )
        await session.execute(delete(ChunkEmbedding).where(ChunkEmbedding.chunk_id == first))

    with pytest.raises(IndexStateError) as caught:
        await _search(session_factory, provider, course)

    assert caught.value.corrupt_version_ids == (version,)
    assert len(provider.calls) == calls and await _query_calls(session_factory) == []


async def test_a_chunk_state_mismatch_fails_closed_before_any_provider_call(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    course, version = await _marked_course(session_factory)
    provider = _provider()
    await _index(session_factory, course, provider)
    calls = len(provider.calls)
    async with session_factory() as session, session.begin():
        last = await session.scalar(
            select(func.max(Chunk.ordinal)).where(Chunk.resource_version_id == version)
        )
        await session.execute(
            delete(Chunk).where(Chunk.resource_version_id == version, Chunk.ordinal == last)
        )

    with pytest.raises(IndexStateError):
        await _search(session_factory, provider, course)

    assert len(provider.calls) == calls and await _query_calls(session_factory) == []


async def test_searchable_plus_unindexed_searches_only_the_verified_version(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    course, indexed = await _marked_course(session_factory)
    provider = _provider()
    await _index(session_factory, course, provider)
    await seed_version(session_factory, course, _blocks(MARKED), source_uri="upload:new.pdf")

    result = await _search(session_factory, provider, course, k=50)

    assert (result.current_versions, result.searchable_versions) == (2, 1)
    assert result.unindexed_versions == 1
    assert {hit.version.id for hit in result.hits} == {indexed}  # exactly the verified subset


async def test_searchable_plus_corrupt_fails_closed(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    course, _ = await _marked_course(session_factory)
    other = await seed_version(
        session_factory, course, paragraphs(20, 3), source_uri="upload:other.pdf"
    )
    provider = _provider()
    await _index(session_factory, course, provider)
    async with session_factory() as session, session.begin():
        await session.execute(
            delete(ChunkEmbedding).where(
                ChunkEmbedding.chunk_id.in_(
                    select(Chunk.id).where(Chunk.resource_version_id == other, Chunk.ordinal == 0)
                )
            )
        )
    calls = len(provider.calls)

    with pytest.raises(IndexStateError) as caught:
        await _search(session_factory, provider, course)

    assert caught.value.corrupt_version_ids == (other,)
    assert caught.value.current_versions == 2
    assert len(provider.calls) == calls


@pytest.mark.parametrize("mode", ["vector", "hybrid"])  # the modes that pay for a query
@pytest.mark.parametrize("state", ["no-versions", "not-indexed"])
async def test_an_empty_index_makes_no_provider_call_and_no_model_call(
    session_factory: async_sessionmaker[AsyncSession], state: str, mode: str
) -> None:
    course = await seed_course(session_factory)
    if state == "not-indexed":
        await seed_version(session_factory, course, _blocks(MARKED))
    provider = _provider()
    async with session_factory() as session:  # the active space was never indexed
        assert await store.find_space(session, provider.space) is None

    result = await _search(session_factory, provider, course, mode=mode)

    assert result.hits == () and result.query_model_call_id is None
    assert result.searchable_versions == 0
    assert result.current_versions == (1 if state == "not-indexed" else 0)
    assert provider.calls == [] and await _model_calls(session_factory) == []


@pytest.mark.parametrize("mode", ["vector", "hybrid"])
@pytest.mark.parametrize("state", ["no-versions", "not-indexed", "superseded-by-unindexed"])
async def test_an_existing_space_without_searchable_course_content_makes_no_query_call(
    session_factory: async_sessionmaker[AsyncSession], state: str, mode: str
) -> None:
    """The active space row exists (another course is indexed in it), but this
    course has no searchable current version there: the first verification
    must stop before the paid query call."""
    other, _ = await _marked_course(session_factory, "Other course")
    provider = _provider()
    await _index(session_factory, other, provider)
    course = await seed_course(session_factory, "Course A")
    if state == "not-indexed":
        await seed_version(session_factory, course, _blocks(MARKED))
    if state == "superseded-by-unindexed":
        await seed_version(session_factory, course, _blocks(MARKED))
        await _index(session_factory, course, provider)
        await seed_version(session_factory, course, _blocks("a newer, unindexed version"))
    async with session_factory() as session:
        assert await store.find_space(session, provider.space) is not None
    calls = len(provider.calls)

    result = await _search(session_factory, provider, course, mode=mode)

    assert result.hits == () and result.query_model_call_id is None
    assert result.searchable_versions == 0
    assert result.current_versions == (0 if state == "no-versions" else 1)
    assert len(provider.calls) == calls and await _query_calls(session_factory) == []


async def test_an_unknown_course_fails_before_any_provider_call(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    provider = _provider()

    with pytest.raises(CourseNotFoundError):
        await _search(session_factory, provider, uuid.uuid4())

    assert provider.calls == []


# --- the query ModelCall --------------------------------------------------------------------------


async def test_each_search_records_one_standalone_query_call_without_query_text(
    session_factory: async_sessionmaker[AsyncSession], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    course, _ = await _marked_course(session_factory)
    provider = _provider()
    await _index(session_factory, course, provider)
    caplog.clear()

    result = await _search(session_factory, provider, course)

    (call,) = await _query_calls(session_factory)
    assert result.query_model_call_id == call.id
    assert call.call_kind is ModelCallKind.EMBEDDING and call.model_role is None
    assert call.workflow_run_id is None and call.repair_rounds == 0
    assert call.status is ModelCallStatus.SUCCEEDED
    assert call.output_schema == f"vector[{DIMS}]"
    assert call.attempts == 1 and call.provider_request_id is not None
    row = json.dumps({c.key: str(getattr(call, c.key)) for c in ModelCall.__table__.columns})
    assert "zebra" not in row and "Instruct" not in row
    logs = caplog.text
    assert "zebra" not in logs and "week nine" not in logs and "upload:" not in logs
    assert "retrieval.search_completed" in [r.getMessage() for r in caplog.records]


async def test_repeated_identical_searches_pay_every_time_because_queries_are_not_cached(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    course, _ = await _marked_course(session_factory)
    provider = _provider()
    await _index(session_factory, course, provider)
    calls = len(provider.calls)

    first = await _search(session_factory, provider, course)
    second = await _search(session_factory, provider, course)

    assert len(provider.calls) == calls + 2
    assert len(await _query_calls(session_factory)) == 2
    assert first.query_model_call_id != second.query_model_call_id
    async with session_factory() as session:
        stored = await session.scalar(
            select(func.count()).where(Embedding.input_format == "query/v1")
        )
    assert stored == 0  # query vectors are never persisted


def _failing(error: ModelCallErrorCategory) -> AIProviderError:
    return AIProviderError(error, "scripted", model="fake", attempts=3, usage=TokenUsage())


def _return_bad_vectors(provider: FakeEmbeddingProvider, kind: str) -> None:
    """Make this provider answer with all-zero or NaN vectors."""
    original = provider.embed

    async def embed(inputs: Sequence[EmbeddingInput]) -> EmbeddingResult:
        result = await original(inputs)
        value = 0.0 if kind == "zero" else math.nan
        return dataclasses.replace(result, vectors=((value,) * DIMS,))

    provider.embed = embed  # type: ignore[method-assign]


@pytest.mark.parametrize(
    ("setup", "category"),
    [
        ("timeout", ModelCallErrorCategory.TIMEOUT),
        ("rate_limited", ModelCallErrorCategory.RATE_LIMITED),
        ("authentication", ModelCallErrorCategory.AUTHENTICATION),
        ("truncated", ModelCallErrorCategory.INVALID_OUTPUT),
        ("zero", ModelCallErrorCategory.INVALID_OUTPUT),
        ("nan", ModelCallErrorCategory.INVALID_OUTPUT),
    ],
)
async def test_query_provider_failures_are_typed_and_recorded(
    session_factory: async_sessionmaker[AsyncSession],
    setup: str,
    category: ModelCallErrorCategory,
) -> None:
    course, _ = await _marked_course(session_factory)
    provider = _provider()
    await _index(session_factory, course, provider)
    if setup in {"zero", "nan"}:
        _return_bad_vectors(provider, setup)
    elif setup == "truncated":
        provider.truncate_results = True
    else:
        provider.failures = [_failing(category)]

    with pytest.raises(SearchProviderError) as caught:
        await _search(session_factory, provider, course)

    (call,) = await _query_calls(session_factory)
    assert caught.value.category is category
    assert caught.value.model_call_id == call.id
    assert call.status is ModelCallStatus.FAILED and call.error_category is category


async def test_corrupted_provenance_after_the_query_call_fails_the_search_and_keeps_the_call(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    course, version = await _marked_course(session_factory)
    provider = _provider()
    await _index(session_factory, course, provider)

    async def corrupt_spans(_inputs: object) -> None:  # after the preflight, inside the call
        async with session_factory() as session, session.begin():
            await session.execute(
                update(SourceSpan)
                .where(SourceSpan.resource_version_id == version)
                .values(excerpt=func.concat("#", func.substr(SourceSpan.excerpt, 2)))
            )

    provider.before_embed = corrupt_spans

    with pytest.raises(ProvenanceError):
        await _search(session_factory, provider, course)

    (call,) = await _query_calls(session_factory)
    assert call.status is ModelCallStatus.SUCCEEDED  # the paid query really succeeded


@pytest.mark.parametrize(
    ("values", "fields"),
    [
        ({"query": "   "}, ["query"]),
        ({"query": "x" * 2001}, ["query"]),
        ({"query": "ok", "k": 0}, ["k"]),
        ({"query": "ok", "k": 51}, ["k"]),
        ({"query": "ok", "resource_types": set()}, ["resource_types"]),
        ({"query": "ok", "resource_types": {"essay"}}, ["resource_types"]),
    ],
)
def test_invalid_requests_name_fields_and_never_echo_the_query(
    values: dict[str, Any], fields: list[str]
) -> None:
    with pytest.raises(InvalidSearchRequest) as caught:
        SearchRequest.build(course_id=uuid.uuid4(), **values)

    assert list(caught.value.fields) == fields
    assert "xxxx" not in str(caught.value)


def test_k_defaults_to_5_and_the_query_is_stripped() -> None:
    request = SearchRequest.build(course_id=uuid.uuid4(), query="  When is CW1 due?  ")

    assert request.k == 5
    assert request.query == "When is CW1 due?"


# --- valid-empty content versus invalid source state ---------------------------------------------


async def _index_allowing_failures(
    sf: async_sessionmaker[AsyncSession], course: uuid.UUID, provider: FakeEmbeddingProvider
) -> None:
    service = IndexingService(
        sf, provider, batch_max_items=8, batch_max_estimated_tokens=400, params=SMALL
    )
    await service.index_course(course)


async def test_a_version_with_no_visible_text_is_unindexed_and_siblings_stay_searchable(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    course, marked = await _marked_course(session_factory)
    await seed_version(session_factory, course, _blocks(NBSP, NBSP), source_uri="upload:blank.pdf")
    provider = _provider()
    await _index_allowing_failures(session_factory, course, provider)  # blank one fails

    reports = await inspect_course(session_factory, course, provider.space, params=SMALL)
    result = await _search(session_factory, provider, course, k=50)

    blank = [r for r in reports if r.resource_version_id != marked]
    assert [(r.content, classify(r)) for r in blank] == [
        (SourceContent.NO_VISIBLE_TEXT, VersionSearchState.UNINDEXED)
    ]
    assert (result.current_versions, result.searchable_versions) == (2, 1)
    assert {hit.version.id for hit in result.hits} == {marked}


async def test_invalid_span_state_is_corrupt_and_fails_the_whole_search_closed(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    course, _ = await _marked_course(session_factory)
    broken = await seed_version(
        session_factory, course, paragraphs(30, 3), source_uri="upload:broken.pdf"
    )
    provider = _provider()
    await _index(session_factory, course, provider)
    async with session_factory() as session, session.begin():
        # Shift span 1 back over span 0 (its excerpt length still matches its offsets).
        await session.execute(
            update(SourceSpan)
            .where(SourceSpan.resource_version_id == broken, SourceSpan.ordinal == 1)
            .values(start_offset=SourceSpan.start_offset - 3, end_offset=SourceSpan.end_offset - 3)
        )
    calls = len(provider.calls)

    reports = await inspect_course(session_factory, course, provider.space, params=SMALL)
    with pytest.raises(IndexStateError) as caught:
        await _search(session_factory, provider, course)

    (report,) = [r for r in reports if r.resource_version_id == broken]
    assert (report.content, classify(report)) == (
        SourceContent.INVALID,
        VersionSearchState.CORRUPT,
    )
    assert caught.value.corrupt_version_ids == (broken,)
    assert caught.value.query_model_call_id is None  # refused before any paid call
    assert len(provider.calls) == calls


def test_classification_never_downgrades_invalid_source_to_unindexed() -> None:
    version = uuid.uuid4()

    def report(content: SourceContent, chunks: int) -> VersionIndexReport:
        return VersionIndexReport(version, content, None, None, chunks)

    assert classify(report(SourceContent.NO_VISIBLE_TEXT, 0)) is VersionSearchState.UNINDEXED
    assert classify(report(SourceContent.NO_VISIBLE_TEXT, 2)) is VersionSearchState.CORRUPT
    assert classify(report(SourceContent.INVALID, 0)) is VersionSearchState.CORRUPT
    assert classify(report(SourceContent.INVALID, 3)) is VersionSearchState.CORRUPT


# --- the second verification, after the query is embedded ------------------------------------------


async def test_corruption_appearing_during_the_query_call_fails_closed_and_keeps_the_call(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    course, version = await _marked_course(session_factory)
    provider = _provider()
    await _index(session_factory, course, provider)

    async def remove_one_link(_inputs: object) -> None:
        async with session_factory() as session, session.begin():
            first = await session.scalar(
                select(Chunk.id).where(Chunk.resource_version_id == version, Chunk.ordinal == 0)
            )
            await session.execute(delete(ChunkEmbedding).where(ChunkEmbedding.chunk_id == first))

    provider.before_embed = remove_one_link
    calls = len(provider.calls)

    with pytest.raises(IndexStateError) as caught:
        await _search(session_factory, provider, course)

    (call,) = await _query_calls(session_factory)
    assert call.status is ModelCallStatus.SUCCEEDED  # unchanged by the later refusal
    assert caught.value.query_model_call_id == call.id
    assert caught.value.corrupt_version_ids == (version,)
    assert len(provider.calls) == calls + 1  # one query request, never a second


async def test_a_version_superseded_during_the_query_call_is_never_ranked(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    course, v1 = await _marked_course(session_factory)
    provider = _provider()
    await _index(session_factory, course, provider)

    async def ingest_unindexed_v2(_inputs: object) -> None:
        await seed_version(session_factory, course, _blocks(MARKED))  # same resource: v2

    provider.before_embed = ingest_unindexed_v2

    result = await _search(session_factory, provider, course, k=50)

    (call,) = await _query_calls(session_factory)
    assert result.hits == ()  # v1 is stale; v2 is current but not indexed
    assert (result.current_versions, result.searchable_versions) == (1, 0)
    assert result.query_model_call_id == call.id  # the paid request did happen
    assert call.status is ModelCallStatus.SUCCEEDED
    assert v1 not in {hit.version.id for hit in result.hits}


async def test_a_version_indexed_during_the_query_call_may_join_only_once_verified(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    course, first = await _marked_course(session_factory)
    provider = _provider()
    await _index(session_factory, course, provider)
    second = await seed_version(
        session_factory, course, _blocks(MARKED), source_uri="upload:second.pdf"
    )
    indexer = _provider()  # a separate client in the same space

    async def index_meanwhile(_inputs: object) -> None:
        await _index(session_factory, course, indexer)
        # ... and a third version arrives that nobody indexes:
        await seed_version(session_factory, course, _blocks(MARKED), source_uri="upload:third.pdf")

    provider.before_embed = index_meanwhile

    result = await _search(session_factory, provider, course, k=50)

    assert (result.current_versions, result.searchable_versions) == (3, 2)
    assert {hit.version.id for hit in result.hits} == {first, second}  # never the unverified one
