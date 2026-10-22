# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""The demo intelligence rebuilds from a clean clone: every recording it
reads is committed (none under the gitignored ``evals/runs/``), and the
whole rebuild runs with outbound HTTP refused."""

from __future__ import annotations

import builtins
import io
import pathlib
import subprocess
import uuid
from datetime import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai.embeddings import EmbeddingSpace
from app.ai.nebius import NebiusTokenFactoryProvider
from app.ai.nebius_embeddings import PROVIDER_NAME, NebiusEmbeddingProvider
from app.db.models import (
    MaterialMapping,
    Obligation,
    Resource,
    ResourceVersion,
    SourceSpan,
    WorkflowRun,
)
from app.demo import recordings
from app.demo.intelligence import build_demo_intelligence
from app.domain.enums import ObligationCategory, VerificationStatus, WorkflowRunStatus
from app.evaluation.northbridge.labels import REPO_ROOT
from app.ingestion.storage import LocalRawObjectStore

pytestmark = pytest.mark.anyio

SPACE = EmbeddingSpace(PROVIDER_NAME, "Qwen/Qwen3-Embedding-8B", 4096, "v1")


def _tracked() -> set[Path]:
    listed = subprocess.run(
        ["git", "ls-files", "-z", "evals", "demo_data"],
        cwd=REPO_ROOT,
        capture_output=True,
        check=True,
    ).stdout.decode("utf-8")
    return {(REPO_ROOT / name).resolve() for name in listed.split("\0") if name}


def test_every_demo_recording_is_committed_and_outside_evals_runs() -> None:
    tracked = _tracked()
    for path in recordings.ALL:
        assert path.resolve() in tracked, path.relative_to(REPO_ROOT).as_posix()
        assert "runs" not in path.relative_to(REPO_ROOT / "evals").parts


@pytest.fixture(autouse=True)
def _no_outbound_http(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> None:
    async def refuse(*_: Any, **__: Any) -> Any:
        raise AssertionError("a live provider call was attempted")

    # The in-process API client sends through httpx too: with it, the
    # providers themselves are refused instead.
    if "client" not in request.fixturenames:
        monkeypatch.setattr(httpx.AsyncClient, "send", refuse)
    monkeypatch.setattr(NebiusEmbeddingProvider, "embed", refuse)
    monkeypatch.setattr(NebiusTokenFactoryProvider, "chat_structured", refuse)


@pytest.fixture
def reads(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Every repository data file opened during the test."""
    seen: list[Path] = []
    real_open, real_path_open = builtins.open, pathlib.Path.open

    def note(target: Any) -> None:
        if isinstance(target, (str, Path)):
            path = Path(target).resolve()
            if path.is_relative_to(REPO_ROOT / "evals") or path.is_relative_to(
                REPO_ROOT / "demo_data"
            ):
                seen.append(path)

    def audited_open(file: Any, *args: Any, **kwargs: Any) -> io.IOBase:
        note(file)
        return real_open(file, *args, **kwargs)

    def audited_path_open(self: Path, *args: Any, **kwargs: Any) -> Any:
        note(self)
        return real_path_open(self, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", audited_open)
    monkeypatch.setattr(pathlib.Path, "open", audited_path_open)
    return seen


async def test_a_clean_clone_rebuilds_the_demo_intelligence_offline(
    committed_session: AsyncSession, tmp_path: Path, reads: list[Path]
) -> None:
    sessions = async_sessionmaker(committed_session.bind, expire_on_commit=False)
    raw = tmp_path / "raw"
    raw.mkdir()
    build = await build_demo_intelligence(sessions, LocalRawObjectStore(raw), SPACE)

    assert sorted(build.courses) == ["aml7001", "msd7000"]
    async with sessions() as session:
        runs = (await session.scalars(select(WorkflowRun))).all()
    assert {r.status for r in runs} == {
        WorkflowRunStatus.SUCCEEDED
    }  # the workflow and its children
    assert sorted(build.runs) == ["aml7001", "msd7000"]
    # Every recording was read, and nothing that is not committed.
    tracked = _tracked()
    assert {p.resolve() for p in recordings.ALL} <= set(reads)
    assert [p for p in reads if p not in tracked] == []
    assert not [p for p in reads if p.is_relative_to(REPO_ROOT / "evals" / "runs")]

    async with sessions() as session:
        supervisor = await session.scalar(
            select(Obligation).where(Obligation.category == ObligationCategory.SUPERVISOR_REVIEW)
        )
        lecture_07 = (
            await session.scalars(
                select(Resource.source_uri)
                .join(ResourceVersion, ResourceVersion.resource_id == Resource.id)
                .join(SourceSpan, SourceSpan.resource_version_id == ResourceVersion.id)
                .join(MaterialMapping, MaterialMapping.source_span_id == SourceSpan.id)
            )
        ).all()
    assert supervisor is not None and supervisor.status is VerificationStatus.VERIFIED
    assert any(uri.endswith("/lecture-07-class-imbalance") for uri in lecture_07)


# --- dependency and conflict acceptance on the demo -----------------------------------------


async def test_demo_dependencies_and_conflict_through_the_api(
    committed_session: AsyncSession, tmp_path: Path, client: httpx.AsyncClient
) -> None:
    from app.db.models import Conflict, DependencyEdge
    from app.domain.enums import ConflictStatus
    from app.domain.enums import VerificationStatus as V
    from app.reasoning.conflicts import detect_course_conflicts
    from app.reasoning.dependencies import (
        DependencyIntegrityError,
        derive_course_dependencies,
        derive_edges,
        obligation_facts,
        replace_edges,
    )

    sessions = async_sessionmaker(committed_session.bind, expire_on_commit=False)
    raw = tmp_path / "raw"
    raw.mkdir()
    build = await build_demo_intelligence(sessions, LocalRawObjectStore(raw), SPACE)
    msd, aml = build.courses["msd7000"], build.courses["aml7001"]

    # 1-2: both demo dependencies, each with the exact source wording.
    edges = (await client.get(f"/api/v1/courses/{msd}/dependencies")).json()
    pairs = {(e["prerequisite"]["label"], e["relation"], e["dependent"]["label"]) for e in edges}
    review = next(e for e in edges if e["dependent"]["kind"] == "assessment")
    assert review["prerequisite"]["kind"] == "obligation"
    assert review["prerequisite"]["status"] == "verified"
    assert review["dependent"]["label"] == "Project Specification"
    assert review["relation"] == "required_for"
    assert review["evidence"][0]["quote"].startswith("Before you submit your Project Specification")
    assert ("Ethics approval", "required_for", "Data collection") in pairs
    ethics = next(e for e in edges if e["dependent"]["label"] == "Data collection")
    assert "before ethics approval" in " ".join(ethics["evidence"][0]["quote"].split())
    assert all(link["verification_status"] == "verified" for e in edges for link in e["evidence"])

    # 3-5: one Coursework 1 conflict, both values with their own evidence, no winner.
    (conflict,) = (await client.get(f"/api/v1/courses/{aml}/conflicts")).json()
    assert conflict["status"] == "open" and conflict["field"] == "deadline"
    assert conflict["assessment_title"].startswith("Coursework 1")
    values = [(s["due_date"], s["due_time"]) for s in conflict["sides"]]
    assert values == [("2026-11-12", "14:00"), ("2026-11-13", "23:59")]
    assert all(s["status"] == "verified" and s["evidence"] for s in conflict["sides"])
    assert not {"value", "resolved_value", "winner", "chosen"} & set(conflict)
    per_assessment = await client.get(f"/api/v1/assessments/{conflict['assessment_id']}/conflicts")
    assert per_assessment.json() == [conflict]
    assessment = (await client.get(f"/api/v1/assessments/{conflict['assessment_id']}")).json()
    assert len(assessment["deadlines"]) == 2  # both still shown on the assessment
    assert (await client.get(f"/api/v1/courses/{msd}/conflicts")).json() == []

    # Reruns change nothing: same rows, same IDs.
    async with sessions() as session:
        before = {e.id for e in (await session.scalars(select(DependencyEdge))).all()}
        conflict_ids = {c.id for c in (await session.scalars(select(Conflict))).all()}
    again = await derive_course_dependencies(sessions, msd)
    assert (again.summary.created, again.summary.kept) == (0, 2)
    await detect_course_conflicts(sessions, aml)
    async with sessions() as session:
        assert {e.id for e in (await session.scalars(select(DependencyEdge))).all()} == before
        assert {c.id for c in (await session.scalars(select(Conflict))).all()} == conflict_ids

    # 6: an invented end cannot persist, and the failed write changes nothing.
    async with sessions() as session:
        derived = derive_edges(await obligation_facts(session, msd))
    invented = [
        type(derived[0])(
            derived[0].relation,
            type(derived[0].prerequisite)(
                derived[0].prerequisite.kind, obligation_id=uuid.uuid4()
            ),
            derived[0].dependent, derived[0].rule, derived[0].obligation_id, derived[0].evidence,
        )
    ]  # fmt: skip
    with pytest.raises(DependencyIntegrityError, match="not claims of course"):
        async with sessions() as session, session.begin():
            await replace_edges(session, msd, invented, again.run_id)
    async with sessions() as session:
        assert {e.id for e in (await session.scalars(select(DependencyEdge))).all()} == before

    # A source stops disagreeing: resolved by source, never by choice; it reopens.
    async with sessions() as session, session.begin():
        from app.db.models import AssessmentDeadline

        later = await session.scalar(
            select(AssessmentDeadline).where(AssessmentDeadline.due_time == time(23, 59))
        )
        assert later is not None
        later.status, later.review_reason = V.NEEDS_REVIEW, "missing_timezone"  # type: ignore[assignment]
    await detect_course_conflicts(sessions, aml)
    (resolved,) = (await client.get(f"/api/v1/courses/{aml}/conflicts")).json()
    assert resolved["status"] == ConflictStatus.RESOLVED_BY_SOURCE.value
    assert len(resolved["sides"]) == 2  # the history of the disagreement is kept
    async with sessions() as session, session.begin():
        later = await session.get(AssessmentDeadline, later.id)
        assert later is not None
        later.status, later.review_reason = V.VERIFIED, None
    reopened = await detect_course_conflicts(sessions, aml)
    assert reopened.summary.reopened == 1
