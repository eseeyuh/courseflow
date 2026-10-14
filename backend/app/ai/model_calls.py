# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Run a model call (structured chat or embeddings) and record it as one
``ModelCall`` row.

The row is written in its own short transaction, not the caller's: a failed
call must stay on record even if the caller's work is rolled back. For
embeddings, the successful row and the vectors it produced are written in
the same transaction; if that fails, the successful row is retried alone.

Every outcome is recorded: success, ``AIProviderError``, and any other
exception (as ``unexpected``, since that is a provider contract violation).
Cancellation is not recorded: the task is being torn down.
"""

import json
import logging
import math
import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import NoReturn

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai.embeddings import EmbeddingInput, EmbeddingProvider, EmbeddingResult, check_batch
from app.ai.errors import AIProviderError
from app.ai.provider import AIProvider, ChatMessage, StructuredResult, TokenUsage
from app.db.models import ModelCall
from app.domain.enums import ModelCallErrorCategory, ModelCallKind, ModelCallStatus, ModelRole

logger = logging.getLogger(__name__)

_MAX_OUTPUT_SUMMARY_CHARS = 2000
_TRUNCATION_MARKER = "...[truncated]"
_MAX_DETAIL_CHARS = 500


@dataclass(frozen=True, slots=True)
class RecordedResult[T: BaseModel]:
    result: StructuredResult[T]
    model_call_id: uuid.UUID


class RecordedCallError(Exception):
    """A model call failed. ``model_call_id`` is its row, or ``None`` if recording
    also failed (then ``record_error`` says why)."""

    def __init__(
        self,
        error: AIProviderError,
        model_call_id: uuid.UUID | None,
        record_error: Exception | None = None,
    ) -> None:
        super().__init__(str(error))
        self.error = error
        self.model_call_id = model_call_id
        self.record_error = record_error


async def call_structured_recorded[T: BaseModel](
    provider: AIProvider,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    messages: Sequence[ChatMessage],
    schema: type[T],
    role: ModelRole,
    prompt_version: str,
    workflow_run_id: uuid.UUID | None = None,
    temperature: float | None = None,
) -> RecordedResult[T]:
    started_at = datetime.now(UTC)
    # Monotonic clock for duration; finished_at is derived from it so a wall
    # clock step cannot make finished_at < started_at.
    started = time.perf_counter()
    row = ModelCall(
        workflow_run_id=workflow_run_id,
        call_kind=ModelCallKind.CHAT_STRUCTURED,
        provider=provider.name,
        model=provider.model_for(role),
        model_role=role,
        prompt_version=prompt_version,
        output_schema=schema.__name__,
        started_at=started_at,
    )
    try:
        result = await provider.chat_structured(
            messages, schema, role=role, temperature=temperature
        )
        if not isinstance(result.output, schema):
            raise TypeError(
                f"provider returned {type(result.output).__name__}, expected {schema.__name__}"
            )
    except AIProviderError as error:
        await _record_failure(session_factory, row, started, error)
    except Exception as exc:  # noqa: BLE001 - recorded as unexpected, then re-raised
        error = AIProviderError(
            ModelCallErrorCategory.UNEXPECTED,
            f"provider contract violation: {type(exc).__name__}: {exc}"[:_MAX_DETAIL_CHARS],
            model=row.model,
            attempts=1,  # unknown; at least one request was attempted
            usage=TokenUsage(),
        )
        await _record_failure(session_factory, row, started, error, cause=exc)

    _fill(row, started, result.usage, result.attempts, result.repair_rounds, result.request_id)
    row.model = result.model
    row.status = ModelCallStatus.SUCCEEDED
    summary = result.output.model_dump_json()
    if len(summary) > _MAX_OUTPUT_SUMMARY_CHARS:
        summary = summary[: _MAX_OUTPUT_SUMMARY_CHARS - len(_TRUNCATION_MARKER)]
        summary += _TRUNCATION_MARKER
    row.output_summary = summary
    try:
        await _persist(session_factory, row)
    except Exception:
        # The paid, validated result cannot be audited: fail loudly, with the
        # spend in the logs.
        logger.exception("ai.model_call_record_failed", extra=_row_fields(row))
        raise
    return RecordedResult(result=result, model_call_id=row.id)


async def _record_failure(
    session_factory: async_sessionmaker[AsyncSession],
    row: ModelCall,
    started: float,
    error: AIProviderError,
    cause: BaseException | None = None,
) -> NoReturn:
    """Record a failed call, then raise ``RecordedCallError``.

    If recording also fails (e.g. the database is down), that is logged with
    everything the row would have held, and the caller still receives the
    original model failure.
    """
    _fill(row, started, error.usage, error.attempts, error.repair_rounds, error.request_id)
    row.model = error.model
    row.status = ModelCallStatus.FAILED
    row.error_category = error.category
    row.error_detail = error.detail
    try:
        await _persist(session_factory, row)
    except Exception as record_error:
        logger.exception("ai.model_call_record_failed", extra=_row_fields(row))
        raise RecordedCallError(error, None, record_error) from cause or error
    raise RecordedCallError(error, row.id) from cause or error


def _fill(
    row: ModelCall,
    started: float,
    usage: TokenUsage,
    attempts: int,
    repair_rounds: int,
    request_id: str | None,
) -> None:
    row.latency_ms = round((time.perf_counter() - started) * 1000)
    row.finished_at = row.started_at + timedelta(milliseconds=row.latency_ms)
    row.attempts = attempts
    row.repair_rounds = repair_rounds
    row.prompt_tokens = usage.prompt_tokens
    row.completion_tokens = usage.completion_tokens
    row.reasoning_tokens = usage.reasoning_tokens
    row.provider_request_id = request_id


async def _persist(session_factory: async_sessionmaker[AsyncSession], row: ModelCall) -> None:
    async with session_factory() as session, session.begin():
        session.add(row)


def _row_fields(row: ModelCall) -> dict[str, object]:
    """Everything the row holds except the output, for a log line."""
    return {
        "workflow_run_id": row.workflow_run_id,
        "call_kind": row.call_kind,
        "provider": row.provider,
        "model": row.model,
        "model_role": row.model_role,
        "prompt_version": row.prompt_version,
        "output_schema": row.output_schema,
        "status": row.status,
        "error_category": row.error_category,
        "error_detail": row.error_detail,
        "attempts": row.attempts,
        "repair_rounds": row.repair_rounds,
        "prompt_tokens": row.prompt_tokens,
        "completion_tokens": row.completion_tokens,
        "reasoning_tokens": row.reasoning_tokens,
        "latency_ms": row.latency_ms,
        "provider_request_id": row.provider_request_id,
        "started_at": row.started_at,
        "finished_at": row.finished_at,
    }


# --- embedding calls ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RecordedEmbeddings[S]:
    result: EmbeddingResult
    model_call_id: uuid.UUID
    # What ``store`` returned, written in the same transaction as the row.
    stored: S | None


class VectorStorageError(Exception):
    """The provider call succeeded, but the transaction recording it (and storing
    any vectors it produced) failed.

    The successful call was then recorded alone, best effort, in a fresh
    transaction under the same ID (so an ambiguous first commit cannot leave
    two rows): ``model_call_id`` is that row, or ``None`` if the database
    refused that too (``record_error`` says why). ``usage`` is what the paid
    request reported. Either way the caller must stop: storage is failing.
    """

    def __init__(
        self,
        model_call_id: uuid.UUID | None,
        record_error: Exception | None,
        usage: TokenUsage | None = None,
    ) -> None:
        super().__init__("the successful embedding call could not be stored")
        self.model_call_id = model_call_id
        self.record_error = record_error
        self.usage = usage or TokenUsage()


type StoreVectors[S] = Callable[[AsyncSession, EmbeddingResult, uuid.UUID], Awaitable[S]]


async def call_embeddings_recorded[S](
    provider: EmbeddingProvider,
    session_factory: async_sessionmaker[AsyncSession],
    inputs: Sequence[EmbeddingInput],
    *,
    workflow_run_id: uuid.UUID | None = None,
    store: StoreVectors[S] | None = None,
) -> RecordedEmbeddings[S]:
    """One real embedding request, recorded as exactly one ``ModelCall`` row.

    A malformed batch raises ``ValueError`` before any request: nothing is
    recorded. A provider failure is recorded as a failed row and raised as
    ``RecordedCallError``. On success, the row and whatever ``store`` writes
    (the vectors) commit in one transaction; if that transaction fails, see
    ``VectorStorageError``.
    """
    check_batch(inputs, provider.max_batch_items)
    formats = {item.input_format for item in inputs}
    if len(formats) != 1:
        raise ValueError("an embedding batch must use one input format")
    space = provider.space
    started_at = datetime.now(UTC)
    started = time.perf_counter()
    row = ModelCall(
        id=uuid.uuid4(),  # fixed now: the fallback row reuses it
        workflow_run_id=workflow_run_id,
        call_kind=ModelCallKind.EMBEDDING,
        provider=space.provider,
        model=space.model,
        model_role=None,  # roles route generative models; embeddings have none
        prompt_version=formats.pop(),
        output_schema=f"vector[{space.dimensions}]",
        started_at=started_at,
    )
    try:
        result = await provider.embed(inputs)
        if result.space != space:  # wiring bug, not model output
            raise TypeError("provider returned vectors for a different space")
    except AIProviderError as error:
        await _record_failure(session_factory, row, started, error)
    except Exception as exc:  # noqa: BLE001 - recorded as unexpected, then re-raised
        error = AIProviderError(
            ModelCallErrorCategory.UNEXPECTED,
            f"provider contract violation: {type(exc).__name__}"[:_MAX_DETAIL_CHARS],
            model=space.model,
            attempts=1,  # unknown; at least one request was attempted
            usage=TokenUsage(),
        )
        await _record_failure(session_factory, row, started, error, cause=exc)

    # Output that does not fit the request is the provider's invalid output
    # (providers check this themselves; this is the boundary's own guarantee).
    problem = _vector_shape_problem(result, len(inputs), space.dimensions)
    if problem is not None:
        error = AIProviderError(
            ModelCallErrorCategory.INVALID_OUTPUT,
            problem,
            model=result.served_model or space.model,
            attempts=result.attempts,
            usage=result.usage,
            request_id=result.request_id,
        )
        await _record_failure(session_factory, row, started, error)

    _fill(row, started, result.usage, result.attempts, 0, result.request_id)
    row.model = result.served_model or space.model
    row.status = ModelCallStatus.SUCCEEDED
    row.output_summary = json.dumps(
        {
            "vectors": len(result.vectors),
            "dimensions": space.dimensions,
            "space_version": space.space_version,
            "requested_model": space.model,
        }
    )
    # Taken before the transaction: a rolled-back row must not be read again.
    fields = {column.key: getattr(row, column.key) for column in _COPIED_COLUMNS}
    stored: S | None = None
    try:
        async with session_factory() as session, session.begin():
            session.add(row)
            await session.flush()
            if store is not None:
                stored = await store(session, result, row.id)
    except Exception as storage_error:
        logger.error(
            "ai.embedding_storage_failed",
            extra={**fields, "error_type": type(storage_error).__name__},
        )
        fallback = ModelCall(**fields)
        try:
            await _persist(session_factory, fallback)
        except Exception as record_error:  # noqa: BLE001 - reported via VectorStorageError
            logger.error(
                "ai.model_call_record_failed",
                extra={**fields, "error_type": type(record_error).__name__},
            )
            raise VectorStorageError(None, record_error, result.usage) from storage_error
        raise VectorStorageError(fallback.id, None, result.usage) from storage_error
    return RecordedEmbeddings(result=result, model_call_id=row.id, stored=stored)


# Every column except created_at, including the id: the fallback row is the
# same call, so if the first commit did land, re-inserting it conflicts instead
# of recording the paid call twice.
_COPIED_COLUMNS = [column for column in ModelCall.__table__.columns if column.key != "created_at"]


def _vector_shape_problem(result: EmbeddingResult, expected: int, dimensions: int) -> str | None:
    """Why ``result`` does not answer a request of ``expected`` inputs, if it does not:
    wrong count or length, non-finite values, or an all-zero vector (which makes
    every cosine score NaN). Counts and positions only: never values or text."""
    if len(result.vectors) != expected:
        return f"expected {expected} vectors, got {len(result.vectors)}"
    for index, vector in enumerate(result.vectors):
        if len(vector) != dimensions:
            return f"vector {index} has {len(vector)} dimensions; requested {dimensions}"
        if not all(math.isfinite(value) for value in vector):
            return f"vector {index} has non-finite values"
        if not any(vector):
            return f"vector {index} is all zeros (cosine undefined)"
    return None
