# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Chunk statistics: exact values on hand-built layouts, and the read-only,
offline course report."""

import uuid

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import SourceSpan
from app.domain.enums import BlockKind
from app.retrieval.chunking import SPAN_PACK_V1, ChunkingParams, chunk_version
from app.retrieval.indexing import CourseNotFoundError, IndexingService, SourceContent
from app.retrieval.stats import Distribution, StatsIntegrityError, chunk_stats, course_stats
from app.retrieval.store import ChunkState
from tests.chunking_cases import SEPARATOR, layout, sentences
from tests.embedding_fakes import FakeEmbeddingProvider
from tests.retrieval_seed import paragraphs, seed_course, seed_version

P = BlockKind.PAGE
EXAMPLE = [
    (P, "Week 3: recursion and base cases.", None),
    (P, "Lab 2 covers linked lists.", None),
    (P, "Coursework 1 is due on 31 October.", None),
]
# The worked example's toy parameters: chunks [0,33), [18,61), [48,97).
TOY = ChunkingParams("test/toy", 12, 17, 4, 13, 4, 6)
SMALL = ChunkingParams("test/small", 40, 50, 6, 44, 10, 20)


def _stats(blocks: list[tuple[BlockKind, str, None]], params: ChunkingParams):
    text, spans = layout(blocks)
    return chunk_stats(text, spans, chunk_version(text, spans, params), params)


def test_the_worked_example_has_exact_statistics() -> None:
    stats = _stats(EXAMPLE, TOY)

    assert (stats.chunks, stats.spans, stats.visible_spans) == (3, 3, 3)
    assert stats.estimated_tokens == Distribution(min=9, p50=11, p95=13, max=13)
    assert (stats.chunks_at_or_above_target, stats.chunks_at_hard_max) == (1, 0)
    assert (stats.spans_per_chunk_mean, stats.spans_per_chunk_max) == (5 / 3, 2)
    assert stats.split_spans == 0  # every span lies whole inside some chunk
    assert stats.chunk_edges_inside_spans == 2  # the two overlap starts
    assert (stats.total_chunk_chars, stats.unique_covered_chars) == (33 + 43 + 49, 97)
    assert stats.duplication_factor == pytest.approx(125 / 97)
    assert stats.overlap_extra_ratio == pytest.approx(28 / 97)
    assert stats.visible_coverage == 1.0
    assert stats.uncovered_whitespace_chars == 0


def test_without_overlap_nothing_is_duplicated_and_separators_stay_uncovered() -> None:
    no_overlap = ChunkingParams("test/no-overlap", 12, 17, 0, 13, 4, 6)

    stats = _stats(EXAMPLE, no_overlap)

    assert stats.duplication_factor == 1.0 and stats.overlap_extra_ratio == 0.0
    assert stats.uncovered_whitespace_chars == 2 * len(SEPARATOR)
    assert stats.visible_coverage == 1.0


def test_an_oversized_span_cut_inside_itself_counts_as_split() -> None:
    stats = _stats([(P, sentences(0, 20), None)], SMALL)

    assert stats.chunks > 1 and stats.split_spans == 1
    assert stats.chunks_at_hard_max <= stats.chunks
    assert stats.visible_coverage == 1.0


def test_no_visible_text_reports_not_applicable_not_a_manufactured_100_percent() -> None:
    text, spans = layout([(P, "\u00a0", None)])

    stats = chunk_stats(text, spans, [], SPAN_PACK_V1)

    assert stats.visible_coverage is None
    assert stats.duplication_factor is None and stats.overlap_extra_ratio is None
    assert stats.estimated_tokens is None and stats.spans_per_chunk_mean is None
    assert (stats.visible_spans, stats.uncovered_whitespace_chars) == (0, 1)


# --- the course report ---------------------------------------------------------------------------


@pytest.fixture
def session_factory(committed_session: AsyncSession) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(committed_session.bind, expire_on_commit=False)


@pytest.mark.anyio
async def test_course_stats_report_each_current_version_and_whether_stored_chunks_match(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    course = await seed_course(session_factory)
    await seed_version(session_factory, course, paragraphs(0, 6), source_uri="upload:a.pdf")
    await seed_version(
        session_factory, course, [(P, "\u00a0", None)], source_uri="upload:blank.pdf"
    )

    before = await course_stats(session_factory, course, params=SMALL)
    await IndexingService(
        session_factory,
        FakeEmbeddingProvider(),
        batch_max_items=8,
        batch_max_estimated_tokens=400,
        params=SMALL,
    ).index_course(course)
    after = await course_stats(session_factory, course, params=SMALL)

    contents = sorted(v.content for v in before.versions)
    assert contents == sorted([SourceContent.CHUNKABLE, SourceContent.NO_VISIBLE_TEXT])
    assert {v.stored_chunk_state for v in before.versions} == {ChunkState.NONE}
    chunkable = [v for v in after.versions if v.content is SourceContent.CHUNKABLE]
    assert [v.stored_chunk_state for v in chunkable] == [ChunkState.EXACT]
    assert after.total.chunks == chunkable[0].stats.chunks
    assert after.total.visible_coverage == 1.0  # the blank version is not applicable
    assert after.chunker_version == "test/small"


@pytest.mark.anyio
async def test_invalid_source_state_is_an_integrity_failure_not_a_statistic(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    course = await seed_course(session_factory)
    version = await seed_version(session_factory, course, paragraphs(0, 3))
    async with session_factory() as session, session.begin():
        await session.execute(
            update(SourceSpan)
            .where(SourceSpan.resource_version_id == version, SourceSpan.ordinal == 1)
            .values(start_offset=SourceSpan.start_offset - 3, end_offset=SourceSpan.end_offset - 3)
        )

    with pytest.raises(StatsIntegrityError) as caught:
        await course_stats(session_factory, course, params=SMALL)

    assert caught.value.version_ids == (version,)


@pytest.mark.anyio
async def test_an_unknown_course_is_refused(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    with pytest.raises(CourseNotFoundError):
        await course_stats(session_factory, uuid.uuid4())
