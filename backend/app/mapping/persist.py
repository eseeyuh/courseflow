# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Persist one requirement's gated selection: its mappings become exactly
the selected candidates.

- Identity is (requirement, relation, target span): selecting the same
  material again keeps the row and its ID, refreshing the retrieval and
  lineage to the latest run; material no longer selected loses its mapping;
  ``none`` leaves the requirement with no mapping. A failed verifier call
  never reaches here, so it never removes a mapping.
- Nothing is written unless every target is checked first, in the caller's
  transaction: the span is in a current version of a teaching resource of
  the requirement's course, the candidate's version and resource are that
  span's, and the evidence range lies inside the span with visible text
  that equals the candidate's. The verifier call ran in the run, the run is
  of that course, and the query call is an embedding call. Any violation
  raises ``MappingIntegrityError`` before the first write.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Assessment,
    AssessmentRequirement,
    MaterialMapping,
    ModelCall,
    Resource,
    ResourceVersion,
    SourceSpan,
    WorkflowRun,
)
from app.domain.enums import MappingRelation, ModelCallKind
from app.mapping.candidates import TEACHING_RESOURCE_TYPES, Candidate, navigation_rationale

RELATION = MappingRelation.SUPPORTED_BY
RETRIEVAL_MODES = frozenset({"vector", "lexical", "hybrid"})


class MappingIntegrityError(RuntimeError):
    """A mapping would break the candidate contract. Names IDs, never text."""


@dataclass(frozen=True, slots=True)
class MappingLineage:
    retrieval_mode: str
    query_model_call_id: uuid.UUID | None  # None only for lexical retrieval
    verifier_model_call_id: uuid.UUID
    workflow_run_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class MappingCounts:
    created: int = 0
    kept: int = 0
    removed: int = 0


async def _course_of(session: AsyncSession, requirement_id: uuid.UUID) -> uuid.UUID:
    course_id = await session.scalar(
        select(Assessment.course_id)
        .join(AssessmentRequirement, AssessmentRequirement.assessment_id == Assessment.id)
        .where(AssessmentRequirement.id == requirement_id)
    )
    if course_id is None:
        raise MappingIntegrityError(f"requirement {requirement_id} does not exist")
    return course_id


async def _check_lineage(
    session: AsyncSession, course_id: uuid.UUID, lineage: MappingLineage
) -> None:
    if lineage.retrieval_mode not in RETRIEVAL_MODES:
        raise MappingIntegrityError(f"unknown retrieval mode {lineage.retrieval_mode!r}")
    if (lineage.retrieval_mode == "lexical") != (lineage.query_model_call_id is None):
        raise MappingIntegrityError("a query call exists exactly when the query was embedded")
    run_course = await session.scalar(
        select(WorkflowRun.course_id).where(WorkflowRun.id == lineage.workflow_run_id)
    )
    if run_course != course_id:
        raise MappingIntegrityError(f"run {lineage.workflow_run_id} is not of course {course_id}")
    verifier = await session.get(ModelCall, lineage.verifier_model_call_id)
    if (
        verifier is None
        or verifier.call_kind is not ModelCallKind.CHAT_STRUCTURED
        or verifier.workflow_run_id != lineage.workflow_run_id
    ):
        raise MappingIntegrityError(
            f"verifier call {lineage.verifier_model_call_id} is not a structured call of the run"
        )
    if lineage.query_model_call_id is not None:
        query = await session.get(ModelCall, lineage.query_model_call_id)
        if query is None or query.call_kind is not ModelCallKind.EMBEDDING:
            raise MappingIntegrityError(
                f"query call {lineage.query_model_call_id} is not an embedding call"
            )


async def _check_targets(
    session: AsyncSession, course_id: uuid.UUID, selected: Sequence[Candidate]
) -> None:
    if len({c.span_id for c in selected}) != len(selected):
        raise MappingIntegrityError("a span is selected twice")
    rows = {
        span.id: (span, version, resource)
        for span, version, resource in (
            await session.execute(
                select(SourceSpan, ResourceVersion, Resource)
                .join(ResourceVersion, ResourceVersion.id == SourceSpan.resource_version_id)
                .join(Resource, Resource.id == ResourceVersion.resource_id)
                .where(SourceSpan.id.in_([c.span_id for c in selected]))
            )
        )
    }
    for candidate in selected:
        found = rows.get(candidate.span_id)
        if found is None:
            raise MappingIntegrityError(f"span {candidate.span_id} does not exist")
        span, version, resource = found
        if (
            resource.course_id != course_id
            or resource.type not in TEACHING_RESOURCE_TYPES
            or resource.current_version_id != version.id
            or candidate.version_id != version.id
            or candidate.resource_id != resource.id
        ):
            raise MappingIntegrityError(
                f"span {span.id} is not current teaching material of course {course_id}"
            )
        text = span.excerpt[candidate.quote_start : candidate.quote_end]
        if not (
            0 <= candidate.quote_start < candidate.quote_end <= len(span.excerpt)
            and text == candidate.text
            and text.strip()
        ):
            raise MappingIntegrityError(f"span {span.id}: the evidence range is not exact")


async def replace_mappings(
    session: AsyncSession,
    requirement_id: uuid.UUID,
    selected: Sequence[Candidate],
    lineage: MappingLineage,
) -> MappingCounts:
    course_id = await _course_of(session, requirement_id)
    await _check_lineage(session, course_id, lineage)
    await _check_targets(session, course_id, selected)

    existing = set(
        (
            await session.scalars(
                select(MaterialMapping.source_span_id)
                .where(
                    MaterialMapping.requirement_id == requirement_id,
                    MaterialMapping.relation == RELATION,
                )
                .with_for_update()
            )
        ).all()
    )
    chosen = {c.span_id for c in selected}
    stale = existing - chosen
    if stale:
        await session.execute(
            delete(MaterialMapping).where(
                MaterialMapping.requirement_id == requirement_id,
                MaterialMapping.relation == RELATION,
                MaterialMapping.source_span_id.in_(stale),
            )
        )
    for candidate in selected:
        values = {
            "quote_start": candidate.quote_start,
            "quote_end": candidate.quote_end,
            "rationale": navigation_rationale(candidate),
            "retrieval_mode": lineage.retrieval_mode,
            "retrieval_rank": candidate.retrieval_rank,
            "retrieval_score": candidate.retrieval_score,
            "query_model_call_id": lineage.query_model_call_id,
            "verifier_model_call_id": lineage.verifier_model_call_id,
            "workflow_run_id": lineage.workflow_run_id,
        }
        await session.execute(
            insert(MaterialMapping)
            .values(
                requirement_id=requirement_id,
                relation=RELATION,
                source_span_id=candidate.span_id,
                **values,
            )
            .on_conflict_do_update(
                constraint="uq_material_mappings_target", set_=values | {"updated_at": func.now()}
            )
        )
    return MappingCounts(
        created=len(chosen - existing), kept=len(chosen & existing), removed=len(stale)
    )
