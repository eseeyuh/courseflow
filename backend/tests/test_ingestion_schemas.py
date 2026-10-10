# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

from typing import Any

import pytest
from pydantic import ValidationError

from app.domain.enums import BlockKind, MediaType
from app.ingestion.hashing import sha256_hex, text_hash
from app.ingestion.schemas import (
    PREAMBLE,
    ParsedBlock,
    ParsedDocument,
    RawSource,
    SpanLocator,
    section_path,
)

HASH = sha256_hex(b"original bytes")


def _block(text: str, **locator: Any) -> ParsedBlock:
    return ParsedBlock(kind=BlockKind.PAGE, locator=SpanLocator(**locator), text=text)


def _document(*blocks: ParsedBlock) -> ParsedDocument:
    return ParsedDocument(
        media_type=MediaType.DOCX,
        content_hash=HASH,
        byte_size=14,
        parser_name="test-parser",
        parser_version="1",
        blocks=blocks,
    )


# --- RawSource ---------------------------------------------------------------


def test_raw_source_accepts_bare_name_and_logical_uri() -> None:
    source = RawSource(
        content=b"%PDF-", display_name="Week 3 brief.pdf", source_uri="upload:Week 3 brief.pdf"
    )
    assert source.display_name == "Week 3 brief.pdf"


@pytest.mark.parametrize(
    "name",
    [
        "../secret.pdf",
        "notes/brief.pdf",
        "..\\brief.pdf",
        "C:\\Users\\someone\\brief.pdf",
        "/etc/passwd",
        "..",
        ".",
        " brief.pdf",
        "brief\x00.pdf",
        "brief\n.pdf",
        "",
        "a" * 256,
    ],
)
def test_raw_source_rejects_paths_and_unsafe_display_names(name: str) -> None:
    with pytest.raises(ValidationError, match="display_name"):
        RawSource(content=b"x", display_name=name, source_uri="upload:x")


@pytest.mark.parametrize(
    "uri",
    [
        "C:\\Users\\someone\\brief.pdf",
        "c:/Users/someone/brief.pdf",
        "/home/someone/brief.pdf",
        "file:///home/someone/brief.pdf",
        "brief.pdf",
        "upload:",
        "upload: brief.pdf",
        "upload:a\\b.pdf",
        "upload:brief\n.pdf",
        "",
    ],
)
def test_raw_source_rejects_local_paths_as_source_uri(uri: str) -> None:
    with pytest.raises(ValidationError, match="source_uri"):
        RawSource(content=b"x", display_name="x.pdf", source_uri=uri)


@pytest.mark.parametrize("uri", ["upload:brief.pdf", "demo://northbridge/cs101/brief"])
def test_raw_source_accepts_logical_uris(uri: str) -> None:
    assert RawSource(content=b"x", display_name="x.pdf", source_uri=uri).source_uri == uri


def test_raw_source_content_must_be_bytes_not_str() -> None:
    with pytest.raises(ValidationError, match="content"):
        RawSource(content="text", display_name="x.html", source_uri="upload:x")  # type: ignore[arg-type]


def test_raw_source_repr_and_errors_do_not_expose_content_or_paths() -> None:
    source = RawSource(content=b"CONFIDENTIAL-BYTES", display_name="x.pdf", source_uri="upload:x")
    assert "CONFIDENTIAL-BYTES" not in repr(source)

    with pytest.raises(ValidationError) as excinfo:
        RawSource(content=b"x", display_name="C:\\Users\\someone\\x.pdf", source_uri="upload:x")
    assert "someone" not in str(excinfo.value)


def test_raw_source_is_immutable() -> None:
    source = RawSource(content=b"x", display_name="x.pdf", source_uri="upload:x")
    with pytest.raises(ValidationError):
        source.display_name = "y.pdf"  # type: ignore[misc]


# --- SpanLocator and section paths ------------------------------------------


@pytest.mark.parametrize(
    "locator",
    [{"page_number": 1}, {"slide_number": 7}, {"section_path": "Brief > Submission"}],
)
def test_locator_accepts_any_single_locator(locator: dict[str, Any]) -> None:
    SpanLocator(**locator)


def test_locator_requires_at_least_one_field() -> None:
    with pytest.raises(ValidationError, match="page number, slide number or section path"):
        SpanLocator()


@pytest.mark.parametrize(
    ("locator", "field"),
    [
        ({"page_number": 0}, "page_number"),
        ({"slide_number": 0}, "slide_number"),
        ({"section_path": "  "}, "section_path"),
    ],
)
def test_locator_rejects_values_the_database_would_reject(
    locator: dict[str, Any], field: str
) -> None:
    with pytest.raises(ValidationError, match=field):
        SpanLocator(**locator)


