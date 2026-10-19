# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Embedding snapshots of the frozen retrieval dataset: the expected inputs,
the file format and hashes, the recorder's plan, bounds and atomic
publication (with a fake provider: no request leaves the process), and the
replay provider. No retrieval is run and nothing is scored."""

from __future__ import annotations

import ast
import base64
import hashlib
import json
import random
import socket
import struct
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app.ai.embeddings import (
    DOCUMENT_INPUT_FORMAT,
    QUERY_INPUT_FORMAT,
    EmbeddingInput,
    EmbeddingResult,
    EmbeddingSpace,
    document_input,
    query_input,
)
from app.ai.errors import AIProviderError
from app.ai.nebius_transport import PROVIDER_NAME
from app.ai.provider import TokenUsage
from app.domain.enums import ModelCallErrorCategory
from app.evaluation.retrieval import replay as replay_module
from app.evaluation.retrieval.dataset import Dataset, load_dataset
from app.evaluation.retrieval.record import (
    MAX_SPEND_USD,
    RecordingFailed,
    RecordingPlan,
    RecordingRefused,
    plan_recording,
    record_snapshot,
)
from app.evaluation.retrieval.replay import (
    REPLAY_REQUEST_ID_PREFIX,
    ReplayEmbeddingProvider,
    ReplayInputError,
    load_replay_provider,
)
from app.evaluation.retrieval.snapshot import (
    ExpectedInputs,
    SnapshotError,
    decode_vector,
    encode_vector,
    expected_inputs,
    file_sha256,
    inputs_sha256,
    load_snapshot,
    sidecar_path,
    version_chunk_texts,
)
from app.retrieval.chunking import SPAN_PACK_V1
from app.retrieval.search import SearchRequest

DATASET_DIR = Path(__file__).resolve().parents[2] / "evals" / "datasets" / "retrieval" / "v1"
FROZEN_SHA = "07c11efaea7efd516f6d6ec0bdb27229008bd0f1972890fb82d4827e0ba45747"
FROZEN_INPUTS_SHA = "66da4dcb4121b3fb28a458a431376dfbc954b40e07fab314bdf353b17838fdea"
SPACE = EmbeddingSpace(PROVIDER_NAME, "Qwen/Qwen3-Embedding-8B", 4096, "v1")
SERVED = "Qwen/Qwen3-Embedding-8B"
PROVIDER_REQUEST_ID = "req-0f3c9a-provider-side"
CLOCK = datetime(2026, 10, 19, 12, 0, 0, tzinfo=UTC)

pytestmark = pytest.mark.anyio


@pytest.fixture(scope="module")
def dataset() -> Dataset:
    return load_dataset(DATASET_DIR)


@pytest.fixture(scope="module")
def inputs(dataset: Dataset) -> ExpectedInputs:
    return expected_inputs(dataset)


def fake_vector(input_hash: str, dimensions: int = SPACE.dimensions) -> tuple[float, ...]:
    """Deterministic, float32-exact, never all zero."""
    rng = random.Random(input_hash)
    values = struct.pack(f"<{dimensions}f", *(rng.uniform(-1, 1) for _ in range(dimensions)))
    return struct.unpack(f"<{dimensions}f", values)


class FakeProvider:
    """Stands in for the live provider: counts calls, never touches a network."""

    def __init__(
        self,
        *,
        space: EmbeddingSpace = SPACE,
        fail_on_call: int | None = None,
        attempts: int = 1,
        tokens: Any = "estimate",
        max_batch_items: int = 64,
        vector: Any = fake_vector,
    ) -> None:
        self._space = space
        self._fail_on_call = fail_on_call
        self._attempts = attempts
        self._tokens = tokens
        self._max_batch_items = max_batch_items
        self._vector = vector
        self.calls: list[list[EmbeddingInput]] = []

    @property
    def space(self) -> EmbeddingSpace:
        return self._space

    @property
    def max_batch_items(self) -> int:
        return self._max_batch_items

    async def embed(self, inputs: Sequence[EmbeddingInput]) -> EmbeddingResult:
        self.calls.append(list(inputs))
        if self._fail_on_call == len(self.calls):
            raise AIProviderError(
                ModelCallErrorCategory.SERVER_ERROR,
                "HTTP 503",
                model=SERVED,
                attempts=3,
                usage=TokenUsage(),
            )
        if self._tokens == "estimate":
            tokens: int | None = sum(-(-len(i.text) // 4) for i in inputs)
        elif callable(self._tokens):
            tokens = self._tokens(len(self.calls))
        else:
            tokens = self._tokens
        return EmbeddingResult(
            vectors=tuple(self._vector(i.input_hash) for i in inputs),
            space=self._space,
            served_model=SERVED,
            usage=TokenUsage(prompt_tokens=tokens, completion_tokens=0),
            attempts=self._attempts,
            request_id=PROVIDER_REQUEST_ID,
        )

    async def aclose(self) -> None:
        return None


def make_plan(
    dataset: Dataset, inputs: ExpectedInputs, output: Path, **overrides: Any
) -> RecordingPlan:
    values: dict[str, Any] = {
        "batch_max_items": 64,
        "batch_max_estimated_tokens": 32_000,
        "attempts_per_request": 3,
        "price_usd_per_million": Decimal("0.01"),
        "output": output,
        "inputs": inputs,
    } | overrides
    return plan_recording(dataset, SPACE, **values)


async def record(
    dataset: Dataset, inputs: ExpectedInputs, output: Path, provider: Any = None, **overrides: Any
) -> Any:
    plan = make_plan(dataset, inputs, output, **overrides)
    return await record_snapshot(plan, provider or FakeProvider(), now=lambda: CLOCK)


@pytest.fixture
async def recorded(dataset: Dataset, inputs: ExpectedInputs, tmp_path: Path) -> Path:
    path = tmp_path / "snap" / "snapshot.jsonl"
    await record(dataset, inputs, path)
    return path


def lines(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_bytes().decode("utf-8").splitlines()]


def rewrite(path: Path, records: list[dict[str, Any]], *, raw: bytes | None = None) -> None:
    """Write new snapshot bytes and a matching sidecar, so a test reaches the rule it is about."""
    data = (
        raw
        if raw is not None
        else "".join(
            json.dumps(r, sort_keys=True, separators=(",", ":")) + "\n" for r in records
        ).encode("utf-8")
    )
    path.write_bytes(data)
    sidecar_path(path).write_bytes(f"{file_sha256(data)}\n".encode("ascii"))


def problems(
    path: Path, dataset: Dataset, inputs: ExpectedInputs, space: EmbeddingSpace = SPACE
) -> str:
    with pytest.raises(SnapshotError) as caught:
        load_snapshot(path, dataset=dataset, space=space, inputs=inputs)
    return "; ".join(caught.value.problems)


# --- the expected inputs ------------------------------------------------------------------


def test_the_expected_input_set_is_the_frozen_55_keys(
    dataset: Dataset, inputs: ExpectedInputs
) -> None:
    assert dataset.sha256 == FROZEN_SHA
    assert inputs.counts() == {DOCUMENT_INPUT_FORMAT: 41, QUERY_INPUT_FORMAT: 14}
    assert len(inputs.keys) == 55 == len(inputs.all)  # no duplicate keys
    assert {i.input_format for i in inputs.documents} == {DOCUMENT_INPUT_FORMAT}
    assert {i.input_format for i in inputs.queries} == {QUERY_INPUT_FORMAT}
    again = expected_inputs(dataset)
    assert again == inputs  # same inputs, same order
    assert inputs_sha256(inputs.keys) == FROZEN_INPUTS_SHA


def test_document_inputs_are_production_document_inputs_of_every_version(
    dataset: Dataset, inputs: ExpectedInputs
) -> None:
    texts = [
        text
        for course in dataset.manifest.courses
        for resource in course.resources
        for entry in resource.versions
        for text in version_chunk_texts(
            dataset, course.key, resource.key, entry.version, SPAN_PACK_V1
        )
    ]
    assert len(texts) == dataset.manifest.expected_chunks.all_versions
    assert list(inputs.documents) == [document_input(text) for text in texts]
    for item in inputs.documents:  # the hash is of the exact UTF-8 provider bytes
        assert item.input_hash == hashlib.sha256(item.text.encode("utf-8")).hexdigest()


def test_query_inputs_are_what_search_would_send_for_each_case(
    dataset: Dataset, inputs: ExpectedInputs
) -> None:
    """Search embeds ``query_input(SearchRequest.query)``; for every case that is
    exactly the snapshot's query input (validated offline, nothing is searched)."""
    import uuid

    sent = [
        query_input(SearchRequest.build(course_id=uuid.uuid4(), query=case.query).query)
        for case in dataset.cases
    ]
    assert list(inputs.queries) == sent
    assert all(item.text.startswith("Instruct: ") for item in inputs.queries)


