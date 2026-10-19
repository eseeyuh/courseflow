# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Recorded embedding snapshots (``embedding-snapshot/v1``) for replayed evaluation.

A snapshot holds one vector per provider input a frozen retrieval dataset
needs, in one embedding space. It stores no text: every input is rebuilt
from the dataset with the production builders (``document_input`` /
``query_input``), so the snapshot can only answer exactly those inputs.

File (UTF-8, LF only, every line canonical JSON: sorted keys, compact
separators, ASCII, no NaN), then one LF after every line:

- line 1, the header (``SnapshotHeader``);
- then one record per input, ``{"input_format", "input_hash", "vector"}``,
  in strictly ascending bytewise order of (input_format, input_hash).

``float32-le-base64/v1``: a vector is its values packed as IEEE 754 float32,
little-endian, in order, then standard base64 with padding (RFC 4648 §4).
pgvector stores float32, so nothing downstream sees more precision.

``sha256-input-keys/v1`` (``inputs_sha256``): SHA-256 over the ASCII line
``sha256-input-keys/v1`` and a LF, then for each key in ascending bytewise
order of (input_format UTF-8, input_hash UTF-8): the format bytes, one NUL,
the 64 hash characters, one LF. Formats match ``(document|query)/vN`` and
hashes are lowercase hex, so neither can contain NUL or LF: the encoding is
unambiguous, and an empty key set hashes the header line alone.

``sha256-file-bytes/v1`` (the snapshot ID): SHA-256 of the exact snapshot
file bytes, written to ``<file>.sha256`` as 64 lowercase hex digits and a
LF. The sidecar is not part of the hash.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
import re
import struct
import uuid
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from app.ai.embeddings import EmbeddingInput, EmbeddingSpace, document_input, query_input
from app.evaluation.retrieval.dataset import Dataset
from app.evaluation.retrieval.resolve import production_limits
from app.ingestion.dispatch import parse_source
from app.ingestion.schemas import RawSource
from app.ingestion.service import build_span_rows
from app.retrieval import store
from app.retrieval.chunking import SPAN_PACK_V1, ChunkingParams, chunk_spans
from app.retrieval.indexing import SourceContent, derive_chunks

SNAPSHOT_SCHEMA_VERSION = "embedding-snapshot/v1"
VECTOR_ENCODING = "float32-le-base64/v1"
INPUTS_HASH_SCHEME = "sha256-input-keys/v1"
SNAPSHOT_HASH_SCHEME = "sha256-file-bytes/v1"
SIDECAR_SUFFIX = ".sha256"

InputKey = tuple[str, str]  # (input_format, input_hash)

_FORMAT = re.compile(r"(document|query)/v[1-9][0-9]*")
_HEX64 = re.compile(r"[0-9a-f]{64}")
# derive_chunks never reads the version ID; offline there is none.
_UNSET_VERSION_ID = uuid.UUID(int=0)


class SnapshotError(ValueError):
    """The snapshot is unusable. ``problems`` lists every reason found."""

    def __init__(self, problems: Sequence[str]) -> None:
        super().__init__(f"{len(problems)} snapshot problem(s): " + "; ".join(problems))
        self.problems = tuple(problems)


# --- the expected inputs ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ExpectedInputs:
    """Every provider input a dataset needs, built by the production builders.

    ``documents``: every chunk of every version (current and superseded),
    since a superseded version is indexed while it is still current.
    ``queries``: every case's query. Both are deduplicated by key, in the
    order first seen, so the same dataset always gives the same lists.
    """

    documents: tuple[EmbeddingInput, ...]
    queries: tuple[EmbeddingInput, ...]

    @property
    def all(self) -> tuple[EmbeddingInput, ...]:
        return self.documents + self.queries

    @property
    def keys(self) -> frozenset[InputKey]:
        return frozenset(input_key(item) for item in self.all)

    def counts(self) -> dict[str, int]:
        return dict(sorted(Counter(item.input_format for item in self.all).items()))


def input_key(item: EmbeddingInput) -> InputKey:
    return (item.input_format, item.input_hash)


def file_chunk_texts(path: Path, params: ChunkingParams, where: str) -> list[str]:
    """The chunk texts indexing derives for one source file: the production
    parser and span rows, then ``derive_chunks`` exactly as indexing calls it."""
    document = parse_source(
        RawSource(content=path.read_bytes(), display_name=path.name, source_uri="eval:snapshot"),
        production_limits(),
    )
    spans = chunk_spans(SimpleNamespace(**row) for row in build_span_rows(document))
    source = store.VersionSource(_UNSET_VERSION_ID, document.extracted_text, spans)
    content, drafts = derive_chunks(source, params)
    if content is not SourceContent.CHUNKABLE:
        raise SnapshotError([f"{where}: not chunkable ({content.value})"])
    return [draft.text for draft in drafts]


