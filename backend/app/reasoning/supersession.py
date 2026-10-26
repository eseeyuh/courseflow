# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Source supersession (``source-supersession/v1``): one explicit,
deterministic rule, not a general engine.

A current source version that says "this replaces the date in the brief"
in the same section as one of its verified deadlines supersedes, for that
deadline's assessment, every other deadline whose current verified
evidence is in briefs only. The superseded deadline and its evidence stay
stored (history); it no longer counts as active for conflicts, the plan or
current views. Nothing else is superseded, and a disagreement with any
other current source (a handbook) stays a conflict.
"""

from __future__ import annotations

import re
import uuid
from collections import defaultdict
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Assessment,
    AssessmentDeadline,
    EvidenceLink,
    Resource,
    ResourceVersion,
    SourceSpan,
)
from app.domain.enums import EvidenceField, ResourceType, VerificationStatus

RULES_VERSION = "source-supersession/v1"
REPLACES_BRIEF = re.compile(r"\bthis replaces the (?:date|deadline) in the brief\b", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class Supersession:
    deadline_id: uuid.UUID  # the superseded one
    by_deadline_id: uuid.UUID
    span_id: uuid.UUID  # the replacing statement, exactly
    quote_start: int
    quote_end: int
    rule: str = RULES_VERSION


async def superseded_deadlines(
    session: AsyncSession, course_id: uuid.UUID
) -> dict[uuid.UUID, Supersession]:
    """Each superseded deadline of the course and what supersedes it."""
    rows = (
        await session.execute(
            select(
                AssessmentDeadline.id,
                AssessmentDeadline.assessment_id,
                SourceSpan,
                ResourceVersion.id,
                Resource.type,
            )
            .join(Assessment, Assessment.id == AssessmentDeadline.assessment_id)
            .join(EvidenceLink, EvidenceLink.assessment_deadline_id == AssessmentDeadline.id)
            .join(SourceSpan, SourceSpan.id == EvidenceLink.source_span_id)
            .join(ResourceVersion, ResourceVersion.id == SourceSpan.resource_version_id)
            .join(Resource, Resource.id == ResourceVersion.resource_id)
            .where(
                Assessment.course_id == course_id,
                AssessmentDeadline.status == VerificationStatus.VERIFIED,
                EvidenceLink.field == EvidenceField.DEADLINE,
                EvidenceLink.verification_status == VerificationStatus.VERIFIED,
                Resource.current_version_id == ResourceVersion.id,
            )
        )
    ).all()
    assessment_of: dict[uuid.UUID, uuid.UUID] = {}
    types: dict[uuid.UUID, set[ResourceType]] = defaultdict(set)
    located: dict[tuple[uuid.UUID, str | None], set[uuid.UUID]] = defaultdict(set)
    for deadline_id, assessment_id, span, version_id, kind in rows:
        assessment_of[deadline_id] = assessment_id
        types[deadline_id].add(kind)
        located[(version_id, span.section_path)].add(deadline_id)
    statements = (
        await session.scalars(
            select(SourceSpan)
            .join(ResourceVersion, ResourceVersion.id == SourceSpan.resource_version_id)
            .join(Resource, Resource.id == ResourceVersion.resource_id)
            .where(
                Resource.course_id == course_id,
                Resource.current_version_id == ResourceVersion.id,
                SourceSpan.excerpt.ilike("%replaces the%in the brief%"),
            )
            .order_by(SourceSpan.resource_version_id, SourceSpan.ordinal)
        )
    ).all()
    out: dict[uuid.UUID, Supersession] = {}
    for span in statements:
        match = REPLACES_BRIEF.search(span.excerpt)
        if match is None:
            continue
        for new in sorted(located.get((span.resource_version_id, span.section_path), ()), key=str):
            for old, assessment_id in sorted(assessment_of.items(), key=lambda item: str(item[0])):
                if (
                    old != new
                    and assessment_id == assessment_of[new]
                    and types[old] == {ResourceType.BRIEF}
                    and old not in out
                ):
                    out[old] = Supersession(old, new, span.id, match.start(), match.end())
    return out