def test_inputs_sha256_matches_an_independent_implementation(inputs: ExpectedInputs) -> None:
    stream = b"sha256-input-keys/v1\n" + b"".join(
        fmt.encode() + b"\0" + digest.encode() + b"\n"
        for fmt, digest in sorted(inputs.keys, key=lambda k: (k[0].encode(), k[1].encode()))
    )
    assert inputs_sha256(inputs.keys) == hashlib.sha256(stream).hexdigest()
    assert inputs_sha256(sorted(inputs.keys, reverse=True)) == inputs_sha256(inputs.keys)
    assert inputs_sha256([]) == hashlib.sha256(b"sha256-input-keys/v1\n").hexdigest()
    with pytest.raises(ValueError, match="distinct"):
        inputs_sha256([("query/v1", "a" * 64)] * 2)
    with pytest.raises(ValueError, match="malformed"):
        inputs_sha256([("query/v1\n", "a" * 64)])


# --- vectors ------------------------------------------------------------------------------


def test_float32_little_endian_base64_round_trip() -> None:
    encoded = encode_vector([1.0, -2.5, 0.1], 3)
    assert base64.b64decode(encoded) == struct.pack("<3f", 1.0, -2.5, 0.1)
    assert base64.b64decode(encoded)[:4] == b"\x00\x00\x80\x3f"  # 1.0, little-endian
    assert decode_vector(encoded, 3) == struct.unpack("<3f", struct.pack("<3f", 1.0, -2.5, 0.1))


