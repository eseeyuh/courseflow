# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""What each sync changed, read from stored state only: the ChangeEvents
(real version state), what they deterministically affected, and the
before/after study plan snapshots compared by stable task key.

Affected items (``kind``):

- ``deadline_added``: a verified deadline whose evidence is in the new
  version and not in the old one;
- ``deadline_superseded``: a deadline the new version supersedes
  (``source-supersession/v1``), with the superseding value and statement;
- ``obligation_wording_changed``: a verified obligation stated by both
  versions, with the quotes only one of them has;
- ``conflict_changed``: a conflict check task whose sides differ between
  the plan before and the plan after.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import datetime
from typing import Any

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Assessment,
    AssessmentDeadline,
    ChangeEvent,
    Course,
    EvidenceLink,
    Obligation,
    Resource,
    ResourceVersion,
    SourceSpan,
    StudyPlan,
    StudyTask,
)
from app.domain.enums import ChangeType, EvidenceField, ResourceType, VerificationStatus
from app.reasoning.supersession import superseded_deadlines


class SourceQuoteRead(BaseModel):
    """An exact quote: ``source_span.excerpt[start:end]``, where it is."""

    source_span_id: uuid.UUID
    quote: str
    resource_title: str
    version_number: int
    page_number: int | None
    slide_number: int | None
    section_path: str | None


class VersionRead(BaseModel):
    id: uuid.UUID
    version_number: int
    content_hash: str


class AffectedRead(BaseModel):
    kind: str
    assessment_id: uuid.UUID | None = None
    assessment_title: str | None = None
    obligation_id: uuid.UUID | None = None
    old_value: dict[str, Any] | None = None
    new_value: dict[str, Any] | None = None
    sources: list[SourceQuoteRead] = []


class ChangeRead(BaseModel):
    id: uuid.UUID
    observed_at: datetime
    change_type: ChangeType
    course_id: uuid.UUID
    course_code: str | None
    course_title: str
    resource_id: uuid.UUID
    resource_title: str
    resource_type: ResourceType
    old_version: VersionRead | None
    new_version: VersionRead
    workflow_run_id: uuid.UUID | None
    affected: list[AffectedRead]


class TaskChangeRead(BaseModel):
    task_key: str
    title: str
    fields: dict[str, list[Any]]  # field -> [before, after]


class PlanDiffRead(BaseModel):
    """Two saved plans compared by task key. Time-relative text (days left)
    is not a change; positions, status, anchors, titles, prerequisites and
    conflict sides are."""

    before_plan_id: uuid.UUID
    after_plan_id: uuid.UUID
    before_as_of: datetime
    after_as_of: datetime
    added: list[TaskChangeRead]
    removed: list[TaskChangeRead]
    changed: list[TaskChangeRead]
    unchanged_count: int


class SyncRead(BaseModel):
    observed_at: datetime
    changes: list[ChangeRead]
    plan_diff: PlanDiffRead | None


def _value(d: AssessmentDeadline) -> dict[str, Any]:
    return {
        "deadline_id": str(d.id),
        "date": d.due_date.isoformat(),
        "time": d.due_time.strftime("%H:%M") if d.due_time else None,
        "timezone": d.timezone,
    }


async def _quotes(
    session: AsyncSession, column: Any, version_id: uuid.UUID, field: EvidenceField
) -> dict[uuid.UUID, list[SourceQuoteRead]]:
    rows = await session.execute(
        select(EvidenceLink, SourceSpan, ResourceVersion, Resource)
        .join(SourceSpan, SourceSpan.id == EvidenceLink.source_span_id)
        .join(ResourceVersion, ResourceVersion.id == SourceSpan.resource_version_id)
        .join(Resource, Resource.id == ResourceVersion.resource_id)
        .where(
            SourceSpan.resource_version_id == version_id,
            EvidenceLink.field == field,
            EvidenceLink.verification_status == VerificationStatus.VERIFIED,
            column.is_not(None),
        )
        .order_by(SourceSpan.ordinal, EvidenceLink.quote_start)
    )
    out: dict[uuid.UUID, list[SourceQuoteRead]] = defaultdict(list)
    for link, span, version, resource in rows:
        out[getattr(link, column.key)].append(
            _source(span, version, resource, link.quote_start, link.quote_end)
        )
    return out


