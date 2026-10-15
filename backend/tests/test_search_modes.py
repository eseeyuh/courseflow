# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Search modes against PostgreSQL with real commits: the same trust contract
for vector and lexical search, lexical query construction under adversarial
input, offline lexical search, the shared ranking snapshot, and the vector
ranker's equivalence with the original vector query."""

import logging
import uuid
from collections.abc import Sequence
from typing import Any

import openai
import pytest
from sqlalchemy import Text, and_, bindparam, cast, delete, func, select, text
from sqlalchemy.dialects.postgresql import ARRAY, TSQUERY
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai.embeddings import DOCUMENT_INPUT_FORMAT, query_input
from app.ai.errors import AIConfigurationError
from app.ai.nebius_embeddings import NebiusEmbeddingProvider
from app.db.models import (
    Chunk,
    ChunkEmbedding,
    Course,
    Embedding,
    ModelCall,
    Resource,
    ResourceVersion,
)
from app.db.session import read_only_snapshot
from app.domain.enums import BlockKind, ResourceType
from app.retrieval import search, store
from app.retrieval.chunking import ChunkingParams
from app.retrieval.indexing import IndexingService
from app.retrieval.rankers import (
    LEXICAL_VERSION,
    RankedCandidate,
    SearchScope,
    or_tsquery,
    query_lexemes,
    quote_lexeme,
    rank_vector,
)
from app.retrieval.search import (
    IndexStateError,
    InvalidSearchRequest,
    SearchMode,
    SearchRequest,
    SearchResult,
    SearchService,
    VersionSearchState,
)
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
DIMS = 256
MARKER = "zebra quokka narwhal"
MARKED = f"The {MARKER} rubric is assessed in week nine."
MODES = [SearchMode.VECTOR, SearchMode.LEXICAL]
BACKSLASH = chr(92)


@pytest.fixture
def session_factory(committed_session: AsyncSession) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(committed_session.bind, expire_on_commit=False)


def _provider() -> FakeEmbeddingProvider:
    return FakeEmbeddingProvider(dimensions=DIMS)


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
    mode: SearchMode,
    query: str = MARKER,
    **kwargs: Any,
) -> SearchResult:
    """Lexical search is given no provider at all: it must not need one."""
    request = SearchRequest.build(course_id=course, query=query, mode=mode, **kwargs)
    client = provider if mode is SearchMode.VECTOR else None
    return await SearchService(sf, provider.space, client, params=SMALL).search_course(request)


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


async def _count(sf: async_sessionmaker[AsyncSession], model: type[Any]) -> int:
    async with sf() as session:
        return int(await session.scalar(select(func.count()).select_from(model)) or 0)


def _assert_exact_provenance(result: SearchResult) -> None:
    for hit in result.hits:
        assert hit.spans
        for span in hit.spans:
            assert (
                span.excerpt[span.relative_start : span.relative_end]
                == hit.text[
                    span.overlap_start - hit.start_offset : span.overlap_end - hit.start_offset
                ]
            )


async def _drop_links(
    sf: async_sessionmaker[AsyncSession], version: uuid.UUID, **where: Any
) -> None:
    chunks = select(Chunk.id).where(Chunk.resource_version_id == version)
    if "ordinal" in where:
        chunks = chunks.where(Chunk.ordinal == where["ordinal"])
    async with sf() as session, session.begin():
        await session.execute(delete(ChunkEmbedding).where(ChunkEmbedding.chunk_id.in_(chunks)))


# --- one trust contract, both modes ---------------------------------------------------------


@pytest.mark.parametrize("mode", MODES)
async def test_another_course_never_leaks(
    session_factory: async_sessionmaker[AsyncSession], mode: SearchMode
) -> None:
    course_a, _ = await _marked_course(session_factory, "Course A")
    course_b = await seed_course(session_factory, "Course B")
    await seed_version(session_factory, course_b, _blocks(MARKER))  # matches best of all
    provider = _provider()
    await _index(session_factory, course_a, provider)
    await _index(session_factory, course_b, provider)

    result = await _search(session_factory, provider, course_a, mode, k=50)

    assert result.hits
    async with session_factory() as session:
        b_chunks = set(await session.scalars(select(Chunk.id).where(Chunk.text == MARKER)))
    assert b_chunks and not b_chunks & {hit.chunk_id for hit in result.hits}


