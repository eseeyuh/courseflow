# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Extract a course's assessments and deadlines with evidence.

One WorkflowRun (``assessment-extraction/v1``) per call, whose inputs are the
versions processed (by default the course's current versions). For each
version, each unit is one recorded structured call (a ModelCall in the run;
no tools), gated deterministically, and persisted in its own transaction.
No transaction is open during a model call.

A unit whose call fails is counted and the run continues; the run ends
failed if any unit failed. Logs and the summary hold IDs and counts only,
never document text.
"""

from __future__ import annotations

import logging
import uuid
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, fields
from datetime import UTC, datetime
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai.model_calls import RecordedCallError, call_structured_recorded
from app.ai.prompts import load_prompt
from app.ai.provider import AIProvider
from app.db.models import Course, Institution, WorkflowRun, workflow_run_inputs
from app.domain.enums import ModelRole, WorkflowRunStatus
from app.extraction.gate import GATE_VERSION, GateResult, gate
from app.extraction.persist import Lineage, PersistCounts, persist_unit
from app.extraction.schema import AssessmentExtraction
from app.extraction.units import ExtractionUnit, build_messages, load_units
from app.retrieval import store

logger = logging.getLogger(__name__)

WORKFLOW_VERSION = "assessment-extraction/v1"
# v2: the resource metadata (type, title) is inside the untrusted-material
# markers, like the spans. v1 stays loadable for its measured reports.
PROMPT_VERSION = "assessment-extraction/v2"


class ExtractionSummary(BaseModel):
    """The ``summary`` of a WorkflowRun with workflow_version "assessment-extraction/v1"."""

    model_config = ConfigDict(extra="forbid")

    workflow_version: Literal["assessment-extraction/v1"] = WORKFLOW_VERSION
    prompt_version: str
    gate_version: str = GATE_VERSION
    model_role: str
    thinking: bool | None = None  # None: the model's default
    versions: int = 0
    units: int = 0
    units_failed: int = 0
    assessments_proposed: int = 0
    assessments_created: int = 0
    assessments_merged: int = 0
    deadlines_created: int = 0
    deadlines_merged: int = 0
    links_created: int = 0
    links_existing: int = 0
    weighting_disagreements: int = 0
    prompt_tokens: int | None = 0
    completion_tokens: int | None = 0
    # Stable reason codes -> counts: never text.
    rejected: dict[str, int] = {}
    abstained: dict[str, int] = {}
    review_reasons: dict[str, int] = {}
    failures: dict[str, int] = {}  # model call error category -> units
    # Where persisted candidates' identities are supported ("title",
    # "deadline_heading", ...): see gate.GateResult.identity.
    identity: dict[str, int] = {}


@dataclass(frozen=True, slots=True)
class ExtractionRunResult:
    run_id: uuid.UUID
    status: WorkflowRunStatus
    summary: ExtractionSummary


def _add_tokens(current: int | None, more: int | None) -> int | None:
    return None if current is None or more is None else current + more


def _usable_zone(zone: str | None, course_id: uuid.UUID) -> str | None:
    """The institution's timezone if zoneinfo knows it; otherwise None, so a
    local time without a stated zone needs review (missing_timezone)."""
    if zone is None:
        return None
    try:
        ZoneInfo(zone)
    except (ZoneInfoNotFoundError, ValueError):
        logger.warning(
            "extraction.institution_timezone_unusable", extra={"course_id": str(course_id)}
        )
        return None
    return zone


class AssessmentExtractionService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        provider: AIProvider,
        *,
        role: ModelRole = ModelRole.FAST,
        prompt_version: str = PROMPT_VERSION,
        temperature: float | None = 0.0,
        thinking: bool | None = None,
    ) -> None:
        self._sessions = session_factory
        self._provider = provider
        self._role = role
        self._prompt = load_prompt(prompt_version)
        self._temperature = temperature
        self._thinking = thinking

    async def extract_course(
        self, course_id: uuid.UUID, version_ids: Sequence[uuid.UUID] | None = None
    ) -> ExtractionRunResult:
        run_id, versions, zone = await self._start(course_id, version_ids)
        summary = ExtractionSummary(
            prompt_version=self._prompt.version,
            model_role=self._role.value,
            thinking=self._thinking,
            versions=len(versions),
        )
        counts = PersistCounts()
        rejected: Counter[str] = Counter()
        abstained: Counter[str] = Counter()
        reasons: Counter[str] = Counter()
        identity: Counter[str] = Counter()
        failures: Counter[str] = Counter()
        try:
            for version_id in versions:
                async with self._sessions() as session:
                    units = await load_units(session, course_id, version_id)
                for unit in units:
                    summary.units += 1
                    outcome = await self._unit(unit, run_id, zone, summary, failures)
                    if outcome is None:
                        continue
                    result, unit_counts = outcome
                    counts.add(unit_counts)
                    rejected.update(result.rejected)
                    abstained.update(result.abstained)
                    reasons.update(result.review_reasons)
                    identity.update(result.identity)
        except BaseException:
            await self._finish(run_id, WorkflowRunStatus.FAILED, summary)
            raise
        for item in fields(PersistCounts):
            setattr(summary, item.name, getattr(counts, item.name))
        summary.rejected, summary.abstained = dict(rejected), dict(abstained)
        summary.review_reasons, summary.failures = dict(reasons), dict(failures)
        summary.identity = dict(identity)
        summary.units_failed = sum(failures.values())
        status = WorkflowRunStatus.FAILED if summary.units_failed else WorkflowRunStatus.SUCCEEDED
        await self._finish(run_id, status, summary)
        return ExtractionRunResult(run_id, status, summary)

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
            versions = list(current if version_ids is None else version_ids)
            outside = set(versions) - set(current)
            if outside:
                raise LookupError(f"{len(outside)} version(s) are not current in the course")
            run = WorkflowRun(
                workflow_version=WORKFLOW_VERSION,
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
        return run.id, versions, _usable_zone(zone, course_id)

    async def _unit(
        self,
        unit: ExtractionUnit,
        run_id: uuid.UUID,
        zone: str | None,
        summary: ExtractionSummary,
        failures: Counter[str],
    ) -> tuple[GateResult, PersistCounts] | None:
        try:
            recorded = await call_structured_recorded(
                self._provider,
                self._sessions,
                messages=build_messages(self._prompt, unit),
                schema=AssessmentExtraction,
                role=self._role,
                prompt_version=self._prompt.version,
                workflow_run_id=run_id,
                temperature=self._temperature,
                thinking=self._thinking,
            )
        except RecordedCallError as failed:
            failures[failed.error.category.value] += 1
            summary.prompt_tokens = _add_tokens(
                summary.prompt_tokens, failed.error.usage.prompt_tokens
            )
            logger.warning(
                "extraction.unit_failed",
                extra={
                    "run_id": str(run_id),
                    "resource_version_id": str(unit.resource_version_id),
                    "unit": unit.ordinal,
                    "category": failed.error.category.value,
                },
            )
            return None
        usage = recorded.result.usage
        summary.prompt_tokens = _add_tokens(summary.prompt_tokens, usage.prompt_tokens)
        summary.completion_tokens = _add_tokens(summary.completion_tokens, usage.completion_tokens)
        extraction = recorded.result.output
        summary.assessments_proposed += len(extraction.assessments)
        result = gate(unit, extraction, zone)
        async with self._sessions() as session, session.begin():
            counts = await persist_unit(
                session,
                unit.course_id,
                result.assessments,
                Lineage(model_call_id=recorded.model_call_id, workflow_run_id=run_id),
            )
        return result, counts

    async def _finish(
        self, run_id: uuid.UUID, status: WorkflowRunStatus, summary: ExtractionSummary
    ) -> None:
        async with self._sessions() as session, session.begin():
            run = await session.get(WorkflowRun, run_id)
            if run is not None:
                run.status = status
                run.finished_at = datetime.now(UTC)
                run.summary = summary.model_dump(mode="json")
        logger.info(
            "extraction.run_finished",
            extra={"run_id": str(run_id), "status": status.value, "units": summary.units},
        )
