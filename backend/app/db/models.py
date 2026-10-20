# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""ORM models for the source/provenance layer and workflow lineage.

Provenance chain (docs/architecture/data-contract.md):
    Institution -> Course -> Resource -> ResourceVersion -> SourceSpan
WorkflowRun records which ResourceVersions a run consumed. ModelCall records
each logical model call, inside a run or standalone.

Academic and trust layers:
    Course -> Assessment -> AssessmentDeadline
    EvidenceLink -> SourceSpan, for an Assessment fact or an AssessmentDeadline
An Assessment is canonical per course; every source that states it, its
weighting or a deadline adds an EvidenceLink instead of a duplicate row.
Sources that disagree on a deadline give one AssessmentDeadline each: the
schema never picks a winner.

Retrieval layer (see ADR-007):
    ResourceVersion -> Chunk -> ChunkEmbedding -> Embedding -> EmbeddingSpaceRecord
A Chunk is an exact slice of one version's text; its resource and course come
only through that version. Embeddings are global and content-addressed: one
row per (space, input format, input hash), shared by every chunk whose
provider input was identical.

All relationships use lazy="raise": async code must load related objects
explicitly (e.g. selectinload) instead of triggering hidden I/O.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any

from pgvector.sqlalchemy import VECTOR
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    Computed,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Numeric,
    SmallInteger,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, CreatedAt, Timestamps, UUIDPrimaryKey, str_enum
from app.domain.enums import (
    BlockKind,
    CourseStatus,
    DeadlinePrecision,
    EvidenceField,
    EvidenceSubject,
    LmsType,
    MediaType,
    ModelCallErrorCategory,
    ModelCallKind,
    ModelCallStatus,
    ModelRole,
    ResourceType,
    TimezoneSource,
    VerificationStatus,
    WorkflowRunStatus,
)