@pytest.mark.parametrize(
    ("vector", "message"),
    [
        ([1.0, 2.0], "dimensions"),
        ([1.0, 2.0, 3.0, 4.0], "dimensions"),
        ([1.0, float("nan"), 0.0], "non-finite"),
        ([1.0, float("inf"), 0.0], "non-finite"),
        ([0.0, 0.0, 0.0], "all zeros"),
        ([1e-50, 0.0, 0.0], "all zeros"),  # rounds to zero in float32
        ([1e39, 0.0, 0.0], "float32 range"),
        ([True, 1.0, 1.0], "numbers"),
    ],
)
def test_unrecordable_vectors_are_rejected(vector: list[float], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        encode_vector(vector, 3)


@pytest.mark.parametrize(
    ("encoded", "message"),
    [
        ("not base64!", "base64"),
        (base64.b64encode(struct.pack("<3f", 1, 2, 3)).decode()[:-1], "base64"),
        (base64.b64encode(struct.pack("<2f", 1, 2)).decode(), "bytes"),
        (base64.b64encode(struct.pack("<3f", 1, float("nan"), 3)).decode(), "non-finite"),
        (base64.b64encode(struct.pack("<3f", 0, 0, 0)).decode(), "all zeros"),
        ("é", "base64"),
    ],
)
def test_malformed_encoded_vectors_are_rejected(encoded: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        decode_vector(encoded, 3)


# --- the recorded file --------------------------------------------------------------------


async def test_a_recorded_snapshot_loads_and_holds_exactly_the_expected_vectors(
    recorded: Path, dataset: Dataset, inputs: ExpectedInputs
) -> None:
    snapshot = load_snapshot(recorded, dataset=dataset, space=SPACE, inputs=inputs)
    assert set(snapshot.vectors) == inputs.keys
    assert snapshot.vectors[("query/v1", inputs.queries[0].input_hash)] == fake_vector(
        inputs.queries[0].input_hash
    )
    assert snapshot.sha256 == hashlib.sha256(recorded.read_bytes()).hexdigest()
    assert sidecar_path(recorded).read_bytes() == f"{snapshot.sha256}\n".encode()
    header = snapshot.header
    assert header.dataset.sha256 == FROZEN_SHA
    assert header.inputs_sha256 == FROZEN_INPUTS_SHA
    assert header.record_counts == {"document/v1": 41, "query/v1": 14}
    assert header.space.provider == PROVIDER_NAME and header.space.dimensions == 4096
    assert header.recording.logical_requests == 2 and header.recording.http_attempts == 2
    assert header.recording.served_models == (SERVED,)
    assert header.recording.recorded_at == "2026-10-19T12:00:00Z"


async def test_the_snapshot_is_canonical_sorted_lf_jsonl_without_text_or_request_ids(
    recorded: Path, inputs: ExpectedInputs
) -> None:
    data = recorded.read_bytes()
    assert b"\r" not in data and data.endswith(b"\n")
    assert PROVIDER_REQUEST_ID.encode() not in data and b"request_id" not in data
    for item in inputs.all:  # no provider text is stored, not even a fragment
        assert item.text[:40].encode("utf-8") not in data
    text_lines = data.decode().splitlines()
    for line in text_lines:
        assert line == json.dumps(json.loads(line), sort_keys=True, separators=(",", ":"))
    records = [json.loads(line) for line in text_lines[1:]]
    keys = [(r["input_format"], r["input_hash"]) for r in records]
    assert keys == sorted(keys) and len(keys) == 55
    assert all(set(r) == {"input_format", "input_hash", "vector"} for r in records)


async def test_recording_is_byte_for_byte_reproducible(
    dataset: Dataset, inputs: ExpectedInputs, tmp_path: Path
) -> None:
    first = await record(dataset, inputs, tmp_path / "a" / "s.jsonl")
    second = await record(dataset, inputs, tmp_path / "b" / "s.jsonl")
    assert first.sha256 == second.sha256
    assert first.snapshot.read_bytes() == second.snapshot.read_bytes()


# --- load-time rejection ------------------------------------------------------------------


async def test_a_missing_or_malformed_sidecar_is_rejected(
    recorded: Path, dataset: Dataset, inputs: ExpectedInputs
) -> None:
    sidecar = sidecar_path(recorded)
    good = sidecar.read_bytes()
    sidecar.write_bytes(good.upper())
    assert "64 lowercase hex" in problems(recorded, dataset, inputs)
    sidecar.write_bytes(good.rstrip(b"\n"))
    assert "64 lowercase hex" in problems(recorded, dataset, inputs)
    sidecar.write_bytes(b"0" * 64 + b"\n")
    assert "does not match its sidecar" in problems(recorded, dataset, inputs)
    sidecar.unlink()
    assert "missing" in problems(recorded, dataset, inputs)


async def test_a_changed_byte_without_a_new_sidecar_is_rejected(
    recorded: Path, dataset: Dataset, inputs: ExpectedInputs
) -> None:
    data = bytearray(recorded.read_bytes())
    data[-10] ^= 1
    recorded.write_bytes(bytes(data))
    assert "does not match its sidecar" in problems(recorded, dataset, inputs)


async def test_crlf_and_malformed_jsonl_are_rejected(
    recorded: Path, dataset: Dataset, inputs: ExpectedInputs
) -> None:
    data = recorded.read_bytes()
    rewrite(recorded, [], raw=data.replace(b"\n", b"\r\n"))
    assert "carriage returns" in problems(recorded, dataset, inputs)
    head, rest = data.split(b"\n", 1)
    rewrite(recorded, [], raw=head + b"\n{not json\n" + rest)
    assert "line 2: not a snapshot record" in problems(recorded, dataset, inputs)
    rewrite(recorded, [], raw=b"{not json\n" + rest)
    assert "header is not valid JSON" in problems(recorded, dataset, inputs)
    rewrite(recorded, [], raw=data.rstrip(b"\n"))
    assert "must end with a LF" in problems(recorded, dataset, inputs)


async def test_non_canonical_lines_are_rejected(
    recorded: Path, dataset: Dataset, inputs: ExpectedInputs
) -> None:
    records = lines(recorded)
    data = (
        json.dumps(records[0], sort_keys=True, separators=(",", ":"))
        + "\n"
        + "".join(json.dumps(r, sort_keys=True) + "\n" for r in records[1:])  # spaces
    ).encode()
    rewrite(recorded, [], raw=data)
    assert "not canonical JSON" in problems(recorded, dataset, inputs)


async def test_duplicate_missing_extra_and_unordered_records_are_rejected(
    recorded: Path, dataset: Dataset, inputs: ExpectedInputs
) -> None:
    header, *records = lines(recorded)
    rewrite(recorded, [header, records[0], *records])
    assert "duplicate record" in problems(recorded, dataset, inputs)

    rewrite(recorded, [header, *records[1:]])
    found = problems(recorded, dataset, inputs)
    assert f"missing record {records[0]['input_format']} {records[0]['input_hash']}" in found

    extra = dict(records[-1], input_hash="f" * 64)
    rewrite(recorded, [header, *records, extra])
    assert f"unexpected record query/v1 {'f' * 64}" in problems(recorded, dataset, inputs)

    rewrite(recorded, [header, records[1], records[0], *records[2:]])
    assert "not in (input_format, input_hash) order" in problems(recorded, dataset, inputs)


async def test_bad_vectors_in_the_file_are_rejected(
    recorded: Path, dataset: Dataset, inputs: ExpectedInputs
) -> None:
    header, *records = lines(recorded)
    for vector, message in [
        (base64.b64encode(struct.pack("<4096f", *([0.0] * 4096))).decode(), "all zeros"),
        (
            base64.b64encode(struct.pack("<4096f", float("nan"), *([1.0] * 4095))).decode(),
            "non-finite",
        ),
        (
            base64.b64encode(struct.pack("<4095f", *([1.0] * 4095))).decode(),
            "16380 bytes; expected 16384",
        ),
        ("%%%", "base64"),
    ]:
        rewrite(recorded, [header, dict(records[0], vector=vector), *records[1:]])
        assert message in problems(recorded, dataset, inputs)


async def test_a_snapshot_for_another_dataset_or_space_is_rejected(
    recorded: Path, dataset: Dataset, inputs: ExpectedInputs
) -> None:
    header, *records = lines(recorded)
    wrong_sha = json.loads(json.dumps(header))
    wrong_sha["dataset"]["sha256"] = "0" * 64
    rewrite(recorded, [wrong_sha, *records])
    assert "snapshot is for dataset" in problems(recorded, dataset, inputs)

    rewrite(recorded, [header, *records])
    for space in [
        EmbeddingSpace("nebius", SPACE.model, 4096, "v1"),
        EmbeddingSpace(PROVIDER_NAME, "Qwen/Qwen3-Embedding-4B", 4096, "v1"),
        EmbeddingSpace(PROVIDER_NAME, SPACE.model, 4096, "v2"),
    ]:
        assert "snapshot space" in problems(recorded, dataset, inputs, space)


async def test_a_snapshot_with_the_wrong_dimensions_is_rejected(
    recorded: Path, dataset: Dataset, inputs: ExpectedInputs
) -> None:
    header, *records = lines(recorded)
    smaller = json.loads(json.dumps(header))
    smaller["space"]["dimensions"] = 1024
    rewrite(recorded, [smaller, *records])
    found = problems(recorded, dataset, inputs)
    assert "snapshot space" in found and "16384 bytes; expected 4096" in found  # 1024 float32


async def test_wrong_encoding_counts_and_inputs_hash_are_rejected(
    recorded: Path, dataset: Dataset, inputs: ExpectedInputs
) -> None:
    header, *records = lines(recorded)
    for path, value, message in [
        (("vector_encoding",), "float16-le-base64/v1", "invalid header at vector_encoding"),
        (("inputs_sha256",), "1" * 64, "inputs_sha256"),
        (("record_counts",), {"document/v1": 41}, "record_counts"),
        (("schema_version",), "embedding-snapshot/v2", "invalid header at schema_version"),
        (("recording", "request_ids"), ["x"], "invalid header at recording.request_ids"),
    ]:
        changed = json.loads(json.dumps(header))
        target = changed
        for part in path[:-1]:
            target = target[part]
        target[path[-1]] = value
        rewrite(recorded, [changed, *records])
        assert message in problems(recorded, dataset, inputs)


# --- the recorder: plan and bounds --------------------------------------------------------


def test_the_plan_is_computed_by_the_production_batcher(
    dataset: Dataset, inputs: ExpectedInputs, tmp_path: Path
) -> None:
    plan = make_plan(dataset, inputs, tmp_path / "s.jsonl")
    assert [(b.input_format, len(b.items)) for b in plan.batches] == [
        ("document/v1", 41),
        ("query/v1", 14),
    ]
    assert [b.estimated_tokens for b in plan.batches] == [27_233, 692]
    assert plan.logical_requests == 2 and plan.max_http_attempts == 6
    assert plan.expected_spend_usd == Decimal("0.00027925")
    assert plan.max_spend_usd == Decimal("0.00104718750")
    assert plan.problems() == []
    report = plan.describe()
    assert report["inputs"]["inputs_sha256"] == FROZEN_INPUTS_SHA
    assert report["dataset"]["sha256"] == FROZEN_SHA
    assert report == make_plan(dataset, inputs, tmp_path / "s.jsonl").describe()


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"batch_max_items": 20}, "logical requests"),  # 41 documents need 3 batches
        ({"batch_max_estimated_tokens": 10_000}, "logical requests"),
        ({"attempts_per_request": 4}, "HTTP attempts"),
        ({"price_usd_per_million": Decimal("0.0955")}, "maximum spend"),
    ],
)
async def test_a_plan_outside_the_bounds_makes_no_request(
    dataset: Dataset,
    inputs: ExpectedInputs,
    tmp_path: Path,
    overrides: dict[str, Any],
    message: str,
) -> None:
    provider = FakeProvider()
    path = tmp_path / "s.jsonl"
    with pytest.raises(RecordingRefused) as caught:
        await record(dataset, inputs, path, provider, **overrides)
    assert any(message in p for p in caught.value.problems)
    assert provider.calls == []
    assert list(tmp_path.iterdir()) == []


