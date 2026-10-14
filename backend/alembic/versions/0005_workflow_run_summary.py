# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""workflow_runs.summary: a run's final counts, written once when it finishes

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-14

The summary is a JSON object whose shape is defined by the run's
workflow_version (e.g. "index/v1"): counts and identifiers only, never
document text. Existing rows keep NULL. Downgrade drops the column and any
summaries in it.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "workflow_runs",
        sa.Column("summary", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.create_check_constraint(
        op.f("ck_workflow_runs_summary_is_object"),
        "workflow_runs",
        "summary IS NULL OR jsonb_typeof(summary) = 'object'",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_workflow_runs_summary_is_object"), "workflow_runs", type_="check")
    op.drop_column("workflow_runs", "summary")
