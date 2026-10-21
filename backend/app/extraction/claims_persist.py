# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Persist gated requirements and obligations: canonical rows, every piece
of evidence kept.

- Identity is the anchor sentence's key: one AssessmentRequirement per
  (assessment, key), one Obligation per (course, key). The same wording in
  another version or source adds EvidenceLinks, never a row.
- A requirement's text is fixed by its key, so evidence only adds support:
  it is verified once any verified source supports it (as a deadline value
  is). Otherwise it keeps the most important reason, in ``ReviewReason``
  order, so the result does not depend on processing order.
- An obligation aggregates values proposed by several calls (category,
  modality, assessment, due date), so it is verified only while every
  source verifies it. Two sources that disagree on a value make it a
  conflict (``conflicting_*``): the value becomes unknown where it can be
  (modality, assessment, due date; the first category is kept), the row
  needs review, and no later source clears it. A value one source states
  and another leaves out is not a disagreement: the stated one is kept.
- Its canonical action and category are the first proposing call's
  (``model_call_id``); lineage of every link is its ModelCall and run.
- Every write is idempotent (unique constraints, ON CONFLICT DO NOTHING):
  re-running a unit adds nothing.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, fields

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AssessmentRequirement, EvidenceLink, Obligation
from app.domain.enums import EvidenceField, EvidenceSubject, ReviewReason, VerificationStatus
from app.extraction.gate import GatedDeadline, Located
from app.extraction.obligations import GatedObligation
from app.extraction.persist import Lineage
from app.extraction.requirements import GatedRequirement

V = VerificationStatus
R = ReviewReason
_PRIORITY = {reason: i for i, reason in enumerate(ReviewReason)}
CONFLICTS = frozenset(
    {
        R.CONFLICTING_CATEGORY,
        R.CONFLICTING_MODALITY,
        R.CONFLICTING_ASSESSMENT_LINK,
        R.CONFLICTING_DUE_DATE,
    }
)


@dataclass(slots=True)
class ClaimCounts:
    created: int = 0
    merged: int = 0
    conflicts: int = 0
    links_created: int = 0
    links_existing: int = 0

    def add(self, other: ClaimCounts) -> None:
        for item in fields(self):
            setattr(self, item.name, getattr(self, item.name) + getattr(other, item.name))


def first_reason(*reasons: ReviewReason | None) -> ReviewReason | None:
    present = [r for r in reasons if r is not None]
    return min(present, key=_PRIORITY.__getitem__) if present else None


