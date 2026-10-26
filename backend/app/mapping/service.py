# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Map a course's requirements to its teaching material
(``material-mapping/v1``): retrieve, verify, persist.

Per requirement, in its own transaction for the write and with none open
during a model call:

1. Query (``mapping-query/v1``): the requirement's text, then its
   assessment's and course's titles.
2. Retrieval: the course's search in the default mode, top ``k`` chunks of
   current teaching resources only (``TEACHING_RESOURCE_TYPES``).
3. Candidates: the retrieved spans, IDs C1..Cn (``candidates``).
4. Verifier: one recorded structured call; it answers candidate IDs or
   ``none`` (``selection``). The gate keeps issued IDs only.
5. Persistence: the requirement's mappings become the selection.

No candidates: no call, and the requirement keeps no mapping. A failed
retrieval or verifier call is counted, leaves that requirement's mappings
as they were, and the run continues; the run ends failed if any
requirement failed. Logs and the summary hold IDs and counts only.
"""

from __future__ import annotations

import logging
import secrets
import uuid
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict
from sqlalchemy import delete, insert, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai.embeddings import QUERY_V1_MAX_CHARS
from app.ai.model_calls import RecordedCallError, call_structured_recorded
from app.ai.prompts import PromptFiles, load_prompt
from app.ai.provider import AIProvider, ChatMessage
from app.db.models import (
    Assessment,
    AssessmentRequirement,
    Course,
    MaterialMapping,
    Resource,
    WorkflowRun,
    workflow_run_inputs,
)
from app.domain.enums import ModelRole, WorkflowRunStatus
from app.extraction.service import add_tokens
from app.extraction.units import safe_title
from app.mapping.candidates import (
    CANDIDATES_VERSION,
    TEACHING_RESOURCE_TYPES,
    Candidate,
    RetrievalContractError,
    build_candidates,
)
from app.mapping.persist import MappingCounts, MappingLineage, replace_mappings
from app.mapping.selection import MappingSelection, gate_selection
from app.retrieval.search import (
    DEFAULT_SEARCH_MODE,
    IndexStateError,
    ProvenanceError,
    QueryCallNotRecordedError,
    SearchMode,
    SearchProviderError,
    SearchRequest,
    SearchService,
)

logger = logging.getLogger(__name__)

WORKFLOW_VERSION = "material-mapping/v1"
PROMPT_VERSION = "material-mapping/v1"
QUERY_FORMAT = "mapping-query/v1"
DEFAULT_K = 5
CANDIDATE_TEXT_MAX = 700  # what the verifier sees of one candidate; evidence is unaffected


def mapping_query(requirement: str, assessment: str, course: str) -> str:
    query = f"Requirement: {requirement}\nAssessment: {assessment}\nCourse: {course}"
    return query[:QUERY_V1_MAX_CHARS]


def render_candidates(candidates: Sequence[Candidate]) -> str:
    def text(c: Candidate) -> str:
        return c.text if len(c.text) <= CANDIDATE_TEXT_MAX else c.text[:CANDIDATE_TEXT_MAX] + " …"

    return "\n\n".join(
        f"[{c.local_id}] {c.locator} — {safe_title(c.resource_title)}\n{text(c)}"
        for c in candidates
    )


@dataclass(frozen=True, slots=True)
class MappingTarget:
    requirement_id: uuid.UUID
    requirement: str
    assessment: str
    course: str


async def mapping_targets(session: AsyncSession, course: Course) -> list[MappingTarget]:
    """Every stored requirement of the course, in a fixed order."""
    rows = (
        await session.execute(
            select(AssessmentRequirement.id, AssessmentRequirement.text, Assessment.title)
            .join(Assessment, Assessment.id == AssessmentRequirement.assessment_id)
            .where(Assessment.course_id == course.id)
            .order_by(Assessment.canonical_key, AssessmentRequirement.text)
        )
    ).all()
    return [MappingTarget(rid, text, title, course.title) for rid, text, title in rows]


def build_mapping_messages(
    prompt: PromptFiles,
    target: MappingTarget,
    candidates: Sequence[Candidate],
    *,
    nonce: str | None = None,
) -> list[ChatMessage]:
    rendered = render_candidates(candidates)
    texts = (rendered, target.requirement, target.assessment, target.course)
    while nonce is None:
        nonce = secrets.token_hex(8)
        nonce = None if any(nonce in t for t in texts) else nonce
    if any(nonce in t for t in texts):
        raise ValueError("the nonce occurs in the material")
    user = prompt.user.substitute(
        nonce=nonce,
        course_title=safe_title(target.course),
        assessment_title=safe_title(target.assessment),
        requirement=" ".join(target.requirement.split()),
        candidates=rendered,
    )
    return [ChatMessage("system", prompt.system), ChatMessage("user", user)]


class MappingSummary(BaseModel):
    """The ``summary`` of a material-mapping/v1 run: counts and codes only."""

    model_config = ConfigDict(extra="forbid")

    workflow_version: Literal["material-mapping/v1"] = WORKFLOW_VERSION
    prompt_version: str
    candidates_version: str = CANDIDATES_VERSION
    query_format: str = QUERY_FORMAT
    retrieval_mode: str
    k: int
    model_role: str
    thinking: bool | None = None
    requirements: int = 0
    requirements_failed: int = 0
    no_candidates: int = 0
    verified_none: int = 0
    mapped_requirements: int = 0
    candidates: int = 0
    selected: int = 0
    mappings_created: int = 0
    mappings_kept: int = 0
    mappings_removed: int = 0
    prompt_tokens: int | None = 0
    completion_tokens: int | None = 0
    rejected: dict[str, int] = {}
    failures: dict[str, int] = {}


@dataclass(frozen=True, slots=True)
class RequirementOutcome:
    """One requirement's result, for callers and evaluation (IDs only)."""

    requirement_id: uuid.UUID
    candidate_span_ids: tuple[uuid.UUID, ...]
    selected_span_ids: tuple[uuid.UUID, ...]
    failure: str | None = None


