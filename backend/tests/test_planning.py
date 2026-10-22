# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""The deterministic study plan: prerequisites first, then anchor,
mandatory, studying, key; why-now rendered from reason codes alone; a
deadline conflict exposed with every side and never resolved."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, date, datetime, time
from types import SimpleNamespace
from typing import Any

from app.domain.enums import (
    DependencyNodeKind,
    DependencyRelation,
    Milestone,
    ObligationModality,
    StudyStateValue,
    StudyTaskStatus,
    StudyTaskType,
    VerificationStatus,
)
from app.planning.plan import (
    CourseFacts,
    DeadlineFact,
    PlanFacts,
    TargetFact,
    plan,
    render_why_now,
)

AS_OF = datetime(2026, 10, 5, 8, 0, tzinfo=UTC)
V = VerificationStatus.VERIFIED
LONDON = "Europe/London"


def _id(n: int) -> uuid.UUID:
    return uuid.UUID(int=n)


def _deadline(n: int, day: date, at: time | None = time(12)) -> DeadlineFact:
    return DeadlineFact(_id(n), day, at, LONDON)


def _edge(**ends: Any) -> Any:
    fields = {f"{end}_{part}": None for end in ("prerequisite", "dependent")
              for part in ("assessment_id", "obligation_id", "milestone")}  # fmt: skip
    return SimpleNamespace(
        id=uuid.uuid4(), relation=DependencyRelation.REQUIRED_FOR, **(fields | ends)
    )


def _course(n: int, ref: str) -> CourseFacts:
    return CourseFacts(_id(n), ref, f"Course {ref}")


def _project_course() -> CourseFacts:
    """A spec due 30 Nov with a mandatory review before it, an ethics edge."""
    course = _course(100, "msd")
    spec, review = _id(1), _id(2)
    course.assessments[spec] = ("Project Specification", V)
    course.deadlines[spec] = [_deadline(10, date(2026, 11, 30))]
    course.obligations.append((review, "Have your supervisor review a draft",
                               ObligationModality.MANDATORY, spec))  # fmt: skip
    course.edges = [
        _edge(prerequisite_kind=DependencyNodeKind.OBLIGATION, prerequisite_obligation_id=review,
              dependent_kind=DependencyNodeKind.ASSESSMENT, dependent_assessment_id=spec),
        _edge(prerequisite_kind=DependencyNodeKind.MILESTONE,
              prerequisite_milestone=Milestone.ETHICS_APPROVAL,
              dependent_kind=DependencyNodeKind.MILESTONE,
              dependent_milestone=Milestone.DATA_COLLECTION),
    ]  # fmt: skip
    return course


def _coursework_course(*, conflict: bool = True) -> CourseFacts:
    """Coursework 1 with a lecture mapped to it; two disagreeing deadlines."""
    course = _course(200, "aml")
    cw1 = _id(3)
    course.assessments[cw1] = ("Coursework 1", V)
    early, late = (
        _deadline(20, date(2026, 11, 12), time(14)),
        _deadline(21, date(2026, 11, 13), time(23, 59)),
    )
    course.deadlines[cw1] = [early, late] if conflict else [early]
    if conflict:
        course.conflicts[cw1] = (_id(30), [late, early])
    course.targets[cw1] = [
        TargetFact(_id(40 + s), _id(50), "Week 7", s, None, None) for s in (2, 3, 4, 6)
    ]
    return course


def _plan(*courses: CourseFacts, states: dict[uuid.UUID, StudyStateValue] | None = None) -> Any:
    return plan(PlanFacts(list(courses), states or {}), AS_OF)


def _keys(tasks: Any) -> list[str]:
    return [t.key for t in tasks]


def test_prerequisites_come_first_and_block_what_they_are_required_for() -> None:
    tasks = _plan(_project_course())
    keys = _keys(tasks)
    assert keys.index(f"obligation:{_id(2)}") < keys.index(f"submit:{_id(1)}")
    ethics, data = f"milestone:ethics_approval:{_id(100)}", f"milestone:data_collection:{_id(100)}"
    assert keys.index(ethics) < keys.index(data)
    by_key = {t.key: t for t in tasks}
    assert by_key[f"submit:{_id(1)}"].status is StudyTaskStatus.BLOCKED
    assert by_key[data].status is StudyTaskStatus.BLOCKED
    assert by_key[ethics].status is StudyTaskStatus.READY
    assert (
        "Required before: Submit Project Specification." in by_key[f"obligation:{_id(2)}"].why_now
    )


