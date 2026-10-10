# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Parser-side schemas: pure, in-memory, no database identity.

``RawSource`` is what arrives; ``ParsedDocument`` is what a parser returns:
ordered ``ParsedBlock``s, each with a ``SpanLocator``. Persistence turns one
``ParsedDocument`` into one ResourceVersion and one SourceSpan per block.
The rules mirror the database CHECK constraints so a parser bug fails here,
in a unit test, before any row is written.

Validation errors never echo input values: they may be document text or a
local path, and errors end up in logs.
"""

import re
from collections.abc import Sequence
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.domain.enums import BlockKind, MediaType
from app.ingestion.hashing import text_hash
from app.ingestion.normalize import normalize_text

# Joins blocks into ResourceVersion.extracted_text; span offsets index that string.
BLOCK_SEPARATOR = "\n\n"
SECTION_SEPARATOR = " > "
# section_path for text that comes before the first heading.
PREAMBLE = "(preamble)"

_SHA256_HEX = r"^[0-9a-f]{64}$"
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
# A logical URI: a scheme of two or more characters (so "c:/..." is not one),
# then a non-blank remainder.
_LOGICAL_URI = re.compile(r"^[a-z][a-z0-9+.-]+:\S")


def section_path(headings: Sequence[str]) -> str:
    """The heading chain as a ``section_path`` value, or the preamble marker."""
    return SECTION_SEPARATOR.join(headings) if headings else PREAMBLE


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)


class RawSource(_Frozen):
    """Original source bytes as received, before any parsing."""

    # strict: refuse str, which would otherwise be silently encoded.
    content: bytes = Field(strict=True, repr=False)
    # Bare file name for display and type detection. Never used as a path.
    display_name: str = Field(min_length=1, max_length=255)
    # Logical identity of the source within its course, e.g. "upload:brief.pdf"
    # or "demo://northbridge/cs101/brief". Never a local file system path.
    source_uri: str = Field(min_length=1, max_length=2048)

    @field_validator("display_name")
    @classmethod
    def _bare_file_name(cls, value: str) -> str:
        if (
            "/" in value
            or "\\" in value
            or value in {".", ".."}
            or value != value.strip()
            or _CONTROL_CHARS.search(value)
        ):
            raise ValueError("display_name must be a bare file name, not a path")
        return value

    @field_validator("source_uri")
    @classmethod
    def _logical_uri(cls, value: str) -> str:
        if (
            not _LOGICAL_URI.match(value)
            or value.startswith("file:")
            or "\\" in value
            or _CONTROL_CHARS.search(value)
        ):
            raise ValueError(
                "source_uri must be a logical URI such as 'upload:brief.pdf', not a local path"
            )
        return value


class SpanLocator(_Frozen):
    """Where a person finds a block in the original source.

    At least one locator is required, as for SourceSpan. Media timestamps are
    not produced by any v0.1 parser and are added with media support.
    """

    page_number: int | None = Field(default=None, ge=1)  # physical, 1-based
    slide_number: int | None = Field(default=None, ge=1)  # presentation order, 1-based
    section_path: str | None = None

    @field_validator("section_path")
    @classmethod
    def _non_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("section_path must not be blank")
        return value

    @model_validator(mode="after")
    def _at_least_one(self) -> Self:
        if self.page_number is None and self.slide_number is None and self.section_path is None:
            raise ValueError("a locator needs a page number, slide number or section path")
        return self


# The locator each kind of block must carry.
_REQUIRED_LOCATOR: dict[BlockKind, str] = {
    BlockKind.PAGE: "page_number",
    BlockKind.HEADING: "section_path",
    BlockKind.PARAGRAPH: "section_path",
    BlockKind.TABLE: "section_path",
    BlockKind.SLIDE_TEXT: "slide_number",
    BlockKind.SLIDE_NOTES: "slide_number",
}


class ParsedBlock(_Frozen):
    """One structural unit of a source (page, heading, paragraph, table, slide text, notes)."""

    kind: BlockKind
    locator: SpanLocator
    text: str = Field(min_length=1)

    @field_validator("text")
    @classmethod
    def _already_normalized(cls, value: str) -> str:
        # Parsers must normalise; this also rejects whitespace-only text.
        if normalize_text(value) != value:
            raise ValueError("block text must be normalised and non-blank")
        return value

    @model_validator(mode="after")
    def _locator_fits_kind(self) -> Self:
        field = _REQUIRED_LOCATOR[self.kind]
        if getattr(self.locator, field) is None:
            raise ValueError(f"a {self.kind.value} block needs a {field} locator")
        return self


class ParsedDocument(_Frozen):
    """A parser's complete output for one source version.

    Identifies its source by ``content_hash`` (SHA-256 of the raw bytes): the
    ResourceVersion row does not exist until persistence.
    """

    media_type: MediaType
    content_hash: str = Field(pattern=_SHA256_HEX)
    byte_size: int = Field(ge=0)
    parser_name: str = Field(min_length=1)
    parser_version: str = Field(min_length=1)
    # Source order. Empty documents are an ingestion error, not a ParsedDocument.
    blocks: tuple[ParsedBlock, ...] = Field(min_length=1)

    @property
    def extracted_text(self) -> str:
        return BLOCK_SEPARATOR.join(block.text for block in self.blocks)

    @property
    def text_hash(self) -> str:
        return text_hash(self.extracted_text)

    def block_offsets(self) -> tuple[tuple[int, int], ...]:
        """``[start, end)`` of each block in ``extracted_text``, in Unicode code points."""
        offsets = []
        start = 0
        for block in self.blocks:
            end = start + len(block.text)
            offsets.append((start, end))
            start = end + len(BLOCK_SEPARATOR)
        return tuple(offsets)
