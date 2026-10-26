# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""reasoning layer: dependency edges and deadline conflicts

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-22

- dependency_edges: a directed ordering between two ends of one course,
  each an assessment (same course by composite foreign key), an obligation
  or a closed milestone; the rule that derived it, the obligation whose
  evidence states it and the run.
- conflicts + conflict_sides: one conflict per (assessment, field) holding
  every disagreeing deadline; no chosen value.
- evidence_links: a dependency_edge subject and the ``dependency`` field;
  the subject and field checks and the fact-quote uniqueness are rebuilt.

Existing rows satisfy every rebuilt check unchanged. Downgrade drops every
edge, its evidence and every conflict.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0010"
down_revision: str | Sequence[str] | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SUBJECT_TYPES_0008 = (
    "subject_type IN ('assessment', 'assessment_deadline', 'assessment_requirement', 'obligation')"
)
_SUBJECT_TYPES = (
    "subject_type IN ('assessment', 'assessment_deadline', 'assessment_requirement', "
    "'obligation', 'dependency_edge')"
)
_FIELDS_0008 = (
    "field IN ('title', 'weighting', 'deadline', 'requirement', 'obligation', 'due_date')"
)
_FIELDS = (
    "field IN ('title', 'weighting', 'deadline', 'requirement', 'obligation', 'due_date', "
    "'dependency')"
)
_SUBJECT_0008 = (
    "(subject_type = 'assessment') = (assessment_id IS NOT NULL)"
    " AND (subject_type = 'assessment_deadline') = (assessment_deadline_id IS NOT NULL)"
    " AND (subject_type = 'assessment_requirement') = (assessment_requirement_id IS NOT NULL)"
    " AND (subject_type = 'obligation') = (obligation_id IS NOT NULL)"
)
_SUBJECT = _SUBJECT_0008 + (
    " AND (subject_type = 'dependency_edge') = (dependency_edge_id IS NOT NULL)"
)
_FIELD_FITS_0008 = (
    "(subject_type = 'assessment' AND field IN ('title', 'weighting'))"
    " OR (subject_type = 'assessment_deadline' AND field = 'deadline')"
    " OR (subject_type = 'assessment_requirement' AND field = 'requirement')"
    " OR (subject_type = 'obligation' AND field IN ('obligation', 'due_date'))"
)
_FIELD_FITS = _FIELD_FITS_0008 + " OR (subject_type = 'dependency_edge' AND field = 'dependency')"
_FACT_QUOTE_0008 = (
    "assessment_id",
    "assessment_deadline_id",
    "assessment_requirement_id",
    "obligation_id",
    "field",
    "source_span_id",
    "quote_start",
    "quote_end",
)
_FACT_QUOTE = (*_FACT_QUOTE_0008[:4], "dependency_edge_id", *_FACT_QUOTE_0008[4:])
_KINDS = "IN ('assessment', 'obligation', 'milestone')"
_MILESTONES = "IN ('ethics_approval', 'data_collection')"


def _endpoint(end: str) -> str:
    return (
        f"({end}_kind = 'assessment') = ({end}_assessment_id IS NOT NULL)"
        f" AND ({end}_kind = 'obligation') = ({end}_obligation_id IS NOT NULL)"
        f" AND ({end}_kind = 'milestone') = ({end}_milestone IS NOT NULL)"
    )


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


def _replace_check(name: str, condition: str) -> None:
    op.drop_constraint(op.f(f"ck_evidence_links_{name}"), "evidence_links", type_="check")
    op.create_check_constraint(op.f(f"ck_evidence_links_{name}"), "evidence_links", condition)


def _replace_fact_quote(columns: Sequence[str]) -> None:
    op.drop_constraint("uq_evidence_links_fact_quote", "evidence_links", type_="unique")
    op.create_unique_constraint(
        "uq_evidence_links_fact_quote",
        "evidence_links",
        list(columns),
        postgresql_nulls_not_distinct=True,
    )


def _ends(end: str) -> list[sa.Column]:  # type: ignore[type-arg]
    return [
        sa.Column(f"{end}_kind", sa.String(length=32), nullable=False),
        sa.Column(f"{end}_assessment_id", sa.Uuid(), nullable=True),
        sa.Column(f"{end}_obligation_id", sa.Uuid(), nullable=True),
        sa.Column(f"{end}_milestone", sa.String(length=32), nullable=True),
    ]


def _end_constraints(end: str) -> list[sa.Constraint]:
    return [
        sa.CheckConstraint(f"{end}_kind {_KINDS}", name=op.f(f"ck_dependency_edges_{end}_kind")),
        sa.CheckConstraint(
            f"{end}_milestone {_MILESTONES}", name=op.f(f"ck_dependency_edges_{end}_milestone")
        ),
        sa.CheckConstraint(_endpoint(end), name=op.f(f"ck_dependency_edges_{end}_one_end")),
        sa.ForeignKeyConstraint(
            [f"{end}_assessment_id", "course_id"],
            ["assessments.id", "assessments.course_id"],
            name=f"fk_dependency_edges_{end}_same_course",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            [f"{end}_obligation_id"],
            ["obligations.id"],
            name=op.f(f"fk_dependency_edges_{end}_obligation_id_obligations"),
            ondelete="CASCADE",
        ),
    ]


