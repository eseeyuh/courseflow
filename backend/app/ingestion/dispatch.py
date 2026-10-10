# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Turn one RawSource into one ParsedDocument, or one IngestionError.

guards (size, type, package) -> parser for the media type -> non-empty
check -> extracted-character limit -> ParsedDocument. Pure: no database,
no storage, no model calls.
"""

from collections.abc import Mapping

from app.domain.enums import MediaType
from app.ingestion.errors import IngestionError
from app.ingestion.guards import IngestionLimits, check_before_parse, check_extracted_chars
from app.ingestion.hashing import sha256_hex
from app.ingestion.parsers.common import Parser
from app.ingestion.parsers.docx import DOCX_PARSER
from app.ingestion.parsers.html import HTML_PARSER
from app.ingestion.parsers.pdf import PDF_PARSER
from app.ingestion.parsers.pptx import PPTX_PARSER
from app.ingestion.schemas import ParsedDocument, RawSource

PARSERS: Mapping[MediaType, Parser] = {
    MediaType.PDF: PDF_PARSER,
    MediaType.DOCX: DOCX_PARSER,
    MediaType.PPTX: PPTX_PARSER,
    MediaType.HTML: HTML_PARSER,
}


def parse_source(source: RawSource, limits: IngestionLimits) -> ParsedDocument:
    """Parse ``source`` deterministically. Raises ``IngestionError`` only."""
    media_type = check_before_parse(source, limits)
    parser = PARSERS[media_type]
    try:
        blocks = parser.parse(source.content, limits)
    except IngestionError:
        raise
    except Exception as exc:  # noqa: BLE001 - every other failure is a parser_failure
        # Not chained: the original message may quote document text or a path.
        raise IngestionError.parser_failure(exc) from None

    if not blocks:
        raise IngestionError.empty_text(parser.empty_reason)

    document = ParsedDocument(
        media_type=media_type,
        content_hash=sha256_hex(source.content),
        byte_size=len(source.content),
        parser_name=parser.name,
        parser_version=parser.version,
        blocks=tuple(blocks),
    )
    check_extracted_chars(len(document.extracted_text), limits)
    return document
