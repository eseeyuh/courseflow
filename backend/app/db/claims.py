# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Requirement and obligation queries, each with every evidence link resolved
to its exact quote, location, source version and lineage. Rows are ordered by
where their evidence first occurs in the course (source, version, span,
offset) over all their evidence, then by ID: a stable, natural order."""

import uuid
from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas.assessment import EvidenceRead
from app.api.schemas.claims import DueRead, MaterialMappingRead, ObligationRead, RequirementRead
from app.db.assessments import evidence_reads
from app.db.models import (
    Assessment,
    AssessmentRequirement,
    EvidenceLink,
    MaterialMapping,
    ModelCall,
    Obligation,
    Resource,
    ResourceVersion,
    SourceSpan,
)

Position = tuple[str, int, int, int]  # source URI, version, span ordinal, offset


async def _positions(session: AsyncSession, links: list[EvidenceLink]) -> dict[uuid.UUID, Position]:
    """Where each link's quote is in the course, in natural source order."""
    rows = await session.execute(
        select(
            SourceSpan.id, SourceSpan.ordinal, ResourceVersion.version_number, Resource.source_uri
        )
        .join(ResourceVersion, ResourceVersion.id == SourceSpan.resource_version_id)
        .join(Resource, Resource.id == ResourceVersion.resource_id)
        .where(SourceSpan.id.in_({link.source_span_id for link in links}))
    )
    spans = {row.id: (row.source_uri, row.version_number, row.ordinal) for row in rows}
    return {link.id: (*spans[link.source_span_id], link.quote_start) for link in links}


def _order(positions: dict[uuid.UUID, Position], evidence: list[EvidenceRead]) -> list[Position]:
    """A claim sorts by all its evidence positions, earliest first."""
    return sorted(positions[e.id] for e in evidence)


async def material_reads(
    session: AsyncSession, requirement_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[MaterialMappingRead]]:
    """Each requirement's mappings, resolved to the exact span range, location,
    source version and lineage, in source order."""
    rows = await session.execute(
        select(MaterialMapping, SourceSpan, ResourceVersion, Resource, ModelCall)
        .join(SourceSpan, SourceSpan.id == MaterialMapping.source_span_id)
        .join(ResourceVersion, ResourceVersion.id == SourceSpan.resource_version_id)
        .join(Resource, Resource.id == ResourceVersion.resource_id)
        .join(ModelCall, ModelCall.id == MaterialMapping.verifier_model_call_id)
        .where(MaterialMapping.requirement_id.in_(requirement_ids))
        .order_by(Resource.source_uri, ResourceVersion.version_number, SourceSpan.ordinal,
                  MaterialMapping.quote_start)
    )  # fmt: skip
    out: dict[uuid.UUID, list[MaterialMappingRead]] = defaultdict(list)
    for mapping, span, version, resource, call in rows:
        out[mapping.requirement_id].append(
            MaterialMappingRead(
                id=mapping.id,
                relation=mapping.relation,
                rationale=mapping.rationale,
                quote=span.excerpt[mapping.quote_start : mapping.quote_end],
                source_span_id=span.id,
                page_number=span.page_number,
                slide_number=span.slide_number,
                section_path=span.section_path,
                resource_id=resource.id,
                resource_title=resource.title,
                resource_type=resource.type,
                resource_version_id=version.id,
                version_number=version.version_number,
                current_version=resource.current_version_id == version.id,
                retrieval_mode=mapping.retrieval_mode,
                retrieval_rank=mapping.retrieval_rank,
                retrieval_score=mapping.retrieval_score,
                query_model_call_id=mapping.query_model_call_id,
                verifier_model_call_id=mapping.verifier_model_call_id,
                verifier_model=call.model,
                verifier_prompt_version=call.prompt_version,
                workflow_run_id=mapping.workflow_run_id,
            )
        )
    return out


async def course_requirements(
    session: AsyncSession, course_id: uuid.UUID, *, assessment_id: uuid.UUID | None = None
) -> list[RequirementRead]:
    query = (
        select(AssessmentRequirement, Assessment)
        .join(Assessment, Assessment.id == AssessmentRequirement.assessment_id)
        .where(Assessment.course_id == course_id)
    )
    if assessment_id is not None:
        query = query.where(Assessment.id == assessment_id)
    rows = (await session.execute(query)).all()
    ids = [requirement.id for requirement, _ in rows]
    evidence: dict[uuid.UUID, list[EvidenceRead]] = defaultdict(list)
    found = await evidence_reads(session, EvidenceLink.assessment_requirement_id.in_(ids))
    for link, read in found:
        evidence[link.assessment_requirement_id].append(read)  # type: ignore[index]
    positions = await _positions(session, [link for link, _ in found])
    materials = await material_reads(session, ids)
    reads = [
        RequirementRead(
            id=requirement.id,
            assessment_id=assessment.id,
            assessment_title=assessment.title,
            text=requirement.text,
            status=requirement.status,
            review_reason=requirement.review_reason,
            supported_by_current_version=any(e.current_version for e in evidence[requirement.id]),
            evidence=evidence[requirement.id],
            materials=materials.get(requirement.id, []),
        )
        for requirement, assessment in rows
    ]
    keys = {assessment.id: assessment.canonical_key for _, assessment in rows}
    return sorted(
        reads, key=lambda r: (keys[r.assessment_id], _order(positions, r.evidence), str(r.id))
    )


async def course_obligations(session: AsyncSession, course_id: uuid.UUID) -> list[ObligationRead]:
    obligations = (
        await session.scalars(select(Obligation).where(Obligation.course_id == course_id))
    ).all()
    ids = [o.id for o in obligations]
    evidence: dict[uuid.UUID, list[EvidenceRead]] = defaultdict(list)
    found = await evidence_reads(session, EvidenceLink.obligation_id.in_(ids))
    for link, read in found:
        evidence[link.obligation_id].append(read)  # type: ignore[index]
    positions = await _positions(session, [link for link, _ in found])
    reads = [
        ObligationRead(
            id=o.id,
            course_id=o.course_id,
            assessment_id=o.assessment_id,
            canonical_action=o.canonical_action,
            category=o.category,
            modality=o.modality,
            due=(
                DueRead(
                    due_date=o.due_date,
                    due_time=o.due_time.strftime("%H:%M") if o.due_time else None,
                    timezone=o.due_timezone,
                    timezone_source=o.due_timezone_source,
                    precision=o.due_precision,
                    due_at=o.due_at,
                )
                if o.due_date is not None
                and o.due_timezone_source is not None
                and o.due_precision is not None
                else None
            ),
            status=o.status,
            review_reason=o.review_reason,
            supported_by_current_version=any(e.current_version for e in evidence[o.id]),
            evidence=evidence[o.id],
        )
        for o in obligations
    ]
    return sorted(reads, key=lambda r: (_order(positions, r.evidence), str(r.id)))