class Institution(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "institutions"
    __table_args__ = (CheckConstraint("length(btrim(timezone)) > 0", name="timezone_nonblank"),)

    name: Mapped[str] = mapped_column(Text)
    lms_type: Mapped[LmsType] = mapped_column(str_enum(LmsType, "lms_type"))
    # IANA name (e.g. "Europe/London"), validated by the application: the
    # timezone of a deadline whose source states none. NULL: unknown.
    timezone: Mapped[str | None] = mapped_column(Text)

    courses: Mapped[list[Course]] = relationship(back_populates="institution", lazy="raise")


class Course(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "courses"
    __table_args__ = (
        # Re-syncing an LMS course must find the existing row, not duplicate it.
        # (NULL external_ids never conflict.) Its leading column also serves
        # as the index for the institution_id foreign key.
        UniqueConstraint("institution_id", "external_id", name="uq_courses_institution_external"),
    )

    institution_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("institutions.id"))
    title: Mapped[str] = mapped_column(Text)
    external_id: Mapped[str | None] = mapped_column(Text)
    status: Mapped[CourseStatus] = mapped_column(
        str_enum(CourseStatus, "course_status"),
        default=CourseStatus.ACTIVE,
        server_default=CourseStatus.ACTIVE.value,
    )

    institution: Mapped[Institution] = relationship(back_populates="courses", lazy="raise")
    resources: Mapped[list[Resource]] = relationship(back_populates="course", lazy="raise")


class Resource(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "resources"
    __table_args__ = (
        # Logical identity inside a course: re-importing the same source_uri
        # adds a version to the same Resource instead of a new Resource.
        UniqueConstraint("course_id", "source_uri", name="uq_resources_course_source_uri"),
        CheckConstraint("length(btrim(source_uri)) > 0", name="source_uri_nonblank"),
        # Composite FK: the current version must be a version OF THIS resource.
        # use_alter=True: added after both tables exist, because
        # resources <-> resource_versions reference each other.
        ForeignKeyConstraint(
            ["current_version_id", "id"],
            ["resource_versions.id", "resource_versions.resource_id"],
            name="fk_resources_current_version",
            use_alter=True,
        ),
    )

    course_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("courses.id"), index=True)
    type: Mapped[ResourceType] = mapped_column(str_enum(ResourceType, "resource_type"))
    title: Mapped[str] = mapped_column(Text)
    # Logical URI (e.g. "upload:brief.pdf"), never a local file system path.
    source_uri: Mapped[str] = mapped_column(Text)
    current_version_id: Mapped[uuid.UUID | None] = mapped_column(index=True)

    course: Mapped[Course] = relationship(back_populates="resources", lazy="raise")
    versions: Mapped[list[ResourceVersion]] = relationship(
        back_populates="resource",
        primaryjoin="Resource.id == ResourceVersion.resource_id",
        foreign_keys="ResourceVersion.resource_id",
        lazy="raise",
    )
    # post_update: set by a separate UPDATE after both rows are inserted,
    # which is how the ORM resolves the row-level cycle.
    current_version: Mapped[ResourceVersion | None] = relationship(
        primaryjoin="Resource.current_version_id == ResourceVersion.id",
        foreign_keys=[current_version_id],
        post_update=True,
        lazy="raise",
    )


class ResourceVersion(UUIDPrimaryKey, CreatedAt, Base):
    """Immutable content snapshot of a Resource.

    ``content_hash`` (SHA-256 of the original bytes) identifies the content, not
    the row: a revert (A -> B -> A) repeats a hash in a later version. The
    bytes themselves live in the raw object store under ``raw_object_ref``.
    ``extracted_text`` is the normalised text that SourceSpan offsets index.
    """

    __tablename__ = "resource_versions"
    __table_args__ = (
        UniqueConstraint("resource_id", "version_number", name="uq_resource_versions_number"),
        # Target of fk_resources_current_version (id is already unique on its own).
        UniqueConstraint("id", "resource_id", name="uq_resource_versions_id_resource"),
        CheckConstraint("version_number >= 1", name="version_number_positive"),
        CheckConstraint("content_hash ~ '^[0-9a-f]{64}$'", name="content_hash_sha256_hex"),
        # Content-addressed: the reference names the bytes, not where they are stored.
        CheckConstraint(
            "raw_object_ref = 'sha256:' || content_hash", name="raw_object_ref_matches_hash"
        ),
        CheckConstraint("byte_size >= 0", name="byte_size_nonnegative"),
        CheckConstraint("length(btrim(display_name)) > 0", name="display_name_nonblank"),
        CheckConstraint("length(btrim(parser_name)) > 0", name="parser_name_nonblank"),
        CheckConstraint("length(btrim(parser_version)) > 0", name="parser_version_nonblank"),
        CheckConstraint("length(extracted_text) > 0", name="extracted_text_nonempty"),
        # Integrity backstop for the application-computed hash.
        CheckConstraint(
            "text_hash = encode(sha256(convert_to(extracted_text, 'UTF8')), 'hex')",
            name="text_hash_matches_text",
        ),
        # Change detection: "have we seen this content for this resource?"
        # Not unique: content may legitimately revert to an earlier state.
        Index("ix_resource_versions_resource_hash", "resource_id", "content_hash"),
    )

    resource_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("resources.id"))
    version_number: Mapped[int]
    content_hash: Mapped[str] = mapped_column(Text)
    raw_object_ref: Mapped[str] = mapped_column(Text)
    media_type: Mapped[MediaType] = mapped_column(str_enum(MediaType, "media_type", length=128))
    byte_size: Mapped[int] = mapped_column(BigInteger)
    # File name as received for this version. Display only; not part of identity.
    display_name: Mapped[str] = mapped_column(Text)
    parser_name: Mapped[str] = mapped_column(Text)
    parser_version: Mapped[str] = mapped_column(Text)
    extracted_text: Mapped[str] = mapped_column(Text)
    text_hash: Mapped[str] = mapped_column(Text)

    resource: Mapped[Resource] = relationship(
        back_populates="versions",
        primaryjoin="ResourceVersion.resource_id == Resource.id",
        foreign_keys=[resource_id],
        lazy="raise",
    )
    spans: Mapped[list[SourceSpan]] = relationship(back_populates="resource_version", lazy="raise")


