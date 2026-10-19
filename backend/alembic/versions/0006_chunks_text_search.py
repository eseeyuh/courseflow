# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""chunks.text_tsv: a stored full-text vector of each chunk's text, GIN indexed

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-14

The column is GENERATED ALWAYS ... STORED from ``chunks.text`` with the
explicit ``english`` configuration (never ``default_text_search_config``, so
the stored lexemes cannot depend on a session or server setting). Chunks are
immutable, so the column can never drift from the text it was computed from.

Backfill is automatic: adding a stored generated column rewrites ``chunks``
and computes the value for every existing row, holding an ACCESS EXCLUSIVE
lock on the table for the duration. Row triggers (``chunks_provenance``) do
not fire for that rewrite. Downgrade drops the index, then the column.

English stemming and stop words apply to all text: this is English lexical
retrieval, not multilingual retrieval. Changing the configuration is a new
migration and a new lexical version, never an edit of this one.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0006"
down_revision: str | Sequence[str] | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Must equal the Computed expression on Chunk.text_tsv in app/db/models.py
# (migrations never import app code).
_TEXT_TSV = "to_tsvector('english'::regconfig, text)"


def upgrade() -> None:
    op.add_column(
        "chunks",
        sa.Column(
            "text_tsv",
            postgresql.TSVECTOR(),
            sa.Computed(_TEXT_TSV, persisted=True),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_chunks_text_tsv", "chunks", ["text_tsv"], unique=False, postgresql_using="gin"
    )


def downgrade() -> None:
    op.drop_index("ix_chunks_text_tsv", table_name="chunks", postgresql_using="gin")
    op.drop_column("chunks", "text_tsv")
