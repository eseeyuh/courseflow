# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Canonical domain vocabularies (see docs/architecture/data-contract.md).

Stored as VARCHAR + CHECK constraint, not native PostgreSQL ENUM types, so
adding a value is a small, reviewable migration. Extending any of these is a
contract change: update the data contract and add a migration.
"""

from enum import StrEnum


class LmsType(StrEnum):
    DEMO = "demo"
    UPLOAD = "upload"
    MOODLE = "moodle"


class CourseStatus(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class ResourceType(StrEnum):
    LECTURE = "lecture"
    LAB = "lab"
    READING = "reading"
    WORKSHOP = "workshop"
    HANDBOOK = "handbook"
    BRIEF = "brief"
    ANNOUNCEMENT = "announcement"
    PAGE = "page"


class WorkflowRunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class ModelRole(StrEnum):
    """What a call needs, resolved to a model ID by configuration."""

    FAST = "fast"
    STRONG = "strong"


class ModelCallStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class ModelCallErrorCategory(StrEnum):
    """Why a model call failed. The first four are transient and retried."""

    TIMEOUT = "timeout"
    CONNECTION = "connection"
    RATE_LIMITED = "rate_limited"
    SERVER_ERROR = "server_error"
    AUTHENTICATION = "authentication"
    INVALID_REQUEST = "invalid_request"
    TRUNCATED = "truncated"
    REFUSED = "refused"
    INVALID_OUTPUT = "invalid_output"
    UNEXPECTED = "unexpected"


class MediaType(StrEnum):
    """Source formats ingestion accepts (IANA media types).

    A property of each ResourceVersion, not of the Resource: a brief may be
    re-issued as a PDF after starting life as a DOCX.
    """

    PDF = "application/pdf"
    DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    HTML = "text/html"


class BlockKind(StrEnum):
    """Structural role of a SourceSpan created by ingestion."""

    PAGE = "page"
    HEADING = "heading"
    PARAGRAPH = "paragraph"
    TABLE = "table"
    SLIDE_TEXT = "slide_text"
    SLIDE_NOTES = "slide_notes"


class IngestionErrorCategory(StrEnum):
    """Why one file could not be ingested. Stable: used in logs and evaluation."""

    # An unsupported file format, container variant or intentionally unsupported
    # subtype (e.g. macro-enabled, encrypted or legacy Office), not only an extension.
    UNSUPPORTED_TYPE = "unsupported_type"
    CORRUPT_FILE = "corrupt_file"
    EMPTY_TEXT = "empty_text"
    LIMIT_EXCEEDED = "limit_exceeded"
    PARSER_FAILURE = "parser_failure"
