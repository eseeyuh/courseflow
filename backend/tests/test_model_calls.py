# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""ModelCall: one recorded row per logical model call, for success and failure."""

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import BaseModel
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.ai.course_fact import PROMPT_VERSION, CourseFact, build_messages
from app.ai.model_calls import RecordedCallError, call_structured_recorded
from app.ai.provider import StructuredResult, TokenUsage
from app.db.models import Course, Institution, ModelCall, WorkflowRun
from app.domain.enums import (
    LmsType,
    ModelCallErrorCategory,
    ModelCallStatus,
    ModelRole,
)
from tests.ai_fakes import (
    API_KEY,
    FAST_MODEL,
    STRONG_MODEL,
    VALID_FACT,
    ScriptedServer,
    completion,
    fact_completion,
    make_provider,
    status,
)
from tests.conftest import UNREACHABLE_DATABASE_URL

pytestmark = pytest.mark.anyio

MESSAGES = build_messages("Submit via Moodle by 23:59 on 31 October 2026.", "Deadline?")


@pytest.fixture
def session_factory(committed_session: AsyncSession) -> async_sessionmaker[AsyncSession]:
    """Real commits against the (truncated-before-and-after) test database."""
    return async_sessionmaker(committed_session.bind, expire_on_commit=False)


async def _record(
    server: ScriptedServer,
    session_factory: async_sessionmaker[AsyncSession],
    **kwargs: Any,
):
    return await call_structured_recorded(
        make_provider(server),
        session_factory,
        messages=MESSAGES,
        schema=CourseFact,
        role=kwargs.pop("role", ModelRole.FAST),
        prompt_version=PROMPT_VERSION,
        **kwargs,
    )


async def _only_row(session_factory: async_sessionmaker[AsyncSession]) -> ModelCall:
    async with session_factory() as session:
        rows = (await session.scalars(select(ModelCall))).all()
    assert len(rows) == 1
    return rows[0]


# --- recorder: success and failure -----------------------------------------


