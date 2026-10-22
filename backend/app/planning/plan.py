# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""The cross-course study plan (``study-plan/v1``): tasks from stored facts
only, ordered by fixed rules, explained by reason codes. No model.

Tasks (one per fact, a stable ``key`` per fact):

- ``submit_assessment``: a verified assessment with an active deadline;
- ``study_material``: the teaching material mapped to an assessment's
  requirements, one task per (assessment, resource), targets = the mapped
  spans; completed when the learner has studied every target;
- ``obligation``: a verified obligation (not one that states a milestone
  ordering, which its milestone tasks show);
- ``check_conflict``: an open deadline conflict, with every side;
- ``ethics_approval`` / ``data_collection``: the milestone ends of a
  dependency edge.

Anchor: a task's assessment deadline (the obligation's assessment for an
obligation). An assessment in conflict is anchored to its earliest
conflicting deadline: a conservative scheduling choice that says nothing
about which source is right.

Order: (A) every prerequisite before the task it precedes (dependency
edges; study and conflict tasks before their submission); then, among the
tasks free to go next, (B) earlier anchor (none last), (C) mandatory before
recommended, (D) material being studied first, (E) the task key. Completed
tasks follow the open ones. ``blocked``: a ``required_for`` prerequisite is
not completed.

Why now: reason codes with their values (``reasons``) and one sentence
rendered from them alone (``render_why_now``).
"""

from __future__ import annotations

import heapq
import uuid
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Assessment,
    AssessmentDeadline,
    AssessmentRequirement,
    Conflict,
    Course,
    DependencyEdge,
    EvidenceLink,
    MaterialMapping,
    Obligation,
    Resource,
    ResourceVersion,
    SourceSpan,
    StudyState,
    conflict_sides,
)
from app.domain.enums import (
    ConflictStatus,
    DependencyNodeKind,
    DependencyRelation,
    Milestone,
    ObligationModality,
    StudyStateValue,
    StudyTaskStatus,
    StudyTaskType,
    VerificationStatus,
)
from app.reasoning.conflicts import active_deadlines

PLAN_VERSION = "study-plan/v1"
LEARNER = "demo-student"
T = StudyTaskType
# Fixed defaults per task type, not estimates of the learner or the material.
MINUTES = {
    T.STUDY_MATERIAL: 60,
    T.OBLIGATION: 30,
    T.SUBMIT_ASSESSMENT: 20,
    T.CHECK_CONFLICT: 10,
    T.ETHICS_APPROVAL: 45,
    T.DATA_COLLECTION: 120,
}
MILESTONE_TASKS = {
    Milestone.ETHICS_APPROVAL: T.ETHICS_APPROVAL,
    Milestone.DATA_COLLECTION: T.DATA_COLLECTION,
}
MILESTONE_TITLES = {
    Milestone.ETHICS_APPROVAL: "Get ethics approval",
    Milestone.DATA_COLLECTION: "Start data collection",
}


# --- facts --------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DeadlineFact:
    id: uuid.UUID
    due_date: date
    due_time: time | None
    timezone: str | None

    @property
    def instant(self) -> datetime:
        zone = ZoneInfo(self.timezone) if self.timezone else UTC
        return datetime.combine(self.due_date, self.due_time or time(23, 59), zone)

    def value(self) -> dict[str, Any]:
        return {
            "deadline_id": str(self.id),
            "date": self.due_date.isoformat(),
            "time": self.due_time.strftime("%H:%M") if self.due_time else None,
            "timezone": self.timezone,
        }


@dataclass(frozen=True, slots=True)
class TargetFact:
    span_id: uuid.UUID
    resource_id: uuid.UUID
    resource_title: str
    slide: int | None
    page: int | None
    section: str | None


@dataclass(slots=True)
class CourseFacts:
    id: uuid.UUID
    ref: str
    title: str
    assessments: dict[uuid.UUID, tuple[str, VerificationStatus]] = field(default_factory=dict)
    deadlines: dict[uuid.UUID, list[DeadlineFact]] = field(default_factory=dict)  # active
    conflicts: dict[uuid.UUID, tuple[uuid.UUID, list[DeadlineFact]]] = field(default_factory=dict)
    obligations: list[tuple[uuid.UUID, str, ObligationModality | None, uuid.UUID | None]] = field(
        default_factory=list
    )
    edges: list[DependencyEdge] = field(default_factory=list)
    targets: dict[uuid.UUID, list[TargetFact]] = field(default_factory=dict)  # assessment -> spans


@dataclass(slots=True)
class PlanFacts:
    courses: list[CourseFacts]
    states: dict[uuid.UUID, StudyStateValue]  # span -> the learner's state


async def load_facts(session: AsyncSession, learner: str = LEARNER) -> PlanFacts:
    courses = (await session.scalars(select(Course).order_by(Course.external_id, Course.id))).all()
    out = []
    for course in courses:
        facts = CourseFacts(course.id, course.external_id or str(course.id), course.title)
        for a in (
            await session.scalars(select(Assessment).where(Assessment.course_id == course.id))
        ).all():
            facts.assessments[a.id] = (a.title, a.status)
        for assessment_id, deadlines in (await active_deadlines(session, course.id)).items():
            facts.deadlines[assessment_id] = [_deadline(d) for d in deadlines]
        for conflict in (
            await session.scalars(
                select(Conflict).where(
                    Conflict.course_id == course.id, Conflict.status == ConflictStatus.OPEN
                )
            )
        ).all():
            sides = (
                await session.scalars(
                    select(AssessmentDeadline)
                    .join(
                        conflict_sides,
                        conflict_sides.c.assessment_deadline_id == AssessmentDeadline.id,
                    )
                    .where(conflict_sides.c.conflict_id == conflict.id)
                )
            ).all()
            facts.conflicts[conflict.assessment_id] = (conflict.id, [_deadline(d) for d in sides])
        facts.edges = list(
            (
                await session.scalars(
                    select(DependencyEdge).where(DependencyEdge.course_id == course.id)
                )
            ).all()
        )
        milestone_sources = {
            e.derived_from_obligation_id
            for e in facts.edges
            if e.prerequisite_kind is DependencyNodeKind.MILESTONE
        }
        # Only obligations a current version still states.
        current = set(
            (
                await session.scalars(
                    select(EvidenceLink.obligation_id)
                    .join(SourceSpan, SourceSpan.id == EvidenceLink.source_span_id)
                    .join(ResourceVersion, ResourceVersion.id == SourceSpan.resource_version_id)
                    .join(Resource, Resource.id == ResourceVersion.resource_id)
                    .where(
                        Resource.course_id == course.id,
                        Resource.current_version_id == ResourceVersion.id,
                        EvidenceLink.verification_status == VerificationStatus.VERIFIED,
                        EvidenceLink.obligation_id.is_not(None),
                    )
                )
            ).all()
        )
        for o in (
            await session.scalars(
                select(Obligation).where(
                    Obligation.course_id == course.id,
                    Obligation.status == VerificationStatus.VERIFIED,
                    Obligation.id.in_(current),
                )
            )
        ).all():
            if o.id not in milestone_sources:
                facts.obligations.append((o.id, o.canonical_action, o.modality, o.assessment_id))
        rows = await session.execute(
            select(AssessmentRequirement.assessment_id, SourceSpan, Resource)
            .join(MaterialMapping, MaterialMapping.requirement_id == AssessmentRequirement.id)
            .join(SourceSpan, SourceSpan.id == MaterialMapping.source_span_id)
            .join(ResourceVersion, ResourceVersion.id == SourceSpan.resource_version_id)
            .join(Resource, Resource.id == ResourceVersion.resource_id)
            .join(Assessment, Assessment.id == AssessmentRequirement.assessment_id)
            .where(
                Assessment.course_id == course.id,
                Resource.current_version_id == ResourceVersion.id,
            )
            .order_by(Resource.source_uri, SourceSpan.ordinal)
        )
        for assessment_id, span, resource in rows:
            targets = facts.targets.setdefault(assessment_id, [])
            if all(t.span_id != span.id for t in targets):
                targets.append(TargetFact(span.id, resource.id, resource.title, span.slide_number,
                                          span.page_number, span.section_path))  # fmt: skip
        out.append(facts)
    states = dict(
        (
            await session.execute(
                select(StudyState.source_span_id, StudyState.state).where(
                    StudyState.learner == learner
                )
            )
        ).all()
    )
    return PlanFacts(out, states)


def _deadline(d: AssessmentDeadline) -> DeadlineFact:
    return DeadlineFact(d.id, d.due_date, d.due_time, d.timezone)


# --- tasks --------------------------------------------------------------------------------


@dataclass(slots=True)
class PlannedTask:
    key: str
    task_type: StudyTaskType
    course_id: uuid.UUID
    course_title: str
    title: str
    mandatory: bool
    assessment_id: uuid.UUID | None = None
    assessment_title: str | None = None
    obligation_id: uuid.UUID | None = None
    conflict_id: uuid.UUID | None = None
    edge_id: uuid.UUID | None = None
    anchor: DeadlineFact | None = None
    anchor_basis: str | None = None  # stated_deadline | earliest_conflicting_deadline
    conflict_values: list[DeadlineFact] = field(default_factory=list)
    targets: list[TargetFact] = field(default_factory=list)
    study_state: StudyStateValue | None = None
    # (task key, blocking): blocking for a required_for edge, else ordering only.
    prerequisites: list[tuple[str, bool]] = field(default_factory=list)
    status: StudyTaskStatus = StudyTaskStatus.READY
    position: int = 0
    reasons: list[dict[str, Any]] = field(default_factory=list)
    why_now: str = ""

    @property
    def minutes(self) -> int:
        return MINUTES[self.task_type]


def _ranges(numbers: Sequence[int]) -> str:
    values = sorted(set(numbers))
    runs: list[list[int]] = []
    for n in values:
        if runs and n == runs[-1][-1] + 1:
            runs[-1].append(n)
        else:
            runs.append([n])
    return ", ".join(f"{r[0]}–{r[-1]}" if len(r) > 1 else str(r[0]) for r in runs)


def _where(targets: Sequence[TargetFact]) -> str:
    slides = [t.slide for t in targets if t.slide is not None]
    pages = [t.page for t in targets if t.page is not None]
    sections = {t.section for t in targets if t.slide is None and t.page is None and t.section}
    parts = []
    if slides:
        parts.append(f"slide{'s' if len(set(slides)) > 1 else ''} {_ranges(slides)}")
    if pages:
        parts.append(f"page{'s' if len(set(pages)) > 1 else ''} {_ranges(pages)}")
    if sections:
        parts.append(f"{len(sections)} section{'s' if len(sections) > 1 else ''}")
    return ", ".join(parts)


def _anchor(
    course: CourseFacts, assessment_id: uuid.UUID | None
) -> tuple[DeadlineFact | None, str | None]:
    if assessment_id is None:
        return None, None
    if assessment_id in course.conflicts:
        sides = course.conflicts[assessment_id][1]
        return min(sides, key=lambda d: d.instant), "earliest_conflicting_deadline"
    deadlines = course.deadlines.get(assessment_id, [])
    if not deadlines:
        return None, None
    return min(deadlines, key=lambda d: d.instant), "stated_deadline"


def build_tasks(facts: PlanFacts) -> list[PlannedTask]:
    tasks: dict[str, PlannedTask] = {}
    for course in facts.courses:

        def submit_key(assessment_id: uuid.UUID) -> str:
            return f"submit:{assessment_id}"

        for assessment_id, (title, status) in course.assessments.items():
            if status is not VerificationStatus.VERIFIED:
                continue
            anchor, basis = _anchor(course, assessment_id)
            if anchor is None:
                continue
            task = PlannedTask(submit_key(assessment_id), T.SUBMIT_ASSESSMENT, course.id,
                               course.title, f"Submit {title}", True, assessment_id, title,
                               anchor=anchor, anchor_basis=basis)  # fmt: skip
            tasks[task.key] = task
            if assessment_id in course.conflicts:
                conflict_id, sides = course.conflicts[assessment_id]
                check = PlannedTask(f"conflict:{conflict_id}", T.CHECK_CONFLICT, course.id,
                                    course.title, f"Check the {title} deadline with the module team",
                                    True, assessment_id, title, conflict_id=conflict_id,
                                    anchor=anchor, anchor_basis=basis,
                                    conflict_values=sorted(sides, key=lambda d: d.instant))  # fmt: skip
                tasks[check.key] = check
                task.prerequisites.append((check.key, False))
        for assessment_id, targets in course.targets.items():
            title = course.assessments.get(assessment_id, ("an assessment", None))[0]
            anchor, basis = _anchor(course, assessment_id)
            by_resource: dict[uuid.UUID, list[TargetFact]] = defaultdict(list)
            for target in targets:
                by_resource[target.resource_id].append(target)
            for resource_id, group in by_resource.items():
                states = [facts.states.get(t.span_id, StudyStateValue.NOT_STARTED) for t in group]
                state = (
                    StudyStateValue.STUDIED if all(s is StudyStateValue.STUDIED for s in states)
                    else StudyStateValue.STUDYING if any(s is not StudyStateValue.NOT_STARTED for s in states)
                    else StudyStateValue.NOT_STARTED
                )  # fmt: skip
                where = _where(group)
                task = PlannedTask(
                    f"study:{assessment_id}:{resource_id}", T.STUDY_MATERIAL, course.id,
                    course.title, f"Study “{group[0].resource_title}”"
                    + (f", {where}," if where else "") + f" for {title}",
                    False, assessment_id, title, anchor=anchor, anchor_basis=basis,
                    targets=group, study_state=state,
                )  # fmt: skip
                tasks[task.key] = task
                if submit_key(assessment_id) in tasks:
                    tasks[submit_key(assessment_id)].prerequisites.append((task.key, False))
        for obligation_id, action, modality, assessment_id in course.obligations:
            anchor, basis = _anchor(course, assessment_id)
            title = (
                course.assessments[assessment_id][0]
                if assessment_id in course.assessments
                else None
            )
            task = PlannedTask(f"obligation:{obligation_id}", T.OBLIGATION, course.id,
                               course.title, action, modality is ObligationModality.MANDATORY,
                               assessment_id, title, obligation_id=obligation_id, anchor=anchor,
                               anchor_basis=basis)  # fmt: skip
            tasks[task.key] = task
        for edge in sorted(course.edges, key=lambda e: str(e.id)):
            ends = []
            for name in ("prerequisite", "dependent"):
                kind = getattr(edge, f"{name}_kind")
                if kind is DependencyNodeKind.OBLIGATION:
                    ends.append(f"obligation:{getattr(edge, f'{name}_obligation_id')}")
                elif kind is DependencyNodeKind.ASSESSMENT:
                    ends.append(submit_key(getattr(edge, f"{name}_assessment_id")))
                else:
                    milestone: Milestone = getattr(edge, f"{name}_milestone")
                    key = f"milestone:{milestone.value}:{course.id}"
                    if key not in tasks:
                        tasks[key] = PlannedTask(key, MILESTONE_TASKS[milestone], course.id,
                                                 course.title, MILESTONE_TITLES[milestone],
                                                 milestone is Milestone.ETHICS_APPROVAL,
                                                 edge_id=edge.id)  # fmt: skip
                    ends.append(key)
            before, after = ends
            if before in tasks and after in tasks:
                blocking = edge.relation is DependencyRelation.REQUIRED_FOR
                tasks[after].prerequisites.append((before, blocking))
    for task in tasks.values():
        if task.task_type is T.STUDY_MATERIAL and task.study_state is StudyStateValue.STUDIED:
            task.status = StudyTaskStatus.COMPLETED
    return list(tasks.values())


# --- order and reasons --------------------------------------------------------------------


def _rank(task: PlannedTask) -> tuple[object, ...]:
    anchor = task.anchor.instant if task.anchor else datetime.max.replace(tzinfo=UTC)
    return (anchor, 0 if task.mandatory else 1,
            0 if task.study_state is StudyStateValue.STUDYING else 1, task.key)  # fmt: skip


def order_tasks(tasks: Sequence[PlannedTask]) -> list[PlannedTask]:
    """Prerequisites first (A), then anchor, mandatory, studying, key (B-E)."""
    by_key = {t.key: t for t in tasks}
    done = {t.key for t in tasks if t.status is StudyTaskStatus.COMPLETED}
    open_tasks = [t for t in tasks if t.key not in done]
    waiting = {
        t.key: {k for k, _ in t.prerequisites if k in by_key and k not in done} for t in open_tasks
    }
    followers: dict[str, list[str]] = defaultdict(list)
    for key, needs in waiting.items():
        for need in needs:
            followers[need].append(key)
    ready = [(_rank(by_key[k]), k) for k, needs in waiting.items() if not needs]
    heapq.heapify(ready)
    ordered: list[PlannedTask] = []
    while ready:
        _, key = heapq.heappop(ready)
        ordered.append(by_key[key])
        for follower in followers[key]:
            waiting[follower].discard(key)
            if not waiting[follower]:
                heapq.heappush(ready, (_rank(by_key[follower]), follower))
    if len(ordered) != len(open_tasks):  # a cycle: keep the rest in rank order, never loop
        placed = {t.key for t in ordered}
        ordered += sorted((t for t in open_tasks if t.key not in placed), key=_rank)
    ordered += sorted((by_key[k] for k in done), key=_rank)
    for position, task in enumerate(ordered, start=1):
        task.position = position
        blocking = [k for k, b in task.prerequisites if b and k in by_key and k not in done]
        if task.status is not StudyTaskStatus.COMPLETED and blocking:
            task.status = StudyTaskStatus.BLOCKED
    return ordered


def _value_text(d: dict[str, Any]) -> str:
    day = _day(d["date"])
    return f"{day} {d['time']}" if d["time"] else day


def _day(iso: str) -> str:
    value = date.fromisoformat(iso)
    return f"{value.day} {value.strftime('%b %Y')}"


def reasons_for(
    task: PlannedTask, by_key: dict[str, PlannedTask], as_of: datetime
) -> list[dict[str, Any]]:
    reasons: list[dict[str, Any]] = []
    if task.conflict_values:
        reasons.append({"code": "conflicting_deadline", "assessment": task.assessment_title,
                        "values": [d.value() for d in task.conflict_values],
                        "anchor": "earliest", "resolved": False})  # fmt: skip
    if task.anchor is not None:
        zone = ZoneInfo(task.anchor.timezone) if task.anchor.timezone else UTC
        days = (task.anchor.due_date - as_of.astimezone(zone).date()).days
        reasons.append({"code": "deadline_in_days", "days": days, "assessment": task.assessment_title,
                        "basis": task.anchor_basis, **task.anchor.value()})  # fmt: skip
    else:
        reasons.append({"code": "no_deadline"})
    for key, blocking in task.prerequisites:
        if blocking and key in by_key and by_key[key].status is not StudyTaskStatus.COMPLETED:
            reasons.append({"code": "blocked_by", "task_key": key, "title": by_key[key].title})
    for other in by_key.values():
        for key, blocking in other.prerequisites:
            if key == task.key and blocking:
                reasons.append(
                    {"code": "required_for", "task_key": other.key, "title": other.title}
                )
    reasons.append({"code": "mandatory" if task.mandatory else "recommended"})
    if task.study_state is StudyStateValue.STUDYING:
        reasons.append({"code": "currently_studying"})
    if task.study_state is StudyStateValue.STUDIED:
        reasons.append({"code": "studied"})
    return reasons


def render_why_now(reasons: Sequence[dict[str, Any]]) -> str:
    """One short sentence from the reason codes and their values alone."""
    parts: list[str] = []
    codes = {r["code"]: r for r in reasons}
    if "studied" in codes:
        return "Done: you have studied this material."
    for r in reasons:
        code = r["code"]
        if code == "conflicting_deadline":
            values = " or ".join(_value_text(v) for v in r["values"])
            parts.append(f"Sources disagree on the {r['assessment']} deadline ({values}); "
                         "this plan uses the earliest only to schedule, which does not decide "
                         "which source is right.")  # fmt: skip
        elif code == "deadline_in_days":
            when = (f"in {r['days']} days" if r["days"] > 1 else "tomorrow" if r["days"] == 1
                    else "today" if r["days"] == 0 else f"{-r['days']} days ago")  # fmt: skip
            what = (
                "earliest stated deadline"
                if r["basis"] == "earliest_conflicting_deadline"
                else "deadline"
            )
            parts.append(f"The {r['assessment']} {what} is {when} ({_value_text(r)}).")
        elif code == "no_deadline":
            parts.append("No deadline is stated.")
        elif code == "required_for":
            parts.append(f"Required before: {r['title']}.")
        elif code == "blocked_by":
            parts.append(f"Waiting for: {r['title']}.")
        elif code == "currently_studying":
            parts.append("You are studying this.")
    if "mandatory" in codes and not any(p.startswith("Required") for p in parts):
        parts.append("Mandatory.")
    return " ".join(parts)


def plan(facts: PlanFacts, as_of: datetime) -> list[PlannedTask]:
    """The ordered, explained plan: pure and deterministic for its inputs."""
    if as_of.tzinfo is None:
        raise ValueError("as_of must be timezone-aware")
    ordered = order_tasks(build_tasks(facts))
    by_key = {t.key: t for t in ordered}
    for task in ordered:
        task.reasons = reasons_for(task, by_key, as_of)
        task.why_now = render_why_now(task.reasons)
    return ordered
