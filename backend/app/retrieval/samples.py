# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Synthetic sample documents for chunking: built exactly as ingestion lays
text out, with invented text only. Used to measure the chunker and to pin its
output in tests (the golden document)."""

from collections.abc import Sequence

from app.domain.enums import BlockKind
from app.retrieval.chunking import ChunkSpan

SEPARATOR = "\n\n"  # what ingestion puts between span excerpts
Block = tuple[BlockKind, str, int | None]  # kind, excerpt, slide_number


def layout(blocks: Sequence[Block]) -> tuple[str, list[ChunkSpan]]:
    """Extracted text and spans exactly as ingestion lays them out."""
    text, spans, offset = "", [], 0
    for ordinal, (kind, excerpt, slide) in enumerate(blocks):
        if ordinal:
            text += SEPARATOR
            offset += len(SEPARATOR)
        spans.append(ChunkSpan(ordinal, kind, slide, offset, offset + len(excerpt)))
        text += excerpt
        offset += len(excerpt)
    return text, spans


def sentences(first: int, count: int) -> str:
    return " ".join(
        f"Sentence {i} explains topic {i % 7} in plain words." for i in range(first, first + count)
    )


H, P, T = BlockKind.HEADING, BlockKind.PARAGRAPH, BlockKind.TABLE


def golden_document() -> tuple[str, list[ChunkSpan]]:
    """Headings, flowing paragraphs, a heading chain, an oversized paragraph
    with internal paragraph breaks, Unicode, a table and a final heading."""
    oversized = SEPARATOR.join(sentences(1000 + 25 * part, 25) for part in range(12))
    return layout(
        [
            (H, "Introduction", None),
            (P, sentences(0, 40), None),
            (P, sentences(40, 30), None),
            (P, "Café résumé \U0001f600 naïve — " + sentences(70, 20), None),
            (H, "Methods", None),
            (H, "Data collection", None),
            (P, sentences(90, 20), None),
            (P, oversized, None),
            (T, "\n".join(f"Week {w} | Topic {w % 5} | Lab {w}" for w in range(1, 40)), None),
            (P, sentences(400, 3), None),
            (H, "Appendix", None),
        ]
    )
