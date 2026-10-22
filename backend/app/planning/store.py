# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Study states and saved plan snapshots."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import MaterialMapping, StudyPlan, StudyState, StudyTask
from app.domain.enums import StudyStateValue
from app.planning.plan import LEARNER, PLAN_VERSION, PlannedTask, load_facts, plan


class NotATeachingTarget(ValueError):
    """A study state may be set only on a span some mapping points at."""

    def __init__(self, span_ids: Sequence[uuid.UUID]) -> None:
        super().__init__(f"not mapped teaching material: {', '.join(sorted(map(str, span_ids)))}")
        self.span_ids = tuple(span_ids)


async def set_study_states(
    session: AsyncSession,
    span_ids: Sequence[uuid.UUID],
    state: StudyStateValue,
    learner: str = LEARNER,
) -> int:
    """Set the learner's state on every target, all or none; setting the same
    state again changes nothing. Returns how many rows changed."""
    wanted = set(span_ids)
    mapped = set(
        (
            await session.scalars(
                select(MaterialMapping.source_span_id)
                .where(MaterialMapping.source_span_id.in_(wanted))
                .distinct()
            )
        ).all()
    )
    if wanted - mapped:
        raise NotATeachingTarget(sorted(wanted - mapped, key=str))
    changed = 0
    for span_id in sorted(wanted, key=str):
        result = await session.execute(
            insert(StudyState)
            .values(learner=learner, source_span_id=span_id, state=state)
            .on_conflict_do_update(
                constraint="uq_study_states_learner_span",
                set_={"state": state, "updated_at": func.now()},
                where=StudyState.state != state,
            )
            .returning(StudyState.id)
        )
        changed += result.first() is not None
    return changed


async def compute_plan(
    session: AsyncSession, as_of: datetime, learner: str = LEARNER
) -> list[PlannedTask]:
    return plan(await load_facts(session, learner), as_of)


async def save_plan(
    session: AsyncSession,
    tasks: Sequence[PlannedTask],
    as_of: datetime,
    workflow_run_id: uuid.UUID | None = None,
    learner: str = LEARNER,
) -> uuid.UUID:
    """A snapshot of the plan, so a later plan can be compared with it."""
    snapshot = StudyPlan(learner=learner, plan_version=PLAN_VERSION, as_of=as_of,
                         task_count=len(tasks), workflow_run_id=workflow_run_id)  # fmt: skip
    session.add(snapshot)
    await session.flush()
    session.add_all(
        StudyTask(
            plan_id=snapshot.id,
            position=t.position,
            task_key=t.key,
            task_type=t.task_type,
            status=t.status,
            course_id=t.course_id,
            assessment_id=t.assessment_id,
            obligation_id=t.obligation_id,
            conflict_id=t.conflict_id,
            title=t.title,
            anchor_at=t.anchor.instant if t.anchor else None,
            estimated_minutes=t.minutes,
            reasons=t.reasons,
            why_now=t.why_now,
            prerequisite_keys=[k for k, _ in t.prerequisites],
            target_span_ids=[str(target.span_id) for target in t.targets],
        )
        for t in tasks
    )
    return snapshot.id


async def latest_plan(session: AsyncSession, learner: str = LEARNER) -> StudyPlan | None:
    return await session.scalar(
        select(StudyPlan)
        .where(StudyPlan.learner == learner)
        .order_by(StudyPlan.created_at.desc(), StudyPlan.id.desc())
        .limit(1)
    )