async def test_success_is_recorded_with_lineage_tokens_and_validated_output(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    server = ScriptedServer([fact_completion(request_id="req-1")])

    recorded = await _record(server, session_factory, role=ModelRole.STRONG)

    row = await _only_row(session_factory)
    assert row.id == recorded.model_call_id
    assert row.workflow_run_id is None
    assert row.provider == "nebius-token-factory"
    assert row.model == STRONG_MODEL
    assert row.model_role is ModelRole.STRONG
    assert row.prompt_version == PROMPT_VERSION
    assert row.output_schema == "CourseFact"
    assert row.status is ModelCallStatus.SUCCEEDED
    assert row.error_category is None and row.error_detail is None
    assert row.attempts == 1
    assert (row.prompt_tokens, row.completion_tokens, row.reasoning_tokens) == (100, 50, 30)
    assert row.provider_request_id == "req-1"
    assert json.loads(row.output_summary or "") == VALID_FACT
    assert row.started_at <= row.finished_at
    assert row.latency_ms >= 0


async def test_permanent_failure_is_recorded_and_reraised(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    server = ScriptedServer([status(404)])

    with pytest.raises(RecordedCallError) as caught:
        await _record(server, session_factory)

    row = await _only_row(session_factory)
    assert caught.value.model_call_id == row.id
    assert caught.value.error.category is ModelCallErrorCategory.INVALID_REQUEST
    assert row.status is ModelCallStatus.FAILED
    assert row.error_category is ModelCallErrorCategory.INVALID_REQUEST
    assert "404" in (row.error_detail or "")
    assert row.attempts == 1
    assert row.model == FAST_MODEL
    assert row.output_summary is None
    assert row.prompt_tokens is None  # no usage was reported, so none is invented


async def test_retry_exhaustion_is_recorded_with_every_attempt_counted(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    server = ScriptedServer([status(503)] * 3)

    with pytest.raises(RecordedCallError):
        await _record(server, session_factory)

    row = await _only_row(session_factory)
    assert row.error_category is ModelCallErrorCategory.SERVER_ERROR
    assert row.attempts == 3


async def test_invalid_output_failure_keeps_tokens_spent_on_both_turns(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    server = ScriptedServer([completion("nope"), completion("still nope")])

    with pytest.raises(RecordedCallError):
        await _record(server, session_factory)

    row = await _only_row(session_factory)
    assert row.error_category is ModelCallErrorCategory.INVALID_OUTPUT
    assert row.attempts == 2
    assert (row.prompt_tokens, row.completion_tokens) == (200, 100)


async def test_recorded_rows_never_contain_the_api_key(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    server = ScriptedServer([status(401, body={"detail": f"bad key {API_KEY}"})])

    with pytest.raises(RecordedCallError):
        await _record(server, session_factory)

    row = await _only_row(session_factory)
    stored = " ".join(str(value) for value in vars(row).values())
    assert API_KEY not in stored


async def test_failure_record_survives_caller_rollback(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as caller_session:
        await caller_session.begin()
        caller_session.add(Institution(name="Rolled back", lms_type=LmsType.DEMO))
        await caller_session.flush()
        with pytest.raises(RecordedCallError):
            await _record(ScriptedServer([status(500)] * 3), session_factory)
        await caller_session.rollback()

    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(Institution)) == 0
    row = await _only_row(session_factory)
    assert row.status is ModelCallStatus.FAILED


async def test_recording_failure_does_not_mask_the_model_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    engine = create_async_engine(UNREACHABLE_DATABASE_URL, connect_args={"timeout": 1})
    try:
        with pytest.raises(RecordedCallError) as caught:
            await _record(
                ScriptedServer([status(401)]), async_sessionmaker(engine, expire_on_commit=False)
            )
    finally:
        await engine.dispose()

    assert caught.value.error.category is ModelCallErrorCategory.AUTHENTICATION
    assert caught.value.model_call_id is None
    assert caught.value.record_error is not None
    (record,) = [r for r in caplog.records if r.getMessage() == "ai.model_call_record_failed"]
    assert record.error_category == ModelCallErrorCategory.AUTHENTICATION
    assert record.attempts == 1
    assert record.prompt_version == PROMPT_VERSION


async def test_unrecordable_success_fails_loudly_with_spend_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    engine = create_async_engine(UNREACHABLE_DATABASE_URL, connect_args={"timeout": 1})
    try:
        with pytest.raises(OSError):
            await _record(
                ScriptedServer([fact_completion()]),
                async_sessionmaker(engine, expire_on_commit=False),
            )
    finally:
        await engine.dispose()

    (record,) = [r for r in caplog.records if r.getMessage() == "ai.model_call_record_failed"]
    assert record.status == ModelCallStatus.SUCCEEDED
    assert record.prompt_tokens == 100


async def test_calls_link_to_their_workflow_run_and_are_deleted_with_it(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session, session.begin():
        course = Course(
            institution=Institution(name="Northbridge University", lms_type=LmsType.DEMO),
            title="Data Systems",
        )
        run = WorkflowRun(workflow_version="test/v1", course=course)
        session.add(run)

    await _record(ScriptedServer([fact_completion()]), session_factory, workflow_run_id=run.id)

    row = await _only_row(session_factory)
    assert row.workflow_run_id == run.id
    async with session_factory() as session, session.begin():
        await session.execute(delete(WorkflowRun).where(WorkflowRun.id == run.id))
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(ModelCall)) == 0


# --- database-enforced invariants ------------------------------------------


def _row(**fields: Any) -> ModelCall:
    now = datetime.now(UTC)
    values: dict[str, Any] = {
        "provider": "p",
        "model": "m",
        "prompt_version": "v1",
        "output_schema": "CourseFact",
        "status": ModelCallStatus.SUCCEEDED,
        "attempts": 1,
        "latency_ms": 10,
        "output_summary": "{}",
        "started_at": now,
        "finished_at": now,
    }
    return ModelCall(**(values | fields))


async def test_minimal_valid_rows_are_accepted(db_session: AsyncSession) -> None:
    db_session.add_all(
        [
            _row(),
            _row(
                status=ModelCallStatus.FAILED,
                error_category=ModelCallErrorCategory.TIMEOUT,
                output_summary=None,
            ),
        ]
    )
    await db_session.flush()


@pytest.mark.parametrize(
    ("fields", "constraint"),
    [
        ({"error_category": ModelCallErrorCategory.TIMEOUT}, "outcome_consistent"),
        ({"output_summary": None}, "outcome_consistent"),
        ({"status": ModelCallStatus.FAILED}, "outcome_consistent"),
        ({"attempts": 0}, "attempts_positive"),
        ({"repair_rounds": 1, "attempts": 1}, "repair_rounds_bounded"),
        ({"repair_rounds": -1}, "repair_rounds_bounded"),
        ({"latency_ms": -1}, "latency_nonnegative"),
        ({"prompt_tokens": -5}, "tokens_nonnegative"),
        (
            {"finished_at": datetime.now(UTC) - timedelta(seconds=1)},
            "finished_after_started",
        ),
    ],
    ids=[
        "success-with-error",
        "success-without-output",
        "failure-without-category",
        "zero-attempts",
        "repairs-without-attempts",
        "negative-repairs",
        "negative-latency",
        "negative-tokens",
        "finished-before-started",
    ],
)
async def test_invalid_rows_are_rejected_by_named_constraint(
    db_session: AsyncSession, fields: dict[str, Any], constraint: str
) -> None:
    db_session.add(_row(**fields))
    with pytest.raises(IntegrityError, match=f"ck_model_calls_{constraint}"):
        await db_session.flush()


async def test_repair_then_exhausted_retries_records_everything_spent(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    server = ScriptedServer([completion("nope")] + [status(503)] * 3)

    with pytest.raises(RecordedCallError):
        await _record(server, session_factory)

    row = await _only_row(session_factory)
    assert row.error_category is ModelCallErrorCategory.SERVER_ERROR
    assert (row.attempts, row.repair_rounds) == (4, 1)
    assert (row.prompt_tokens, row.completion_tokens) == (100, 50)  # from turn 1


async def test_successful_repair_is_recorded(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _record(ScriptedServer([completion("nope"), fact_completion()]), session_factory)

    row = await _only_row(session_factory)
    assert row.status is ModelCallStatus.SUCCEEDED
    assert (row.attempts, row.repair_rounds) == (2, 1)


# --- providers that break the contract ---------------------------------------


class _FakeProvider:
    """An AIProvider whose chat_structured does whatever the test needs."""

    name = "fake"

    def __init__(self, behaviour: Any) -> None:
        self._behaviour = behaviour

    def model_for(self, role: ModelRole) -> str:
        return "fake/model"

    async def chat_structured(self, messages, schema, *, role, temperature=None):  # type: ignore[no-untyped-def]
        return self._behaviour()

    async def list_models(self) -> list:
        return []


async def _record_with(provider: Any, session_factory, schema=CourseFact):  # type: ignore[no-untyped-def]
    return await call_structured_recorded(
        provider,
        session_factory,
        messages=MESSAGES,
        schema=schema,
        role=ModelRole.FAST,
        prompt_version=PROMPT_VERSION,
    )


async def test_non_provider_exception_is_still_recorded_as_unexpected(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    def explode() -> None:
        raise RuntimeError("provider bug")

    with pytest.raises(RecordedCallError) as caught:
        await _record_with(_FakeProvider(explode), session_factory)

    row = await _only_row(session_factory)
    assert caught.value.model_call_id == row.id
    assert isinstance(caught.value.__cause__, RuntimeError)
    assert row.error_category is ModelCallErrorCategory.UNEXPECTED
    assert "RuntimeError: provider bug" in (row.error_detail or "")


async def test_wrong_output_type_is_never_recorded_as_success(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    class Other(BaseModel):
        x: int = 1

    wrong = StructuredResult(output=Other(), model="fake/model", usage=TokenUsage(), attempts=1)

    with pytest.raises(RecordedCallError):
        await _record_with(_FakeProvider(lambda: wrong), session_factory)

    row = await _only_row(session_factory)
    assert row.status is ModelCallStatus.FAILED
    assert row.error_category is ModelCallErrorCategory.UNEXPECTED


async def test_long_output_summary_is_marked_as_truncated(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    class Long(BaseModel):
        text: str

    big = StructuredResult(
        output=Long(text="x" * 5000), model="fake/model", usage=TokenUsage(), attempts=1
    )

    await _record_with(_FakeProvider(lambda: big), session_factory, schema=Long)

    row = await _only_row(session_factory)
    assert row.output_summary is not None
    assert len(row.output_summary) == 2000
    assert row.output_summary.endswith("...[truncated]")


# --- the database accepts every enum value the code can produce --------------


@pytest.mark.parametrize("category", list(ModelCallErrorCategory))
async def test_every_error_category_is_allowed_by_the_schema(
    db_session: AsyncSession, category: ModelCallErrorCategory
) -> None:
    # Enum CHECKs are hand-written in migrations; autogenerate does not diff them.
    db_session.add(
        _row(status=ModelCallStatus.FAILED, error_category=category, output_summary=None)
    )
    await db_session.flush()


@pytest.mark.parametrize("role", list(ModelRole))
async def test_every_model_role_is_allowed_by_the_schema(
    db_session: AsyncSession, role: ModelRole
) -> None:
    db_session.add(_row(model_role=role))
    await db_session.flush()


async def test_unknown_error_category_is_rejected(db_session: AsyncSession) -> None:
    with pytest.raises(IntegrityError, match="ck_model_calls_model_call_error_category"):
        await db_session.execute(
            text(
                "INSERT INTO model_calls (provider, model, prompt_version, output_schema, "
                "status, error_category, attempts, latency_ms, started_at, finished_at) "
                "VALUES ('p', 'm', 'v', 's', 'failed', 'gremlins', 1, 1, now(), now())"
            )
        )