@dataclass(frozen=True, slots=True)
class MappingRunResult:
    run_id: uuid.UUID
    status: WorkflowRunStatus
    summary: MappingSummary
    outcomes: tuple[RequirementOutcome, ...]


class MaterialMappingService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        search: SearchService,
        provider: AIProvider,
        *,
        k: int = DEFAULT_K,
        mode: SearchMode = DEFAULT_SEARCH_MODE,
        role: ModelRole = ModelRole.FAST,
        temperature: float | None = 0.0,
        thinking: bool | None = None,
    ) -> None:
        self._sessions = session_factory
        self._search = search
        self._provider = provider
        self._k = k
        self._mode = mode
        self._role = role
        self._temperature = temperature
        self._thinking = thinking
        self._prompt = load_prompt(PROMPT_VERSION)

    async def map_course(self, course_id: uuid.UUID) -> MappingRunResult:
        run_id, targets = await self._start(course_id)
        summary = MappingSummary(
            prompt_version=self._prompt.version,
            retrieval_mode=self._mode.value,
            k=self._k,
            model_role=self._role.value,
            thinking=self._thinking,
            requirements=len(targets),
        )
        rejected: Counter[str] = Counter()
        failures: Counter[str] = Counter()
        outcomes: list[RequirementOutcome] = []
        try:
            for target in targets:
                outcome = await self._map(course_id, run_id, target, summary, rejected, failures)
                outcomes.append(outcome)
        except BaseException:
            await self._finish(run_id, WorkflowRunStatus.FAILED, summary)
            raise
        summary.rejected, summary.failures = dict(rejected), dict(failures)
        summary.requirements_failed = sum(failures.values())
        status = (
            WorkflowRunStatus.FAILED if summary.requirements_failed else WorkflowRunStatus.SUCCEEDED
        )
        await self._finish(run_id, status, summary)
        return MappingRunResult(run_id, status, summary, tuple(outcomes))

    async def _start(self, course_id: uuid.UUID) -> tuple[uuid.UUID, list[MappingTarget]]:
        async with self._sessions() as session, session.begin():
            course = await session.get(Course, course_id)
            if course is None:
                raise LookupError(f"course {course_id} does not exist")
            targets = await mapping_targets(session, course)
            versions = (
                await session.scalars(
                    select(Resource.current_version_id).where(
                        Resource.course_id == course_id,
                        Resource.type.in_(TEACHING_RESOURCE_TYPES),
                        Resource.current_version_id.is_not(None),
                    )
                )
            ).all()
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
        return run.id, targets

    async def _map(
        self,
        course_id: uuid.UUID,
        run_id: uuid.UUID,
        target: MappingTarget,
        summary: MappingSummary,
        rejected: Counter[str],
        failures: Counter[str],
    ) -> RequirementOutcome:
        def failed(code: str) -> RequirementOutcome:
            failures[code] += 1
            logger.warning(
                "material_mapping.requirement_failed",
                extra={"run_id": str(run_id), "requirement_id": str(target.requirement_id),
                       "failure": code},
            )  # fmt: skip
            return RequirementOutcome(target.requirement_id, (), (), failure=code)

        try:
            result = await self._search.search_course(
                SearchRequest.build(
                    course_id=course_id,
                    query=mapping_query(target.requirement, target.assessment, target.course),
                    k=self._k,
                    resource_types=TEACHING_RESOURCE_TYPES,
                    mode=self._mode,
                )
            )
            candidates = build_candidates(result.hits)
        except SearchProviderError as exc:
            return failed(f"retrieval:{exc.category.value}")
        except (
            IndexStateError,
            ProvenanceError,
            QueryCallNotRecordedError,
            RetrievalContractError,
        ) as exc:
            return failed(f"retrieval:{type(exc).__name__}")
        summary.candidates += len(candidates)
        span_ids = tuple(c.span_id for c in candidates)
        if not candidates:
            summary.no_candidates += 1
            async with self._sessions() as session, session.begin():
                await session.execute(
                    delete(MaterialMapping).where(
                        MaterialMapping.requirement_id == target.requirement_id
                    )
                )
            return RequirementOutcome(target.requirement_id, (), ())
        try:
            recorded = await call_structured_recorded(
                self._provider,
                self._sessions,
                messages=build_mapping_messages(self._prompt, target, candidates),
                schema=MappingSelection,
                role=self._role,
                prompt_version=self._prompt.version,
                workflow_run_id=run_id,
                temperature=self._temperature,
                thinking=self._thinking,
            )
        except RecordedCallError as exc:
            summary.prompt_tokens = add_tokens(summary.prompt_tokens, exc.error.usage.prompt_tokens)
            return failed(f"verifier:{exc.error.category.value}")
        usage = recorded.result.usage
        summary.prompt_tokens = add_tokens(summary.prompt_tokens, usage.prompt_tokens)
        summary.completion_tokens = add_tokens(summary.completion_tokens, usage.completion_tokens)
        gated = gate_selection(recorded.result.output, candidates)
        rejected.update(reason.value for _, reason in gated.rejected)
        lineage = MappingLineage(
            retrieval_mode=result.mode.value,
            query_model_call_id=result.query_model_call_id,
            verifier_model_call_id=recorded.model_call_id,
            workflow_run_id=run_id,
        )
        async with self._sessions() as session, session.begin():
            counts: MappingCounts = await replace_mappings(
                session, target.requirement_id, gated.selected, lineage
            )
        summary.selected += len(gated.selected)
        summary.mappings_created += counts.created
        summary.mappings_kept += counts.kept
        summary.mappings_removed += counts.removed
        if gated.is_none:
            summary.verified_none += 1
        else:
            summary.mapped_requirements += 1
        return RequirementOutcome(
            target.requirement_id, span_ids, tuple(c.span_id for c in gated.selected)
        )

    async def _finish(
        self, run_id: uuid.UUID, status: WorkflowRunStatus, summary: MappingSummary
    ) -> None:
        async with self._sessions() as session, session.begin():
            run = await session.get(WorkflowRun, run_id)
            if run is not None:
                run.status = status
                run.finished_at = datetime.now(UTC)
                run.summary = summary.model_dump(mode="json")
        logger.info(
            "material_mapping.run_finished",
            extra={"run_id": str(run_id), "status": status.value,
                   "requirements": summary.requirements},
        )  # fmt: skip
