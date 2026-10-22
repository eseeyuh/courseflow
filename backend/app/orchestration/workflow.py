# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""The course-intelligence workflow (``course-intelligence/v1``): the
existing services, in a fixed order, with one traced WorkflowRun per course.

Steps (``STEPS``, the allowlist, in execution order):

    ingest -> assessments -> claims -> mapping -> dependencies -> conflicts
    -> validation -> plan

A step is a call into an existing service; no business logic lives here.
Each service keeps its own child WorkflowRun, ModelCalls and transaction
boundaries, so a failed step leaves only what its service commits per unit
(a failed unit writes nothing). Steps run step by step across the courses
(every course's claims before any mapping: the recorded mapping queries
cover every course).

Ingest creates the courses, so each course's run starts after it; the
ingest step is the first entry of its trace. ``plan`` is one cross-course
study plan, saved as a snapshot at the replay's demo clock (``as_of``);
it is recorded in every course's trace. The workflow never retries
(retries are the provider layer's, bounded there) and never loops: a step
that raises, or whose child run failed, or a validation with errors, stops
it. That course's run is failed with the step and a deterministic error
category; the other courses' runs are failed as ``aborted``.

Requested step names must be in the allowlist; anything else is refused
before any write.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import Course, Institution, WorkflowRun
from app.demo.intelligence import DemoReplay, StepOutcome
from app.domain.enums import LmsType, WorkflowRunStatus
from app.extraction.validation import validate_course_intelligence
from app.reasoning.conflicts import detect_course_conflicts
from app.reasoning.dependencies import derive_course_dependencies

WORKFLOW_VERSION = "course-intelligence/v1"
StepName = Literal[
    "ingest",
    "assessments",
    "claims",
    "mapping",
    "dependencies",
    "conflicts",
    "validation",
    "plan",
]
STEPS: tuple[StepName, ...] = (
    "ingest", "assessments", "claims", "mapping", "dependencies", "conflicts", "validation",
    "plan",
)  # fmt: skip
# Run once for every course, outside the per-course loop.
GLOBAL_STEPS = frozenset({"ingest", "plan"})


class UnknownStepError(ValueError):
    """A requested step is not in the allowlist. Names it, writes nothing."""

    def __init__(self, names: Sequence[str]) -> None:
        super().__init__(f"steps not allowed: {', '.join(sorted(names))}")
        self.names = tuple(names)


class StepFailed(RuntimeError):
    """A step finished but did not succeed (a failed child run, a validation
    with errors)."""

    def __init__(self, category: str, outcome: StepOutcome) -> None:
        super().__init__(category)
        self.category = category
        self.outcome = outcome


def allowed_steps(steps: Sequence[str] | None) -> list[str]:
    """The requested steps in the fixed order; UnknownStepError for any name
    outside the allowlist (or none at all)."""
    requested = list(STEPS if steps is None else steps)
    unknown = [name for name in requested if name not in STEPS]
    if unknown or not requested:
        raise UnknownStepError(unknown or ["<none>"])
    return [name for name in STEPS if name in requested]


def classify(exc: BaseException) -> str:
    """A stable error category from the exception type: never its text."""
    from app.ai.errors import AIProviderError
    from app.ai.model_calls import RecordedCallError
    from app.demo.seed import IndexingIncomplete
    from app.evaluation.claims.run import ReplayMissing as ClaimReplayMissing
    from app.evaluation.northbridge.labels import LabelError
    from app.evaluation.retrieval.snapshot import SnapshotError
    from app.mapping.persist import MappingIntegrityError
    from app.mapping.replay import ReplayMissing as MappingReplayMissing
    from app.reasoning.dependencies import DependencyIntegrityError
    from app.retrieval.search import IndexStateError, SearchProviderError

    if isinstance(exc, StepFailed):
        return exc.category
    table: tuple[tuple[tuple[type[BaseException], ...], str], ...] = (
        ((ClaimReplayMissing, MappingReplayMissing, SnapshotError, LabelError), "replay_unavailable"),
        ((AIProviderError, RecordedCallError, SearchProviderError), "provider_error"),
        ((MappingIntegrityError, DependencyIntegrityError, IntegrityError), "integrity_error"),
        ((IndexStateError, IndexingIncomplete), "index_state"),
    )  # fmt: skip
    for types, category in table:
        if isinstance(exc, types):
            return category
    return "unexpected"


class StepTrace(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    status: Literal["succeeded", "failed"]
    started_at: datetime
    finished_at: datetime
    child_run_ids: list[uuid.UUID] = []
    counts: dict[str, Any] = {}
    error_category: str | None = None
    error_type: str | None = None


class WorkflowTrace(BaseModel):
    """The ``summary`` of a course-intelligence/v1 run: IDs and counts only."""

    model_config = ConfigDict(extra="forbid")

    workflow_version: Literal["course-intelligence/v1"] = WORKFLOW_VERSION
    mode: str
    requested_steps: list[str]
    current_step: str | None = None
    failed_step: str | None = None
    error_category: str | None = None
    steps: list[StepTrace] = []


@dataclass(slots=True)
class WorkflowResult:
    courses: dict[str, uuid.UUID]
    runs: dict[str, uuid.UUID] = field(default_factory=dict)  # course ref -> run ID
    traces: dict[str, WorkflowTrace] = field(default_factory=dict)
    statuses: dict[str, WorkflowRunStatus] = field(default_factory=dict)

    @property
    def succeeded(self) -> bool:
        return bool(self.statuses) and all(
            s is WorkflowRunStatus.SUCCEEDED for s in self.statuses.values()
        )

    def status_by_course(self) -> dict[str, str]:
        return {ref: status.value for ref, status in self.statuses.items()}


CourseStep = Callable[[uuid.UUID], Awaitable[StepOutcome]]


class CourseIntelligenceWorkflow:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], replay: DemoReplay) -> None:
        self._sessions = sessions
        self._replay = replay

    def _registry(self, courses: dict[str, uuid.UUID]) -> dict[str, CourseStep]:
        """The allowlisted per-course steps: each one existing service call."""
        replay, sessions = self._replay, self._sessions

        async def mapping(course_id: uuid.UUID) -> StepOutcome:
            return await replay.mapping(course_id, courses)

        async def dependencies(course_id: uuid.UUID) -> StepOutcome:
            r = await derive_course_dependencies(sessions, course_id)
            return StepOutcome(r.status, (r.run_id,),
                               {"edges": r.summary.edges, "created": r.summary.created,
                                "kept": r.summary.kept, "removed": r.summary.removed,
                                "by_rule": r.summary.by_rule})  # fmt: skip

        async def conflicts(course_id: uuid.UUID) -> StepOutcome:
            r = await detect_course_conflicts(sessions, course_id)
            return StepOutcome(r.status, (r.run_id,),
                               {"open": r.summary.open, "created": r.summary.created,
                                "reopened": r.summary.reopened,
                                "resolved_by_source": r.summary.resolved_by_source})  # fmt: skip

        async def validation(course_id: uuid.UUID) -> StepOutcome:
            async with sessions() as session:
                report = await validate_course_intelligence(session, course_id)
            outcome = StepOutcome(
                WorkflowRunStatus.SUCCEEDED if report.clean else WorkflowRunStatus.FAILED,
                (),
                {"errors": len(report.errors), "warnings": len(report.warnings),
                 "by_code": {k: v for k, v in report.counts().items() if v}},
            )  # fmt: skip
            if not report.clean:
                raise StepFailed("validation_failed", outcome)
            return outcome

        return {
            "assessments": replay.assessments,
            "claims": replay.claims,
            "mapping": mapping,
            "dependencies": dependencies,
            "conflicts": conflicts,
            "validation": validation,
        }

    async def run(
        self, *, reset: bool = False, steps: Sequence[str] | None = None
    ) -> WorkflowResult:
        ordered = allowed_steps(steps)

        started = datetime.now(UTC)
        if "ingest" in ordered:
            courses, ingest = await self._replay.ingest(reset=reset)
        else:
            courses, ingest = await self._demo_courses(), {}
        finished = datetime.now(UTC)
        result = WorkflowResult(courses)
        for ref in sorted(courses):
            trace = WorkflowTrace(mode=self._replay.mode, requested_steps=ordered)
            if ref in ingest:
                trace.steps.append(_trace("ingest", ingest[ref], started, finished))
            result.traces[ref] = trace
            result.runs[ref] = await self._start(courses[ref], trace, started)

        try:
            return await self._steps(result, ordered)
        except (
            BaseException
        ) as exc:  # outside any step (cancelled, storage down): never left running
            current = next((t.current_step for t in result.traces.values() if t.current_step), None)
            await self._fail(result, "", current or "", classify(exc))
            raise

    async def _steps(self, result: WorkflowResult, ordered: list[str]) -> WorkflowResult:
        courses = result.courses
        registry = self._registry(courses)
        for name in ordered:
            if name in GLOBAL_STEPS:
                continue
            for ref in sorted(courses):
                trace = result.traces[ref]
                trace.current_step = name
                await self._save(result.runs[ref], trace)
                step_started = datetime.now(UTC)
                try:
                    outcome = await registry[name](courses[ref])
                    if outcome.status is not WorkflowRunStatus.SUCCEEDED:
                        raise StepFailed("child_run_failed", outcome)
                except Exception as exc:  # noqa: BLE001 - classified, recorded, then stop
                    failed = exc.outcome if isinstance(exc, StepFailed) else StepOutcome(
                        WorkflowRunStatus.FAILED
                    )  # fmt: skip
                    step = _trace(name, failed, step_started, datetime.now(UTC))
                    step.error_category, step.error_type = classify(exc), type(exc).__name__
                    trace.steps.append(step)
                    await self._fail(result, ref, name, step.error_category)
                    return result
                trace.steps.append(_trace(name, outcome, step_started, datetime.now(UTC)))
        if "plan" in ordered and not await self._plan(result):
            return result
        for ref in sorted(courses):
            result.traces[ref].current_step = None
            await self._finish(result.runs[ref], result.traces[ref], WorkflowRunStatus.SUCCEEDED)
            result.statuses[ref] = WorkflowRunStatus.SUCCEEDED
        return result

    async def _plan(self, result: WorkflowResult) -> bool:
        """The cross-course plan, saved once; False if it failed."""
        from app.planning.store import compute_plan, save_plan

        for ref in sorted(result.courses):
            result.traces[ref].current_step = "plan"
            await self._save(result.runs[ref], result.traces[ref])
        started = datetime.now(UTC)
        try:
            async with self._sessions() as session, session.begin():
                tasks = await compute_plan(session, self._replay.as_of)
                first = result.runs[min(result.courses)]
                plan_id = await save_plan(session, tasks, self._replay.as_of, first)
            statuses = [t.status.value for t in tasks]
            outcome = StepOutcome(WorkflowRunStatus.SUCCEEDED, (), {
                "plan_id": str(plan_id), "as_of": self._replay.as_of.isoformat(),
                "tasks": len(tasks), "blocked": statuses.count("blocked"),
                "completed": statuses.count("completed"),
            })  # fmt: skip
        except Exception as exc:  # noqa: BLE001 - classified, recorded, then stop
            step = _trace("plan", StepOutcome(WorkflowRunStatus.FAILED), started, datetime.now(UTC))
            step.error_category, step.error_type = classify(exc), type(exc).__name__
            for ref in sorted(result.courses):
                result.traces[ref].steps.append(step)
            await self._fail(result, "", "plan", step.error_category)
            return False
        for ref in sorted(result.courses):
            result.traces[ref].steps.append(_trace("plan", outcome, started, datetime.now(UTC)))
        return True

    async def _demo_courses(self) -> dict[str, uuid.UUID]:
        async with self._sessions() as session:
            rows = await session.execute(
                select(Course.external_id, Course.id)
                .join(Institution, Institution.id == Course.institution_id)
                .where(Institution.lms_type == LmsType.DEMO)
            )
            return {ref: course_id for ref, course_id in rows if ref is not None}

    async def _start(
        self, course_id: uuid.UUID, trace: WorkflowTrace, started: datetime
    ) -> uuid.UUID:
        async with self._sessions() as session, session.begin():
            run = WorkflowRun(
                workflow_version=WORKFLOW_VERSION,
                course_id=course_id,
                status=WorkflowRunStatus.RUNNING,
                started_at=started,
                summary=trace.model_dump(mode="json"),
            )
            session.add(run)
            await session.flush()
            return run.id

    async def _save(self, run_id: uuid.UUID, trace: WorkflowTrace) -> None:
        async with self._sessions() as session, session.begin():
            run = await session.get(WorkflowRun, run_id)
            if run is not None:
                run.summary = trace.model_dump(mode="json")

    async def _finish(
        self, run_id: uuid.UUID, trace: WorkflowTrace, status: WorkflowRunStatus
    ) -> None:
        async with self._sessions() as session, session.begin():
            run = await session.get(WorkflowRun, run_id)
            if run is not None:
                run.status = status
                run.finished_at = datetime.now(UTC)
                run.summary = trace.model_dump(mode="json")

    async def _fail(
        self, result: WorkflowResult, failed_ref: str, step: str, category: str
    ) -> None:
        for ref in sorted(result.courses):
            trace = result.traces[ref]
            trace.failed_step = step
            trace.error_category = category if ref == failed_ref or not failed_ref else "aborted"
            await self._finish(result.runs[ref], trace, WorkflowRunStatus.FAILED)
            result.statuses[ref] = WorkflowRunStatus.FAILED


def _trace(name: str, outcome: StepOutcome, started: datetime, finished: datetime) -> StepTrace:
    return StepTrace(
        name=name,
        status="succeeded" if outcome.status is WorkflowRunStatus.SUCCEEDED else "failed",
        started_at=started,
        finished_at=finished,
        child_run_ids=list(outcome.child_run_ids),
        counts=outcome.counts,
    )