@pytest.mark.parametrize("mode", MODES)
async def test_a_stale_version_never_appears(
    session_factory: async_sessionmaker[AsyncSession], mode: SearchMode
) -> None:
    course, v1 = await _marked_course(session_factory)
    provider = _provider()
    await _index(session_factory, course, provider)
    v2 = await seed_version(session_factory, course, paragraphs(0, 4))  # marker removed
    await _index(session_factory, course, provider)

    result = await _search(session_factory, provider, course, mode, f"{MARKER} sentence", k=50)

    assert result.hits
    assert {hit.version.id for hit in result.hits} == {v2}
    assert v1 not in {hit.version.id for hit in result.hits}


@pytest.mark.parametrize("mode", MODES)
async def test_resource_type_filters_restrict_results(
    session_factory: async_sessionmaker[AsyncSession], mode: SearchMode
) -> None:
    course = await seed_course(session_factory)
    await seed_version(
        session_factory, course, _blocks(MARKED), source_uri="upload:brief.pdf",
        resource_type=ResourceType.BRIEF,
    )  # fmt: skip
    await seed_version(
        session_factory, course, _blocks(MARKED), source_uri="upload:lecture.pdf",
        resource_type=ResourceType.LECTURE,
    )  # fmt: skip
    provider = _provider()
    await _index(session_factory, course, provider)

    briefs = await _search(
        session_factory, provider, course, mode, resource_types={ResourceType.BRIEF}
    )
    both = await _search(
        session_factory, provider, course, mode,
        resource_types={ResourceType.BRIEF, ResourceType.LECTURE},
    )  # fmt: skip

    assert briefs.hits and {h.resource.type for h in briefs.hits} == {ResourceType.BRIEF}
    assert {h.resource.type for h in both.hits} == {ResourceType.BRIEF, ResourceType.LECTURE}


@pytest.mark.parametrize("mode", MODES)
async def test_a_corrupt_current_version_fails_closed(
    session_factory: async_sessionmaker[AsyncSession], mode: SearchMode
) -> None:
    course, version = await _marked_course(session_factory)
    provider = _provider()
    await _index(session_factory, course, provider)
    async with session_factory() as session, session.begin():
        last = await session.scalar(
            select(func.max(Chunk.ordinal)).where(Chunk.resource_version_id == version)
        )
        await session.execute(
            delete(Chunk).where(Chunk.resource_version_id == version, Chunk.ordinal == last)
        )
    requests = len(provider.calls)

    with pytest.raises(IndexStateError) as caught:
        await _search(session_factory, provider, course, mode)

    assert caught.value.corrupt_version_ids == (version,)
    assert len(provider.calls) == requests  # no paid call in either mode


@pytest.mark.parametrize("mode", MODES)
async def test_a_partial_active_space_link_set_fails_closed(
    session_factory: async_sessionmaker[AsyncSession], mode: SearchMode
) -> None:
    course, version = await _marked_course(session_factory)
    provider = _provider()
    await _index(session_factory, course, provider)
    await _drop_links(session_factory, version, ordinal=0)

    with pytest.raises(IndexStateError) as caught:
        await _search(session_factory, provider, course, mode)

    assert caught.value.corrupt_version_ids == (version,)


@pytest.mark.parametrize("mode", MODES)
async def test_chunks_without_active_space_links_are_never_searched(
    session_factory: async_sessionmaker[AsyncSession], mode: SearchMode
) -> None:
    """Exact chunks with a text_tsv but no links in the active space are
    unindexed: lexical search does not search them merely because it could."""
    course, indexed = await _marked_course(session_factory)
    other = await seed_version(
        session_factory, course, _blocks(MARKED, MARKER), source_uri="upload:other.pdf"
    )
    provider = _provider()
    await _index(session_factory, course, provider)
    await _drop_links(session_factory, other)

    result = await _search(session_factory, provider, course, mode, k=50)

    assert (result.current_versions, result.searchable_versions) == (2, 1)
    assert result.unindexed_versions == 1
    assert result.hits and {hit.version.id for hit in result.hits} == {indexed}


