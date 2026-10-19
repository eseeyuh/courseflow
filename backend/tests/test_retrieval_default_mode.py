# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Frozen regressions for the runtime default search mode, on retrieval
benchmark v1 with the committed replay snapshot (no provider, no network).

Each test seeds the corpus once through the production lifecycle and runs
only the cases it is about, with requests that name no mode, so they go
through ``DEFAULT_SEARCH_MODE``. The full evaluation (every case, every
mode, fresh-seed determinism) lives in the evaluation tests."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.evaluation.retrieval.dataset import Dataset, load_dataset
from app.evaluation.retrieval.lifecycle import seed_and_index
from app.evaluation.retrieval.metrics import SEARCH_K, CaseResult, Hit, score_case
from app.evaluation.retrieval.replay import ReplayEmbeddingProvider, load_replay_provider
from app.evaluation.retrieval.resolve import ResolvedDataset, resolve_dataset
from app.ingestion.storage import LocalRawObjectStore
from app.retrieval.search import DEFAULT_SEARCH_MODE, SearchMode, SearchRequest, SearchService
from tests.test_retrieval_snapshot import DATASET_DIR, SPACE

pytestmark = pytest.mark.anyio

SNAPSHOT = (
    DATASET_DIR.parents[2]
    / "snapshots"
    / "retrieval"
    / "v1"
    / "nebius-qwen3-embedding-8b-d4096-sv1.jsonl"
)
CRITICAL_CASE = "ds101-mixed-cw1-deadline"
CRITICAL_MAX_RANK = 3
INVARIANT_CASES = ("ds101-inv-stale-group-submission", "ds101-inv-cross-course-loudness")


@dataclass(frozen=True, slots=True)
class Seeded:
    sessions: async_sessionmaker[AsyncSession]
    dataset: Dataset
    resolved: ResolvedDataset
    replay: ReplayEmbeddingProvider


@pytest.fixture
async def seeded(committed_session: AsyncSession, tmp_path: Path) -> Seeded:
    sessions = async_sessionmaker(committed_session.bind, expire_on_commit=False)
    dataset = load_dataset(DATASET_DIR)
    replay = load_replay_provider(SNAPSHOT, dataset=dataset, active_space=SPACE, max_batch_items=64)
    raw = tmp_path / "raw"
    raw.mkdir()
    result = await seed_and_index(
        sessions,
        LocalRawObjectStore(raw),
        dataset,
        replay,
        batch_max_items=64,
        batch_max_estimated_tokens=32_000,
    )
    async with sessions() as session:
        resolved = await resolve_dataset(session, dataset, result.versions)
    return Seeded(sessions, dataset, resolved, replay)


async def run_default(seeded: Seeded, case_id: str) -> CaseResult:
    """One case, in the default mode (the request names no mode)."""
    dataset, resolved = seeded.dataset, seeded.resolved
    index = next(i for i, c in enumerate(dataset.cases) if c.case_id == case_id)
    case, resolved_case = dataset.cases[index], resolved.cases[index]
    versions = resolved.versions
    by_id = {v.version_id: v for v in versions.values()}
    course_id = next(v.course_id for v in versions.values() if v.key[0] == case.course)
    request = SearchRequest.build(
        course_id=course_id,
        query=case.query,
        k=SEARCH_K,
        resource_types=frozenset(case.resource_types) if case.resource_types else None,
    )
    assert request.mode is DEFAULT_SEARCH_MODE is SearchMode.HYBRID
    result = await SearchService(seeded.sessions, SPACE, seeded.replay).search_course(request)
    assert result.mode is DEFAULT_SEARCH_MODE
    hits = [
        Hit(
            rank=hit.rank,
            version=by_id[hit.version.id].key if hit.version.id in by_id else None,
            current=hit.version.id in by_id and by_id[hit.version.id].current,
            start=hit.start_offset,
            end=hit.end_offset,
            ordinal=hit.chunk_ordinal,
        )
        for hit in result.hits
    ]
    return score_case(case, resolved_case, hits)


async def test_the_critical_deadline_case_stays_in_the_top_3_by_default(seeded: Seeded) -> None:
    assert seeded.dataset.manifest.critical_case.case_id == CRITICAL_CASE
    result = await run_default(seeded, CRITICAL_CASE)
    assert result.critical and result.invariant_failures == ()
    assert result.first_relevant_rank is not None
    assert result.first_relevant_rank <= CRITICAL_MAX_RANK


async def test_the_default_mode_never_returns_stale_or_foreign_versions(seeded: Seeded) -> None:
    for case_id in INVARIANT_CASES:
        result = await run_default(seeded, case_id)
        assert result.hits, case_id  # the probe ran against a populated corpus
        assert result.invariant_failures == (), case_id