def test_the_spend_bound_is_strict(
    dataset: Dataset, inputs: ExpectedInputs, tmp_path: Path
) -> None:
    # The price at which the planned maximum equals the bound exactly is refused.
    plan = make_plan(dataset, inputs, tmp_path / "s.jsonl")
    at_bound = MAX_SPEND_USD / plan.max_spend_usd * plan.price_usd_per_million
    exact = make_plan(dataset, inputs, tmp_path / "s.jsonl", price_usd_per_million=at_bound)
    assert exact.max_spend_usd == MAX_SPEND_USD and exact.problems()


async def test_observed_usage_stops_recording_before_the_next_batch(
    dataset: Dataset, inputs: ExpectedInputs, tmp_path: Path
) -> None:
    # The first response reports far more tokens than estimated: continuing
    # could breach the bound, so the second batch is never sent.
    provider = FakeProvider(tokens=lambda call: 950_000 if call == 1 else 10)
    with pytest.raises(RecordingRefused, match="remaining worst case"):
        await record(dataset, inputs, tmp_path / "s.jsonl", provider)
    assert len(provider.calls) == 1
    assert list(tmp_path.iterdir()) == []


async def test_an_observed_ratio_above_the_margin_is_applied_to_the_rest(
    dataset: Dataset, inputs: ExpectedInputs, tmp_path: Path
) -> None:
    # At $0.035/1M the plan's maximum is ~$0.0037. Batch 1 then reports 10x its
    # estimate (272,330 tokens, ~$0.00953 spent). At the 1.25 margin batch 2's
    # worst case (692 x 3 x 1.25) would keep the total under $0.01; at the
    # observed 10x ratio (692 x 3 x 10, ~$0.00073) it would not: stop.
    price = Decimal("0.035")
    assert make_plan(dataset, inputs, tmp_path / "x", price_usd_per_million=price).problems() == []
    provider = FakeProvider(tokens=lambda call: 272_330 if call == 1 else 692)
    with pytest.raises(RecordingRefused, match="remaining worst case"):
        await record(dataset, inputs, tmp_path / "s.jsonl", provider, price_usd_per_million=price)
    assert len(provider.calls) == 1
    assert list(tmp_path.iterdir()) == []


