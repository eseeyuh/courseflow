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
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai.embeddings import EmbeddingSpace
from app.ai.nebius import NebiusTokenFactoryProvider
from app.ai.nebius_embeddings import PROVIDER_NAME, NebiusEmbeddingProvider
from app.db.models import MaterialMapping, Obligation, Resource, ResourceVersion, SourceSpan
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
def _no_outbound_http(monkeypatch: pytest.MonkeyPatch) -> None:
    async def refuse(*_: Any, **__: Any) -> Any:
        raise AssertionError("a live provider call was attempted")

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
    statuses = {run["status"] for runs in build.runs.values() for run in runs}
    assert statuses == {WorkflowRunStatus.SUCCEEDED.value}
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
