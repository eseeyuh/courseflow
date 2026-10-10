# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Safety checks that run before (and around) parsing.

Order matters and is part of the contract:

1. byte size                      -> limit_exceeded
2. extension allowlist            -> unsupported_type
3. DOCX/PPTX package, inspected from the ZIP directory only:
   OLE file (legacy or encrypted) -> unsupported_type
   not a ZIP                      -> corrupt_file
   macro project present          -> unsupported_type
   members / uncompressed size /
   compression ratio              -> limit_exceeded
   macro-enabled or wrong content
   type (read only after limits)  -> unsupported_type / corrupt_file

The PDF page count is checked by the PDF parser after PDFium opens the file,
and the extracted-character limit by dispatch.parse_source after parsing,
both through the helpers below. Damaged package members are found by the
parsers (corrupt_file).

These are bounded defensive checks against the expected zip-bomb and
resource-exhaustion cases, not a guarantee against every malformed input.
Isolating native parsers from hostile files (separate process, time
limits) is deferred to the threat model.
"""

import io
import posixpath
import zipfile
import zlib
from dataclasses import dataclass
from typing import Self

from app.core.config import Settings
from app.domain.enums import MediaType
from app.ingestion.errors import IngestionError
from app.ingestion.schemas import RawSource

_MEDIA_TYPE_BY_EXTENSION: dict[str, MediaType] = {
    ".pdf": MediaType.PDF,
    ".docx": MediaType.DOCX,
    ".pptx": MediaType.PPTX,
    ".html": MediaType.HTML,
    ".htm": MediaType.HTML,
}
_SUPPORTED_EXTENSIONS = ", ".join(sorted(_MEDIA_TYPE_BY_EXTENSION))

_OOXML_CONTENT_TYPES = "[Content_Types].xml"
# Signature of an OLE compound file: legacy Office binaries and encrypted OOXML.
_OLE_COMPOUND_FILE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
# The package's main part content type, as listed in [Content_Types].xml.
_OOXML_MAIN_CONTENT_TYPE: dict[MediaType, bytes] = {
    MediaType.DOCX: (
        b"application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
    ),
    MediaType.PPTX: (
        b"application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"
    ),
}
# Errors zipfile/zlib raise for damaged, truncated or encrypted members.
_ZIP_READ_ERRORS = (
    zipfile.BadZipFile,
    zlib.error,
    EOFError,
    OSError,
    ValueError,
    NotImplementedError,
    RuntimeError,
)


@dataclass(frozen=True, slots=True)
class IngestionLimits:
    max_bytes: int
    max_pdf_pages: int
    max_extracted_chars: int
    max_zip_members: int
    max_zip_uncompressed_bytes: int
    max_zip_compression_ratio: float

    @classmethod
    def from_settings(cls, settings: Settings) -> Self:
        return cls(
            max_bytes=settings.ingest_max_bytes,
            max_pdf_pages=settings.ingest_max_pdf_pages,
            max_extracted_chars=settings.ingest_max_extracted_chars,
            max_zip_members=settings.ingest_max_zip_members,
            max_zip_uncompressed_bytes=settings.ingest_max_zip_uncompressed_bytes,
            max_zip_compression_ratio=settings.ingest_max_zip_compression_ratio,
        )


def check_before_parse(source: RawSource, limits: IngestionLimits) -> MediaType:
    """Run every pre-parse guard in contract order; return the detected media type."""
    check_size(len(source.content), limits)
    media_type = media_type_for(source.display_name)
    if media_type in _OOXML_MAIN_CONTENT_TYPE:
        check_ooxml_package(source.content, media_type, limits)
    return media_type


def check_size(byte_size: int, limits: IngestionLimits) -> None:
    if byte_size > limits.max_bytes:
        raise IngestionError.limit_exceeded(
            f"file is {byte_size} bytes; the limit is {limits.max_bytes}"
        )


def media_type_for(display_name: str) -> MediaType:
    """Media type from the file extension (v0.1 does not sniff content)."""
    # posixpath: identical behaviour on every OS; display_name is never a path.
    extension = posixpath.splitext(display_name)[1].lower()
    media_type = _MEDIA_TYPE_BY_EXTENSION.get(extension)
    if media_type is None:
        # The extension itself is not echoed: it comes from the input.
        raise IngestionError.unsupported_type(
            f"file type is not supported; supported extensions: {_SUPPORTED_EXTENSIONS}"
        )
    return media_type


def check_ooxml_package(content: bytes, media_type: MediaType, limits: IngestionLimits) -> None:
    """Defensive checks on a DOCX/PPTX package before any parser opens it.

    Limits member count, declared uncompressed size and compression ratio,
    and reads only the central directory and ``[Content_Types].xml``. Sizes
    come from the central directory and can understate the real data;
    CPython's zipfile stops at a member's declared size and then fails its CRC
    check, so such a member is rejected as corrupt_file when the parser reads
    it (test_understated_member_size_is_rejected).
    """
    label = "DOCX" if media_type is MediaType.DOCX else "PPTX"
    if content.startswith(_OLE_COMPOUND_FILE):
        # Legacy .doc/.ppt, or an encrypted Office file: not an OOXML package.
        raise IngestionError.unsupported_type(
            f"encrypted or legacy Office container is unsupported; save it as an unprotected "
            f".{label.lower()}"
        )
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except _ZIP_READ_ERRORS:
        raise IngestionError.corrupt_file(f"not a readable {label} package") from None

    with archive:
        members = archive.infolist()
        # Macro projects are rejected by name, before anything is decompressed.
        if any(posixpath.basename(m.filename).lower() == "vbaproject.bin" for m in members):
            raise IngestionError.unsupported_type("macro-enabled documents are not supported")

        if len(members) > limits.max_zip_members:
            raise IngestionError.limit_exceeded(
                f"{label} package has {len(members)} parts; the limit is {limits.max_zip_members}"
            )
        uncompressed = sum(m.file_size for m in members)
        if uncompressed > limits.max_zip_uncompressed_bytes:
            raise IngestionError.limit_exceeded(
                f"{label} package expands to {uncompressed} bytes; "
                f"the limit is {limits.max_zip_uncompressed_bytes}"
            )
        compressed = max(sum(m.compress_size for m in members), 1)
        if uncompressed > compressed * limits.max_zip_compression_ratio:
            raise IngestionError.limit_exceeded(
                f"{label} package compression ratio exceeds "
                f"{limits.max_zip_compression_ratio:g}:1 (possible zip bomb)"
            )

        try:
            content_types = archive.read(_OOXML_CONTENT_TYPES).lower()
        except KeyError:
            raise IngestionError.corrupt_file(
                f"not a {label} package: {_OOXML_CONTENT_TYPES} is missing"
            ) from None
        except _ZIP_READ_ERRORS:
            raise IngestionError.corrupt_file(f"{label} package is damaged") from None

    # Byte search, not XML parsing: no entity expansion on untrusted XML.
    if b"macroenabled" in content_types:
        raise IngestionError.unsupported_type("macro-enabled documents are not supported")
    if _OOXML_MAIN_CONTENT_TYPE[media_type] not in content_types:
        raise IngestionError.corrupt_file(f"not a {label} package: main document part not found")


def check_pdf_pages(page_count: int, limits: IngestionLimits) -> None:
    if page_count > limits.max_pdf_pages:
        raise IngestionError.limit_exceeded(
            f"PDF has {page_count} pages; the limit is {limits.max_pdf_pages}"
        )


def check_extracted_chars(char_count: int, limits: IngestionLimits) -> None:
    if char_count > limits.max_extracted_chars:
        raise IngestionError.limit_exceeded(
            f"extracted text has {char_count} characters; the limit is {limits.max_extracted_chars}"
        )
