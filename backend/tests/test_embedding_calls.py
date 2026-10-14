# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""call_embeddings_recorded: one ModelCall per real embedding request, and the
vectors stored in the same transaction as a successful row."""

import dataclasses
import json
import logging
import uuid
from typing import Any

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai import model_calls
from app.ai.embeddings import (
    DOCUMENT_INPUT_FORMAT,
    EmbeddingResult,
    document_input,
    query_input,
)
from app.ai.errors import AIProviderError
from app.ai.model_calls import RecordedCallError, VectorStorageError, call_embeddings_recorded
from app.ai.provider import TokenUsage
from app.db.models import ModelCall
from app.domain.enums import ModelCallErrorCategory, ModelCallKind, ModelCallStatus
from tests.ai_fakes import EMBEDDING_MODEL, ScriptedServer, embeddings, make_embedding_provider
from tests.embedding_fakes import FAKE_MODEL, FakeEmbeddingProvider

pytestmark = pytest.mark.anyio

SENSITIVE = "SENSITIVE-CHUNK-TEXT"


@pytest.fixture
def session_factory(committed_session: AsyncSession) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(committed_session.bind, expire_on_commit=False)


async def _rows(session_factory: async_sessionmaker[AsyncSession]) -> list[ModelCall]:
    async with session_factory() as session:
        return list((await session.scalars(select(ModelCall))).all())


def _stored_text(row: ModelCall) -> str:
    return json.dumps({c.key: str(getattr(row, c.key)) for c in ModelCall.__table__.columns})


async def test_success_is_one_embedding_row_written_with_the_stored_vectors(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    provider = FakeEmbeddingProvider(dimensions=16)
    seen: dict[str, Any] = {}

    async def store(session: AsyncSession, result: EmbeddingResult, call_id: uuid.UUID) -> str:
        # Same transaction: the row is already flushed and visible here.
        seen["row_visible"] = await session.scalar(
            select(func.count()).where(ModelCall.id == call_id)
        )
        return "stored"

    recorded = await call_embeddings_recorded(
        provider, session_factory, [document_input(SENSITIVE), document_input("b")], store=store
    )

    (row,) = await _rows(session_factory)
    assert recorded.model_call_id == row.id and recorded.stored == "stored"
    assert seen["row_visible"] == 1
    assert row.call_kind is ModelCallKind.EMBEDDING
    assert row.model_role is None and row.repair_rounds == 0
    assert row.status is ModelCallStatus.SUCCEEDED
    assert row.prompt_version == DOCUMENT_INPUT_FORMAT
    assert row.output_schema == "vector[16]"  # the space's actual dimensions
    assert row.provider == "fake" and row.model == FAKE_MODEL
    assert row.attempts == 1 and row.provider_request_id == "fake-1"
    assert row.prompt_tokens == 4 and row.completion_tokens == 0
    assert json.loads(row.output_summary or "") == {
        "vectors": 2,
        "dimensions": 16,
        "space_version": "v1",
        "requested_model": FAKE_MODEL,
    }
    assert SENSITIVE not in _stored_text(row)


async def test_the_served_model_is_telemetry_and_the_configured_one_is_kept_in_the_summary(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    server = ScriptedServer([embeddings(served_model="vendor/embedding-model-2026-10")])

    await call_embeddings_recorded(
        make_embedding_provider(server), session_factory, [query_input("When is CW1 due?")]
    )

    (row,) = await _rows(session_factory)
    assert row.model == "vendor/embedding-model-2026-10"
    assert json.loads(row.output_summary or "")["requested_model"] == EMBEDDING_MODEL
    assert row.prompt_version == "query/v1"
    assert row.workflow_run_id is None  # a standalone call


async def test_a_provider_failure_is_recorded_as_a_failed_embedding_row(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    failure = AIProviderError(
        ModelCallErrorCategory.RATE_LIMITED,
        "HTTP 429",
        model=FAKE_MODEL,
        attempts=3,
        usage=TokenUsage(),
        request_id="req-429",
    )
    provider = FakeEmbeddingProvider(failures=[failure])
    stores: list[object] = []

    async def store(*args: object) -> None:
        stores.append(args)

    with pytest.raises(RecordedCallError) as caught:
        await call_embeddings_recorded(
            provider, session_factory, [document_input(SENSITIVE)], store=store
        )

    (row,) = await _rows(session_factory)
    assert caught.value.model_call_id == row.id
    assert row.status is ModelCallStatus.FAILED
    assert row.call_kind is ModelCallKind.EMBEDDING and row.model_role is None
    assert row.error_category is ModelCallErrorCategory.RATE_LIMITED
    assert row.attempts == 3 and row.provider_request_id == "req-429"
    assert row.output_summary is None
    assert stores == []
    assert SENSITIVE not in _stored_text(row)


@pytest.mark.parametrize(
    "inputs",
    [[], [document_input("a")] * 65, [document_input("a"), query_input("b")]],
    ids=["empty", "too-many", "mixed-formats"],
)
async def test_a_malformed_batch_records_nothing_and_calls_nothing(
    session_factory: async_sessionmaker[AsyncSession], inputs: list[Any]
) -> None:
    provider = FakeEmbeddingProvider()

    with pytest.raises(ValueError):
        await call_embeddings_recorded(provider, session_factory, inputs)

    assert provider.calls == []
    assert await _rows(session_factory) == []


async def test_a_wrong_vector_count_is_recorded_as_invalid_output_with_its_spend(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    provider = FakeEmbeddingProvider(truncate_results=True)
    stores: list[object] = []

    async def store(*args: object) -> None:
        stores.append(args)

    with pytest.raises(RecordedCallError) as caught:
        await call_embeddings_recorded(
            provider, session_factory, [document_input("a"), document_input("b")], store=store
        )

    (row,) = await _rows(session_factory)
    assert caught.value.error.category is ModelCallErrorCategory.INVALID_OUTPUT
    assert not caught.value.error.retriable
    assert row.status is ModelCallStatus.FAILED
    assert row.error_category is ModelCallErrorCategory.INVALID_OUTPUT
    assert row.error_detail == "expected 2 vectors, got 1"
    assert row.attempts == 1 and row.prompt_tokens == 2  # the spend is kept
    assert row.provider_request_id == "fake-1"
    assert stores == []  # nothing stored for invalid output


async def test_vectors_of_the_wrong_length_are_invalid_output(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    provider = FakeEmbeddingProvider(dimensions=8)
    original = provider.embed

    async def short_vectors(inputs: Any) -> EmbeddingResult:
        result = await original(inputs)
        return dataclasses.replace(result, vectors=tuple(v[:4] for v in result.vectors))

    provider.embed = short_vectors  # type: ignore[method-assign]

    with pytest.raises(RecordedCallError):
        await call_embeddings_recorded(provider, session_factory, [document_input("a")])

    (row,) = await _rows(session_factory)
    assert row.error_category is ModelCallErrorCategory.INVALID_OUTPUT
    assert row.error_detail == "vector 0 has 4 dimensions; requested 8"


async def test_a_result_for_another_space_is_an_unexpected_wiring_error(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    provider = FakeEmbeddingProvider()
    other = FakeEmbeddingProvider(space_version="v2")
    provider.embed = other.embed  # type: ignore[method-assign]

    with pytest.raises(RecordedCallError):
        await call_embeddings_recorded(provider, session_factory, [document_input("a")])

    (row,) = await _rows(session_factory)
    assert row.error_category is ModelCallErrorCategory.UNEXPECTED


async def test_vector_storage_failure_keeps_a_successful_row_and_no_vectors(
    session_factory: async_sessionmaker[AsyncSession], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)

    async def store(session: AsyncSession, result: EmbeddingResult, call_id: uuid.UUID) -> None:
        await session.execute(text("CREATE TEMPORARY TABLE stored_marker (x int)"))
        raise OperationalError("INSERT INTO embeddings", {}, Exception("disk full"))

    with pytest.raises(VectorStorageError) as caught:
        await call_embeddings_recorded(
            FakeEmbeddingProvider(), session_factory, [document_input(SENSITIVE)], store=store
        )

    (row,) = await _rows(session_factory)
    assert caught.value.model_call_id == row.id
    assert row.status is ModelCallStatus.SUCCEEDED  # the paid request did succeed
    assert row.error_category is None
    assert row.output_summary is not None
    assert SENSITIVE not in caplog.text
    assert "ai.embedding_storage_failed" in [r.getMessage() for r in caplog.records]


async def test_when_the_fallback_row_also_fails_nothing_is_stored_and_logs_stay_safe(
    session_factory: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    caplog.set_level(logging.DEBUG)

    async def store(*args: object) -> None:
        raise OperationalError("INSERT INTO embeddings", {}, Exception("connection lost"))

    async def persist(*args: object) -> None:
        raise OperationalError("INSERT INTO model_calls", {}, Exception("connection lost"))

    monkeypatch.setattr(model_calls, "_persist", persist)

    with pytest.raises(VectorStorageError) as caught:
        await call_embeddings_recorded(
            FakeEmbeddingProvider(), session_factory, [document_input(SENSITIVE)], store=store
        )

    assert caught.value.model_call_id is None
    assert isinstance(caught.value.record_error, OperationalError)
    assert await _rows(session_factory) == []
    assert SENSITIVE not in caplog.text
    messages = [r.getMessage() for r in caplog.records]
    assert "ai.embedding_storage_failed" in messages
    assert "ai.model_call_record_failed" in messages
