# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""The chunk provenance contract, checked before any chunk is persisted.

The same rules are enforced again, row by row, by the ``chunks_provenance``
database trigger and the ``chunks`` CHECK constraints (migration 0004). This
check is the first line: a bad chunk fails here, with a clear message, before
reaching PostgreSQL. The trigger is the backstop for other writers and bugs.

Messages name offsets and ordinals only, never text.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Protocol


class ChunkProvenanceError(ValueError):
    """A chunk does not map exactly onto its version's text and spans."""


@dataclass(frozen=True, slots=True)
class SpanBounds:
    ordinal: int
    start_offset: int
    end_offset: int


class _SpanRow(Protocol):
    @property
    def ordinal(self) -> int: ...
    @property
    def start_offset(self) -> int | None: ...
    @property
    def end_offset(self) -> int | None: ...


def span_bounds(spans: Iterable[_SpanRow]) -> dict[int, SpanBounds]:
    """Span offsets by ordinal. A span without offsets cannot anchor a chunk."""
    bounds = {}
    for span in spans:
        if span.start_offset is None or span.end_offset is None:
            raise ChunkProvenanceError(f"span {span.ordinal} has no offsets")
        bounds[span.ordinal] = SpanBounds(span.ordinal, span.start_offset, span.end_offset)
    return bounds


@dataclass(frozen=True, slots=True)
class ChunkDraft:
    """A chunk before it is persisted (the ``chunks`` row minus ids)."""

    ordinal: int
    start_offset: int
    end_offset: int
    first_span_ordinal: int
    last_span_ordinal: int
    text: str
    estimated_tokens: int

    @property
    def text_hash(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()


def check_chunk_provenance(
    chunk: ChunkDraft, version_text: str, spans: Mapping[int, SpanBounds]
) -> None:
    """Raise ``ChunkProvenanceError`` unless ``chunk`` is an exact slice of
    ``version_text`` that starts inside its first span and ends inside its last."""
    where = f"chunk {chunk.ordinal}"
    if chunk.ordinal < 0:
        raise ChunkProvenanceError(f"{where}: ordinal is negative")
    if chunk.estimated_tokens <= 0:
        raise ChunkProvenanceError(f"{where}: estimated_tokens must be positive")
    if not 0 <= chunk.start_offset < chunk.end_offset:
        raise ChunkProvenanceError(
            f"{where}: offsets [{chunk.start_offset}, {chunk.end_offset}) are not ordered"
        )
    if chunk.first_span_ordinal > chunk.last_span_ordinal:
        raise ChunkProvenanceError(
            f"{where}: first span {chunk.first_span_ordinal} is after "
            f"last span {chunk.last_span_ordinal}"
        )
    first = _span(spans, chunk.first_span_ordinal, where)
    last = _span(spans, chunk.last_span_ordinal, where)
    if not first.start_offset <= chunk.start_offset < first.end_offset:
        raise ChunkProvenanceError(
            f"{where}: start {chunk.start_offset} is outside first span {first.ordinal} "
            f"[{first.start_offset}, {first.end_offset})"
        )
    if not last.start_offset < chunk.end_offset <= last.end_offset:
        raise ChunkProvenanceError(
            f"{where}: end {chunk.end_offset} is outside last span {last.ordinal} "
            f"[{last.start_offset}, {last.end_offset})"
        )
    if version_text[chunk.start_offset : chunk.end_offset] != chunk.text:
        raise ChunkProvenanceError(
            f"{where}: text is not the exact version slice "
            f"[{chunk.start_offset}, {chunk.end_offset})"
        )


def _span(spans: Mapping[int, SpanBounds], ordinal: int, where: str) -> SpanBounds:
    span = spans.get(ordinal)
    if span is None:
        raise ChunkProvenanceError(f"{where}: span {ordinal} is not a span of this version")
    return span
