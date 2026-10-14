# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""An offline, deterministic ``EmbeddingProvider`` for indexing and search tests.

Vectors are a feature-hashed bag of words: texts that share words point in
similar directions, so ranking tests can be written in plain words. This is a
test double, not a model: it says nothing about real retrieval quality.
"""

import hashlib
import re
from collections.abc import Awaitable, Callable, Sequence

from app.ai.embeddings import EmbeddingInput, EmbeddingResult, EmbeddingSpace, check_batch
from app.ai.errors import AIProviderError
from app.ai.provider import TokenUsage

FAKE_PROVIDER = "fake"
FAKE_MODEL = "fake/hashed-bag-of-words"
_WORD = re.compile(r"[a-z0-9]+")


def hashed_bag_of_words(text: str, dimensions: int) -> tuple[float, ...]:
    vector = [0.0] * dimensions
    for word in _WORD.findall(text.lower()):
        digest = hashlib.sha256(word.encode("utf-8")).digest()
        position = int.from_bytes(digest[:4], "big") % dimensions
        vector[position] += 1.0 if digest[4] & 1 else -1.0
    if not any(vector):
        vector[0] = 1.0  # never all zeros: cosine is undefined for it
    return tuple(vector)


class FakeEmbeddingProvider:
    """Records every batch it is asked to embed; ``failures`` are raised, in
    order, by the next calls (after recording them). ``before_embed`` is
    awaited inside every call, before it returns: tests use it to pause a call
    (concurrency) or to change the database mid-run. ``space`` replaces the
    fake identity (e.g. to match a space derived from settings);
    ``truncate_results`` drops the last vector, as a broken provider might."""

    def __init__(
        self,
        *,
        dimensions: int = 64,
        space_version: str = "v1",
        max_batch_items: int = 64,
        failures: Sequence[AIProviderError] = (),
        before_embed: Callable[[Sequence[EmbeddingInput]], Awaitable[None]] | None = None,
        space: EmbeddingSpace | None = None,
        truncate_results: bool = False,
    ) -> None:
        self._space = space or EmbeddingSpace(FAKE_PROVIDER, FAKE_MODEL, dimensions, space_version)
        self.truncate_results = truncate_results
        self._max_batch_items = max_batch_items
        self.failures = list(failures)
        self.before_embed = before_embed
        self.calls: list[list[EmbeddingInput]] = []

    @property
    def space(self) -> EmbeddingSpace:
        return self._space

    @property
    def max_batch_items(self) -> int:
        return self._max_batch_items

    async def aclose(self) -> None:
        pass

    async def embed(self, inputs: Sequence[EmbeddingInput]) -> EmbeddingResult:
        check_batch(inputs, self._max_batch_items)
        self.calls.append(list(inputs))
        if self.before_embed is not None:
            await self.before_embed(inputs)
        if self.failures:
            raise self.failures.pop(0)
        return EmbeddingResult(
            vectors=tuple(
                hashed_bag_of_words(item.text, self._space.dimensions)
                for item in (inputs[:-1] if self.truncate_results else inputs)
            ),
            space=self._space,
            served_model=FAKE_MODEL,
            usage=TokenUsage(
                prompt_tokens=sum(len(_WORD.findall(item.text.lower())) for item in inputs),
                completion_tokens=0,
            ),
            attempts=1,
            request_id=f"fake-{len(self.calls)}",
        )

    @property
    def embedded_texts(self) -> list[str]:
        return [item.text for batch in self.calls for item in batch]
