# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""academic layer: assessment requirements and obligations; evidence for them

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-21

- assessment_requirements: one row per (assessment, canonical_key), the
  SHA-256 of the normal form of the verbatim source statement.
- obligations: one row per (course, canonical_key), with the model's
  canonical action and narrow category, the gate's modality and status, an
  optional explicit due date typed like an assessment deadline, and the
  proposing ModelCall. A related assessment must be of the same course
  (composite foreign key onto the new uq_assessments_id_course).
- evidence_links: two more subject columns and subject types, the fields
  ``requirement``, ``obligation`` and ``due_date``; the subject and field
  checks and the fact-quote uniqueness are rebuilt to cover them.
- review_reason: new gate-derived codes on all five claim tables.

Existing rows satisfy every rebuilt check unchanged. Downgrade deletes all
requirements, obligations and their evidence, and refuses (live database
only) while an assessment, deadline or link uses a code 0007 does not know.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

# revision identifiers, used by Alembic.
revision: str = "0008"
down_revision: str | Sequence[str] | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STATUS = "IN ('verified', 'needs_review', 'rejected')"
_REASONS_0007 = (
    "'no_verified_assessment_fact', 'conflicting_weighting', 'non_unique_quote_location', "
    "'missing_timezone', 'unrecognised_timezone', 'conflicting_timezone', "
    "'weekday_mismatch', 'ambiguous_time', 'ambiguous_local_time'"
)
_REASONS_NEW = (
    "'permissive_wording', 'mixed_modality', 'no_requirement_language', "
    "'assessment_link_unsupported', 'category_unsupported', 'conflicting_category', "
    "'conflicting_modality', 'conflicting_assessment_link', 'conflicting_due_date'"
)
_REASONS = f"review_reason IN ({_REASONS_0007}, {_REASONS_NEW})"
# Tables whose review_reason check 0007 created.
_REASONED_0007 = ("assessments", "assessment_deadlines", "evidence_links")

_SUBJECT_0007 = (
    "(subject_type = 'assessment' AND assessment_id IS NOT NULL"
    " AND assessment_deadline_id IS NULL)"
    " OR (subject_type = 'assessment_deadline' AND assessment_deadline_id IS NOT NULL"
    " AND assessment_id IS NULL)"
)
_SUBJECT = (
    "(subject_type = 'assessment') = (assessment_id IS NOT NULL)"
    " AND (subject_type = 'assessment_deadline') = (assessment_deadline_id IS NOT NULL)"
    " AND (subject_type = 'assessment_requirement') = (assessment_requirement_id IS NOT NULL)"
    " AND (subject_type = 'obligation') = (obligation_id IS NOT NULL)"
)
_FIELD_FITS_0007 = "(field = 'deadline') = (subject_type = 'assessment_deadline')"
_FIELD_FITS = (
    "(subject_type = 'assessment' AND field IN ('title', 'weighting'))"
    " OR (subject_type = 'assessment_deadline' AND field = 'deadline')"
    " OR (subject_type = 'assessment_requirement' AND field = 'requirement')"
    " OR (subject_type = 'obligation' AND field IN ('obligation', 'due_date'))"
)
_SUBJECT_TYPES_0007 = "subject_type IN ('assessment', 'assessment_deadline')"
_SUBJECT_TYPES = (
    "subject_type IN ('assessment', 'assessment_deadline', 'assessment_requirement', 'obligation')"
)
_FIELDS_0007 = "field IN ('title', 'weighting', 'deadline')"
_FIELDS = "field IN ('title', 'weighting', 'deadline', 'requirement', 'obligation', 'due_date')"
_FACT_QUOTE_0007 = (
    "assessment_id",
    "assessment_deadline_id",
    "field",
    "source_span_id",
    "quote_start",
    "quote_end",
)
_FACT_QUOTE = (
    "assessment_id",
    "assessment_deadline_id",
    "assessment_requirement_id",
    "obligation_id",
    "field",
    "source_span_id",
    "quote_start",
    "quote_end",
)
_CANONICAL_KEY = "canonical_key ~ '^[0-9a-f]{64}$'"


class MigrationPreconditionError(RuntimeError):
    """The database holds data this migration step cannot convert safely."""


def _reason_checks(table: str) -> list[sa.CheckConstraint]:
    return [
        sa.CheckConstraint(_REASONS, name=op.f(f"ck_{table}_review_reason")),
        sa.CheckConstraint(
            "(review_reason IS NOT NULL) = (status = 'needs_review')",
            name=op.f(f"ck_{table}_review_reason_iff_review"),
        ),
    ]


def _timestamps() -> list[sa.Column]:  # type: ignore[type-arg]
    return [
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    ]


def _replace_check(table: str, name: str, condition: str) -> None:
    op.drop_constraint(op.f(f"ck_{table}_{name}"), table, type_="check")
    op.create_check_constraint(op.f(f"ck_{table}_{name}"), table, condition)


