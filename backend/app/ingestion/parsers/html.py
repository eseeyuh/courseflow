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
<head> and any <title> (with or without an explicit <head>), comments and
elements with the ``hidden`` attribute (including table rows, row groups and
captions) are discarded. Image alt text is not extracted in v0.1.

Decoding is deterministic, never guessed: UTF-8 (with or without BOM),
otherwise the charset the page declares if it is a standard web encoding,
otherwise `corrupt_file`. Pages nested deeper than ``MAX_NESTING_DEPTH``
elements are `limit_exceeded`.
"""

import codecs
import re

from bs4 import BeautifulSoup, NavigableString, Tag
from bs4.dammit import EncodingDetector

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

# Named explicitly: parsers differ on malformed markup, so the choice is part
# of the output contract. html.parser is in the standard library.
_TREE_BUILDER = "html.parser"

_DISCARDED = frozenset(
    {
        "head", "title", "script", "style", "template", "noscript", "iframe", "object",
        "embed", "svg", "math",
    }
)  # fmt: skip
_HEADING_LEVEL = {f"h{level}": level for level in range(1, 7)}
_BLOCK = frozenset(
    {
        "address", "article", "aside", "blockquote", "body", "dd", "details", "dialog", "div",
        "dl", "dt", "fieldset", "figcaption", "figure", "footer", "form", "header", "hgroup",
        "hr", "html", "li", "main", "nav", "ol", "p", "pre", "section", "summary", "ul",
    }
)  # fmt: skip
_CELL_SEPARATOR = " | "

# Element nesting the walker accepts. Real pages stay far below it; deeper
# trees would exhaust the recursion limit, so they are rejected up front.
MAX_NESTING_DEPTH = 256

# Declared encodings accepted, by Python codec name: the standard web
# encodings Python supports. Any other label (e.g. "unicode_escape", "rot13")
# would decode differently from a browser, so it is refused.
_WEB_ENCODINGS = frozenset(
    {
        "utf-8", "utf-16-le", "utf-16-be", "cp866", "koi8-r", "koi8-u", "mac-roman",
        "iso8859-2", "iso8859-3", "iso8859-4", "iso8859-5", "iso8859-6", "iso8859-7",
        "iso8859-8", "iso8859-10", "iso8859-13", "iso8859-14", "iso8859-15", "iso8859-16",
        "cp1250", "cp1251", "cp1252", "cp1253", "cp1254", "cp1255", "cp1256", "cp1257",
        "cp1258", "gbk", "gb18030", "big5", "euc_jp", "iso2022_jp", "shift_jis", "euc_kr",
    }
)  # fmt: skip
# Browsers decode these labels as windows-1252 (WHATWG Encoding Standard).
_DECODED_AS_WINDOWS_1252 = frozenset({"iso8859-1", "ascii"})
_HTML_WHITESPACE = re.compile(r"[ \t\n\r\f]+")


def parse_html(content: bytes, limits: IngestionLimits) -> ParserOutput:
    soup = BeautifulSoup(_decode(content), _TREE_BUILDER)
    if _nesting_depth(soup) > MAX_NESTING_DEPTH:
        raise IngestionError.limit_exceeded(
            f"HTML nesting is deeper than {MAX_NESTING_DEPTH} elements"
        )
    walker = _BlockWalker()
    walker.walk(soup)
    walker.flush()
    return ParserOutput(walker.blocks)


def _decode(content: bytes) -> str:
    try:
        return content.decode("utf-8-sig")
    except UnicodeDecodeError:
        pass
    declared = EncodingDetector.find_declared_encoding(content, is_html=True)
    if declared:
        try:
            codec = codecs.lookup(declared).name
            if codec in _DECODED_AS_WINDOWS_1252:
                codec = "cp1252"
            if codec in _WEB_ENCODINGS:
                return content.decode(codec)
        except (LookupError, UnicodeDecodeError):
            pass
    raise IngestionError.corrupt_file(
        "HTML is not valid UTF-8 and declares no usable character encoding"
    )


def _nesting_depth(soup: BeautifulSoup) -> int:
    """Deepest element nesting, computed without recursion."""
    deepest = 0
    pending: list[tuple[Tag, int]] = [(soup, 0)]
    while pending:
        node, depth = pending.pop()
        deepest = max(deepest, depth)
        pending.extend((child, depth + 1) for child in node.children if isinstance(child, Tag))
    return deepest


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
    if (
        isinstance(caption, Tag)
        and caption.find_parent("table") is table
        and not _hidden_within(caption, table)
    ):
        lines.append(" ".join(_inline_text(caption).split()))
    for row in table.find_all("tr"):
        if row.find_parent("table") is not table or _hidden_within(row, table):
            continue
        cells = [
            " ".join(_inline_text(cell).split())
            for cell in row.find_all(["td", "th"], recursive=False)
            if not _skipped(cell)
        ]
        if any(cells):  # a row of empty cells is not text
            lines.append(_CELL_SEPARATOR.join(cells))
    return "\n".join(lines)


def _hidden_within(tag: Tag, table: Tag) -> bool:
    """True if ``tag`` or a container between it and ``table`` (e.g. a row
    group) is skipped."""
    node: Tag | None = tag
    while node is not None and node is not table:
        if _skipped(node):
            return True
        node = node.parent
    return False


HTML_PARSER = Parser(
    name="courseflow-html",
    version=parser_version(1, "beautifulsoup4"),
    parse=parse_html,
    empty_reason="page contains no extractable text",
)
