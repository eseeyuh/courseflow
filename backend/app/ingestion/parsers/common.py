# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Pieces shared by the format parsers."""

from collections.abc import Callable
from dataclasses import dataclass
from importlib.metadata import version

from app.domain.enums import BlockKind
from app.ingestion.guards import IngestionLimits
from app.ingestion.normalize import normalize_text
from app.ingestion.schemas import ParsedBlock, SpanLocator, section_path


@dataclass(frozen=True, slots=True)
class Parser:
    """A registered format parser.

    ``version`` combines CourseFlow's parser revision with the library
    version, so a library upgrade is visible on every ResourceVersion.
    """

    name: str
    version: str
    parse: Callable[[bytes, IngestionLimits], list[ParsedBlock]]
    # Detail for `empty_text` when parsing succeeds but yields no blocks.
    empty_reason: str


def parser_version(revision: int, library: str) -> str:
    return f"{revision} ({library} {version(library)})"


def make_block(
    kind: BlockKind,
    text: str,
    *,
    page_number: int | None = None,
    slide_number: int | None = None,
    section: str | None = None,
) -> ParsedBlock | None:
    """Normalise ``text`` into a block, or ``None`` when nothing visible remains."""
    normalized = normalize_text(text)
    if not normalized:
        return None
    locator = SpanLocator(page_number=page_number, slide_number=slide_number, section_path=section)
    return ParsedBlock(kind=kind, locator=locator, text=normalized)


class HeadingTrail:
    """The chain of open headings, for ``section_path``.

    A heading closes every open heading at its level or deeper, so
    H1 > H2 > H3 followed by a new H2 gives H1 > (new H2).
    """

    def __init__(self) -> None:
        self._open: list[tuple[int, str]] = []

    def enter(self, level: int, title: str) -> str:
        """Open a heading; return the section path that includes it."""
        while self._open and self._open[-1][0] >= level:
            self._open.pop()
        # A path is one line even if the heading text contains line breaks.
        self._open.append((level, " ".join(title.split("\n"))))
        return self.path

    @property
    def path(self) -> str:
        return section_path([title for _, title in self._open])
