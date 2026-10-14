# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Development measurements: embedding-provider throughput and chunker speed.

    docker compose run --rm --no-deps api python -m app.retrieval.measure throughput --plan-only
    docker compose run --rm --no-deps api python -m app.retrieval.measure throughput
    docker compose run --rm --no-deps api python -m app.retrieval.measure chunker

Observations from one development environment, never production performance
claims. Inputs are synthetic generated text: no course data is ever sent.

Throughput (real provider; every request is recorded as a standalone ModelCall):
- matrix: estimated input sizes x batch sizes; per combination one warm-up
  request (not measured) and ``repetitions`` measured requests; sequential.
- a hard request budget: a plan larger than the budget is refused before any
  request, and the run stops if it would ever exceed it.
- the first failed request stops the run; the partial report is returned.

Metric definitions (frozen before any run):
- latency_ms: wall time of one provider request as observed by CourseFlow,
  around ``EmbeddingProvider.embed`` only (it includes the provider's own
  retries and any backoff sleep; recording the ModelCall is not included).
- p50/p95 latency: nearest-rank over measured requests that needed one attempt
  (non-retried). Retried requests are counted and listed separately, never
  mixed into the distribution.
- inputs_per_second: inputs in the batch / latency, per request; reported as
  the median over non-retried measured requests.
- provider_tokens_per_second: provider-reported prompt_tokens / latency, per
  request, median as above; unavailable when usage was not reported (the
  chars/4 estimate is never substituted).
- estimated_to_reported_ratio: sum of CourseFlow's estimated tokens /
  sum of provider-reported prompt_tokens, over measured requests that reported
  usage. The estimate is ``ceil(chars / 4)`` and is never called actual tokens.

Chunker (offline, no provider): wall time of ``chunk_version`` on the golden
sample document and on a synthetic ~2M-character upper-bound document; the
median of a few repetitions.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import math
import statistics
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai.embeddings import (
    DOCUMENT_INPUT_FORMAT,
    EmbeddingInput,
    EmbeddingProvider,
    EmbeddingResult,
    EmbeddingSpace,
    document_input,
)
from app.ai.errors import AIConfigurationError
from app.ai.model_calls import RecordedCallError, call_embeddings_recorded
from app.ai.nebius_embeddings import NebiusEmbeddingProvider
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.db.session import create_db_engine, create_session_factory
from app.domain.enums import BlockKind
from app.retrieval.chunking import SPAN_PACK_V1, ChunkSpan, chunk_version, estimate_tokens
from app.retrieval.samples import golden_document, layout, sentences

DEFAULT_MAX_REQUESTS = 40
_WORDS = [
    "alpha",
    "bravo",
    "charlie",
    "delta",
    "echo",
    "foxtrot",
    "golf",
    "hotel",
    "india",
    "juliet",
    "kilo",
    "lima",
    "mike",
    "november",
    "oscar",
    "papa",
    "quebec",
    "romeo",
    "sierra",
    "tango",
    "uniform",
    "victor",
    "whiskey",
    "xray",
]


class BudgetExceededError(RuntimeError):
    """The experiment would make more external requests than allowed."""


@dataclass(frozen=True, slots=True)
class ThroughputPlan:
    input_estimated_tokens: tuple[int, ...] = (200, 800)
    batch_sizes: tuple[int, ...] = (1, 8, 32, 64)
    warmups: int = 1
    repetitions: int = 3

    @property
    def requests(self) -> int:
        per_combination = self.warmups + self.repetitions
        return len(self.input_estimated_tokens) * len(self.batch_sizes) * per_combination

    @property
    def inputs(self) -> int:
        per_combination = self.warmups + self.repetitions
        return len(self.input_estimated_tokens) * sum(self.batch_sizes) * per_combination

    @property
    def estimated_tokens(self) -> int:
        per_combination = self.warmups + self.repetitions
        return per_combination * sum(
            size * batch for size in self.input_estimated_tokens for batch in self.batch_sizes
        )


