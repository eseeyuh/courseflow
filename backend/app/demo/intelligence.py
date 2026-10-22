# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Rebuild the Northbridge demo intelligence offline, from the committed
recordings (``recordings``) only: no network, no spend, the same result
every time.

Seed and index (document embedding snapshot), then per course, through the
production services: assessments, requirements and obligations (recorded
FAST outputs replayed through the current gates), then material mapping
(recorded query vectors and verifier outputs).
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai.embeddings import EmbeddingInput, EmbeddingSpace, query_input
from app.db.models import Course
from app.demo import recordings
from app.ingestion.storage import RawObjectStore
from app.mapping.service import MappingRunResult, mapping_query, mapping_targets
from app.retrieval.search import SearchRequest


@dataclass(slots=True)
class DemoBuild:
    courses: dict[str, uuid.UUID]  # course external ID -> course ID
    runs: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    mapping: list[MappingRunResult] = field(default_factory=list)


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


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


async def build_extractions(
    sessions: async_sessionmaker[AsyncSession], store: RawObjectStore, space: EmbeddingSpace
) -> DemoBuild:
    """Seed, index, and extract assessments, requirements and obligations."""
    from app.demo.seed import seed
    from app.demo.snapshot import load_demo_replay
    from app.evaluation.assessments import run as assessments_run
    from app.evaluation.assessments.run import EvalConfig, ReplayProvider, assign_outputs
    from app.evaluation.claims.run import ClaimReplayProvider, recorded_by_material
    from app.evaluation.northbridge.labels import load_golden
    from app.evaluation.retrieval.resolve import production_limits
    from app.extraction.claims_service import ClaimExtractionService
    from app.extraction.service import AssessmentExtractionService
    from app.retrieval.indexing import IndexingService

    indexer = IndexingService(
        sessions,
        load_demo_replay(space, max_batch_items=64, path=recordings.DOCUMENT_EMBEDDINGS),
        batch_max_items=64,
        batch_max_estimated_tokens=32_000,
    )
    await seed(sessions, store, limits=production_limits(), indexer=indexer)
    plan = assessments_run.plan_units(load_golden(), EvalConfig(recordings.ASSESSMENT_PROMPT))
    assess = AssessmentExtractionService(
        sessions,
        ReplayProvider(
            assign_outputs(_load(recordings.ASSESSMENT_OUTPUTS), plan), "replay:recorded-fast"
        ),
        thinking=False,
        prompt_version=recordings.ASSESSMENT_PROMPT,
    )
    claims = ClaimReplayProvider(recorded_by_material(_load(recordings.CLAIM_OUTPUTS)))
    tasks = {
        task: ClaimExtractionService(sessions, claims, task, thinking=False)
        for task in ("requirements", "obligations")
    }
    async with sessions() as session:
        courses = dict((await session.execute(select(Course.external_id, Course.id))).all())
    build = DemoBuild(courses)
    for ref in sorted(courses):
        runs = build.runs.setdefault(ref, [])
        result = await assess.extract_course(courses[ref])
        runs.append({"workflow": "assessment-extraction", "status": result.status.value})
        for task, service in tasks.items():
            claim_result = await service.extract_course(courses[ref])
            runs.append({"workflow": f"{task}-extraction", "status": claim_result.status.value})
    return build


def mapping_search(
    sessions: async_sessionmaker[AsyncSession], space: EmbeddingSpace, inputs: Any
) -> Any:
    """The production search over the recorded mapping query vectors."""
    from app.demo.snapshot import snapshot_subject
    from app.evaluation.northbridge.labels import load_golden
    from app.evaluation.retrieval.replay import load_replay_provider
    from app.evaluation.retrieval.snapshot import ExpectedInputs
    from app.retrieval.search import SearchService

    replay = load_replay_provider(
        recordings.MAPPING_QUERY_EMBEDDINGS,
        dataset=snapshot_subject(load_golden()),
        active_space=space,
        max_batch_items=64,
        inputs=ExpectedInputs((), inputs),
    )
    return SearchService(sessions, space, replay)


async def build_demo_intelligence(
    sessions: async_sessionmaker[AsyncSession], store: RawObjectStore, space: EmbeddingSpace
) -> DemoBuild:
    """The initial Northbridge state with every demo claim, by replay."""
    from app.mapping.replay import MappingReplayProvider
    from app.mapping.service import MaterialMappingService

    build = await build_extractions(sessions, store, space)
    search = mapping_search(sessions, space, await query_inputs(sessions, build.courses))
    verifier = MappingReplayProvider.from_recorded(_load(recordings.MAPPING_OUTPUTS))
    mapping = MaterialMappingService(sessions, search, verifier, thinking=False)
    for ref in sorted(build.courses):
        result = await mapping.map_course(build.courses[ref])
        build.mapping.append(result)
        build.runs[ref].append({"workflow": "material-mapping", "status": result.status.value})
    return build