def test_section_path_joins_headings_and_marks_preamble() -> None:
    assert section_path(["Assessment Brief", "Submission", "Late penalties"]) == (
        "Assessment Brief > Submission > Late penalties"
    )
    assert section_path([]) == PREAMBLE == "(preamble)"


# --- ParsedBlock --------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "locator"),
    [
        (BlockKind.PAGE, {"page_number": 2}),
        (BlockKind.HEADING, {"section_path": "Brief"}),
        (BlockKind.PARAGRAPH, {"section_path": PREAMBLE}),
        (BlockKind.TABLE, {"section_path": "Brief > Marking"}),
        (BlockKind.SLIDE_TEXT, {"slide_number": 3}),
        (BlockKind.SLIDE_NOTES, {"slide_number": 3}),
    ],
)
def test_block_kind_with_its_required_locator(kind: BlockKind, locator: dict[str, Any]) -> None:
    ParsedBlock(kind=kind, locator=SpanLocator(**locator), text="Text")


@pytest.mark.parametrize(
    ("kind", "locator"),
    [
        (BlockKind.PAGE, {"section_path": "Brief"}),
        (BlockKind.PARAGRAPH, {"page_number": 1}),
        (BlockKind.SLIDE_NOTES, {"page_number": 1}),
    ],
)
def test_block_rejects_a_locator_that_does_not_fit_its_kind(
    kind: BlockKind, locator: dict[str, Any]
) -> None:
    with pytest.raises(ValidationError, match=f"{kind.value} block needs"):
        ParsedBlock(kind=kind, locator=SpanLocator(**locator), text="Text")


@pytest.mark.parametrize("text", ["", "   ", " padded", "two  spaces", "a\r\nb", "nul\x00"])
def test_block_text_must_already_be_normalised(text: str) -> None:
    with pytest.raises(ValidationError, match="text"):
        _block(text, page_number=1)


def test_block_validation_error_does_not_echo_document_text() -> None:
    with pytest.raises(ValidationError) as excinfo:
        _block("Student 12345  private  note", page_number=1)
    assert "12345" not in str(excinfo.value)


# --- ParsedDocument -----------------------------------------------------------


def test_document_joins_blocks_with_a_blank_line() -> None:
    document = _document(_block("First", page_number=1), _block("Second", page_number=2))
    assert document.extracted_text == "First\n\nSecond"


def test_block_offsets_slice_back_to_each_block_exactly() -> None:
    document = _document(
        _block("Week 3\n\nLecture notes", page_number=1),  # contains the separator itself
        _block("Deadline: 31 Oct 23:59", page_number=2),
        _block("End", page_number=3),
    )
    text = document.extracted_text
    offsets = document.block_offsets()

    assert [text[start:end] for start, end in offsets] == [b.text for b in document.blocks]
    assert offsets[0] == (0, len("Week 3\n\nLecture notes"))


def test_offsets_count_unicode_code_points_not_bytes_or_utf16_units() -> None:
    # "📄" is 1 code point, 4 UTF-8 bytes, 2 UTF-16 units; "é" is 1 code point.
    document = _document(_block("📄 Caf\u00e9", page_number=1), _block("Next", page_number=2))
    assert document.block_offsets() == ((0, 6), (8, 12))
    assert document.extracted_text[8:12] == "Next"


def test_text_hash_is_sha256_of_extracted_text_and_differs_from_content_hash() -> None:
    document = _document(_block("First", page_number=1))
    assert document.text_hash == text_hash("First") == sha256_hex(b"First")
    assert document.text_hash != document.content_hash


def test_document_requires_at_least_one_block() -> None:
    with pytest.raises(ValidationError, match="blocks"):
        _document()


@pytest.mark.parametrize("bad_hash", ["ABC", HASH.upper(), HASH[:-1], "sha256:" + HASH])
def test_document_content_hash_must_be_lowercase_sha256_hex(bad_hash: str) -> None:
    with pytest.raises(ValidationError, match="content_hash"):
        ParsedDocument(
            media_type=MediaType.PDF,
            content_hash=bad_hash,
            byte_size=1,
            parser_name="p",
            parser_version="1",
            blocks=(_block("x", page_number=1),),
        )


def test_sha256_hex_matches_known_vector() -> None:
    assert sha256_hex(b"abc") == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
