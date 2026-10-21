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
- An obligation row stores only values its evidence supports: a modality
  only when the wording determines it, an assessment only when the link is
  supported, a due date only when verified (an unverified one keeps its
  evidence, for review). One verified source verifies it, as for a
  requirement; an uncertain source adds its evidence and the values the row
  lacks, and never demotes it. Two sources supporting different values
  (category, modality, assessment, due date) are a conflict
  (``conflicting_*``): the value becomes unknown where it can be (the first
  category is kept), the row needs review, and no later source clears it.
- Its canonical action is the first proposing call's (``model_call_id``);
  lineage of every link is its ModelCall and run.
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
from app.extraction.obligations import REASON_ORDER, GatedObligation
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
    *,
    review_reason: ReviewReason | None = None,
) -> None:
    """One link per exact occurrence; a non-unique quote's links need review,
    and so do the links of a fact with its own review_reason."""
    reason = first_reason(
        review_reason, None if evidence.status is V.VERIFIED else R.NON_UNIQUE_QUOTE_LOCATION
    )
    status = V.VERIFIED if reason is None else V.NEEDS_REVIEW
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


def _verified_due(obligation: GatedObligation) -> GatedDeadline | None:
    """The due date a row may store: only a verified one."""
    due = obligation.due
    return due if due is not None and due.status is V.VERIFIED else None


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


def _category_supported(row: Obligation) -> bool:
    """A stored row's category is unsupported only while that is its reason
    (CATEGORY_UNSUPPORTED is always reported first: obligations.REASON_ORDER)."""
    return row.review_reason is not R.CATEGORY_UNSUPPORTED


def _merge(row: Obligation, obligation: GatedObligation) -> list[ReviewReason]:
    """Merge one more source into ``row``: adopt the values it supports that
    the row lacks; the conflicts where both support different values. A value
    already in conflict is not looked at again."""
    conflicts = []
    if row.review_reason is not R.CONFLICTING_CATEGORY and obligation.category_supported:
        if not _category_supported(row):
            row.category = obligation.category
        elif row.category is not obligation.category:
            conflicts.append(R.CONFLICTING_CATEGORY)
    if row.review_reason is not R.CONFLICTING_MODALITY and obligation.modality is not None:
        if row.modality is None:
            row.modality = obligation.modality
        elif row.modality is not obligation.modality:
            conflicts.append(R.CONFLICTING_MODALITY)
            row.modality = None
    if row.review_reason is not R.CONFLICTING_ASSESSMENT_LINK and obligation.assessment:
        new_assessment = obligation.assessment.assessment_id
        if row.assessment_id is None:
            row.assessment_id = new_assessment
        elif row.assessment_id != new_assessment:
            conflicts.append(R.CONFLICTING_ASSESSMENT_LINK)
            row.assessment_id = None
    due = _verified_due(obligation)
    if row.review_reason is not R.CONFLICTING_DUE_DATE and due is not None:
        values = _due_values(due)
        if row.due_date is None:
            for name, value in values.items():
                setattr(row, name, value)
        elif (row.due_date, row.due_time) != (values["due_date"], values["due_time"]):
            conflicts.append(R.CONFLICTING_DUE_DATE)
            for name in values:
                setattr(row, name, None)
    return conflicts


def _merged_status(
    row: Obligation,
    obligation: GatedObligation,
    was: VerificationStatus,
    sticky: ReviewReason | None,
) -> tuple[VerificationStatus, ReviewReason | None]:
    """A conflict, once found, decides. Otherwise one verified source verifies
    the obligation (an uncertain one never demotes it); else the most
    important reason of either source that still applies."""
    if sticky is not None:
        return V.NEEDS_REVIEW, sticky
    if V.VERIFIED in (was, obligation.status):
        return V.VERIFIED, None
    reasons = set(obligation.reasons) | {row.review_reason} - {None}
    # ``row.review_reason`` is still the pre-merge one here: the row's category
    # is supported if it was, or if this source supplied a supported one.
    if _category_supported(row) or obligation.category_supported:
        reasons.discard(R.CATEGORY_UNSUPPORTED)
    else:
        reasons.add(R.CATEGORY_UNSUPPORTED)
    ordered = [r for r in REASON_ORDER if r in reasons]
    if not ordered:  # two needs_review sources always leave a reason
        raise RuntimeError("an unverified obligation lost its review reason")
    return V.NEEDS_REVIEW, ordered[0]


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
                **_due_values(_verified_due(obligation)),
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
            was = row.status
            sticky = row.review_reason if row.review_reason in CONFLICTS else None
            conflicts = _merge(row, obligation)
            counts.conflicts += len(conflicts)
            row.status, row.review_reason = _merged_status(
                row, obligation, was, sticky or first_reason(*conflicts)
            )
        else:
            counts.created += 1
        subject = {"subject_type": EvidenceSubject.OBLIGATION, "obligation_id": row_id}
        for evidence in obligation.evidence:
            await _links(session, counts, evidence, EvidenceField.OBLIGATION, lineage, subject)
        if obligation.due is not None:
            # An unverified due date is not stored as a value; its evidence is
            # kept for review with the due date's own reason.
            await _links(
                session,
                counts,
                obligation.due.evidence,
                EvidenceField.DUE_DATE,
                lineage,
                subject,
                review_reason=obligation.due.review_reason,
            )
    return counts
