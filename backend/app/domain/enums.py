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


class ModelCallKind(StrEnum):
    """What operation a model call performed. Independent of ``ModelRole``,
    which says why a model was chosen; embedding calls have no role."""

    CHAT_STRUCTURED = "chat_structured"
    EMBEDDING = "embedding"


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


class VerificationStatus(StrEnum):
    """Whether a claim may be presented as fact. Only ``verified`` claims are."""

    VERIFIED = "verified"
    NEEDS_REVIEW = "needs_review"
    REJECTED = "rejected"


class DeadlinePrecision(StrEnum):
    """How much of a deadline the source states. A month, a week or a term is
    not a deadline: those are abstentions, never a row."""

    DATETIME = "datetime"  # a calendar date and a local time of day
    DATE = "date"  # a calendar date with no time of day stated


class TimezoneSource(StrEnum):
    """How a deadline's timezone was obtained. Never more than the evidence says."""

    EXPLICIT_SOURCE = "explicit_source"  # the cited quote itself names it (e.g. "UK time")
    # The quote names none; the institution's own timezone was applied.
    INSTITUTION_DEFAULT = "institution_default"
    UNKNOWN = "unknown"  # neither: no instant can be derived


class EvidenceSubject(StrEnum):
    """The kind of domain object an EvidenceLink supports."""

    ASSESSMENT = "assessment"
    ASSESSMENT_DEADLINE = "assessment_deadline"


class EvidenceField(StrEnum):
    """Which fact of its subject an EvidenceLink supports."""

    TITLE = "title"  # that the assessment exists, under this name
    WEIGHTING = "weighting"
    DEADLINE = "deadline"


class ReviewReason(StrEnum):
    """Why a stored claim or piece of evidence needs review. Derived by the
    evidence gate from the evidence itself, never supplied by a model."""

    # An assessment candidate with no verified deadline and no verified weighting.
    NO_VERIFIED_ASSESSMENT_FACT = "no_verified_assessment_fact"
    # Verified sources state different weightings for one assessment.
    CONFLICTING_WEIGHTING = "conflicting_weighting"
    # The quote occurs more than once in its span: no single location.
    NON_UNIQUE_QUOTE_LOCATION = "non_unique_quote_location"
    # A local time, with no timezone in the quote and none for the institution.
    MISSING_TIMEZONE = "missing_timezone"
    # The quote names a timezone that cannot be mapped (never replaced by a default).
    UNRECOGNISED_TIMEZONE = "unrecognised_timezone"
    CONFLICTING_TIMEZONE = "conflicting_timezone"  # the quote names two
    WEEKDAY_MISMATCH = "weekday_mismatch"  # the stated weekday is not the date's
    AMBIGUOUS_TIME = "ambiguous_time"  # "midnight": the start or the end of the day?
    AMBIGUOUS_LOCAL_TIME = "ambiguous_local_time"  # occurs twice (DST overlap)


class RejectionReason(StrEnum):
    """Why the gate rejected a proposed fact; rejected facts are never stored."""

    SPAN_UNKNOWN = "span_unknown"  # not a local span ID issued for the unit
    QUOTE_NOT_FOUND = "quote_not_found"
    QUOTE_OVERLAPS_INJECTION = "quote_overlaps_injection"
    TITLE_UNSUPPORTED = "title_unsupported"
    WEIGHTING_UNSUPPORTED = "weighting_unsupported"
    SCHEMA_VIOLATION = "schema_violation"
    MODEL_DATE_MISMATCH = "model_date_mismatch"
    MODEL_TIME_MISMATCH = "model_time_mismatch"
    MODEL_TIME_INVALID = "model_time_invalid"
    MONTH_ONLY = "month_only"  # a month, never a date
    NO_DATE = "no_date"
    NUMERIC_DATE = "numeric_date"  # 12/11/2026: day-month order undecidable
    MULTIPLE_DATES = "multiple_dates"
    IMPOSSIBLE_DATE = "impossible_date"
    IMPOSSIBLE_TIME = "impossible_time"
    MULTIPLE_TIMES = "multiple_times"
    LOCAL_TIME_NONEXISTENT = "local_time_nonexistent"  # skipped by a DST gap
    # The quote's date has an explicit non-deadline role (released, published,
    # opens, feedback...) or is a parenthesised notice date.
    NON_DEADLINE_DATE = "non_deadline_date"
    # Nothing in the quote establishes that its date is a deadline.
    NO_DEADLINE_SEMANTICS = "no_deadline_semantics"
    # No valid evidence anywhere in the candidate supports its title.
    IDENTITY_UNSUPPORTED = "identity_unsupported"
