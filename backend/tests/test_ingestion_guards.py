# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Pre-parse safety guards and the ingestion error taxonomy.

Packages are built in memory; nothing here is committed as a fixture
(in particular, never the zip bomb).
"""

import dataclasses
import io
import zipfile

import pytest

from app.core.config import Settings
from app.domain.enums import IngestionErrorCategory as Category
from app.domain.enums import MediaType
from app.ingestion.errors import IngestionError
from app.ingestion.guards import (
    IngestionLimits,
    check_before_parse,
    check_extracted_chars,
    check_ooxml_package,
    check_pdf_pages,
    media_type_for,
)
from app.ingestion.schemas import RawSource

DOCX_MAIN = "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
PPTX_MAIN = "application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"
DOCM_MAIN = "application/vnd.ms-word.document.macroEnabled.main+xml"

LIMITS = IngestionLimits.from_settings(
    Settings(_env_file=None, database_url="postgresql+asyncpg://u:p@127.0.0.1/db")  # type: ignore[call-arg, arg-type]
)


UNSUPPORTED_DETAIL = (
    "file type is not supported; supported extensions: .docx, .htm, .html, .pdf, .pptx"
)


def _content_types(main_type: str) -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        f'<Override PartName="/main.xml" ContentType="{main_type}"/></Types>'
    ).encode()


def _package(main_type: str | None = DOCX_MAIN, extra: dict[str, bytes] | None = None) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        if main_type is not None:
            archive.writestr("[Content_Types].xml", _content_types(main_type))
        archive.writestr("word/document.xml", b"<w:document/>")
        for name, data in (extra or {}).items():
            archive.writestr(name, data)
    return buffer.getvalue()


def _source(content: bytes, name: str) -> RawSource:
    return RawSource(content=content, display_name=name, source_uri=f"upload:{name}")


def _category(excinfo: pytest.ExceptionInfo[IngestionError]) -> Category:
    return excinfo.value.category


# --- taxonomy -------------------------------------------------------------------


def test_taxonomy_values_are_stable() -> None:
    # Logged and used in evaluation: renaming one is a contract change.
    assert [c.value for c in Category] == [
        "unsupported_type",
        "corrupt_file",
        "empty_text",
        "limit_exceeded",
        "parser_failure",
    ]


def test_parser_failure_detail_keeps_only_the_exception_type() -> None:
    exc = ValueError("bad token in C:\\Users\\someone\\brief.docx: 'Student 12345 grade'")
    error = IngestionError.parser_failure(exc)

    assert error.category is Category.PARSER_FAILURE
    assert error.detail == "unexpected ValueError"
    assert "someone" not in str(error) and "12345" not in str(error)


def test_limits_come_from_settings() -> None:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        database_url="postgresql+asyncpg://u:p@127.0.0.1/db",  # type: ignore[arg-type]
        ingest_max_bytes=10,
        ingest_max_zip_compression_ratio=7.5,
    )
    limits = IngestionLimits.from_settings(settings)
    assert limits.max_bytes == 10
    assert limits.max_zip_compression_ratio == 7.5
    assert limits.max_pdf_pages == 500


# --- file type --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "media_type"),
    [
        ("brief.pdf", MediaType.PDF),
        ("BRIEF.PDF", MediaType.PDF),
        ("handbook.docx", MediaType.DOCX),
        ("week3.pptx", MediaType.PPTX),
        ("page.html", MediaType.HTML),
        ("page.HTM", MediaType.HTML),
        ("archive.tar.pdf", MediaType.PDF),
    ],
)
def test_supported_extensions(name: str, media_type: MediaType) -> None:
    assert media_type_for(name) is media_type


@pytest.mark.parametrize(
    "name",
    [
        "setup.exe",
        "old.doc",
        "old.ppt",
        "macro.docm",
        "macro.pptm",
        "sheet.xlsx",
        "notes.txt",
        "README",
        "pdf",
        ".pdf",  # a hidden file named ".pdf", not a PDF extension
        "brief.pdf.exe",
    ],
)
def test_unsupported_extensions(name: str) -> None:
    with pytest.raises(IngestionError) as excinfo:
        media_type_for(name)
    assert _category(excinfo) is Category.UNSUPPORTED_TYPE
    # The detail is fixed text: nothing from the file name is echoed.
    assert excinfo.value.detail == UNSUPPORTED_DETAIL


# --- size limit -------------------------------------------------------------------


def test_file_at_the_size_limit_passes_and_one_byte_over_is_limit_exceeded() -> None:
    limits = dataclasses.replace(LIMITS, max_bytes=100)
    assert check_before_parse(_source(b"x" * 100, "page.html"), limits) is MediaType.HTML

    with pytest.raises(IngestionError) as excinfo:
        check_before_parse(_source(b"x" * 101, "page.html"), limits)
    assert _category(excinfo) is Category.LIMIT_EXCEEDED
    assert "101 bytes" in excinfo.value.detail


def test_size_is_checked_before_type() -> None:
    # An oversized file is rejected by the limit even if its type is unsupported.
    limits = dataclasses.replace(LIMITS, max_bytes=10)
    with pytest.raises(IngestionError) as excinfo:
        check_before_parse(_source(b"MZ" * 100, "setup.exe"), limits)
    assert _category(excinfo) is Category.LIMIT_EXCEEDED


def test_default_size_limit_is_25_mib() -> None:
    assert LIMITS.max_bytes == 25 * 1024 * 1024


# --- DOCX / PPTX packages ---------------------------------------------------------


def test_valid_docx_and_pptx_packages_pass() -> None:
    assert check_before_parse(_source(_package(), "brief.docx"), LIMITS) is MediaType.DOCX
    pptx = _package(PPTX_MAIN)
    assert check_before_parse(_source(pptx, "week3.pptx"), LIMITS) is MediaType.PPTX


@pytest.mark.parametrize(
    "content",
    [
        b"",
        b"not a zip at all",
        b"%PDF-1.7\n...",  # a PDF renamed to .docx
        _package()[:40],  # truncated
    ],
)
def test_non_zip_bytes_are_corrupt_file(content: bytes) -> None:
    with pytest.raises(IngestionError) as excinfo:
        check_before_parse(_source(content, "brief.docx"), LIMITS)
    assert _category(excinfo) is Category.CORRUPT_FILE


def test_package_without_content_types_is_corrupt_file() -> None:
    with pytest.raises(IngestionError) as excinfo:
        check_ooxml_package(_package(main_type=None), MediaType.DOCX, LIMITS)
    assert _category(excinfo) is Category.CORRUPT_FILE


def test_pptx_renamed_to_docx_is_corrupt_file() -> None:
    with pytest.raises(IngestionError) as excinfo:
        check_ooxml_package(_package(PPTX_MAIN), MediaType.DOCX, LIMITS)
    assert _category(excinfo) is Category.CORRUPT_FILE
    assert "main document part" in excinfo.value.detail


def test_damaged_member_is_corrupt_file() -> None:
    package = bytearray(_package())
    # Corrupt the first member's compressed data (just after its local header).
    name_length = int.from_bytes(package[26:28], "little")
    data_start = 30 + name_length
    package[data_start : data_start + 8] = b"\xff" * 8

    with pytest.raises(IngestionError) as excinfo:
        check_ooxml_package(bytes(package), MediaType.DOCX, LIMITS)
    assert _category(excinfo) is Category.CORRUPT_FILE


@pytest.mark.parametrize(
    "package",
    [
        _package(DOCM_MAIN),  # macro-enabled content type renamed to .docx
        _package(extra={"word/vbaProject.bin": b"\x00" * 64}),  # macro project part
    ],
)
def test_macro_enabled_package_is_unsupported_type(package: bytes) -> None:
    with pytest.raises(IngestionError) as excinfo:
        check_before_parse(_source(package, "brief.docx"), LIMITS)
    assert _category(excinfo) is Category.UNSUPPORTED_TYPE


def test_too_many_members_is_limit_exceeded() -> None:
    limits = dataclasses.replace(LIMITS, max_zip_members=5)
    package = _package(extra={f"word/media/{i}.xml": b"<x/>" for i in range(5)})

    with pytest.raises(IngestionError) as excinfo:
        check_ooxml_package(package, MediaType.DOCX, limits)
    assert _category(excinfo) is Category.LIMIT_EXCEEDED
    assert "7 parts" in excinfo.value.detail


def test_total_uncompressed_size_over_limit_is_limit_exceeded() -> None:
    limits = dataclasses.replace(LIMITS, max_zip_uncompressed_bytes=1_000)
    package = _package(extra={"word/media/image.bin": bytes(range(256)) * 8})

    with pytest.raises(IngestionError) as excinfo:
        check_ooxml_package(package, MediaType.DOCX, limits)
    assert _category(excinfo) is Category.LIMIT_EXCEEDED
    assert "expands to" in excinfo.value.detail


def test_zip_bomb_is_rejected_by_compression_ratio_with_default_limits() -> None:
    # 20 MiB of zeros deflates to ~20 KiB (~1000:1): far below the 200 MiB
    # size cap, so only the ratio guard can stop it.
    bomb = _package(extra={"word/document2.xml": b"\x00" * (20 * 1024 * 1024)})
    assert len(bomb) < 100_000

    with pytest.raises(IngestionError) as excinfo:
        check_before_parse(_source(bomb, "brief.docx"), LIMITS)
    assert _category(excinfo) is Category.LIMIT_EXCEEDED
    assert "zip bomb" in excinfo.value.detail


def test_macro_check_runs_before_limits_without_decompressing() -> None:
    # A bomb that also carries a macro project is rejected by name, so the
    # oversized member is never read.
    limits = dataclasses.replace(LIMITS, max_zip_members=1)
    package = _package(extra={"word/vbaProject.bin": b"\x00"})
    with pytest.raises(IngestionError) as excinfo:
        check_ooxml_package(package, MediaType.DOCX, limits)
    assert _category(excinfo) is Category.UNSUPPORTED_TYPE


def test_html_and_pdf_skip_the_package_checks() -> None:
    # Not ZIP formats: their content is checked by the parsers.
    assert check_before_parse(_source(b"PK\x03\x04junk", "page.html"), LIMITS) is MediaType.HTML
    assert check_before_parse(_source(b"junk", "brief.pdf"), LIMITS) is MediaType.PDF


# --- parser-side limits -------------------------------------------------------------


def test_pdf_page_limit() -> None:
    check_pdf_pages(500, LIMITS)
    with pytest.raises(IngestionError) as excinfo:
        check_pdf_pages(501, LIMITS)
    assert _category(excinfo) is Category.LIMIT_EXCEEDED


def test_extracted_character_limit() -> None:
    check_extracted_chars(2_000_000, LIMITS)
    with pytest.raises(IngestionError) as excinfo:
        check_extracted_chars(2_000_001, LIMITS)
    assert _category(excinfo) is Category.LIMIT_EXCEEDED


def test_limit_details_do_not_contain_document_content() -> None:
    limits = dataclasses.replace(LIMITS, max_bytes=5)
    with pytest.raises(IngestionError) as excinfo:
        check_before_parse(_source(b"Student 12345 secret", "page.html"), limits)
    assert "12345" not in str(excinfo.value)