async def _links(
    session: AsyncSession,
    counts: ClaimCounts,
    evidence: Located,
    field: EvidenceField,
    lineage: Lineage,
    subject: dict[str, object],
) -> None:
    """One link per exact occurrence; a non-unique quote's links need review."""
    status = evidence.status
    reason = None if status is V.VERIFIED else R.NON_UNIQUE_QUOTE_LOCATION
    for location in evidence.locations:
        created = await session.execute(
            insert(EvidenceLink)
            .values(
                **subject,
                field=field,
                source_span_id=location.span_id,
                quote_start=location.quote_start,
                quote_end=location.quote_end,
                verification_status=status,
                review_reason=reason,
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


# --- requirements -------------------------------------------------------------------------


async def persist_requirements(
    session: AsyncSession, gated: list[GatedRequirement], lineage: Lineage
) -> ClaimCounts:
    counts = ClaimCounts()
    for requirement in gated:
        inserted = await session.execute(
            insert(AssessmentRequirement)
            .values(
                assessment_id=requirement.assessment.assessment_id,
                canonical_key=requirement.canonical_key,
                text=requirement.text,
                status=requirement.status,
                review_reason=requirement.review_reason,
            )
            .on_conflict_do_nothing(constraint="uq_assessment_requirements_assessment_key")
            .returning(AssessmentRequirement.id)
        )
        row_id = inserted.scalar()
        if row_id is None:
            counts.merged += 1
            row = await session.scalar(
                select(AssessmentRequirement)
                .where(
                    AssessmentRequirement.assessment_id == requirement.assessment.assessment_id,
                    AssessmentRequirement.canonical_key == requirement.canonical_key,
                )
                .with_for_update()
            )
            if row is None:
                raise RuntimeError("requirement vanished during persistence")
            row_id = row.id
            if V.VERIFIED in (row.status, requirement.status):
                row.status, row.review_reason = V.VERIFIED, None
            else:
                row.review_reason = first_reason(row.review_reason, requirement.review_reason)
        else:
            counts.created += 1
        subject = {
            "subject_type": EvidenceSubject.ASSESSMENT_REQUIREMENT,
            "assessment_requirement_id": row_id,
        }
        await _links(
            session, counts, requirement.statement, EvidenceField.REQUIREMENT, lineage, subject
        )
        if requirement.lead_in is not None:
            await _links(
                session, counts, requirement.lead_in, EvidenceField.REQUIREMENT, lineage, subject
            )
    return counts


# --- obligations --------------------------------------------------------------------------


def _due_values(due: GatedDeadline | None) -> dict[str, object]:
    if due is None:
        return {
            "due_date": None,
            "due_time": None,
            "due_timezone": None,
            "due_timezone_source": None,
            "due_precision": None,
            "due_at": None,
        }
    value = due.value
    return {
        "due_date": value.due_date,
        "due_time": value.due_time,
        "due_timezone": value.timezone,
        "due_timezone_source": value.timezone_source,
        "due_precision": value.precision,
        "due_at": value.due_at,
    }


def _merge(row: Obligation, obligation: GatedObligation) -> list[ReviewReason]:
    """Merge one more source into ``row``; the conflicts it found."""
    conflicts = []
    if obligation.category is not row.category:
        conflicts.append(R.CONFLICTING_CATEGORY)
    # The modal outcome: a modality, or why there is none.
    modal_reasons = {R.PERMISSIVE_WORDING, R.MIXED_MODALITY, R.CONFLICTING_MODALITY}
    row_modal = (row.modality, row.review_reason if row.review_reason in modal_reasons else None)
    new_modal = (
        obligation.modality,
        obligation.review_reason if obligation.review_reason in modal_reasons else None,
    )
    if row.review_reason is not R.CONFLICTING_MODALITY and row_modal != new_modal:
        conflicts.append(R.CONFLICTING_MODALITY)
        row.modality = None
    new_assessment = obligation.assessment.assessment_id if obligation.assessment else None
    if row.review_reason is not R.CONFLICTING_ASSESSMENT_LINK and new_assessment is not None:
        if row.assessment_id is None:
            row.assessment_id = new_assessment
        elif row.assessment_id != new_assessment:
            conflicts.append(R.CONFLICTING_ASSESSMENT_LINK)
            row.assessment_id = None
    new_due = _due_values(obligation.due)
    if row.review_reason is not R.CONFLICTING_DUE_DATE and obligation.due is not None:
        stated = (row.due_date, row.due_time, row.due_timezone)
        if row.due_date is None:
            for name, value in new_due.items():
                setattr(row, name, value)
        elif stated != (new_due["due_date"], new_due["due_time"], new_due["due_timezone"]):
            conflicts.append(R.CONFLICTING_DUE_DATE)
            for name in new_due:
                setattr(row, name, None)
    return conflicts


async def persist_obligations(
    session: AsyncSession, course_id: uuid.UUID, gated: list[GatedObligation], lineage: Lineage
) -> ClaimCounts:
    counts = ClaimCounts()
    for obligation in gated:
        inserted = await session.execute(
            insert(Obligation)
            .values(
                course_id=course_id,
                assessment_id=(
                    obligation.assessment.assessment_id if obligation.assessment else None
                ),
                canonical_key=obligation.canonical_key,
                canonical_action=obligation.action,
                category=obligation.category,
                modality=obligation.modality,
                **_due_values(obligation.due),
                status=obligation.status,
                review_reason=obligation.review_reason,
                model_call_id=lineage.model_call_id,
            )
            .on_conflict_do_nothing(constraint="uq_obligations_course_key")
            .returning(Obligation.id)
        )
        row_id = inserted.scalar()
        if row_id is None:
            counts.merged += 1
            row = await session.scalar(
                select(Obligation)
                .where(
                    Obligation.course_id == course_id,
                    Obligation.canonical_key == obligation.canonical_key,
                )
                .with_for_update()
            )
            if row is None:
                raise RuntimeError("obligation vanished during persistence")
            row_id = row.id
            sticky = row.review_reason if row.review_reason in CONFLICTS else None
            conflicts = _merge(row, obligation)
            counts.conflicts += len(conflicts)
            # A conflict, once found, is the reason; then the sources' own.
            reason = (
                sticky
                or first_reason(*conflicts)
                or first_reason(row.review_reason, obligation.review_reason)
            )
            if reason is None:  # both sources verify it, and they agree
                row.status, row.review_reason = V.VERIFIED, None
            else:
                row.status, row.review_reason = V.NEEDS_REVIEW, reason
        else:
            counts.created += 1
        subject = {"subject_type": EvidenceSubject.OBLIGATION, "obligation_id": row_id}
        for evidence in obligation.evidence:
            await _links(session, counts, evidence, EvidenceField.OBLIGATION, lineage, subject)
        if obligation.due is not None:
            await _links(
                session, counts, obligation.due.evidence, EvidenceField.DUE_DATE, lineage, subject
            )
    return counts
