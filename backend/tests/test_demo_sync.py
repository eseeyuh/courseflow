# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""The demo LMS sync, offline: initial -> final through the real versioning
path, ChangeEvents from the version state, only changed versions
reprocessed, the brief's deadline superseded by the source that says so,
the remaining disagreement kept as a conflict, and the plan before vs
after compared by task key. Syncing again changes nothing."""

from __future__ import annotations

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
    AssessmentDeadline,
    ChangeEvent,
    Conflict,
    DependencyEdge,
    EvidenceLink,
    MaterialMapping,
    ModelCall,
    Obligation,
    ResourceVersion,
    StudyPlan,
)
from app.demo.intelligence import DemoReplay
from app.ingestion.storage import LocalRawObjectStore
from app.orchestration.workflow import SYNC_STEPS
from app.reasoning.supersession import REPLACES_BRIEF
from tests.test_demo_replay import SPACE

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def _no_live_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    async def refuse(*_: Any, **__: Any) -> Any:
        raise AssertionError("a live provider call was attempted")

    monkeypatch.setattr(NebiusEmbeddingProvider, "embed", refuse)
    monkeypatch.setattr(NebiusTokenFactoryProvider, "chat_structured", refuse)


async def _counts(sessions: async_sessionmaker[AsyncSession]) -> dict[str, int]:
    async with sessions() as session:
        return {
            m.__name__: int(await session.scalar(select(func.count()).select_from(m)) or 0)
            for m in (AssessmentDeadline, Obligation, MaterialMapping, DependencyEdge, Conflict,
                      EvidenceLink, ChangeEvent, StudyPlan, ResourceVersion, ModelCall)
        }  # fmt: skip


def _deadlines(assessment: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    return {(d["due_date"], d["due_time"]): d for d in assessment["deadlines"]}


async def test_initial_sync_final_and_sync_again(
    app: FastAPI, client: httpx.AsyncClient, committed_session: AsyncSession, tmp_path: Path
) -> None:
    sessions = async_sessionmaker(committed_session.bind, expire_on_commit=False)
    raw = tmp_path / "raw"
    raw.mkdir()
    app.dependency_overrides[get_demo_replay] = lambda: DemoReplay(
        sessions, LocalRawObjectStore(raw), SPACE
    )

    # BEFORE: the initial state, its conflict and its plan.
    assert (await client.post("/api/v1/demo/workflow-runs", json={"reset": True})).json()[
        "succeeded"
    ]
    assert (await client.get("/api/v1/changes")).json() == []
    (conflict,) = [
        c for course in (await client.get("/api/v1/courses")).json()
        for c in (await client.get(f"/api/v1/courses/{course['id']}/conflicts")).json()
    ]  # fmt: skip
    cw1 = conflict["assessment_id"]
    before = _deadlines((await client.get(f"/api/v1/assessments/{cw1}")).json())
    assert set(before) == {("2026-11-12", "14:00"), ("2026-11-13", "23:59")}
    assert all(d["active"] for d in before.values())
    plan_before = (await client.get("/api/v1/study-plan")).json()
    assert plan_before["as_of"].startswith("2026-10-05T08:00")
    calls_before = (await _counts(sessions))["ModelCall"]

    # SYNC: exactly the two changed resources, through the versioning path.
    synced = (await client.post("/api/v1/demo/sync")).json()
    assert synced["succeeded"] is True
    assert synced["observed_at"].startswith("2026-10-21T09:00")
    assert sorted((c["resource_title"], c["old_version"], c["new_version"])
                  for c in synced["changes"]) == [
        ("AML7001 announcements", 1, 2),
        ("MSD7000 MSc Dissertation Handbook 2026-27", 1, 2),
    ]  # fmt: skip
    assert len(synced["affected_course_ids"]) == 2 and len(synced["runs"]) == 2
    for run in synced["runs"]:
        trace = run["summary"]
        assert [s["name"] for s in trace["steps"]] == list(SYNC_STEPS)
        assert len(trace["changed_version_ids"]) == 1  # only the new version was read
        mapping = next(s for s in trace["steps"] if s["name"] == "mapping")
        assert mapping["counts"] == {"skipped": "no brief or teaching material changed"}
    async with sessions() as session:
        events = (await session.scalars(select(ChangeEvent))).all()
    assert len(events) == 2
    assert all(e.old_content_hash != e.new_content_hash and e.workflow_run_id for e in events)
    # Replay only: the changed versions' extraction calls are replayed recordings.
    assert (await _counts(sessions))["ModelCall"] > calls_before

    # AFTER: the brief's date is history, the announcement's is active, and its
    # disagreement with the (unchanged) handbook is still a conflict.
    after = _deadlines((await client.get(f"/api/v1/assessments/{cw1}")).json())
    brief, handbook, announced = (after[("2026-11-12", "14:00")], after[("2026-11-13", "23:59")],
                                  after[("2026-11-19", "14:00")])  # fmt: skip
    assert brief["active"] is False and brief["evidence"]  # kept, with its evidence
    assert brief["superseded_by_deadline_id"] == announced["id"]
    assert brief["superseded_by_quote"] == "This replaces the date in the brief"
    assert handbook["active"] and announced["active"]
    (now,) = (await client.get(f"/api/v1/assessments/{cw1}/conflicts")).json()
    assert now["id"] == conflict["id"] and now["status"] == "open"
    assert [(s["due_date"], s["due_time"]) for s in now["sides"]] == [
        ("2026-11-13", "23:59"),
        ("2026-11-19", "14:00"),
    ]
    assert not {"winner", "resolved_value", "chosen"} & set(now)

    # The plan moved to the sync clock; the conflict task shows the final sides.
    plan_after = (await client.get("/api/v1/study-plan")).json()
    assert plan_after["as_of"].startswith("2026-10-21T09:00")
    check = next(t for t in plan_after["tasks"] if t["task_type"] == "check_conflict")
    assert [(d["due_date"], d["due_time"]) for d in check["conflict_values"]] == [
        ("2026-11-13", "23:59"),
        ("2026-11-19", "14:00"),
    ]
    assert "does not decide which source is right" in check["why_now"]
    # The supervisor review still drives its dependency: v2 still states it.
    msd = next(c for c in synced["affected_course_ids"] if c != now["course_id"])
    edges = (await client.get(f"/api/v1/courses/{msd}/dependencies")).json()
    assert {e["dependent"]["label"] for e in edges} == {"Project Specification", "Data collection"}

    # /changes explains it: what each version affected, and the plan diff.
    (sync,) = (await client.get("/api/v1/changes")).json()
    kinds = {a["kind"] for c in sync["changes"] for a in c["affected"]}
    assert {"deadline_added", "deadline_superseded", "conflict_changed"} <= kinds
    superseded = next(a for c in sync["changes"] for a in c["affected"]
                      if a["kind"] == "deadline_superseded")  # fmt: skip
    assert (superseded["old_value"]["date"], superseded["new_value"]["date"]) == (
        "2026-11-12", "2026-11-19",
    )  # fmt: skip
    assert superseded["sources"][0]["quote"] == "This replaces the date in the brief"
    diff = sync["plan_diff"]
    assert diff["added"] == [] and diff["removed"] == []
    changed = {c["task_key"]: c["fields"] for c in diff["changed"]}
    assert changed[check["task_key"]]["conflict_values"] == [
        [["2026-11-12", "14:00"], ["2026-11-13", "23:59"]],
        [["2026-11-13", "23:59"], ["2026-11-19", "14:00"]],
    ]
    assert diff["unchanged_count"] == len(plan_after["tasks"]) - len(changed)
    assert (await client.get("/api/v1/changes")).json() == [sync]  # deterministic

    # SYNC AGAIN: nothing new, no run, no fake change, no new plan.
    state = await _counts(sessions)
    again = (await client.post("/api/v1/demo/sync")).json()
    assert again["succeeded"] is True and again["changes"] == [] and again["runs"] == []
    assert await _counts(sessions) == state


def test_the_supersession_rule_reads_only_the_explicit_statement() -> None:
    assert REPLACES_BRIEF.search("This replaces the date in the brief. Nothing else changed.")
    assert REPLACES_BRIEF.search("this replaces the deadline in the brief")
    for text in (
        "The submission deadline is the one given in the brief.",
        "This replaces the date in the handbook.",
        "The brief replaces earlier guidance.",
    ):
        assert REPLACES_BRIEF.search(text) is None


async def test_production_refuses_the_demo_sync(settings: Any) -> None:
    from app.main import create_app
    from tests.conftest import _serve

    production = settings.model_copy(update={"app_env": "production"})
    async with _serve(create_app(production)) as client:
        assert (await client.post("/api/v1/demo/sync")).status_code == 404
        assert (await client.get("/api/v1/changes")).status_code == 200  # reads stay
