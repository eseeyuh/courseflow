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

With ``supplement`` (the demo's default) it also runs the supplementary
courses (``supplement``): their own committed vectors, and authored answers
in place of recorded model outputs. The evaluation builds leave them out.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
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
    sessions: async_sessionmaker[AsyncSession],
    space: EmbeddingSpace,
    inputs: Any,
    supplement_inputs: Sequence[EmbeddingInput] = (),
) -> SearchService:
    """The production search over the recorded mapping query vectors: the
    golden courses' (``inputs``) and, if any, the supplementary courses'."""
    from app.demo.snapshot import snapshot_subject
    from app.demo.supplement import CombinedReplay, load_query_replay
    from app.evaluation.northbridge.labels import load_golden
    from app.evaluation.retrieval.replay import load_replay_provider
    from app.evaluation.retrieval.snapshot import ExpectedInputs

    replay: Any = load_replay_provider(
        recordings.MAPPING_QUERY_EMBEDDINGS,
        dataset=snapshot_subject(load_golden()),
        active_space=space,
        max_batch_items=64,
        inputs=ExpectedInputs((), inputs),
    )
    if supplement_inputs:
        replay = CombinedReplay(
            replay, load_query_replay(space, supplement_inputs, max_batch_items=64)
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
        *,
        supplement: bool = True,
    ) -> None:
        from app.demo.supplement import AuthoredProvider
        from app.evaluation.claims.run import ClaimReplayProvider, recorded_by_material
        from app.mapping.replay import MappingReplayProvider

        self.sessions, self._store, self._space = sessions, store, space
        self._supplement = supplement
        self._authored = AuthoredProvider()
        self._claims = ClaimReplayProvider(recorded_by_material(_load(recordings.CLAIM_OUTPUTS)))
        self._verifier = MappingReplayProvider.from_recorded(_load(recordings.MAPPING_OUTPUTS))
        self._search: SearchService | None = None
        self.mapping_results: list[MappingRunResult] = []

    @staticmethod
    def _connector() -> Any:
        from app.connectors.demo import DemoConnector
        from app.demo.generate import DATA_ROOT

        return DemoConnector(DATA_ROOT)

    def _institution(self) -> Any:
        """The demo institution as the LMS shows it, with the supplement if on."""
        from app.connectors.demo import DemoConnector
        from app.demo.generate import DATA_ROOT

        return DemoConnector(DATA_ROOT, supplement=self._supplement)

    async def _is_authored(self, course_id: uuid.UUID) -> bool:
        """Whether a course is supplementary: answered by the authored provider."""
        from app.demo.supplement import supplement_refs

        if not self._supplement:
            return False
        async with self.sessions() as session:
            course = await session.get(Course, course_id)
        return course is not None and course.external_id in supplement_refs()

    @classmethod
    def initial_sync_at(cls) -> datetime:
        return cls._connector().manifest.initial_sync_at  # type: ignore[no-any-return]

    @classmethod
    def final_sync_at(cls) -> datetime:
        """The demo sync time: the last change the connector publishes."""
        manifest = cls._connector().manifest
        return max(
            v.published_at for c in manifest.courses for r in c.resources for v in r.versions
        )

    async def clock(self) -> datetime:
        """The demo clock, from the stored state, never the wall clock: the
        initial sync until a sync has brought changes in, then the sync time."""
        from sqlalchemy import func

        from app.db.models import ChangeEvent

        async with self.sessions() as session:
            synced = await session.scalar(select(func.count()).select_from(ChangeEvent))
        return self.final_sync_at() if synced else self.initial_sync_at()

    def _indexer(self) -> Any:
        from app.demo.snapshot import load_demo_replay
        from app.demo.supplement import CombinedReplay, load_document_replay
        from app.retrieval.indexing import IndexingService

        provider: Any = load_demo_replay(
            self._space, max_batch_items=64, path=recordings.DOCUMENT_EMBEDDINGS
        )
        if self._supplement:
            provider = CombinedReplay(
                provider, load_document_replay(self._space, max_batch_items=64)
            )
        return IndexingService(
            self.sessions,
            provider,
            batch_max_items=64,
            batch_max_estimated_tokens=32_000,
        )

    async def sync(
        self,
    ) -> tuple[dict[str, uuid.UUID], dict[str, StepOutcome], dict[str, list[uuid.UUID]]]:
        """Bring every demo course to the connector's final state through the
        real versioning path, index the new versions, and record one
        ChangeEvent per version not recorded yet. Returns the affected
        courses (ref -> ID), each one's outcome and its new version IDs.
        Syncing again brings and records nothing."""
        from app.connectors.sync import apply_changes
        from app.db.models import ChangeEvent, Institution, Resource, ResourceVersion
        from app.domain.enums import ChangeType, LmsType
        from app.evaluation.retrieval.resolve import production_limits

        connector = self._institution()
        initial, observed = self.initial_sync_at(), self.final_sync_at()
        async with self.sessions() as session:
            demo = dict(
                (
                    await session.execute(
                        select(Course.external_id, Course.id)
                        .join(Institution, Institution.id == Course.institution_id)
                        .where(Institution.lms_type == LmsType.DEMO)
                    )
                ).all()
            )
        courses, outcomes, changed = {}, {}, {}
        indexer = self._indexer()
        for ref in sorted(demo):
            course_id = demo[ref]
            await apply_changes(self.sessions, self._store, connector, course_id, ref, initial,
                                limits=production_limits())  # fmt: skip
            index = await indexer.index_course(course_id)
            later = {
                f"{c.ref}:{r.key}:{v.version}"
                for c in connector.manifest.courses if c.ref == ref
                for r in c.resources for v in r.versions if v.published_at > initial
            }  # fmt: skip
            created: list[ChangeEvent] = []
            async with self.sessions() as session, session.begin():
                rows = (
                    await session.execute(
                        select(Resource, ResourceVersion)
                        .join(ResourceVersion, ResourceVersion.resource_id == Resource.id)
                        .where(Resource.course_id == course_id)
                        .order_by(Resource.source_uri, ResourceVersion.version_number)
                    )
                ).all()
                recorded = set((await session.scalars(select(ChangeEvent.new_version_id))).all())
                versions = {(r.id, v.version_number): v for r, v in rows}
                for resource, version in rows:
                    key = f"{ref}:{resource.source_uri.rsplit('/', 1)[1]}:{version.version_number}"
                    if key not in later or version.id in recorded:
                        continue
                    old = versions.get((resource.id, version.version_number - 1))
                    event = ChangeEvent(
                        course_id=course_id, resource_id=resource.id,
                        change_type=ChangeType.UPDATED if old else ChangeType.ADDED,
                        old_version_id=old.id if old else None, new_version_id=version.id,
                        old_content_hash=old.content_hash if old else None,
                        new_content_hash=version.content_hash, observed_at=observed,
                    )  # fmt: skip
                    session.add(event)
                    created.append(event)
                await session.flush()
            if created:
                courses[ref] = course_id
                changed[ref] = [e.new_version_id for e in created]
                outcomes[ref] = StepOutcome(
                    WorkflowRunStatus.SUCCEEDED,
                    (index.run_id,),
                    {"change_event_ids": [str(e.id) for e in created],
                     "new_version_ids": [str(e.new_version_id) for e in created],
                     "observed_at": observed.isoformat()},
                )  # fmt: skip
        return courses, outcomes, changed

    async def changed_types(self, version_ids: Sequence[uuid.UUID]) -> set[Any]:
        from app.db.models import Resource, ResourceVersion

        async with self.sessions() as session:
            return set(
                (
                    await session.scalars(
                        select(Resource.type)
                        .join(ResourceVersion, ResourceVersion.resource_id == Resource.id)
                        .where(ResourceVersion.id.in_(version_ids))
                    )
                ).all()
            )

    async def ingest(self, *, reset: bool) -> tuple[dict[str, uuid.UUID], dict[str, StepOutcome]]:
        """Seed and index every demo course: (course ref -> ID, outcome)."""
        from app.demo.seed import seed
        from app.evaluation.retrieval.resolve import production_limits

        summary = await seed(
            self.sessions, self._store, limits=production_limits(), indexer=self._indexer(),
            reset=reset, supplement=self._supplement,
        )  # fmt: skip
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

    async def assessments(
        self, course_id: uuid.UUID, version_ids: Sequence[uuid.UUID] | None = None
    ) -> StepOutcome:
        from app.evaluation.assessments import run as assessments_run
        from app.evaluation.assessments.run import EvalConfig, ReplayProvider, assign_outputs
        from app.evaluation.northbridge.labels import load_golden
        from app.extraction.service import AssessmentExtractionService

        provider: Any = self._authored
        if not await self._is_authored(course_id):
            plan = assessments_run.plan_units(
                load_golden(), EvalConfig(recordings.ASSESSMENT_PROMPT)
            )
            provider = ReplayProvider(
                assign_outputs(_load(recordings.ASSESSMENT_OUTPUTS), plan), "replay:recorded-fast"
            )
        service = AssessmentExtractionService(
            self.sessions, provider, thinking=False, prompt_version=recordings.ASSESSMENT_PROMPT
        )
        result = await service.extract_course(course_id, version_ids)
        s = result.summary
        return StepOutcome(result.status, (result.run_id,),
                           {"versions": s.versions, "created": s.assessments_created,
                            "merged": s.assessments_merged, "deadlines_created": s.deadlines_created,
                            "units_failed": s.units_failed})  # fmt: skip

    async def claims(
        self, course_id: uuid.UUID, version_ids: Sequence[uuid.UUID] | None = None
    ) -> StepOutcome:
        from app.extraction.claims_service import ClaimExtractionService

        runs, counts, statuses = [], {}, []
        provider = self._authored if await self._is_authored(course_id) else self._claims
        for task in ("requirements", "obligations"):
            service = ClaimExtractionService(self.sessions, provider, task, thinking=False)
            result = await service.extract_course(course_id, version_ids)
            runs.append(result.run_id)
            statuses.append(result.status)
            counts[task] = {"versions": result.summary.versions,
                            "created": result.summary.created, "merged": result.summary.merged,
                            "units_failed": result.summary.units_failed}  # fmt: skip
        failed = WorkflowRunStatus.FAILED in statuses
        return StepOutcome(
            WorkflowRunStatus.FAILED if failed else WorkflowRunStatus.SUCCEEDED, tuple(runs), counts
        )

    async def mapping(self, course_id: uuid.UUID, courses: dict[str, uuid.UUID]) -> StepOutcome:
        """Map one course; the recorded query vectors are checked against
        every course's queries, so all requirements must exist first."""
        from app.demo.supplement import supplement_refs
        from app.mapping.service import MaterialMappingService

        if self._search is None:
            extra = supplement_refs() if self._supplement else frozenset()
            golden = {ref: c for ref, c in courses.items() if ref not in extra}
            ours = {ref: c for ref, c in courses.items() if ref in extra}
            self._search = mapping_search(
                self.sessions,
                self._space,
                await query_inputs(self.sessions, golden),
                await query_inputs(self.sessions, ours) if ours else (),
            )
        verifier = self._authored if await self._is_authored(course_id) else self._verifier
        service = MaterialMappingService(self.sessions, self._search, verifier, thinking=False)
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
    """Seed, index, and extract assessments, requirements and obligations of
    the golden courses (the evaluation corpus: no supplement)."""
    replay = DemoReplay(sessions, store, space, supplement=False)
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