@dataclass
class RequestBudget:
    max_requests: int
    used: int = 0

    def take(self) -> None:
        if self.used >= self.max_requests:
            raise BudgetExceededError(f"request budget of {self.max_requests} exhausted")
        self.used += 1


def synthetic_input(estimated_tokens: int, index: int) -> EmbeddingInput:
    """Generated text of exactly ``estimated_tokens * 4`` characters, unique per
    index. Invented words only: no course data."""
    length = estimated_tokens * 4
    words, size, position = [str(index)], len(str(index)), index  # the index leads: unique
    while size < length:
        word = _WORDS[position % len(_WORDS)]
        words.append(word)
        size += len(word) + 1
        position += 7
    text = " ".join(words)[:length].rstrip()
    text = text + "x" * (length - len(text))  # never end on a space; keep exact length
    return document_input(text)


@dataclass(frozen=True, slots=True)
class RequestSample:
    input_estimated_tokens: int
    batch_size: int
    warmup: bool
    latency_ms: float
    attempts: int
    estimated_tokens: int
    reported_prompt_tokens: int | None
    model_call_id: str


@dataclass(frozen=True, slots=True)
class CombinationResult:
    input_estimated_tokens: int
    batch_size: int
    measured_requests: int
    retried_requests: int
    retry_rate: float | None
    retried_attempts: tuple[int, ...]
    retried_latency_ms: tuple[float, ...]
    latency_ms_p50: float | None
    latency_ms_p95: float | None
    inputs_per_second: float | None
    provider_tokens_per_second: float | None  # None: unavailable (usage not reported)
    estimated_to_reported_ratio: float | None


@dataclass(frozen=True, slots=True)
class ThroughputReport:
    space: dict[str, object]
    plan: dict[str, object]
    max_requests: int
    requests_made: int
    completed: bool
    stopped_reason: str | None
    combinations: tuple[CombinationResult, ...]
    requests_without_usage: int
    reported_prompt_tokens: int | None
    assumed_usd_per_million_tokens: float | None
    computed_cost_usd: float | None
    samples: tuple[RequestSample, ...] = field(repr=False)


class _TimedProvider:
    """Times ``embed`` only, so latency excludes recording the ModelCall."""

    def __init__(self, inner: EmbeddingProvider, clock: Callable[[], float]) -> None:
        self._inner = inner
        self._clock = clock
        self.last_latency_ms = 0.0

    @property
    def space(self) -> EmbeddingSpace:
        return self._inner.space

    @property
    def max_batch_items(self) -> int:
        return self._inner.max_batch_items

    async def aclose(self) -> None:
        await self._inner.aclose()

    async def embed(self, inputs: Sequence[EmbeddingInput]) -> EmbeddingResult:
        started = self._clock()
        try:
            return await self._inner.embed(inputs)
        finally:
            self.last_latency_ms = (self._clock() - started) * 1000


def plan_summary(
    plan: ThroughputPlan, space: EmbeddingSpace, max_requests: int
) -> dict[str, object]:
    """What will be sent, before anything is sent. Safe to print."""
    return {
        "provider": space.provider,
        "model": space.model,
        "dimensions": space.dimensions,
        "space_version": space.space_version,
        "input_format": DOCUMENT_INPUT_FORMAT,
        "planned_requests": plan.requests,
        "planned_inputs": plan.inputs,
        "planned_estimated_tokens": plan.estimated_tokens,
        "input_estimated_tokens": list(plan.input_estimated_tokens),
        "batch_sizes": list(plan.batch_sizes),
        "warmups_per_combination": plan.warmups,
        "measured_per_combination": plan.repetitions,
        "max_requests": max_requests,
        "inputs": "synthetic generated text only",
    }


