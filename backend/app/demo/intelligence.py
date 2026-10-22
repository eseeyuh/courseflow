# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Rebuild the Northbridge demo intelligence offline, from the committed
recordings (``recordings``) only: no network, no spend, the same result
every time.

``DemoReplay`` holds the replay providers and runs each model-backed step
of the course-intelligence workflow through the production services:
ingest (seed and index from the document embedding snapshot), assessments,
requirements and obligations (recorded FAST outputs through the current
gates), and material mapping (recorded query vectors and verifier
outputs). Dependencies, conflicts and validation need no model; the
workflow (``app.orchestration.workflow``) runs them directly.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai.embeddings import EmbeddingInput, EmbeddingSpace, query_input
from app.db.models import Course
from app.demo import recordings
from app.domain.enums import WorkflowRunStatus
from app.ingestion.storage import RawObjectStore
from app.mapping.service import MappingRunResult, mapping_query, mapping_targets
from app.retrieval.search import SearchRequest, SearchService


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


@dataclass(frozen=True, slots=True)
class StepOutcome:
    """What one step did for one course: its child runs and counts."""

    status: WorkflowRunStatus
    child_run_ids: tuple[uuid.UUID, ...] = ()
    counts: dict[str, Any] = field(default_factory=dict)


async def query_inputs(
    sessions: async_sessionmaker[AsyncSession], courses: dict[str, uuid.UUID]
) -> tuple[EmbeddingInput, ...]:
    """Exactly the query/v1 inputs a mapping run of these courses embeds."""
    found: dict[str, EmbeddingInput] = {}
    async with sessions() as session:
        for ref in sorted(courses):
            course = await session.get(Course, courses[ref])
            assert course is not None
            for target in await mapping_targets(session, course):
                query = SearchRequest.build(
                    course_id=course.id,
                    query=mapping_query(target.requirement, target.assessment, target.course),
                ).query
                item = query_input(query)
                found.setdefault(item.text, item)
    return tuple(found.values())


def mapping_search(
    sessions: async_sessionmaker[AsyncSession], space: EmbeddingSpace, inputs: Any
) -> SearchService:
    """The production search over the recorded mapping query vectors."""
    from app.demo.snapshot import snapshot_subject
    from app.evaluation.northbridge.labels import load_golden
    from app.evaluation.retrieval.replay import load_replay_provider
    from app.evaluation.retrieval.snapshot import ExpectedInputs

    replay = load_replay_provider(
        recordings.MAPPING_QUERY_EMBEDDINGS,
        dataset=snapshot_subject(load_golden()),
        active_space=space,
        max_batch_items=64,
        inputs=ExpectedInputs((), inputs),
    )
    return SearchService(sessions, space, replay)


