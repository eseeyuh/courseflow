# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""initial schema: pgvector, source/provenance tables and workflow lineage

Revision ID: 0001
Revises:
Create Date: 2026-10-06

Drafted with --autogenerate, then reviewed and corrected by hand:
- CREATE EXTENSION vector added (autogenerate does not manage extensions).
- Enum columns written as VARCHAR(32) + one explicitly named CHECK each
  (the draft emitted duplicate CHECKs and failed on apply).
- fk_resources_current_version (resources <-> resource_versions cycle) is
  created after both tables exist; op.create_table ignores use_alter.
- downgrade() drops in reverse dependency order, cycle FK first.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _id() -> sa.Column:
    return sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False)


def _ts(name: str) -> sa.Column:
    return sa.Column(
        name, sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
    )


def upgrade() -> None:
    # Vector similarity search for chunk embeddings (columns arrive later).
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "institutions",
        _id(),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("lms_type", sa.String(length=32), nullable=False),
        _ts("created_at"),
        _ts("updated_at"),
        sa.CheckConstraint(
            "lms_type IN ('demo', 'upload', 'moodle')",
            name=op.f("ck_institutions_lms_type"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_institutions")),
    )

    op.create_table(
        "courses",
        _id(),
        sa.Column("institution_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("external_id", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=32), server_default="active", nullable=False),
        _ts("created_at"),
        _ts("updated_at"),
        sa.CheckConstraint(
            "status IN ('active', 'archived')", name=op.f("ck_courses_course_status")
        ),
        sa.ForeignKeyConstraint(
            ["institution_id"],
            ["institutions.id"],
            name=op.f("fk_courses_institution_id_institutions"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_courses")),
        sa.UniqueConstraint(
            "institution_id", "external_id", name="uq_courses_institution_external"
        ),
    )

    op.create_table(
        "resources",
        _id(),
        sa.Column("course_id", sa.Uuid(), nullable=False),
        sa.Column("type", sa.String(length=32), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("source_uri", sa.Text(), nullable=True),
        # FK added below, once resource_versions exists.
        sa.Column("current_version_id", sa.Uuid(), nullable=True),
        _ts("created_at"),
        _ts("updated_at"),
        sa.CheckConstraint(
            "type IN ('lecture', 'lab', 'reading', 'workshop', 'handbook', 'brief', "
            "'announcement', 'page')",
            name=op.f("ck_resources_resource_type"),
        ),
        sa.ForeignKeyConstraint(
            ["course_id"], ["courses.id"], name=op.f("fk_resources_course_id_courses")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_resources")),
    )
    op.create_index(op.f("ix_resources_course_id"), "resources", ["course_id"])
    op.create_index(op.f("ix_resources_current_version_id"), "resources", ["current_version_id"])

    op.create_table(
        "resource_versions",
        _id(),
        sa.Column("resource_id", sa.Uuid(), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column("raw_text_ref", sa.Text(), nullable=True),
        _ts("created_at"),
        sa.CheckConstraint(
            "version_number >= 1", name=op.f("ck_resource_versions_version_number_positive")
        ),
        sa.CheckConstraint(
            "content_hash ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_resource_versions_content_hash_sha256_hex"),
        ),
        sa.ForeignKeyConstraint(
            ["resource_id"],
            ["resources.id"],
            name=op.f("fk_resource_versions_resource_id_resources"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_resource_versions")),
        sa.UniqueConstraint("resource_id", "version_number", name="uq_resource_versions_number"),
        sa.UniqueConstraint("id", "resource_id", name="uq_resource_versions_id_resource"),
    )
    op.create_index(
        "ix_resource_versions_resource_hash", "resource_versions", ["resource_id", "content_hash"]
    )

    # Close the cycle: a resource's current version must be one of ITS versions.
    op.create_foreign_key(
        "fk_resources_current_version",
        "resources",
        "resource_versions",
        ["current_version_id", "id"],
        ["id", "resource_id"],
    )

    op.create_table(
        "source_spans",
        _id(),
        sa.Column("resource_version_id", sa.Uuid(), nullable=False),
        # Human/source locators (at least one required)
        sa.Column("page_number", sa.Integer(), nullable=True),
        sa.Column("slide_number", sa.Integer(), nullable=True),
        sa.Column("section_path", sa.Text(), nullable=True),
        sa.Column("timestamp_seconds", sa.Numeric(precision=10, scale=3), nullable=True),
        # Machine provenance inside the extracted text (both or neither)
        sa.Column("start_offset", sa.Integer(), nullable=True),
        sa.Column("end_offset", sa.Integer(), nullable=True),
        # Evidence
        sa.Column("excerpt", sa.Text(), nullable=False),
        _ts("created_at"),
        sa.CheckConstraint(
            "page_number IS NOT NULL OR slide_number IS NOT NULL "
            "OR section_path IS NOT NULL OR timestamp_seconds IS NOT NULL",
            name=op.f("ck_source_spans_locator_present"),
        ),
        sa.CheckConstraint("page_number >= 1", name=op.f("ck_source_spans_page_positive")),
        sa.CheckConstraint("slide_number >= 1", name=op.f("ck_source_spans_slide_positive")),
        sa.CheckConstraint(
            "length(btrim(section_path)) > 0", name=op.f("ck_source_spans_section_nonblank")
        ),
        sa.CheckConstraint(
            "timestamp_seconds >= 0", name=op.f("ck_source_spans_timestamp_nonnegative")
        ),
        sa.CheckConstraint(
            "(start_offset IS NULL) = (end_offset IS NULL)",
            name=op.f("ck_source_spans_offsets_paired"),
        ),
        sa.CheckConstraint(
            "start_offset >= 0 AND start_offset < end_offset",
            name=op.f("ck_source_spans_offsets_ordered"),
        ),
        sa.CheckConstraint("length(excerpt) > 0", name=op.f("ck_source_spans_excerpt_nonempty")),
        sa.ForeignKeyConstraint(
            ["resource_version_id"],
            ["resource_versions.id"],
            name=op.f("fk_source_spans_resource_version_id_resource_versions"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_source_spans")),
    )
    op.create_index(
        op.f("ix_source_spans_resource_version_id"), "source_spans", ["resource_version_id"]
    )

    op.create_table(
        "workflow_runs",
        _id(),
        sa.Column("workflow_version", sa.Text(), nullable=False),
        sa.Column("course_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="pending", nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        _ts("created_at"),
        _ts("updated_at"),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'failed')",
            name=op.f("ck_workflow_runs_workflow_run_status"),
        ),
        sa.CheckConstraint(
            "finished_at IS NULL OR (started_at IS NOT NULL AND finished_at >= started_at)",
            name=op.f("ck_workflow_runs_finished_after_started"),
        ),
        sa.ForeignKeyConstraint(
            ["course_id"], ["courses.id"], name=op.f("fk_workflow_runs_course_id_courses")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workflow_runs")),
    )
    op.create_index(op.f("ix_workflow_runs_course_id"), "workflow_runs", ["course_id"])

    op.create_table(
        "workflow_run_inputs",
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("resource_version_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["workflow_runs.id"],
            name=op.f("fk_workflow_run_inputs_run_id_workflow_runs"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["resource_version_id"],
            ["resource_versions.id"],
            name=op.f("fk_workflow_run_inputs_resource_version_id_resource_versions"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "run_id", "resource_version_id", name=op.f("pk_workflow_run_inputs")
        ),
    )
    op.create_index(
        op.f("ix_workflow_run_inputs_resource_version_id"),
        "workflow_run_inputs",
        ["resource_version_id"],
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_workflow_run_inputs_resource_version_id"), table_name="workflow_run_inputs"
    )
    op.drop_table("workflow_run_inputs")
    op.drop_index(op.f("ix_workflow_runs_course_id"), table_name="workflow_runs")
    op.drop_table("workflow_runs")
    op.drop_index(op.f("ix_source_spans_resource_version_id"), table_name="source_spans")
    op.drop_table("source_spans")
    # Break the cycle before dropping either side of it.
    op.drop_constraint("fk_resources_current_version", "resources", type_="foreignkey")
    op.drop_index("ix_resource_versions_resource_hash", table_name="resource_versions")
    op.drop_table("resource_versions")
    op.drop_index(op.f("ix_resources_current_version_id"), table_name="resources")
    op.drop_index(op.f("ix_resources_course_id"), table_name="resources")
    op.drop_table("resources")
    op.drop_table("courses")
    op.drop_table("institutions")
    op.execute("DROP EXTENSION IF EXISTS vector")
