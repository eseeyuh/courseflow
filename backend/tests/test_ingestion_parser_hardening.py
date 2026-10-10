# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Regression tests for parser edge cases found in review: tracked changes and
other run wrappers in DOCX, HTML visibility/nesting/encoding rules, legacy or
encrypted Office files, XML entity attacks and lying ZIP headers."""

import io
import struct
import time
import zipfile
from pathlib import Path

import pytest

from app.domain.enums import IngestionErrorCategory as Category
from app.ingestion.dispatch import parse_source
from app.ingestion.errors import IngestionError
from app.ingestion.parsers.html import MAX_NESTING_DEPTH
from app.ingestion.schemas import ParsedDocument
from tests.fixtures.ingestion.make_fixtures import FIXTURE_DIR
from tests.test_ingestion_parsers import LIMITS, _source

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
DATE = 'w:id="{}" w:author="Editor" w:date="2026-01-01T00:00:00Z"'


def _parse(content: bytes, name: str) -> ParsedDocument:
    return parse_source(_source(content, name), LIMITS)


def _error(content: bytes, name: str) -> IngestionError:
    with pytest.raises(IngestionError) as excinfo:
        _parse(content, name)
    return excinfo.value


def _docx_with_body(body: str, doctype: str = "") -> bytes:
    """The brief fixture with its body replaced (styles and parts kept)."""
    original = zipfile.ZipFile(FIXTURE_DIR / "brief.docx")
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as target:
        for info in original.infolist():
            data = original.read(info.filename)
            if info.filename == "word/document.xml":
                xml = data.decode("utf-8")
                head, rest = xml.split("<w:body>", 1)
                section = rest[rest.index("<w:sectPr") :]
                if doctype:
                    declaration, root = head.split("?>", 1)
                    head = f"{declaration}?>{doctype}{root}"
                data = f"{head}<w:body>{body}{section}".encode()
            target.writestr(info, data)
    return out.getvalue()


def _texts(document: ParsedDocument) -> list[str]:
    return [block.text for block in document.blocks]


# --- DOCX: text inside run wrappers ----------------------------------------------------


def test_docx_keeps_tracked_insertions_and_drops_tracked_deletions() -> None:
    body = (
        '<w:p><w:r><w:t xml:space="preserve">Submit by </w:t></w:r>'
        f"<w:ins {DATE.format(1)}><w:r><w:t>31 October 23:59</w:t></w:r></w:ins>"
        f"<w:del {DATE.format(2)}><w:r><w:delText>30 October 16:00</w:delText></w:r></w:del>"
        "</w:p>"
        f"<w:p><w:moveFrom {DATE.format(3)}><w:r><w:t>MOVED-AWAY</w:t></w:r></w:moveFrom>"
        f"<w:moveTo {DATE.format(4)}><w:r><w:t>MOVED-HERE</w:t></w:r></w:moveTo></w:p>"
    )
    document = _parse(_docx_with_body(body), "brief.docx")

    assert _texts(document) == ["Submit by 31 October 23:59", "MOVED-HERE"]
    assert "30 October" not in document.extracted_text
    assert "MOVED-AWAY" not in document.extracted_text


def test_docx_reads_smart_tags_fields_hyperlinks_and_content_controls() -> None:
    body = (
        '<w:p><w:smartTag w:uri="urn:x" w:element="date"><w:r><w:t>SMART-TAG</w:t></w:r>'
        "</w:smartTag></w:p>"
        '<w:p><w:fldSimple w:instr=" DATE "><w:r><w:t>FIELD-RESULT</w:t></w:r></w:fldSimple></w:p>'
        '<w:p><w:r><w:fldChar w:fldCharType="begin"/></w:r>'
        '<w:r><w:instrText xml:space="preserve"> PAGE </w:instrText></w:r>'
        '<w:r><w:fldChar w:fldCharType="separate"/></w:r><w:r><w:t>COMPLEX-FIELD</w:t></w:r>'
        '<w:r><w:fldChar w:fldCharType="end"/></w:r></w:p>'
        "<w:p><w:hyperlink><w:r><w:t>LINK-TEXT</w:t></w:r></w:hyperlink></w:p>"
        "<w:sdt><w:sdtContent><w:p><w:r><w:t>CONTENT-CONTROL</w:t></w:r></w:p>"
        "</w:sdtContent></w:sdt>"
        "<w:p><w:sdt><w:sdtContent><w:r><w:t>INLINE-CONTROL</w:t></w:r></w:sdtContent>"
        "</w:sdt></w:p>"
    )
    texts = _texts(_parse(_docx_with_body(body), "brief.docx"))

    assert texts == [
        "SMART-TAG",
        "FIELD-RESULT",
        "COMPLEX-FIELD",
        "LINK-TEXT",
        "CONTENT-CONTROL",
        "INLINE-CONTROL",
    ]
    assert not any("PAGE" in t for t in texts)  # field instructions are not text


def test_docx_tab_stops_and_text_boxes_are_not_run_text() -> None:
    body = (
        '<w:p><w:pPr><w:tabs><w:tab w:val="left" w:pos="720"/></w:tabs></w:pPr>'
        "<w:r><w:t>Before</w:t></w:r><w:r><w:tab/></w:r><w:r><w:t>after</w:t></w:r>"
        "<w:r><w:pict><w:txbxContent><w:p><w:r><w:t>TEXT-BOX</w:t></w:r></w:p>"
        "</w:txbxContent></w:pict></w:r></w:p>"
    )
    # A tab in a run is a space after normalisation; the tab-stop definition
    # and the text-box paragraph contribute nothing.
    assert _texts(_parse(_docx_with_body(body), "brief.docx")) == ["Before after"]


def test_docx_table_cells_also_keep_tracked_insertions() -> None:
    body = (
        "<w:tbl><w:tr><w:tc><w:p><w:r><w:t>Due</w:t></w:r></w:p></w:tc>"
        f"<w:tc><w:p><w:ins {DATE.format(5)}><w:r><w:t>31 October</w:t></w:r></w:ins></w:p></w:tc>"
        "</w:tr></w:tbl>"
    )
    assert _texts(_parse(_docx_with_body(body), "brief.docx")) == ["Due | 31 October"]


def test_docx_external_entities_are_never_resolved(tmp_path: Path) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("LOCAL-FILE-CONTENT", encoding="utf-8")
    doctype = f'<!DOCTYPE w:document [<!ENTITY xxe SYSTEM "{secret.as_uri()}">]>'
    content = _docx_with_body("<w:p><w:r><w:t>before &xxe; after</w:t></w:r></w:p>", doctype)

    try:
        document = _parse(content, "brief.docx")
    except IngestionError as error:
        assert error.category is Category.CORRUPT_FILE
    else:
        assert "LOCAL-FILE-CONTENT" not in document.extracted_text


def test_docx_entity_expansion_bomb_is_rejected_quickly() -> None:
    entities = '<!ENTITY lol "lol">' + "".join(
        f'<!ENTITY lol{i} "{f"&lol{i - 1};" * 10 if i > 1 else "&lol;" * 10}">'
        for i in range(1, 10)
    )
    content = _docx_with_body(
        "<w:p><w:r><w:t>&lol9;</w:t></w:r></w:p>", f"<!DOCTYPE w:document [{entities}]>"
    )

    started = time.monotonic()
    try:
        document = _parse(content, "brief.docx")
    except IngestionError as error:
        assert error.category is Category.CORRUPT_FILE
    else:
        assert len(document.extracted_text) < 1_000  # never expanded
    assert time.monotonic() - started < 5


def test_docx_member_that_understates_its_size_is_corrupt_file() -> None:
    """Central-directory sizes can lie; zipfile stops at the declared size and
    the CRC check then fails, so the parser rejects the file."""
    original = zipfile.ZipFile(FIXTURE_DIR / "brief.docx")
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as target:
        for info in original.infolist():
            data = original.read(info.filename)
            if info.filename == "word/document.xml":
                data += b" " * (5 * 1024 * 1024)  # 5 MiB of padding after the XML
            target.writestr(info, data)
    package = bytearray(out.getvalue())

    member = b"word/document.xml"
    declared = 4_000
    position = 0
    while (position := package.find(b"PK\x01\x02", position)) != -1:  # central directory
        name_length = struct.unpack_from("<H", package, position + 28)[0]
        if package[position + 46 : position + 46 + name_length] == member:
            struct.pack_into("<I", package, position + 24, declared)
            local = struct.unpack_from("<I", package, position + 42)[0]
            struct.pack_into("<I", package, local + 22, declared)  # local header
        position += 4

    error = _error(bytes(package), "brief.docx")
    assert error.category is Category.CORRUPT_FILE


@pytest.mark.parametrize("name", ["brief.docx", "deck.pptx"])
def test_legacy_or_encrypted_office_file_is_unsupported_type(name: str) -> None:
    ole = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 504
    error = _error(ole, name)
    assert error.category is Category.UNSUPPORTED_TYPE
    assert error.detail.startswith("encrypted or legacy Office container is unsupported")


# --- HTML -----------------------------------------------------------------------------


def test_html_title_is_discarded_even_without_a_head_element() -> None:
    page = b"<!doctype html><title>PAGE-TITLE</title><p>Body text</p>"
    assert _texts(_parse(page, "p.html")) == ["Body text"]


def test_html_hidden_table_rows_row_groups_and_captions_are_discarded() -> None:
    page = (
        b"<table><caption hidden>HIDDEN-CAPTION</caption>"
        b"<tbody hidden><tr><td>HIDDEN-GROUP</td></tr></tbody>"
        b"<tbody><tr hidden><td>HIDDEN-ROW</td></tr><tr><td>shown</td><td>row</td></tr>"
        b"</tbody></table>"
    )
    assert _texts(_parse(page, "p.html")) == ["shown | row"]


def test_html_nesting_within_the_limit_parses() -> None:
    depth = MAX_NESTING_DEPTH - 10
    page = ("<div>" * depth + "deep text" + "</div>" * depth).encode()
    assert _texts(_parse(page, "p.html")) == ["deep text"]


@pytest.mark.parametrize("depth", [MAX_NESTING_DEPTH + 10, 5_000])
def test_html_nesting_beyond_the_limit_is_limit_exceeded(depth: int) -> None:
    page = ("<div>" * depth + "deep text" + "</div>" * depth).encode()
    error = _error(page, "p.html")
    assert error.category is Category.LIMIT_EXCEEDED
    assert error.detail == f"HTML nesting is deeper than {MAX_NESTING_DEPTH} elements"


@pytest.mark.parametrize("label", ["unicode_escape", "rot13", "base64", "utf-7"])
def test_html_non_web_declared_encodings_are_refused(label: str) -> None:
    page = f'<meta charset="{label}"><p>caf'.encode() + b"\xe9</p>"
    assert _error(page, "p.html").category is Category.CORRUPT_FILE


def test_html_latin1_label_decodes_like_a_browser_as_windows_1252() -> None:
    page = b'<meta charset="iso-8859-1"><p>\x93Quoted\x94 caf\xe9</p>'
    assert _texts(_parse(page, "p.html")) == ["“Quoted” café"]