@pytest.mark.parametrize("mode", MODES)
async def test_searchable_plus_never_indexed_searches_only_the_verified_version(
    session_factory: async_sessionmaker[AsyncSession], mode: SearchMode
) -> None:
    course, indexed = await _marked_course(session_factory)
    provider = _provider()
    await _index(session_factory, course, provider)
    await seed_version(session_factory, course, _blocks(MARKED), source_uri="upload:new.pdf")

    result = await _search(session_factory, provider, course, mode, k=50)

    assert (result.current_versions, result.searchable_versions) == (2, 1)
    assert {hit.version.id for hit in result.hits} == {indexed}


@pytest.mark.parametrize("mode", MODES)
async def test_hits_carry_exact_span_provenance_and_logs_carry_no_text(
    session_factory: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
    mode: SearchMode,
) -> None:
    course, version = await _marked_course(session_factory)
    provider = _provider()
    await _index(session_factory, course, provider)
    caplog.set_level(logging.DEBUG)

    result = await _search(session_factory, provider, course, mode, f"{MARKER} assessed", k=50)

    assert result.hits and MARKER in result.hits[0].text
    assert result.hits[0].version.id == version
    _assert_exact_provenance(result)
    logged = " ".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)
    for secret in ("zebra", "quokka", "assess", "rubric", "Sentence", "upload:notes.pdf"):
        assert secret not in logged
    completed = [r for r in caplog.records if r.getMessage() == "retrieval.search_completed"]
    assert [r.__dict__["mode"] for r in completed] == [mode.value]


# --- lexical search ---------------------------------------------------------------------------


