# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""The measurement tooling, with the fake provider and a deterministic clock:
plan arithmetic, the hard request budget, metric definitions and the chunker
timing. No real request is ever made here."""

import dataclasses
import itertools
from collections.abc import Sequence

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai.embeddings import EmbeddingInput, EmbeddingResult
from app.ai.errors import AIProviderError
from app.ai.provider import TokenUsage
from app.db.models import ModelCall
from app.domain.enums import ModelCallErrorCategory
from app.retrieval.chunking import estimate_tokens
from app.retrieval.measure import (
    BudgetExceededError,
    RequestBudget,
    ThroughputPlan,
    plan_summary,
    run_chunker,
    run_throughput,
    synthetic_input,
)
from tests.embedding_fakes import FakeEmbeddingProvider

SMALL_PLAN = ThroughputPlan(input_estimated_tokens=(10,), batch_sizes=(1, 2), repetitions=3)


@pytest.fixture
def session_factory(committed_session: AsyncSession) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(committed_session.bind, expire_on_commit=False)


def _clock(step_seconds: float = 0.05):
    ticks = itertools.count(step=step_seconds)
    return lambda: next(ticks)


def test_the_planned_experiment_is_32_requests_840_inputs_and_420k_estimated_tokens() -> None:
    plan = ThroughputPlan()

    assert (plan.requests, plan.inputs, plan.estimated_tokens) == (32, 840, 420_000)
    summary = plan_summary(plan, FakeEmbeddingProvider().space, max_requests=40)
    assert summary["planned_requests"] == 32 and summary["max_requests"] == 40
    assert summary["inputs"] == "synthetic generated text only"


@pytest.mark.parametrize("size", [1, 10, 200, 800])
def test_synthetic_inputs_have_exactly_the_planned_estimate_and_are_unique(size: int) -> None:
    inputs = [synthetic_input(size, index) for index in range(5)]

    assert all(estimate_tokens(i.text) == size for i in inputs)
    assert all(not i.text[-1].isspace() for i in inputs)
    assert len({i.input_hash for i in inputs}) == 5


def test_the_request_budget_is_a_hard_stop() -> None:
    budget = RequestBudget(max_requests=2)
    budget.take()
    budget.take()

    with pytest.raises(BudgetExceededError):
        budget.take()


@pytest.mark.anyio
async def test_a_plan_above_the_budget_is_refused_before_any_request(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    provider = FakeEmbeddingProvider()

    with pytest.raises(BudgetExceededError, match="needs 32 requests; the budget is 31"):
        await run_throughput(provider, session_factory, ThroughputPlan(), max_requests=31)

    assert provider.calls == []


@pytest.mark.anyio
async def test_metrics_follow_their_definitions_and_every_request_is_recorded(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    provider = FakeEmbeddingProvider()

    report = await run_throughput(
        provider,
        session_factory,
        SMALL_PLAN,
        max_requests=8,
        assumed_usd_per_million_tokens=0.01,
        clock=_clock(0.05),
    )

    assert report.completed and report.requests_made == 8 == len(provider.calls)
    one, two = report.combinations
    assert (one.batch_size, one.measured_requests, one.retried_requests) == (1, 3, 0)
    assert one.latency_ms_p50 == pytest.approx(50.0) and one.latency_ms_p95 == pytest.approx(50.0)
    assert one.inputs_per_second == pytest.approx(1 / 0.05)
    assert two.inputs_per_second == pytest.approx(2 / 0.05)
    assert one.provider_tokens_per_second is not None  # the fake reports usage
    assert one.estimated_to_reported_ratio is not None
    assert report.computed_cost_usd == pytest.approx(report.reported_prompt_tokens * 0.01 / 1e6)
    async with session_factory() as session:
        calls = await session.scalar(
            select(func.count()).where(ModelCall.workflow_run_id.is_(None))
        )
    assert calls == 8  # warm-ups are real requests too, and recorded


@pytest.mark.anyio
async def test_retried_requests_are_reported_separately_from_the_latency_distribution(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    provider = FakeEmbeddingProvider()
    original = provider.embed

    async def second_measured_request_retried(inputs: Sequence[EmbeddingInput]) -> EmbeddingResult:
        result = await original(inputs)
        return dataclasses.replace(result, attempts=2 if len(provider.calls) == 3 else 1)

    provider.embed = second_measured_request_retried  # type: ignore[method-assign]

    report = await run_throughput(
        provider, session_factory, SMALL_PLAN, max_requests=8, clock=_clock()
    )

    one = report.combinations[0]
    assert (one.measured_requests, one.retried_requests) == (3, 1)
    assert one.retry_rate == pytest.approx(1 / 3)
    assert one.retried_attempts == (2,)
    assert len(one.retried_latency_ms) == 1


@pytest.mark.anyio
async def test_missing_usage_is_unavailable_never_replaced_by_the_estimate(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    provider = FakeEmbeddingProvider()
    original = provider.embed

    async def no_usage(inputs: Sequence[EmbeddingInput]) -> EmbeddingResult:
        return dataclasses.replace(await original(inputs), usage=TokenUsage())

    provider.embed = no_usage  # type: ignore[method-assign]

    report = await run_throughput(
        provider, session_factory, SMALL_PLAN, max_requests=8, clock=_clock()
    )

    assert report.requests_without_usage == 8 and report.reported_prompt_tokens is None
    assert all(c.provider_tokens_per_second is None for c in report.combinations)
    assert all(c.estimated_to_reported_ratio is None for c in report.combinations)


@pytest.mark.anyio
async def test_the_first_provider_failure_stops_the_run_with_a_partial_report(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    provider = FakeEmbeddingProvider()
    original = provider.embed

    async def fail_on_third(inputs: Sequence[EmbeddingInput]) -> EmbeddingResult:
        if len(provider.calls) == 2:
            provider.failures = [
                AIProviderError(
                    ModelCallErrorCategory.RATE_LIMITED,
                    "HTTP 429",
                    model="fake",
                    attempts=3,
                    usage=TokenUsage(),
                )
            ]
        return await original(inputs)

    provider.embed = fail_on_third  # type: ignore[method-assign]

    report = await run_throughput(
        provider, session_factory, SMALL_PLAN, max_requests=8, clock=_clock()
    )

    assert not report.completed
    assert report.stopped_reason == "provider failure: rate_limited"
    assert report.requests_made == 3 and len(report.samples) == 2


def test_the_chunker_measurement_reports_sizes_and_counts() -> None:
    golden, upper = run_chunker(repetitions=1, upper_bound_characters=50_000)

    assert golden.document == "golden" and (golden.characters, golden.chunks) == (20311, 9)
    assert upper.document == "upper_bound" and upper.characters >= 50_000
    assert upper.chunks > 0 and upper.wall_ms_median >= 0
