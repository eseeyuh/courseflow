# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Plan, and only when explicitly asked, record an embedding snapshot of a
frozen retrieval dataset (development).

    python -m app.evaluation.retrieval.record --dataset <dir> --snapshot <file.jsonl> \\
        --expect-dataset-sha <sha256> --price-usd-per-million <price> [--live]

Without ``--live`` (the default) it prints the plan and makes no request:
nothing needs an API key. The plan is computed, never assumed: the expected
inputs come from the production builders, the batches from the production
batch planner and limits, the attempts from the configured retry policy.
A plan outside the hard bounds is refused before any request:

- at most ``MAX_LOGICAL_REQUESTS`` logical requests and ``MAX_HTTP_ATTEMPTS``
  HTTP attempts (every logical request may use the full retry policy);
- planned maximum spend below ``MAX_SPEND_USD``. The maximum assumes every
  attempt of every batch is billed for ``ESTIMATE_MARGIN`` x its estimated
  tokens. The price is a required argument, never a stored default: it must
  be re-verified against the provider's current catalog before a live run.

With ``--live``, after every response the spend so far (provider-reported
tokens; the worst case for a batch that reported none) plus the worst case
of the remaining batches (at the larger of the margin and the observed
reported/estimated ratio) must stay under the bound, or recording stops
before the next batch. Any failure stops recording.

Publication is atomic: the snapshot and its sidecar are written to
temporary files in the destination directory, verified, then renamed into
place, snapshot first. On any failure neither final file exists. An
existing snapshot or sidecar is never overwritten. Provider request IDs
appear only in logs, never in the snapshot.

Exit codes: 0 ok (plan printed, or snapshot recorded); 1 unexpected error;
3 invalid dataset or snapshot; 4 plan or budget refused; 5 recording failed;
64 usage.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import sys
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from app.ai.embeddings import EmbeddingInput, EmbeddingProvider, EmbeddingSpace
from app.ai.errors import AIProviderError
from app.evaluation.retrieval.dataset import Dataset, DatasetError, load_dataset
from app.evaluation.retrieval.snapshot import (
    SNAPSHOT_SCHEMA_VERSION,
    VECTOR_ENCODING,
    ExpectedInputs,
    InputKey,
    SnapshotDataset,
    SnapshotError,
    SnapshotHeader,
    SnapshotRecording,
    SnapshotSpace,
    SnapshotSubject,
    encode_vector,
    expected_inputs,
    file_sha256,
    input_key,
    inputs_sha256,
    parse_snapshot_bytes,
    serialize,
    sidecar_bytes,
    sidecar_path,
    subject_of,
)
from app.retrieval.batching import plan_batches
from app.retrieval.chunking import estimate_tokens

RECORDER_TOOL = "courseflow-snapshot-recorder/v1"
MAX_LOGICAL_REQUESTS = 2
MAX_HTTP_ATTEMPTS = 6
MAX_SPEND_USD = Decimal("0.01")
# chars/4 under-estimated provider tokens by about 5-6% on synthetic text
# (development measurement); the bound allows far more than that.
ESTIMATE_MARGIN = Decimal("1.25")
_MILLION = Decimal(1_000_000)

EXIT_OK = 0
EXIT_UNEXPECTED = 1
EXIT_INVALID = 3
EXIT_REFUSED = 4
EXIT_RECORDING_FAILED = 5
EXIT_USAGE = 64


