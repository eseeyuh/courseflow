# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""planning layer: study states and saved study plans

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-22

- study_states: one learner's state per mapped teaching SourceSpan.
- study_plans + study_tasks: saved snapshots of the deterministic
  cross-course plan, each task with its stored facts, position, reasons
  and rendered sentence, so two plans can be compared.

No existing table changes. Downgrade drops every state and plan.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0011"
down_revision: str | Sequence[str] | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _created() -> sa.Column:  # type: ignore[type-arg]
    return sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
    )


def _updated() -> sa.Column:  # type: ignore[type-arg]
    return sa.Column(
        "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
    )


def upgrade() -> None:
    op.create_table(
        "study_states",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("learner", sa.Text(), nullable=False),
        sa.Column("source_span_id", sa.Uuid(), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        _created(),
        _updated(),
        sa.CheckConstraint(
            "state IN ('not_started', 'studying', 'studied')",
            name=op.f("ck_study_states_study_state"),
        ),
        sa.CheckConstraint(
            "length(btrim(learner)) > 0", name=op.f("ck_study_states_learner_nonblank")
        ),
        sa.ForeignKeyConstraint(
            ["source_span_id"],
            ["source_spans.id"],
            name=op.f("fk_study_states_source_span_id_source_spans"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_study_states")),
        sa.UniqueConstraint("learner", "source_span_id", name="uq_study_states_learner_span"),
    )
    op.create_index(op.f("ix_study_states_source_span_id"), "study_states", ["source_span_id"])

    op.create_table(
        "study_plans",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("learner", sa.Text(), nullable=False),
        sa.Column("plan_version", sa.Text(), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("task_count", sa.Integer(), nullable=False),
        sa.Column("workflow_run_id", sa.Uuid(), nullable=True),
        _created(),
        sa.CheckConstraint("task_count >= 0", name=op.f("ck_study_plans_task_count_nonnegative")),
        sa.ForeignKeyConstraint(
            ["workflow_run_id"],
            ["workflow_runs.id"],
            name=op.f("fk_study_plans_workflow_run_id_workflow_runs"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_study_plans")),
    )
    op.create_index(op.f("ix_study_plans_workflow_run_id"), "study_plans", ["workflow_run_id"])

    op.create_table(
        "study_tasks",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("plan_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("task_key", sa.Text(), nullable=False),
        sa.Column("task_type", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("course_id", sa.Uuid(), nullable=False),
        sa.Column("assessment_id", sa.Uuid(), nullable=True),
        sa.Column("obligation_id", sa.Uuid(), nullable=True),
        sa.Column("conflict_id", sa.Uuid(), nullable=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("anchor_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("estimated_minutes", sa.Integer(), nullable=False),
        sa.Column("reasons", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("why_now", sa.Text(), nullable=False),
        sa.Column("prerequisite_keys", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("target_span_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        _created(),
        sa.CheckConstraint(
            "task_type IN ('study_material', 'obligation', 'submit_assessment', "
            "'check_conflict', 'ethics_approval', 'data_collection')",
            name=op.f("ck_study_tasks_study_task_type"),
        ),
        sa.CheckConstraint(
            "status IN ('ready', 'blocked', 'completed')",
            name=op.f("ck_study_tasks_study_task_status"),
        ),
        sa.CheckConstraint("position >= 1", name=op.f("ck_study_tasks_position_positive")),
        sa.CheckConstraint(
            "estimated_minutes >= 0", name=op.f("ck_study_tasks_estimated_minutes_nonnegative")
        ),
        sa.CheckConstraint(
            "jsonb_typeof(reasons) = 'array'", name=op.f("ck_study_tasks_reasons_is_array")
        ),
        sa.ForeignKeyConstraint(
            ["plan_id"],
            ["study_plans.id"],
            name=op.f("fk_study_tasks_plan_id_study_plans"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["course_id"],
            ["courses.id"],
            name=op.f("fk_study_tasks_course_id_courses"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["assessment_id"],
            ["assessments.id"],
            name=op.f("fk_study_tasks_assessment_id_assessments"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["obligation_id"],
            ["obligations.id"],
            name=op.f("fk_study_tasks_obligation_id_obligations"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["conflict_id"],
            ["conflicts.id"],
            name=op.f("fk_study_tasks_conflict_id_conflicts"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_study_tasks")),
        sa.UniqueConstraint("plan_id", "task_key", name="uq_study_tasks_plan_key"),
        sa.UniqueConstraint("plan_id", "position", name="uq_study_tasks_plan_position"),
    )
    op.create_index(op.f("ix_study_tasks_course_id"), "study_tasks", ["course_id"])


def downgrade() -> None:
    op.drop_table("study_tasks")  # its indexes go with it
    op.drop_table("study_plans")
    op.drop_table("study_states")
