# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""An ``EmbeddingProvider`` that answers from a recorded snapshot, for
evaluation and tests only.

It has no client, no credentials and no network code: the only vectors it
can return are the snapshot's, looked up by exactly (input_format,
input_hash). An input the snapshot does not hold raises ``ReplayInputError``
for the whole batch: never an empty or synthesized vector, never a fallback
to a live provider. The error names formats and hashes only, never text.

Its ``space`` is the recorded production space (the real provider, model,
dimensions and space version), so replayed vectors are stored and compared
exactly where live ones would be. ``load_replay_provider`` refuses a
snapshot whose space is not the active one. Replayed calls are marked, not
disguised: each result's ``request_id`` is ``replay:<snapshot sha256>``, it
reports no token usage, and ``served_model`` is the recorded one.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from app.ai.embeddings import (
    EmbeddingInput,
    EmbeddingResult,
    EmbeddingSpace,
    check_batch,
)
from app.ai.errors import AIProviderError
from app.ai.provider import TokenUsage
from app.domain.enums import ModelCallErrorCategory
from app.evaluation.retrieval.dataset import Dataset
from app.evaluation.retrieval.snapshot import (
    ExpectedInputs,
    Snapshot,
    SnapshotSubject,
    expected_inputs,
    input_key,
    load_snapshot,
)

REPLAY_REQUEST_ID_PREFIX = "replay:"


class ReplayInputError(AIProviderError):
    """An input the snapshot does not hold. Deterministic, so never retried;
    an evaluation run that meets it is broken, not a retrieval miss."""

    def __init__(self, missing: Sequence[tuple[str, str]], *, model: str) -> None:
        keys = ", ".join(f"{fmt} {digest}" for fmt, digest in missing)
        super().__init__(
            ModelCallErrorCategory.INVALID_REQUEST,
            f"replay snapshot has no vector for {len(missing)} input(s): {keys}",
            model=model,
            attempts=1,  # one replay lookup; no request was sent anywhere
            usage=TokenUsage(),
        )
        self.missing = tuple(missing)


class ReplayEmbeddingProvider:
    def __init__(self, snapshot: Snapshot, *, max_batch_items: int) -> None:
        if max_batch_items < 1:
            raise ValueError("max_batch_items must be >= 1")
        self._snapshot = snapshot
        self._space = snapshot.space
        self._max_batch_items = max_batch_items
        served = snapshot.header.recording.served_models
        self._served_model = served[0] if len(served) == 1 else None
        self.calls = 0  # replayed batches answered

    @property
    def space(self) -> EmbeddingSpace:
        return self._space

    @property
    def max_batch_items(self) -> int:
        return self._max_batch_items

    @property
    def snapshot_sha256(self) -> str:
        return self._snapshot.sha256

    async def embed(self, inputs: Sequence[EmbeddingInput]) -> EmbeddingResult:
        check_batch(inputs, self._max_batch_items)
        vectors = self._snapshot.vectors
        keys = [input_key(item) for item in inputs]
        missing = list(dict.fromkeys(key for key in keys if key not in vectors))
        if missing:
            raise ReplayInputError(missing, model=self._space.model)
        self.calls += 1
        return EmbeddingResult(
            vectors=tuple(vectors[key] for key in keys),
            space=self._space,
            served_model=self._served_model,
            usage=TokenUsage(),  # nothing was spent
            attempts=1,
            request_id=f"{REPLAY_REQUEST_ID_PREFIX}{self._snapshot.sha256}",
        )

    async def aclose(self) -> None:
        return None


def load_replay_provider(
    path: Path,
    *,
    dataset: Dataset | SnapshotSubject,
    active_space: EmbeddingSpace,
    max_batch_items: int,
    inputs: ExpectedInputs | None = None,
) -> ReplayEmbeddingProvider:
    """A replay provider for the frozen ``dataset``, after full snapshot
    validation against it and the application's active embedding space."""
    if inputs is None:
        if isinstance(dataset, SnapshotSubject):
            raise ValueError("a SnapshotSubject needs its expected inputs")
        inputs = expected_inputs(dataset)
    snapshot = load_snapshot(path, dataset=dataset, space=active_space, inputs=inputs)
    return ReplayEmbeddingProvider(snapshot, max_batch_items=max_batch_items)