def _source(
    span: SourceSpan, version: ResourceVersion, resource: Resource, start: int, end: int
) -> SourceQuoteRead:
    return SourceQuoteRead(
        source_span_id=span.id,
        quote=span.excerpt[start:end],
        resource_title=resource.title,
        version_number=version.version_number,
        page_number=span.page_number,
        slide_number=span.slide_number,
        section_path=span.section_path,
    )


async def _affected(session: AsyncSession, event: ChangeEvent) -> list[AffectedRead]:
    out: list[AffectedRead] = []
    titles = dict(
        (
            await session.execute(
                select(Assessment.id, Assessment.title).where(
                    Assessment.course_id == event.course_id
                )
            )
        ).all()
    )
    new_deadlines = await _quotes(
        session, EvidenceLink.assessment_deadline_id, event.new_version_id, EvidenceField.DEADLINE
    )
    old_deadlines = (
        await _quotes(session, EvidenceLink.assessment_deadline_id, event.old_version_id,
                      EvidenceField.DEADLINE)
        if event.old_version_id else {}
    )  # fmt: skip
    deadlines = {
        d.id: d
        for d in (
            await session.scalars(
                select(AssessmentDeadline).where(AssessmentDeadline.assessment_id.in_(titles))
            )
        ).all()
    }
    for deadline_id in sorted(set(new_deadlines) - set(old_deadlines), key=str):
        d = deadlines[deadline_id]
        if d.status is VerificationStatus.VERIFIED:
            out.append(AffectedRead(kind="deadline_added", assessment_id=d.assessment_id,
                                    assessment_title=titles[d.assessment_id],
                                    new_value=_value(d), sources=new_deadlines[deadline_id]))  # fmt: skip
    for s in (await superseded_deadlines(session, event.course_id)).values():
        span, version, resource = (
            await session.execute(
                select(SourceSpan, ResourceVersion, Resource)
                .join(ResourceVersion, ResourceVersion.id == SourceSpan.resource_version_id)
                .join(Resource, Resource.id == ResourceVersion.resource_id)
                .where(SourceSpan.id == s.span_id)
            )
        ).one()
        if version.id != event.new_version_id:
            continue
        old, new = deadlines[s.deadline_id], deadlines[s.by_deadline_id]
        out.append(
            AffectedRead(
                kind="deadline_superseded",
                assessment_id=old.assessment_id,
                assessment_title=titles[old.assessment_id],
                old_value=_value(old),
                new_value=_value(new) | {"rule": s.rule},
                sources=[_source(span, version, resource, s.quote_start, s.quote_end)],
            )
        )
    if event.old_version_id:
        before = await _quotes(
            session, EvidenceLink.obligation_id, event.old_version_id, EvidenceField.OBLIGATION
        )
        after = await _quotes(
            session, EvidenceLink.obligation_id, event.new_version_id, EvidenceField.OBLIGATION
        )
        verified = set(
            (
                await session.scalars(
                    select(Obligation.id).where(
                        Obligation.id.in_(set(before) & set(after)),
                        Obligation.status == VerificationStatus.VERIFIED,
                    )
                )
            ).all()
        )
        for obligation_id in sorted(verified, key=str):
            old_q = {q.quote for q in before[obligation_id]}
            new_q = {q.quote for q in after[obligation_id]}
            if old_q != new_q:
                out.append(AffectedRead(
                    kind="obligation_wording_changed", obligation_id=obligation_id,
                    old_value={"quotes": sorted(old_q - new_q)},
                    new_value={"quotes": sorted(new_q - old_q)},
                    sources=[q for q in after[obligation_id] if q.quote in new_q - old_q],
                ))  # fmt: skip
    return out


def _conflict_values(task: StudyTask) -> list[Any] | None:
    for reason in task.reasons:
        if reason.get("code") == "conflicting_deadline":
            return [[v["date"], v["time"]] for v in reason["values"]]
    return None


def _task_fields(task: StudyTask) -> dict[str, Any]:
    return {
        "position": task.position,
        "status": task.status.value,
        "anchor_at": task.anchor_at.isoformat() if task.anchor_at else None,
        "title": task.title,
        "prerequisite_keys": sorted(task.prerequisite_keys),
        "conflict_values": _conflict_values(task),
    }


