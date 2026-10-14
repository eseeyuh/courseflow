# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Nebius Token Factory embeddings over its OpenAI-compatible API.

Request policy:
- ``dimensions`` is always sent: the space never depends on a provider default.
- ``encoding_format="float"`` is always sent: without it the SDK silently
  requests base64 and decodes it client-side.

Response policy: a response is accepted only if it has one vector per input,
indices exactly 0..n-1 (vectors are placed by index, not list order), the
requested length, finite numbers and no all-zero vector (cosine is undefined
for it). Anything else is ``invalid_output``; it is not retried.

Inputs are document or query text, so error details keep only HTTP status,
error class and validation locations, never free-text provider messages,
which could echo the input. Logs and errors never contain input text.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import math
import random
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Self

import openai
from openai.types import CreateEmbeddingResponse
from pydantic import SecretStr

from app.ai.embeddings import EmbeddingInput, EmbeddingResult, EmbeddingSpace, check_batch
from app.ai.errors import AIProviderError
from app.ai.nebius_transport import (
    PROVIDER_NAME,
    NebiusTransport,
    Spend,
    missing_settings,
    retry_policy_from,
)
from app.ai.provider import TokenUsage
from app.ai.retry import RetryPolicy
from app.core.config import Settings
from app.domain.enums import ModelCallErrorCategory as Category

logger = logging.getLogger(__name__)


def embedding_space_from_settings(settings: Settings) -> EmbeddingSpace:
    """The configured embedding space, without credentials or a client.

    Read-only work (index status, and search filtering) needs only this
    identity; ``NebiusEmbeddingProvider.from_settings`` derives its own space
    here too, so the two can never disagree.
    """
    if settings.embedding_model is None:
        raise missing_settings(EMBEDDING_MODEL=None)
    return EmbeddingSpace(
        PROVIDER_NAME,
        settings.embedding_model,
        settings.embedding_dimensions,
        settings.embedding_space_version,
    )


