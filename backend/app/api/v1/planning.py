# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

import uuid
from collections import defaultdict
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.api.deps import SessionDep
from app.api.schemas.assessment import DeadlineRead, EvidenceRead
from app.db.assessments import course_assessments, evidence_reads
from app.db.models import (
    Course,
    EvidenceLink,
    MaterialMapping,
    Resource,
    ResourceVersion,
    SourceSpan,
    StudyState,
)
from app.domain.enums import StudyStateValue, StudyTaskStatus, StudyTaskType
from app.planning.plan import LEARNER, PLAN_VERSION, PlannedTask
from app.planning.store import NotATeachingTarget, compute_plan, latest_plan, set_study_states

router = APIRouter(tags=["study plan"])


class CourseRef(BaseModel):
    id: uuid.UUID
    code: str | None
    title: str


class AssessmentRef(BaseModel):
    id: uuid.UUID
    title: str


class StudyTargetRead(BaseModel):
    """A mapped teaching span to study: exact range, where it is, the
    learner's state."""

    source_span_id: uuid.UUID
    quote: str
    resource_id: uuid.UUID
    resource_title: str
    version_number: int
    current_version: bool
    page_number: int | None
    slide_number: int | None
    section_path: str | None
    study_state: StudyStateValue


class StudyTaskRead(BaseModel):
    """One planned task. ``why_now`` is rendered from ``reasons`` alone.
    For an assessment in conflict, ``anchor_basis`` is
    ``earliest_conflicting_deadline``: a scheduling choice, never a ruling on
    which source is right; ``conflict_values`` lists every side."""

    position: int
    task_key: str
    task_type: StudyTaskType
    status: StudyTaskStatus
    title: str
    course: CourseRef
    assessment: AssessmentRef | None
    obligation_id: uuid.UUID | None
    conflict_id: uuid.UUID | None
    mandatory: bool
    anchor_at: datetime | None
    anchor_basis: str | None
    estimated_minutes: int
    why_now: str
    reasons: list[dict[str, Any]]
    prerequisite_keys: list[str]
    study_state: StudyStateValue | None
    targets: list[StudyTargetRead]
    deadlines: list[DeadlineRead]  # the anchor deadline(s), each with its evidence
    conflict_values: list[DeadlineRead]  # every side of the conflict, none chosen
    evidence: list[EvidenceRead]  # the obligation's or the ordering's source wording


class StudyPlanRead(BaseModel):
    plan_version: str
    learner: str
    as_of: datetime
    latest_snapshot_id: uuid.UUID | None
    tasks: list[StudyTaskRead]


class StudyStateRead(BaseModel):
    source_span_id: uuid.UUID
    state: StudyStateValue
    updated_at: datetime


class StudyStateUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_span_ids: list[uuid.UUID] = Field(min_length=1, max_length=200)
    state: StudyStateValue


async def _targets(
    session: SessionDep, span_ids: set[uuid.UUID], states: dict[uuid.UUID, StudyStateValue]
) -> dict[uuid.UUID, StudyTargetRead]:
    rows = await session.execute(
        select(SourceSpan, ResourceVersion, Resource, MaterialMapping)
        .join(ResourceVersion, ResourceVersion.id == SourceSpan.resource_version_id)
        .join(Resource, Resource.id == ResourceVersion.resource_id)
        .join(MaterialMapping, MaterialMapping.source_span_id == SourceSpan.id)
        .where(SourceSpan.id.in_(span_ids))
        .order_by(SourceSpan.id, MaterialMapping.quote_start)
    )
    out: dict[uuid.UUID, StudyTargetRead] = {}
    for span, version, resource, mapping in rows:
        out.setdefault(
            span.id,
            StudyTargetRead(
                source_span_id=span.id,
                quote=span.excerpt[mapping.quote_start : mapping.quote_end],
                resource_id=resource.id,
                resource_title=resource.title,
                version_number=version.version_number,
                current_version=resource.current_version_id == version.id,
                page_number=span.page_number,
                slide_number=span.slide_number,
                section_path=span.section_path,
                study_state=states.get(span.id, StudyStateValue.NOT_STARTED),
            ),
        )
    return out


