# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""The live smoke module, exercised offline with the fake provider: its checks,
its idempotency and the safety of its report. (The live run itself uses the
configured Token Factory provider.)"""

import json
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai.embeddings import EmbeddingSpace
from app.core.config import Settings
from app.db.models import (
    Chunk,
    ChunkEmbedding,
    Course,
    Embedding,
    Institution,
    ModelCall,
    Resource,
    ResourceVersion,
)
from app.domain.enums import LmsType
from app.ingestion.guards import IngestionLimits
from app.ingestion.storage import LocalRawObjectStore
from app.retrieval import smoke_test
from tests.embedding_fakes import FakeEmbeddingProvider

pytestmark = pytest.mark.anyio

LIMITS = IngestionLimits.from_settings(
    Settings(_env_file=None, database_url="postgresql+asyncpg://u:p@127.0.0.1/db")  # type: ignore[call-arg, arg-type]
)
SPACE = EmbeddingSpace("fake", smoke_test.EXPECTED_MODEL, 256, "v1")


@pytest.fixture
def session_factory(committed_session: AsyncSession) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(committed_session.bind, expire_on_commit=False)


@pytest.fixture
def raw_store(tmp_path: Path) -> LocalRawObjectStore:
    root = tmp_path / "raw"
    root.mkdir()
    return LocalRawObjectStore(root)


async def _smoke(sf: async_sessionmaker[AsyncSession], store: LocalRawObjectStore, **kwargs):
    provider = kwargs.pop("provider", None) or FakeEmbeddingProvider(space=SPACE)
    return await smoke_test.run_smoke(
        provider, sf, store, LIMITS, expected_dimensions=SPACE.dimensions, **kwargs
    )


async def _counts(sf: async_sessionmaker[AsyncSession]) -> dict[str, int]:
    async with sf() as session:
        return {
            model.__tablename__: await session.scalar(select(func.count()).select_from(model))
            for model in (
                Institution,
                Course,
                Resource,
                ResourceVersion,
                Chunk,
                Embedding,
                ChunkEmbedding,
                ModelCall,
            )
        }


async def test_every_check_passes_and_the_report_holds_no_source_or_query_text(
    session_factory: async_sessionmaker[AsyncSession], raw_store: LocalRawObjectStore
) -> None:
    report = await _smoke(session_factory, raw_store)

    assert report["status"] == "passed", report["checks"]
    assert set(report["checks"]) >= {
        "configured_model",
        "configured_dimensions",
        "stored_vector_dimensions",
        "target_in_top_5",
        "course_isolation",
        "stale_version_excluded",
        "provenance_present",
        "active_space_only",
        "query_model_call_recorded",
        "document_model_calls_recorded",
    }
    assert set(report["queries"]) == {"Q1", "Q2", "Q3"}
    assert len(report["course_a_versions"]["stale"]) == 1  # the brief's first version
    for label, query in report["queries"].items():
        assert query["target_rank"] is not None and query["target_rank"] <= 5, label
        assert query["query_model_call"]["prompt_version"] == "query/v1"
        assert query["query_model_call"]["workflow_run_id"] is None
    serialized = json.dumps(report, default=str)
    for question, marker in smoke_test.QUERIES.values():
        assert question not in serialized and marker not in serialized
    assert "invented example" not in serialized


async def test_reruns_add_no_records_except_the_new_query_calls(
    session_factory: async_sessionmaker[AsyncSession], raw_store: LocalRawObjectStore
) -> None:
    provider = FakeEmbeddingProvider(space=SPACE)
    first = await _smoke(session_factory, raw_store, provider=provider)
    before = await _counts(session_factory)
    document_calls = len(provider.calls) - len(smoke_test.QUERIES)

    second = await _smoke(session_factory, raw_store, provider=provider)

    after = await _counts(session_factory)
    assert second["status"] == "passed"
    assert after == before | {"model_calls": before["model_calls"] + len(smoke_test.QUERIES)}
    assert second["courses"] == first["courses"]
    assert second["course_a_versions"] == first["course_a_versions"]
    assert all(run["provider_calls"] == 0 for run in second["index_runs"].values())
    assert len(provider.calls) == document_calls + 2 * len(smoke_test.QUERIES)


async def test_a_configuration_other_than_the_expected_model_fails_its_check(
    session_factory: async_sessionmaker[AsyncSession], raw_store: LocalRawObjectStore
) -> None:
    other = FakeEmbeddingProvider(space=EmbeddingSpace("fake", "some/other-model", 256, "v1"))

    report = await _smoke(session_factory, raw_store, provider=other)

    assert report["status"] == "failed"
    assert report["checks"]["configured_model"] is False


async def test_it_never_touches_other_courses(
    session_factory: async_sessionmaker[AsyncSession], raw_store: LocalRawObjectStore
) -> None:
    async with session_factory() as session, session.begin():
        session.add(
            Course(
                institution=Institution(name="Another institution", lms_type=LmsType.UPLOAD),
                title="Unrelated course",
            )
        )

    await _smoke(session_factory, raw_store)

    async with session_factory() as session:
        titles = set(await session.scalars(select(Course.title)))
    assert titles == {"Unrelated course", smoke_test.COURSE_A[1], smoke_test.COURSE_B[1]}
