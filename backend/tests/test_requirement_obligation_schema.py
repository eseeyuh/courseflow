# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""The requirement and obligation schema enforces its own invariants:
canonical rows per assessment or course, a verified obligation always has a
modality, an explicit due date is typed like a deadline, a related
assessment is of the same course, and evidence fits its subject."""

from __future__ import annotations

import hashlib
import re
import uuid
from datetime import UTC, date, datetime, time
from enum import StrEnum
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import Assessment, AssessmentRequirement, EvidenceLink, Obligation
from app.demo.reset import reset_demo_institution
from app.domain.enums import (
    DeadlinePrecision,
    EvidenceField,
    EvidenceSubject,
    LmsType,
    ObligationCategory,
    ObligationModality,
    ReviewReason,
    TimezoneSource,
    VerificationStatus,
)
from tests.test_assessment_schema import LONDON, _add, _assessment, _course, _link, _rejected, _span

pytestmark = pytest.mark.anyio

V = VerificationStatus
KEY = hashlib.sha256(b"before you submit your project specification").hexdigest()
OTHER_KEY = hashlib.sha256(b"meet your supervisor").hexdigest()


@pytest.fixture
def sessions(committed_session: AsyncSession) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(committed_session.bind, expire_on_commit=False)


def _reasoned(row: dict[str, Any]) -> dict[str, Any]:
    if row.get("status") is V.NEEDS_REVIEW and "review_reason" not in row:
        row["review_reason"] = ReviewReason.PERMISSIVE_WORDING
    return row


def _requirement(assessment_id: uuid.UUID, **values: Any) -> AssessmentRequirement:
    fields: dict[str, Any] = {
        "canonical_key": KEY,
        "text": "1. A focused research question.",
        "status": V.VERIFIED,
    }
    return AssessmentRequirement(assessment_id=assessment_id, **_reasoned(fields | values))


def _obligation(course_id: uuid.UUID, **values: Any) -> Obligation:
    fields: dict[str, Any] = {
        "canonical_key": KEY,
        "canonical_action": "Have your supervisor review a complete specification draft",
        "category": ObligationCategory.SUPERVISOR_REVIEW,
        "modality": ObligationModality.MANDATORY,
        "status": V.VERIFIED,
    }
    return Obligation(course_id=course_id, **_reasoned(fields | values))


UNVERIFIED: dict[str, Any] = {
    "status": V.NEEDS_REVIEW,
    "review_reason": ReviewReason.MISSING_TIMEZONE,
}
# An explicit due date, typed exactly like an assessment deadline.
DUE_DATETIME: dict[str, Any] = {
    "due_date": date(2026, 11, 16),
    "due_time": time(12, 0),
    "due_timezone": LONDON,
    "due_timezone_source": TimezoneSource.EXPLICIT_SOURCE,
    "due_precision": DeadlinePrecision.DATETIME,
    "due_at": datetime(2026, 11, 16, 12, 0, tzinfo=UTC),
}


async def _assessment_in(sessions: async_sessionmaker[AsyncSession], course_id: uuid.UUID) -> Any:
    assessment = _assessment(course_id)
    await _add(sessions, assessment)
    return assessment.id


# --- requirements -------------------------------------------------------------------------


async def test_a_requirement_is_canonical_per_assessment(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    course_id = await _course(sessions)
    first, second = _assessment(course_id), _assessment(course_id, canonical_key="exam")
    await _add(sessions, first, second)
    # The same statement for two assessments is two requirements.
    await _add(sessions, _requirement(first.id), _requirement(second.id))
    await _rejected(sessions, _requirement(first.id), "uq_assessment_requirements_assessment_key")


@pytest.mark.parametrize(
    ("values", "constraint"),
    [
        ({"canonical_key": "a focused research question"}, "canonical_key_sha256_hex"),
        ({"canonical_key": KEY.upper()}, "canonical_key_sha256_hex"),
        ({"text": "  "}, "text_nonblank_bounded"),
        ({"text": "x" * 2001}, "text_nonblank_bounded"),
        ({"status": V.NEEDS_REVIEW, "review_reason": None}, "review_reason_iff_review"),
        ({"review_reason": ReviewReason.NO_REQUIREMENT_LANGUAGE}, "review_reason_iff_review"),
    ],
)
async def test_requirement_values_are_checked(
    sessions: async_sessionmaker[AsyncSession], values: dict[str, Any], constraint: str
) -> None:
    assessment_id = await _assessment_in(sessions, await _course(sessions))
    await _rejected(sessions, _requirement(assessment_id, **values), constraint)


# --- obligations --------------------------------------------------------------------------


async def test_the_obligation_shapes_the_contract_allows(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    course_id = await _course(sessions)
    assessment_id = await _assessment_in(sessions, course_id)
    keys = iter(hashlib.sha256(str(i).encode()).hexdigest() for i in range(10))
    await _add(
        sessions,
        # No due date, no assessment: the common case.
        _obligation(course_id, canonical_key=next(keys)),
        # Related to an assessment of the same course.
        _obligation(course_id, canonical_key=next(keys), assessment_id=assessment_id),
        # Recommended, not required.
        _obligation(
            course_id,
            canonical_key=next(keys),
            category=ObligationCategory.OTHER,
            modality=ObligationModality.RECOMMENDED,
        ),
        # Permissive wording: no modality, so only ever needs_review.
        _obligation(course_id, canonical_key=next(keys), modality=None, status=V.NEEDS_REVIEW),
        # An explicit due date and time with its instant.
        _obligation(course_id, canonical_key=next(keys), **DUE_DATETIME),
        # A date with no time: no time invented, no instant.
        _obligation(
            course_id,
            canonical_key=next(keys),
            due_date=date(2026, 11, 16),
            due_timezone=LONDON,
            due_timezone_source=TimezoneSource.INSTITUTION_DEFAULT,
            due_precision=DeadlinePrecision.DATE,
        ),
        # A local time with no known timezone: no instant, needs review.
        _obligation(
            course_id,
            canonical_key=next(keys),
            **DUE_DATETIME
            | {"due_timezone": None, "due_at": None, "due_timezone_source": TimezoneSource.UNKNOWN}
            | UNVERIFIED,
        ),
    )
    async with sessions() as session:
        count = await session.scalar(select(func.count()).select_from(Obligation))
    assert count == 7


async def test_an_obligation_is_canonical_per_course(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    first, second = await _course(sessions), await _course(sessions, "Second U")
    await _add(sessions, _obligation(first), _obligation(second))
    await _rejected(sessions, _obligation(first), "uq_obligations_course_key")


@pytest.mark.parametrize(
    ("values", "constraint"),
    [
        ({"canonical_key": "meet your supervisor"}, "canonical_key_sha256_hex"),
        ({"canonical_action": " "}, "canonical_action_nonblank_bounded"),
        ({"canonical_action": "x" * 301}, "canonical_action_nonblank_bounded"),
        ({"modality": None}, "verified_has_modality"),
        ({"status": V.NEEDS_REVIEW, "review_reason": None}, "review_reason_iff_review"),
        ({"review_reason": ReviewReason.PERMISSIVE_WORDING}, "review_reason_iff_review"),
        # Due fields without a due date.
        ({"due_time": time(12, 0)}, "due_fields_need_date"),
        ({"due_timezone": LONDON}, "due_fields_need_date"),
        ({"due_precision": DeadlinePrecision.DATE}, "due_fields_need_date"),
        # A due date must say how precise it is and where its timezone came from.
        ({"due_date": date(2026, 11, 16)}, "due_date_typed"),
        # (Each case breaks exactly one rule: unverified where the instant rule would also fire.)
        (
            DUE_DATETIME | {"due_time": None, "due_at": None, **UNVERIFIED},
            "due_time_fits_precision",
        ),
        (
            DUE_DATETIME | {"due_precision": DeadlinePrecision.DATE, "due_at": None},
            "due_time_fits_precision",
        ),
        (
            DUE_DATETIME | {"due_timezone": None, "due_at": None, **UNVERIFIED},
            "due_timezone_sourced",
        ),
        (DUE_DATETIME | {"due_timezone": " "}, "due_timezone_nonblank"),
        (
            DUE_DATETIME | {"due_timezone": None, "due_timezone_source": TimezoneSource.UNKNOWN},
            "due_instant_needs_time_and_timezone",
        ),
        (DUE_DATETIME | {"due_at": None}, "verified_due_datetime_has_instant"),
    ],
)
async def test_inconsistent_obligations_are_refused(
    sessions: async_sessionmaker[AsyncSession], values: dict[str, Any], constraint: str
) -> None:
    course_id = await _course(sessions)
    await _rejected(sessions, _obligation(course_id, **values), constraint)


async def test_an_obligations_assessment_is_of_the_same_course(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    course_id, other_course = await _course(sessions), await _course(sessions, "Other U")
    foreign = await _assessment_in(sessions, other_course)
    await _rejected(
        sessions,
        _obligation(course_id, assessment_id=foreign),
        "fk_obligations_assessment_same_course",
    )
    # An assessment an obligation rests on cannot be deleted under it.
    await _add(sessions, _obligation(other_course, assessment_id=foreign))
    with pytest.raises(IntegrityError, match="fk_obligations_assessment_same_course"):
        async with sessions() as session, session.begin():
            await session.execute(delete(Assessment).where(Assessment.id == foreign))


# --- evidence -----------------------------------------------------------------------------


async def _claims(
    sessions: async_sessionmaker[AsyncSession], tmp_path: Path
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    course_id = await _course(sessions)
    assessment_id = await _assessment_in(sessions, course_id)
    requirement, obligation = _requirement(assessment_id), _obligation(course_id)
    await _add(sessions, requirement, obligation)
    span_id = await _span(sessions, course_id, tmp_path)
    return assessment_id, requirement.id, obligation.id, span_id


async def test_requirements_and_obligations_carry_their_own_evidence(
    sessions: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    _, requirement_id, obligation_id, span_id = await _claims(sessions, tmp_path)
    requirement = {
        "subject_type": EvidenceSubject.ASSESSMENT_REQUIREMENT,
        "assessment_requirement_id": requirement_id,
        "field": EvidenceField.REQUIREMENT,
    }
    obligation = {"subject_type": EvidenceSubject.OBLIGATION, "obligation_id": obligation_id}
    await _add(
        sessions,
        _link(span_id, **requirement),
        # Repeated wording elsewhere is one more link, not one more claim.
        _link(span_id, **requirement, quote_start=20, quote_end=30),
        _link(span_id, **obligation, field=EvidenceField.OBLIGATION),
        _link(span_id, **obligation, field=EvidenceField.DUE_DATE),
    )
    await _rejected(sessions, _link(span_id, **requirement), "uq_evidence_links_fact_quote")


@pytest.mark.parametrize(
    ("subject", "values", "constraint"),
    [
        ("requirement", {"field": EvidenceField.OBLIGATION}, "field_fits_subject"),
        ("requirement", {"field": EvidenceField.TITLE}, "field_fits_subject"),
        ("obligation", {"field": EvidenceField.REQUIREMENT}, "field_fits_subject"),
        ("obligation", {"field": EvidenceField.DEADLINE}, "field_fits_subject"),
        ("assessment", {"field": EvidenceField.REQUIREMENT}, "field_fits_subject"),
        ("assessment", {"field": EvidenceField.DUE_DATE}, "field_fits_subject"),
        # The column set is not the one subject_type names, or two are set.
        (
            "requirement",
            {"subject_type": EvidenceSubject.OBLIGATION, "field": EvidenceField.OBLIGATION},
            "one_subject",
        ),
        ("both", {"field": EvidenceField.OBLIGATION}, "one_subject"),
    ],
)
async def test_misattributed_requirement_or_obligation_evidence_is_refused(
    sessions: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    subject: str,
    values: dict[str, Any],
    constraint: str,
) -> None:
    assessment_id, requirement_id, obligation_id, span_id = await _claims(sessions, tmp_path)
    subjects: dict[str, dict[str, Any]] = {
        "requirement": {
            "subject_type": EvidenceSubject.ASSESSMENT_REQUIREMENT,
            "assessment_requirement_id": requirement_id,
            "field": EvidenceField.REQUIREMENT,
        },
        "obligation": {
            "subject_type": EvidenceSubject.OBLIGATION,
            "obligation_id": obligation_id,
            "field": EvidenceField.OBLIGATION,
        },
        "assessment": {"subject_type": EvidenceSubject.ASSESSMENT, "assessment_id": assessment_id},
        "both": {
            "subject_type": EvidenceSubject.OBLIGATION,
            "obligation_id": obligation_id,
            "assessment_requirement_id": requirement_id,
        },
    }
    await _rejected(sessions, _link(span_id, **(subjects[subject] | values)), constraint)


async def test_evidence_goes_with_its_requirement_or_obligation(
    sessions: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    assessment_id, requirement_id, obligation_id, span_id = await _claims(sessions, tmp_path)
    await _add(
        sessions,
        _link(
            span_id,
            subject_type=EvidenceSubject.ASSESSMENT_REQUIREMENT,
            assessment_requirement_id=requirement_id,
            field=EvidenceField.REQUIREMENT,
        ),
        _link(
            span_id,
            subject_type=EvidenceSubject.OBLIGATION,
            obligation_id=obligation_id,
            field=EvidenceField.OBLIGATION,
        ),
    )
    async with sessions() as session, session.begin():
        await session.execute(delete(Obligation).where(Obligation.id == obligation_id))
        # An assessment's requirements and their evidence go with it.
        await session.execute(delete(Assessment).where(Assessment.id == assessment_id))
    async with sessions() as session:
        counts = [
            await session.scalar(select(func.count()).select_from(model))
            for model in (AssessmentRequirement, EvidenceLink)
        ]
    assert counts == [0, 0]


async def test_reset_removes_a_demo_institutions_requirements_and_obligations(
    sessions: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    demo_course = await _course(sessions, "Northbridge University", LmsType.DEMO)
    kept_course = await _course(sessions)
    span_id = await _span(sessions, demo_course, tmp_path)
    demo_assessment = await _assessment_in(sessions, demo_course)
    requirement = _requirement(demo_assessment)
    # The demo obligation rests on the demo assessment, so it must go first.
    demo = _obligation(demo_course, assessment_id=demo_assessment)
    kept = _obligation(kept_course, canonical_key=OTHER_KEY)
    await _add(sessions, requirement, demo, kept)
    await _add(
        sessions,
        _link(
            span_id,
            subject_type=EvidenceSubject.OBLIGATION,
            obligation_id=demo.id,
            field=EvidenceField.OBLIGATION,
        ),
    )

    await reset_demo_institution(sessions, "Northbridge University", LmsType.DEMO)
    async with sessions() as session:
        remaining = (await session.scalars(select(Obligation.id))).all()
        requirements = await session.scalar(select(func.count()).select_from(AssessmentRequirement))
        links = await session.scalar(select(func.count()).select_from(EvidenceLink))
    assert remaining == [kept.id] and requirements == 0 and links == 0


# --- vocabularies -------------------------------------------------------------------------

ENUM_CHECKS: list[tuple[str, type[StrEnum]]] = [
    *(
        (f"ck_{table}_review_reason", ReviewReason)
        for table in (
            "assessments",
            "assessment_deadlines",
            "evidence_links",
            "assessment_requirements",
            "obligations",
        )
    ),
    ("ck_evidence_links_evidence_subject", EvidenceSubject),
    ("ck_evidence_links_evidence_field", EvidenceField),
    ("ck_obligations_obligation_category", ObligationCategory),
    ("ck_obligations_obligation_modality", ObligationModality),
]


@pytest.mark.parametrize(("constraint", "enum"), ENUM_CHECKS, ids=[c for c, _ in ENUM_CHECKS])
async def test_the_migrated_vocabulary_is_exactly_the_domain_enum(
    committed_session: AsyncSession, constraint: str, enum: type[StrEnum]
) -> None:
    """Alembic does not compare CHECK constraints, so a value added to an enum
    without a migration would only fail at insert time."""
    definition = await committed_session.scalar(
        text("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = :name"),
        {"name": constraint},
    )
    assert definition is not None
    allowed = set(re.findall(r"'([a-z_]+)'::", definition))
    assert allowed == {member.value for member in enum}