class RecordingRefused(Exception):
    """The plan or the remaining budget is outside the hard bounds; no
    further request is made."""

    def __init__(self, problems: Sequence[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = tuple(problems)


class RecordingFailed(Exception):
    """A request or a check failed after recording started; nothing was published."""


# --- the plan -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PlannedBatch:
    input_format: str
    items: tuple[EmbeddingInput, ...]

    @property
    def estimated_tokens(self) -> int:
        return sum(estimate_tokens(item.text) for item in self.items)


@dataclass(frozen=True, slots=True)
class RecordingPlan:
    dataset: Dataset | SnapshotSubject
    space: EmbeddingSpace
    inputs: ExpectedInputs
    batches: tuple[PlannedBatch, ...]
    attempts_per_request: int
    price_usd_per_million: Decimal
    output: Path

    @property
    def logical_requests(self) -> int:
        return len(self.batches)

    @property
    def max_http_attempts(self) -> int:
        return self.logical_requests * self.attempts_per_request

    @property
    def estimated_tokens(self) -> int:
        return sum(batch.estimated_tokens for batch in self.batches)

    def cost(self, tokens: Decimal | int) -> Decimal:
        return Decimal(tokens) * self.price_usd_per_million / _MILLION

    def worst_case_usd(self, batches: Sequence[PlannedBatch], ratio: Decimal) -> Decimal:
        tokens = sum(Decimal(b.estimated_tokens) for b in batches)
        return self.cost(tokens * self.attempts_per_request * ratio)

    @property
    def expected_spend_usd(self) -> Decimal:
        return self.cost(self.estimated_tokens)

    @property
    def max_spend_usd(self) -> Decimal:
        return self.worst_case_usd(self.batches, ESTIMATE_MARGIN)

    def problems(self) -> list[str]:
        problems = []
        if self.logical_requests > MAX_LOGICAL_REQUESTS:
            problems.append(
                f"plan needs {self.logical_requests} logical requests; "
                f"the bound is {MAX_LOGICAL_REQUESTS}"
            )
        if self.max_http_attempts > MAX_HTTP_ATTEMPTS:
            problems.append(
                f"plan allows {self.max_http_attempts} HTTP attempts; "
                f"the bound is {MAX_HTTP_ATTEMPTS}"
            )
        if self.max_spend_usd >= MAX_SPEND_USD:
            problems.append(
                f"planned maximum spend ${self.max_spend_usd:.6f} is not below ${MAX_SPEND_USD}"
            )
        return problems

    def describe(self) -> dict[str, Any]:
        """The deterministic plan report (no timestamps, no credentials)."""
        counts = self.inputs.counts()
        return {
            "dataset": {
                "name": subject_of(self.dataset).name,
                "version": subject_of(self.dataset).version,
                "sha256": subject_of(self.dataset).sha256,
            },
            "embedding_space": SnapshotSpace.of(self.space).model_dump(),
            "inputs": {
                "counts": counts,
                "total_unique_keys": len(self.inputs.keys),
                "inputs_hash_scheme": "sha256-input-keys/v1",
                "inputs_sha256": inputs_sha256(self.inputs.keys),
            },
            "batches": [
                {
                    "input_format": b.input_format,
                    "inputs": len(b.items),
                    "estimated_tokens": b.estimated_tokens,
                }
                for b in self.batches
            ],
            "batch_counts": {
                fmt: sum(1 for b in self.batches if b.input_format == fmt) for fmt in counts
            },
            "estimated_tokens_total": self.estimated_tokens,
            "token_estimate": "ceil(chars / 4) per input; not a tokenizer count",
            "logical_requests": self.logical_requests,
            "logical_request_cap": MAX_LOGICAL_REQUESTS,
            "attempts_per_request": self.attempts_per_request,
            "max_http_attempts": self.max_http_attempts,
            "http_attempt_cap": MAX_HTTP_ATTEMPTS,
            "price_usd_per_million_input_tokens": str(self.price_usd_per_million),
            "expected_spend_usd": f"{self.expected_spend_usd:.6f}",
            "max_spend_usd": f"{self.max_spend_usd:.6f}",
            "max_spend_assumes": (f"every attempt billed for {ESTIMATE_MARGIN} x estimated tokens"),
            "spend_guard_usd": str(MAX_SPEND_USD),
            "output": {
                "snapshot": self.output.as_posix(),
                "sidecar": sidecar_path(self.output).as_posix(),
            },
            "problems": self.problems(),
        }


def plan_recording(
    dataset: Dataset | SnapshotSubject,
    space: EmbeddingSpace,
    *,
    batch_max_items: int,
    batch_max_estimated_tokens: int,
    attempts_per_request: int,
    price_usd_per_million: Decimal,
    output: Path,
    inputs: ExpectedInputs | None = None,
) -> RecordingPlan:
    """Documents, then queries, each split by the production batch planner."""
    if attempts_per_request < 1:
        raise ValueError("attempts_per_request must be >= 1")
    if not price_usd_per_million.is_finite() or price_usd_per_million < 0:
        raise ValueError("price must be a finite, non-negative number")
    if inputs is None:
        if isinstance(dataset, SnapshotSubject):
            raise ValueError("a SnapshotSubject needs its expected inputs")
        inputs = expected_inputs(dataset)
    batches: list[PlannedBatch] = []
    for group in (inputs.documents, inputs.queries):
        for batch in plan_batches(
            group, max_items=batch_max_items, max_estimated_tokens=batch_max_estimated_tokens
        ):
            batches.append(PlannedBatch(batch[0].input_format, tuple(batch)))
    return RecordingPlan(
        dataset=dataset,
        space=space,
        inputs=inputs,
        batches=tuple(batches),
        attempts_per_request=attempts_per_request,
        price_usd_per_million=price_usd_per_million,
        output=output,
    )


# --- recording ----------------------------------------------------------------------------


@dataclass(slots=True)
class _Progress:
    requests: int = 0
    http_attempts: int = 0
    reported_tokens: int | None = 0
    # Spend so far: reported tokens, or the batch's worst case when unreported.
    spent_usd: Decimal = Decimal(0)
    estimated_done: int = 0  # estimated tokens of batches that reported usage
    reported_done: int = 0  # their reported tokens
    served_models: set[str] = field(default_factory=set)
    vectors: dict[InputKey, str] = field(default_factory=dict)

    def ratio(self) -> Decimal:
        if self.estimated_done == 0:
            return ESTIMATE_MARGIN
        return max(ESTIMATE_MARGIN, Decimal(self.reported_done) / Decimal(self.estimated_done))


@dataclass(frozen=True, slots=True)
class RecordingResult:
    snapshot: Path
    sidecar: Path
    sha256: str
    header: SnapshotHeader
    spent_usd: Decimal


def _guard_next(
    plan: RecordingPlan, progress: _Progress, remaining: Sequence[PlannedBatch]
) -> None:
    problems = []
    if progress.requests + len(remaining) > MAX_LOGICAL_REQUESTS:
        problems.append("the logical request bound would be exceeded")
    if progress.http_attempts + len(remaining) * plan.attempts_per_request > MAX_HTTP_ATTEMPTS:
        problems.append("the HTTP attempt bound could be exceeded")
    projected = progress.spent_usd + plan.worst_case_usd(remaining, progress.ratio())
    if projected >= MAX_SPEND_USD:
        problems.append(
            f"spend so far ${progress.spent_usd:.6f} plus the remaining worst case would reach "
            f"${projected:.6f}, not below ${MAX_SPEND_USD}"
        )
    if problems:
        raise RecordingRefused(problems)


def _account(
    plan: RecordingPlan, progress: _Progress, batch: PlannedBatch, attempts: int, tokens: int | None
) -> None:
    progress.http_attempts += attempts
    if tokens is None:
        progress.reported_tokens = None
        progress.spent_usd += plan.worst_case_usd([batch], ESTIMATE_MARGIN)
    else:
        if progress.reported_tokens is not None:
            progress.reported_tokens += tokens
        progress.spent_usd += plan.cost(tokens)
        progress.estimated_done += batch.estimated_tokens
        progress.reported_done += tokens


async def record_snapshot(
    plan: RecordingPlan,
    provider: EmbeddingProvider,
    *,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    replace: Callable[[Path, Path], None] = os.replace,
) -> RecordingResult:
    """Make the plan's requests, then publish the snapshot atomically."""
    problems = plan.problems()
    if provider.space != plan.space:
        problems.append("the provider's space is not the planned space")
    if any(len(batch.items) > provider.max_batch_items for batch in plan.batches):
        problems.append("a planned batch exceeds the provider's batch limit")
    problems.extend(existing_outputs(plan.output))
    if problems:
        raise RecordingRefused(problems)

    progress = _Progress()
    for index, batch in enumerate(plan.batches):
        _guard_next(plan, progress, plan.batches[index:])
        try:
            result = await provider.embed(batch.items)
        except AIProviderError as error:
            progress.requests += 1
            _account(plan, progress, batch, error.attempts, error.usage.prompt_tokens)
            raise RecordingFailed(
                f"batch {index + 1}/{plan.logical_requests} failed: {error.category.value}"
            ) from error
        progress.requests += 1
        _account(plan, progress, batch, result.attempts, result.usage.prompt_tokens)
        if result.attempts > plan.attempts_per_request:
            raise RecordingFailed("a request used more attempts than the retry policy allows")
        if result.space != plan.space:
            raise RecordingFailed("the provider answered for a different space")
        if len(result.vectors) != len(batch.items):
            raise RecordingFailed(f"batch {index + 1}: wrong number of vectors")
        if result.served_model:
            progress.served_models.add(result.served_model)
        for item, vector in zip(batch.items, result.vectors, strict=True):
            try:
                progress.vectors[input_key(item)] = encode_vector(vector, plan.space.dimensions)
            except ValueError as exc:
                raise RecordingFailed(f"batch {index + 1}: unusable vector ({exc})") from None

    header = _header(plan, progress, now())
    data = serialize(header, progress.vectors)
    sha = file_sha256(data)
    try:  # the exact loader checks, on the exact bytes about to be published
        parse_snapshot_bytes(
            plan.output, sha, data, dataset=plan.dataset, space=plan.space, inputs=plan.inputs
        )
    except SnapshotError as exc:
        raise RecordingFailed(f"the recorded snapshot does not validate: {exc}") from exc
    publish(plan.output, data, sha, replace=replace)
    return RecordingResult(plan.output, sidecar_path(plan.output), sha, header, progress.spent_usd)


def _header(plan: RecordingPlan, progress: _Progress, when: datetime) -> SnapshotHeader:
    subject = subject_of(plan.dataset)
    return SnapshotHeader(
        schema_version=SNAPSHOT_SCHEMA_VERSION,
        dataset=SnapshotDataset(
            name=subject.name,
            version=subject.version,
            sha256=subject.sha256,
            hash_scheme=subject.hash_scheme,
        ),
        chunker=subject.chunker,
        space=SnapshotSpace.of(plan.space),
        vector_encoding=VECTOR_ENCODING,
        inputs_hash_scheme="sha256-input-keys/v1",
        inputs_sha256=inputs_sha256(plan.inputs.keys),
        record_counts=plan.inputs.counts(),
        recording=SnapshotRecording(
            tool=RECORDER_TOOL,
            recorded_at=when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            served_models=tuple(sorted(progress.served_models)),
            logical_requests=progress.requests,
            http_attempts=progress.http_attempts,
            reported_prompt_tokens=progress.reported_tokens,
        ),
    )


# --- publication --------------------------------------------------------------------------


def existing_outputs(path: Path) -> list[str]:
    return [
        f"{p.as_posix()} already exists; re-recording is not supported"
        for p in (path, sidecar_path(path))
        if p.exists() or p.is_symlink()
    ]


def _write_temp(directory: Path, name: str, data: bytes) -> Path:
    fd, raw = tempfile.mkstemp(dir=directory, prefix=f".{name}.", suffix=".tmp")
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    return Path(raw)


def publish(
    path: Path, data: bytes, sha: str, *, replace: Callable[[Path, Path], None] = os.replace
) -> None:
    """Write the snapshot and its sidecar so that either both final files
    exist and are correct, or neither does."""
    sidecar = sidecar_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temps: list[Path] = []
    try:
        temps.append(_write_temp(path.parent, path.name, data))
        temps.append(_write_temp(path.parent, sidecar.name, sidecar_bytes(sha)))
        if file_sha256(temps[0].read_bytes()) != sha:
            raise RecordingFailed("the written snapshot does not hash to the recorded SHA")
        existing = existing_outputs(path)
        if existing:
            raise RecordingRefused(existing)
        replace(temps[0], path)
        try:
            replace(temps[1], sidecar)
        except BaseException:
            with contextlib.suppress(OSError):
                path.unlink()
            raise
    finally:
        for temp in temps:
            with contextlib.suppress(OSError):
                temp.unlink(missing_ok=True)


# --- command line -------------------------------------------------------------------------


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> Any:  # argparse would exit with 2
        _emit({"status": "error", "error": "usage", "detail": message})
        raise SystemExit(EXIT_USAGE)


def parse_price(value: str) -> Decimal:
    try:
        price = Decimal(value)
    except InvalidOperation:
        raise argparse.ArgumentTypeError("not a decimal number") from None
    if not price.is_finite() or price < 0:
        raise argparse.ArgumentTypeError("must be a finite, non-negative number")
    return price


def _parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(prog="python -m app.evaluation.retrieval.record")
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--snapshot", type=Path, required=True, help="destination .jsonl")
    parser.add_argument("--expect-dataset-sha", required=True)
    parser.add_argument(
        "--price-usd-per-million",
        type=parse_price,
        required=True,
        help="current catalog input price, verified before a live run",
    )
    parser.add_argument("--live", action="store_true", help="make the paid provider requests")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    from app.ai.nebius_embeddings import NebiusEmbeddingProvider, embedding_space_from_settings
    from app.core.config import get_settings

    args = _parser().parse_args(argv)
    try:
        dataset = load_dataset(args.dataset.resolve())
        if dataset.sha256 != args.expect_dataset_sha:
            _emit(
                {
                    "status": "error",
                    "error": "dataset_mismatch",
                    "detail": f"dataset sha256 {dataset.sha256} != expected",
                }
            )
            return EXIT_INVALID
        settings = get_settings()
        plan = plan_recording(
            dataset,
            embedding_space_from_settings(settings),
            batch_max_items=settings.embedding_batch_max_items,
            batch_max_estimated_tokens=settings.embedding_batch_max_estimated_tokens,
            attempts_per_request=settings.ai_max_attempts,
            price_usd_per_million=args.price_usd_per_million,
            output=args.snapshot,
        )
        report = plan.describe()
        report["existing_output"] = existing_outputs(args.snapshot)
        if not args.live:
            refused = bool(report["problems"] or report["existing_output"])
            _emit({"status": "refused" if refused else "planned", "live": False, "plan": report})
            return EXIT_REFUSED if refused else EXIT_OK

        async def live() -> RecordingResult:
            provider = NebiusEmbeddingProvider.from_settings(settings)
            try:
                return await record_snapshot(plan, provider)
            finally:
                await provider.aclose()

        result = asyncio.run(live())
        _emit(
            {
                "status": "recorded",
                "live": True,
                "plan": report,
                "snapshot": result.snapshot.as_posix(),
                "sidecar": result.sidecar.as_posix(),
                "snapshot_sha256": result.sha256,
                "recording": result.header.recording.model_dump(),
                "spent_usd_from_reported_tokens": f"{result.spent_usd:.6f}",
            }
        )
    except (DatasetError, SnapshotError) as exc:
        _emit({"status": "error", "error": "invalid", "problems": list(exc.problems)})
        return EXIT_INVALID
    except RecordingRefused as exc:
        _emit({"status": "refused", "problems": list(exc.problems)})
        return EXIT_REFUSED
    except RecordingFailed as exc:
        _emit({"status": "error", "error": "recording_failed", "detail": str(exc)})
        return EXIT_RECORDING_FAILED
    except Exception as exc:  # noqa: BLE001 - report the type only
        _emit({"status": "error", "error": "unexpected", "type": type(exc).__name__})
        return EXIT_UNEXPECTED
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
