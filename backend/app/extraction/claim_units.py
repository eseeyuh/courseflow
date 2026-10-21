# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Units for requirement and obligation extraction: an assessment unit's
spans plus the course's assessments, under local IDs.

Assessments are shown to the model as A1, A2, ... (issued here, per unit);
only ``resolve_assessment`` maps one back to a real assessment. Their titles
come from earlier extraction of LMS text, so they are untrusted and appear
only inside the nonce-delimited material, sanitised like a resource title.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.prompts import PromptFiles
from app.ai.provider import ChatMessage
from app.db.models import Assessment, EvidenceLink, ResourceVersion, SourceSpan
from app.domain.enums import EvidenceField
from app.extraction.units import ExtractionUnit, load_units, new_nonce, render_material, safe_title


@dataclass(frozen=True, slots=True)
class AssessmentRef:
    local_id: str
    assessment_id: uuid.UUID
    title: str
    canonical_key: str
    # Whether the assessment has title evidence in this unit's resource (any
    # version): the resource is then about it, e.g. its brief.
    evidenced_here: bool


@dataclass(frozen=True, slots=True)
class ClaimUnit:
    unit: ExtractionUnit
    assessments: tuple[AssessmentRef, ...]

    def resolve_assessment(self, local_id: str) -> AssessmentRef | None:
        """The assessment a local ID was issued for, in this unit only."""
        return next((a for a in self.assessments if a.local_id == local_id), None)


async def course_assessments(
    session: AsyncSession, course_id: uuid.UUID, version_id: uuid.UUID
) -> tuple[AssessmentRef, ...]:
    """The course's assessments in canonical order, and which have title
    evidence in the resource of ``version_id``."""
    resource_id = await session.scalar(
        select(ResourceVersion.resource_id).where(ResourceVersion.id == version_id)
    )
    rows = (
        await session.execute(
            select(Assessment.id, Assessment.title, Assessment.canonical_key)
            .where(Assessment.course_id == course_id)
            .order_by(Assessment.canonical_key)
        )
    ).all()
    evidenced = set(
        (
            await session.scalars(
                select(EvidenceLink.assessment_id)
                .join(SourceSpan, SourceSpan.id == EvidenceLink.source_span_id)
                .join(ResourceVersion, ResourceVersion.id == SourceSpan.resource_version_id)
                .where(
                    EvidenceLink.field == EvidenceField.TITLE,
                    ResourceVersion.resource_id == resource_id,
                )
            )
        ).all()
    )
    return tuple(
        AssessmentRef(f"A{i}", row.id, row.title, row.canonical_key, row.id in evidenced)
        for i, row in enumerate(rows, 1)
    )


async def load_claim_units(
    session: AsyncSession, course_id: uuid.UUID, version_id: uuid.UUID
) -> list[ClaimUnit]:
    """The units of a current version, each with the course's assessments."""
    units = await load_units(session, course_id, version_id)
    assessments = await course_assessments(session, course_id, version_id)
    return [ClaimUnit(unit, assessments) for unit in units]


def render_assessments(assessments: tuple[AssessmentRef, ...]) -> str:
    if not assessments:
        return "(none)"
    return "\n".join(f"[{a.local_id}] {safe_title(a.title)}" for a in assessments)


def build_claim_messages(
    prompt: PromptFiles, claim_unit: ClaimUnit, *, nonce: str | None = None
) -> list[ChatMessage]:
    """The fixed system rules, then the task with the delimited material:
    resource metadata, the assessment list and the spans, all untrusted."""
    unit = claim_unit.unit
    assessments = render_assessments(claim_unit.assessments)
    while nonce is None:
        candidate = new_nonce(unit)
        nonce = candidate if candidate not in assessments else None
    material = render_material(unit)
    if nonce in material or nonce in unit.resource_title or nonce in assessments:
        raise ValueError("the nonce occurs in the material")
    user = prompt.user.substitute(
        resource_type=unit.resource_type,
        resource_title=safe_title(unit.resource_title),
        nonce=nonce,
        assessments=assessments,
        material=material,
    )
    return [ChatMessage("system", prompt.system), ChatMessage("user", user)]