async def plan_diff(session: AsyncSession, after: StudyPlan) -> PlanDiffRead | None:
    before = await session.scalar(
        select(StudyPlan)
        .where(StudyPlan.learner == after.learner, StudyPlan.created_at < after.created_at)
        .order_by(StudyPlan.created_at.desc(), StudyPlan.id.desc())
        .limit(1)
    )
    if before is None:
        return None
    tasks: dict[uuid.UUID, dict[str, StudyTask]] = {}
    for plan in (before, after):
        rows = (await session.scalars(select(StudyTask).where(StudyTask.plan_id == plan.id))).all()
        tasks[plan.id] = {t.task_key: t for t in rows}
    old, new = tasks[before.id], tasks[after.id]
    changed = []
    for key in sorted(set(old) & set(new)):
        a, b = _task_fields(old[key]), _task_fields(new[key])
        fields = {f: [a[f], b[f]] for f in a if a[f] != b[f]}
        if fields:
            changed.append(TaskChangeRead(task_key=key, title=new[key].title, fields=fields))
    changed.sort(key=lambda c: new[c.task_key].position)
    return PlanDiffRead(
        before_plan_id=before.id,
        after_plan_id=after.id,
        before_as_of=before.as_of,
        after_as_of=after.as_of,
        added=[TaskChangeRead(task_key=k, title=new[k].title, fields={})
               for k in sorted(set(new) - set(old), key=lambda k: new[k].position)],
        removed=[TaskChangeRead(task_key=k, title=old[k].title, fields={})
                 for k in sorted(set(old) - set(new), key=lambda k: old[k].position)],
        changed=changed,
        unchanged_count=len(set(old) & set(new)) - len(changed),
    )  # fmt: skip


async def syncs(session: AsyncSession) -> list[SyncRead]:
    events = (
        await session.scalars(
            select(ChangeEvent).order_by(ChangeEvent.observed_at, ChangeEvent.created_at,
                                         ChangeEvent.id)
        )
    ).all()  # fmt: skip
    groups: dict[datetime, list[ChangeEvent]] = defaultdict(list)
    for event in events:
        groups[event.observed_at].append(event)
    out = []
    for observed_at, group in groups.items():
        reads = [await _change(session, e) for e in group]
        after = await session.scalar(
            select(StudyPlan)
            .where(StudyPlan.workflow_run_id.in_({e.workflow_run_id for e in group}))
            .order_by(StudyPlan.created_at.desc())
            .limit(1)
        )
        diff = await plan_diff(session, after) if after else None
        if diff is not None and after is not None:
            await _conflict_changes(session, diff, after, reads)
        out.append(SyncRead(observed_at=observed_at, changes=reads, plan_diff=diff))
    return out


async def _conflict_changes(
    session: AsyncSession, diff: PlanDiffRead, after: StudyPlan, reads: list[ChangeRead]
) -> None:
    """A conflict whose sides moved is attached to its course's change."""
    for change in diff.changed:
        if "conflict_values" not in change.fields:
            continue
        task = await session.scalar(
            select(StudyTask).where(
                StudyTask.plan_id == after.id, StudyTask.task_key == change.task_key
            )
        )
        read = next((r for r in reads if task and r.course_id == task.course_id), None)
        if task is None or read is None:
            continue
        before, now = change.fields["conflict_values"]
        title = await session.scalar(
            select(Assessment.title).where(Assessment.id == task.assessment_id)
        )
        read.affected.append(
            AffectedRead(
                kind="conflict_changed",
                assessment_id=task.assessment_id,
                assessment_title=title,
                old_value={"sides": before},
                new_value={"sides": now},
            )
        )


async def _change(session: AsyncSession, event: ChangeEvent) -> ChangeRead:
    course = await session.get(Course, event.course_id)
    resource = await session.get(Resource, event.resource_id)
    new = await session.get(ResourceVersion, event.new_version_id)
    old = await session.get(ResourceVersion, event.old_version_id) if event.old_version_id else None
    assert course is not None and resource is not None and new is not None
    return ChangeRead(
        id=event.id,
        observed_at=event.observed_at,
        change_type=event.change_type,
        course_id=course.id,
        course_code=course.external_id,
        course_title=course.title,
        resource_id=resource.id,
        resource_title=resource.title,
        resource_type=resource.type,
        old_version=VersionRead(id=old.id, version_number=old.version_number,
                                content_hash=old.content_hash) if old else None,
        new_version=VersionRead(id=new.id, version_number=new.version_number,
                                content_hash=new.content_hash),
        workflow_run_id=event.workflow_run_id,
        affected=await _affected(session, event),
    )  # fmt: skip
