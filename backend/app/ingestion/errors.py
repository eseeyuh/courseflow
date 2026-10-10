# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Controlled ingestion failures.

Every rejected file maps to exactly one ``IngestionErrorCategory``. Details
are written by CourseFlow, never copied from the input or from a parser
exception: they end up in logs and API responses, and must not carry
document text, secrets or local paths.
"""

from typing import Self

from app.domain.enums import IngestionErrorCategory


class IngestionError(Exception):
    """One file could not be ingested. Other files in the same import are unaffected."""

    def __init__(self, category: IngestionErrorCategory, detail: str) -> None:
        super().__init__(f"{category.value}: {detail}")
        self.category = category
        self.detail = detail

    @classmethod
    def unsupported_type(cls, detail: str) -> Self:
        return cls(IngestionErrorCategory.UNSUPPORTED_TYPE, detail)

    @classmethod
    def corrupt_file(cls, detail: str) -> Self:
        return cls(IngestionErrorCategory.CORRUPT_FILE, detail)

    @classmethod
    def empty_text(cls, detail: str) -> Self:
        return cls(IngestionErrorCategory.EMPTY_TEXT, detail)

    @classmethod
    def limit_exceeded(cls, detail: str) -> Self:
        return cls(IngestionErrorCategory.LIMIT_EXCEEDED, detail)

    @classmethod
    def parser_failure(cls, exc: BaseException) -> Self:
        """An unexpected failure. Keeps only the exception type: its message may
        quote document content or a file system path."""
        return cls(IngestionErrorCategory.PARSER_FAILURE, f"unexpected {type(exc).__name__}")