# Each block kind requires its canonical locator. Additional locator fields
# are permitted by the provenance contract. Same rule as ParsedBlock.
LOCATOR_FITS_KIND_SQL = (
    "(block_kind <> 'page' OR page_number IS NOT NULL) "
    "AND (block_kind NOT IN ('heading', 'paragraph', 'table') OR section_path IS NOT NULL) "
    "AND (block_kind NOT IN ('slide_text', 'slide_notes') OR slide_number IS NOT NULL)"
)


class SourceSpan(UUIDPrimaryKey, CreatedAt, Base):
    """A structural block of one ResourceVersion, created by ingestion.

    Human/source locators say where a person finds it (page, slide, section,
    time). Offsets locate it inside the version's ``extracted_text`` (Unicode
    code points). The excerpt is the verbatim text. Evidence that needs a
    narrower range references a span plus sub-offsets in the evidence layer;
    it does not create a different kind of SourceSpan.
    """

    __tablename__ = "source_spans"
    __table_args__ = (
        CheckConstraint(
            "page_number IS NOT NULL OR slide_number IS NOT NULL "
            "OR section_path IS NOT NULL OR timestamp_seconds IS NOT NULL",
            name="locator_present",
        ),
        CheckConstraint("page_number >= 1", name="page_positive"),
        CheckConstraint("slide_number >= 1", name="slide_positive"),
        CheckConstraint("length(btrim(section_path)) > 0", name="section_nonblank"),
        CheckConstraint("timestamp_seconds >= 0", name="timestamp_nonnegative"),
        CheckConstraint("(start_offset IS NULL) = (end_offset IS NULL)", name="offsets_paired"),
        CheckConstraint("start_offset >= 0 AND start_offset < end_offset", name="offsets_ordered"),
        CheckConstraint("length(excerpt) > 0", name="excerpt_nonempty"),
        CheckConstraint(
            "end_offset IS NULL OR end_offset - start_offset = length(excerpt)",
            name="excerpt_matches_offsets",
        ),
        # Same rule as ParsedBlock (app.ingestion.schemas); a parity test keeps them equal.
        CheckConstraint(LOCATOR_FITS_KIND_SQL, name="locator_fits_kind"),
        CheckConstraint("ordinal >= 0", name="ordinal_nonnegative"),
        # Also serves lookups by resource_version_id (leading column).
        UniqueConstraint("resource_version_id", "ordinal", name="uq_source_spans_version_ordinal"),
    )

    resource_version_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("resource_versions.id"))
    # 0-based source order within the version.
    ordinal: Mapped[int]
    block_kind: Mapped[BlockKind] = mapped_column(str_enum(BlockKind, "block_kind"))
    # Human/source locators (at least one required)
    page_number: Mapped[int | None]
    slide_number: Mapped[int | None]
    section_path: Mapped[str | None] = mapped_column(Text)
    timestamp_seconds: Mapped[Decimal | None] = mapped_column(Numeric(10, 3))
    # Machine provenance inside the extracted text (both or neither)
    start_offset: Mapped[int | None]
    end_offset: Mapped[int | None]
    # Evidence
    excerpt: Mapped[str] = mapped_column(Text)

    resource_version: Mapped[ResourceVersion] = relationship(back_populates="spans", lazy="raise")