class DemoReplay:
    """The replay implementation of the model-backed workflow steps."""

    mode = "replay"

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        store: RawObjectStore,
        space: EmbeddingSpace,
    ) -> None:
        from app.evaluation.claims.run import ClaimReplayProvider, recorded_by_material
        from app.mapping.replay import MappingReplayProvider

        self.sessions, self._store, self._space = sessions, store, space
        self._claims = ClaimReplayProvider(recorded_by_material(_load(recordings.CLAIM_OUTPUTS)))
        self._verifier = MappingReplayProvider.from_recorded(_load(recordings.MAPPING_OUTPUTS))
        self._search: SearchService | None = None
        self.mapping_results: list[MappingRunResult] = []

    @property
    def as_of(self) -> datetime:
        """The demo clock: the connector's initial sync, never the wall clock."""
        from app.connectors.demo import DemoConnector
        from app.demo.generate import DATA_ROOT

        return DemoConnector(DATA_ROOT).manifest.initial_sync_at

    async def ingest(self, *, reset: bool) -> tuple[dict[str, uuid.UUID], dict[str, StepOutcome]]:
        """Seed and index every demo course: (course ref -> ID, outcome)."""
        from app.demo.seed import seed
        from app.demo.snapshot import load_demo_replay
        from app.evaluation.retrieval.resolve import production_limits
        from app.retrieval.indexing import IndexingService

        indexer = IndexingService(
            self.sessions,
            load_demo_replay(self._space, max_batch_items=64, path=recordings.DOCUMENT_EMBEDDINGS),
            batch_max_items=64,
            batch_max_estimated_tokens=32_000,
        )
        summary = await seed(
            self.sessions, self._store, limits=production_limits(), indexer=indexer, reset=reset
        )
        courses, outcomes = {}, {}
        for entry in summary["courses"]:
            courses[entry["course"]] = uuid.UUID(entry["course_id"])
            index = entry["initial_index"]
            outcomes[entry["course"]] = StepOutcome(
                WorkflowRunStatus.SUCCEEDED,
                (uuid.UUID(index["run_id"]),),
                {"versions_created": entry["initial_versions_created"],
                 "versions_indexed": index["versions_indexed"],
                 "chunks_created": index["chunks_created"]},
            )  # fmt: skip
        self._search = None  # the requirements, so the mapping queries, may change
        return courses, outcomes

    async def assessments(self, course_id: uuid.UUID) -> StepOutcome:
        from app.evaluation.assessments import run as assessments_run
        from app.evaluation.assessments.run import EvalConfig, ReplayProvider, assign_outputs
        from app.evaluation.northbridge.labels import load_golden
        from app.extraction.service import AssessmentExtractionService

        plan = assessments_run.plan_units(load_golden(), EvalConfig(recordings.ASSESSMENT_PROMPT))
        provider = ReplayProvider(
            assign_outputs(_load(recordings.ASSESSMENT_OUTPUTS), plan), "replay:recorded-fast"
        )
        service = AssessmentExtractionService(
            self.sessions, provider, thinking=False, prompt_version=recordings.ASSESSMENT_PROMPT
        )
        result = await service.extract_course(course_id)
        s = result.summary
        return StepOutcome(result.status, (result.run_id,),
                           {"created": s.assessments_created, "merged": s.assessments_merged,
                            "units_failed": s.units_failed})  # fmt: skip

    async def claims(self, course_id: uuid.UUID) -> StepOutcome:
        from app.extraction.claims_service import ClaimExtractionService

        runs, counts, statuses = [], {}, []
        for task in ("requirements", "obligations"):
            service = ClaimExtractionService(self.sessions, self._claims, task, thinking=False)
            result = await service.extract_course(course_id)
            runs.append(result.run_id)
            statuses.append(result.status)
            counts[task] = {"created": result.summary.created, "merged": result.summary.merged,
                            "units_failed": result.summary.units_failed}  # fmt: skip
        failed = WorkflowRunStatus.FAILED in statuses
        return StepOutcome(
            WorkflowRunStatus.FAILED if failed else WorkflowRunStatus.SUCCEEDED, tuple(runs), counts
        )

    async def mapping(self, course_id: uuid.UUID, courses: dict[str, uuid.UUID]) -> StepOutcome:
        """Map one course; the recorded query vectors are checked against
        every course's queries, so all requirements must exist first."""
        from app.mapping.service import MaterialMappingService

        if self._search is None:
            inputs = await query_inputs(self.sessions, courses)
            self._search = mapping_search(self.sessions, self._space, inputs)
        service = MaterialMappingService(
            self.sessions, self._search, self._verifier, thinking=False
        )
        result = await service.map_course(course_id)
        self.mapping_results.append(result)
        s = result.summary
        return StepOutcome(result.status, (result.run_id,),
                           {"requirements": s.requirements, "mapped": s.mapped_requirements,
                            "none": s.verified_none, "created": s.mappings_created,
                            "kept": s.mappings_kept, "removed": s.mappings_removed,
                            "rejected": s.rejected, "failed": s.requirements_failed})  # fmt: skip


@dataclass(slots=True)
class DemoBuild:
    courses: dict[str, uuid.UUID]  # course external ID -> course ID
    runs: dict[str, uuid.UUID] = field(default_factory=dict)  # course -> workflow run
    mapping: list[MappingRunResult] = field(default_factory=list)


async def build_extractions(
    sessions: async_sessionmaker[AsyncSession], store: RawObjectStore, space: EmbeddingSpace
) -> DemoBuild:
    """Seed, index, and extract assessments, requirements and obligations."""
    replay = DemoReplay(sessions, store, space)
    courses, _ = await replay.ingest(reset=False)
    for ref in sorted(courses):
        await replay.assessments(courses[ref])
    for ref in sorted(courses):
        await replay.claims(courses[ref])
    return DemoBuild(courses)


async def build_demo_intelligence(
    sessions: async_sessionmaker[AsyncSession],
    store: RawObjectStore,
    space: EmbeddingSpace,
    *,
    reset: bool = False,
) -> DemoBuild:
    """The initial Northbridge state with every demo claim: one full
    course-intelligence workflow in replay mode. RuntimeError if it fails."""
    from app.orchestration.workflow import CourseIntelligenceWorkflow

    replay = DemoReplay(sessions, store, space)
    result = await CourseIntelligenceWorkflow(sessions, replay).run(reset=reset)
    if not result.succeeded:
        raise RuntimeError(f"demo workflow failed: {result.status_by_course()}")
    return DemoBuild(result.courses, result.runs, replay.mapping_results)
