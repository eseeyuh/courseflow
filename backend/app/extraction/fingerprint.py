# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Deterministic claim fingerprints: the same claim, extracted again, from
any run, with any evidence, has the same fingerprint.

A fingerprint is the SHA-256 of a versioned canonical serialisation of the
claim's existing domain identity, the same identity deduplication uses:

- course: its LMS identity (institution name and LMS type, the course's
  external ID); a course with no external ID (an upload) has only its row
  ID, so that is its identity;
- Assessment: course + ``canonical_key``;
- AssessmentDeadline: its assessment + the stated value (local date, local
  time, IANA timezone; the precision follows from the time);
- AssessmentRequirement: its assessment + ``canonical_key``;
- Obligation: course + ``canonical_key``.

Never an input: a claim's own row ID, ModelCall, WorkflowRun, EvidenceLink,
SourceSpan or ResourceVersion ID, a status or a review reason. A change of
version or evidence leaves the fingerprint; a change of the claim itself
(another statement, another deadline value) changes it.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Sequence
from datetime import date, time

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Assessment,
    AssessmentDeadline,
    AssessmentRequirement,
    Course,
    Institution,
    Obligation,
)

FINGERPRINT_VERSION = "claim-fingerprint/v1"

CourseIdentity = tuple[str, ...]


def course_identity(course: Course, institution: Institution) -> CourseIdentity:
    if course.external_id is not None:
        return ("lms", institution.lms_type.value, institution.name, course.external_id)
    return ("course", str(course.id))  # no LMS identity: the row is the course


def _digest(kind: str, *parts: object) -> str:
    payload = json.dumps(
        [FINGERPRINT_VERSION, kind, *parts], ensure_ascii=False, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def assessment_fingerprint(course: CourseIdentity, canonical_key: str) -> str:
    return _digest("assessment", list(course), canonical_key)


def deadline_fingerprint(
    course: CourseIdentity,
    assessment_key: str,
    due_date: date,
    due_time: time | None,
    timezone: str | None,
) -> str:
    local_time = due_time.strftime("%H:%M") if due_time is not None else None
    return _digest(
        "assessment_deadline", list(course), assessment_key, due_date.isoformat(),
        local_time, timezone,
    )  # fmt: skip


def requirement_fingerprint(course: CourseIdentity, assessment_key: str, canonical_key: str) -> str:
    return _digest("assessment_requirement", list(course), assessment_key, canonical_key)


def obligation_fingerprint(course: CourseIdentity, canonical_key: str) -> str:
    return _digest("obligation", list(course), canonical_key)


async def course_fingerprints(
    session: AsyncSession, course_id: uuid.UUID
) -> dict[str, dict[uuid.UUID, str]]:
    """Every stored claim of a course: kind -> row ID -> fingerprint."""
    row = (
        await session.execute(
            select(Course, Institution)
            .join(Institution, Institution.id == Course.institution_id)
            .where(Course.id == course_id)
        )
    ).one_or_none()
    if row is None:
        raise LookupError(f"course {course_id} does not exist")
    course = course_identity(*row)
    assessments: Sequence[Assessment] = (
        await session.scalars(select(Assessment).where(Assessment.course_id == course_id))
    ).all()
    keys = {a.id: a.canonical_key for a in assessments}
    deadlines = (
        await session.scalars(
            select(AssessmentDeadline).where(AssessmentDeadline.assessment_id.in_(keys))
        )
    ).all()
    requirements = (
        await session.scalars(
            select(AssessmentRequirement).where(AssessmentRequirement.assessment_id.in_(keys))
        )
    ).all()
    obligations = (
        await session.scalars(select(Obligation).where(Obligation.course_id == course_id))
    ).all()
    return {
        "assessment": {a.id: assessment_fingerprint(course, a.canonical_key) for a in assessments},
        "assessment_deadline": {
            d.id: deadline_fingerprint(
                course, keys[d.assessment_id], d.due_date, d.due_time, d.timezone
            )
            for d in deadlines
        },
        "assessment_requirement": {
            r.id: requirement_fingerprint(course, keys[r.assessment_id], r.canonical_key)
            for r in requirements
        },
        "obligation": {o.id: obligation_fingerprint(course, o.canonical_key) for o in obligations},
    }