async def test_unreported_usage_counts_as_the_worst_case(
    dataset: Dataset, inputs: ExpectedInputs, tmp_path: Path
) -> None:
    result = await record(dataset, inputs, tmp_path / "s.jsonl", FakeProvider(tokens=None))
    assert result.header.recording.reported_prompt_tokens is None
    plan = make_plan(dataset, inputs, tmp_path / "x.jsonl")
    assert result.spent_usd == plan.max_spend_usd


async def test_a_request_using_more_attempts_than_the_policy_fails(
    dataset: Dataset, inputs: ExpectedInputs, tmp_path: Path
) -> None:
    provider = FakeProvider(attempts=4)
    with pytest.raises(RecordingFailed, match="more attempts"):
        await record(dataset, inputs, tmp_path / "s.jsonl", provider)
    assert list(tmp_path.iterdir()) == []


async def test_attempts_already_used_bound_the_next_batch(
    dataset: Dataset, inputs: ExpectedInputs, tmp_path: Path
) -> None:
    # Three attempts on batch 1 leave exactly three for batch 2: allowed.
    result = await record(dataset, inputs, tmp_path / "s.jsonl", FakeProvider(attempts=3))
    assert result.header.recording.http_attempts == 6


# --- the recorder: failures and publication -----------------------------------------------


