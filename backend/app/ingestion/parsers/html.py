# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Static HTML (e.g. a Moodle-like module page): heading-aware text blocks.

h1-h6 are headings and form the section path; text before the first
heading belongs to the preamble. Every block-level element (p, li, div,
...) starts a new paragraph block; a table is one block. Whitespace follows
HTML rendering rules: runs of whitespace are one space, except inside
<pre>; <br> is a line break.

The page is treated as inert data. Nothing is fetched or executed. Script,
style, template, noscript, embedded frames and objects, SVG/MathML, the
<head> (including <title>), comments and elements with the ``hidden``
attribute are discarded. Image alt text is not extracted in v0.1.

Decoding is deterministic, never guessed: UTF-8 (with or without BOM),
otherwise the charset the page declares, otherwise `corrupt_file`.
"""

import re

from bs4 import BeautifulSoup, NavigableString, Tag
from bs4.dammit import EncodingDetector

from app.domain.enums import BlockKind
from app.ingestion.errors import IngestionError
from app.ingestion.guards import IngestionLimits
from app.ingestion.normalize import normalize_text
from app.ingestion.parsers.common import HeadingTrail, Parser, make_block, parser_version
from app.ingestion.schemas import ParsedBlock

# Named explicitly: parsers differ on malformed markup, so the choice is part
# of the output contract. html.parser is in the standard library.
_TREE_BUILDER = "html.parser"

_DISCARDED = frozenset(
    {"head", "script", "style", "template", "noscript", "iframe", "object", "embed", "svg", "math"}
)
_HEADING_LEVEL = {f"h{level}": level for level in range(1, 7)}
_BLOCK = frozenset(
    {
        "address", "article", "aside", "blockquote", "body", "dd", "details", "dialog", "div",
        "dl", "dt", "fieldset", "figcaption", "figure", "footer", "form", "header", "hgroup",
        "hr", "html", "li", "main", "nav", "ol", "p", "pre", "section", "summary", "ul",
    }
)  # fmt: skip
_CELL_SEPARATOR = " | "
_HTML_WHITESPACE = re.compile(r"[ \t\n\r\f]+")


def parse_html(content: bytes, limits: IngestionLimits) -> list[ParsedBlock]:
    soup = BeautifulSoup(_decode(content), _TREE_BUILDER)
    walker = _BlockWalker()
    walker.walk(soup)
    walker.flush()
    return walker.blocks


def _decode(content: bytes) -> str:
    try:
        return content.decode("utf-8-sig")
    except UnicodeDecodeError:
        pass
    declared = EncodingDetector.find_declared_encoding(content, is_html=True)
    if declared:
        try:
            return content.decode(declared)
        except (LookupError, UnicodeDecodeError):
            pass
    raise IngestionError.corrupt_file(
        "HTML is not valid UTF-8 and declares no usable character encoding"
    )


def _is_text(node: object) -> bool:
    # Exact type: comments, doctypes, CDATA and script/style strings are
    # NavigableString subclasses and are never document text.
    return type(node) is NavigableString


def _skipped(tag: Tag) -> bool:
    return tag.name in _DISCARDED or tag.has_attr("hidden")


def _text_node(node: NavigableString, preformatted: bool) -> str:
    return str(node) if preformatted else _HTML_WHITESPACE.sub(" ", str(node))


class _BlockWalker:
    def __init__(self) -> None:
        self.blocks: list[ParsedBlock] = []
        self._trail = HeadingTrail()
        self._buffer: list[str] = []

    def walk(self, node: Tag, preformatted: bool = False) -> None:
        for child in node.children:
            if _is_text(child):
                self._buffer.append(_text_node(child, preformatted))  # type: ignore[arg-type]
            elif isinstance(child, Tag) and not _skipped(child):
                self._element(child, preformatted)

    def _element(self, tag: Tag, preformatted: bool) -> None:
        name = tag.name
        if name == "br":
            self._buffer.append("\n")
        elif name in _HEADING_LEVEL:
            self.flush()
            title = normalize_text(_inline_text(tag))
            if title:
                path = self._trail.enter(_HEADING_LEVEL[name], title)
                self._add(make_block(BlockKind.HEADING, title, section=path))
        elif name == "table":
            self.flush()
            self._add(make_block(BlockKind.TABLE, _table_text(tag), section=self._trail.path))
        elif name in _BLOCK:
            self.flush()
            self.walk(tag, preformatted or name == "pre")
            self.flush()
        else:  # inline element: its text continues the current block
            self.walk(tag, preformatted)

    def flush(self) -> None:
        text = "".join(self._buffer)
        self._buffer.clear()
        self._add(make_block(BlockKind.PARAGRAPH, text, section=self._trail.path))

    def _add(self, block: ParsedBlock | None) -> None:
        if block is not None:
            self.blocks.append(block)


def _inline_text(tag: Tag) -> str:
    """All visible text under ``tag``; nested blocks become line breaks."""
    parts: list[str] = []
    for child in tag.children:
        if _is_text(child):
            parts.append(_text_node(child, tag.name == "pre"))  # type: ignore[arg-type]
        elif isinstance(child, Tag) and not _skipped(child):
            if child.name == "br":
                parts.append("\n")
            elif child.name in _BLOCK or child.name in _HEADING_LEVEL or child.name == "table":
                parts.append(f"\n{_inline_text(child)}\n")
            else:
                parts.append(_inline_text(child))
    return "".join(parts)


def _table_text(table: Tag) -> str:
    """Caption first, then rows on separate lines with cells joined by " | ".
    Rows of nested tables appear inside their cell, not as rows of this table."""
    lines = []
    caption = table.find("caption")
    if isinstance(caption, Tag) and caption.find_parent("table") is table:
        lines.append(" ".join(_inline_text(caption).split()))
    for row in table.find_all("tr"):
        if row.find_parent("table") is not table:
            continue
        cells = [
            " ".join(_inline_text(cell).split())
            for cell in row.find_all(["td", "th"], recursive=False)
            if not _skipped(cell)
        ]
        if any(cells):  # a row of empty cells is not text
            lines.append(_CELL_SEPARATOR.join(cells))
    return "\n".join(lines)


HTML_PARSER = Parser(
    name="courseflow-html",
    version=parser_version(1, "beautifulsoup4"),
    parse=parse_html,
    empty_reason="page contains no extractable text",
)
