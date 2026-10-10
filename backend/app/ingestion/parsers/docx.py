# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""DOCX: one block per body paragraph or table, in document order.

Headings are paragraphs styled "Title" (level 0) or "Heading N"; they form
the section path of everything after them. Text before the first heading
belongs to the preamble. DOCX has no stable page numbers.

Paragraph text is read from every run in the paragraph, so tracked
insertions, hyperlinks, smart tags and field results are included, while
deleted and moved-away text (tracked changes) is not. Paragraphs and tables
inside content controls are read like any other body content.

Not extracted in v0.1: headers and footers, footnotes and endnotes,
comments and text boxes.
"""

import io
import re
import zipfile
from collections.abc import Iterator

from docx import Document
from docx.document import Document as DocxDocument
from docx.opc.exceptions import PackageNotFoundError
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph
from lxml.etree import XMLSyntaxError, _Element

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

_P, _TBL, _R = qn("w:p"), qn("w:tbl"), qn("w:r")
_T, _TAB, _BR, _CR = qn("w:t"), qn("w:tab"), qn("w:br"), qn("w:cr")
# Block-level wrappers whose content is ordinary body content.
_SDT, _SDT_CONTENT, _CUSTOM_XML = qn("w:sdt"), qn("w:sdtContent"), qn("w:customXml")
# Text under these is not part of the current document text.
_REMOVED = frozenset({qn("w:del"), qn("w:moveFrom")})
# Paragraphs of a text box are not part of the anchoring paragraph (not extracted in v0.1).
_TEXT_BOX = qn("w:txbxContent")


def parse_docx(content: bytes, limits: IngestionLimits) -> ParserOutput:
    try:
        document = Document(io.BytesIO(content))
    except _OPEN_ERRORS:
        raise IngestionError.corrupt_file("not a readable DOCX document") from None
    return ParserOutput(_body_blocks(document))


def _body_blocks(document: DocxDocument) -> list[ParsedBlock]:
    trail = HeadingTrail()
    blocks: list[ParsedBlock] = []
    for element in _block_elements(document.element.body):
        if element.tag == _TBL:
            text = _table_text(Table(element, document))  # type: ignore[arg-type]
            block = make_block(BlockKind.TABLE, text, section=trail.path)
        else:
            text = paragraph_text(element)
            level = _heading_level(Paragraph(element, document))  # type: ignore[arg-type]
            if level is None:
                block = make_block(BlockKind.PARAGRAPH, text, section=trail.path)
            else:
                title = normalize_text(text)
                if not title:
                    continue  # an empty heading opens no section
                block = make_block(BlockKind.HEADING, title, section=trail.enter(level, title))
        if block is not None:
            blocks.append(block)
    return blocks


def _block_elements(container: _Element) -> Iterator[_Element]:
    """Paragraph and table elements in document order, looking inside content
    controls and custom XML wrappers."""
    for child in container.iterchildren():
        if child.tag in (_P, _TBL):
            yield child
        elif child.tag == _SDT:
            content = child.find(_SDT_CONTENT)
            if content is not None:
                yield from _block_elements(content)
        elif child.tag == _CUSTOM_XML:
            yield from _block_elements(child)


def paragraph_text(paragraph: _Element) -> str:
    """The current text of one paragraph element: every run's text, tabs and
    line breaks, excluding deleted/moved-away text and text-box content."""
    parts: list[str] = []
    for node in paragraph.iter(_T, _TAB, _BR, _CR):
        if node.getparent().tag != _R or _excluded(node, paragraph):
            continue  # e.g. tab-stop definitions in paragraph properties
        if node.tag == _T:
            parts.append(node.text or "")
        elif node.tag == _TAB:
            parts.append("\t")
        else:
            parts.append("\n")
    return "".join(parts)


def _excluded(node: _Element, paragraph: _Element) -> bool:
    ancestor = node.getparent()
    while ancestor is not None and ancestor is not paragraph:
        if ancestor.tag in _REMOVED or ancestor.tag == _TEXT_BOX:
            return True
        ancestor = ancestor.getparent()
    return False


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
            cells.append(_cell_text(cell._tc, table))
        if any(cells):  # a row of empty cells is not text
            lines.append(_CELL_SEPARATOR.join(cells))
    return "\n".join(lines)


def _cell_text(cell: _Element, table: Table) -> str:
    """Paragraphs and nested tables of one cell, flattened to a single line."""
    parts = []
    for element in _block_elements(cell):
        if element.tag == _TBL:
            text = _table_text(Table(element, table._parent))  # type: ignore[arg-type]
        else:
            text = paragraph_text(element)
        parts.append(" ".join(text.split()))
    return " ".join(part for part in parts if part)


DOCX_PARSER = Parser(
    name="courseflow-docx",
    version=parser_version(1, "python-docx"),
    parse=parse_docx,
    empty_reason="document contains no extractable text",
)
