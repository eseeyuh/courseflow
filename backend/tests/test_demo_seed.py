# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Course import from the demo connector, change application and the scoped
reset, on the committed Northbridge institution. Seeds index with the
offline fake provider; replay is covered by test_demo_seed_acceptance."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.connectors.demo import DemoConnector
from app.connectors.sync import SyncConflictError, ensure_institution, import_course
from app.db.models import Course, Institution, Resource, ResourceVersion, SourceSpan
from app.demo.generate import DATA_ROOT
from app.demo.reset import reset_demo_institution
from app.demo.seed import seed
from app.domain.enums import LmsType
from app.evaluation.retrieval.resolve import production_limits
from app.ingestion.storage import LocalRawObjectStore
from app.retrieval.indexing import IndexingService
from tests.embedding_fakes import FakeEmbeddingProvider

pytestmark = pytest.mark.anyio


@pytest.fixture
def sessions(committed_session: AsyncSession) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(committed_session.bind, expire_on_commit=False)


@pytest.fixture
def store(tmp_path: Path) -> LocalRawObjectStore:
    root = tmp_path / "raw"
    root.mkdir()
    return LocalRawObjectStore(root)


@pytest.fixture
def limits_and_indexer(sessions: async_sessionmaker[AsyncSession]) -> dict[str, Any]:
    indexer = IndexingService(
        sessions, FakeEmbeddingProvider(), batch_max_items=64, batch_max_estimated_tokens=32_000
    )
    return {"limits": production_limits(), "indexer": indexer}


async def _count(sessions: async_sessionmaker[AsyncSession], model: object) -> int:
    async with sessions() as session:
        return int(await session.scalar(select(func.count()).select_from(model)) or 0)


async def _current_numbers(sessions: async_sessionmaker[AsyncSession]) -> dict[str, int]:
    async with sessions() as session:
        rows = await session.execute(
            select(Resource.source_uri, ResourceVersion.version_number).join(
                ResourceVersion, ResourceVersion.id == Resource.current_version_id
            )
        )
        return {uri.rsplit("/", 1)[1]: number for uri, number in rows}


async def test_seed_initial_then_final_is_idempotent_at_every_step(
    sessions: async_sessionmaker[AsyncSession],
    store: LocalRawObjectStore,
    limits_and_indexer: dict[str, Any],
) -> None:
    opts = limits_and_indexer
    first = await seed(sessions, store, **opts)
    assert [c["initial_versions_created"] for c in first["courses"]] == [6, 4]
    assert (await _count(sessions, Course), await _count(sessions, ResourceVersion)) == (2, 10)
    assert await _count(sessions, SourceSpan) > 0

    again = await seed(sessions, store, **opts)
    assert [c["initial_versions_created"] for c in again["courses"]] == [0, 0]

    final = await seed(sessions, store, **opts, state="final")
    assert [c["change_versions_created"] for c in final["courses"]] == [1, 1]
    current = await _current_numbers(sessions)
    assert current["announcements"] == current["dissertation-handbook"] == 2
    assert sum(current.values()) == 12  # 10 resources, two of them at v2

    # Re-importing the initial (v1) sources after v2 must not create a v3.
    replay = await seed(sessions, store, **opts, state="final")
    assert [c["initial_versions_created"] for c in replay["courses"]] == [0, 0]
    assert [c["change_versions_created"] for c in replay["courses"]] == [0, 0]
    assert await _count(sessions, ResourceVersion) == 12


async def test_stored_content_that_disagrees_with_the_connector_is_a_conflict(
    sessions: async_sessionmaker[AsyncSession],
    store: LocalRawObjectStore,
    tmp_path: Path,
    limits_and_indexer: dict[str, Any],
) -> None:
    opts = limits_and_indexer
    await seed(sessions, store, **opts)
    altered = tmp_path / "northbridge"
    shutil.copytree(DATA_ROOT, altered)
    brief = altered / "courses/aml7001/AML_Assignment_Brief_v1.pdf"
    brief.write_bytes(brief.read_bytes() + b"\n%altered\n")
    connector = DemoConnector(altered)
    (stub, _) = await connector.list_courses("demo-student")
    with pytest.raises(SyncConflictError, match="stored content differs"):
        await import_course(
            sessions,
            store,
            connector,
            stub,
            institution_name="Northbridge University",
            lms_type=LmsType.DEMO,
            limits=opts["limits"],
        )


async def test_reset_removes_only_the_northbridge_institution(
    sessions: async_sessionmaker[AsyncSession],
    store: LocalRawObjectStore,
    limits_and_indexer: dict[str, Any],
) -> None:
    opts = limits_and_indexer
    async with sessions() as session, session.begin():
        await ensure_institution(session, "Other University", LmsType.DEMO)
        await ensure_institution(session, "Northbridge University", LmsType.UPLOAD)
    await seed(sessions, store, **opts, state="final")

    summary = await seed(sessions, store, **opts, reset=True)
    assert summary["reset"] == {
        "institutions": 1,
        "courses": 2,
        "resources": 10,
        "versions": 12,
        "workflow_runs": 4,
    }
    async with sessions() as session:
        names = sorted(
            (name, lms.value)
            for name, lms in await session.execute(select(Institution.name, Institution.lms_type))
        )
    # The reseed recreated Northbridge (demo); the other two were never touched.
    assert names == [
        ("Northbridge University", "demo"),
        ("Northbridge University", "upload"),
        ("Other University", "demo"),
    ]
    assert await _count(sessions, ResourceVersion) == 10


async def test_only_a_demo_institution_can_be_reset(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    with pytest.raises(ValueError, match="only a demo institution"):
        await reset_demo_institution(sessions, "Northbridge University", LmsType.MOODLE)


async def test_the_seed_gives_northbridge_its_timezone_and_never_overwrites_one(
    sessions: async_sessionmaker[AsyncSession],
    store: LocalRawObjectStore,
    limits_and_indexer: dict[str, Any],
) -> None:
    await seed(sessions, store, **limits_and_indexer)
    async with sessions() as session:
        zone = await session.scalar(
            select(Institution.timezone).where(Institution.lms_type == LmsType.DEMO)
        )
    assert zone == "Europe/London"
    async with sessions() as session, session.begin():
        # An unknown timezone is filled; a different one is a conflict.
        await ensure_institution(session, "Unzoned", LmsType.DEMO)
        await ensure_institution(session, "Unzoned", LmsType.DEMO, timezone="Europe/Paris")
        with pytest.raises(SyncConflictError, match="has timezone"):
            await ensure_institution(
                session, "Northbridge University", LmsType.DEMO, timezone="Europe/Paris"
            )
