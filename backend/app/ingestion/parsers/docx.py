# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""DOCX: one block per body paragraph or table, in document order.

Headings are paragraphs styled "Title" (level 0) or "Heading N"; they form
the section path of everything after them. Text before the first heading
belongs to the preamble. DOCX has no stable page numbers.

Not extracted in v0.1: headers and footers, footnotes and endnotes,
comments, text boxes and content controls.
"""

import io
import re
import zipfile

from docx import Document
from docx.document import Document as DocxDocument
from docx.opc.exceptions import PackageNotFoundError
from docx.table import Table, _Cell
from docx.text.paragraph import Paragraph
from lxml.etree import XMLSyntaxError

from app.domain.enums import BlockKind
from app.ingestion.errors import IngestionError
from app.ingestion.guards import IngestionLimits
from app.ingestion.normalize import normalize_text
from app.ingestion.parsers.common import (
    HeadingTrail,
    Parser,
    ParserOutput,
    make_block,
    parser_version,
)
from app.ingestion.schemas import ParsedBlock

_HEADING_STYLE = re.compile(r"^Heading ([1-9])$")
_CELL_SEPARATOR = " | "
_OPEN_ERRORS = (PackageNotFoundError, KeyError, zipfile.BadZipFile, XMLSyntaxError, ValueError)


def parse_docx(content: bytes, limits: IngestionLimits) -> ParserOutput:
    try:
        document = Document(io.BytesIO(content))
    except _OPEN_ERRORS:
        raise IngestionError.corrupt_file("not a readable DOCX document") from None
    return ParserOutput(_body_blocks(document))


def _body_blocks(document: DocxDocument) -> list[ParsedBlock]:
    trail = HeadingTrail()
    blocks: list[ParsedBlock] = []
    for item in document.iter_inner_content():
        if isinstance(item, Table):
            block = make_block(BlockKind.TABLE, _table_text(item), section=trail.path)
        else:
            level = _heading_level(item)
            if level is None:
                block = make_block(BlockKind.PARAGRAPH, item.text, section=trail.path)
            else:
                title = normalize_text(item.text)
                if not title:
                    continue  # an empty heading opens no section
                block = make_block(BlockKind.HEADING, title, section=trail.enter(level, title))
        if block is not None:
            blocks.append(block)
    return blocks


def _heading_level(paragraph: Paragraph) -> int | None:
    style = paragraph.style
    name = style.name if style is not None else None
    if name == "Title":
        return 0
    match = _HEADING_STYLE.match(name or "")
    return int(match.group(1)) if match else None


def _table_text(table: Table) -> str:
    """Rows on separate lines, cells joined by " | ". A merged cell is
    reported once, where it starts."""
    seen: set[object] = set()  # cell elements; holding them keeps identity stable
    lines = []
    for row in table.rows:
        cells = []
        for cell in row.cells:
            if cell._tc in seen:
                continue
            seen.add(cell._tc)
            cells.append(_cell_text(cell))
        if any(cells):  # a row of empty cells is not text
            lines.append(_CELL_SEPARATOR.join(cells))
    return "\n".join(lines)


def _cell_text(cell: _Cell) -> str:
    """Paragraphs and nested tables of one cell, flattened to a single line."""
    parts = []
    for item in cell.iter_inner_content():
        text = _table_text(item) if isinstance(item, Table) else item.text
        parts.append(" ".join(text.split()))
    return " ".join(part for part in parts if part)


DOCX_PARSER = Parser(
    name="courseflow-docx",
    version=parser_version(1, "python-docx"),
    parse=parse_docx,
    empty_reason="document contains no extractable text",
)