async def test_a_failed_request_publishes_nothing(
    dataset: Dataset, inputs: ExpectedInputs, tmp_path: Path
) -> None:
    provider = FakeProvider(fail_on_call=2)
    with pytest.raises(RecordingFailed, match="batch 2/2 failed: server_error"):
        await record(dataset, inputs, tmp_path / "s.jsonl", provider)
    assert len(provider.calls) == 2
    assert list(tmp_path.iterdir()) == []


async def test_a_bad_vector_publishes_nothing(
    dataset: Dataset, inputs: ExpectedInputs, tmp_path: Path
) -> None:
    provider = FakeProvider(vector=lambda digest: (0.0,) * 4096)
    with pytest.raises(RecordingFailed, match="all zeros"):
        await record(dataset, inputs, tmp_path / "s.jsonl", provider)
    assert list(tmp_path.iterdir()) == []


async def test_wrong_space_or_batch_limit_is_refused_before_any_request(
    dataset: Dataset, inputs: ExpectedInputs, tmp_path: Path
) -> None:
    for provider in [
        FakeProvider(space=EmbeddingSpace(PROVIDER_NAME, SPACE.model, 4096, "v2")),
        FakeProvider(max_batch_items=40),
    ]:
        with pytest.raises(RecordingRefused):
            await record(dataset, inputs, tmp_path / "s.jsonl", provider)
        assert provider.calls == []