# Normalised run -> input lineage. Deleting a run removes its input rows;
# a version that any run consumed cannot be deleted (RESTRICT).
workflow_run_inputs = Table(
    "workflow_run_inputs",
    Base.metadata,
    Column(
        "run_id",
        ForeignKey("workflow_runs.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "resource_version_id",
        ForeignKey("resource_versions.id", ondelete="RESTRICT"),
        primary_key=True,
        index=True,
    ),
)


class WorkflowRun(UUIDPrimaryKey, Timestamps, Base):
    """One execution of an import/analysis/change workflow."""

    __tablename__ = "workflow_runs"
    __table_args__ = (
        CheckConstraint(
            "finished_at IS NULL OR (started_at IS NOT NULL AND finished_at >= started_at)",
            name="finished_after_started",
        ),
        CheckConstraint(
            "summary IS NULL OR jsonb_typeof(summary) = 'object'", name="summary_is_object"
        ),
    )

    # "<kind>/v<N>", e.g. "index/v1". It also fixes the shape of ``summary``.
    workflow_version: Mapped[str] = mapped_column(Text)
    course_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("courses.id"), index=True)
    status: Mapped[WorkflowRunStatus] = mapped_column(
        str_enum(WorkflowRunStatus, "workflow_run_status"),
        default=WorkflowRunStatus.PENDING,
        server_default=WorkflowRunStatus.PENDING.value,
    )
    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]
    # Final counts, written once when the run finishes. Counts and IDs only,
    # never document text; the shape is defined by workflow_version.
    summary: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    course: Mapped[Course] = relationship(lazy="raise")
    input_versions: Mapped[list[ResourceVersion]] = relationship(
        secondary=workflow_run_inputs, lazy="raise"
    )


class ModelCall(UUIDPrimaryKey, CreatedAt, Base):
    """One logical model call: the final outcome after retries and repairs.

    Per-attempt detail (each HTTP request, its latency and error) lives in
    the logs; this row is the auditable summary. Never stores secrets or
    prompts; the output summary is the *validated* output only.

    Token counts are those reported in responses received. Attempts that
    failed in flight may have consumed unreported tokens: a lower bound.
    """

    __tablename__ = "model_calls"
    __table_args__ = (
        CheckConstraint("finished_at >= started_at", name="finished_after_started"),
        CheckConstraint("attempts >= 1", name="attempts_positive"),
        CheckConstraint(
            "repair_rounds >= 0 AND repair_rounds < attempts", name="repair_rounds_bounded"
        ),
        CheckConstraint("latency_ms >= 0", name="latency_nonnegative"),
        CheckConstraint(
            "prompt_tokens >= 0 AND completion_tokens >= 0 AND reasoning_tokens >= 0",
            name="tokens_nonnegative",
        ),
        # A success has an output and no error; a failure has an error category.
        CheckConstraint(
            "(status = 'succeeded' AND error_category IS NULL AND output_summary IS NOT NULL)"
            " OR (status = 'failed' AND error_category IS NOT NULL)",
            name="outcome_consistent",
        ),
        # Roles route generative models; an embedding call has none, and no repairs.
        CheckConstraint(
            "call_kind <> 'embedding' OR (model_role IS NULL AND repair_rounds = 0)",
            name="embedding_has_no_role",
        ),
    )

    # Nullable: smoke tests and evaluations call models outside any workflow.
    workflow_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("workflow_runs.id", ondelete="CASCADE"), index=True
    )
    # What operation happened. No default: every writer states it.
    call_kind: Mapped[ModelCallKind] = mapped_column(str_enum(ModelCallKind, "model_call_kind"))
    provider: Mapped[str] = mapped_column(Text)
    # The served model ID when the response reports one, else the configured one.
    model: Mapped[str] = mapped_column(Text)
    model_role: Mapped[ModelRole | None] = mapped_column(str_enum(ModelRole, "model_role"))
    prompt_version: Mapped[str] = mapped_column(Text)
    output_schema: Mapped[str] = mapped_column(Text)
    status: Mapped[ModelCallStatus] = mapped_column(str_enum(ModelCallStatus, "model_call_status"))
    error_category: Mapped[ModelCallErrorCategory | None] = mapped_column(
        str_enum(ModelCallErrorCategory, "model_call_error_category")
    )
    error_detail: Mapped[str | None] = mapped_column(Text)
    # HTTP requests made, including transport retries and repair turns.
    attempts: Mapped[int]
    # Repair turns after invalid output (schema-violation recovery metric).
    repair_rounds: Mapped[int] = mapped_column(default=0, server_default="0")
    prompt_tokens: Mapped[int | None]
    completion_tokens: Mapped[int | None]
    reasoning_tokens: Mapped[int | None]
    latency_ms: Mapped[int]
    provider_request_id: Mapped[str | None] = mapped_column(Text)
    output_summary: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime]
    finished_at: Mapped[datetime]

    workflow_run: Mapped[WorkflowRun | None] = relationship(lazy="raise")