def upgrade() -> None:
    op.create_table(
        "dependency_edges",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("course_id", sa.Uuid(), nullable=False),
        sa.Column("relation", sa.String(length=32), nullable=False),
        *_ends("prerequisite"),
        *_ends("dependent"),
        sa.Column("rule", sa.Text(), nullable=False),
        sa.Column("derived_from_obligation_id", sa.Uuid(), nullable=False),
        sa.Column("workflow_run_id", sa.Uuid(), nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "relation IN ('before', 'required_for')",
            name=op.f("ck_dependency_edges_dependency_relation"),
        ),
        *_end_constraints("prerequisite"),
        *_end_constraints("dependent"),
        sa.CheckConstraint(
            "length(btrim(rule)) > 0", name=op.f("ck_dependency_edges_rule_nonblank")
        ),
        sa.ForeignKeyConstraint(
            ["course_id"], ["courses.id"], name=op.f("fk_dependency_edges_course_id_courses")
        ),
        sa.ForeignKeyConstraint(
            ["derived_from_obligation_id"],
            ["obligations.id"],
            name=op.f("fk_dependency_edges_derived_from_obligation_id_obligations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workflow_run_id"],
            ["workflow_runs.id"],
            name=op.f("fk_dependency_edges_workflow_run_id_workflow_runs"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_dependency_edges")),
        sa.UniqueConstraint(
            "course_id",
            "relation",
            "prerequisite_kind",
            "prerequisite_assessment_id",
            "prerequisite_obligation_id",
            "prerequisite_milestone",
            "dependent_kind",
            "dependent_assessment_id",
            "dependent_obligation_id",
            "dependent_milestone",
            name="uq_dependency_edges_ends",
            postgresql_nulls_not_distinct=True,
        ),
    )
    for column in (
        "prerequisite_obligation_id",
        "dependent_obligation_id",
        "derived_from_obligation_id",
        "workflow_run_id",
    ):
        op.create_index(op.f(f"ix_dependency_edges_{column}"), "dependency_edges", [column])

    op.create_table(
        "conflicts",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("course_id", sa.Uuid(), nullable=False),
        sa.Column("assessment_id", sa.Uuid(), nullable=False),
        sa.Column("field", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("workflow_run_id", sa.Uuid(), nullable=False),
        *_timestamps(),
        sa.CheckConstraint("field IN ('deadline')", name=op.f("ck_conflicts_conflict_field")),
        sa.CheckConstraint(
            "status IN ('open', 'acknowledged', 'resolved_by_source')",
            name=op.f("ck_conflicts_conflict_status"),
        ),
        sa.ForeignKeyConstraint(
            ["assessment_id", "course_id"],
            ["assessments.id", "assessments.course_id"],
            name="fk_conflicts_assessment_same_course",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["course_id"], ["courses.id"], name=op.f("fk_conflicts_course_id_courses")
        ),
        sa.ForeignKeyConstraint(
            ["workflow_run_id"],
            ["workflow_runs.id"],
            name=op.f("fk_conflicts_workflow_run_id_workflow_runs"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_conflicts")),
        sa.UniqueConstraint("assessment_id", "field", name="uq_conflicts_assessment_field"),
    )
    op.create_index(op.f("ix_conflicts_course_id"), "conflicts", ["course_id"])
    op.create_index(op.f("ix_conflicts_workflow_run_id"), "conflicts", ["workflow_run_id"])
    op.create_table(
        "conflict_sides",
        sa.Column("conflict_id", sa.Uuid(), nullable=False),
        sa.Column("assessment_deadline_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["conflict_id"],
            ["conflicts.id"],
            name=op.f("fk_conflict_sides_conflict_id_conflicts"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["assessment_deadline_id"],
            ["assessment_deadlines.id"],
            name=op.f("fk_conflict_sides_assessment_deadline_id_assessment_deadlines"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "conflict_id", "assessment_deadline_id", name=op.f("pk_conflict_sides")
        ),
    )
    op.create_index(
        op.f("ix_conflict_sides_assessment_deadline_id"),
        "conflict_sides",
        ["assessment_deadline_id"],
    )

    op.add_column("evidence_links", sa.Column("dependency_edge_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        op.f("fk_evidence_links_dependency_edge_id_dependency_edges"),
        "evidence_links",
        "dependency_edges",
        ["dependency_edge_id"],
        ["id"],
        ondelete="CASCADE",
    )
    _replace_check("evidence_subject", _SUBJECT_TYPES)
    _replace_check("evidence_field", _FIELDS)
    _replace_check("one_subject", _SUBJECT)
    _replace_check("field_fits_subject", _FIELD_FITS)
    _replace_fact_quote(_FACT_QUOTE)
    op.create_index(
        "ix_evidence_links_dependency_edge_id", "evidence_links", ["dependency_edge_id"]
    )


def downgrade() -> None:
    # Evidence for edges goes with them; nothing else changes.
    op.execute("DELETE FROM evidence_links WHERE subject_type = 'dependency_edge'")
    op.drop_index("ix_evidence_links_dependency_edge_id", table_name="evidence_links")
    _replace_fact_quote(_FACT_QUOTE_0008)
    for name in ("one_subject", "field_fits_subject", "evidence_subject", "evidence_field"):
        op.drop_constraint(op.f(f"ck_evidence_links_{name}"), "evidence_links", type_="check")
    op.drop_column("evidence_links", "dependency_edge_id")  # its foreign key goes with it
    op.create_check_constraint(
        op.f("ck_evidence_links_evidence_subject"), "evidence_links", _SUBJECT_TYPES_0008
    )
    op.create_check_constraint(
        op.f("ck_evidence_links_evidence_field"), "evidence_links", _FIELDS_0008
    )
    op.create_check_constraint(
        op.f("ck_evidence_links_one_subject"), "evidence_links", _SUBJECT_0008
    )
    op.create_check_constraint(
        op.f("ck_evidence_links_field_fits_subject"), "evidence_links", _FIELD_FITS_0008
    )
    op.drop_table("conflict_sides")
    op.drop_table("conflicts")
    op.drop_table("dependency_edges")  # its indexes go with them
