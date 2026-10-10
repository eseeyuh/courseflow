# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Database-enforced invariants of the provenance schema.

These rules hold no matter which code path writes the row, so they are
tested against PostgreSQL itself, not against Python validation.
"""

from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import IntegrityError, InvalidRequestError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import (
    Course,
    Institution,
    Resource,
    ResourceVersion,
    SourceSpan,
    WorkflowRun,
    workflow_run_inputs,
)
from app.domain.enums import BlockKind, CourseStatus, LmsType, MediaType, ResourceType
from app.ingestion.hashing import text_hash
from app.ingestion.schemas import ParsedBlock, SpanLocator

pytestmark = pytest.mark.anyio

HASH_A = "a" * 64
HASH_B = "b" * 64
EXCERPT = "Submit the report by 31 October at 16:00."  # 41 code points


def _course() -> Course:
    institution = Institution(name="Northbridge University", lms_type=LmsType.DEMO)
    return Course(institution=institution, title="Data Systems", external_id="DS101")


def _version(
    course: Course, *, content_hash: str = HASH_A, text: str = EXCERPT, **fields: Any
) -> ResourceVersion:
    resource = Resource(
        course=course,
        type=ResourceType.BRIEF,
        title="Coursework brief",
        source_uri=f"upload:brief-{content_hash[:8]}.pdf",
    )
    values: dict[str, Any] = {
        "resource": resource,
        "version_number": 1,
        "content_hash": content_hash,
        "raw_object_ref": f"sha256:{content_hash}",
        "media_type": MediaType.PDF,
        "byte_size": 1024,
        "display_name": "brief.pdf",
        "parser_name": "test-parser",
        "parser_version": "1",
        "extracted_text": text,
        "text_hash": text_hash(text),
    }
    return ResourceVersion(**(values | fields))


def _kind_for(fields: dict[str, Any]) -> BlockKind:
    if fields.get("page_number") is not None:
        return BlockKind.PAGE
    if fields.get("slide_number") is not None:
        return BlockKind.SLIDE_TEXT
    return BlockKind.PARAGRAPH


def _span(version: ResourceVersion, **fields: Any) -> SourceSpan:
    fields.setdefault("excerpt", EXCERPT)
    fields.setdefault("ordinal", 0)
    fields.setdefault("block_kind", _kind_for(fields))
    return SourceSpan(resource_version=version, **fields)


# --- SourceSpan locator / offset / evidence contract ----------------------


@pytest.mark.parametrize(
    "fields",
    [
        {"page_number": 3, "start_offset": 120, "end_offset": 161},
        {"page_number": 1, "start_offset": 0, "end_offset": 1, "excerpt": "S"},
        {"slide_number": 7},
        {"section_path": "2 > 2.3 Marking criteria"},
        {"page_number": 1, "timestamp_seconds": Decimal("754.250")},
        {"page_number": 2, "section_path": "Assessment"},
    ],
    ids=["page+offsets", "offset-zero", "slide", "section", "page+timestamp", "page+section"],
)
async def test_valid_source_spans_are_accepted(
    db_session: AsyncSession, fields: dict[str, Any]
) -> None:
    span = _span(_version(_course()), **fields)
    db_session.add(span)
    await db_session.flush()

    stored = await db_session.scalar(select(SourceSpan).where(SourceSpan.id == span.id))
    assert stored is not None
    assert stored.excerpt.startswith("S")


@pytest.mark.parametrize(
    ("fields", "constraint"),
    [
        # Several CHECKs reject some rows; PostgreSQL reports the first by name.
        ({}, "ck_source_spans_locator_(fits_kind|present)"),
        ({"page_number": 0}, "ck_source_spans_page_positive"),
        ({"slide_number": 0}, "ck_source_spans_slide_positive"),
        ({"section_path": "   "}, "ck_source_spans_section_nonblank"),
        (
            {"page_number": 1, "timestamp_seconds": Decimal("-0.5")},
            "ck_source_spans_timestamp_nonnegative",
        ),
        ({"page_number": 1, "start_offset": 5}, "ck_source_spans_offsets_paired"),
        ({"page_number": 1, "end_offset": 5}, "ck_source_spans_offsets_paired"),
        (
            {"page_number": 1, "start_offset": 5, "end_offset": 5},
            "ck_source_spans_(excerpt_matches_offsets|offsets_ordered)",
        ),
        (
            {"page_number": 1, "start_offset": 9, "end_offset": 5},
            "ck_source_spans_(excerpt_matches_offsets|offsets_ordered)",
        ),
        (
            {"page_number": 1, "start_offset": -1, "end_offset": 5, "excerpt": "Submit"},
            "ck_source_spans_offsets_ordered",
        ),
        (
            {"page_number": 1, "start_offset": 0, "end_offset": 40},
            "ck_source_spans_excerpt_matches_offsets",
        ),
        ({"page_number": 1, "ordinal": -1}, "ck_source_spans_ordinal_nonnegative"),
        ({"page_number": 1, "excerpt": ""}, "ck_source_spans_excerpt_nonempty"),
    ],
    ids=[
        "no-locator",
        "page-zero",
        "slide-zero",
        "blank-section",
        "negative-timestamp",
        "start-without-end",
        "end-without-start",
        "empty-range",
        "reversed-range",
        "negative-start",
        "excerpt-shorter-than-range",
        "negative-ordinal",
        "empty-excerpt",
    ],
)
async def test_invalid_source_spans_are_rejected_by_the_database(
    db_session: AsyncSession, fields: dict[str, Any], constraint: str
) -> None:
    db_session.add(_span(_version(_course()), **fields))

    with pytest.raises(IntegrityError, match=constraint):
        await db_session.flush()


async def test_source_span_requires_excerpt(db_session: AsyncSession) -> None:
    db_session.add(_span(_version(_course()), page_number=1, excerpt=None))

    with pytest.raises(IntegrityError, match="excerpt"):
        await db_session.flush()


# --- Resource <-> ResourceVersion cycle -------------------------------------


async def test_resource_and_current_version_created_in_one_flush(db_session: AsyncSession) -> None:
    version = _version(_course())
    resource = version.resource
    resource.current_version = version  # cycle resolved by post_update
    db_session.add(resource)
    await db_session.flush()

    stored = await db_session.scalar(
        select(Resource.current_version_id).where(Resource.id == resource.id)
    )
    assert stored == version.id


async def test_current_version_must_belong_to_the_same_resource(
    db_session: AsyncSession,
) -> None:
    course = _course()
    version_a = _version(course, content_hash=HASH_A)
    version_b = _version(course, content_hash=HASH_B)
    db_session.add_all([version_a, version_b])
    await db_session.flush()

    version_a.resource.current_version = version_b  # a version of a DIFFERENT resource

    with pytest.raises(IntegrityError, match="fk_resources_current_version"):
        await db_session.flush()


async def test_content_hash_must_be_sha256_hex(db_session: AsyncSession) -> None:
    db_session.add(_version(_course(), content_hash="NOT-A-HASH"))

    with pytest.raises(IntegrityError, match="ck_resource_versions_content_hash_sha256_hex"):
        await db_session.flush()


# --- ResourceVersion ingestion fields ----------------------------------------


@pytest.mark.parametrize(
    ("fields", "constraint"),
    [
        (
            {"raw_object_ref": f"sha256:{HASH_B}"},
            "ck_resource_versions_raw_object_ref_matches_hash",
        ),
        ({"raw_object_ref": HASH_A}, "ck_resource_versions_raw_object_ref_matches_hash"),
        ({"text_hash": text_hash("other text")}, "ck_resource_versions_text_hash_matches_text"),
        ({"byte_size": -1}, "ck_resource_versions_byte_size_nonnegative"),
        ({"display_name": "  "}, "ck_resource_versions_display_name_nonblank"),
        ({"parser_name": ""}, "ck_resource_versions_parser_name_nonblank"),
        ({"parser_version": " "}, "ck_resource_versions_parser_version_nonblank"),
        (
            {"extracted_text": "", "text_hash": text_hash("")},
            "ck_resource_versions_extracted_text_nonempty",
        ),
    ],
    ids=[
        "ref-other-hash",
        "ref-without-scheme",
        "text-hash-mismatch",
        "negative-size",
        "blank-display-name",
        "blank-parser-name",
        "blank-parser-version",
        "empty-text",
    ],
)
async def test_invalid_resource_versions_are_rejected_by_the_database(
    db_session: AsyncSession, fields: dict[str, Any], constraint: str
) -> None:
    db_session.add(_version(_course(), **fields))

    with pytest.raises(IntegrityError, match=constraint):
        await db_session.flush()


async def test_database_text_hash_matches_application_hash_for_unicode(
    db_session: AsyncSession,
) -> None:
    # Non-ASCII text proves the database hashes the same UTF-8 bytes as Python.
    text = "Café \U0001f4c4 — Abgabe bis 31.10."
    version = _version(_course(), text=text)
    db_session.add(version)
    await db_session.flush()

    stored = await db_session.scalar(
        select(
            func.encode(func.sha256(func.convert_to(ResourceVersion.extracted_text, "UTF8")), "hex")
        ).where(ResourceVersion.id == version.id)
    )
    assert stored == text_hash(text) == version.text_hash


@pytest.mark.parametrize("media_type", list(MediaType))
async def test_every_media_type_is_accepted(
    db_session: AsyncSession, media_type: MediaType
) -> None:
    db_session.add(_version(_course(), media_type=media_type))
    await db_session.flush()


async def test_unknown_media_type_rejected_by_database(db_session: AsyncSession) -> None:
    version = _version(_course())
    db_session.add(version)
    await db_session.flush()

    with pytest.raises(IntegrityError, match="ck_resource_versions_media_type"):
        await db_session.execute(
            text("UPDATE resource_versions SET media_type = 'application/zip' WHERE id = :id"),
            {"id": version.id},
        )


# --- Resource identity ----------------------------------------------------------


async def test_source_uri_is_unique_within_a_course(db_session: AsyncSession) -> None:
    course = _course()
    db_session.add_all(
        [
            Resource(course=course, type=ResourceType.BRIEF, title="A", source_uri="upload:a.pdf"),
            Resource(course=course, type=ResourceType.LAB, title="B", source_uri="upload:a.pdf"),
        ]
    )
    with pytest.raises(IntegrityError, match="uq_resources_course_source_uri"):
        await db_session.flush()


async def test_same_source_uri_in_different_courses_is_allowed(db_session: AsyncSession) -> None:
    db_session.add_all(
        [
            Resource(course=_course(), type=ResourceType.BRIEF, title="A", source_uri="upload:a"),
            Resource(course=_course(), type=ResourceType.BRIEF, title="A", source_uri="upload:a"),
        ]
    )
    await db_session.flush()


@pytest.mark.parametrize(
    ("source_uri", "error"),
    [(None, "source_uri"), ("   ", "ck_resources_source_uri_nonblank")],
    ids=["null", "blank"],
)
async def test_resource_requires_a_source_uri(
    db_session: AsyncSession, source_uri: str | None, error: str
) -> None:
    db_session.add(
        Resource(course=_course(), type=ResourceType.BRIEF, title="A", source_uri=source_uri)
    )
    with pytest.raises(IntegrityError, match=error):
        await db_session.flush()


# --- SourceSpan structure ---------------------------------------------------------


async def test_ordinal_is_unique_within_a_version(db_session: AsyncSession) -> None:
    version = _version(_course())
    db_session.add_all([_span(version, page_number=1), _span(version, page_number=2)])

    with pytest.raises(IntegrityError, match="uq_source_spans_version_ordinal"):
        await db_session.flush()


async def test_same_ordinal_in_different_versions_is_allowed(db_session: AsyncSession) -> None:
    course = _course()
    db_session.add_all(
        [
            _span(_version(course, content_hash=HASH_A), page_number=1),
            _span(_version(course, content_hash=HASH_B), page_number=1),
        ]
    )
    await db_session.flush()


async def test_offsets_count_code_points_like_python(db_session: AsyncSession) -> None:
    # "📄 Abgabe" is 8 code points (11 UTF-8 bytes, 9 UTF-16 units).
    excerpt = "\U0001f4c4 Abgabe"
    db_session.add(
        _span(_version(_course()), page_number=1, start_offset=0, end_offset=8, excerpt=excerpt)
    )
    await db_session.flush()


async def test_unknown_block_kind_rejected_by_database(db_session: AsyncSession) -> None:
    span = _span(_version(_course()), page_number=1)
    db_session.add(span)
    await db_session.flush()

    with pytest.raises(IntegrityError, match="ck_source_spans_block_kind"):
        await db_session.execute(
            text("UPDATE source_spans SET block_kind = 'footnote' WHERE id = :id"),
            {"id": span.id},
        )


# Every combination of the three locators a parser can set.
_LOCATOR_COMBINATIONS = [
    {"page_number": page, "slide_number": slide, "section_path": section}
    for page in (None, 1)
    for slide in (None, 1)
    for section in (None, "Brief > Submission")
]

# The canonical locator each block kind requires (the approved contract,
# written out independently of both implementations).
_CANONICAL_LOCATOR = {
    BlockKind.PAGE: "page_number",
    BlockKind.HEADING: "section_path",
    BlockKind.PARAGRAPH: "section_path",
    BlockKind.TABLE: "section_path",
    BlockKind.SLIDE_TEXT: "slide_number",
    BlockKind.SLIDE_NOTES: "slide_number",
}


@pytest.mark.parametrize("kind", list(BlockKind))
async def test_database_and_parsed_block_accept_the_same_locators(
    db_session: AsyncSession, kind: BlockKind
) -> None:
    """Each block kind requires its canonical locator; additional locator fields
    are permitted. ParsedBlock and the database both implement exactly that,
    for every kind and every combination of locators."""
    version = _version(_course())
    db_session.add(version)
    await db_session.flush()

    for ordinal, locator in enumerate(_LOCATOR_COMBINATIONS):
        try:
            ParsedBlock(kind=kind, locator=SpanLocator(**locator), text=EXCERPT)
            app_accepts = True
        except ValidationError:
            app_accepts = False

        try:
            async with db_session.begin_nested():
                db_session.add(_span(version, ordinal=ordinal, block_kind=kind, **locator))
                await db_session.flush()
            db_accepts = True
        except IntegrityError:
            db_accepts = False

        expected = locator[_CANONICAL_LOCATOR[kind]] is not None
        assert app_accepts == expected, f"ParsedBlock: {kind.value} with {locator}"
        assert db_accepts == expected, f"database: {kind.value} with {locator}"


# --- WorkflowRun lineage ----------------------------------------------------


async def test_consumed_resource_version_cannot_be_deleted(db_session: AsyncSession) -> None:
    course = _course()
    version = _version(course)
    db_session.add(
        WorkflowRun(workflow_version="import/v1", course=course, input_versions=[version])
    )
    await db_session.flush()

    with pytest.raises(IntegrityError, match="workflow_run_inputs"):
        await db_session.execute(delete(ResourceVersion).where(ResourceVersion.id == version.id))


async def test_deleting_a_run_removes_its_input_links(db_session: AsyncSession) -> None:
    course = _course()
    run = WorkflowRun(
        workflow_version="import/v1", course=course, input_versions=[_version(course)]
    )
    db_session.add(run)
    await db_session.flush()

    await db_session.execute(delete(WorkflowRun).where(WorkflowRun.id == run.id))

    remaining = await db_session.scalar(select(func.count()).select_from(workflow_run_inputs))
    assert remaining == 0


async def test_run_cannot_finish_before_it_starts(db_session: AsyncSession) -> None:
    course = _course()
    db_session.add(course)
    await db_session.flush()

    with pytest.raises(IntegrityError, match="ck_workflow_runs_finished_after_started"):
        await db_session.execute(
            text(
                "INSERT INTO workflow_runs (workflow_version, course_id, started_at, finished_at) "
                "VALUES ('import/v1', :course_id, now(), now() - interval '1 minute')"
            ),
            {"course_id": course.id},
        )


# --- Enum CHECKs and server defaults (raw SQL bypasses Python entirely) -----


async def test_unknown_enum_value_rejected_by_database(db_session: AsyncSession) -> None:
    with pytest.raises(IntegrityError, match="ck_institutions_lms_type"):
        await db_session.execute(
            text("INSERT INTO institutions (name, lms_type) VALUES ('X', 'blackboard')")
        )


async def test_server_defaults_fill_id_status_and_timestamptz(db_session: AsyncSession) -> None:
    institution_id = await db_session.scalar(
        text("INSERT INTO institutions (name, lms_type) VALUES ('X', 'demo') RETURNING id")
    )
    row = (
        await db_session.execute(
            text(
                "INSERT INTO courses (institution_id, title) VALUES (:iid, 'C') "
                "RETURNING id, status, created_at, updated_at"
            ),
            {"iid": institution_id},
        )
    ).one()

    assert row.id is not None
    assert row.status == CourseStatus.ACTIVE.value
    assert row.created_at.tzinfo is not None
    assert row.updated_at.tzinfo is not None


# --- lazy="raise": no implicit I/O ------------------------------------------


async def test_unloaded_relationship_raises_instead_of_querying(db_session: AsyncSession) -> None:
    version = _version(_course())
    db_session.add(version)
    await db_session.flush()
    course_id = version.resource.course.id
    db_session.expunge_all()

    course = await db_session.scalar(select(Course).where(Course.id == course_id))
    assert course is not None
    with pytest.raises(InvalidRequestError, match="lazy='raise'"):
        _ = course.resources

    loaded = await db_session.scalar(
        select(Course)
        .where(Course.id == course_id)
        .options(selectinload(Course.resources))
        .execution_options(populate_existing=True)
    )
    assert loaded is not None
    assert [r.title for r in loaded.resources] == ["Coursework brief"]
