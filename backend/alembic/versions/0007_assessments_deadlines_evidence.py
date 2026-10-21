# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""academic and trust layers: assessments, assessment deadlines, evidence links;
institutions.timezone

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-20

- assessments: one canonical row per (course, canonical_key).
- assessment_deadlines: one row per deadline value a source states, as a
  local date, an optional local time and an IANA timezone, with how the
  timezone was obtained; ``due_at`` only when all three are known. Disagreeing sources give separate rows.
- review_reason on all three: why a needs_review row needs review (a typed,
  gate-derived code); present exactly when the status is needs_review.
- evidence_links: one row per (fact, span, quote range) supporting an
  assessment or a deadline, with its verification status and ModelCall /
  WorkflowRun lineage. No cascade from spans, calls or runs: evidence must
  be removed with its claim, never orphaned.
- institutions.timezone: nullable; existing rows keep NULL (unknown).

New tables only, so existing data is untouched. Downgrade drops them, and
every assessment, deadline and evidence link in them, and the column.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0007"
down_revision: str | Sequence[str] | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STATUS = "IN ('verified', 'needs_review', 'rejected')"
_REASONS = (
    "review_reason IN ("
    "'no_verified_assessment_fact', 'conflicting_weighting', 'non_unique_quote_location', 'missing_timezone', 'unrecognised_timezone', 'conflicting_timezone', 'weekday_mismatch', 'ambiguous_time', 'ambiguous_local_time'"
    ")"
)


def _reason_checks(table: str, status: str) -> list[sa.CheckConstraint]:
    return [
        sa.CheckConstraint(_REASONS, name=op.f(f"ck_{table}_review_reason")),
        sa.CheckConstraint(
            f"(review_reason IS NOT NULL) = ({status} = 'needs_review')",
            name=op.f(f"ck_{table}_review_reason_iff_review"),
        ),
    ]


def _timestamps(*, updated: bool = True) -> list[sa.Column]:  # type: ignore[type-arg]
    columns = [
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        )
    ]
    if updated:
        columns.append(
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("now()"),
                nullable=False,
            )
        )
    return columns


