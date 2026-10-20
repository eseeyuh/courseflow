# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Assessment queries: assessments, their deadlines and every evidence link,
resolved to the exact quote, its location, its source version and lineage."""

import uuid
from collections import defaultdict
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas.assessment import AssessmentRead, DeadlineRead, EvidenceRead
from app.db.models import (
    Assessment,
    AssessmentDeadline,
    EvidenceLink,
    ModelCall,
    Resource,
    ResourceVersion,
    SourceSpan,
)


async def _evidence(
    session: AsyncSession, assessment_ids: Sequence[uuid.UUID], deadline_ids: Sequence[uuid.UUID]
) -> list[tuple[EvidenceLink, EvidenceRead]]:
    rows = await session.execute(
        select(EvidenceLink, SourceSpan, ResourceVersion, Resource, ModelCall)
        .join(SourceSpan, SourceSpan.id == EvidenceLink.source_span_id)
        .join(ResourceVersion, ResourceVersion.id == SourceSpan.resource_version_id)
        .join(Resource, Resource.id == ResourceVersion.resource_id)
        .outerjoin(ModelCall, ModelCall.id == EvidenceLink.model_call_id)
        .where(
            EvidenceLink.assessment_id.in_(assessment_ids)
            | EvidenceLink.assessment_deadline_id.in_(deadline_ids)
        )
        .order_by(EvidenceLink.field, Resource.source_uri, ResourceVersion.version_number,
                  SourceSpan.ordinal, EvidenceLink.quote_start)
    )  # fmt: skip
    return [
        (
            link,
            EvidenceRead(
                id=link.id,
                field=link.field,
                verification_status=link.verification_status,
                review_reason=link.review_reason,
                quote=span.excerpt[link.quote_start : link.quote_end],
                source_span_id=span.id,
                page_number=span.page_number,
                slide_number=span.slide_number,
                section_path=span.section_path,
                resource_id=resource.id,
                resource_title=resource.title,
                resource_version_id=version.id,
                version_number=version.version_number,
                current_version=resource.current_version_id == version.id,
                model=call.model if call else None,
                prompt_version=call.prompt_version if call else None,
                model_call_id=link.model_call_id,
                workflow_run_id=link.workflow_run_id,
            ),
        )
        for link, span, version, resource, call in rows
    ]


async def course_assessments(
    session: AsyncSession, course_id: uuid.UUID, *, assessment_id: uuid.UUID | None = None
) -> list[AssessmentRead]:
    """A course's assessments (or one), by canonical key, deadlines by date."""
    query = select(Assessment).where(Assessment.course_id == course_id)
    if assessment_id is not None:
        query = query.where(Assessment.id == assessment_id)
    assessments = (await session.scalars(query.order_by(Assessment.canonical_key))).all()
    ids = [a.id for a in assessments]
    deadlines = (
        await session.scalars(
            select(AssessmentDeadline)
            .where(AssessmentDeadline.assessment_id.in_(ids))
            .order_by(AssessmentDeadline.due_date, AssessmentDeadline.due_time)
        )
    ).all()
    by_assessment: dict[uuid.UUID, list[EvidenceRead]] = defaultdict(list)
    by_deadline: dict[uuid.UUID, list[EvidenceRead]] = defaultdict(list)
    for link, read in await _evidence(session, ids, [d.id for d in deadlines]):
        if link.assessment_deadline_id is not None:
            by_deadline[link.assessment_deadline_id].append(read)
        elif link.assessment_id is not None:
            by_assessment[link.assessment_id].append(read)
    deadline_reads: dict[uuid.UUID, list[DeadlineRead]] = defaultdict(list)
    for d in deadlines:
        deadline_reads[d.assessment_id].append(
            DeadlineRead(
                id=d.id,
                due_date=d.due_date,
                due_time=d.due_time.strftime("%H:%M") if d.due_time else None,
                timezone=d.timezone,
                timezone_source=d.timezone_source,
                precision=d.precision,
                due_at=d.due_at,
                status=d.status,
                review_reason=d.review_reason,
                supported_by_current_version=any(e.current_version for e in by_deadline[d.id]),
                evidence=by_deadline[d.id],
            )
        )
    return [
        AssessmentRead(
            id=a.id,
            course_id=a.course_id,
            title=a.title,
            canonical_key=a.canonical_key,
            weighting_percent=a.weighting_percent,
            status=a.status,
            review_reason=a.review_reason,
            evidence=by_assessment[a.id],
            deadlines=deadline_reads[a.id],
        )
        for a in assessments
    ]