async def _read_tasks(session: SessionDep, tasks: list[PlannedTask]) -> list[StudyTaskRead]:
    courses = {c.id: c for c in (await session.scalars(select(Course))).all()}
    deadlines: dict[uuid.UUID, DeadlineRead] = {}
    for course_id in {t.course_id for t in tasks}:
        for assessment in await course_assessments(session, course_id):
            deadlines.update({d.id: d for d in assessment.deadlines})
    states = dict(
        (
            await session.execute(
                select(StudyState.source_span_id, StudyState.state).where(
                    StudyState.learner == LEARNER
                )
            )
        ).all()
    )
    targets = await _targets(session, {s.span_id for t in tasks for s in t.targets}, states)
    evidence: dict[str, list[EvidenceRead]] = defaultdict(list)
    obligations = [t.obligation_id for t in tasks if t.obligation_id]
    for link, read in await evidence_reads(session, EvidenceLink.obligation_id.in_(obligations)):
        evidence[f"obligation:{link.obligation_id}"].append(read)
    edges = [t.edge_id for t in tasks if t.edge_id]
    for link, read in await evidence_reads(session, EvidenceLink.dependency_edge_id.in_(edges)):
        evidence[f"edge:{link.dependency_edge_id}"].append(read)
    reads = []
    for t in tasks:
        course = courses[t.course_id]
        anchors = [deadlines[t.anchor.id]] if t.anchor and t.anchor.id in deadlines else []
        reads.append(
            StudyTaskRead(
                position=t.position,
                task_key=t.key,
                task_type=t.task_type,
                status=t.status,
                title=t.title,
                course=CourseRef(id=course.id, code=course.external_id, title=course.title),
                assessment=AssessmentRef(id=t.assessment_id, title=t.assessment_title)
                if t.assessment_id and t.assessment_title
                else None,
                obligation_id=t.obligation_id,
                conflict_id=t.conflict_id,
                mandatory=t.mandatory,
                anchor_at=t.anchor.instant if t.anchor else None,
                anchor_basis=t.anchor_basis,
                estimated_minutes=t.minutes,
                why_now=t.why_now,
                reasons=t.reasons,
                prerequisite_keys=[k for k, _ in t.prerequisites],
                study_state=t.study_state,
                targets=[targets[s.span_id] for s in t.targets if s.span_id in targets],
                deadlines=anchors,
                conflict_values=[deadlines[d.id] for d in t.conflict_values if d.id in deadlines],
                evidence=evidence[f"obligation:{t.obligation_id}"]
                if t.obligation_id
                else evidence[f"edge:{t.edge_id}"]
                if t.edge_id
                else [],
            )
        )
    return reads


@router.get("/study-plan")
async def get_study_plan(session: SessionDep, as_of: AwareDatetime | None = None) -> StudyPlanRead:
    """The deterministic cross-course plan from the stored facts and the
    learner's study states, as of ``as_of`` (default: the demo clock of the
    latest saved plan). Never the wall clock."""
    snapshot = await latest_plan(session)
    if as_of is None:
        if snapshot is None:
            raise HTTPException(
                status_code=409, detail="no plan yet: run the workflow or give as_of"
            )
        as_of = snapshot.as_of
    tasks = await compute_plan(session, as_of)
    return StudyPlanRead(
        plan_version=PLAN_VERSION,
        learner=LEARNER,
        as_of=as_of,
        latest_snapshot_id=snapshot.id if snapshot else None,
        tasks=await _read_tasks(session, tasks),
    )


@router.get("/study-states")
async def get_study_states(session: SessionDep) -> list[StudyStateRead]:
    rows = (
        await session.scalars(
            select(StudyState)
            .where(StudyState.learner == LEARNER)
            .order_by(StudyState.source_span_id)
        )
    ).all()
    return [StudyStateRead(source_span_id=r.source_span_id, state=r.state, updated_at=r.updated_at)
            for r in rows]  # fmt: skip


@router.put("/study-states")
async def put_study_states(session: SessionDep, body: StudyStateUpdate) -> dict[str, int]:
    """Set the learner's state on mapped teaching targets, all or none.
    Idempotent: the same state again changes nothing."""
    try:
        changed = await set_study_states(session, body.source_span_ids, body.state)
    except NotATeachingTarget as exc:
        await session.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from None
    await session.commit()
    return {"changed": changed}
