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
from app.retrieval.samples import (  # noqa: F401  (re-exported for the tests)
    SEPARATOR,
    Block,
    golden_document,
    layout,
    sentences,
)

H, P, T = BlockKind.HEADING, BlockKind.PARAGRAPH, BlockKind.TABLE


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