def test_nearer_mandatory_work_ranks_above_later_recommended_work_across_courses() -> None:
    tasks = _plan(_project_course(), _coursework_course())
    assert {t.course_id for t in tasks} == {_id(100), _id(200)}
    by_key = {t.key: t for t in tasks}
    submit_cw1, study_spec_free = by_key[f"submit:{_id(3)}"], by_key[f"obligation:{_id(2)}"]
    assert submit_cw1.position < study_spec_free.position  # 12 Nov before 30 Nov
    # Same anchor: mandatory (conflict check) before recommended (study).
    check, study = by_key[f"conflict:{_id(30)}"], by_key[f"study:{_id(3)}:{_id(50)}"]
    assert check.position < study.position < submit_cw1.position


def test_the_conflict_is_exposed_with_every_side_and_never_resolved() -> None:
    tasks = _plan(_coursework_course())
    by_key = {t.key: t for t in tasks}
    check = by_key[f"conflict:{_id(30)}"]
    assert check.task_type is StudyTaskType.CHECK_CONFLICT
    (conflict,) = [r for r in check.reasons if r["code"] == "conflicting_deadline"]
    assert [v["date"] for v in conflict["values"]] == ["2026-11-12", "2026-11-13"]
    assert conflict["resolved"] is False and conflict["anchor"] == "earliest"
    assert "12 Nov 2026 14:00 or 13 Nov 2026 23:59" in check.why_now
    assert "does not decide which source is right" in check.why_now
    for task in (check, by_key[f"submit:{_id(3)}"]):
        assert task.anchor_basis == "earliest_conflicting_deadline"
        assert task.anchor.id == _id(20)  # scheduling only: the earliest side
    text = json.dumps([t.reasons for t in tasks])
    assert not any(word in text for word in ("winner", "authoritative", 'resolved": true'))
    # Without the conflict, the single stated deadline is the anchor.
    plain = {t.key: t for t in _plan(_coursework_course(conflict=False))}
    assert plain[f"submit:{_id(3)}"].anchor_basis == "stated_deadline"


def test_studied_material_completes_its_task_and_leaves_the_rest() -> None:
    course = _coursework_course()
    study_key = f"study:{_id(3)}:{_id(50)}"
    before = {t.key: t for t in _plan(course)}
    spans = {t.span_id for t in course.targets[_id(3)]}
    half = {s: StudyStateValue.STUDIED for s in list(spans)[:2]}
    studying = {t.key: t for t in _plan(_coursework_course(), states=half)}
    assert studying[study_key].study_state is StudyStateValue.STUDYING
    assert "You are studying this." in studying[study_key].why_now
    done = _plan(_coursework_course(), states=dict.fromkeys(spans, StudyStateValue.STUDIED))
    by_key = {t.key: t for t in done}
    assert by_key[study_key].status is StudyTaskStatus.COMPLETED
    assert done[-1].key == study_key  # completed work follows the open work
    assert set(by_key) == set(before)
    assert [t.key for t in done[:-1]] == [k for k in _keys(before.values()) if k != study_key]


def test_the_plan_is_stable_and_why_now_is_reproducible_from_its_reasons() -> None:
    first, second = (
        _plan(_project_course(), _coursework_course()),
        _plan(_project_course(), _coursework_course()),
    )
    assert [(t.key, t.position, t.why_now) for t in first] == [
        (t.key, t.position, t.why_now) for t in second
    ]
    for task in first:
        assert render_why_now(json.loads(json.dumps(task.reasons))) == task.why_now
        assert task.why_now


def test_a_dependency_cycle_never_loops() -> None:
    course = _project_course()
    review, spec = _id(2), _id(1)
    course.edges.append(
        _edge(prerequisite_kind=DependencyNodeKind.ASSESSMENT, prerequisite_assessment_id=spec,
              dependent_kind=DependencyNodeKind.OBLIGATION, dependent_obligation_id=review)
    )  # fmt: skip
    tasks = _plan(course)
    assert len(tasks) == len({t.key for t in tasks})
