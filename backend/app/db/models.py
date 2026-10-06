# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""ORM models for the source/provenance layer and workflow lineage.

Provenance chain (docs/architecture/data-contract.md):
    Institution -> Course -> Resource -> ResourceVersion -> SourceSpan
WorkflowRun records which ResourceVersions a run consumed.

All relationships use lazy="raise": async code must load related objects
explicitly (e.g. selectinload) instead of triggering hidden I/O.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    Column,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Numeric,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, CreatedAt, Timestamps, UUIDPrimaryKey, str_enum
from app.domain.enums import CourseStatus, LmsType, ResourceType, WorkflowRunStatus


class Institution(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "institutions"

    name: Mapped[str] = mapped_column(Text)
    lms_type: Mapped[LmsType] = mapped_column(str_enum(LmsType, "lms_type"))

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
    source_uri: Mapped[str | None] = mapped_column(Text)
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
    """Immutable content snapshot of a Resource."""

    __tablename__ = "resource_versions"
    __table_args__ = (
        UniqueConstraint("resource_id", "version_number", name="uq_resource_versions_number"),
        # Target of fk_resources_current_version (id is already unique on its own).
        UniqueConstraint("id", "resource_id", name="uq_resource_versions_id_resource"),
        CheckConstraint("version_number >= 1", name="version_number_positive"),
        CheckConstraint("content_hash ~ '^[0-9a-f]{64}$'", name="content_hash_sha256_hex"),
        # Change detection: "have we seen this content for this resource?"
        # Not unique: content may legitimately revert to an earlier state.
        Index("ix_resource_versions_resource_hash", "resource_id", "content_hash"),
    )

    resource_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("resources.id"))
    version_number: Mapped[int]
    content_hash: Mapped[str] = mapped_column(Text)
    raw_text_ref: Mapped[str | None] = mapped_column(Text)

    resource: Mapped[Resource] = relationship(
        back_populates="versions",
        primaryjoin="ResourceVersion.resource_id == Resource.id",
        foreign_keys=[resource_id],
        lazy="raise",
    )
    spans: Mapped[list[SourceSpan]] = relationship(back_populates="resource_version", lazy="raise")


class SourceSpan(UUIDPrimaryKey, CreatedAt, Base):
    """An addressable piece of evidence inside one ResourceVersion.

    Human/source locators say where a person finds it (page, slide, section,
    time). Offsets locate it inside CourseFlow's extracted text. The excerpt
    is the verbatim evidence.
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
    )

    resource_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("resource_versions.id"), index=True
    )
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
    )

    workflow_version: Mapped[str] = mapped_column(Text)
    course_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("courses.id"), index=True)
    status: Mapped[WorkflowRunStatus] = mapped_column(
        str_enum(WorkflowRunStatus, "workflow_run_status"),
        default=WorkflowRunStatus.PENDING,
        server_default=WorkflowRunStatus.PENDING.value,
    )
    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]

    course: Mapped[Course] = relationship(lazy="raise")
    input_versions: Mapped[list[ResourceVersion]] = relationship(
        secondary=workflow_run_inputs, lazy="raise"
    )