async def test_a_failure_while_renaming_the_sidecar_leaves_no_final_file(
    dataset: Dataset, inputs: ExpectedInputs, tmp_path: Path
) -> None:
    import os

    path = tmp_path / "out" / "s.jsonl"
    renamed: list[Path] = []

    def replace(source: Path, target: Path) -> None:
        if target.name.endswith(".sha256"):
            raise OSError("disk full")
        os.replace(source, target)
        renamed.append(target)

    plan = make_plan(dataset, inputs, path)
    with pytest.raises(OSError, match="disk full"):
        await record_snapshot(plan, FakeProvider(), now=lambda: CLOCK, replace=replace)
    assert renamed == [path]  # the snapshot was renamed, then removed again
    assert list(path.parent.iterdir()) == []  # no final file, no temporary file


@pytest.mark.parametrize("existing", ["snapshot", "sidecar"])
async def test_an_existing_snapshot_is_never_overwritten(
    dataset: Dataset, inputs: ExpectedInputs, tmp_path: Path, existing: str
) -> None:
    path = tmp_path / "s.jsonl"
    target = path if existing == "snapshot" else sidecar_path(path)
    target.write_bytes(b"keep me\n")
    provider = FakeProvider()
    with pytest.raises(RecordingRefused, match="already exists"):
        await record(dataset, inputs, path, provider)
    assert provider.calls == []
    assert target.read_bytes() == b"keep me\n"