async def test_lexical_search_ranks_by_ts_rank_cd_without_any_model_call(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    course, version = await _marked_course(session_factory)
    provider = _provider()
    await _index(session_factory, course, provider)
    calls, requests = await _count(session_factory, ModelCall), len(provider.calls)

    result = await _search(session_factory, provider, course, SearchMode.LEXICAL)

    assert result.mode is SearchMode.LEXICAL and result.score_kind == "ts_rank_cd"
    assert result.lexical_version == LEXICAL_VERSION == "pg-fts-english/v1"
    assert result.query_format is None and result.query_model_call_id is None
    assert result.document_format == DOCUMENT_INPUT_FORMAT
    assert [hit.rank for hit in result.hits] == list(range(1, len(result.hits) + 1))
    assert MARKER in result.hits[0].text and result.hits[0].version.id == version
    scores = [hit.score for hit in result.hits]
    assert scores == sorted(scores, reverse=True) and all(s > 0 for s in scores)
    assert await _count(session_factory, ModelCall) == calls
    assert len(provider.calls) == requests


async def test_lexical_search_stems_with_the_explicit_english_configuration(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    course, _ = await _marked_course(session_factory)
    provider = _provider()
    await _index(session_factory, course, provider)

    async with session_factory() as session:
        await session.execute(text("SET default_text_search_config = 'pg_catalog.simple'"))
        lexemes = await query_lexemes(session, "Assessing the Rubrics")
    result = await _search(session_factory, provider, course, SearchMode.LEXICAL, "assessing")

    assert lexemes == ["assess", "rubric"]  # english, whatever the session default
    assert result.hits and "assessed" in result.hits[0].text


async def test_only_matching_chunks_are_lexical_candidates(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    course, _ = await _marked_course(session_factory)
    provider = _provider()
    await _index(session_factory, course, provider)

    result = await _search(session_factory, provider, course, SearchMode.LEXICAL, "xylophone")

    assert result.hits == () and result.searchable_versions == 1


async def test_equal_lexical_scores_break_ties_by_version_then_ordinal(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    course = await seed_course(session_factory)
    for name in ("a", "b", "c"):
        await seed_version(session_factory, course, _blocks(MARKED), source_uri=f"upload:{name}")
    provider = _provider()
    await _index(session_factory, course, provider)

    first = await _search(session_factory, provider, course, SearchMode.LEXICAL, k=50)
    again = await _search(session_factory, provider, course, SearchMode.LEXICAL, k=50)

    keys = [(hit.version.id, hit.chunk_ordinal) for hit in first.hits]
    assert len(keys) == 3 and len({hit.score for hit in first.hits}) == 1
    assert keys == sorted(keys)
    assert [h.chunk_id for h in again.hits] == [h.chunk_id for h in first.hits]


async def test_lexical_k_is_a_prefix_of_a_deeper_ranking(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    course = await seed_course(session_factory)
    await seed_version(session_factory, course, [*paragraphs(0, 12), (P, MARKED, None)])
    provider = _provider()
    await _index(session_factory, course, provider)

    deep = await _search(session_factory, provider, course, SearchMode.LEXICAL, "sentence", k=50)
    top = await _search(session_factory, provider, course, SearchMode.LEXICAL, "sentence", k=3)

    assert len(deep.hits) > 3
    assert [h.chunk_id for h in top.hits] == [h.chunk_id for h in deep.hits[:3]]


async def test_lexical_search_needs_no_provider_and_builds_no_client(
    session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    course, _ = await _marked_course(session_factory)
    provider = _provider()
    await _index(session_factory, course, provider)
    calls = await _count(session_factory, ModelCall)

    def forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("lexical search must not construct a provider or client")

    monkeypatch.setattr(NebiusEmbeddingProvider, "from_settings", forbidden)
    monkeypatch.setattr(NebiusEmbeddingProvider, "__init__", forbidden)
    monkeypatch.setattr(openai.AsyncOpenAI, "__init__", forbidden)

    service = SearchService(session_factory, provider.space, params=SMALL)  # no provider
    request = SearchRequest.build(course_id=course, query=MARKER, mode=SearchMode.LEXICAL)
    result = await service.search_course(request)

    assert result.hits and MARKER in result.hits[0].text
    assert await _count(session_factory, ModelCall) == calls


async def test_vector_search_without_a_provider_is_a_configuration_error(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    course, _ = await _marked_course(session_factory)
    service = SearchService(session_factory, _provider().space, params=SMALL)

    with pytest.raises(AIConfigurationError):
        await service.search_course(SearchRequest.build(course_id=course, query=MARKER))

    assert await _count(session_factory, ModelCall) == 0


def test_the_provider_must_embed_in_the_search_space() -> None:
    other = FakeEmbeddingProvider(dimensions=DIMS + 1)

    with pytest.raises(ValueError, match="embedding space"):
        SearchService(None, _provider().space, other)  # type: ignore[arg-type]


def test_vector_is_the_default_mode_and_only_implemented_modes_validate() -> None:
    assert SearchRequest.build(course_id=uuid.uuid4(), query="q").mode is SearchMode.VECTOR
    assert [m.value for m in SearchMode] == ["vector", "lexical"]
    with pytest.raises(InvalidSearchRequest) as caught:
        SearchRequest.build(course_id=uuid.uuid4(), query="q", mode="hybrid")
    assert caught.value.fields == ("mode",)


def test_a_nul_character_is_an_invalid_request_that_never_echoes_the_query() -> None:
    with pytest.raises(InvalidSearchRequest) as caught:
        SearchRequest.build(course_id=uuid.uuid4(), query=f"zebra{chr(0)}quokka")

    assert caught.value.fields == ("query",) and "zebra" not in str(caught.value)


# --- adversarial lexical queries ------------------------------------------------------------

ADVERSARIAL = {
    "sql-injection": "zebra'; DROP TABLE chunks; --",
    "tautology": "zebra' OR 'x'='x",
    "double-quotes": 'quokka" | "narwhal',
    "tsquery-operators": "zebra & !quokka | narwhal",
    "prefix-and-weights": "narwhal:* zebra:AB (quokka)",
    "backslashes": f"zebra{BACKSLASH}' {BACKSLASH}{BACKSLASH}quokka",
    "apostrophes": "O'Brien's zebra",
    "followed-by": "zebra <-> quokka <2> narwhal",
}
NO_LEXEMES = {
    "stop-words": "the of and is to",
    "punctuation": "?!.,;:...",
    "operators-only": "| & ! : * ( ) <->",
    "quotes-only": "' '' \" \"\"",
}


@pytest.mark.parametrize("query", ADVERSARIAL.values(), ids=ADVERSARIAL.keys())
async def test_adversarial_queries_are_data_never_sql_or_tsquery_syntax(
    session_factory: async_sessionmaker[AsyncSession], query: str
) -> None:
    course, version = await _marked_course(session_factory)
    other = await seed_course(session_factory, "Other")
    await seed_version(session_factory, other, _blocks(MARKED))
    provider = _provider()
    await _index(session_factory, course, provider)
    await _index(session_factory, other, provider)
    chunks = await _count(session_factory, Chunk)

    result = await _search(session_factory, provider, course, SearchMode.LEXICAL, query, k=50)

    assert result.hits and MARKER in result.hits[0].text  # matched as plain words
    assert {hit.version.id for hit in result.hits} == {version}  # still course-scoped
    assert await _count(session_factory, Chunk) == chunks  # nothing was executed


@pytest.mark.parametrize("query", NO_LEXEMES.values(), ids=NO_LEXEMES.keys())
async def test_queries_without_lexemes_have_no_lexical_candidates_and_no_error(
    session_factory: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
    query: str,
) -> None:
    course, _ = await _marked_course(session_factory)
    provider = _provider()
    await _index(session_factory, course, provider)
    calls = await _count(session_factory, ModelCall)
    caplog.set_level(logging.DEBUG)

    result = await _search(session_factory, provider, course, SearchMode.LEXICAL, query)

    assert result.hits == () and result.searchable_versions == 1
    assert await _count(session_factory, ModelCall) == calls
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


async def test_vector_search_still_ranks_a_query_without_lexemes(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    course, _ = await _marked_course(session_factory)
    provider = _provider()
    await _index(session_factory, course, provider)

    result = await _search(session_factory, provider, course, SearchMode.VECTOR, "the of and")

    assert result.hits  # the vector ranker always ranks the verified corpus


TRICKY_LEXEMES = [
    "o'brien",
    f"back{BACKSLASH}slash",
    "a|b",
    "x & y",
    "!not",
    "c:*",
    "sp ace",
    "'",
    "(",
    "<->",
    "plain",
]


async def test_quoted_lexemes_round_trip_through_tsquery_exactly(
    db_session: AsyncSession,
) -> None:
    tsquery = or_tsquery(TRICKY_LEXEMES)
    parsed = cast(bindparam("q", tsquery), TSQUERY)

    nodes = await db_session.scalar(select(func.numnode(parsed)))
    assert nodes == 2 * len(TRICKY_LEXEMES) - 1  # n operands joined by n - 1 ORs
    for lexeme in TRICKY_LEXEMES:
        document = func.array_to_tsvector(cast(bindparam("l", [lexeme]), ARRAY(Text)))
        assert await db_session.scalar(select(document.bool_op("@@")(parsed))), lexeme
    unrelated = func.array_to_tsvector(cast(bindparam("u", ["brien"]), ARRAY(Text)))
    assert await db_session.scalar(select(unrelated.bool_op("@@")(parsed))) is False


def test_quote_lexeme_doubles_quotes_and_escapes_backslashes() -> None:
    assert quote_lexeme("o'brien") == "'o''brien'"
    assert quote_lexeme(f"a{BACKSLASH}b") == f"'a{BACKSLASH}{BACKSLASH}b'"
    assert or_tsquery(["b", "a"]) == "'b' | 'a'"  # caller's (already sorted) order
    with pytest.raises(ValueError):
        or_tsquery([])


async def test_query_lexemes_are_postgresql_normalised_and_sorted(
    db_session: AsyncSession,
) -> None:
    lexemes = await query_lexemes(db_session, "Submitting the REPORTS, then submit again!")

    # Stemmed, stop words ("the", "then", "again") dropped, distinct, C-sorted.
    assert lexemes == ["report", "submit"]


# --- one snapshot for final classification, ranking and evidence --------------------------------


async def test_read_only_snapshot_is_repeatable_read_and_read_only(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with read_only_snapshot(session_factory) as session:
        isolation = await session.scalar(text("SHOW transaction_isolation"))
        read_only = await session.scalar(text("SHOW transaction_read_only"))
        with pytest.raises(DBAPIError, match="read-only transaction"):
            await session.execute(text("CREATE TEMP TABLE forbidden (x int)"))

    assert (isolation, read_only) == ("repeatable read", "on")


async def test_a_snapshot_does_not_see_commits_made_after_its_first_read(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with read_only_snapshot(session_factory) as session:
        before = await session.scalar(select(func.count()).select_from(Course))
        await seed_course(session_factory, "Committed meanwhile")
        during = await session.scalar(select(func.count()).select_from(Course))

    assert during == before
    assert await _count(session_factory, Course) == before + 1


class _Recorder:
    """Wraps the classification and the rankers, recording which session and
    which version IDs each one used."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.classified: list[tuple[AsyncSession, tuple[uuid.UUID, ...]]] = []
        self.ranked: list[tuple[str, AsyncSession, tuple[uuid.UUID, ...]]] = []
        self.after_classify: Any = None
        classify, vector, lexical = (
            search.classify_course_in_session,
            search.rank_vector,
            search.rank_lexical,
        )

        async def classify_wrapper(session: AsyncSession, *args: Any, **kwargs: Any) -> Any:
            states = await classify(session, *args, **kwargs)
            searchable = tuple(v for v, s in states if s is VersionSearchState.SEARCHABLE)
            self.classified.append((session, searchable))
            if self.after_classify is not None:
                await self.after_classify()
            return states

        def ranker(name: str, inner: Any) -> Any:
            async def wrapper(
                session: AsyncSession, scope: SearchScope, *args: Any
            ) -> list[RankedCandidate]:
                self.ranked.append((name, session, scope.version_ids))
                return list(await inner(session, scope, *args))

            return wrapper

        monkeypatch.setattr(search, "classify_course_in_session", classify_wrapper)
        monkeypatch.setattr(search, "rank_vector", ranker("vector", vector))
        monkeypatch.setattr(search, "rank_lexical", ranker("lexical", lexical))


async def test_vector_ranks_only_versions_classified_in_its_own_snapshot(
    session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    course, first = await _marked_course(session_factory)
    provider = _provider()
    await _index(session_factory, course, provider)
    second = await seed_version(
        session_factory, course, _blocks(MARKED), source_uri="upload:second.pdf"
    )
    recorder = _Recorder(monkeypatch)

    async def index_second_during_the_call(_inputs: object) -> None:
        await _index(session_factory, course, _provider())

    provider.before_embed = index_second_during_the_call

    result = await _search(session_factory, provider, course, SearchMode.VECTOR, k=50)

    (initial_session, initial), (final_session, final) = recorder.classified
    ((name, ranking_session, ranked),) = recorder.ranked
    assert initial == (first,)  # before the call: only the first version was indexed
    assert set(final) == {first, second}  # after it: both, verified in the final snapshot
    assert name == "vector" and ranking_session is final_session
    assert ranking_session is not initial_session
    assert ranked == final  # exactly the IDs verified in the ranking's own snapshot
    assert {hit.version.id for hit in result.hits} == {first, second}


async def test_lexical_classifies_ranks_and_reads_evidence_in_one_snapshot(
    session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    course, v1 = await _marked_course(session_factory)
    provider = _provider()
    await _index(session_factory, course, provider)
    recorder = _Recorder(monkeypatch)

    async def supersede_v1() -> None:  # committed by another session, mid-search
        recorder.after_classify = None
        await seed_version(session_factory, course, _blocks("an unindexed v2"))

    recorder.after_classify = supersede_v1

    result = await _search(session_factory, provider, course, SearchMode.LEXICAL)

    ((classify_session, classified),) = recorder.classified
    ((name, ranking_session, ranked),) = recorder.ranked
    assert name == "lexical" and ranking_session is classify_session
    assert ranked == classified == (v1,)
    # The ranking and the evidence read the snapshot the classification read:
    # v1 is still current there, so its verified chunks rank, with evidence.
    assert result.hits and {hit.version.id for hit in result.hits} == {v1}
    _assert_exact_provenance(result)

    after = await _search(session_factory, provider, course, SearchMode.LEXICAL)
    assert after.hits == () and (after.current_versions, after.searchable_versions) == (1, 0)


async def test_no_transaction_is_open_while_the_query_is_embedded(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    course, _ = await _marked_course(session_factory)
    provider = _provider()
    await _index(session_factory, course, provider)
    seen: list[int] = []

    async def count_open_transactions(_inputs: object) -> None:
        async with session_factory() as session:
            seen.append(
                int(
                    await session.scalar(
                        text(
                            "SELECT count(*) FROM pg_stat_activity "
                            "WHERE datname = current_database() AND pid <> pg_backend_pid() "
                            "AND state LIKE 'idle in transaction%'"
                        )
                    )
                    or 0
                )
            )

    provider.before_embed = count_open_transactions

    await _search(session_factory, provider, course, SearchMode.VECTOR)

    assert seen == [0]


# --- the vector ranker is the original vector query ----------------------------------------------


async def _reference_vector_ranking(
    session: AsyncSession,
    course: uuid.UUID,
    searchable: Sequence[uuid.UUID],
    space_id: uuid.UUID,
    dimensions: int,
    vector: Sequence[float],
    k: int,
) -> list[tuple[uuid.UUID, float]]:
    """The vector query as it was before rankers existed, verbatim in shape."""
    distance = Embedding.vector.cosine_distance(list(vector)).label("distance")
    statement = (
        select(Chunk.id, distance)
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
            Resource.course_id == course,
            Chunk.resource_version_id.in_(searchable),
            Chunk.chunker_version == SMALL.version,
            ChunkEmbedding.space_id == space_id,
            ChunkEmbedding.input_format == DOCUMENT_INPUT_FORMAT,
            Embedding.space_id == space_id,
            Embedding.dimensions == dimensions,
        )
        .order_by(distance, ResourceVersion.id, Chunk.ordinal)
        .limit(k)
    )
    return [(row.id, float(row.distance)) for row in (await session.execute(statement)).all()]


@pytest.mark.parametrize(
    "query", [MARKER, "sentence explains topic", "rubric week nine", "unrelated words"]
)
@pytest.mark.parametrize("k", [1, 5, 50])
async def test_rank_vector_equals_the_original_vector_query(
    session_factory: async_sessionmaker[AsyncSession], query: str, k: int
) -> None:
    course, _ = await _marked_course(session_factory)
    for name in ("dup-a", "dup-b"):  # identical texts: exact distance ties
        await seed_version(session_factory, course, _blocks(MARKED), source_uri=f"upload:{name}")
    stale_course = await seed_course(session_factory, "Other")
    await seed_version(session_factory, stale_course, _blocks(MARKED))
    provider = _provider()
    await _index(session_factory, course, provider)
    await _index(session_factory, stale_course, provider)
    vector = (await provider.embed([query_input(query)])).vectors[0]

    async with read_only_snapshot(session_factory) as session:
        states = await search.classify_course_in_session(
            session, course, provider.space, params=SMALL
        )
        searchable = tuple(v for v, s in states if s is VersionSearchState.SEARCHABLE)
        space = await store.find_space(session, provider.space)
        assert space is not None
        scope = SearchScope(course, searchable, SMALL.version)
        ranked = await rank_vector(session, scope, space, vector, k)
        reference = await _reference_vector_ranking(
            session, course, searchable, space.id, space.dimensions, vector, k
        )

    assert [(c.chunk_id, c.vector_distance) for c in ranked] == reference
    assert [c.rank for c in ranked] == list(range(1, len(ranked) + 1))
    assert all(c.lexical_score is None for c in ranked)