async def run_throughput(
    provider: EmbeddingProvider,
    session_factory: async_sessionmaker[AsyncSession],
    plan: ThroughputPlan,
    *,
    max_requests: int = DEFAULT_MAX_REQUESTS,
    assumed_usd_per_million_tokens: float | None = None,
    clock: Callable[[], float] = time.perf_counter,
) -> ThroughputReport:
    if plan.requests > max_requests:
        raise BudgetExceededError(
            f"the plan needs {plan.requests} requests; the budget is {max_requests}"
        )
    budget = RequestBudget(max_requests)
    timed = _TimedProvider(provider, clock)
    samples: list[RequestSample] = []
    stopped: str | None = None
    index = 0
    try:
        for size in plan.input_estimated_tokens:
            for batch in plan.batch_sizes:
                for repetition in range(plan.warmups + plan.repetitions):
                    budget.take()
                    inputs = [synthetic_input(size, index + i) for i in range(batch)]
                    index += batch
                    recorded = await call_embeddings_recorded(timed, session_factory, inputs)
                    result = recorded.result
                    samples.append(
                        RequestSample(
                            input_estimated_tokens=size,
                            batch_size=batch,
                            warmup=repetition < plan.warmups,
                            latency_ms=round(timed.last_latency_ms, 3),
                            attempts=result.attempts,
                            estimated_tokens=sum(estimate_tokens(i.text) for i in inputs),
                            reported_prompt_tokens=result.usage.prompt_tokens,
                            model_call_id=str(recorded.model_call_id),
                        )
                    )
    except RecordedCallError as failed:
        stopped = f"provider failure: {failed.error.category.value}"
    except BudgetExceededError as exhausted:  # pragma: no cover - the plan was checked
        stopped = str(exhausted)
    measured = [s for s in samples if not s.warmup]
    with_usage = [s for s in samples if s.reported_prompt_tokens is not None]
    reported = sum(s.reported_prompt_tokens or 0 for s in with_usage) if with_usage else None
    return ThroughputReport(
        space=dataclasses.asdict(provider.space),
        plan=dataclasses.asdict(plan),
        max_requests=max_requests,
        requests_made=budget.used,
        completed=stopped is None,
        stopped_reason=stopped,
        combinations=tuple(
            _combination(
                size,
                batch,
                [s for s in measured if (s.input_estimated_tokens, s.batch_size) == (size, batch)],
            )
            for size in plan.input_estimated_tokens
            for batch in plan.batch_sizes
        ),
        requests_without_usage=len(samples) - len(with_usage),
        reported_prompt_tokens=reported,
        assumed_usd_per_million_tokens=assumed_usd_per_million_tokens,
        computed_cost_usd=(
            None
            if reported is None or assumed_usd_per_million_tokens is None
            else reported * assumed_usd_per_million_tokens / 1_000_000
        ),
        samples=tuple(samples),
    )


def _combination(size: int, batch: int, measured: Sequence[RequestSample]) -> CombinationResult:
    plain = [s for s in measured if s.attempts == 1]
    retried = [s for s in measured if s.attempts > 1]
    latencies = sorted(s.latency_ms for s in plain)
    rates = [s.batch_size / (s.latency_ms / 1000) for s in plain if s.latency_ms > 0]
    token_rates = [
        s.reported_prompt_tokens / (s.latency_ms / 1000)
        for s in plain
        if s.reported_prompt_tokens is not None and s.latency_ms > 0
    ]
    with_usage = [s for s in measured if s.reported_prompt_tokens]
    reported = sum(s.reported_prompt_tokens or 0 for s in with_usage)
    return CombinationResult(
        input_estimated_tokens=size,
        batch_size=batch,
        measured_requests=len(measured),
        retried_requests=len(retried),
        retry_rate=len(retried) / len(measured) if measured else None,
        retried_attempts=tuple(s.attempts for s in retried),
        retried_latency_ms=tuple(s.latency_ms for s in retried),
        latency_ms_p50=_nearest_rank(latencies, 0.50),
        latency_ms_p95=_nearest_rank(latencies, 0.95),
        inputs_per_second=statistics.median(rates) if rates else None,
        provider_tokens_per_second=statistics.median(token_rates) if token_rates else None,
        estimated_to_reported_ratio=(
            sum(s.estimated_tokens for s in with_usage) / reported if reported else None
        ),
    )


def _nearest_rank(sorted_values: Sequence[float], quantile: float) -> float | None:
    if not sorted_values:
        return None
    return sorted_values[max(0, math.ceil(quantile * len(sorted_values)) - 1)]


