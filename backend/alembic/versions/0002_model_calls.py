# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""model calls: one auditable row per logical model call

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-09

Drafted with --autogenerate, then reviewed and corrected by hand:
- Enum columns written as VARCHAR(32) + one explicitly named CHECK each
  (the draft emitted each enum CHECK twice, as in 0001).
- workflow_run_id is nullable: standalone calls (smoke tests, evaluations)
  belong to no run. Deleting a run deletes its calls, like its inputs.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ERROR_CATEGORIES = (
    "'timeout', 'connection', 'rate_limited', 'server_error', 'authentication', "
    "'invalid_request', 'truncated', 'refused', 'invalid_output', 'unexpected'"
)


def upgrade() -> None:
    op.create_table(
        "model_calls",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("workflow_run_id", sa.Uuid(), nullable=True),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("model_role", sa.String(length=32), nullable=True),
        sa.Column("prompt_version", sa.Text(), nullable=False),
        sa.Column("output_schema", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("error_category", sa.String(length=32), nullable=True),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("repair_rounds", sa.Integer(), server_default="0", nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), nullable=True),
        sa.Column("completion_tokens", sa.Integer(), nullable=True),
        sa.Column("reasoning_tokens", sa.Integer(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("provider_request_id", sa.Text(), nullable=True),
        sa.Column("output_summary", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "model_role IN ('fast', 'strong')", name=op.f("ck_model_calls_model_role")
        ),
        sa.CheckConstraint(
            "status IN ('succeeded', 'failed')", name=op.f("ck_model_calls_model_call_status")
        ),
        sa.CheckConstraint(
            f"error_category IN ({_ERROR_CATEGORIES})",
            name=op.f("ck_model_calls_model_call_error_category"),
        ),
        sa.CheckConstraint(
            "(status = 'succeeded' AND error_category IS NULL AND output_summary IS NOT NULL)"
            " OR (status = 'failed' AND error_category IS NOT NULL)",
            name=op.f("ck_model_calls_outcome_consistent"),
        ),
        sa.CheckConstraint("attempts >= 1", name=op.f("ck_model_calls_attempts_positive")),
        sa.CheckConstraint(
            "repair_rounds >= 0 AND repair_rounds < attempts",
            name=op.f("ck_model_calls_repair_rounds_bounded"),
        ),
        sa.CheckConstraint(
            "finished_at >= started_at", name=op.f("ck_model_calls_finished_after_started")
        ),
        sa.CheckConstraint("latency_ms >= 0", name=op.f("ck_model_calls_latency_nonnegative")),
        sa.CheckConstraint(
            "prompt_tokens >= 0 AND completion_tokens >= 0 AND reasoning_tokens >= 0",
            name=op.f("ck_model_calls_tokens_nonnegative"),
        ),
        sa.ForeignKeyConstraint(
            ["workflow_run_id"],
            ["workflow_runs.id"],
            name=op.f("fk_model_calls_workflow_run_id_workflow_runs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_model_calls")),
    )
    op.create_index(
        op.f("ix_model_calls_workflow_run_id"), "model_calls", ["workflow_run_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_model_calls_workflow_run_id"), table_name="model_calls")
    op.drop_table("model_calls")
