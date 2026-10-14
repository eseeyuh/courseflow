# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""span-pack/v1: the designed cases, real fixtures, seeded random layouts and
pinned golden output. Pure: no database, no model."""

import dataclasses
import hashlib
import itertools
import json
import os
import random
import subprocess
import sys
from pathlib import Path

import pytest

from app.core.config import Settings
from app.domain.enums import BlockKind
from app.ingestion.dispatch import parse_source
from app.ingestion.guards import IngestionLimits
from app.ingestion.schemas import RawSource
from app.retrieval.chunking import (
    SPAN_PACK_V1,
    ChunkingError,
    ChunkingParams,
    ChunkSpan,
    build_units,
    check_coverage,
    chunk_spans,
    chunk_version,
    estimate_tokens,
    group_units,
)
from app.retrieval.provenance import ChunkDraft
from tests.chunking_cases import (
    SEPARATOR,
    golden_document,
    layout,
    random_layout,
    sentences,
    serialize,
)

H, P, T, PAGE = BlockKind.HEADING, BlockKind.PARAGRAPH, BlockKind.TABLE, BlockKind.PAGE
TEXT, NOTES = BlockKind.SLIDE_TEXT, BlockKind.SLIDE_NOTES

# Scaled-down parameters (estimated tokens) so small texts cross every boundary.
SMALL = ChunkingParams(
    version="test/small",
    target_estimated_tokens=40,
    hard_max_estimated_tokens=50,
    overlap_estimated_tokens=6,
    split_above_estimated_tokens=44,
    min_estimated_tokens=10,
    split_piece_min_estimated_tokens=20,
)
# The worked example from the design (overlap of 16 characters).
TOY = ChunkingParams(
    version="test/toy",
    target_estimated_tokens=12,
    hard_max_estimated_tokens=17,
    overlap_estimated_tokens=4,
    split_above_estimated_tokens=13,
    min_estimated_tokens=4,
    split_piece_min_estimated_tokens=6,
)


def _prose(tokens: int, label: str = "w") -> str:
    """About ``tokens`` estimated tokens of words, sentences included."""
    words, size, index = [], 0, 0
    while size < tokens * 4 - 1:
        word = f"{label}{index}" + ("." if index % 6 == 5 else "")
        words.append(word)
        size += len(word) + 1
        index += 1
    return " ".join(words)[: tokens * 4].rstrip(" .") + "."


def _ranges(drafts: list[ChunkDraft]) -> list[tuple[int, int]]:
    return [(d.start_offset, d.end_offset) for d in drafts]


def _span_of(spans: list[ChunkSpan], position: int) -> ChunkSpan:
    return next(s for s in spans if s.start_offset <= position < s.end_offset)


# --- parameters ----------------------------------------------------------------------------


def test_span_pack_v1_parameters_are_pinned() -> None:
    """Changing any of these requires a new chunker version."""
    assert dataclasses.asdict(SPAN_PACK_V1) == {
        "version": "span-pack/v1",
        "target_estimated_tokens": 800,
        "hard_max_estimated_tokens": 1000,
        "overlap_estimated_tokens": 120,
        "split_above_estimated_tokens": 880,
        "min_estimated_tokens": 200,
        "split_piece_min_estimated_tokens": 400,
        "chars_per_estimated_token": 4,
    }


@pytest.mark.parametrize(("length", "expected"), [(0, 0), (1, 1), (4, 1), (5, 2), (4000, 1000)])
def test_estimate_is_ceil_of_characters_over_four(length: int, expected: int) -> None:
    assert estimate_tokens("x" * length) == expected


@pytest.mark.parametrize(
    "change",
    [
        {"split_above_estimated_tokens": 900},  # 900 + 120 overlap > hard max
        {"target_estimated_tokens": 900},  # target above split_above
        {"min_estimated_tokens": 0},
        {"split_piece_min_estimated_tokens": 800},
    ],
)
def test_inconsistent_parameters_are_rejected(change: dict[str, int]) -> None:
    with pytest.raises(ValueError, match="inconsistent"):
        dataclasses.replace(SPAN_PACK_V1, **change)


# --- the worked example (case 9: exact offsets) ---------------------------------------------

EXAMPLE = [
    (PAGE, "Week 3: recursion and base cases.", None),
    (PAGE, "Lab 2 covers linked lists.", None),
    (PAGE, "Coursework 1 is due on 31 October.", None),
]


def test_worked_example_with_real_parameters_is_one_slice_with_its_separators() -> None:
    text, spans = layout(EXAMPLE)

    (chunk,) = chunk_version(text, spans)

    assert (chunk.start_offset, chunk.end_offset) == (0, 97)
    assert (chunk.first_span_ordinal, chunk.last_span_ordinal) == (0, 2)
    assert chunk.text == text[0:97]
    assert chunk.text.count(SEPARATOR) == 2  # already in extracted_text, never synthesised


def test_worked_example_offsets_with_overlap() -> None:
    text, spans = layout(EXAMPLE)

    drafts = chunk_version(text, spans, TOY)

    assert [
        (d.start_offset, d.end_offset, d.first_span_ordinal, d.last_span_ordinal) for d in drafts
    ] == [(0, 33, 0, 0), (18, 61, 0, 1), (48, 97, 1, 2)]
    assert [d.text for d in drafts] == [
        "Week 3: recursion and base cases.",
        "and base cases.\n\nLab 2 covers linked lists.",
        "linked lists.\n\nCoursework 1 is due on 31 October.",
    ]
    assert [d.estimated_tokens for d in drafts] == [9, 11, 13]


# --- case 1: packing adjacent spans; case 7: overlap ----------------------------------------


def test_adjacent_spans_are_packed_whole_with_overlap_at_word_starts() -> None:
    text, spans = layout([(P, _prose(n, f"p{i}-"), None) for i, n in enumerate([15, 12, 10, 14])])

    drafts = chunk_version(text, spans, SMALL)

    assert len(drafts) >= 2
    span_ends = {s.end_offset for s in spans}
    for previous, chunk in itertools.pairwise(drafts):
        assert previous.end_offset in span_ends  # whole spans, never cut
        assert previous.start_offset < chunk.start_offset < previous.end_offset  # overlap
        assert previous.end_offset - chunk.start_offset <= 6 * 4
        assert text[chunk.start_offset - 1].isspace()  # starts at a word
        assert chunk.text == text[chunk.start_offset : chunk.end_offset]


# --- case 2: heading-forward -----------------------------------------------------------------


def test_heading_starts_the_next_chunk_without_overlap() -> None:
    text, spans = layout([(P, _prose(35), None), (H, "Marking", None), (P, _prose(20), None)])

    drafts = chunk_version(text, spans, SMALL)

    assert _ranges(drafts) == [
        (spans[0].start_offset, spans[0].end_offset),
        (spans[1].start_offset, spans[2].end_offset),
    ]


def test_consecutive_headings_attach_together_to_the_following_content() -> None:
    text, spans = layout(
        [(P, _prose(35), None), (H, "Methods", None), (H, "Data", None), (P, _prose(20), None)]
    )

    drafts = chunk_version(text, spans, SMALL)

    assert drafts[-1].start_offset == spans[1].start_offset
    assert (drafts[-1].first_span_ordinal, drafts[-1].last_span_ordinal) == (1, 3)
    assert all(_span_of(spans, d.end_offset - 1).block_kind is not H for d in drafts)


def test_heading_chain_larger_than_the_hard_max_leaves_a_heading_only_chunk() -> None:
    """Accepted span-pack/v1 edge: the group (two headings + a 44-token paragraph)
    estimates 51 > 50, so it cannot stay whole. The headings that do not fit form
    their own chunk; the next chunk overlaps back to the nearest heading that fits.
    Provenance, coverage and the hard maximum all still hold."""
    text, spans = layout(
        [(H, "Heading one", None), (H, "A second heading", None), (P, _prose(43, "b"), None)]
    )

    drafts = chunk_version(text, spans, SMALL)

    assert [
        (d.start_offset, d.end_offset, d.first_span_ordinal, d.last_span_ordinal) for d in drafts
    ] == [(0, 29, 0, 1), (13, 204, 1, 2)]
    assert drafts[0].text == "Heading one\n\nA second heading"
    assert drafts[1].text.startswith("A second heading\n\n")


def test_a_final_heading_with_nothing_after_it_attaches_backward() -> None:
    text, spans = layout([(P, _prose(10), None), (H, "Appendix", None)])

    (chunk,) = chunk_version(text, spans, SMALL)

    assert chunk.last_span_ordinal == 1


# --- case 3: slide text and notes -------------------------------------------------------------


def test_a_slides_text_and_notes_stay_together_and_new_slides_get_no_overlap() -> None:
    text, spans = layout(
        [
            (TEXT, _prose(8, "a"), 1),
            (TEXT, _prose(8, "b"), 1),
            (NOTES, _prose(12, "c"), 1),
            (TEXT, _prose(15, "d"), 2),
            (NOTES, _prose(12, "e"), 2),
            (TEXT, _prose(5, "f"), 3),
        ]
    )

    drafts = chunk_version(text, spans, SMALL)

    assert _ranges(drafts) == [
        (spans[0].start_offset, spans[2].end_offset),
        (spans[3].start_offset, spans[5].end_offset),
    ]


def test_an_oversized_slide_is_split_with_overlap_inside_the_same_slide() -> None:
    text, spans = layout(
        [(TEXT, _prose(6, "t"), 1), (NOTES, _prose(120, "n"), 1), (TEXT, _prose(5, "x"), 2)]
    )

    drafts = chunk_version(text, spans, SMALL)

    inside_slide_1 = [d for d in drafts if d.last_span_ordinal <= 1]
    assert len(inside_slide_1) >= 3
    overlaps = [
        (a, b) for a, b in itertools.pairwise(inside_slide_1) if b.start_offset < a.end_offset
    ]
    assert overlaps  # text flow within one slide may overlap
    slide_2 = next(d for d in drafts if d.last_span_ordinal == 2)
    assert slide_2.start_offset >= spans[1].start_offset  # never repeats slide 1 text...
    assert slide_2.start_offset == spans[2].start_offset or slide_2.first_span_ordinal == 1


def test_repeated_slide_numbers_are_grouped_only_when_contiguous() -> None:
    """Slide 1 appears again after slide 2: it is a separate group, kept in
    source order, never merged with the earlier slide 1 text."""
    text, spans = layout(
        [(TEXT, _prose(30, "a"), 1), (TEXT, _prose(30, "b"), 2), (NOTES, _prose(5, "c"), 1)]
    )

    groups = group_units(build_units(text, spans, SMALL))
    drafts = chunk_version(text, spans, SMALL)

    assert [[unit.span.ordinal for unit in group] for group in groups] == [[0], [1], [2]]
    assert _ranges(drafts) == [
        (spans[0].start_offset, spans[0].end_offset),
        # Slide 1's notes follow slide 2 in the source and stay there; no overlap
        # because they begin a different slide from the last new material.
        (spans[1].start_offset, spans[2].end_offset),
    ]


# --- case 4: page boundaries ---------------------------------------------------------------


def test_page_boundaries_do_not_suppress_overlap() -> None:
    text, spans = layout([(PAGE, _prose(30, "a"), None), (PAGE, _prose(30, "b"), None)])

    first, second = chunk_version(text, spans, SMALL)

    assert first.last_span_ordinal == 0
    assert second.first_span_ordinal == 0  # starts inside page 1 ...
    assert second.start_offset < spans[0].end_offset  # ... as overlap across the page break


# --- case 5: very small spans ----------------------------------------------------------------


def test_many_tiny_spans_are_packed_together() -> None:
    text, spans = layout([(P, f"Item {i}.", None) for i in range(60)])

    drafts = chunk_version(text, spans, SMALL)

    assert len(drafts) < 20
    assert sum(d.last_span_ordinal - d.first_span_ordinal + 1 for d in drafts) >= 60


def test_a_single_tiny_span_is_one_chunk() -> None:
    text, spans = layout([(P, "Room 3.14", None)])

    assert _ranges(chunk_version(text, spans, SMALL)) == [(0, 9)]


def test_a_small_final_chunk_merges_into_the_previous_one_when_it_fits() -> None:
    text, spans = layout([(P, _prose(38, "a"), None), (P, "Done now.", None)])

    (chunk,) = chunk_version(text, spans, SMALL)

    assert (chunk.start_offset, chunk.end_offset) == (0, len(text))


def test_a_small_final_chunk_stays_separate_when_merging_would_exceed_the_hard_max() -> None:
    # 176 chars = 44 estimated tokens (not split); the heading-led tail gets no
    # overlap, and merging would give ceil(205 / 4) = 52 > 50.
    text, spans = layout(
        [(P, "x" * 176, None), (H, "End", None), (P, "Done, really done now.", None)]
    )

    drafts = chunk_version(text, spans, SMALL)

    assert _ranges(drafts) == [(0, 176), (178, 205)]
    assert drafts[-1].estimated_tokens < SMALL.min_estimated_tokens


# --- case 6: oversized single spans; case 8: overlap inside them -----------------------------


def test_an_oversized_span_is_cut_at_its_paragraph_breaks_with_overlap_inside() -> None:
    excerpt = SEPARATOR.join(_prose(12, f"q{i}-") for i in range(12))
    text, spans = layout([(PAGE, excerpt, None)])

    drafts = chunk_version(text, spans, SMALL)

    assert len(drafts) > 3
    for chunk in drafts:
        assert chunk.first_span_ordinal == chunk.last_span_ordinal == 0
        assert text[chunk.end_offset : chunk.end_offset + 2] in {SEPARATOR, ""}
    for previous, chunk in itertools.pairwise(drafts):
        assert previous.start_offset < chunk.start_offset < previous.end_offset


def _pieces(excerpt: str) -> list[str]:
    text, spans = layout([(P, excerpt, None)])
    return [text[u.start : u.end] for u in build_units(text, spans, SMALL)]


def test_cut_prefers_a_line_break_over_a_sentence_end() -> None:
    lines = "\n".join(f"line {i} has a sentence. and more words here" for i in range(12))

    pieces = _pieces(lines)

    assert len(pieces) > 1
    assert all(piece.endswith("more words here") for piece in pieces[:-1])
    assert "\n".join(pieces) == lines  # each cut consumed exactly one line break


def test_cut_prefers_a_sentence_end_over_a_plain_space() -> None:
    prose = " ".join(f"Point {i} is made in five words." for i in range(30))

    assert all(piece.endswith(".") for piece in _pieces(prose)[:-1])


def test_cut_falls_back_to_a_space_and_never_splits_a_word() -> None:
    words = " ".join(f"word{i:03d}" for i in range(80))

    pieces = _pieces(words)

    assert all(text in words.split(" ") for piece in pieces for text in piece.split(" "))


def test_long_text_without_whitespace_is_hard_cut_into_contiguous_pieces() -> None:
    blob = "".join("0123456789abcdef"[i % 16] for i in range(1000))

    pieces = _pieces(blob)

    assert "".join(pieces) == blob  # no gaps: nothing is lost
    assert all(len(piece) == 40 * 4 for piece in pieces[:-1])
    drafts = chunk_version(*layout([(P, blob, None)]), SMALL)
    assert all(d.estimated_tokens <= SMALL.hard_max_estimated_tokens for d in drafts)


def test_a_cut_uses_early_whitespace_before_splitting_a_word() -> None:
    excerpt = "see: " + "x" * 1000

    assert _pieces(excerpt)[0] == "see:"


def test_whitespace_only_split_gaps_stay_uncovered_and_pass_the_coverage_check() -> None:
    excerpt = SEPARATOR.join(_prose(30, f"g{i}-") for i in range(4))
    text, spans = layout([(PAGE, excerpt, None)])
    no_overlap = dataclasses.replace(SMALL, version="test/no-overlap", overlap_estimated_tokens=0)

    drafts = chunk_version(text, spans, no_overlap)

    gaps = [text[a.end_offset : b.start_offset] for a, b in itertools.pairwise(drafts)]
    assert gaps and all(gap == SEPARATOR for gap in gaps)


# --- Unicode, whitespace-edged and whitespace-only spans -------------------------------------


def test_offsets_are_code_points_through_emoji_and_combining_characters() -> None:
    excerpt = " ".join(["café \U0001f600 naïve — résumé."] * 40)
    text, spans = layout([(P, "Intro \U0001f4da.", None), (PAGE, excerpt, None)])

    drafts = chunk_version(text, spans, SMALL)

    assert len(drafts) > 2
    for chunk in drafts:
        assert chunk.text == text[chunk.start_offset : chunk.end_offset]
        assert len(chunk.text) == chunk.end_offset - chunk.start_offset
        assert chunk.text_hash == hashlib.sha256(chunk.text.encode("utf-8")).hexdigest()


def test_chunks_never_start_or_end_with_nbsp_and_blank_spans_need_no_chunk() -> None:
    text, spans = layout(
        [(P, "\u00a0", None), (P, "\u00a0Hello there.\u00a0", None), (P, "\u00a0", None)]
    )

    (chunk,) = chunk_version(text, spans, SMALL)

    assert chunk.text == "Hello there."
    assert (chunk.first_span_ordinal, chunk.last_span_ordinal) == (1, 1)


# --- invalid input and the coverage check ----------------------------------------------------


@pytest.mark.parametrize(
    ("spans", "message"),
    [
        ([], "without spans"),
        ([ChunkSpan(0, P, None, 0, 99)], "outside the text"),
        ([ChunkSpan(0, P, None, 5, 5)], "outside the text"),
        ([ChunkSpan(1, P, None, 0, 4), ChunkSpan(0, P, None, 6, 9)], "source order"),
        ([ChunkSpan(0, P, None, 0, 6), ChunkSpan(1, P, None, 4, 9)], "source order"),
    ],
)
def test_invalid_spans_are_refused(spans: list[ChunkSpan], message: str) -> None:
    with pytest.raises(ChunkingError, match=message):
        chunk_version("abcd\n\nefghij", spans, SMALL)


def test_a_version_with_only_blank_spans_is_an_error_not_zero_chunks() -> None:
    with pytest.raises(ChunkingError, match="no span has visible text"):
        chunk_version(*layout([(P, "\u00a0", None)]), SMALL)


def test_spans_without_offsets_cannot_be_chunked() -> None:
    row = dataclasses.make_dataclass(
        "Row", ["ordinal", "block_kind", "slide_number", "start_offset", "end_offset"]
    )(0, P, None, None, None)

    with pytest.raises(ChunkingError, match="no offsets"):
        chunk_spans([row])


def _good() -> tuple[str, list[ChunkSpan], list[ChunkDraft]]:
    text, spans = layout(EXAMPLE)
    return text, spans, chunk_version(text, spans, TOY)


def _mutate(drafts: list[ChunkDraft], index: int, **fields: object) -> list[ChunkDraft]:
    changed = list(drafts)
    changed[index] = dataclasses.replace(changed[index], **fields)  # type: ignore[arg-type]
    return changed


def test_coverage_rejects_visible_text_in_no_chunk() -> None:
    text, spans, drafts = _good()
    gap = [drafts[0], dataclasses.replace(drafts[2], ordinal=1)]

    with pytest.raises(ChunkingError, match="visible text before it"):
        check_coverage(gap, text, spans, TOY)


@pytest.mark.parametrize(
    ("index", "fields", "message"),
    [
        (1, {"ordinal": 5}, "ordinals"),
        (1, {"estimated_tokens": 3}, "does not match"),
        (
            2,
            {
                "start_offset": 55,
                "text": "lists.\n\nCoursework 1 is due on 31 October.",
                "estimated_tokens": 11,
            },
            None,
        ),
    ],
)
def test_coverage_rejects_broken_drafts(
    index: int, fields: dict[str, object], message: str | None
) -> None:
    text, spans, drafts = _good()
    changed = _mutate(drafts, index, **fields)
    if message is None:  # a valid alternative chunk: accepted
        check_coverage(changed, text, spans, TOY)
        return

    with pytest.raises(ChunkingError, match=message):
        check_coverage(changed, text, spans, TOY)


def test_coverage_rejects_non_increasing_offsets_and_oversized_chunks() -> None:
    text, spans, drafts = _good()

    with pytest.raises(ChunkingError, match="strictly increase"):
        check_coverage([drafts[0], dataclasses.replace(drafts[0], ordinal=1)], text, spans, TOY)
    with pytest.raises(ChunkingError, match="exceeds"):
        check_coverage(chunk_version(text, spans), text, spans, TOY)  # 25 > 17


def test_coverage_rejects_bad_provenance_and_no_chunks() -> None:
    text, spans, drafts = _good()

    with pytest.raises(ChunkingError, match="exact version slice"):
        check_coverage(_mutate(drafts, 0, text="X" + drafts[0].text[1:]), text, spans, TOY)
    with pytest.raises(ChunkingError, match="no chunks"):
        check_coverage([], text, spans, TOY)


# --- ingestion fixtures through the real parsers --------------------------------------------------

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "ingestion"
LIMITS = IngestionLimits.from_settings(
    Settings(_env_file=None, database_url="postgresql+asyncpg://u:p@127.0.0.1/db")  # type: ignore[call-arg, arg-type]
)


def _fixture(name: str) -> tuple[str, list[ChunkSpan]]:
    source = RawSource(
        content=(FIXTURE_DIR / name).read_bytes(), display_name=name, source_uri=f"upload:{name}"
    )
    document = parse_source(source, LIMITS)
    spans = [
        ChunkSpan(ordinal, block.kind, block.locator.slide_number, start, end)
        for ordinal, (block, (start, end)) in enumerate(
            zip(document.blocks, document.block_offsets(), strict=True)
        )
    ]
    return document.extracted_text, spans


# (start, end, first span, last span, estimated tokens)
FIXTURE_GOLDEN_V1 = {
    "handbook.pdf": [(0, 244, 0, 2, 61)],
    "brief.docx": [(0, 578, 0, 11, 145)],
    "lecture.pptx": [(0, 420, 0, 9, 105)],
    "module-page.html": [(0, 457, 0, 13, 115)],
}
FIXTURE_GOLDEN_SMALL = {
    # Overlap flows across page breaks.
    "handbook.pdf": [(0, 71, 0, 0), (65, 195, 0, 1), (174, 244, 1, 2)],
    # Chunks after a break start exactly at a heading; none ends with one.
    "brief.docx": [(0, 135, 0, 2), (137, 297, 3, 6), (288, 415, 6, 7), (417, 548, 8, 10),
                   (536, 578, 10, 11)],
    # Whole slides, text with notes, and no overlap across slides.
    "lecture.pptx": [(0, 64, 0, 1), (66, 213, 2, 4), (215, 365, 5, 7), (367, 420, 8, 9)],
    "module-page.html": [(0, 140, 0, 3), (142, 237, 4, 7), (239, 398, 8, 11), (384, 457, 11, 13)],
}  # fmt: skip


@pytest.mark.parametrize("name", FIXTURE_GOLDEN_V1)
def test_ingestion_fixtures_with_span_pack_v1(name: str) -> None:
    drafts = chunk_version(*_fixture(name))

    assert [
        (
            d.start_offset,
            d.end_offset,
            d.first_span_ordinal,
            d.last_span_ordinal,
            d.estimated_tokens,
        )
        for d in drafts
    ] == FIXTURE_GOLDEN_V1[name]


@pytest.mark.parametrize("name", FIXTURE_GOLDEN_SMALL)
def test_ingestion_fixtures_cross_real_boundaries_with_small_parameters(name: str) -> None:
    text, spans = _fixture(name)

    drafts = chunk_version(text, spans, SMALL)

    assert [
        (d.start_offset, d.end_offset, d.first_span_ordinal, d.last_span_ordinal) for d in drafts
    ] == FIXTURE_GOLDEN_SMALL[name]
    _assert_properties(text, spans, drafts, SMALL)


# --- seeded random layouts ---------------------------------------------------------------------


def _assert_properties(
    text: str, spans: list[ChunkSpan], drafts: list[ChunkDraft], params: ChunkingParams
) -> None:
    """Beyond check_coverage (already run inside chunk_version)."""
    assert chunk_version(text, spans, params) == drafts  # deterministic
    units = build_units(text, spans, params)
    # Every keep-together group (slide text + notes, headings + what follows)
    # that fits the hard maximum lies inside one chunk.
    for group in group_units(units):
        start, end = group[0].start, group[-1].end
        if params.estimate(end - start) <= params.hard_max_estimated_tokens:
            assert any(d.start_offset <= start and end <= d.end_offset for d in drafts)
    for previous, chunk in itertools.pairwise(drafts):
        first_new = next(u for u in units if u.start >= previous.end_offset)
        if chunk.start_offset < previous.end_offset:  # overlap
            last_old = [u for u in units if u.end <= previous.end_offset][-1]
            assert not first_new.is_heading
            assert first_new.slide is None or first_new.slide == last_old.slide
            assert previous.end_offset - chunk.start_offset <= params.chars(
                params.overlap_estimated_tokens
            )
        else:
            assert chunk.start_offset == first_new.start


@pytest.mark.parametrize("seed", range(300))
def test_random_layouts_with_small_parameters(seed: int) -> None:
    text, spans = random_layout(random.Random(seed), SMALL.hard_max_estimated_tokens)

    _assert_properties(text, spans, chunk_version(text, spans, SMALL), SMALL)


@pytest.mark.parametrize("seed", range(30))
def test_random_layouts_with_span_pack_v1(seed: int) -> None:
    text, spans = random_layout(random.Random(10_000 + seed), 1000)

    _assert_properties(text, spans, chunk_version(text, spans), SPAN_PACK_V1)


# --- golden output for span-pack/v1 --------------------------------------------------------------

GOLDEN_V1 = [
    (0, 3154, 0, 2, 789),
    (2705, 5014, 2, 6, 578),
    (4555, 7366, 6, 7, 703),
    (6897, 9718, 7, 7, 706),
    (9249, 12070, 7, 7, 706),
    (11601, 14422, 7, 7, 706),
    (13953, 16774, 7, 7, 706),
    (16305, 19126, 7, 7, 706),
    (18657, 20311, 7, 10, 414),
]
GOLDEN_V1_SHA256 = "b4ab7b0e300491597ab49a44892fb944ecddb1453e55f7b59f39733505f5a0b6"


def test_golden_document_offsets_are_pinned() -> None:
    """A change here means span-pack/v1 changed: bump the chunker version."""
    drafts = chunk_version(*golden_document())

    assert [
        (
            d.start_offset,
            d.end_offset,
            d.first_span_ordinal,
            d.last_span_ordinal,
            d.estimated_tokens,
        )
        for d in drafts
    ] == GOLDEN_V1
    assert hashlib.sha256(serialize(drafts).encode()).hexdigest() == GOLDEN_V1_SHA256


def test_golden_document_keeps_its_structure() -> None:
    text, spans = golden_document()
    drafts = chunk_version(text, spans)

    # The heading chain Methods > Data collection stays with its paragraph.
    assert any(d.first_span_ordinal <= 4 and d.last_span_ordinal >= 6 for d in drafts)
    # The oversized paragraph is split, inside itself, at its paragraph breaks.
    inside = [d for d in drafts if d.first_span_ordinal == d.last_span_ordinal == 7]
    assert len(inside) >= 3
    assert all(text[d.end_offset : d.end_offset + 2] == SEPARATOR for d in inside)
    assert sentences(0, 1) in drafts[0].text


_SUBPROCESS = (
    "from tests.chunking_cases import golden_document, serialize\n"
    "from app.retrieval.chunking import chunk_version\n"
    "print(serialize(chunk_version(*golden_document())))\n"
)


@pytest.mark.parametrize("seed", ["0", "1", "4242"])
def test_golden_output_is_identical_in_a_separate_process(seed: str) -> None:
    backend = Path(__file__).resolve().parents[1]

    completed = subprocess.run(
        [sys.executable, "-c", _SUBPROCESS],
        cwd=backend,
        env={**os.environ, "PYTHONHASHSEED": seed, "PYTHONPATH": str(backend)},
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )

    in_process = serialize(chunk_version(*golden_document()))
    assert json.loads(completed.stdout) == json.loads(in_process)
