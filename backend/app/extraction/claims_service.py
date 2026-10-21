# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Extract a course's assessment requirements or its obligations, with
evidence: two separate tasks with their own prompts, gates and runs, so
that a failure in one is diagnosable on its own.

- requirements (``requirement-extraction/v1``): the current versions of
  the course's briefs;
- obligations (``obligation-extraction/v1``): every current version, since
  a hidden obligation can be stated anywhere (handbooks, guidance,
  announcements, briefs).

Both read the course's existing assessments (local IDs A1, ...), so they run
after assessment extraction. Per unit: one recorded structured call (a
ModelCall in the run; no tools), the deterministic gate, and persistence
in its own transaction; no transaction is open during a model call. A unit
whose call fails is counted and the run continues; the run ends failed if
any unit failed. Logs and the summary hold IDs and counts only.
"""

from __future__ import annotations

import logging
import uuid
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, fields
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai.model_calls import RecordedCallError, call_structured_recorded
from app.ai.prompts import load_prompt
from app.ai.provider import AIProvider
from app.db.models import Course, Institution, Resource, WorkflowRun, workflow_run_inputs
from app.domain.enums import ModelRole, ResourceType, WorkflowRunStatus
from app.extraction import obligations, requirements
from app.extraction.claim_units import ClaimUnit, build_claim_messages, load_claim_units
from app.extraction.claims_persist import ClaimCounts, persist_obligations, persist_requirements
from app.extraction.persist import Lineage
from app.extraction.service import add_tokens, usable_zone
from app.retrieval import store

logger = logging.getLogger(__name__)

Task = Literal["requirements", "obligations"]


@dataclass(frozen=True, slots=True)
class TaskSpec:
    workflow_version: str
    prompt_version: str
    gate_version: str
    schema: type[BaseModel]
    # Resource types read; None: every type.
    resource_types: frozenset[ResourceType] | None


TASKS: dict[Task, TaskSpec] = {
    "requirements": TaskSpec(
        "requirement-extraction/v1",
        "requirement-extraction/v1",
        requirements.GATE_VERSION,
        requirements.RequirementExtraction,
        frozenset({ResourceType.BRIEF}),
    ),
    "obligations": TaskSpec(
        "obligation-extraction/v1",
        "obligation-extraction/v1",
        obligations.GATE_VERSION,
        obligations.ObligationExtraction,
        None,
    ),
}


class ClaimExtractionSummary(BaseModel):
    """The ``summary`` of a requirement- or obligation-extraction/v1 run."""

    model_config = ConfigDict(extra="forbid")

    workflow_version: Literal["requirement-extraction/v1", "obligation-extraction/v1"]
    prompt_version: str
    gate_version: str
    model_role: str
    thinking: bool | None = None
    versions: int = 0
    units: int = 0
    units_failed: int = 0
    proposed: int = 0
    created: int = 0
    merged: int = 0
    conflicts: int = 0
    links_created: int = 0
    links_existing: int = 0
    prompt_tokens: int | None = 0
    completion_tokens: int | None = 0
    # Stable reason codes -> counts: never text.
    rejected: dict[str, int] = {}
    abstained: dict[str, int] = {}
    review_reasons: dict[str, int] = {}
    failures: dict[str, int] = {}


@dataclass(frozen=True, slots=True)
class ClaimRunResult:
    run_id: uuid.UUID
    status: WorkflowRunStatus
    summary: ClaimExtractionSummary


class ClaimExtractionService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        provider: AIProvider,
        task: Task,
        *,
        role: ModelRole = ModelRole.FAST,
        temperature: float | None = 0.0,
        thinking: bool | None = None,
        prompt_version: str | None = None,
    ) -> None:
        """``prompt_version`` overrides the task's default prompt (an evaluated
        variant of the same task, e.g. ``obligation-extraction/v2``)."""
        self._sessions = session_factory
        self._provider = provider
        self._spec = TASKS[task]
        self._prompt = load_prompt(prompt_version or self._spec.prompt_version)
        if not self._prompt.version.startswith(self._spec.prompt_version.rsplit("/", 1)[0]):
            raise ValueError(f"{self._prompt.version} is not a {task} prompt")
        self._role = role
        self._temperature = temperature
        self._thinking = thinking

    async def extract_course(
        self, course_id: uuid.UUID, version_ids: Sequence[uuid.UUID] | None = None
    ) -> ClaimRunResult:
        run_id, versions, zone = await self._start(course_id, version_ids)
        summary = ClaimExtractionSummary(
            workflow_version=self._spec.workflow_version,  # type: ignore[arg-type]
            prompt_version=self._prompt.version,
            gate_version=self._spec.gate_version,
            model_role=self._role.value,
            thinking=self._thinking,
            versions=len(versions),
        )
        counts = ClaimCounts()
        rejected: Counter[str] = Counter()
        abstained: Counter[str] = Counter()
        reasons: Counter[str] = Counter()
        failures: Counter[str] = Counter()
        try:
            for version_id in versions:
                async with self._sessions() as session:
                    units = await load_claim_units(session, course_id, version_id)
                for claim_unit in units:
                    summary.units += 1
                    outcome = await self._unit(claim_unit, run_id, zone, summary, failures)
                    if outcome is None:
                        continue
                    unit_counts, unit_rejected, unit_abstained, unit_reasons = outcome
                    counts.add(unit_counts)
                    rejected.update(unit_rejected)
                    abstained.update(unit_abstained)
                    reasons.update(unit_reasons)
        except BaseException:
            await self._finish(run_id, WorkflowRunStatus.FAILED, summary)
            raise
        for item in fields(ClaimCounts):
            setattr(summary, item.name, getattr(counts, item.name))
        summary.rejected, summary.abstained = dict(rejected), dict(abstained)
        summary.review_reasons, summary.failures = dict(reasons), dict(failures)
        summary.units_failed = sum(failures.values())
        status = WorkflowRunStatus.FAILED if summary.units_failed else WorkflowRunStatus.SUCCEEDED
        await self._finish(run_id, status, summary)
        return ClaimRunResult(run_id, status, summary)

    async def _start(
        self, course_id: uuid.UUID, version_ids: Sequence[uuid.UUID] | None
    ) -> tuple[uuid.UUID, list[uuid.UUID], str | None]:
        async with self._sessions() as session, session.begin():
            zone = await session.scalar(
                select(Institution.timezone)
                .join(Course, Course.institution_id == Institution.id)
                .where(Course.id == course_id)
            )
            current = await store.current_versions(session, course_id)
            if not current and not await session.get(Course, course_id):
                raise LookupError(f"course {course_id} does not exist")
            outside = set(version_ids or ()) - set(current)
            if outside:
                raise LookupError(f"{len(outside)} version(s) are not current in the course")
            in_scope = set(current)
            if self._spec.resource_types is not None:
                in_scope = set(
                    (
                        await session.scalars(
                            select(Resource.current_version_id).where(
                                Resource.course_id == course_id,
                                Resource.type.in_(self._spec.resource_types),
                            )
                        )
                    ).all()
                )
            requested = current if version_ids is None else list(version_ids)
            versions = [v for v in requested if v in in_scope]
            run = WorkflowRun(
                workflow_version=self._spec.workflow_version,
                course_id=course_id,
                status=WorkflowRunStatus.RUNNING,
                started_at=datetime.now(UTC),
            )
            session.add(run)
            await session.flush()
            if versions:
                await session.execute(
                    insert(workflow_run_inputs),
                    [{"run_id": run.id, "resource_version_id": v} for v in versions],
                )
        return run.id, versions, usable_zone(zone, course_id)

    async def _unit(
        self,
        claim_unit: ClaimUnit,
        run_id: uuid.UUID,
        zone: str | None,
        summary: ClaimExtractionSummary,
        failures: Counter[str],
    ) -> tuple[ClaimCounts, Counter[str], Counter[str], Counter[str]] | None:
        unit = claim_unit.unit
        try:
            recorded = await call_structured_recorded(
                self._provider,
                self._sessions,
                messages=build_claim_messages(self._prompt, claim_unit),
                schema=self._spec.schema,
                role=self._role,
                prompt_version=self._prompt.version,
                workflow_run_id=run_id,
                temperature=self._temperature,
                thinking=self._thinking,
            )
        except RecordedCallError as failed:
            failures[failed.error.category.value] += 1
            summary.prompt_tokens = add_tokens(
                summary.prompt_tokens, failed.error.usage.prompt_tokens
            )
            logger.warning(
                "claim_extraction.unit_failed",
                extra={
                    "run_id": str(run_id),
                    "resource_version_id": str(unit.resource_version_id),
                    "unit": unit.ordinal,
                    "category": failed.error.category.value,
                },
            )
            return None
        usage = recorded.result.usage
        summary.prompt_tokens = add_tokens(summary.prompt_tokens, usage.prompt_tokens)
        summary.completion_tokens = add_tokens(summary.completion_tokens, usage.completion_tokens)
        lineage = Lineage(model_call_id=recorded.model_call_id, workflow_run_id=run_id)
        output = recorded.result.output
        abstained: Counter[str] = Counter()
        async with self._sessions() as session, session.begin():
            if isinstance(output, requirements.RequirementExtraction):
                summary.proposed += len(output.requirements)
                gated_r = requirements.gate_requirements(claim_unit, output)
                counts = await persist_requirements(session, gated_r.requirements, lineage)
                rejected, reasons = gated_r.rejected, gated_r.review_reasons
            elif isinstance(output, obligations.ObligationExtraction):
                summary.proposed += len(output.obligations)
                gated_o = obligations.gate_obligations(claim_unit, output, zone)
                counts = await persist_obligations(
                    session, unit.course_id, gated_o.obligations, lineage
                )
                rejected, reasons = gated_o.rejected, gated_o.review_reasons
                abstained = gated_o.abstained
            else:  # the schema is fixed by the task
                raise TypeError(f"unexpected output type {type(output).__name__}")
        return counts, rejected, abstained, reasons

    async def _finish(
        self, run_id: uuid.UUID, status: WorkflowRunStatus, summary: ClaimExtractionSummary
    ) -> None:
        async with self._sessions() as session, session.begin():
            run = await session.get(WorkflowRun, run_id)
            if run is not None:
                run.status = status
                run.finished_at = datetime.now(UTC)
                run.summary = summary.model_dump(mode="json")
        logger.info(
            "claim_extraction.run_finished",
            extra={"run_id": str(run_id), "status": status.value, "units": summary.units},
        )
