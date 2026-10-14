# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Retrieval acceptance on the real stack: real ingestion (temporary raw store),
real span-pack/v1 chunking, real PostgreSQL + pgvector, indexing and search.
Only the embedding model is the offline fake. One test per requirement."""

import uuid
from pathlib import Path

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.db.models import Chunk, ChunkEmbedding, Course, Embedding, Institution
from app.domain.enums import LmsType, ResourceType
from app.ingestion.guards import IngestionLimits
from app.ingestion.schemas import RawSource
from app.ingestion.service import ingest_file
from app.ingestion.storage import LocalRawObjectStore
from app.retrieval.indexing import IndexingService
from app.retrieval.search import SearchRequest, SearchResult, SearchService
from tests.embedding_fakes import FakeEmbeddingProvider

pytestmark = pytest.mark.anyio

LIMITS = IngestionLimits.from_settings(
    Settings(_env_file=None, database_url="postgresql+asyncpg://u:p@127.0.0.1/db")  # type: ignore[call-arg, arg-type]
)
TARGET = "Coursework one requires a reflective log of pair programming sessions."
QUERY = "reflective log of pair programming sessions"


def _page(title: str, *paragraphs: str) -> bytes:
    body = "".join(f"<p>{p}</p>" for p in paragraphs)
    return f"<!DOCTYPE html><html><body><h1>{title}</h1>{body}</body></html>".encode()


def _filler(topic: str, count: int) -> list[str]:
    return [f"Week {i} of the {topic} module covers worked examples {i}." for i in range(count)]


@pytest.fixture
def session_factory(committed_session: AsyncSession) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(committed_session.bind, expire_on_commit=False)


@pytest.fixture
def raw_store(tmp_path: Path) -> LocalRawObjectStore:
    root = tmp_path / "raw"
    root.mkdir()
    return LocalRawObjectStore(root)


async def _course(sf: async_sessionmaker[AsyncSession], title: str) -> uuid.UUID:
    async with sf() as session, session.begin():
        course = Course(
            institution=Institution(name="Synthetic Test Institution", lms_type=LmsType.UPLOAD),
            title=title,
        )
        session.add(course)
    return course.id


async def _ingest(
    sf: async_sessionmaker[AsyncSession],
    store: LocalRawObjectStore,
    course: uuid.UUID,
    name: str,
    content: bytes,
    resource_type: ResourceType = ResourceType.BRIEF,
) -> uuid.UUID:
    async with sf() as session:
        result = await ingest_file(
            session,
            store,
            course_id=course,
            resource_type=resource_type,
            source=RawSource(content=content, display_name=name, source_uri=f"upload:{name}"),
            limits=LIMITS,
        )
        await session.commit()
    return result.version_id


async def _index(
    sf: async_sessionmaker[AsyncSession], course: uuid.UUID, provider: FakeEmbeddingProvider
):
    return await IndexingService(
        sf, provider, batch_max_items=64, batch_max_estimated_tokens=32_000
    ).index_course(course)


async def _search(
    sf: async_sessionmaker[AsyncSession],
    provider: FakeEmbeddingProvider,
    course: uuid.UUID,
    k: int = 5,
) -> SearchResult:
    request = SearchRequest.build(course_id=course, query=QUERY, k=k)
    return await SearchService(sf, provider).search_course(request)


async def _course_with_target(
    sf: async_sessionmaker[AsyncSession], store: LocalRawObjectStore, title: str = "Course A"
) -> tuple[uuid.UUID, uuid.UUID]:
    course = await _course(sf, title)
    brief = await _ingest(
        sf, store, course, "brief.html", _page("Brief", *_filler("databases", 20), TARGET)
    )
    await _ingest(
        sf,
        store,
        course,
        "lecture.html",
        _page("Lecture", *_filler("networks", 60)),
        ResourceType.LECTURE,
    )
    return course, brief


def _provider(**kwargs: object) -> FakeEmbeddingProvider:
    return FakeEmbeddingProvider(dimensions=256, **kwargs)  # type: ignore[arg-type]


# --- the original acceptance criteria -------------------------------------------------------------


async def test_a_known_query_retrieves_the_intentionally_matching_chunk_in_the_top_k(
    session_factory: async_sessionmaker[AsyncSession], raw_store: LocalRawObjectStore
) -> None:
    course, brief = await _course_with_target(session_factory, raw_store)
    provider = _provider()
    await _index(session_factory, course, provider)

    result = await _search(session_factory, provider, course)

    targets = [hit for hit in result.hits if TARGET in hit.text]
    assert targets and targets[0].rank <= 5
    assert targets[0].version.id == brief


async def test_the_course_filter_prevents_leakage_from_another_course(
    session_factory: async_sessionmaker[AsyncSession], raw_store: LocalRawObjectStore
) -> None:
    course_a, _ = await _course_with_target(session_factory, raw_store, "Course A")
    course_b = await _course(session_factory, "Course B")
    await _ingest(session_factory, raw_store, course_b, "b.html", _page("Other", QUERY))
    provider = _provider()
    await _index(session_factory, course_a, provider)
    await _index(session_factory, course_b, provider)

    result = await _search(session_factory, provider, course_a, k=50)

    async with session_factory() as session:
        course_a_versions = set(
            await session.scalars(
                text(
                    "SELECT rv.id FROM resource_versions rv JOIN resources r "
                    "ON r.id = rv.resource_id WHERE r.course_id = :c"
                ).bindparams(c=course_a)
            )
        )
    assert result.hits
    assert {hit.version.id for hit in result.hits} <= course_a_versions


async def test_every_returned_chunk_has_at_least_one_source_span(
    session_factory: async_sessionmaker[AsyncSession], raw_store: LocalRawObjectStore
) -> None:
    course, _ = await _course_with_target(session_factory, raw_store)
    provider = _provider()
    await _index(session_factory, course, provider)

    result = await _search(session_factory, provider, course, k=50)

    assert result.hits
    for hit in result.hits:
        assert hit.spans
        for span in hit.spans:
            assert span.section_path  # HTML spans locate by heading path
            assert (
                span.excerpt[span.relative_start : span.relative_end]
                == hit.text[
                    span.overlap_start - hit.start_offset : span.overlap_end - hit.start_offset
                ]
            )


async def test_reindexing_unchanged_content_does_not_duplicate_chunks(
    session_factory: async_sessionmaker[AsyncSession], raw_store: LocalRawObjectStore
) -> None:
    course, _ = await _course_with_target(session_factory, raw_store)
    provider = _provider()
    first = await _index(session_factory, course, provider)
    counts = await _counts(session_factory)
    calls = len(provider.calls)

    second = await _index(session_factory, course, provider)

    assert await _counts(session_factory) == counts
    assert len(provider.calls) == calls
    assert second.summary.versions_already_indexed == first.summary.versions_indexed == 2
    assert second.summary.chunks_created == second.summary.embeddings_created == 0


# --- the definition of done -------------------------------------------------------------------------


async def test_pgvector_backed_vector_storage_is_populated_and_searchable(
    session_factory: async_sessionmaker[AsyncSession], raw_store: LocalRawObjectStore
) -> None:
    course, _ = await _course_with_target(session_factory, raw_store)
    provider = _provider()
    await _index(session_factory, course, provider)

    async with session_factory() as session:
        dims = set(await session.scalars(select(func.vector_dims(Embedding.vector))))
        linked = await session.scalar(select(func.count()).select_from(ChunkEmbedding))
        chunks = await session.scalar(select(func.count()).select_from(Chunk))
        ann = await session.scalar(
            text(
                "SELECT count(*) FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
                "JOIN pg_am a ON a.oid = c.relam WHERE a.amname IN ('hnsw', 'ivfflat')"
            )
        )
    assert dims == {256} and linked == chunks > 0
    assert ann == 0  # exact cosine scan by design: no approximate index
    assert (await _search(session_factory, provider, course)).hits


async def test_the_embedding_provider_is_swappable(
    session_factory: async_sessionmaker[AsyncSession], raw_store: LocalRawObjectStore
) -> None:
    course, _ = await _course_with_target(session_factory, raw_store)
    first, second = _provider(), FakeEmbeddingProvider(dimensions=64, space_version="v-alt")
    for provider in (first, second):
        await _index(session_factory, course, provider)

    for provider, dims in ((first, 256), (second, 64)):
        result = await _search(session_factory, provider, course)
        assert result.hits and result.space.dimensions == dims


async def test_indexing_is_incremental_only_a_new_version_is_chunked(
    session_factory: async_sessionmaker[AsyncSession], raw_store: LocalRawObjectStore
) -> None:
    course, v1 = await _course_with_target(session_factory, raw_store)
    provider = _provider()
    await _index(session_factory, course, provider)
    v2 = await _ingest(
        session_factory,
        raw_store,
        course,
        "brief.html",
        _page("Brief", *_filler("databases", 20), TARGET, "Submission moved to week ten."),
    )

    summary = (await _index(session_factory, course, provider)).summary

    assert (summary.versions_indexed, summary.versions_already_indexed) == (1, 1)
    async with session_factory() as session:
        v2_chunks = await session.scalar(
            select(func.count()).where(Chunk.resource_version_id == v2)
        )
    assert summary.chunks_created == v2_chunks  # only the new version was chunked
    assert summary.chunks_reused > 0  # the unchanged lecture was reused, untouched
    result = await _search(session_factory, provider, course, k=50)
    assert v2 in {hit.version.id for hit in result.hits}
    assert v1 not in {hit.version.id for hit in result.hits}


async def _counts(sf: async_sessionmaker[AsyncSession]) -> tuple[int, int, int]:
    async with sf() as session:
        return (
            await session.scalar(select(func.count()).select_from(Chunk)),
            await session.scalar(select(func.count()).select_from(Embedding)),
            await session.scalar(select(func.count()).select_from(ChunkEmbedding)),
        )
