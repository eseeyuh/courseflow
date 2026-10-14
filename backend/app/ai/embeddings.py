# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""The ``EmbeddingProvider`` interface and the vendor-neutral types it speaks.

Vectors are only comparable inside one ``EmbeddingSpace``. A space is
identified by what CourseFlow *asks for* (provider, requested model ID,
dimensions) plus ``space_version``, CourseFlow's own compatibility revision.
The model ID is provider-controlled: the weights behind it could change
without the ID changing, and that cannot be detected here, so bumping
``space_version`` is how stored vectors are deliberately invalidated.

Providers embed exactly the text they are given. All formatting happens
before the call, in ``document_input`` / ``query_input``, so the exact
provider input can be hashed and cached.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from app.ai.provider import TokenUsage

# Versioned input formats. Changing what is sent for a format means a new
# version: stored vectors are keyed by format and input hash.
DOCUMENT_INPUT_FORMAT = "document/v1"
QUERY_INPUT_FORMAT = "query/v1"

# Qwen3-Embedding is asymmetric: queries carry a one-line task instruction,
# documents are embedded as they are (the model card's recommended usage).
QUERY_TASK_V1 = (
    "Given a student's question about a university course, retrieve passages "
    "from the course materials that answer it"
)
# CourseFlow's own bound on a query/v1 question (stripped), not a provider
# limit: a question, not a document. Changing it changes what query/v1 accepts.
QUERY_V1_MAX_CHARS = 2000


@dataclass(frozen=True, slots=True)
class EmbeddingSpace:
    provider: str
    # The requested/configured model ID, not the one a response reports.
    model: str
    dimensions: int
    space_version: str

    def __post_init__(self) -> None:
        if not (self.provider and self.model and self.space_version):
            raise ValueError("provider, model and space_version must be non-empty")
        if self.dimensions < 1:
            raise ValueError("dimensions must be >= 1")


@dataclass(frozen=True, slots=True)
class EmbeddingInput:
    """The exact text a provider will receive, and the format that produced it."""

    text: str
    input_format: str

    @property
    def input_hash(self) -> str:
        """SHA-256 of the exact UTF-8 provider input (same definition as
        ingestion's ``text_hash``, so for ``document/v1`` the two are equal)."""
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()


def document_input(text: str) -> EmbeddingInput:
    """``document/v1``: the chunk text, unchanged."""
    if not text.strip():
        raise ValueError("document text is empty")
    return EmbeddingInput(text, DOCUMENT_INPUT_FORMAT)


def query_input(question: str) -> EmbeddingInput:
    """``query/v1``: the stripped question behind the task instruction."""
    question = question.strip()
    if not question:
        raise ValueError("query is empty")
    if len(question) > QUERY_V1_MAX_CHARS:
        raise ValueError(f"query is longer than {QUERY_V1_MAX_CHARS} characters")
    return EmbeddingInput(f"Instruct: {QUERY_TASK_V1}\nQuery:{question}", QUERY_INPUT_FORMAT)


@dataclass(frozen=True, slots=True)
class EmbeddingResult:
    # One vector per input, in input order, each of space.dimensions floats.
    vectors: tuple[tuple[float, ...], ...]
    space: EmbeddingSpace
    # The model the response reports, for telemetry only: it never replaces
    # space.model in the space identity.
    served_model: str | None
    usage: TokenUsage
    # HTTP requests made, including transport retries.
    attempts: int
    request_id: str | None = None


class EmbeddingProvider(Protocol):
    @property
    def space(self) -> EmbeddingSpace: ...

    @property
    def max_batch_items(self) -> int:
        """The most inputs one ``embed`` call accepts."""
        ...

    async def embed(self, inputs: Sequence[EmbeddingInput]) -> EmbeddingResult:
        """One provider request for ``inputs``: validated vectors in input order,
        or ``AIProviderError``. A malformed batch raises ``ValueError`` before
        any request is made."""
        ...

    async def aclose(self) -> None: ...


def check_batch(inputs: Sequence[EmbeddingInput], max_items: int) -> None:
    """Reject batches no provider request should be made for (caller bugs)."""
    if not inputs:
        raise ValueError("embedding batch is empty")
    if len(inputs) > max_items:
        raise ValueError(f"embedding batch has {len(inputs)} inputs; the limit is {max_items}")
    for position, item in enumerate(inputs):
        if not item.text.strip():
            raise ValueError(f"embedding input {position} is empty")
