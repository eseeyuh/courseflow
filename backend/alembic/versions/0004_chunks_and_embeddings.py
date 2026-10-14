# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""retrieval layer: chunks, embedding spaces, embeddings, chunk links; ModelCall.call_kind

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-14

Written by hand in the style of 0002/0003.

- chunks: exact slices of one resource version's extracted text, with the
  first/last span range given by span ordinals of the same version. The
  cross-table rules (the version exists, start inside the first span, end
  inside the last span, text equals the exact slice) are checked for every
  row by the chunks_provenance trigger, as a backstop behind the same
  checks in application code.
- embedding_spaces, embeddings: global, content-addressed vectors in an
  unbounded `vector` column; each row's dimensions must match its space and
  zero vectors are rejected. No ANN index (exact search).
- chunk_embeddings: explicit chunk -> document-format embedding links.
- model_calls.call_kind: what operation happened. Existing rows are all
  structured chat calls (the only writer so far) and are backfilled as
  `chat_structured`; the temporary default is then dropped so every writer
  must state the kind. Embedding calls have no model_role and no repairs.

Downgrade drops the retrieval tables (derived, recomputable data). It
refuses while embedding ModelCalls exist: without call_kind they would be
indistinguishable from chat calls.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op
from pgvector.sqlalchemy import VECTOR

# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Must equal the CHECKs in app/db/models.py (migrations never import app code).
_INPUT_FORMAT = "input_format ~ '^(document|query)/v[1-9][0-9]*$'"
_DOCUMENT_FORMAT = "input_format ~ '^document/v[1-9][0-9]*$'"

_PROVENANCE_FUNCTION = """
CREATE FUNCTION chunks_check_provenance() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    version_text text;
    first_start integer;
    first_end integer;
    last_start integer;
    last_end integer;
BEGIN
    -- Messages name offsets and ordinals only, never text.
    SELECT extracted_text INTO version_text
    FROM resource_versions WHERE id = NEW.resource_version_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'chunks_provenance: resource version % does not exist',
            NEW.resource_version_id
            USING ERRCODE = 'check_violation', CONSTRAINT = 'chunks_provenance';
    END IF;

    IF NEW.first_span_ordinal > NEW.last_span_ordinal THEN
        RAISE EXCEPTION 'chunks_provenance: first span % is after last span %',
            NEW.first_span_ordinal, NEW.last_span_ordinal
            USING ERRCODE = 'check_violation', CONSTRAINT = 'chunks_provenance';
    END IF;

    SELECT start_offset, end_offset INTO first_start, first_end
    FROM source_spans
    WHERE resource_version_id = NEW.resource_version_id AND ordinal = NEW.first_span_ordinal;
    IF NOT FOUND OR first_start IS NULL THEN
        RAISE EXCEPTION 'chunks_provenance: span % of this version is missing or has no offsets',
            NEW.first_span_ordinal
            USING ERRCODE = 'check_violation', CONSTRAINT = 'chunks_provenance';
    END IF;

    SELECT start_offset, end_offset INTO last_start, last_end
    FROM source_spans
    WHERE resource_version_id = NEW.resource_version_id AND ordinal = NEW.last_span_ordinal;
    IF NOT FOUND OR last_start IS NULL THEN
        RAISE EXCEPTION 'chunks_provenance: span % of this version is missing or has no offsets',
            NEW.last_span_ordinal
            USING ERRCODE = 'check_violation', CONSTRAINT = 'chunks_provenance';
    END IF;

    IF NOT (NEW.start_offset >= first_start AND NEW.start_offset < first_end) THEN
        RAISE EXCEPTION 'chunks_provenance: start % is outside first span % [%, %)',
            NEW.start_offset, NEW.first_span_ordinal, first_start, first_end
            USING ERRCODE = 'check_violation', CONSTRAINT = 'chunks_provenance';
    END IF;

    IF NOT (NEW.end_offset > last_start AND NEW.end_offset <= last_end) THEN
        RAISE EXCEPTION 'chunks_provenance: end % is outside last span % [%, %)',
            NEW.end_offset, NEW.last_span_ordinal, last_start, last_end
            USING ERRCODE = 'check_violation', CONSTRAINT = 'chunks_provenance';
    END IF;

    -- substr is 1-based; offsets are 0-based code points, like Python slicing.
    IF substr(version_text, NEW.start_offset + 1, NEW.end_offset - NEW.start_offset)
            IS DISTINCT FROM NEW.text THEN
        RAISE EXCEPTION 'chunks_provenance: text is not the exact version slice [%, %)',
            NEW.start_offset, NEW.end_offset
            USING ERRCODE = 'check_violation', CONSTRAINT = 'chunks_provenance';
    END IF;

    RETURN NEW;
END;
$$
"""