# --- retrieval layer ------------------------------------------------------------------


# PostgreSQL text search configuration for chunks.text_tsv. English stemming
# and stop words: English lexical retrieval, not multilingual. Changing it is
# a migration (the stored column) plus a new lexical version.
TEXT_SEARCH_CONFIG = "english"
CHUNK_TEXT_TSV_EXPRESSION = f"to_tsvector('{TEXT_SEARCH_CONFIG}'::regconfig, text)"


class Chunk(UUIDPrimaryKey, CreatedAt, Base):
    """A retrieval unit: ``text`` is exactly ``extracted_text[start_offset:end_offset]``
    of one ResourceVersion, starting inside span ``first_span_ordinal`` and
    ending inside span ``last_span_ordinal``.

    Its spans are those with ordinals first..last. A chunk that splits one
    oversized span has first == last, with offsets inside that span. The
    exact-slice and containment rules span several tables: the application
    checks them before writing, and the ``chunks_provenance`` trigger (in the
    migration) checks them again for every row. Immutable: a new chunker
    version creates a new set of chunks.
    """

    __tablename__ = "chunks"
    __table_args__ = (
        # Idempotency key. Its leading column also serves lookups by version.
        UniqueConstraint(
            "resource_version_id",
            "chunker_version",
            "ordinal",
            name="uq_chunks_version_chunker_ordinal",
        ),
        # Both ends of the range are spans OF THIS version.
        ForeignKeyConstraint(
            ["resource_version_id", "first_span_ordinal"],
            ["source_spans.resource_version_id", "source_spans.ordinal"],
            name="fk_chunks_first_span",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["resource_version_id", "last_span_ordinal"],
            ["source_spans.resource_version_id", "source_spans.ordinal"],
            name="fk_chunks_last_span",
            ondelete="CASCADE",
        ),
        CheckConstraint("length(btrim(chunker_version)) > 0", name="chunker_version_nonblank"),
        CheckConstraint("ordinal >= 0", name="ordinal_nonnegative"),
        CheckConstraint("start_offset >= 0 AND start_offset < end_offset", name="offsets_ordered"),
        CheckConstraint("first_span_ordinal <= last_span_ordinal", name="span_range_ordered"),
        CheckConstraint("length(text) = end_offset - start_offset", name="text_matches_offsets"),
        CheckConstraint(
            "text_hash = encode(sha256(convert_to(text, 'UTF8')), 'hex')",
            name="text_hash_matches_text",
        ),
        CheckConstraint("estimated_tokens > 0", name="estimated_tokens_positive"),
        Index("ix_chunks_text_tsv", "text_tsv", postgresql_using="gin"),
    )

    # Derived data follows its source: deleting a version deletes its chunks.
    resource_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("resource_versions.id", ondelete="CASCADE")
    )
    chunker_version: Mapped[str] = mapped_column(Text)
    # 0-based order within (version, chunker_version).
    ordinal: Mapped[int]
    # Unicode code points in the version's extracted_text, like SourceSpan.
    start_offset: Mapped[int]
    end_offset: Mapped[int]
    first_span_ordinal: Mapped[int]
    last_span_ordinal: Mapped[int]
    text: Mapped[str] = mapped_column(Text)
    text_hash: Mapped[str] = mapped_column(Text)
    estimated_tokens: Mapped[int]
    # Lexical search vector, computed by PostgreSQL from ``text`` and never
    # written by the application. The configuration is explicit: stored
    # lexemes must not depend on default_text_search_config. Deferred so
    # ordinary chunk loads do not fetch it.
    text_tsv: Mapped[str] = mapped_column(
        TSVECTOR,
        Computed(CHUNK_TEXT_TSV_EXPRESSION, persisted=True),
        deferred=True,
    )

    resource_version: Mapped[ResourceVersion] = relationship(lazy="raise")


