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
    ASSESSMENT_REQUIREMENT = "assessment_requirement"
    OBLIGATION = "obligation"
    DEPENDENCY_EDGE = "dependency_edge"


class EvidenceField(StrEnum):
    """Which fact of its subject an EvidenceLink supports."""

    TITLE = "title"  # that the assessment exists, under this name
    WEIGHTING = "weighting"
    DEADLINE = "deadline"
    REQUIREMENT = "requirement"  # that the requirement is stated, for its assessment
    OBLIGATION = "obligation"  # that the action is stated, with its modality
    DUE_DATE = "due_date"  # an obligation's explicit calendar due date
    DEPENDENCY = "dependency"  # that the source states the ordering


class DependencyRelation(StrEnum):
    """How a prerequisite constrains its dependent."""

    BEFORE = "before"  # should come first
    REQUIRED_FOR = "required_for"  # must be done first: the dependent is blocked without it


class DependencyNodeKind(StrEnum):
    """What one end of a DependencyEdge is."""

    ASSESSMENT = "assessment"
    OBLIGATION = "obligation"
    MILESTONE = "milestone"


class Milestone(StrEnum):
    """Named academic steps a dependency can point at that are not stored
    claims. Deliberately closed: a new milestone is a contract change."""

    ETHICS_APPROVAL = "ethics_approval"
    DATA_COLLECTION = "data_collection"


class ConflictField(StrEnum):
    """Which fact the sources of a Conflict disagree on."""

    DEADLINE = "deadline"


class ConflictStatus(StrEnum):
    """``resolved_by_source`` only when the sources no longer disagree, never
    by choosing a value."""

    OPEN = "open"
    ACKNOWLEDGED = "acknowledged"
    RESOLVED_BY_SOURCE = "resolved_by_source"


class StudyStateValue(StrEnum):
    """A learner's own state for one piece of teaching material. Opening a
    document never changes it: only the learner does."""

    NOT_STARTED = "not_started"
    STUDYING = "studying"
    STUDIED = "studied"


class StudyTaskType(StrEnum):
    STUDY_MATERIAL = "study_material"
    OBLIGATION = "obligation"
    SUBMIT_ASSESSMENT = "submit_assessment"
    CHECK_CONFLICT = "check_conflict"
    ETHICS_APPROVAL = "ethics_approval"
    DATA_COLLECTION = "data_collection"


class StudyTaskStatus(StrEnum):
    READY = "ready"
    BLOCKED = "blocked"  # a required prerequisite is not done
    COMPLETED = "completed"


class ChangeType(StrEnum):
    ADDED = "added"  # a new resource
    UPDATED = "updated"  # a new version of a known resource


class MappingRelation(StrEnum):
    """How a requirement relates to the teaching material it is mapped to."""

    # The material teaches what the requirement asks for: where to study it.
    SUPPORTED_BY = "supported_by"


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
    # The action is only permitted or possible ("may", "can"): not an obligation.
    PERMISSIVE_WORDING = "permissive_wording"
    MIXED_MODALITY = "mixed_modality"  # the evidence is both mandatory and permissive
    # A requirement candidate whose evidence has no requirement wording.
    NO_REQUIREMENT_LANGUAGE = "no_requirement_language"
    # Nothing in the evidence ties the claim to the assessment it names.
    ASSESSMENT_LINK_UNSUPPORTED = "assessment_link_unsupported"
    # The evidence lacks the wording its obligation category needs.
    CATEGORY_UNSUPPORTED = "category_unsupported"
    # Sources for one canonical obligation disagree on one of its values.
    CONFLICTING_CATEGORY = "conflicting_category"
    CONFLICTING_MODALITY = "conflicting_modality"
    CONFLICTING_ASSESSMENT_LINK = "conflicting_assessment_link"
    CONFLICTING_DUE_DATE = "conflicting_due_date"


class ObligationCategory(StrEnum):
    """What kind of action an obligation is: deliberately narrow. Anything
    that fits none of the specific kinds is ``other``, never a new kind."""

    SUPERVISOR_REVIEW = "supervisor_review"  # a supervisor review or meeting
    APPROVAL = "approval"
    ETHICS_ADMIN_FORM = "ethics_admin_form"  # an ethics or administrative form
    FORMATIVE_PREREQUISITE = "formative_prerequisite"
    SUBMISSION_PREREQUISITE = "submission_prerequisite"
    OTHER = "other"


class ObligationModality(StrEnum):
    """Whether the source requires the action or only recommends it. Derived
    from the evidence wording by the gate; permissive wording ("may") is
    neither, so it never verifies."""

    MANDATORY = "mandatory"  # must, required, must not
    RECOMMENDED = "recommended"  # should, recommended, encouraged


class RejectionReason(StrEnum):
    """Why the gate rejected a proposed fact; rejected facts are never stored."""

    SPAN_UNKNOWN = "span_unknown"  # not a local span ID issued for the unit
    QUOTE_NOT_FOUND = "quote_not_found"
    QUOTE_OVERLAPS_INJECTION = "quote_overlaps_injection"
    TITLE_UNSUPPORTED = "title_unsupported"
    WEIGHTING_UNSUPPORTED = "weighting_unsupported"
    SCHEMA_VIOLATION = "schema_violation"
    MODEL_DATE_MISMATCH = "model_date_mismatch"
    MODEL_DATE_INVALID = "model_date_invalid"  # not a calendar date (2026-13-01)
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
    # Not a local assessment ID issued for the unit.
    ASSESSMENT_UNKNOWN = "assessment_unknown"
    # Neither the statement nor a cited lead-in says it is required.
    NO_REQUIREMENT_LANGUAGE = "no_requirement_language"
    STATEMENT_TOO_LONG = "statement_too_long"  # its sentence exceeds the stored bound
    # No cited quote says the action is required or recommended.
    NO_OBLIGATION_WORDING = "no_obligation_wording"
    NO_EVIDENCE = "no_evidence"  # every cited quote was rejected
    # What an assessed artefact must contain, proposed as an obligation: it is
    # an assessment requirement, not an action the student performs.
    WRONG_CLAIM_TYPE = "wrong_claim_type"
