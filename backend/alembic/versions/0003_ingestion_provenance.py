# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""ingestion provenance: raw object reference, extracted text, structural spans

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-10

Written by hand in the style of 0002 (VARCHAR + one named CHECK per enum).

- resource_versions: raw_text_ref is replaced by raw_object_ref
  ('sha256:' || content_hash), plus media_type, byte_size, display_name,
  parser_name, parser_version, extracted_text and text_hash (verified by the
  database from extracted_text).
- source_spans: ordinal (unique per version) and block_kind; each block kind
  requires its canonical locator (additional locator fields are permitted);
  with offsets, the excerpt length must equal the range.
  ix_source_spans_resource_version_id is dropped: the new unique index
  starts with resource_version_id.
- resources: source_uri becomes NOT NULL, non-blank and unique per course.

Preconditions are checked first, so an environment with data fails with a
clear message instead of at an ALTER TABLE. No backfill exists: versions
and spans cannot be reconstructed without re-ingesting the original files.

Downgrade drops the new columns and their data; raw_text_ref comes back
empty (nullable).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_MEDIA_TYPES = (
    "'application/pdf', "
    "'application/vnd.openxmlformats-officedocument.wordprocessingml.document', "
    "'application/vnd.openxmlformats-officedocument.presentationml.presentation', "
    "'text/html'"
)
_BLOCK_KINDS = "'page', 'heading', 'paragraph', 'table', 'slide_text', 'slide_notes'"
# Must equal LOCATOR_FITS_KIND_SQL in app/db/models.py (migrations never import app code).
_LOCATOR_FITS_KIND = (
    "(block_kind <> 'page' OR page_number IS NOT NULL) "
    "AND (block_kind NOT IN ('heading', 'paragraph', 'table') OR section_path IS NOT NULL) "
    "AND (block_kind NOT IN ('slide_text', 'slide_notes') OR slide_number IS NOT NULL)"
)

_VERSION_TEXT_COLUMNS = (
    "raw_object_ref",
    "display_name",
    "parser_name",
    "parser_version",
    "extracted_text",
    "text_hash",
)
_VERSION_CHECKS = {
    "raw_object_ref_matches_hash": "raw_object_ref = 'sha256:' || content_hash",
    "media_type": f"media_type IN ({_MEDIA_TYPES})",
    "byte_size_nonnegative": "byte_size >= 0",
    "display_name_nonblank": "length(btrim(display_name)) > 0",
    "parser_name_nonblank": "length(btrim(parser_name)) > 0",
    "parser_version_nonblank": "length(btrim(parser_version)) > 0",
    "extracted_text_nonempty": "length(extracted_text) > 0",
    "text_hash_matches_text": (
        "text_hash = encode(sha256(convert_to(extracted_text, 'UTF8')), 'hex')"
    ),
}
_SPAN_CHECKS = {
    "ordinal_nonnegative": "ordinal >= 0",
    "block_kind": f"block_kind IN ({_BLOCK_KINDS})",
    "locator_fits_kind": _LOCATOR_FITS_KIND,
    "excerpt_matches_offsets": "end_offset IS NULL OR end_offset - start_offset = length(excerpt)",
}


class MigrationPreconditionError(RuntimeError):
    """The database holds data this migration cannot convert safely."""


def _check_preconditions() -> None:
    bind = op.get_bind()

    def count(sql: str) -> int:
        return int(bind.scalar(sa.text(sql)) or 0)

    # Spans first: they cannot exist without versions, so this gives the
    # more specific message when both tables hold rows.
    for table in ("source_spans", "resource_versions"):
        rows = count(f"SELECT count(*) FROM {table}")
        if rows:
            raise MigrationPreconditionError(
                f"0003: {table} has {rows} row(s). They need values (extracted text, "
                "raw object reference, ordinals) that only re-ingestion can produce. "
                "Delete them and re-import the sources, then upgrade."
            )

    blank = count(
        "SELECT count(*) FROM resources WHERE source_uri IS NULL OR btrim(source_uri) = ''"
    )
    if blank:
        raise MigrationPreconditionError(
            f"0003: {blank} resource(s) have a NULL or blank source_uri. Give each one a "
            "logical source_uri (e.g. 'upload:brief.pdf'), then upgrade."
        )

    duplicates = count(
        "SELECT count(*) FROM ("
        "SELECT 1 FROM resources GROUP BY course_id, source_uri HAVING count(*) > 1"
        ") AS duplicate_groups"
    )
    if duplicates:
        raise MigrationPreconditionError(
            f"0003: {duplicates} (course_id, source_uri) pair(s) are shared by more than one "
            "resource. Make each source_uri unique within its course, then upgrade."
        )


def upgrade() -> None:
    _check_preconditions()

    # --- resources: logical identity inside a course
    op.alter_column("resources", "source_uri", existing_type=sa.Text(), nullable=False)
    op.create_check_constraint(
        op.f("ck_resources_source_uri_nonblank"), "resources", "length(btrim(source_uri)) > 0"
    )
    op.create_unique_constraint(
        "uq_resources_course_source_uri", "resources", ["course_id", "source_uri"]
    )

    # --- resource_versions: raw bytes reference, extracted text, metadata
    op.drop_column("resource_versions", "raw_text_ref")
    for column in _VERSION_TEXT_COLUMNS:
        op.add_column("resource_versions", sa.Column(column, sa.Text(), nullable=False))
    op.add_column(
        "resource_versions", sa.Column("media_type", sa.String(length=128), nullable=False)
    )
    op.add_column("resource_versions", sa.Column("byte_size", sa.BigInteger(), nullable=False))
    for name, condition in _VERSION_CHECKS.items():
        op.create_check_constraint(
            op.f(f"ck_resource_versions_{name}"), "resource_versions", condition
        )

    # --- source_spans: structural order and kind
    op.drop_index(op.f("ix_source_spans_resource_version_id"), table_name="source_spans")
    op.add_column("source_spans", sa.Column("ordinal", sa.Integer(), nullable=False))
    op.add_column("source_spans", sa.Column("block_kind", sa.String(length=32), nullable=False))
    for name, condition in _SPAN_CHECKS.items():
        op.create_check_constraint(op.f(f"ck_source_spans_{name}"), "source_spans", condition)
    op.create_unique_constraint(
        "uq_source_spans_version_ordinal", "source_spans", ["resource_version_id", "ordinal"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_source_spans_version_ordinal", "source_spans", type_="unique")
    for name in _SPAN_CHECKS:
        op.drop_constraint(op.f(f"ck_source_spans_{name}"), "source_spans", type_="check")
    op.drop_column("source_spans", "block_kind")
    op.drop_column("source_spans", "ordinal")
    op.create_index(
        op.f("ix_source_spans_resource_version_id"), "source_spans", ["resource_version_id"]
    )

    for name in _VERSION_CHECKS:
        op.drop_constraint(op.f(f"ck_resource_versions_{name}"), "resource_versions", type_="check")
    for column in (*_VERSION_TEXT_COLUMNS, "media_type", "byte_size"):
        op.drop_column("resource_versions", column)
    op.add_column("resource_versions", sa.Column("raw_text_ref", sa.Text(), nullable=True))

    op.drop_constraint("uq_resources_course_source_uri", "resources", type_="unique")
    op.drop_constraint(op.f("ck_resources_source_uri_nonblank"), "resources", type_="check")
    op.alter_column("resources", "source_uri", existing_type=sa.Text(), nullable=True)
