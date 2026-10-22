# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Deadline conflicts (``conflict-rules/v1``), detected deterministically.

A deadline is active when it is verified and has verified ``deadline``
evidence in a current resource version. An assessment whose active
deadlines state different local dates or times is in conflict: one
Conflict per (assessment, ``deadline``) holds every active deadline as a
side, each with its own evidence. No value is chosen and none is stored.

Re-detection keeps the Conflict's ID and replaces its sides. A Conflict
whose sources no longer disagree becomes ``resolved_by_source`` and keeps
its last sides; if they disagree again it reopens. An ``acknowledged``
conflict stays acknowledged while its sides are unchanged.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime

from pydantic import BaseModel, ConfigDict
from sqlalchemy import delete, insert, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import (
    Assessment,
    AssessmentDeadline,
    Conflict,
    EvidenceLink,
    Resource,
    ResourceVersion,
    SourceSpan,
    conflict_sides,
)
from app.domain.enums import (
    ConflictField,
    ConflictStatus,
    EvidenceField,
    VerificationStatus,
    WorkflowRunStatus,
)
from app.reasoning.runs import finish_run, start_run

RULES_VERSION = "conflict-rules/v1"


async def active_deadlines(
    session: AsyncSession, course_id: uuid.UUID
) -> dict[uuid.UUID, list[AssessmentDeadline]]:
    """Each assessment's active deadlines, in date order."""
    rows = (
        await session.scalars(
            select(AssessmentDeadline)
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
            .distinct()
        )
    ).all()
    out: dict[uuid.UUID, list[AssessmentDeadline]] = defaultdict(list)
    for deadline in sorted(rows, key=lambda d: (d.due_date, d.due_time or datetime.min.time())):
        out[deadline.assessment_id].append(deadline)
    return out


def disagree(deadlines: list[AssessmentDeadline]) -> bool:
    """Materially different: another local date or time of day."""
    return len({(d.due_date, d.due_time) for d in deadlines}) > 1


class ConflictSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workflow_version: str = RULES_VERSION
    assessments_checked: int = 0
    open: int = 0
    created: int = 0
    reopened: int = 0
    resolved_by_source: int = 0


@dataclass(frozen=True, slots=True)
class ConflictRunResult:
    run_id: uuid.UUID
    status: WorkflowRunStatus
    summary: ConflictSummary


async def _sides(session: AsyncSession, conflict_id: uuid.UUID) -> set[uuid.UUID]:
    return set(
        (
            await session.scalars(
                select(conflict_sides.c.assessment_deadline_id).where(
                    conflict_sides.c.conflict_id == conflict_id
                )
            )
        ).all()
    )


async def detect_course_conflicts(
    sessions: async_sessionmaker[AsyncSession], course_id: uuid.UUID
) -> ConflictRunResult:
    run_id = await start_run(sessions, course_id, RULES_VERSION)
    summary = ConflictSummary()
    try:
        async with sessions() as session, session.begin():
            active = await active_deadlines(session, course_id)
            existing = {
                c.assessment_id: c
                for c in (
                    await session.scalars(
                        select(Conflict)
                        .where(Conflict.course_id == course_id,
                               Conflict.field == ConflictField.DEADLINE)
                        .with_for_update()
                    )
                ).all()
            }  # fmt: skip
            assessments = set(
                (
                    await session.scalars(
                        select(Assessment.id).where(Assessment.course_id == course_id)
                    )
                ).all()
            )
            summary.assessments_checked = len(assessments)
            for assessment_id in sorted(assessments, key=str):
                deadlines = active.get(assessment_id, [])
                conflict = existing.get(assessment_id)
                if not disagree(deadlines):
                    if (
                        conflict is not None
                        and conflict.status is not ConflictStatus.RESOLVED_BY_SOURCE
                    ):
                        conflict.status = ConflictStatus.RESOLVED_BY_SOURCE
                        conflict.workflow_run_id = run_id
                        summary.resolved_by_source += 1
                    continue
                sides = {d.id for d in deadlines}
                summary.open += 1
                if conflict is None:
                    conflict = Conflict(course_id=course_id, assessment_id=assessment_id,
                                        field=ConflictField.DEADLINE, status=ConflictStatus.OPEN,
                                        workflow_run_id=run_id)  # fmt: skip
                    session.add(conflict)
                    await session.flush()
                    summary.created += 1
                elif await _sides(session, conflict.id) != sides or (
                    conflict.status is ConflictStatus.RESOLVED_BY_SOURCE
                ):
                    if conflict.status is ConflictStatus.RESOLVED_BY_SOURCE:
                        summary.reopened += 1
                    conflict.status = ConflictStatus.OPEN
                    conflict.workflow_run_id = run_id
                else:
                    continue  # the same disagreement: unchanged, acknowledged or not
                await session.execute(
                    delete(conflict_sides).where(conflict_sides.c.conflict_id == conflict.id)
                )
                await session.execute(
                    insert(conflict_sides),
                    [
                        {"conflict_id": conflict.id, "assessment_deadline_id": s}
                        for s in sorted(sides, key=str)
                    ],
                )
        status = WorkflowRunStatus.SUCCEEDED
    except BaseException:
        await finish_run(sessions, run_id, WorkflowRunStatus.FAILED, summary)
        raise
    await finish_run(sessions, run_id, status, summary)
    return ConflictRunResult(run_id, status, summary)