def version_chunk_texts(
    dataset: Dataset, course: str, resource: str, version: int, params: ChunkingParams
) -> list[str]:
    path = dataset.corpus_file(course, resource, version)
    return file_chunk_texts(path, params, f"{course}/{resource} v{version}")


def expected_inputs(dataset: Dataset, params: ChunkingParams = SPAN_PACK_V1) -> ExpectedInputs:
    if params.version != dataset.manifest.expected_chunker:
        raise SnapshotError(
            [f"chunker {params.version} != dataset's {dataset.manifest.expected_chunker}"]
        )
    documents: dict[InputKey, EmbeddingInput] = {}
    for course in dataset.manifest.courses:
        for resource in course.resources:
            for entry in resource.versions:
                for text in version_chunk_texts(
                    dataset, course.key, resource.key, entry.version, params
                ):
                    item = document_input(text)
                    documents.setdefault(input_key(item), item)
    queries: dict[InputKey, EmbeddingInput] = {}
    for case in dataset.cases:
        item = query_input(case.query)
        queries.setdefault(input_key(item), item)
    return ExpectedInputs(tuple(documents.values()), tuple(queries.values()))


@dataclass(frozen=True, slots=True)
class SnapshotSubject:
    """What a snapshot is recorded for: a frozen dataset's identity and the
    chunker its document inputs come from. A retrieval ``Dataset`` is one;
    any other frozen corpus states its own."""

    name: str
    version: str
    sha256: str
    hash_scheme: str
    chunker: str


def subject_of(dataset: Dataset | SnapshotSubject) -> SnapshotSubject:
    if isinstance(dataset, SnapshotSubject):
        return dataset
    manifest = dataset.manifest
    return SnapshotSubject(
        name=manifest.dataset,
        version=manifest.dataset_version,
        sha256=dataset.sha256,
        hash_scheme=manifest.dataset_hash_scheme,
        chunker=manifest.expected_chunker,
    )


# --- hashes -------------------------------------------------------------------------------


def _key_bytes(key: InputKey) -> tuple[bytes, bytes]:
    return key[0].encode("utf-8"), key[1].encode("utf-8")


def sorted_keys(keys: Iterable[InputKey]) -> list[InputKey]:
    return sorted(keys, key=_key_bytes)


def inputs_sha256(keys: Iterable[InputKey]) -> str:
    """``sha256-input-keys/v1`` over a set of input keys (see the module docstring)."""
    keys = list(keys)
    if len(set(keys)) != len(keys):
        raise ValueError("input keys must be distinct")
    digest = hashlib.sha256(INPUTS_HASH_SCHEME.encode("ascii") + b"\n")
    for input_format, input_hash in sorted_keys(keys):
        if not _FORMAT.fullmatch(input_format) or not _HEX64.fullmatch(input_hash):
            raise ValueError(f"malformed input key ({input_format!r}, {input_hash!r})")
        digest.update(input_format.encode("utf-8") + b"\0" + input_hash.encode("ascii") + b"\n")
    return digest.hexdigest()


def file_sha256(data: bytes) -> str:
    """``sha256-file-bytes/v1``."""
    return hashlib.sha256(data).hexdigest()


def sidecar_path(path: Path) -> Path:
    return path.with_name(path.name + SIDECAR_SUFFIX)


def sidecar_bytes(sha: str) -> bytes:
    return f"{sha}\n".encode("ascii")


# --- vectors ------------------------------------------------------------------------------


def encode_vector(vector: Sequence[float], dimensions: int) -> str:
    """``float32-le-base64/v1``. Rejects what the snapshot could not replay
    faithfully: the wrong length, non-finite values, values outside float32,
    and vectors that are (or round to) all zeros."""
    if len(vector) != dimensions:
        raise ValueError(f"vector has {len(vector)} dimensions; expected {dimensions}")
    if not all(isinstance(v, float | int) and not isinstance(v, bool) for v in vector):
        raise ValueError("vector values must be numbers")
    if not all(math.isfinite(v) for v in vector):
        raise ValueError("vector has non-finite values")
    try:
        packed = struct.pack(f"<{dimensions}f", *vector)
    except OverflowError as exc:
        raise ValueError("vector has values outside the float32 range") from exc
    encoded = base64.b64encode(packed).decode("ascii")
    decode_vector(encoded, dimensions)  # the round trip must hold
    return encoded