def _replace_fact_quote(columns: Sequence[str]) -> None:
    op.drop_constraint("uq_evidence_links_fact_quote", "evidence_links", type_="unique")
    op.create_unique_constraint(
        "uq_evidence_links_fact_quote",
        "evidence_links",
        list(columns),
        postgresql_nulls_not_distinct=True,
    )


def upgrade() -> None:
    for table in _REASONED_0007:
        _replace_check(table, "review_reason", _REASONS)
    op.create_unique_constraint("uq_assessments_id_course", "assessments", ["id", "course_id"])

    op.create_table(
        "assessment_requirements",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("assessment_id", sa.Uuid(), nullable=False),
        sa.Column("canonical_key", sa.Text(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("review_reason", sa.String(length=32), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            f"status {_STATUS}", name=op.f("ck_assessment_requirements_verification_status")
        ),
        *_reason_checks("assessment_requirements"),
        sa.CheckConstraint(
            _CANONICAL_KEY, name=op.f("ck_assessment_requirements_canonical_key_sha256_hex")
        ),
        sa.CheckConstraint(
            "length(btrim(text)) > 0 AND length(text) <= 2000",
            name=op.f("ck_assessment_requirements_text_nonblank_bounded"),
        ),
        sa.ForeignKeyConstraint(
            ["assessment_id"],
            ["assessments.id"],
            name=op.f("fk_assessment_requirements_assessment_id_assessments"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_assessment_requirements")),
        sa.UniqueConstraint(
            "assessment_id", "canonical_key", name="uq_assessment_requirements_assessment_key"
        ),
    )

    op.create_table(
        "obligations",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("course_id", sa.Uuid(), nullable=False),
        sa.Column("assessment_id", sa.Uuid(), nullable=True),
        sa.Column("canonical_key", sa.Text(), nullable=False),
        sa.Column("canonical_action", sa.Text(), nullable=False),
        sa.Column("category", sa.String(length=32), nullable=False),
        sa.Column("modality", sa.String(length=32), nullable=True),
        sa.Column("due_date", sa.Date(), nullable=True),
        sa.Column("due_time", sa.Time(), nullable=True),
        sa.Column("due_timezone", sa.Text(), nullable=True),
        sa.Column("due_timezone_source", sa.String(length=32), nullable=True),
        sa.Column("due_precision", sa.String(length=32), nullable=True),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("review_reason", sa.String(length=32), nullable=True),
        sa.Column("model_call_id", sa.Uuid(), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(f"status {_STATUS}", name=op.f("ck_obligations_verification_status")),
        *_reason_checks("obligations"),
        sa.CheckConstraint(
            "category IN ('supervisor_review', 'approval', 'ethics_admin_form', "
            "'formative_prerequisite', 'submission_prerequisite', 'other')",
            name=op.f("ck_obligations_obligation_category"),
        ),
        sa.CheckConstraint(
            "modality IN ('mandatory', 'recommended')",
            name=op.f("ck_obligations_obligation_modality"),
        ),
        sa.CheckConstraint(
            "due_timezone_source IN ('explicit_source', 'institution_default', 'unknown')",
            name=op.f("ck_obligations_timezone_source"),
        ),
        sa.CheckConstraint(
            "due_precision IN ('datetime', 'date')", name=op.f("ck_obligations_deadline_precision")
        ),
        sa.CheckConstraint(_CANONICAL_KEY, name=op.f("ck_obligations_canonical_key_sha256_hex")),
        sa.CheckConstraint(
            "length(btrim(canonical_action)) > 0 AND length(canonical_action) <= 300",
            name=op.f("ck_obligations_canonical_action_nonblank_bounded"),
        ),
        sa.CheckConstraint(
            "status <> 'verified' OR modality IS NOT NULL",
            name=op.f("ck_obligations_verified_has_modality"),
        ),
        sa.CheckConstraint(
            "due_date IS NOT NULL OR (due_time IS NULL AND due_timezone IS NULL"
            " AND due_timezone_source IS NULL AND due_precision IS NULL AND due_at IS NULL)",
            name=op.f("ck_obligations_due_fields_need_date"),
        ),
        sa.CheckConstraint(
            "due_date IS NULL OR (due_precision IS NOT NULL AND due_timezone_source IS NOT NULL)",
            name=op.f("ck_obligations_due_date_typed"),
        ),
        sa.CheckConstraint(
            "(due_precision = 'date') = (due_time IS NULL)",
            name=op.f("ck_obligations_due_time_fits_precision"),
        ),
        sa.CheckConstraint(
            "(due_timezone IS NULL) = (due_timezone_source = 'unknown')",
            name=op.f("ck_obligations_due_timezone_sourced"),
        ),
        sa.CheckConstraint(
            "length(btrim(due_timezone)) > 0", name=op.f("ck_obligations_due_timezone_nonblank")
        ),
        sa.CheckConstraint(
            "due_at IS NULL OR (due_precision = 'datetime' AND due_timezone IS NOT NULL)",
            name=op.f("ck_obligations_due_instant_needs_time_and_timezone"),
        ),
        sa.CheckConstraint(
            "status <> 'verified' OR due_precision IS DISTINCT FROM 'datetime'"
            " OR due_at IS NOT NULL",
            name=op.f("ck_obligations_verified_due_datetime_has_instant"),
        ),
        sa.ForeignKeyConstraint(
            ["course_id"], ["courses.id"], name=op.f("fk_obligations_course_id_courses")
        ),
        sa.ForeignKeyConstraint(
            ["assessment_id", "course_id"],
            ["assessments.id", "assessments.course_id"],
            name="fk_obligations_assessment_same_course",
        ),
        sa.ForeignKeyConstraint(
            ["model_call_id"],
            ["model_calls.id"],
            name=op.f("fk_obligations_model_call_id_model_calls"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_obligations")),
        sa.UniqueConstraint("course_id", "canonical_key", name="uq_obligations_course_key"),
    )
    op.create_index("ix_obligations_assessment_id", "obligations", ["assessment_id"])
    op.create_index(op.f("ix_obligations_model_call_id"), "obligations", ["model_call_id"])

    op.add_column(
        "evidence_links", sa.Column("assessment_requirement_id", sa.Uuid(), nullable=True)
    )
    op.add_column("evidence_links", sa.Column("obligation_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        op.f("fk_evidence_links_assessment_requirement_id_assessment_requirements"),
        "evidence_links",
        "assessment_requirements",
        ["assessment_requirement_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        op.f("fk_evidence_links_obligation_id_obligations"),
        "evidence_links",
        "obligations",
        ["obligation_id"],
        ["id"],
        ondelete="CASCADE",
    )
    _replace_check("evidence_links", "evidence_subject", _SUBJECT_TYPES)
    _replace_check("evidence_links", "evidence_field", _FIELDS)
    _replace_check("evidence_links", "one_subject", _SUBJECT)
    _replace_check("evidence_links", "field_fits_subject", _FIELD_FITS)
    _replace_fact_quote(_FACT_QUOTE)
    op.create_index(
        "ix_evidence_links_assessment_requirement_id",
        "evidence_links",
        ["assessment_requirement_id"],
    )
    op.create_index("ix_evidence_links_obligation_id", "evidence_links", ["obligation_id"])


def _check_downgrade_preconditions() -> None:
    if context.is_offline_mode():
        raise MigrationPreconditionError(
            "0008: downgrade preconditions need a live database; run without --sql"
        )
    bind = op.get_bind()
    new_subjects = "subject_type IN ('assessment_requirement', 'obligation')"
    using_new = 0
    for table in _REASONED_0007:
        where = f"review_reason IN ({_REASONS_NEW})"
        if table == "evidence_links":
            where += f" AND NOT ({new_subjects})"
        using_new += int(bind.scalar(sa.text(f"SELECT count(*) FROM {table} WHERE {where}")) or 0)
    if using_new:
        raise MigrationPreconditionError(
            f"0008: {using_new} assessment, deadline or evidence row(s) use a review reason "
            "0007 does not know. Resolve or delete them, then downgrade."
        )


def downgrade() -> None:
    _check_downgrade_preconditions()

    # Evidence for requirements and obligations goes with them; nothing else changes.
    op.execute(
        "DELETE FROM evidence_links WHERE subject_type IN ('assessment_requirement', 'obligation')"
    )
    op.drop_index("ix_evidence_links_obligation_id", table_name="evidence_links")
    op.drop_index("ix_evidence_links_assessment_requirement_id", table_name="evidence_links")
    _replace_fact_quote(_FACT_QUOTE_0007)
    for name in ("one_subject", "field_fits_subject", "evidence_subject", "evidence_field"):
        op.drop_constraint(op.f(f"ck_evidence_links_{name}"), "evidence_links", type_="check")
    op.drop_column("evidence_links", "obligation_id")  # its foreign key goes with it
    op.drop_column("evidence_links", "assessment_requirement_id")
    op.create_check_constraint(
        op.f("ck_evidence_links_evidence_subject"), "evidence_links", _SUBJECT_TYPES_0007
    )
    op.create_check_constraint(
        op.f("ck_evidence_links_evidence_field"), "evidence_links", _FIELDS_0007
    )
    op.create_check_constraint(
        op.f("ck_evidence_links_one_subject"), "evidence_links", _SUBJECT_0007
    )
    op.create_check_constraint(
        op.f("ck_evidence_links_field_fits_subject"), "evidence_links", _FIELD_FITS_0007
    )

    op.drop_table("obligations")  # its indexes go with it
    op.drop_table("assessment_requirements")
    op.drop_constraint("uq_assessments_id_course", "assessments", type_="unique")
    for table in _REASONED_0007:
        _replace_check(table, "review_reason", f"review_reason IN ({_REASONS_0007})")
