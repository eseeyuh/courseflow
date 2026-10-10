# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""The ``AIProvider`` interface and the vendor-neutral types it speaks.

Callers ask for a *role* (fast/strong) and a Pydantic *schema*; they get back
a validated instance of that schema or an ``AIProviderError``. Which vendor,
which model ID, how JSON is constrained and how failures are retried are the
provider's concern.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal, Protocol

from pydantic import BaseModel

from app.domain.enums import ModelRole

_PER_MILLION = Decimal(1_000_000)


@dataclass(frozen=True, slots=True)
class ChatMessage:
    role: Literal["system", "user", "assistant"]
    content: str


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """Token counts as reported by the provider; ``None`` means not reported.

    Adding is strict: if either side is unknown the sum is unknown, so a
    partial total is never presented as complete.
    """

    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    # Hidden reasoning, already included in completion_tokens.
    reasoning_tokens: int | None = None

    def __add__(self, other: TokenUsage) -> TokenUsage:
        def add(a: int | None, b: int | None) -> int | None:
            return None if a is None or b is None else a + b

        return TokenUsage(
            add(self.prompt_tokens, other.prompt_tokens),
            add(self.completion_tokens, other.completion_tokens),
            add(self.reasoning_tokens, other.reasoning_tokens),
        )


@dataclass(frozen=True, slots=True)
class StructuredResult[T: BaseModel]:
    output: T
    model: str
    usage: TokenUsage
    # HTTP requests made, including transport retries and repair turns.
    attempts: int
    # Repair turns needed after invalid output (0 = valid first time).
    repair_rounds: int = 0
    request_id: str | None = None


@dataclass(frozen=True, slots=True)
class ModelInfo:
    """Catalog metadata used for routing and cost estimation."""

    id: str
    context_length: int | None
    input_usd_per_million: Decimal | None
    output_usd_per_million: Decimal | None


class AIProvider(Protocol):
    @property
    def name(self) -> str: ...

    def model_for(self, role: ModelRole) -> str:
        """The configured model ID for a role."""
        ...

    async def chat_structured[T: BaseModel](
        self,
        messages: Sequence[ChatMessage],
        schema: type[T],
        *,
        role: ModelRole,
        temperature: float | None = None,
    ) -> StructuredResult[T]:
        """Return ``schema`` validated from the model's answer, or raise ``AIProviderError``."""
        ...

    async def list_models(self) -> list[ModelInfo]: ...


def estimate_cost_usd(usage: TokenUsage, info: ModelInfo) -> Decimal | None:
    """Cost from reported tokens and catalog prices; ``None`` if either is unknown.

    Never guessed: an unknown price or token count gives no estimate at all.
    """
    if (
        usage.prompt_tokens is None
        or usage.completion_tokens is None
        or info.input_usd_per_million is None
        or info.output_usd_per_million is None
    ):
        return None
    return (
        usage.prompt_tokens * info.input_usd_per_million
        + usage.completion_tokens * info.output_usd_per_million
    ) / _PER_MILLION