# --- chunker ------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ChunkerSample:
    document: str
    characters: int
    spans: int
    chunks: int
    repetitions: int
    wall_ms_median: float
    chunks_per_second: float | None


def upper_bound_document(target_characters: int = 2_000_000) -> tuple[str, list[ChunkSpan]]:
    """Synthetic pages of sentences, about ``target_characters`` long."""
    pages, size, first = [], 0, 0
    while size < target_characters:
        page = sentences(first, 25) + "\n\n" + sentences(first + 25, 25)
        pages.append((BlockKind.PAGE, page, None))
        size += len(page) + 2
        first += 50
    return layout(pages)


def run_chunker(
    *,
    repetitions: int = 5,
    upper_bound_characters: int = 2_000_000,
    clock: Callable[[], float] = time.perf_counter,
) -> tuple[ChunkerSample, ...]:
    documents = {
        "golden": golden_document(),
        "upper_bound": upper_bound_document(upper_bound_characters),
    }
    results = []
    for name, (text, spans) in documents.items():
        timings, chunks = [], 0
        for _ in range(repetitions):
            started = clock()
            chunks = len(chunk_version(text, spans, SPAN_PACK_V1))
            timings.append((clock() - started) * 1000)
        median = statistics.median(timings)
        results.append(
            ChunkerSample(
                document=name,
                characters=len(text),
                spans=len(spans),
                chunks=chunks,
                repetitions=repetitions,
                wall_ms_median=round(median, 3),
                chunks_per_second=round(chunks / (median / 1000), 1) if median > 0 else None,
            )
        )
    return tuple(results)


# --- command line ---------------------------------------------------------------------------------


def _emit(payload: object) -> None:
    print(json.dumps(payload, default=str, sort_keys=True))


async def _throughput(args: argparse.Namespace) -> int:
    settings = get_settings()
    configure_logging(settings.log_level, stream=sys.stderr)
    provider = NebiusEmbeddingProvider.from_settings(settings)
    plan = ThroughputPlan()
    print(
        json.dumps(
            {"experiment_plan": plan_summary(plan, provider.space, args.max_requests)},
            sort_keys=True,
        )
    )
    if plan.requests > args.max_requests:
        _emit({"status": "refused", "reason": "plan exceeds the request budget"})
        await provider.aclose()
        return 4
    if args.plan_only:
        await provider.aclose()
        return 0
    engine = create_db_engine(settings)
    try:
        report = await run_throughput(
            provider,
            create_session_factory(engine),
            plan,
            max_requests=args.max_requests,
            assumed_usd_per_million_tokens=args.assumed_usd_per_million_tokens,
        )
    finally:
        await provider.aclose()
        await engine.dispose()
    payload = dataclasses.asdict(report)
    payload.pop("samples")
    _emit({"status": "completed" if report.completed else "stopped", "report": payload})
    return 0 if report.completed else 5


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.retrieval.measure")
    commands = parser.add_subparsers(dest="command", required=True)
    throughput = commands.add_parser("throughput", help="real-provider embedding throughput")
    throughput.add_argument("--max-requests", type=int, default=DEFAULT_MAX_REQUESTS)
    throughput.add_argument("--plan-only", action="store_true", help="print the plan; send nothing")
    throughput.add_argument(
        "--assumed-usd-per-million-tokens",
        type=float,
        default=None,
        help="a catalog price assumption for the computed cost (not a billing figure)",
    )
    chunker = commands.add_parser("chunker", help="offline chunker wall time")
    chunker.add_argument("--repetitions", type=int, default=5)
    args = parser.parse_args(argv)
    if args.command == "chunker":
        _emit(
            {
                "status": "completed",
                "chunker": [
                    dataclasses.asdict(s) for s in run_chunker(repetitions=args.repetitions)
                ],
            }
        )
        return 0
    try:
        return asyncio.run(_throughput(args))
    except AIConfigurationError as exc:
        _emit({"status": "error", "error": "provider_not_configured", "detail": str(exc)})
        return 2


if __name__ == "__main__":
    sys.exit(main())
