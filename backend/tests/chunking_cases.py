# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Deterministic chunker inputs: hand-built layouts, the golden document and
seeded random layouts. Importable on its own, so a subprocess can rebuild the
same inputs."""

import json
import random
from collections.abc import Sequence

from app.domain.enums import BlockKind
from app.retrieval.chunking import ChunkSpan
from app.retrieval.provenance import ChunkDraft

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


def serialize(drafts: Sequence[ChunkDraft]) -> str:
    return json.dumps(
        [
            [
                d.ordinal,
                d.start_offset,
                d.end_offset,
                d.first_span_ordinal,
                d.last_span_ordinal,
                d.estimated_tokens,
                d.text_hash,
            ]
            for d in drafts
        ]
    )


# --- the golden document for span-pack/v1 ----------------------------------------------

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


# --- seeded random layouts ---------------------------------------------------------------

_WORDS = [
    *("recursion", "base", "case", "linked", "list", "deadline", "coursework"),
    *("lecture", "evidence", "span", "x", "café", "naïve", "\U0001f600", "—"),
]


def _text(rng: random.Random, chars: int, *, heading: bool = False) -> str:
    """Normalised-looking block text of about ``chars`` characters."""
    if heading:
        return " ".join(rng.choice(_WORDS) for _ in range(rng.randint(1, 2)))
    style = rng.random()
    if style < 0.08:  # no whitespace at all (a URL, an encoded blob)
        return "".join(rng.choice("abcdef0123456789") for _ in range(max(1, chars)))
    parts: list[str] = []
    size = 0
    while size < chars:
        word = rng.choice(_WORDS)
        roll = rng.random()
        if roll < 0.08:
            word += "."
        separator = " "
        if parts and roll > 0.97:
            separator = "\n\n"
        elif parts and roll > 0.93:
            separator = "\n"
        parts.append((separator if parts else "") + word)
        size += len(parts[-1])
    text = "".join(parts)
    if rng.random() < 0.05:
        text = "\u00a0" + text  # NBSP survives normalisation
    if rng.random() < 0.05:
        text += "\u00a0"
    return text


def random_layout(rng: random.Random, max_estimated_tokens: int) -> tuple[str, list[ChunkSpan]]:
    """A random version in one format, with sizes from tiny to several times
    ``max_estimated_tokens``."""
    style = rng.choice(["pdf", "docx", "pptx"])
    blocks: list[Block] = []
    slide = 1
    for _ in range(rng.randint(1, 18)):
        roll = rng.random()
        tokens = (
            rng.randint(1, 5)
            if roll < 0.3
            else rng.randint(5, max_estimated_tokens)
            if roll < 0.85
            else rng.randint(max_estimated_tokens, 4 * max_estimated_tokens)
        )
        chars = tokens * 4
        if rng.random() < 0.03:
            blocks.append((P if style != "pptx" else BlockKind.SLIDE_TEXT, "\u00a0", slide))
            continue
        if style == "pdf":
            blocks.append((BlockKind.PAGE, _text(rng, chars), None))
        elif style == "docx":
            if rng.random() < 0.3:
                blocks.append((H, _text(rng, 0, heading=True), None))
            else:
                blocks.append((rng.choice([P, P, T]), _text(rng, chars), None))
        else:
            if rng.random() < 0.45:
                slide += 1
            elif rng.random() < 0.05:
                slide = max(1, slide - 2)  # a slide number seen before, not contiguous
            kind = BlockKind.SLIDE_NOTES if rng.random() < 0.3 else BlockKind.SLIDE_TEXT
            blocks.append((kind, _text(rng, chars), slide))
    if all(excerpt == "\u00a0" for _, excerpt, _ in blocks):
        blocks.append((BlockKind.PAGE, "visible", None))
    return layout(blocks)