def upgrade() -> None:
    op.add_column("institutions", sa.Column("timezone", sa.Text(), nullable=True))
    op.create_check_constraint(
        op.f("ck_institutions_timezone_nonblank"), "institutions", "length(btrim(timezone)) > 0"
    )

    op.create_table(
        "assessments",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("course_id", sa.Uuid(), nullable=False),
        sa.Column("canonical_key", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("weighting_percent", sa.SmallInteger(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("review_reason", sa.String(length=32), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(f"status {_STATUS}", name=op.f("ck_assessments_verification_status")),
        *_reason_checks("assessments", "status"),
        sa.CheckConstraint(
            "length(canonical_key) > 0 AND canonical_key = lower(canonical_key)",
            name=op.f("ck_assessments_canonical_key_normalised"),
        ),
        sa.CheckConstraint("length(btrim(title)) > 0", name=op.f("ck_assessments_title_nonblank")),
        sa.CheckConstraint(
            "weighting_percent BETWEEN 0 AND 100",
            name=op.f("ck_assessments_weighting_percent_range"),
        ),
        sa.ForeignKeyConstraint(
            ["course_id"], ["courses.id"], name=op.f("fk_assessments_course_id_courses")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_assessments")),
        sa.UniqueConstraint("course_id", "canonical_key", name="uq_assessments_course_key"),
    )

    op.create_table(
        "assessment_deadlines",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("assessment_id", sa.Uuid(), nullable=False),
        sa.Column("due_date", sa.Date(), nullable=False),
        sa.Column("due_time", sa.Time(), nullable=True),
        sa.Column("timezone", sa.Text(), nullable=True),
        sa.Column("timezone_source", sa.String(length=32), nullable=False),
        sa.Column("precision", sa.String(length=32), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("review_reason", sa.String(length=32), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            f"status {_STATUS}", name=op.f("ck_assessment_deadlines_verification_status")
        ),
        *_reason_checks("assessment_deadlines", "status"),
        sa.CheckConstraint(
            "timezone_source IN ('explicit_source', 'institution_default', 'unknown')",
            name=op.f("ck_assessment_deadlines_timezone_source"),
        ),
        sa.CheckConstraint(
            "precision IN ('datetime', 'date')",
            name=op.f("ck_assessment_deadlines_deadline_precision"),
        ),
        sa.CheckConstraint(
            "(precision = 'date') = (due_time IS NULL)",
            name=op.f("ck_assessment_deadlines_time_fits_precision"),
        ),
        sa.CheckConstraint(
            "(timezone IS NULL) = (timezone_source = 'unknown')",
            name=op.f("ck_assessment_deadlines_timezone_sourced"),
        ),
        sa.CheckConstraint(
            "length(btrim(timezone)) > 0", name=op.f("ck_assessment_deadlines_timezone_nonblank")
        ),
        sa.CheckConstraint(
            "due_at IS NULL OR (precision = 'datetime' AND timezone IS NOT NULL)",
            name=op.f("ck_assessment_deadlines_instant_needs_time_and_timezone"),
        ),
        sa.CheckConstraint(
            "status <> 'verified' OR precision = 'date' OR due_at IS NOT NULL",
            name=op.f("ck_assessment_deadlines_verified_datetime_has_instant"),
        ),
        sa.ForeignKeyConstraint(
            ["assessment_id"],
            ["assessments.id"],
            name=op.f("fk_assessment_deadlines_assessment_id_assessments"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_assessment_deadlines")),
        sa.UniqueConstraint(
            "assessment_id",
            "due_date",
            "due_time",
            "timezone",
            name="uq_assessment_deadlines_value",
            postgresql_nulls_not_distinct=True,
        ),
    )

    op.create_table(
        "evidence_links",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("subject_type", sa.String(length=32), nullable=False),
        sa.Column("assessment_id", sa.Uuid(), nullable=True),
        sa.Column("assessment_deadline_id", sa.Uuid(), nullable=True),
        sa.Column("field", sa.String(length=32), nullable=False),
        sa.Column("source_span_id", sa.Uuid(), nullable=False),
        sa.Column("quote_start", sa.Integer(), nullable=False),
        sa.Column("quote_end", sa.Integer(), nullable=False),
        sa.Column("verification_status", sa.String(length=32), nullable=False),
        sa.Column("review_reason", sa.String(length=32), nullable=True),
        sa.Column("model_call_id", sa.Uuid(), nullable=True),
        sa.Column("workflow_run_id", sa.Uuid(), nullable=True),
        *_timestamps(updated=False),
        sa.CheckConstraint(
            "subject_type IN ('assessment', 'assessment_deadline')",
            name=op.f("ck_evidence_links_evidence_subject"),
        ),
        sa.CheckConstraint(
            "field IN ('title', 'weighting', 'deadline')",
            name=op.f("ck_evidence_links_evidence_field"),
        ),
        sa.CheckConstraint(
            f"verification_status {_STATUS}", name=op.f("ck_evidence_links_verification_status")
        ),
        sa.CheckConstraint(
            "(subject_type = 'assessment' AND assessment_id IS NOT NULL"
            " AND assessment_deadline_id IS NULL)"
            " OR (subject_type = 'assessment_deadline' AND assessment_deadline_id IS NOT NULL"
            " AND assessment_id IS NULL)",
            name=op.f("ck_evidence_links_one_subject"),
        ),
        sa.CheckConstraint(
            "(field = 'deadline') = (subject_type = 'assessment_deadline')",
            name=op.f("ck_evidence_links_field_fits_subject"),
        ),
        sa.CheckConstraint(
            "quote_start >= 0 AND quote_start < quote_end",
            name=op.f("ck_evidence_links_quote_ordered"),
        ),
        *_reason_checks("evidence_links", "verification_status"),
        sa.ForeignKeyConstraint(
            ["assessment_id"],
            ["assessments.id"],
            name=op.f("fk_evidence_links_assessment_id_assessments"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["assessment_deadline_id"],
            ["assessment_deadlines.id"],
            name=op.f("fk_evidence_links_assessment_deadline_id_assessment_deadlines"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_span_id"],
            ["source_spans.id"],
            name=op.f("fk_evidence_links_source_span_id_source_spans"),
        ),
        sa.ForeignKeyConstraint(
            ["model_call_id"],
            ["model_calls.id"],
            name=op.f("fk_evidence_links_model_call_id_model_calls"),
        ),
        sa.ForeignKeyConstraint(
            ["workflow_run_id"],
            ["workflow_runs.id"],
            name=op.f("fk_evidence_links_workflow_run_id_workflow_runs"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_evidence_links")),
        sa.UniqueConstraint(
            "assessment_id",
            "assessment_deadline_id",
            "field",
            "source_span_id",
            "quote_start",
            "quote_end",
            name="uq_evidence_links_fact_quote",
            postgresql_nulls_not_distinct=True,
        ),
    )
    op.create_index(
        "ix_evidence_links_assessment_deadline_id", "evidence_links", ["assessment_deadline_id"]
    )
    op.create_index(op.f("ix_evidence_links_source_span_id"), "evidence_links", ["source_span_id"])
    op.create_index(op.f("ix_evidence_links_model_call_id"), "evidence_links", ["model_call_id"])
    op.create_index(
        op.f("ix_evidence_links_workflow_run_id"), "evidence_links", ["workflow_run_id"]
    )


def downgrade() -> None:
    op.drop_table("evidence_links")  # its indexes go with it
    op.drop_table("assessment_deadlines")
    op.drop_table("assessments")
    op.drop_constraint(op.f("ck_institutions_timezone_nonblank"), "institutions", type_="check")
    op.drop_column("institutions", "timezone")
