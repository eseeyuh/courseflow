# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Persist gated assessments: canonical rows, every piece of evidence kept.

- One Assessment per (course, canonical_key). A later source merges into it
  and adds its own EvidenceLinks; its title is the longest one (ties: the
  lexicographically smallest), so the result does not depend on order.
- A candidate is only a candidate. Its status is derived from its evidence
  after every write: verified when it has a verified deadline (row and
  link) or a verified, undisputed weighting; otherwise needs_review. A title
  alone, or words in it ("coursework", "exercise"), never verify it.
- Every occurrence of a non-unique quote is stored as its own exact link,
  needs_review; none is chosen.
- A weighting is set from the first verified source. A verified source
  stating a different one is a disagreement: the weighting becomes unknown
  (NULL), every weighting link of the assessment needs review, and no later
  source sets it again: two values are never silently reconciled.
- One AssessmentDeadline per stated value (date, time, timezone): agreeing
  sources share it, disagreeing ones get their own. A row's status is
  verified once any verified evidence supports it; its timezone_source is
  the strongest any evidence gave (explicit_source over institution_default).
- Every write is idempotent (unique constraints, ON CONFLICT DO NOTHING):
  re-running a unit adds nothing.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, fields

from sqlalchemy import exists, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Assessment, AssessmentDeadline, EvidenceLink
from app.domain.enums import (
    EvidenceField,
    EvidenceSubject,
    TimezoneSource,
    VerificationStatus,
)
from app.extraction.gate import GatedAssessment, GatedDeadline, Located

V = VerificationStatus
_SOURCE_STRENGTH = {
    TimezoneSource.UNKNOWN: 0,
    TimezoneSource.INSTITUTION_DEFAULT: 1,
    TimezoneSource.EXPLICIT_SOURCE: 2,
}


@dataclass(slots=True)
class PersistCounts:
    assessments_created: int = 0
    assessments_merged: int = 0
    deadlines_created: int = 0
    deadlines_merged: int = 0
    links_created: int = 0
    links_existing: int = 0
    weighting_disagreements: int = 0

    def add(self, other: PersistCounts) -> None:
        for item in fields(self):
            setattr(self, item.name, getattr(self, item.name) + getattr(other, item.name))


@dataclass(frozen=True, slots=True)
class Lineage:
    model_call_id: uuid.UUID
    workflow_run_id: uuid.UUID


def _better_title(current: str, candidate: str) -> str:
    return min(current, candidate, key=lambda title: (-len(title), title))


async def _assessment(
    session: AsyncSession, course_id: uuid.UUID, gated: GatedAssessment, counts: PersistCounts
) -> Assessment:
    inserted = await session.execute(
        insert(Assessment)
        .values(
            course_id=course_id,
            canonical_key=gated.canonical_key,
            title=gated.title,
            weighting_percent=None,
            status=V.NEEDS_REVIEW,  # until its facts are written (_derive_status)
        )
        .on_conflict_do_nothing(constraint="uq_assessments_course_key")
        .returning(Assessment.id)
    )
    if inserted.scalar() is None:
        counts.assessments_merged += 1
    else:
        counts.assessments_created += 1
    row = await session.scalar(
        select(Assessment)
        .where(Assessment.course_id == course_id, Assessment.canonical_key == gated.canonical_key)
        .with_for_update()
    )
    if row is None:  # inserted or found above, in this transaction
        raise RuntimeError("assessment vanished during persistence")
    row.title = _better_title(row.title, gated.title)
    return row


async def _links(
    session: AsyncSession,
    counts: PersistCounts,
    evidence: Located,
    status: VerificationStatus,
    field: EvidenceField,
    lineage: Lineage,
    *,
    assessment_id: uuid.UUID | None = None,
    deadline_id: uuid.UUID | None = None,
) -> None:
    """One link per exact occurrence of the quote."""
    for location in evidence.locations:
        created = await session.execute(
            insert(EvidenceLink)
            .values(
                subject_type=(
                    EvidenceSubject.ASSESSMENT_DEADLINE
                    if deadline_id
                    else EvidenceSubject.ASSESSMENT
                ),
                assessment_id=assessment_id,
                assessment_deadline_id=deadline_id,
                field=field,
                source_span_id=location.span_id,
                quote_start=location.quote_start,
                quote_end=location.quote_end,
                verification_status=status,
                model_call_id=lineage.model_call_id,
                workflow_run_id=lineage.workflow_run_id,
            )
            .on_conflict_do_nothing(constraint="uq_evidence_links_fact_quote")
            .returning(EvidenceLink.id)
        )
        if created.scalar() is None:
            counts.links_existing += 1
        else:
            counts.links_created += 1


