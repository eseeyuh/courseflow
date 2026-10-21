# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""The academic and trust layer schema enforces its own invariants:
canonical assessments, typed deadlines with precision and timezone
semantics, and evidence links that cannot be orphaned or misattributed."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import (
    Assessment,
    AssessmentDeadline,
    Course,
    EvidenceLink,
    Institution,
    ModelCall,
    SourceSpan,
)
from app.demo.reset import reset_demo_institution
from app.domain.enums import (
    DeadlinePrecision,
    EvidenceField,
    EvidenceSubject,
    LmsType,
    ModelCallKind,
    ModelCallStatus,
    ModelRole,
    ResourceType,
    ReviewReason,
    TimezoneSource,
    VerificationStatus,
)
from app.evaluation.retrieval.resolve import production_limits
from app.ingestion.schemas import RawSource
from app.ingestion.service import ingest_file
from app.ingestion.storage import LocalRawObjectStore

pytestmark = pytest.mark.anyio

V = VerificationStatus
LONDON = "Europe/London"
BRIEF = (
    b"<!DOCTYPE html><html><body><h1>Coursework 1</h1>"
    b"<p>Submission deadline: Thursday 12 November 2026, 14:00 (UK time).</p></body></html>"
)


@pytest.fixture
def sessions(committed_session: AsyncSession) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(committed_session.bind, expire_on_commit=False)


async def _course(
    sessions: async_sessionmaker[AsyncSession],
    name: str = "Schema Test University",
    lms_type: LmsType = LmsType.UPLOAD,
) -> uuid.UUID:
    async with sessions() as session, session.begin():
        course = Course(
            institution=Institution(name=name, lms_type=lms_type, timezone=LONDON),
            title="Schema course",
        )
        session.add(course)
    return course.id


async def _add(sessions: async_sessionmaker[AsyncSession], *rows: Any) -> None:
    async with sessions() as session, session.begin():
        session.add_all(rows)


async def _rejected(sessions: async_sessionmaker[AsyncSession], row: Any, constraint: str) -> None:
    with pytest.raises(IntegrityError, match=constraint):
        await _add(sessions, row)


def _reasoned(row: dict[str, Any], status_field: str) -> dict[str, Any]:
    """A needs_review row needs a reason unless the test sets one itself."""
    if row.get(status_field) is V.NEEDS_REVIEW and "review_reason" not in row:
        row["review_reason"] = ReviewReason.MISSING_TIMEZONE
    return row


def _assessment(course_id: uuid.UUID, **values: Any) -> Assessment:
    fields: dict[str, Any] = {
        "canonical_key": "coursework 1",
        "title": "Coursework 1",
        "status": V.VERIFIED,
    }
    return Assessment(course_id=course_id, **_reasoned(fields | values, "status"))


def _deadline(assessment_id: uuid.UUID, **values: Any) -> AssessmentDeadline:
    fields: dict[str, Any] = {
        "due_date": date(2026, 11, 12),
        "due_time": time(14, 0),
        "timezone": LONDON,
        "timezone_source": TimezoneSource.EXPLICIT_SOURCE,
        "precision": DeadlinePrecision.DATETIME,
        "due_at": datetime(2026, 11, 12, 14, 0, tzinfo=UTC),  # GMT in November
        "status": V.VERIFIED,
    }
    return AssessmentDeadline(assessment_id=assessment_id, **_reasoned(fields | values, "status"))


async def _span(
    sessions: async_sessionmaker[AsyncSession], course_id: uuid.UUID, tmp_path: Path
) -> uuid.UUID:
    root = tmp_path / "raw"
    root.mkdir(exist_ok=True)
    async with sessions() as session:
        result = await ingest_file(
            session,
            LocalRawObjectStore(root),
            course_id=course_id,
            resource_type=ResourceType.BRIEF,
            source=RawSource(content=BRIEF, display_name="brief.html", source_uri="upload:brief"),
            limits=production_limits(),
        )
        await session.commit()
        span = await session.scalar(
            select(SourceSpan.id)
            .where(SourceSpan.resource_version_id == result.version_id)
            .where(SourceSpan.excerpt.contains("deadline"))
        )
    assert span is not None
    return span


def _link(span_id: uuid.UUID, **values: Any) -> EvidenceLink:
    fields: dict[str, Any] = {
        "field": EvidenceField.TITLE,
        "source_span_id": span_id,
        "quote_start": 0,
        "quote_end": 19,
        "verification_status": V.VERIFIED,
    }
    return EvidenceLink(**_reasoned(fields | values, "verification_status"))


# --- assessments --------------------------------------------------------------------------


