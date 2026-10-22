# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""The study plan on the replayed Northbridge demo, through the API: both
courses, prerequisites first, the Coursework 1 conflict exposed and not
resolved, and a study-state change that recomputes the plan."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai.nebius import NebiusTokenFactoryProvider
from app.ai.nebius_embeddings import NebiusEmbeddingProvider
from app.db.models import SourceSpan, StudyPlan, StudyTask
from app.demo.intelligence import build_demo_intelligence
from app.domain.enums import ResourceType
from app.ingestion.storage import LocalRawObjectStore
from tests.test_demo_replay import SPACE

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def _no_live_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    async def refuse(*_: Any, **__: Any) -> Any:
        raise AssertionError("a live provider call was attempted")

    monkeypatch.setattr(NebiusEmbeddingProvider, "embed", refuse)
    monkeypatch.setattr(NebiusTokenFactoryProvider, "chat_structured", refuse)


def _index(tasks: list[dict[str, Any]], pick: Any) -> int:
    return next(i for i, t in enumerate(tasks) if pick(t))


async def test_the_demo_study_plan_end_to_end(
    committed_session: AsyncSession, tmp_path: Path, client: httpx.AsyncClient
) -> None:
    sessions = async_sessionmaker(committed_session.bind, expire_on_commit=False)
    raw = tmp_path / "raw"
    raw.mkdir()
    await build_demo_intelligence(sessions, LocalRawObjectStore(raw), SPACE)
    async with sessions() as session:  # the workflow's plan step saved a snapshot
        assert await session.scalar(select(func.count()).select_from(StudyPlan)) == 1
        saved = await session.scalar(select(func.count()).select_from(StudyTask))

    response = await client.get("/api/v1/study-plan")
    assert response.status_code == 200
    plan = response.json()
    tasks = plan["tasks"]
    assert plan["as_of"].startswith("2026-10-05T08:00")  # the demo clock, not now
    assert len(tasks) == saved
    assert [t["position"] for t in tasks] == list(range(1, len(tasks) + 1))

    # 3: both courses.
    assert {t["course"]["code"] for t in tasks} == {"aml7001", "msd7000"}
    # 1: the supervisor review before the Project Specification submission.
    review = _index(tasks, lambda t: t["task_type"] == "obligation"
                    and t["assessment"] and t["assessment"]["title"] == "Project Specification")  # fmt: skip
    submit_spec = _index(tasks, lambda t: t["title"] == "Submit Project Specification")
    assert review < submit_spec and tasks[submit_spec]["status"] == "blocked"
    assert tasks[review]["evidence"][0]["quote"].startswith("Before you submit your Project")
    # 2: ethics approval before data collection, with the source wording.
    ethics = _index(tasks, lambda t: t["task_type"] == "ethics_approval")
    data = _index(tasks, lambda t: t["task_type"] == "data_collection")
    assert ethics < data and tasks[data]["status"] == "blocked"
    assert "ethics approval" in " ".join(tasks[ethics]["evidence"][0]["quote"].split())
    # 4: nearer mandatory work (Coursework 1, 12 Nov) above later recommended work (30 Nov).
    submit_cw1 = _index(tasks, lambda t: t["title"].startswith("Submit Coursework 1"))
    later_study = _index(tasks, lambda t: t["task_type"] == "study_material"
                         and t["assessment"]["title"] == "Project Specification")  # fmt: skip
    assert submit_cw1 < later_study
    # 5: the mapped Lecture 07 study task, with exact targets.
    lecture = _index(tasks, lambda t: t["task_type"] == "study_material"
                     and "Imbalanced Data" in t["title"])  # fmt: skip
    targets = tasks[lecture]["targets"]
    assert targets and all(x["slide_number"] and x["quote"] for x in targets)
    assert tasks[lecture]["study_state"] == "not_started"
    # 7-8: the conflict task carries both values, and is scheduling only.
    check = tasks[_index(tasks, lambda t: t["task_type"] == "check_conflict")]
    values = [(d["due_date"], d["due_time"]) for d in check["conflict_values"]]
    assert values == [("2026-11-12", "14:00"), ("2026-11-13", "23:59")]
    assert all(d["evidence"] for d in check["conflict_values"])
    assert check["anchor_basis"] == "earliest_conflicting_deadline"
    assert "does not decide which source is right" in check["why_now"]
    conflict = next(r for r in check["reasons"] if r["code"] == "conflicting_deadline")
    assert conflict["resolved"] is False
    assert tasks[submit_cw1]["anchor_basis"] == "earliest_conflicting_deadline"

    # 9: the same facts give the same plan.
    assert (await client.get("/api/v1/study-plan")).json() == plan

    # 6: studying, then studying everything, changes the plan; nothing else moves.
    span_ids = [x["source_span_id"] for x in targets]
    put = await client.put(
        "/api/v1/study-states", json={"source_span_ids": span_ids[:1], "state": "studying"}
    )
    assert put.json() == {"changed": 1}
    again = await client.put(
        "/api/v1/study-states", json={"source_span_ids": span_ids[:1], "state": "studying"}
    )
    assert again.json() == {"changed": 0}  # idempotent
    studying = (await client.get("/api/v1/study-plan")).json()["tasks"]
    task = next(t for t in studying if t["task_key"] == tasks[lecture]["task_key"])
    assert task["study_state"] == "studying" and "You are studying this." in task["why_now"]

    await client.put("/api/v1/study-states", json={"source_span_ids": span_ids, "state": "studied"})
    done = (await client.get("/api/v1/study-plan")).json()["tasks"]
    finished = next(t for t in done if t["task_key"] == tasks[lecture]["task_key"])
    assert finished["status"] == "completed" and done[-1]["task_key"] == finished["task_key"]
    open_before = [t["task_key"] for t in tasks if t["task_key"] != finished["task_key"]]
    assert [t["task_key"] for t in done[:-1]] == open_before
    states = (await client.get("/api/v1/study-states")).json()
    assert {s["source_span_id"] for s in states} == set(span_ids)
    assert {s["state"] for s in states} == {"studied"}


async def test_a_study_state_needs_a_mapped_teaching_target(
    committed_session: AsyncSession, tmp_path: Path, client: httpx.AsyncClient
) -> None:
    from tests.test_assessment_schema import _course
    from tests.test_material_mapping import _ingest

    sessions = async_sessionmaker(committed_session.bind, expire_on_commit=False)
    raw = tmp_path / "raw"
    raw.mkdir()
    course = await _course(sessions)
    page = b"<!DOCTYPE html><html><body><h1>Lecture</h1><p>Text.</p></body></html>"
    version = await _ingest(sessions, raw, course, ResourceType.LECTURE, page, "upload:l1")
    async with sessions() as session:
        span = await session.scalar(
            select(SourceSpan.id).where(SourceSpan.resource_version_id == version)
        )
    for body in (
        {"source_span_ids": [str(span)], "state": "studied"},  # a span nothing maps to
        {"source_span_ids": [str(uuid.uuid4())], "state": "studied"},
    ):
        response = await client.put("/api/v1/study-states", json=body)
        assert response.status_code == 422
    bad_state = await client.put(
        "/api/v1/study-states", json={"source_span_ids": [str(span)], "state": "mastered"}
    )
    assert bad_state.status_code == 422
    assert (await client.get("/api/v1/study-states")).json() == []
    assert (await client.get("/api/v1/study-plan")).status_code == 409  # no plan yet, no clock
