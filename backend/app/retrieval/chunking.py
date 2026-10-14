# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""``span-pack/v1``: deterministic, provenance-preserving chunking of one version.

A chunk is only ever a pair of offsets; its text is always
``extracted_text[start_offset:end_offset]``. Nothing is concatenated: span
separators, overlap and whitespace inside a chunk are there because they lie
between its two offsets in the version's text.

Sizes are *estimated* tokens, ``ceil(characters / 4)``: a deterministic
packing heuristic, not a tokenizer count. Every bound below (target, hard
maximum, overlap) is a bound on that estimate.

Steps:
1. Units: each source span, trimmed to its non-whitespace extent; a span
   above ``split_above`` is cut into pieces at its best internal boundary.
2. Groups: consecutive units of the same slide stay together (slide text
   and notes); headings attach forward to the content that follows them.
3. Packing: groups are added in order while the chunk stays within the
   target. A new chunk may start earlier, inside the previous chunk
   (overlap), only where text flows on: never before a heading or a new slide.
4. A final chunk below ``min`` merges into the previous one when it fits.
5. Every chunk is re-checked (provenance, size, coverage) before it is returned.

Pure: no database, no model, no environment, no ``hash()``. The same text,
spans and parameters always give identical chunks. Changing any parameter or
rule requires a new chunker version.
"""

from __future__ import annotations

import bisect
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Protocol

from app.domain.enums import BlockKind
from app.retrieval.provenance import (
    ChunkDraft,
    ChunkProvenanceError,
    SpanBounds,
    check_chunk_provenance,
)

_WHITESPACE_RUN = re.compile(r"\s+")  # same characters as str.isspace()
_SENTENCE_END = ".!?"
_SLIDE_KINDS = frozenset({BlockKind.SLIDE_TEXT, BlockKind.SLIDE_NOTES})

# Boundary ranks: lower is better.
_SPAN_START, _PARAGRAPH, _LINE, _SENTENCE, _SPACE = range(5)


class ChunkingError(RuntimeError):
    """Invalid input, or output that breaks the chunk contract (a bug).
    Nothing that raises this is ever persisted."""


@dataclass(frozen=True, slots=True)
class ChunkingParams:
    """A chunker version and its bounds, all in *estimated* tokens."""

    version: str
    target_estimated_tokens: int
    hard_max_estimated_tokens: int
    overlap_estimated_tokens: int
    split_above_estimated_tokens: int
    min_estimated_tokens: int
    # Cuts inside an oversized span fall this far, or further, into the piece.
    split_piece_min_estimated_tokens: int
    chars_per_estimated_token: int = 4

    def __post_init__(self) -> None:
        if not (
            0 < self.min_estimated_tokens <= self.target_estimated_tokens
            and 0 < self.split_piece_min_estimated_tokens < self.target_estimated_tokens
            and self.target_estimated_tokens < self.split_above_estimated_tokens
            and self.split_above_estimated_tokens + self.overlap_estimated_tokens
            <= self.hard_max_estimated_tokens
            and self.overlap_estimated_tokens >= 0
            and self.chars_per_estimated_token > 0
        ):
            raise ValueError(f"inconsistent chunking parameters for {self.version}")

    def estimate(self, length: int) -> int:
        """Estimated tokens for ``length`` characters: ceil(length / chars per token)."""
        return -(-length // self.chars_per_estimated_token)

    def chars(self, estimated_tokens: int) -> int:
        """The most characters whose estimate is at most ``estimated_tokens``."""
        return estimated_tokens * self.chars_per_estimated_token


SPAN_PACK_V1 = ChunkingParams(
    version="span-pack/v1",
    target_estimated_tokens=800,
    hard_max_estimated_tokens=1000,
    overlap_estimated_tokens=120,
    split_above_estimated_tokens=880,
    min_estimated_tokens=200,
    split_piece_min_estimated_tokens=400,
)


def estimate_tokens(text: str, params: ChunkingParams = SPAN_PACK_V1) -> int:
    return params.estimate(len(text))


@dataclass(frozen=True, slots=True)
class ChunkSpan:
    """What the chunker needs from a SourceSpan."""

    ordinal: int
    block_kind: BlockKind
    slide_number: int | None
    start_offset: int
    end_offset: int


class _SpanRow(Protocol):
    @property
    def ordinal(self) -> int: ...
    @property
    def block_kind(self) -> BlockKind: ...
    @property
    def slide_number(self) -> int | None: ...
    @property
    def start_offset(self) -> int | None: ...
    @property
    def end_offset(self) -> int | None: ...


def chunk_spans(rows: Iterable[_SpanRow]) -> list[ChunkSpan]:
    """ChunkSpans from SourceSpan rows, in ordinal order."""
    spans = []
    for row in sorted(rows, key=lambda r: r.ordinal):
        if row.start_offset is None or row.end_offset is None:
            raise ChunkingError(f"span {row.ordinal} has no offsets")
        spans.append(
            ChunkSpan(
                row.ordinal, row.block_kind, row.slide_number, row.start_offset, row.end_offset
            )
        )
    return spans


@dataclass(frozen=True, slots=True)
class Unit:
    """A non-whitespace range inside one span: the whole span or one piece of it."""

    start: int
    end: int
    span: ChunkSpan

    @property
    def is_heading(self) -> bool:
        return self.span.block_kind is BlockKind.HEADING

    @property
    def slide(self) -> int | None:
        return self.span.slide_number if self.span.block_kind in _SLIDE_KINDS else None


def chunk_version(
    extracted_text: str, spans: Sequence[ChunkSpan], params: ChunkingParams = SPAN_PACK_V1
) -> list[ChunkDraft]:
    """Chunk one version. Raises ``ChunkingError`` rather than return a bad chunk."""
    _check_input(extracted_text, spans)
    units = build_units(extracted_text, spans, params)
    if not units:
        raise ChunkingError("no span has visible text")
    packer = _Packer(extracted_text, params, units)
    for group in group_units(units):
        packer.add(group)
    ranges = packer.finish()
    drafts = _drafts(extracted_text, spans, ranges, params)
    check_coverage(drafts, extracted_text, spans, params)
    return drafts


# --- 1. units --------------------------------------------------------------------------


def build_units(text: str, spans: Sequence[ChunkSpan], params: ChunkingParams) -> list[Unit]:
    units = []
    for span in spans:
        start, end = span.start_offset, span.end_offset
        while start < end and text[start].isspace():
            start += 1
        while end > start and text[end - 1].isspace():
            end -= 1
        if start == end:
            continue  # whitespace only: nothing to retrieve
        if params.estimate(end - start) <= params.split_above_estimated_tokens:
            units.append(Unit(start, end, span))
        else:
            units.extend(Unit(a, b, span) for a, b in _split(text, start, end, params))
    return units


def _split(text: str, start: int, end: int, params: ChunkingParams) -> list[tuple[int, int]]:
    """Contiguous pieces of ``text[start:end]``, each within the target, cut at
    the best internal boundary. Whitespace at a cut belongs to no piece."""
    pieces = []
    cursor = start
    while params.estimate(end - cursor) > params.split_above_estimated_tokens:
        lowest = cursor + params.chars(params.split_piece_min_estimated_tokens)
        highest = cursor + params.chars(params.target_estimated_tokens)
        piece_end, next_start = _cut(text, cursor, lowest, highest)
        pieces.append((cursor, piece_end))
        cursor = next_start
    pieces.append((cursor, end))
    return pieces


def _cut(text: str, cursor: int, lowest: int, highest: int) -> tuple[int, int]:
    """(piece end, next piece start) for a piece starting at ``cursor``.

    Prefers a whitespace run starting in [lowest, highest]: best rank, then
    the latest. Failing that, the latest run before ``lowest`` (never split a
    word while whitespace exists). Only with no whitespace at all: a hard cut.
    """
    best_in_window: tuple[int, int, int] | None = None  # (rank, -start, end)
    best_before: tuple[int, int] | None = None
    for match in _WHITESPACE_RUN.finditer(text, cursor + 1, highest + 1):
        run_start = match.start()
        run_end = _WHITESPACE_RUN.match(text, run_start).end()  # type: ignore[union-attr]
        if run_start >= lowest:
            candidate = (_run_rank(text, run_start, run_end), -run_start, run_end)
            if best_in_window is None or candidate < best_in_window:
                best_in_window = candidate
        else:
            best_before = (run_start, run_end)  # runs come in order: keep the latest
    if best_in_window is not None:
        return -best_in_window[1], best_in_window[2]
    if best_before is not None:
        return best_before
    return highest, highest


def _run_rank(text: str, run_start: int, run_end: int) -> int:
    run = text[run_start:run_end]
    if "\n\n" in run:
        return _PARAGRAPH
    if "\n" in run:
        return _LINE
    if run_start > 0 and text[run_start - 1] in _SENTENCE_END:
        return _SENTENCE
    return _SPACE


# --- 2. groups -------------------------------------------------------------------------


def group_units(units: Sequence[Unit]) -> list[list[Unit]]:
    """Keep-together groups, in source order.

    - Consecutive units of the same slide form one group. Only consecutive:
      a slide number seen again later starts a new group, so distant text is
      never merged or reordered.
    - Headings attach forward: consecutive headings join the group after them.
      Headings with nothing after them form a final group of their own.
    """
    base: list[list[Unit]] = []
    for unit in units:
        previous = base[-1][-1] if base else None
        if unit.slide is not None and previous is not None and previous.slide == unit.slide:
            base[-1].append(unit)
        else:
            base.append([unit])
    groups: list[list[Unit]] = []
    headings: list[Unit] = []
    for group in base:
        if all(unit.is_heading for unit in group):
            headings.extend(group)
        else:
            groups.append(headings + group)
            headings = []
    if headings:
        groups.append(headings)
    return groups


# --- 3-4. packing ----------------------------------------------------------------------


class _Packer:
    def __init__(self, text: str, params: ChunkingParams, units: Sequence[Unit]) -> None:
        self._text = text
        self._params = params
        # A span's first visible character is the best place for overlap to start.
        self._span_starts = {
            unit.start
            for index, unit in enumerate(units)
            if index == 0 or units[index - 1].span is not unit.span
        }
        self._ranges: list[tuple[int, int]] = []
        self._start = -1
        self._end = -1
        self._last: Unit | None = None  # the last new material in the open chunk

    def add(self, group: Sequence[Unit]) -> None:
        if self._last is None:
            self._open(group, overlap=False)
        elif self._fits(group[-1].end):
            self._extend(group[-1])
        else:
            self._close()
            self._open(group, overlap=self._overlap_allowed(group[0]))

    def finish(self) -> list[tuple[int, int]]:
        self._close()
        ranges = self._ranges
        if len(ranges) >= 2:
            (previous_start, _), (last_start, last_end) = ranges[-2], ranges[-1]
            small = self._estimate(last_start, last_end) < self._params.min_estimated_tokens
            if small and self._estimate(previous_start, last_end) <= self._hard_max:
                ranges[-2:] = [(previous_start, last_end)]
        return ranges

    @property
    def _hard_max(self) -> int:
        return self._params.hard_max_estimated_tokens

    def _estimate(self, start: int, end: int) -> int:
        return self._params.estimate(end - start)

    def _fits(self, end: int) -> bool:
        size = self._estimate(self._start, end)
        if size <= self._params.target_estimated_tokens:
            return True
        small = self._estimate(self._start, self._end) < self._params.min_estimated_tokens
        return small and size <= self._hard_max

    def _overlap_allowed(self, first_new: Unit) -> bool:
        """Overlap only where text flows on: not before a heading, and not
        before material from a different slide than the last new material."""
        if first_new.is_heading:
            return False
        last_slide = self._last.slide if self._last is not None else None
        return not (first_new.slide is not None and first_new.slide != last_slide)

    def _open(self, units: Sequence[Unit], *, overlap: bool) -> None:
        whole_fits = self._estimate(units[0].start, units[-1].end) <= self._hard_max
        must_reach = units[-1].end if whole_fits else units[0].end
        start = units[0].start
        if overlap:
            overlap_start = self._overlap_start(must_reach)
            if overlap_start is not None:
                start = overlap_start
        self._start, self._end, self._last = start, units[0].end, units[0]
        if whole_fits:
            self._extend(units[-1])
            return
        # Too large to keep together: its units are packed one at a time.
        for unit in units[1:]:
            if self._fits(unit.end):
                self._extend(unit)
            else:
                self._close()
                self._open([unit], overlap=self._overlap_allowed(unit))

    def _extend(self, unit: Unit) -> None:
        self._end, self._last = unit.end, unit

    def _close(self) -> None:
        if self._last is not None:
            self._ranges.append((self._start, self._end))

    def _overlap_start(self, must_reach: int) -> int | None:
        """The earliest best-ranked boundary in the last ``overlap`` estimated
        tokens of the previous chunk such that the new chunk, from there to
        ``must_reach``, stays within the hard maximum."""
        previous_start, previous_end = self._ranges[-1]
        lowest = max(
            previous_start + 1,
            previous_end - self._params.chars(self._params.overlap_estimated_tokens),
        )
        best: tuple[int, int] | None = None
        for position in range(lowest, previous_end):
            rank = self._start_rank(position)
            if rank is None or self._estimate(position, must_reach) > self._hard_max:
                continue
            if best is None or (rank, position) < best:
                best = (rank, position)
        return None if best is None else best[1]

    def _start_rank(self, position: int) -> int | None:
        """How good ``position`` is as a chunk start; None if it cannot start one."""
        text = self._text
        if text[position].isspace():
            return None
        if position in self._span_starts:
            return _SPAN_START
        if not text[position - 1].isspace():
            return None
        run_start = position - 1
        while run_start > 0 and text[run_start - 1].isspace():
            run_start -= 1
        return _run_rank(text, run_start, position)


# --- 5. drafts and checks --------------------------------------------------------------


def _drafts(
    text: str,
    spans: Sequence[ChunkSpan],
    ranges: Sequence[tuple[int, int]],
    params: ChunkingParams,
) -> list[ChunkDraft]:
    starts = [span.start_offset for span in spans]
    drafts = []
    for ordinal, (start, end) in enumerate(ranges):
        first = spans[bisect.bisect_right(starts, start) - 1]
        last = spans[bisect.bisect_right(starts, end - 1) - 1]
        drafts.append(
            ChunkDraft(
                ordinal=ordinal,
                start_offset=start,
                end_offset=end,
                first_span_ordinal=first.ordinal,
                last_span_ordinal=last.ordinal,
                text=text[start:end],
                estimated_tokens=params.estimate(end - start),
            )
        )
    return drafts


def check_coverage(
    drafts: Sequence[ChunkDraft],
    text: str,
    spans: Sequence[ChunkSpan],
    params: ChunkingParams = SPAN_PACK_V1,
) -> None:
    """The whole-version contract, checked before anything is persisted."""
    if not drafts:
        raise ChunkingError("no chunks for a version with visible text")
    bounds = {s.ordinal: SpanBounds(s.ordinal, s.start_offset, s.end_offset) for s in spans}
    covered_until = 0
    previous: ChunkDraft | None = None
    for position, draft in enumerate(drafts):
        where = f"chunk {draft.ordinal}"
        if draft.ordinal != position:
            raise ChunkingError(f"{where}: ordinals are not 0..n-1")
        if previous is not None and not (
            draft.start_offset > previous.start_offset and draft.end_offset > previous.end_offset
        ):
            raise ChunkingError(f"{where}: offsets do not strictly increase")
        try:
            check_chunk_provenance(draft, text, bounds)
        except ChunkProvenanceError as exc:
            raise ChunkingError(str(exc)) from exc
        if draft.estimated_tokens != params.estimate(len(draft.text)):
            raise ChunkingError(f"{where}: estimated_tokens does not match its text")
        if draft.estimated_tokens > params.hard_max_estimated_tokens:
            raise ChunkingError(
                f"{where}: {draft.estimated_tokens} estimated tokens exceeds "
                f"{params.hard_max_estimated_tokens}"
            )
        if draft.text[0].isspace() or draft.text[-1].isspace():
            raise ChunkingError(f"{where}: starts or ends with whitespace")
        if draft.start_offset > covered_until and not _blank(
            text, covered_until, draft.start_offset
        ):
            raise ChunkingError(f"{where}: visible text before it is in no chunk")
        covered_until = max(covered_until, draft.end_offset)
        previous = draft
    if not _blank(text, covered_until, len(text)):
        raise ChunkingError("visible text after the last chunk is in no chunk")
    _check_span_coverage(drafts, text, spans)


def _check_span_coverage(
    drafts: Sequence[ChunkDraft], text: str, spans: Sequence[ChunkSpan]
) -> None:
    """Every span with visible text lies in some chunk's [first, last] range."""
    ranges = iter((d.first_span_ordinal, d.last_span_ordinal) for d in drafts)
    current = next(ranges)
    for span in spans:
        if _blank(text, span.start_offset, span.end_offset):
            continue
        while current[1] < span.ordinal:
            following = next(ranges, None)
            if following is None:
                raise ChunkingError(f"span {span.ordinal} is in no chunk")
            current = following
        if current[0] > span.ordinal:
            raise ChunkingError(f"span {span.ordinal} is in no chunk")


def _blank(text: str, start: int, end: int) -> bool:
    return start >= end or text[start:end].isspace()


def _check_input(text: str, spans: Sequence[ChunkSpan]) -> None:
    if not spans:
        raise ChunkingError("a version without spans cannot be chunked")
    previous: ChunkSpan | None = None
    for span in spans:
        if not 0 <= span.start_offset < span.end_offset <= len(text):
            raise ChunkingError(f"span {span.ordinal}: offsets outside the text")
        if previous is not None and not (
            span.ordinal > previous.ordinal and span.start_offset >= previous.end_offset
        ):
            raise ChunkingError(f"span {span.ordinal}: spans are not in source order")
        previous = span