def decode_vector(encoded: str, dimensions: int) -> tuple[float, ...]:
    """The inverse of ``encode_vector``; only canonical encodings are accepted."""
    try:
        packed = base64.b64decode(encoded.encode("ascii"), validate=True)
    except (binascii.Error, UnicodeEncodeError) as exc:
        raise ValueError("vector is not valid base64") from exc
    if base64.b64encode(packed).decode("ascii") != encoded:
        raise ValueError("vector base64 is not canonical")
    if len(packed) != 4 * dimensions:
        raise ValueError(f"vector has {len(packed)} bytes; expected {4 * dimensions}")
    values = struct.unpack(f"<{dimensions}f", packed)
    if not all(math.isfinite(v) for v in values):
        raise ValueError("vector has non-finite values")
    if not any(values):
        raise ValueError("vector is all zeros (cosine undefined)")
    return values


# --- file format --------------------------------------------------------------------------

_Hex64 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
_Name = Annotated[str, StringConstraints(min_length=1, max_length=256)]


class _Strict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class SnapshotDataset(_Strict):
    name: _Name
    version: _Name
    sha256: _Hex64
    hash_scheme: _Name


class SnapshotSpace(_Strict):
    provider: _Name
    model: _Name
    dimensions: int = Field(ge=1, le=16_000)
    space_version: _Name

    @classmethod
    def of(cls, space: EmbeddingSpace) -> SnapshotSpace:
        return cls(
            provider=space.provider,
            model=space.model,
            dimensions=space.dimensions,
            space_version=space.space_version,
        )

    def to_space(self) -> EmbeddingSpace:
        return EmbeddingSpace(self.provider, self.model, self.dimensions, self.space_version)