class NebiusEmbeddingProvider:
    def __init__(
        self,
        *,
        api_key: SecretStr,
        base_url: str,
        model: str,
        dimensions: int,
        space_version: str,
        timeout_seconds: float,
        retry_policy: RetryPolicy,
        max_batch_items: int,
        http_client: openai.DefaultAsyncHttpx2Client | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rand: Callable[[], float] = random.random,
    ) -> None:
        if max_batch_items < 1:
            raise ValueError("max_batch_items must be >= 1")
        self._space = EmbeddingSpace(PROVIDER_NAME, model, dimensions, space_version)
        self._max_batch_items = max_batch_items
        self._transport = NebiusTransport(
            api_key=api_key,
            base_url=base_url,
            timeout_seconds=timeout_seconds,
            retry_policy=retry_policy,
            logger=logger,
            body_text=False,
            http_client=http_client,
            sleep=sleep,
            rand=rand,
        )

    @classmethod
    def from_settings(cls, settings: Settings, **overrides: Any) -> Self:
        key, base_url, model = (
            settings.nebius_api_key,
            settings.nebius_base_url,
            settings.embedding_model,
        )
        if key is None or base_url is None or model is None:
            raise missing_settings(
                NEBIUS_API_KEY=key, NEBIUS_BASE_URL=base_url, EMBEDDING_MODEL=model
            )
        space = embedding_space_from_settings(settings)
        kwargs: dict[str, Any] = {
            "api_key": key,
            "base_url": base_url,
            "model": space.model,
            "dimensions": space.dimensions,
            "space_version": space.space_version,
            "timeout_seconds": settings.ai_request_timeout_seconds,
            "retry_policy": retry_policy_from(settings),
            "max_batch_items": settings.embedding_batch_max_items,
        }
        return cls(**(kwargs | overrides))

    @property
    def space(self) -> EmbeddingSpace:
        return self._space

    @property
    def max_batch_items(self) -> int:
        return self._max_batch_items

    async def aclose(self) -> None:
        await self._transport.aclose()

    async def embed(self, inputs: Sequence[EmbeddingInput]) -> EmbeddingResult:
        check_batch(inputs, self._max_batch_items)
        spend = Spend(model=self._space.model)
        send = functools.partial(
            self._transport.client.embeddings.create,
            model=self._space.model,
            input=[item.text for item in inputs],
            dimensions=self._space.dimensions,
            encoding_format="float",
        )
        response = await self._transport.with_retries(spend, send)
        try:
            vectors = self._vectors(spend, response, len(inputs))
        except AIProviderError as error:
            logger.warning(
                "ai.output_invalid",
                extra={
                    "provider": PROVIDER_NAME,
                    "model": spend.model,
                    "operation": "embeddings",
                    "error_category": error.category.value,
                    "problems": error.detail,
                    "batch_size": len(inputs),
                },
            )
            raise
        except Exception as exc:
            # Tokens were already spent: surface as a classified failure.
            logger.exception("ai.response_handling_failed", extra={"model": spend.model})
            raise spend.fail(Category.UNEXPECTED, self._transport.describe(exc)) from exc
        return EmbeddingResult(
            vectors=vectors,
            space=self._space,
            served_model=spend.served_model,
            usage=spend.usage or TokenUsage(),
            attempts=spend.attempts,
            request_id=spend.request_id,
        )

    def _vectors(
        self, spend: Spend, response: object, expected: int
    ) -> tuple[tuple[float, ...], ...]:
        """Account for the response, then return its vectors in input order,
        or raise the matching failure."""
        if not isinstance(response, CreateEmbeddingResponse):
            # e.g. a proxy's HTML page with HTTP 200: the SDK returns a str.
            raise spend.fail(
                Category.UNEXPECTED, f"response was not an embedding list ({type(response)})"
            )
        spend.add_usage(_usage(response))
        spend.request_id = getattr(response, "_request_id", None) or spend.request_id
        spend.served_model = response.model or spend.served_model

        data = response.data or []
        if len(data) != expected:
            raise spend.fail(
                Category.INVALID_OUTPUT, f"expected {expected} vectors, got {len(data)}"
            )
        by_index = {item.index: item.embedding for item in data}
        if sorted(by_index) != list(range(expected)):
            raise spend.fail(
                Category.INVALID_OUTPUT, f"vector indices are not exactly 0..{expected - 1}"
            )
        dimensions = self._space.dimensions
        vectors = []
        for index in range(expected):
            raw = by_index[index]
            if not isinstance(raw, list):
                raise spend.fail(
                    Category.INVALID_OUTPUT, f"vector {index} is not a list of numbers"
                )
            if len(raw) != dimensions:
                raise spend.fail(
                    Category.INVALID_OUTPUT,
                    f"vector {index} has {len(raw)} dimensions; requested {dimensions}",
                )
            if not all(_is_finite_number(value) for value in raw):
                raise spend.fail(
                    Category.INVALID_OUTPUT, f"vector {index} has non-finite or non-numeric values"
                )
            if not any(raw):
                raise spend.fail(
                    Category.INVALID_OUTPUT, f"vector {index} is all zeros (cosine undefined)"
                )
            vectors.append(tuple(float(value) for value in raw))
        return tuple(vectors)


def _is_finite_number(value: object) -> bool:
    # The SDK already coerces JSON booleans to floats; strings, nulls, NaN and
    # infinities still arrive as they were sent.
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def _usage(response: CreateEmbeddingResponse) -> TokenUsage:
    # Lenient SDK parsing: usage can be absent even though it is typed required.
    usage = getattr(response, "usage", None)
    if usage is None:
        return TokenUsage()
    prompt = usage.prompt_tokens if isinstance(usage.prompt_tokens, int) else None
    total = usage.total_tokens if isinstance(usage.total_tokens, int) else None
    # Embeddings generate no tokens; derived from the reported counts, never assumed.
    completion = total - prompt if prompt is not None and total is not None else None
    return TokenUsage(
        prompt_tokens=prompt,
        completion_tokens=completion if completion is None or completion >= 0 else None,
    )
