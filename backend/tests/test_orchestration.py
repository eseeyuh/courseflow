# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""The course-intelligence workflow: the existing services in a fixed,
allowlisted order, one traced WorkflowRun per course, offline replay, a
failed step recorded and nothing half-written."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai.nebius import NebiusTokenFactoryProvider
from app.ai.nebius_embeddings import NebiusEmbeddingProvider
from app.api.v1.workflows import get_demo_replay
from app.db.models import (
    Assessment,
    AssessmentDeadline,
    AssessmentRequirement,
    Conflict,
    Course,
    DependencyEdge,
    EvidenceLink,
    MaterialMapping,
    ModelCall,
    Obligation,
    WorkflowRun,
)
from app.demo.intelligence import DemoReplay
from app.domain.enums import WorkflowRunStatus
from app.ingestion.storage import LocalRawObjectStore
from app.orchestration.workflow import STEPS, CourseIntelligenceWorkflow, UnknownStepError
from app.reasoning import dependencies
from tests.test_demo_replay import SPACE

pytestmark = pytest.mark.anyio

DOMAIN = (Assessment, AssessmentDeadline, AssessmentRequirement, Obligation, MaterialMapping,
          DependencyEdge, Conflict, EvidenceLink)  # fmt: skip


@pytest.fixture(autouse=True)
def _no_live_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    async def refuse(*_: Any, **__: Any) -> Any:
        raise AssertionError("a live provider call was attempted")

    monkeypatch.setattr(NebiusEmbeddingProvider, "embed", refuse)
    monkeypatch.setattr(NebiusTokenFactoryProvider, "chat_structured", refuse)


@pytest.fixture
def sessions(committed_session: AsyncSession) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(committed_session.bind, expire_on_commit=False)


@pytest.fixture
def replay(sessions: async_sessionmaker[AsyncSession], tmp_path: Path) -> DemoReplay:
    raw = tmp_path / "raw"
    raw.mkdir()
    return DemoReplay(sessions, LocalRawObjectStore(raw), SPACE)


async def _counts(sessions: async_sessionmaker[AsyncSession]) -> dict[str, int]:
    async with sessions() as session:
        return {
            model.__name__: int(await session.scalar(select(func.count()).select_from(model)) or 0)
            for model in (*DOMAIN, Course)
        }


@pytest.mark.parametrize("steps", [["ingest", "shell"], ["DROP TABLE"], [], ["validation", "x"]])
async def test_steps_outside_the_allowlist_are_refused_before_any_write(
    sessions: async_sessionmaker[AsyncSession], replay: DemoReplay, steps: list[str]
) -> None:
    with pytest.raises(UnknownStepError):
        await CourseIntelligenceWorkflow(sessions, replay).run(steps=steps)
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(WorkflowRun)) == 0
        assert await session.scalar(select(func.count()).select_from(Course)) == 0


async def test_the_full_offline_workflow_through_the_api_twice(
    app: FastAPI,
    client: httpx.AsyncClient,
    sessions: async_sessionmaker[AsyncSession],
    replay: DemoReplay,
) -> None:
    app.dependency_overrides[get_demo_replay] = lambda: replay
    refused = await client.post("/api/v1/demo/workflow-runs", json={"steps": ["ingest", "rm"]})
    assert refused.status_code == 422

    first = (await client.post("/api/v1/demo/workflow-runs", json={"reset": True})).json()
    assert first["succeeded"] is True and len(first["runs"]) == 2
    for run in first["runs"]:
        assert run["status"] == "succeeded" and run["finished_at"] is not None
        trace = run["summary"]
        assert [step["name"] for step in trace["steps"]] == list(STEPS)  # every step, in order
        assert all(step["status"] == "succeeded" for step in trace["steps"])
        assert trace["failed_step"] is None and trace["current_step"] is None
        assert trace["mode"] == "replay"
        validation = trace["steps"][-1]["counts"]
        assert validation["errors"] == 0
        fetched = (await client.get(f"/api/v1/workflow-runs/{run['id']}")).json()
        assert fetched == run
        listed = (await client.get(f"/api/v1/courses/{run['course_id']}/workflow-runs")).json()
        assert [r["id"] for r in listed] == [run["id"]]
    # Model lineage is the services' own: every extraction and mapping child
    # run holds its (replayed) ModelCalls.
    child = uuid.UUID(first["runs"][0]["summary"]["steps"][1]["child_run_ids"][0])
    async with sessions() as session:
        calls = await session.scalar(
            select(func.count()).select_from(ModelCall).where(ModelCall.workflow_run_id == child)
        )
    assert calls and calls > 0
    state = await _counts(sessions)
    assert state["DependencyEdge"] == 2 and state["Conflict"] == 1
    assert state["MaterialMapping"] > 0 and state["Obligation"] > 0

    second = (await client.post("/api/v1/demo/workflow-runs", json={})).json()
    assert second["succeeded"] is True
    assert {r["id"] for r in second["runs"]}.isdisjoint({r["id"] for r in first["runs"]})
    assert await _counts(sessions) == state  # a rerun multiplies nothing


async def test_a_failing_step_fails_the_run_and_writes_nothing(
    sessions: async_sessionmaker[AsyncSession],
    replay: DemoReplay,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = dependencies.replace_edges

    async def write_then_fail(session: AsyncSession, *args: Any, **kwargs: Any) -> Any:
        await real(session, *args, **kwargs)  # the edges are written in the step's transaction
        await session.flush()
        raise dependencies.DependencyIntegrityError("forced")

    monkeypatch.setattr(dependencies, "replace_edges", write_then_fail)
    result = await CourseIntelligenceWorkflow(sessions, replay).run(reset=True)

    assert not result.succeeded
    first, other = sorted(result.courses)  # aml7001 runs each step first
    assert result.traces[first].failed_step == "dependencies"
    assert result.traces[first].error_category == "integrity_error"
    assert result.traces[other].error_category == "aborted"
    names = [step.name for step in result.traces[first].steps]
    assert names == ["ingest", "assessments", "claims", "mapping", "dependencies"]
    assert result.traces[first].steps[-1].status == "failed"
    assert result.traces[first].steps[-1].error_type == "DependencyIntegrityError"
    async with sessions() as session:
        runs = (
            await session.scalars(
                select(WorkflowRun).where(WorkflowRun.id.in_(result.runs.values()))
            )
        ).all()
        dependency_runs = (
            await session.scalars(
                select(WorkflowRun).where(
                    WorkflowRun.workflow_version == dependencies.RULES_VERSION
                )
            )
        ).all()
    assert {r.status for r in runs} == {WorkflowRunStatus.FAILED}
    assert all(
        r.finished_at is not None and r.summary["failed_step"] == "dependencies" for r in runs
    )
    assert [r.status for r in dependency_runs] == [WorkflowRunStatus.FAILED]
    state = await _counts(sessions)
    assert state["DependencyEdge"] == 0  # the failed step left nothing behind
    assert state["Conflict"] == 0  # later steps never ran
    assert state["MaterialMapping"] > 0  # earlier steps' committed work stays
