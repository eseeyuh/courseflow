# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Run a structured model call and record it as one ``ModelCall`` row.

The row is written in its own short transaction, not the caller's: a failed
call must stay on record even if the caller's work is rolled back.

Every outcome is recorded: success, ``AIProviderError``, and any other
exception (as ``unexpected``, since that is a provider contract violation).
Cancellation is not recorded: the task is being torn down.
"""

import logging
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import NoReturn

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai.errors import AIProviderError
from app.ai.provider import AIProvider, ChatMessage, StructuredResult, TokenUsage
from app.db.models import ModelCall
from app.domain.enums import ModelCallErrorCategory, ModelCallStatus, ModelRole

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
