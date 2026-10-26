# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""navigation layer: requirement -> teaching material mappings

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-22

- material_mappings: one row per (requirement, relation, target SourceSpan),
  with the exact evidence range inside that span, a navigation rationale
  built by code, the retrieval that found it (mode, rank, score, query
  embedding call) and the verifier call and run that selected it.

No existing table changes. Downgrade drops every mapping.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0009"
down_revision: str | Sequence[str] | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


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


def upgrade() -> None:
    op.create_table(
        "material_mappings",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("requirement_id", sa.Uuid(), nullable=False),
        sa.Column("relation", sa.String(length=32), nullable=False),
        sa.Column("source_span_id", sa.Uuid(), nullable=False),
        sa.Column("quote_start", sa.Integer(), nullable=False),
        sa.Column("quote_end", sa.Integer(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("retrieval_mode", sa.Text(), nullable=False),
        sa.Column("retrieval_rank", sa.Integer(), nullable=False),
        sa.Column("retrieval_score", sa.Double(), nullable=False),
        sa.Column("query_model_call_id", sa.Uuid(), nullable=True),
        sa.Column("verifier_model_call_id", sa.Uuid(), nullable=False),
        sa.Column("workflow_run_id", sa.Uuid(), nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "relation IN ('supported_by')", name=op.f("ck_material_mappings_mapping_relation")
        ),
        sa.CheckConstraint(
            "quote_start >= 0 AND quote_start < quote_end",
            name=op.f("ck_material_mappings_quote_ordered"),
        ),
        sa.CheckConstraint(
            "length(btrim(rationale)) > 0 AND length(rationale) <= 300",
            name=op.f("ck_material_mappings_rationale_nonblank_bounded"),
        ),
        sa.CheckConstraint(
            "retrieval_rank >= 1", name=op.f("ck_material_mappings_retrieval_rank_positive")
        ),
        sa.CheckConstraint(
            "retrieval_mode IN ('vector', 'lexical', 'hybrid')",
            name=op.f("ck_material_mappings_retrieval_mode_known"),
        ),
        sa.CheckConstraint(
            "(retrieval_mode = 'lexical') = (query_model_call_id IS NULL)",
            name=op.f("ck_material_mappings_query_call_iff_embedded"),
        ),
        sa.ForeignKeyConstraint(
            ["requirement_id"],
            ["assessment_requirements.id"],
            name=op.f("fk_material_mappings_requirement_id_assessment_requirements"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_span_id"],
            ["source_spans.id"],
            name=op.f("fk_material_mappings_source_span_id_source_spans"),
        ),
        sa.ForeignKeyConstraint(
            ["query_model_call_id"],
            ["model_calls.id"],
            name=op.f("fk_material_mappings_query_model_call_id_model_calls"),
        ),
        sa.ForeignKeyConstraint(
            ["verifier_model_call_id"],
            ["model_calls.id"],
            name=op.f("fk_material_mappings_verifier_model_call_id_model_calls"),
        ),
        sa.ForeignKeyConstraint(
            ["workflow_run_id"],
            ["workflow_runs.id"],
            name=op.f("fk_material_mappings_workflow_run_id_workflow_runs"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_material_mappings")),
        sa.UniqueConstraint(
            "requirement_id", "relation", "source_span_id", name="uq_material_mappings_target"
        ),
    )
    for column in (
        "source_span_id",
        "query_model_call_id",
        "verifier_model_call_id",
        "workflow_run_id",
    ):
        op.create_index(op.f(f"ix_material_mappings_{column}"), "material_mappings", [column])


def downgrade() -> None:
    op.drop_table("material_mappings")  # its indexes go with it