async def _deadline(
    session: AsyncSession, assessment_id: uuid.UUID, gated: GatedDeadline, counts: PersistCounts
) -> uuid.UUID:
    value, status = gated.value, gated.status
    inserted = await session.execute(
        insert(AssessmentDeadline)
        .values(
            assessment_id=assessment_id,
            due_date=value.due_date,
            due_time=value.due_time,
            timezone=value.timezone,
            timezone_source=value.timezone_source,
            precision=value.precision,
            due_at=value.due_at,
            status=status,
        )
        .on_conflict_do_nothing(constraint="uq_assessment_deadlines_value")
        .returning(AssessmentDeadline.id)
    )
    created = inserted.scalar()
    if created is not None:
        counts.deadlines_created += 1
        return created
    counts.deadlines_merged += 1
    row = await session.scalar(
        select(AssessmentDeadline)
        .where(
            AssessmentDeadline.assessment_id == assessment_id,
            AssessmentDeadline.due_date == value.due_date,
            AssessmentDeadline.due_time.is_not_distinct_from(value.due_time),
            AssessmentDeadline.timezone.is_not_distinct_from(value.timezone),
        )
        .with_for_update()
    )
    if row is None:
        raise RuntimeError("assessment deadline vanished during persistence")
    if status is V.VERIFIED and row.status is not V.VERIFIED:
        row.status, row.due_at = V.VERIFIED, value.due_at
    if _SOURCE_STRENGTH[value.timezone_source] > _SOURCE_STRENGTH[row.timezone_source]:
        row.timezone_source = value.timezone_source
    return row.id


async def _weighting(
    session: AsyncSession,
    row: Assessment,
    gated: GatedAssessment,
    counts: PersistCounts,
    lineage: Lineage,
) -> None:
    if gated.weighting is None:
        return
    status = gated.weighting.status
    weighting_links = (EvidenceLink.assessment_id == row.id) & (
        EvidenceLink.field == EvidenceField.WEIGHTING
    )
    if status is V.VERIFIED:
        if row.weighting_percent is None:
            disputed = await session.scalar(
                select(
                    exists().where(weighting_links, EvidenceLink.verification_status != V.VERIFIED)
                )
            )
            if disputed:
                status = V.NEEDS_REVIEW
            else:
                row.weighting_percent = gated.weighting.percent
        elif row.weighting_percent != gated.weighting.percent:
            counts.weighting_disagreements += 1
            status = V.NEEDS_REVIEW
            row.weighting_percent = None
            await session.execute(
                update(EvidenceLink).where(weighting_links).values(verification_status=status)
            )
    await _links(
        session, counts, gated.weighting.evidence, status, EvidenceField.WEIGHTING, lineage,
        assessment_id=row.id,
    )  # fmt: skip


async def _derive_status(session: AsyncSession, row: Assessment) -> None:
    """Verified only with a verified concrete deadline or a verified weighting."""
    verified_deadline = await session.scalar(
        select(
            exists()
            .where(EvidenceLink.assessment_deadline_id == AssessmentDeadline.id)
            .where(
                AssessmentDeadline.assessment_id == row.id,
                AssessmentDeadline.status == V.VERIFIED,
                EvidenceLink.verification_status == V.VERIFIED,
            )
        )
    )
    verified_weighting = row.weighting_percent is not None and await session.scalar(
        select(
            exists().where(
                EvidenceLink.assessment_id == row.id,
                EvidenceLink.field == EvidenceField.WEIGHTING,
                EvidenceLink.verification_status == V.VERIFIED,
            )
        )
    )
    row.status = V.VERIFIED if verified_deadline or verified_weighting else V.NEEDS_REVIEW


async def persist_unit(
    session: AsyncSession,
    course_id: uuid.UUID,
    assessments: Sequence[GatedAssessment],
    lineage: Lineage,
) -> PersistCounts:
    """Write one unit's gated candidates; the caller owns the transaction."""
    counts = PersistCounts()
    for gated in assessments:
        row = await _assessment(session, course_id, gated, counts)
        await _links(
            session, counts, gated.title_evidence, gated.title_evidence.status,
            EvidenceField.TITLE, lineage, assessment_id=row.id,
        )  # fmt: skip
        if gated.weighting is not None:
            await _weighting(session, row, gated, counts, lineage)
        if gated.deadline is not None:
            deadline_id = await _deadline(session, row.id, gated.deadline, counts)
            await _links(
                session, counts, gated.deadline.evidence, gated.deadline.status,
                EvidenceField.DEADLINE, lineage, deadline_id=deadline_id,
            )  # fmt: skip
        await session.flush()
        await _derive_status(session, row)
    return counts
