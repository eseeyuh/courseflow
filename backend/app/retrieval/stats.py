# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Chunk statistics and token budgets for a course's current versions.

Read-only and offline: the deterministic chunk set is re-derived from each
version's stored text and spans (as indexing and inspection do); stored chunks
are read only to say whether they match it. Nothing is persisted, and no
provider, credential or network is involved.

Definitions (characters are Unicode code points; tokens are CourseFlow's
``ceil(chars / 4)`` estimate, never a tokenizer count):

- estimated_tokens: min, p50, p95, max over chunks (nearest-rank percentiles).
- chunks_at_or_above_target / chunks_at_hard_max: chunks whose estimate is
  >= the target, or equal to the hard maximum.
- spans_per_chunk: last - first span ordinal + 1, mean and max.
- split_spans: visible spans not contained whole in any single chunk (an
  oversized span cut inside itself).
- chunk_edges_inside_spans: chunk starts after its first span's first
  visible character, plus chunk ends before its last span's last one.
- duplication_factor = total chunk characters / unique covered characters:
  1.0 means no text is repeated; above 1.0 reflects overlap.
  overlap_extra_ratio = duplication_factor - 1.
- visible_coverage = covered non-whitespace characters / all non-whitespace
  characters. None (not applicable) when a version has no visible text. For a
  chunkable version anything below 1.0 is an integrity failure, never a
  statistic.
- uncovered_whitespace_chars: characters in no chunk (all whitespace).
"""

from __future__ import annotations

import math
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import Course
from app.retrieval import store
from app.retrieval.chunking import SPAN_PACK_V1, ChunkingParams, ChunkSpan
from app.retrieval.indexing import CourseNotFoundError, SourceContent, derive_chunks
from app.retrieval.provenance import ChunkDraft
from app.retrieval.store import ChunkState


class StatsIntegrityError(RuntimeError):
    """A current version cannot be chunked safely, or its chunks do not cover
    all of its visible text."""

    def __init__(self, version_ids: Sequence[uuid.UUID]) -> None:
        super().__init__(f"{len(version_ids)} version(s) failed chunk integrity checks")
        self.version_ids = tuple(version_ids)


@dataclass(frozen=True, slots=True)
class Distribution:
    min: int
    p50: int
    p95: int
    max: int


@dataclass(frozen=True, slots=True)
class ChunkStats:
    chunks: int
    spans: int
    visible_spans: int
    estimated_tokens: Distribution | None
    chunks_at_or_above_target: int
    chunks_at_hard_max: int
    spans_per_chunk_mean: float | None
    spans_per_chunk_max: int | None
    split_spans: int
    chunk_edges_inside_spans: int
    total_chunk_chars: int
    unique_covered_chars: int
    duplication_factor: float | None
    overlap_extra_ratio: float | None
    visible_chars: int
    covered_visible_chars: int
    visible_coverage: float | None  # None: not applicable (no visible text)
    uncovered_whitespace_chars: int


@dataclass(frozen=True, slots=True)
class VersionStats:
    resource_version_id: uuid.UUID
    content: SourceContent
    # Whether the stored chunks are exactly the deterministic set.
    stored_chunk_state: ChunkState
    stats: ChunkStats


@dataclass(frozen=True, slots=True)
class CourseStats:
    course_id: uuid.UUID
    chunker_version: str
    target_estimated_tokens: int
    hard_max_estimated_tokens: int
    versions: tuple[VersionStats, ...]
    total: ChunkStats


async def course_stats(
    session_factory: async_sessionmaker[AsyncSession],
    course_id: uuid.UUID,
    *,
    params: ChunkingParams = SPAN_PACK_V1,
) -> CourseStats:
    async with session_factory() as session:
        if not await session.scalar(select(exists().where(Course.id == course_id))):
            raise CourseNotFoundError(f"course {course_id} does not exist")
        version_ids = await store.current_versions(session, course_id)
    versions: list[VersionStats] = []
    pooled: list[tuple[str, list[ChunkSpan], list[ChunkDraft]]] = []
    failed: list[uuid.UUID] = []
    for version_id in version_ids:
        async with session_factory() as session:
            source = await store.load_version(session, version_id)
            existing = await store.existing_chunks(session, version_id, params.version)
        content, drafts = derive_chunks(source, params)
        if content is SourceContent.INVALID or source is None or source.spans is None:
            failed.append(version_id)
            continue
        stats = chunk_stats(source.extracted_text, source.spans, drafts, params)
        if content is SourceContent.CHUNKABLE and stats.visible_coverage != 1.0:
            failed.append(version_id)
            continue
        versions.append(
            VersionStats(version_id, content, store.compare_chunks(existing, drafts), stats)
        )
        pooled.append((source.extracted_text, source.spans, drafts))
    if failed:
        raise StatsIntegrityError(failed)
    return CourseStats(
        course_id=course_id,
        chunker_version=params.version,
        target_estimated_tokens=params.target_estimated_tokens,
        hard_max_estimated_tokens=params.hard_max_estimated_tokens,
        versions=tuple(versions),
        total=_combine([v.stats for v in versions], pooled, params),
    )


def chunk_stats(
    text: str, spans: Sequence[ChunkSpan], drafts: Sequence[ChunkDraft], params: ChunkingParams
) -> ChunkStats:
    """Statistics of one version's deterministic chunk set (pure)."""
    estimates = sorted(d.estimated_tokens for d in drafts)
    per_chunk = [d.last_span_ordinal - d.first_span_ordinal + 1 for d in drafts]
    visible = {s.ordinal: _visible_bounds(text, s) for s in spans}
    visible = {ordinal: bounds for ordinal, bounds in visible.items() if bounds is not None}
    covered = _union((d.start_offset, d.end_offset) for d in drafts)
    unique = sum(end - start for start, end in covered)
    total_chars = sum(len(d.text) for d in drafts)
    visible_chars = _count_visible(text, [(0, len(text))])
    covered_visible = _count_visible(text, covered)
    return ChunkStats(
        chunks=len(drafts),
        spans=len(spans),
        visible_spans=len(visible),
        estimated_tokens=_distribution(estimates),
        chunks_at_or_above_target=sum(e >= params.target_estimated_tokens for e in estimates),
        chunks_at_hard_max=sum(e == params.hard_max_estimated_tokens for e in estimates),
        spans_per_chunk_mean=sum(per_chunk) / len(per_chunk) if per_chunk else None,
        spans_per_chunk_max=max(per_chunk) if per_chunk else None,
        split_spans=sum(
            not any(d.start_offset <= start and end <= d.end_offset for d in drafts)
            for start, end in visible.values()
        ),
        chunk_edges_inside_spans=sum(
            (d.start_offset > visible[d.first_span_ordinal][0])
            + (d.end_offset < visible[d.last_span_ordinal][1])
            for d in drafts
        ),
        total_chunk_chars=total_chars,
        unique_covered_chars=unique,
        duplication_factor=total_chars / unique if unique else None,
        overlap_extra_ratio=(total_chars - unique) / unique if unique else None,
        visible_chars=visible_chars,
        covered_visible_chars=covered_visible,
        visible_coverage=covered_visible / visible_chars if visible_chars else None,
        uncovered_whitespace_chars=len(text) - unique - (visible_chars - covered_visible),
    )