class EmbeddingSpaceRecord(UUIDPrimaryKey, CreatedAt, Base):
    """The unit of vector comparability (``app.ai.embeddings.EmbeddingSpace``).

    ``model`` is the configured/requested model ID. ``space_version`` is
    CourseFlow's own compatibility revision, not an upstream model version.
    """

    __tablename__ = "embedding_spaces"
    __table_args__ = (
        UniqueConstraint(
            "provider", "model", "dimensions", "space_version", name="uq_embedding_spaces_identity"
        ),
        # Target of fk_embeddings_space: an embedding's dimensions are its space's.
        UniqueConstraint("id", "dimensions", name="uq_embedding_spaces_id_dimensions"),
        CheckConstraint(
            "length(provider) BETWEEN 1 AND 64 AND provider = btrim(provider)",
            name="provider_valid",
        ),
        CheckConstraint(
            "length(model) BETWEEN 1 AND 256 AND model = btrim(model)", name="model_valid"
        ),
        # Same rule as the EMBEDDING_SPACE_VERSION setting.
        CheckConstraint(
            "space_version ~ '^[a-z0-9][a-z0-9._-]{0,31}$'", name="space_version_valid"
        ),
        # 16,000 is pgvector's limit for the vector type.
        CheckConstraint("dimensions BETWEEN 1 AND 16000", name="dimensions_in_range"),
    )

    provider: Mapped[str] = mapped_column(Text)
    model: Mapped[str] = mapped_column(Text)
    dimensions: Mapped[int]
    space_version: Mapped[str] = mapped_column(Text)


# Versioned input formats ("document/v1", "query/v1"). Only document formats
# can be attached to a chunk; a future document/v2 needs no schema change.
INPUT_FORMAT_SQL = "input_format ~ '^(document|query)/v[1-9][0-9]*$'"
DOCUMENT_FORMAT_SQL = "input_format ~ '^document/v[1-9][0-9]*$'"


class Embedding(UUIDPrimaryKey, CreatedAt, Base):
    """One vector, shared by every chunk whose exact provider input hashed to
    ``input_hash`` in this space and format. Cache key:
    (space_id, input_format, input_hash)."""

    __tablename__ = "embeddings"
    __table_args__ = (
        UniqueConstraint("space_id", "input_format", "input_hash", name="uq_embeddings_cache_key"),
        # Target of fk_chunk_embeddings_embedding.
        UniqueConstraint("id", "space_id", "input_format", name="uq_embeddings_id_space_format"),
        ForeignKeyConstraint(
            ["space_id", "dimensions"],
            ["embedding_spaces.id", "embedding_spaces.dimensions"],
            name="fk_embeddings_space",
        ),
        CheckConstraint(INPUT_FORMAT_SQL, name="input_format_versioned"),
        CheckConstraint("input_hash ~ '^[0-9a-f]{64}$'", name="input_hash_sha256_hex"),
        # The column has no fixed dimension (spaces differ): each row must
        # match its space, and a zero vector would make cosine distance NaN.
        CheckConstraint("vector_dims(vector) = dimensions", name="vector_dims_match"),
        CheckConstraint("vector_norm(vector) > 0", name="vector_nonzero"),
    )

    space_id: Mapped[uuid.UUID]
    # Copied from the space (fk_embeddings_space proves it) so a CHECK can use it.
    dimensions: Mapped[int]
    input_format: Mapped[str] = mapped_column(Text)
    # SHA-256 of the exact UTF-8 provider input.
    input_hash: Mapped[str] = mapped_column(Text)
    # pgvector stores float32.
    vector: Mapped[list[float]] = mapped_column(VECTOR())
    # The call that produced it: telemetry, not identity. Pruning it keeps the vector.
    model_call_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("model_calls.id", ondelete="SET NULL"), index=True
    )

    space: Mapped[EmbeddingSpaceRecord] = relationship(lazy="raise")


