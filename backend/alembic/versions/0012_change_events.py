# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""change layer: one ChangeEvent per resource version a sync brought in

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-22

No existing table changes. Downgrade drops every change event.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0012"
down_revision: str | Sequence[str] | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "change_events",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("course_id", sa.Uuid(), nullable=False),
        sa.Column("resource_id", sa.Uuid(), nullable=False),
        sa.Column("change_type", sa.String(length=32), nullable=False),
        sa.Column("old_version_id", sa.Uuid(), nullable=True),
        sa.Column("new_version_id", sa.Uuid(), nullable=False),
        sa.Column("old_content_hash", sa.Text(), nullable=True),
        sa.Column("new_content_hash", sa.Text(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("workflow_run_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "change_type IN ('added', 'updated')", name=op.f("ck_change_events_change_type")
        ),
        sa.CheckConstraint(
            "(change_type = 'added') = (old_version_id IS NULL)",
            name=op.f("ck_change_events_added_has_no_old_version"),
        ),
        sa.CheckConstraint(
            "old_content_hash IS NULL OR old_content_hash <> new_content_hash",
            name=op.f("ck_change_events_content_changed"),
        ),
        sa.ForeignKeyConstraint(
            ["course_id"],
            ["courses.id"],
            name=op.f("fk_change_events_course_id_courses"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["resource_id"],
            ["resources.id"],
            name=op.f("fk_change_events_resource_id_resources"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["old_version_id"],
            ["resource_versions.id"],
            name=op.f("fk_change_events_old_version_id_resource_versions"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["new_version_id"],
            ["resource_versions.id"],
            name=op.f("fk_change_events_new_version_id_resource_versions"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workflow_run_id"],
            ["workflow_runs.id"],
            name=op.f("fk_change_events_workflow_run_id_workflow_runs"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_change_events")),
        sa.UniqueConstraint("new_version_id", name="uq_change_events_new_version"),
    )
    for column in ("course_id", "resource_id", "workflow_run_id"):
        op.create_index(op.f(f"ix_change_events_{column}"), "change_events", [column])


def downgrade() -> None:
    op.drop_table("change_events")  # its indexes go with it