async def test_an_assessment_is_canonical_per_course(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    course_id = await _course(sessions)
    other_course = await _course(sessions, "Other University")
    await _add(sessions, _assessment(course_id), _assessment(other_course))
    await _rejected(sessions, _assessment(course_id), "uq_assessments_course_key")


@pytest.mark.parametrize(
    ("values", "constraint"),
    [
        ({"canonical_key": "Coursework 1"}, "canonical_key_normalised"),
        ({"canonical_key": ""}, "canonical_key_normalised"),
        ({"title": "  "}, "title_nonblank"),
        ({"weighting_percent": 101}, "weighting_percent_range"),
        ({"weighting_percent": -1}, "weighting_percent_range"),
    ],
)
async def test_assessment_values_are_checked(
    sessions: async_sessionmaker[AsyncSession], values: dict[str, Any], constraint: str
) -> None:
    course_id = await _course(sessions)
    await _rejected(sessions, _assessment(course_id, **values), constraint)


async def test_an_institution_timezone_may_be_unknown_but_not_blank(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    await _add(sessions, Institution(name="Unknown zone", lms_type=LmsType.UPLOAD))
    await _rejected(
        sessions,
        Institution(name="Blank zone", lms_type=LmsType.UPLOAD, timezone=" "),
        "timezone_nonblank",
    )


# --- deadlines ----------------------------------------------------------------------------


async def _assessment_id(sessions: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    row = _assessment(await _course(sessions))
    await _add(sessions, row)
    return row.id


async def test_the_deadline_shapes_the_contract_allows(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    assessment_id = await _assessment_id(sessions)
    await _add(
        sessions,
        _deadline(assessment_id),  # date, time and a stated timezone: an instant
        # A date with no time of day: no instant, never an invented time.
        _deadline(
            assessment_id,
            due_date=date(2026, 12, 1),
            due_time=None,
            precision=DeadlinePrecision.DATE,
            due_at=None,
        ),
        # A local time, timezone not stated and none to fall back on: not verifiable.
        _deadline(
            assessment_id,
            due_date=date(2026, 12, 2),
            timezone=None,
            timezone_source=TimezoneSource.UNKNOWN,
            due_at=None,
            status=V.NEEDS_REVIEW,
        ),
        # Not stated, so the institution's own timezone.
        _deadline(
            assessment_id,
            due_date=date(2026, 11, 13),
            due_time=time(23, 59),
            timezone_source=TimezoneSource.INSTITUTION_DEFAULT,
            due_at=datetime(2026, 11, 13, 23, 59, tzinfo=UTC),
        ),
    )


@pytest.mark.parametrize(
    ("values", "constraint"),
    [
        ({"precision": DeadlinePrecision.DATE, "due_at": None}, "time_fits_precision"),
        ({"due_time": None, "due_at": None, "status": V.NEEDS_REVIEW}, "time_fits_precision"),
        ({"timezone_source": TimezoneSource.UNKNOWN}, "timezone_sourced"),
        ({"timezone": None, "due_at": None, "status": V.NEEDS_REVIEW}, "timezone_sourced"),
        ({"timezone": " "}, "timezone_nonblank"),
        (
            {"timezone": None, "timezone_source": TimezoneSource.UNKNOWN, "status": V.NEEDS_REVIEW},
            "instant_needs_time_and_timezone",
        ),
        ({"due_at": None}, "verified_datetime_has_instant"),
    ],
)
async def test_inconsistent_deadlines_are_refused(
    sessions: async_sessionmaker[AsyncSession], values: dict[str, Any], constraint: str
) -> None:
    assessment_id = await _assessment_id(sessions)
    await _rejected(sessions, _deadline(assessment_id, **values), constraint)


async def test_one_row_per_deadline_value_even_without_a_time(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """Agreeing sources share one row (NULLs are not distinct); a disagreeing
    source is a second row for the same assessment."""
    assessment_id = await _assessment_id(sessions)
    date_only = {"due_time": None, "precision": DeadlinePrecision.DATE, "due_at": None}
    await _add(sessions, _deadline(assessment_id, **date_only))
    await _rejected(sessions, _deadline(assessment_id, **date_only), "uq_assessment_deadlines")
    await _add(sessions, _deadline(assessment_id))
    await _add(
        sessions,
        _deadline(
            assessment_id,
            due_date=date(2026, 11, 13),
            due_at=datetime(2026, 11, 13, 14, 0, tzinfo=UTC),
        ),
    )
    async with sessions() as session:
        count = await session.scalar(select(func.count()).select_from(AssessmentDeadline))
    assert count == 3


# --- evidence links -----------------------------------------------------------------------


async def _claims(
    sessions: async_sessionmaker[AsyncSession], tmp_path: Path
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    course_id = await _course(sessions)
    span_id = await _span(sessions, course_id, tmp_path)
    assessment = _assessment(course_id)
    await _add(sessions, assessment)
    deadline = _deadline(assessment.id)
    await _add(sessions, deadline)
    return assessment.id, deadline.id, span_id


async def test_two_sources_for_one_fact_are_two_links(
    sessions: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    assessment_id, deadline_id, span_id = await _claims(sessions, tmp_path)
    title = {"subject_type": EvidenceSubject.ASSESSMENT, "assessment_id": assessment_id}
    await _add(
        sessions,
        _link(span_id, **title),
        _link(span_id, **title, quote_start=20, quote_end=30),
        _link(
            span_id,
            subject_type=EvidenceSubject.ASSESSMENT_DEADLINE,
            assessment_deadline_id=deadline_id,
            field=EvidenceField.DEADLINE,
        ),
    )
    # The same quote for the same fact is not a second piece of evidence.
    await _rejected(sessions, _link(span_id, **title), "uq_evidence_links_fact_quote")


@pytest.mark.parametrize(
    ("subject", "values", "constraint"),
    [
        ("both", {"subject_type": EvidenceSubject.ASSESSMENT}, "one_subject"),
        ("deadline", {"subject_type": EvidenceSubject.ASSESSMENT}, "one_subject"),
        ("none", {"subject_type": EvidenceSubject.ASSESSMENT}, "one_subject"),
        ("assessment", {"field": EvidenceField.DEADLINE}, "field_fits_subject"),
        (
            "deadline",
            {"subject_type": EvidenceSubject.ASSESSMENT_DEADLINE, "field": EvidenceField.TITLE},
            "field_fits_subject",
        ),
        ("assessment", {"quote_start": 5, "quote_end": 5}, "quote_ordered"),
        ("assessment", {"quote_start": -1}, "quote_ordered"),
    ],
)
async def test_misattributed_evidence_is_refused(
    sessions: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    subject: str,
    values: dict[str, Any],
    constraint: str,
) -> None:
    assessment_id, deadline_id, span_id = await _claims(sessions, tmp_path)
    subjects: dict[str, Any] = {
        "both": {"assessment_id": assessment_id, "assessment_deadline_id": deadline_id},
        "deadline": {"assessment_deadline_id": deadline_id},
        "none": {},
        "assessment": {"assessment_id": assessment_id},
    }
    base: dict[str, Any] = {"subject_type": EvidenceSubject.ASSESSMENT}
    await _rejected(sessions, _link(span_id, **(base | subjects[subject] | values)), constraint)


async def test_evidence_goes_with_its_claim_and_never_outlives_it(
    sessions: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    assessment_id, deadline_id, span_id = await _claims(sessions, tmp_path)
    async with sessions() as session, session.begin():
        call = ModelCall(
            call_kind=ModelCallKind.CHAT_STRUCTURED,
            provider="test",
            model="test-model",
            model_role=ModelRole.FAST,
            prompt_version="assessment-extraction/v1",
            output_schema="AssessmentExtraction",
            status=ModelCallStatus.SUCCEEDED,
            output_summary="{}",
            attempts=1,
            latency_ms=1,
            started_at=datetime.now(UTC),
            finished_at=datetime.now(UTC),
        )
        session.add(call)
    await _add(
        sessions,
        _link(
            span_id,
            subject_type=EvidenceSubject.ASSESSMENT,
            assessment_id=assessment_id,
            model_call_id=call.id,
        ),
        _link(
            span_id,
            subject_type=EvidenceSubject.ASSESSMENT_DEADLINE,
            assessment_deadline_id=deadline_id,
            field=EvidenceField.DEADLINE,
            model_call_id=call.id,
        ),
    )
    # The span and the call the claims rest on cannot be deleted under them.
    for row, target in ((SourceSpan, span_id), (ModelCall, call.id)):
        with pytest.raises(IntegrityError, match="fk_evidence_links"):
            async with sessions() as session, session.begin():
                await session.execute(delete(row).where(row.id == target))
    # Deleting the claim removes its deadlines and all their evidence.
    async with sessions() as session, session.begin():
        await session.execute(delete(Assessment).where(Assessment.id == assessment_id))
    async with sessions() as session:
        counts = [
            await session.scalar(select(func.count()).select_from(model))
            for model in (AssessmentDeadline, EvidenceLink)
        ]
    assert counts == [0, 0]


async def test_reset_removes_a_demo_institutions_assessments_and_their_evidence(
    sessions: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    demo_course = await _course(sessions, "Northbridge University", LmsType.DEMO)
    kept_course = await _course(sessions)
    span_id = await _span(sessions, demo_course, tmp_path)
    demo, kept = _assessment(demo_course), _assessment(kept_course)
    await _add(sessions, demo, kept)
    await _add(
        sessions,
        _link(span_id, subject_type=EvidenceSubject.ASSESSMENT, assessment_id=demo.id),
    )

    await reset_demo_institution(sessions, "Northbridge University", LmsType.DEMO)
    async with sessions() as session:
        remaining = (await session.scalars(select(Assessment.id))).all()
        links = await session.scalar(select(func.count()).select_from(EvidenceLink))
    assert remaining == [kept.id] and links == 0


@pytest.mark.parametrize(
    ("values", "constraint"),
    [
        ({"status": V.NEEDS_REVIEW, "review_reason": None}, "review_reason_iff_review"),
        ({"review_reason": ReviewReason.WEEKDAY_MISMATCH}, "review_reason_iff_review"),
    ],
)
async def test_a_review_reason_is_present_exactly_when_review_is_needed(
    sessions: async_sessionmaker[AsyncSession], values: dict[str, Any], constraint: str
) -> None:
    assessment_id = await _assessment_id(sessions)
    deadline = _deadline(assessment_id, due_at=None, **values)
    await _rejected(sessions, deadline, constraint)
    course_id = await _course(sessions, "Reasons U")
    await _rejected(sessions, _assessment(course_id, **values), constraint)