class ChunkEmbedding(CreatedAt, Base):
    """The explicit link from a chunk to its document-side vector in one space.

    ``space_id`` and ``input_format`` are copied from the embedding
    (fk_chunk_embeddings_embedding proves they match) so that "one vector per
    chunk, space and format" is a plain unique constraint.
    """

    __tablename__ = "chunk_embeddings"
    __table_args__ = (
        ForeignKeyConstraint(
            ["embedding_id", "space_id", "input_format"],
            ["embeddings.id", "embeddings.space_id", "embeddings.input_format"],
            name="fk_chunk_embeddings_embedding",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "chunk_id", "space_id", "input_format", name="uq_chunk_embeddings_chunk_space_format"
        ),
        # Query embeddings exist only at retrieval time, never attached to a chunk.
        CheckConstraint(DOCUMENT_FORMAT_SQL, name="document_format_only"),
        Index("ix_chunk_embeddings_embedding_id", "embedding_id"),
    )

    chunk_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("chunks.id", ondelete="CASCADE"), primary_key=True
    )
    embedding_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    space_id: Mapped[uuid.UUID]
    input_format: Mapped[str] = mapped_column(Text)


# --- academic layer -------------------------------------------------------------------


class Assessment(UUIDPrimaryKey, Timestamps, Base):
    """An assessed deliverable or milestone of a course, once per course.

    ``canonical_key`` is the deterministic normal form of its title: the
    identity that merges the same assessment stated by several sources.
    ``status`` is derived from its evidence; only ``verified`` is shown as fact.
    """

    __tablename__ = "assessments"
    __table_args__ = (
        UniqueConstraint("course_id", "canonical_key", name="uq_assessments_course_key"),
        CheckConstraint(
            "length(canonical_key) > 0 AND canonical_key = lower(canonical_key)",
            name="canonical_key_normalised",
        ),
        CheckConstraint("length(btrim(title)) > 0", name="title_nonblank"),
        CheckConstraint("weighting_percent BETWEEN 0 AND 100", name="weighting_percent_range"),
    )

    # Lookups by course use the unique constraint (leading column).
    course_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("courses.id"))
    canonical_key: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    weighting_percent: Mapped[int | None] = mapped_column(SmallInteger)
    status: Mapped[VerificationStatus] = mapped_column(
        str_enum(VerificationStatus, "verification_status")
    )

    course: Mapped[Course] = relationship(lazy="raise")
    deadlines: Mapped[list[AssessmentDeadline]] = relationship(
        back_populates="assessment", lazy="raise"
    )