def _combine(
    parts: Sequence[ChunkStats],
    pooled: Sequence[tuple[str, list[ChunkSpan], list[ChunkDraft]]],
    params: ChunkingParams,
) -> ChunkStats:
    """Course totals: sums of counts, and distributions over all chunks."""
    estimates = sorted(d.estimated_tokens for _, _, drafts in pooled for d in drafts)
    per_chunk = [
        d.last_span_ordinal - d.first_span_ordinal + 1 for _, _, drafts in pooled for d in drafts
    ]
    total_chars = sum(p.total_chunk_chars for p in parts)
    unique = sum(p.unique_covered_chars for p in parts)
    visible = sum(p.visible_chars for p in parts)
    covered = sum(p.covered_visible_chars for p in parts)
    return ChunkStats(
        chunks=sum(p.chunks for p in parts),
        spans=sum(p.spans for p in parts),
        visible_spans=sum(p.visible_spans for p in parts),
        estimated_tokens=_distribution(estimates),
        chunks_at_or_above_target=sum(p.chunks_at_or_above_target for p in parts),
        chunks_at_hard_max=sum(p.chunks_at_hard_max for p in parts),
        spans_per_chunk_mean=sum(per_chunk) / len(per_chunk) if per_chunk else None,
        spans_per_chunk_max=max(per_chunk) if per_chunk else None,
        split_spans=sum(p.split_spans for p in parts),
        chunk_edges_inside_spans=sum(p.chunk_edges_inside_spans for p in parts),
        total_chunk_chars=total_chars,
        unique_covered_chars=unique,
        duplication_factor=total_chars / unique if unique else None,
        overlap_extra_ratio=(total_chars - unique) / unique if unique else None,
        visible_chars=visible,
        covered_visible_chars=covered,
        visible_coverage=covered / visible if visible else None,
        uncovered_whitespace_chars=sum(p.uncovered_whitespace_chars for p in parts),
    )


def _distribution(sorted_values: Sequence[int]) -> Distribution | None:
    if not sorted_values:
        return None
    return Distribution(
        min=sorted_values[0],
        p50=_nearest_rank(sorted_values, 0.50),
        p95=_nearest_rank(sorted_values, 0.95),
        max=sorted_values[-1],
    )


def _nearest_rank(sorted_values: Sequence[int], quantile: float) -> int:
    return sorted_values[max(0, math.ceil(quantile * len(sorted_values)) - 1)]


def _visible_bounds(text: str, span: ChunkSpan) -> tuple[int, int] | None:
    start, end = span.start_offset, span.end_offset
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return (start, end) if start < end else None


def _union(ranges: Iterable[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _count_visible(text: str, ranges: Sequence[tuple[int, int]]) -> int:
    return sum(not char.isspace() for start, end in ranges for char in text[start:end])