class MigrationPreconditionError(RuntimeError):
    """The database holds data this migration cannot convert safely."""


def upgrade() -> None:
    # --- model_calls.call_kind: backfill existing rows, then require it
    op.add_column(
        "model_calls",
        sa.Column(
            "call_kind", sa.String(length=32), nullable=False, server_default="chat_structured"
        ),
    )
    op.alter_column("model_calls", "call_kind", server_default=None)
    op.create_check_constraint(
        op.f("ck_model_calls_model_call_kind"),
        "model_calls",
        "call_kind IN ('chat_structured', 'embedding')",
    )
    op.create_check_constraint(
        op.f("ck_model_calls_embedding_has_no_role"),
        "model_calls",
        "call_kind <> 'embedding' OR (model_role IS NULL AND repair_rounds = 0)",
    )

    # --- chunks
    op.create_table(
        "chunks",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("resource_version_id", sa.Uuid(), nullable=False),
        sa.Column("chunker_version", sa.Text(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("start_offset", sa.Integer(), nullable=False),
        sa.Column("end_offset", sa.Integer(), nullable=False),
        sa.Column("first_span_ordinal", sa.Integer(), nullable=False),
        sa.Column("last_span_ordinal", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("text_hash", sa.Text(), nullable=False),
        sa.Column("estimated_tokens", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "length(btrim(chunker_version)) > 0", name=op.f("ck_chunks_chunker_version_nonblank")
        ),
        sa.CheckConstraint("ordinal >= 0", name=op.f("ck_chunks_ordinal_nonnegative")),
        sa.CheckConstraint(
            "start_offset >= 0 AND start_offset < end_offset",
            name=op.f("ck_chunks_offsets_ordered"),
        ),
        sa.CheckConstraint(
            "first_span_ordinal <= last_span_ordinal", name=op.f("ck_chunks_span_range_ordered")
        ),
        sa.CheckConstraint(
            "length(text) = end_offset - start_offset", name=op.f("ck_chunks_text_matches_offsets")
        ),
        sa.CheckConstraint(
            "text_hash = encode(sha256(convert_to(text, 'UTF8')), 'hex')",
            name=op.f("ck_chunks_text_hash_matches_text"),
        ),
        sa.CheckConstraint(
            "estimated_tokens > 0", name=op.f("ck_chunks_estimated_tokens_positive")
        ),
        sa.ForeignKeyConstraint(
            ["resource_version_id"],
            ["resource_versions.id"],
            name=op.f("fk_chunks_resource_version_id_resource_versions"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["resource_version_id", "first_span_ordinal"],
            ["source_spans.resource_version_id", "source_spans.ordinal"],
            name="fk_chunks_first_span",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["resource_version_id", "last_span_ordinal"],
            ["source_spans.resource_version_id", "source_spans.ordinal"],
            name="fk_chunks_last_span",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_chunks")),
        sa.UniqueConstraint(
            "resource_version_id",
            "chunker_version",
            "ordinal",
            name="uq_chunks_version_chunker_ordinal",
        ),
    )
    op.execute(_PROVENANCE_FUNCTION)
    op.execute(
        "CREATE TRIGGER chunks_provenance BEFORE INSERT OR UPDATE ON chunks "
        "FOR EACH ROW EXECUTE FUNCTION chunks_check_provenance()"
    )

    # --- embedding_spaces
    op.create_table(
        "embedding_spaces",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("dimensions", sa.Integer(), nullable=False),
        sa.Column("space_version", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "length(provider) BETWEEN 1 AND 64 AND provider = btrim(provider)",
            name=op.f("ck_embedding_spaces_provider_valid"),
        ),
        sa.CheckConstraint(
            "length(model) BETWEEN 1 AND 256 AND model = btrim(model)",
            name=op.f("ck_embedding_spaces_model_valid"),
        ),
        sa.CheckConstraint(
            "space_version ~ '^[a-z0-9][a-z0-9._-]{0,31}$'",
            name=op.f("ck_embedding_spaces_space_version_valid"),
        ),
        sa.CheckConstraint(
            "dimensions BETWEEN 1 AND 16000", name=op.f("ck_embedding_spaces_dimensions_in_range")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_embedding_spaces")),
        sa.UniqueConstraint(
            "provider", "model", "dimensions", "space_version", name="uq_embedding_spaces_identity"
        ),
        sa.UniqueConstraint("id", "dimensions", name="uq_embedding_spaces_id_dimensions"),
    )

    # --- embeddings
    op.create_table(
        "embeddings",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("space_id", sa.Uuid(), nullable=False),
        sa.Column("dimensions", sa.Integer(), nullable=False),
        sa.Column("input_format", sa.Text(), nullable=False),
        sa.Column("input_hash", sa.Text(), nullable=False),
        sa.Column("vector", VECTOR(), nullable=False),
        sa.Column("model_call_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(_INPUT_FORMAT, name=op.f("ck_embeddings_input_format_versioned")),
        sa.CheckConstraint(
            "input_hash ~ '^[0-9a-f]{64}$'", name=op.f("ck_embeddings_input_hash_sha256_hex")
        ),
        sa.CheckConstraint(
            "vector_dims(vector) = dimensions", name=op.f("ck_embeddings_vector_dims_match")
        ),
        sa.CheckConstraint("vector_norm(vector) > 0", name=op.f("ck_embeddings_vector_nonzero")),
        sa.ForeignKeyConstraint(
            ["space_id", "dimensions"],
            ["embedding_spaces.id", "embedding_spaces.dimensions"],
            name="fk_embeddings_space",
        ),
        sa.ForeignKeyConstraint(
            ["model_call_id"],
            ["model_calls.id"],
            name=op.f("fk_embeddings_model_call_id_model_calls"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_embeddings")),
        sa.UniqueConstraint(
            "space_id", "input_format", "input_hash", name="uq_embeddings_cache_key"
        ),
        sa.UniqueConstraint("id", "space_id", "input_format", name="uq_embeddings_id_space_format"),
    )
    op.create_index(op.f("ix_embeddings_model_call_id"), "embeddings", ["model_call_id"])

    # --- chunk_embeddings
    op.create_table(
        "chunk_embeddings",
        sa.Column("chunk_id", sa.Uuid(), nullable=False),
        sa.Column("embedding_id", sa.Uuid(), nullable=False),
        sa.Column("space_id", sa.Uuid(), nullable=False),
        sa.Column("input_format", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(_DOCUMENT_FORMAT, name=op.f("ck_chunk_embeddings_document_format_only")),
        sa.ForeignKeyConstraint(
            ["chunk_id"],
            ["chunks.id"],
            name=op.f("fk_chunk_embeddings_chunk_id_chunks"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["embedding_id", "space_id", "input_format"],
            ["embeddings.id", "embeddings.space_id", "embeddings.input_format"],
            name="fk_chunk_embeddings_embedding",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("chunk_id", "embedding_id", name=op.f("pk_chunk_embeddings")),
        sa.UniqueConstraint(
            "chunk_id", "space_id", "input_format", name="uq_chunk_embeddings_chunk_space_format"
        ),
    )
    op.create_index("ix_chunk_embeddings_embedding_id", "chunk_embeddings", ["embedding_id"])


def _check_downgrade_preconditions() -> None:
    if context.is_offline_mode():
        raise MigrationPreconditionError(
            "0004: downgrade preconditions need a live database; run without --sql"
        )
    embedding_calls = int(
        op.get_bind().scalar(
            sa.text("SELECT count(*) FROM model_calls WHERE call_kind = 'embedding'")
        )
        or 0
    )
    if embedding_calls:
        raise MigrationPreconditionError(
            f"0004: model_calls has {embedding_calls} embedding call(s). Without call_kind "
            "they would look like chat calls. Delete or export them, then downgrade."
        )


def downgrade() -> None:
    _check_downgrade_preconditions()

    op.drop_index("ix_chunk_embeddings_embedding_id", table_name="chunk_embeddings")
    op.drop_table("chunk_embeddings")
    op.drop_index(op.f("ix_embeddings_model_call_id"), table_name="embeddings")
    op.drop_table("embeddings")
    op.drop_table("embedding_spaces")
    op.execute("DROP TRIGGER chunks_provenance ON chunks")
    op.execute("DROP FUNCTION chunks_check_provenance()")
    op.drop_table("chunks")

    op.drop_constraint(op.f("ck_model_calls_embedding_has_no_role"), "model_calls", type_="check")
    op.drop_constraint(op.f("ck_model_calls_model_call_kind"), "model_calls", type_="check")
    op.drop_column("model_calls", "call_kind")