class AssessmentDeadline(UUIDPrimaryKey, Timestamps, Base):
    """One deadline value that a source states for an assessment.

    Stored as the source states it: a local calendar date, an optional local
    time and an IANA timezone. ``due_at`` is the instant, derived
    deterministically only when date, time and timezone are all known. A date
    with no time stays ``precision = date`` with no instant: no time of day is
    ever invented. Sources that disagree give one row each (the disagreement
    is preserved); sources that agree share one row with one link each.
    """

    __tablename__ = "assessment_deadlines"
    __table_args__ = (
        UniqueConstraint(
            "assessment_id",
            "due_date",
            "due_time",
            "timezone",
            name="uq_assessment_deadlines_value",
            postgresql_nulls_not_distinct=True,
        ),
        CheckConstraint("(precision = 'date') = (due_time IS NULL)", name="time_fits_precision"),
        CheckConstraint("(timezone IS NULL) = (timezone_source IS NULL)", name="timezone_sourced"),
        CheckConstraint("length(btrim(timezone)) > 0", name="timezone_nonblank"),
        CheckConstraint(
            "due_at IS NULL OR (precision = 'datetime' AND timezone IS NOT NULL)",
            name="instant_needs_time_and_timezone",
        ),
        CheckConstraint(
            "status <> 'verified' OR precision = 'date' OR due_at IS NOT NULL",
            name="verified_datetime_has_instant",
        ),
    )

    assessment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("assessments.id", ondelete="CASCADE")
    )
    due_date: Mapped[date]
    due_time: Mapped[time | None]
    timezone: Mapped[str | None] = mapped_column(Text)
    timezone_source: Mapped[TimezoneSource | None] = mapped_column(
        str_enum(TimezoneSource, "timezone_source")
    )
    precision: Mapped[DeadlinePrecision] = mapped_column(
        str_enum(DeadlinePrecision, "deadline_precision")
    )
    due_at: Mapped[datetime | None]
    status: Mapped[VerificationStatus] = mapped_column(
        str_enum(VerificationStatus, "verification_status")
    )

    assessment: Mapped[Assessment] = relationship(back_populates="deadlines", lazy="raise")


# --- trust layer ----------------------------------------------------------------------

EVIDENCE_SUBJECT_SQL = (
    "(subject_type = 'assessment' AND assessment_id IS NOT NULL"
    " AND assessment_deadline_id IS NULL)"
    " OR (subject_type = 'assessment_deadline' AND assessment_deadline_id IS NOT NULL"
    " AND assessment_id IS NULL)"
)


class EvidenceLink(UUIDPrimaryKey, CreatedAt, Base):
    """Which SourceSpan supports one fact of a claim, and how it was verified.

    The supporting excerpt is ``source_span.excerpt[quote_start:quote_end]``
    (code points), checked by the evidence gate; a location is never taken
    from the model. Lineage: the span gives the ResourceVersion, the
    ModelCall the model and prompt version, the WorkflowRun the run. Exactly
    one subject column is set: the one ``subject_type`` names.
    """

    __tablename__ = "evidence_links"
    __table_args__ = (
        CheckConstraint(EVIDENCE_SUBJECT_SQL, name="one_subject"),
        CheckConstraint(
            "(field = 'deadline') = (subject_type = 'assessment_deadline')",
            name="field_fits_subject",
        ),
        CheckConstraint("quote_start >= 0 AND quote_start < quote_end", name="quote_ordered"),
        UniqueConstraint(
            "assessment_id",
            "assessment_deadline_id",
            "field",
            "source_span_id",
            "quote_start",
            "quote_end",
            name="uq_evidence_links_fact_quote",
            postgresql_nulls_not_distinct=True,
        ),
        Index("ix_evidence_links_assessment_deadline_id", "assessment_deadline_id"),
    )

    subject_type: Mapped[EvidenceSubject] = mapped_column(
        str_enum(EvidenceSubject, "evidence_subject")
    )
    # Lookups by assessment use the unique constraint (leading column).
    assessment_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("assessments.id", ondelete="CASCADE")
    )
    assessment_deadline_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("assessment_deadlines.id", ondelete="CASCADE")
    )
    field: Mapped[EvidenceField] = mapped_column(str_enum(EvidenceField, "evidence_field"))
    source_span_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("source_spans.id"), index=True)
    quote_start: Mapped[int]
    quote_end: Mapped[int]
    verification_status: Mapped[VerificationStatus] = mapped_column(
        str_enum(VerificationStatus, "verification_status")
    )
    # NULL only for evidence no model produced. No cascade: a call or run
    # cannot be deleted while claims still rest on it.
    model_call_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("model_calls.id"), index=True
    )
    workflow_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("workflow_runs.id"), index=True
    )

    source_span: Mapped[SourceSpan] = relationship(lazy="raise")
