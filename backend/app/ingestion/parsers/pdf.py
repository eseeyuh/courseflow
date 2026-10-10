# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""PDF: one block per physical page, from the PDF's text layer (PDFium).

No OCR in v0.1. A page without a text layer (a scanned image) yields no
block; a document with no text on any page is `empty_text`.
"""

import threading

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_raw

from app.domain.enums import BlockKind
from app.ingestion.errors import IngestionError
from app.ingestion.guards import IngestionLimits, check_pdf_pages
from app.ingestion.parsers.common import Parser, make_block, parser_version
from app.ingestion.schemas import ParsedBlock

# PDFium is not thread-safe: one document is processed at a time per process.
_PDFIUM_LOCK = threading.Lock()
_ENCRYPTED = {pdfium_raw.FPDF_ERR_PASSWORD, pdfium_raw.FPDF_ERR_SECURITY}


def parse_pdf(content: bytes, limits: IngestionLimits) -> list[ParsedBlock]:
    with _PDFIUM_LOCK:
        try:
            document = pdfium.PdfDocument(content)
        except pdfium.PdfiumError as exc:
            if exc.err_code in _ENCRYPTED:
                raise IngestionError.corrupt_file("PDF is password-protected") from None
            raise IngestionError.corrupt_file("not a readable PDF") from None

        try:
            page_count = len(document)
            check_pdf_pages(page_count, limits)
            blocks: list[ParsedBlock] = []
            for index in range(page_count):
                block = make_block(
                    BlockKind.PAGE, _page_text(document, index), page_number=index + 1
                )
                if block is not None:
                    blocks.append(block)
            return blocks
        finally:
            document.close()


def _page_text(document: pdfium.PdfDocument, index: int) -> str:
    page = document[index]
    try:
        text_page = page.get_textpage()
        try:
            return text_page.get_text_range()
        finally:
            text_page.close()
    finally:
        page.close()


PDF_PARSER = Parser(
    name="courseflow-pdf",
    version=parser_version(1, "pypdfium2"),
    parse=parse_pdf,
    empty_reason="no text layer; OCR unsupported in v0.1",
)