class SnapshotRecording(_Strict):
    """How the snapshot was recorded: telemetry, not identity. No provider
    request IDs: they are not needed to reproduce anything."""

    tool: _Name
    recorded_at: Annotated[str, StringConstraints(pattern=r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")]
    served_models: tuple[_Name, ...]
    logical_requests: int = Field(ge=1)
    http_attempts: int = Field(ge=1)
    # Sum of provider-reported prompt tokens; None if any response omitted usage.
    reported_prompt_tokens: int | None = Field(ge=0)


class SnapshotHeader(_Strict):
    schema_version: Literal["embedding-snapshot/v1"]
    dataset: SnapshotDataset
    chunker: _Name
    space: SnapshotSpace
    vector_encoding: Literal["float32-le-base64/v1"]
    inputs_hash_scheme: Literal["sha256-input-keys/v1"]
    inputs_sha256: _Hex64
    record_counts: dict[str, int]
    recording: SnapshotRecording


class SnapshotRecord(_Strict):
    input_format: Annotated[str, StringConstraints(pattern=r"^(document|query)/v[1-9][0-9]*$")]
    input_hash: _Hex64
    vector: str


def canonical_json(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )


def serialize(header: SnapshotHeader, vectors: Mapping[InputKey, str]) -> bytes:
    """The exact snapshot bytes: the header, then records in key order."""
    lines = [canonical_json(header.model_dump(mode="json"))]
    for input_format, input_hash in sorted_keys(vectors):
        record = SnapshotRecord(
            input_format=input_format,
            input_hash=input_hash,
            vector=vectors[(input_format, input_hash)],
        )
        lines.append(canonical_json(record.model_dump(mode="json")))
    return "".join(f"{line}\n" for line in lines).encode("utf-8")


# --- loading ------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Snapshot:
    path: Path
    sha256: str  # the snapshot ID
    header: SnapshotHeader
    vectors: Mapping[InputKey, tuple[float, ...]]

    @property
    def space(self) -> EmbeddingSpace:
        return self.header.space.to_space()


def expected_header_problems(
    header: SnapshotHeader,
    dataset: Dataset | SnapshotSubject,
    space: EmbeddingSpace,
    inputs: ExpectedInputs,
) -> list[str]:
    """How ``header`` disagrees with the frozen dataset, the active space and
    the expected input set."""
    subject = subject_of(dataset)
    problems = []
    expected_dataset = SnapshotDataset(
        name=subject.name,
        version=subject.version,
        sha256=subject.sha256,
        hash_scheme=subject.hash_scheme,
    )
    if header.dataset != expected_dataset:
        problems.append(
            f"snapshot is for dataset {header.dataset.name} {header.dataset.version} "
            f"sha256 {header.dataset.sha256}, not {expected_dataset.name} "
            f"{expected_dataset.version} sha256 {expected_dataset.sha256}"
        )
    if header.chunker != subject.chunker:
        problems.append(f"snapshot chunker {header.chunker} != {subject.chunker}")
    if header.space != SnapshotSpace.of(space):
        problems.append(
            f"snapshot space {header.space.model_dump()} != active space "
            f"{SnapshotSpace.of(space).model_dump()}"
        )
    if header.inputs_sha256 != inputs_sha256(inputs.keys):
        problems.append("snapshot inputs_sha256 != the dataset's expected input keys")
    if header.record_counts != inputs.counts():
        problems.append(f"snapshot record_counts {header.record_counts} != {inputs.counts()}")
    return problems


def load_snapshot(
    path: Path, *, dataset: Dataset | SnapshotSubject, space: EmbeddingSpace, inputs: ExpectedInputs
) -> Snapshot:
    """Validate and load a snapshot; SnapshotError lists every problem.

    Order: the sidecar must exist and be well formed, the file bytes must hash
    to it, and only then are the bytes parsed. Every line must be canonical;
    the header must match the frozen dataset, the active space and the
    expected inputs; the records must be exactly the expected keys, once
    each, in order, with valid vectors.
    """
    try:
        recorded = sidecar_path(path).read_bytes()
    except OSError:
        raise SnapshotError([f"{sidecar_path(path).name}: missing or unreadable"]) from None
    if not re.fullmatch(rb"[0-9a-f]{64}\n", recorded):
        raise SnapshotError([f"{sidecar_path(path).name}: must be 64 lowercase hex digits and LF"])
    try:
        data = path.read_bytes()
    except OSError:
        raise SnapshotError([f"{path.name}: missing or unreadable"]) from None
    sha = file_sha256(data)
    if sidecar_bytes(sha) != recorded:
        raise SnapshotError([f"{path.name}: file hash {sha} does not match its sidecar"])
    return parse_snapshot_bytes(path, sha, data, dataset=dataset, space=space, inputs=inputs)


def parse_snapshot_bytes(
    path: Path,
    sha: str,
    data: bytes,
    *,
    dataset: Dataset | SnapshotSubject,
    space: EmbeddingSpace,
    inputs: ExpectedInputs,
) -> Snapshot:
    if b"\r" in data:
        raise SnapshotError(["carriage returns are not allowed (LF line endings only)"])
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise SnapshotError(["not valid UTF-8"]) from None
    if not text.endswith("\n"):
        raise SnapshotError(["must end with a LF"])
    lines = text[:-1].split("\n")
    header = _parse_header(lines[0])
    problems = expected_header_problems(header, dataset, space, inputs)

    dimensions = header.space.dimensions
    vectors: dict[InputKey, tuple[float, ...]] = {}
    previous: InputKey | None = None
    for number, line in enumerate(lines[1:], start=2):
        where = f"line {number}"
        try:
            raw = json.loads(line)
            record = SnapshotRecord.model_validate_json(line)
        except (json.JSONDecodeError, ValidationError) as exc:
            problems.append(f"{where}: not a snapshot record ({type(exc).__name__})")
            continue
        if canonical_json(raw) != line:
            problems.append(f"{where}: not canonical JSON")
        key = (record.input_format, record.input_hash)
        if key in vectors:
            problems.append(f"{where}: duplicate record {key[0]} {key[1]}")
            continue
        if previous is not None and _key_bytes(key) < _key_bytes(previous):
            problems.append(f"{where}: records are not in (input_format, input_hash) order")
        previous = key
        try:
            vectors[key] = decode_vector(record.vector, dimensions)
        except ValueError as exc:
            problems.append(f"{where} ({key[0]} {key[1]}): {exc}")

    expected = inputs.keys
    problems.extend(f"missing record {f} {h}" for f, h in sorted_keys(expected - vectors.keys()))
    problems.extend(f"unexpected record {f} {h}" for f, h in sorted_keys(vectors.keys() - expected))
    if problems:
        raise SnapshotError(problems)
    return Snapshot(path=path, sha256=sha, header=header, vectors=vectors)


def _parse_header(line: str) -> SnapshotHeader:
    try:
        raw = json.loads(line)
        header = SnapshotHeader.model_validate_json(line)
    except json.JSONDecodeError:
        raise SnapshotError(["line 1: header is not valid JSON"]) from None
    except ValidationError as exc:
        details = [".".join(map(str, e["loc"])) or "(root)" for e in exc.errors()]
        raise SnapshotError([f"line 1: invalid header at {', '.join(details)}"]) from None
    if canonical_json(raw) != line:
        raise SnapshotError(["line 1: header is not canonical JSON"])
    return header