# --- replay -------------------------------------------------------------------------------


@pytest.fixture
async def replay(
    recorded: Path, dataset: Dataset, inputs: ExpectedInputs
) -> ReplayEmbeddingProvider:
    return load_replay_provider(
        recorded, dataset=dataset, active_space=SPACE, max_batch_items=64, inputs=inputs
    )


async def test_replay_returns_the_recorded_vectors_in_input_order(
    replay: ReplayEmbeddingProvider, inputs: ExpectedInputs, recorded: Path
) -> None:
    batch = [inputs.queries[3], inputs.documents[0], inputs.queries[3]]
    result = await replay.embed(batch)
    assert result.vectors == tuple(fake_vector(i.input_hash) for i in batch)
    assert result.space == SPACE  # the recorded production space, unchanged
    assert result.request_id == REPLAY_REQUEST_ID_PREFIX + file_sha256(recorded.read_bytes())
    assert result.usage == TokenUsage()  # nothing spent, nothing reported
    assert result.served_model == SERVED
    assert replay.calls == 1


async def test_an_unknown_replay_input_fails_without_text_or_fallback(
    replay: ReplayEmbeddingProvider, inputs: ExpectedInputs
) -> None:
    secret = "a question the snapshot never saw"
    unknown = query_input(secret)
    with pytest.raises(ReplayInputError) as caught:
        await replay.embed([inputs.queries[0], unknown])
    error = caught.value
    assert isinstance(error, AIProviderError) and not error.retriable
    assert error.missing == (("query/v1", unknown.input_hash),)
    assert unknown.input_hash in str(error) and secret not in str(error)
    assert "Instruct" not in str(error)
    assert replay.calls == 0  # the whole batch failed; nothing was returned
    with pytest.raises(ReplayInputError):  # same text, other format: still unknown
        await replay.embed([document_input(inputs.queries[0].text)])


async def test_replay_makes_no_network_call_and_needs_no_credentials(
    recorded: Path, dataset: Dataset, inputs: ExpectedInputs, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_network(*args: object, **kwargs: object) -> None:
        raise AssertionError("replay attempted a network connection")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    monkeypatch.delenv("NEBIUS_API_KEY", raising=False)
    provider = load_replay_provider(
        recorded, dataset=dataset, active_space=SPACE, max_batch_items=64, inputs=inputs
    )
    result = await provider.embed(list(inputs.documents[:5]))
    assert len(result.vectors) == 5
    assert not any("openai" in type(v).__module__ for v in vars(provider).values())
    await provider.aclose()


def test_the_replay_module_cannot_reach_a_live_client() -> None:
    tree = ast.parse(Path(replay_module.__file__).read_text(encoding="utf-8"))
    imported = {
        node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    } | {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert not {m for m in imported if "openai" in m or "nebius" in m or "httpx" in m}


async def test_replay_refuses_a_snapshot_for_another_active_space(
    recorded: Path, dataset: Dataset, inputs: ExpectedInputs
) -> None:
    with pytest.raises(SnapshotError, match="snapshot space"):
        load_replay_provider(
            recorded,
            dataset=dataset,
            active_space=EmbeddingSpace(PROVIDER_NAME, SPACE.model, 4096, "v2"),
            max_batch_items=64,
            inputs=inputs,
        )


# --- the committed snapshot ---------------------------------------------------------------

COMMITTED = (
    DATASET_DIR.parents[2]
    / "snapshots"
    / "retrieval"
    / "v1"
    / "nebius-qwen3-embedding-8b-d4096-sv1.jsonl"
)


async def test_the_committed_snapshot_replays_every_frozen_input(
    dataset: Dataset, inputs: ExpectedInputs
) -> None:
    provider = load_replay_provider(
        COMMITTED, dataset=dataset, active_space=SPACE, max_batch_items=64, inputs=inputs
    )
    assert provider.snapshot_sha256 == file_sha256(COMMITTED.read_bytes())
    result = await provider.embed(list(inputs.documents))
    assert len(result.vectors) == 41
    result = await provider.embed(list(inputs.queries))
    assert len(result.vectors) == 14 and all(len(v) == 4096 for v in result.vectors)
