# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Dependency and conflict queries, with exact evidence and lineage."""

import uuid
from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas.assessment import EvidenceRead
from app.api.schemas.reasoning import ConflictRead, DependencyEdgeRead, DependencyEndRead
from app.db.assessments import course_assessments, evidence_reads
from app.db.models import (
    Assessment,
    Conflict,
    DependencyEdge,
    EvidenceLink,
    Obligation,
    conflict_sides,
)
from app.domain.enums import DependencyNodeKind, Milestone

MILESTONE_LABELS = {
    Milestone.ETHICS_APPROVAL: "Ethics approval",
    Milestone.DATA_COLLECTION: "Data collection",
}


async def course_dependencies(
    session: AsyncSession, course_id: uuid.UUID
) -> list[DependencyEdgeRead]:
    edges = (
        await session.scalars(
            select(DependencyEdge)
            .where(DependencyEdge.course_id == course_id)
            .order_by(DependencyEdge.rule, DependencyEdge.created_at, DependencyEdge.id)
        )
    ).all()
    obligations = {
        o.id: o
        for o in (
            await session.scalars(select(Obligation).where(Obligation.course_id == course_id))
        ).all()
    }
    assessments = {
        a.id: a
        for a in (
            await session.scalars(select(Assessment).where(Assessment.course_id == course_id))
        ).all()
    }
    evidence: dict[uuid.UUID, list[EvidenceRead]] = defaultdict(list)
    found = await evidence_reads(
        session, EvidenceLink.dependency_edge_id.in_([e.id for e in edges])
    )
    for link, read in found:
        evidence[link.dependency_edge_id].append(read)  # type: ignore[index]

    def end(edge: DependencyEdge, name: str) -> DependencyEndRead:
        kind: DependencyNodeKind = getattr(edge, f"{name}_kind")
        if kind is DependencyNodeKind.OBLIGATION:
            o = obligations[getattr(edge, f"{name}_obligation_id")]
            return DependencyEndRead(kind=kind, id=o.id, milestone=None,
                                     label=o.canonical_action, status=o.status)  # fmt: skip
        if kind is DependencyNodeKind.ASSESSMENT:
            a = assessments[getattr(edge, f"{name}_assessment_id")]
            return DependencyEndRead(kind=kind, id=a.id, milestone=None, label=a.title,
                                     status=a.status)  # fmt: skip
        milestone: Milestone = getattr(edge, f"{name}_milestone")
        return DependencyEndRead(kind=kind, id=None, milestone=milestone,
                                 label=MILESTONE_LABELS[milestone], status=None)  # fmt: skip

    return [
        DependencyEdgeRead(
            id=e.id,
            course_id=e.course_id,
            relation=e.relation,
            prerequisite=end(e, "prerequisite"),
            dependent=end(e, "dependent"),
            rule=e.rule,
            derived_from_obligation_id=e.derived_from_obligation_id,
            workflow_run_id=e.workflow_run_id,
            evidence=evidence[e.id],
        )
        for e in edges
    ]


async def course_conflicts(
    session: AsyncSession, course_id: uuid.UUID, *, assessment_id: uuid.UUID | None = None
) -> list[ConflictRead]:
    query = select(Conflict).where(Conflict.course_id == course_id)
    if assessment_id is not None:
        query = query.where(Conflict.assessment_id == assessment_id)
    conflicts = (await session.scalars(query.order_by(Conflict.created_at, Conflict.id))).all()
    sides: dict[uuid.UUID, set[uuid.UUID]] = defaultdict(set)
    for conflict_id, deadline_id in await session.execute(
        select(conflict_sides.c.conflict_id, conflict_sides.c.assessment_deadline_id).where(
            conflict_sides.c.conflict_id.in_([c.id for c in conflicts])
        )
    ):
        sides[conflict_id].add(deadline_id)
    reads = {a.id: a for a in await course_assessments(session, course_id)}
    return [
        ConflictRead(
            id=c.id,
            course_id=c.course_id,
            assessment_id=c.assessment_id,
            assessment_title=reads[c.assessment_id].title,
            field=c.field,
            status=c.status,
            workflow_run_id=c.workflow_run_id,
            # Date order, as the assessment lists them: no side is first by preference.
            sides=[d for d in reads[c.assessment_id].deadlines if d.id in sides[c.id]],
        )
        for c in conflicts
    ]
