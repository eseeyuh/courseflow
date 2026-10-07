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
from app.domain.enums import CourseStatus, LmsType, ResourceType

pytestmark = pytest.mark.anyio

HASH_A = "a" * 64
HASH_B = "b" * 64


def _course() -> Course:
    institution = Institution(name="Northbridge University", lms_type=LmsType.DEMO)
    return Course(institution=institution, title="Data Systems", external_id="DS101")


def _version(course: Course, *, content_hash: str = HASH_A) -> ResourceVersion:
    resource = Resource(course=course, type=ResourceType.BRIEF, title="Coursework brief")
    return ResourceVersion(resource=resource, version_number=1, content_hash=content_hash)


def _span(version: ResourceVersion, **fields: Any) -> SourceSpan:
    fields.setdefault("excerpt", "Submit the report by 31 October at 16:00.")
    return SourceSpan(resource_version=version, **fields)


# --- SourceSpan locator / offset / evidence contract ----------------------


@pytest.mark.parametrize(
    "fields",
    [
        {"page_number": 3, "start_offset": 120, "end_offset": 162},
        {"page_number": 1, "start_offset": 0, "end_offset": 1},
        {"slide_number": 7},
        {"section_path": "2 > 2.3 Marking criteria"},
        {"timestamp_seconds": Decimal("754.250")},
        {"page_number": 2, "section_path": "Assessment"},
    ],
    ids=["page+offsets", "offset-zero", "slide", "section", "timestamp", "page+section"],
)
async def test_valid_source_spans_are_accepted(
    db_session: AsyncSession, fields: dict[str, Any]
) -> None:
    span = _span(_version(_course()), **fields)
    db_session.add(span)
    await db_session.flush()

    stored = await db_session.scalar(select(SourceSpan).where(SourceSpan.id == span.id))
    assert stored is not None
    assert stored.excerpt.startswith("Submit")


@pytest.mark.parametrize(
    ("fields", "constraint"),
    [
        ({}, "ck_source_spans_locator_present"),
        ({"page_number": 0}, "ck_source_spans_page_positive"),
        ({"slide_number": 0}, "ck_source_spans_slide_positive"),
        ({"section_path": "   "}, "ck_source_spans_section_nonblank"),
        ({"timestamp_seconds": Decimal("-0.5")}, "ck_source_spans_timestamp_nonnegative"),
        ({"page_number": 1, "start_offset": 5}, "ck_source_spans_offsets_paired"),
        ({"page_number": 1, "end_offset": 5}, "ck_source_spans_offsets_paired"),
        ({"page_number": 1, "start_offset": 5, "end_offset": 5}, "ck_source_spans_offsets_ordered"),
        ({"page_number": 1, "start_offset": 9, "end_offset": 5}, "ck_source_spans_offsets_ordered"),
        (
            {"page_number": 1, "start_offset": -1, "end_offset": 5},
            "ck_source_spans_offsets_ordered",
        ),
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
