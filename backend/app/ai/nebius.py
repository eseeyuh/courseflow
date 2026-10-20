# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Nebius Token Factory provider over its OpenAI-compatible API.

Structured output policy (verified live, docs/experiments/model-catalog.md):
1. Request native ``json_schema`` with ``strict: true`` (enforces shape).
2. Validate locally with Pydantic anyway (enforces meaning; strict mode
   still let ``"value": ""`` through).
3. On invalid output, one bounded *repair* turn that shows the model its
   validation errors. Truncated, refused or otherwise incomplete answers are
   never parsed or repaired.

Transport failures are classified, and only transient ones are retried, by
the shared ``NebiusTransport`` (SDK retries disabled, every attempt logged).

Every failure leaves this module as an ``AIProviderError``. Error details are
sanitised: no API key, no raw provider bodies (they can echo the prompt), no
model output values.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import random
from collections.abc import Awaitable, Callable, Mapping, Sequence
from decimal import Decimal, InvalidOperation
from typing import Any, Self

import openai
from openai.types.chat import ChatCompletion
from pydantic import BaseModel, SecretStr, ValidationError

from app.ai.errors import AIProviderError
from app.ai.nebius_transport import (
    PROVIDER_NAME,
    NebiusTransport,
    Spend,
    missing_settings,
    retry_policy_from,
)
from app.ai.provider import ChatMessage, ModelInfo, StructuredResult, TokenUsage
from app.ai.retry import RetryPolicy
from app.core.config import Settings
from app.domain.enums import ModelCallErrorCategory as Category
from app.domain.enums import ModelRole

logger = logging.getLogger(__name__)

# Bounds on what is echoed back in a repair turn or stored as error detail.
_MAX_ECHOED_OUTPUT_CHARS = 4000
_MAX_DETAIL_CHARS = 500
_MAX_REPORTED_VALIDATION_ERRORS = 10


class NebiusTokenFactoryProvider:
    def __init__(
        self,
        *,
        api_key: SecretStr,
        base_url: str,
        models: Mapping[ModelRole, str],
        timeout_seconds: float,
        retry_policy: RetryPolicy,
        max_repair_attempts: int,
        max_output_tokens: int,
        http_client: openai.DefaultAsyncHttpx2Client | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rand: Callable[[], float] = random.random,
    ) -> None:
        self._models = dict(models)
        self._max_repair_attempts = max_repair_attempts
        self._max_output_tokens = max_output_tokens
        self._transport = NebiusTransport(
            api_key=api_key,
            base_url=base_url,
            timeout_seconds=timeout_seconds,
            retry_policy=retry_policy,
            logger=logger,
            http_client=http_client,
            sleep=sleep,
            rand=rand,
        )
        self._client = self._transport.client

    @classmethod
    def from_settings(cls, settings: Settings, **overrides: Any) -> Self:
        key, base_url = settings.nebius_api_key, settings.nebius_base_url
        fast, strong = settings.model_fast, settings.model_strong
        if key is None or base_url is None or fast is None or strong is None:
            raise missing_settings(
                NEBIUS_API_KEY=key, NEBIUS_BASE_URL=base_url, MODEL_FAST=fast, MODEL_STRONG=strong
            )
        kwargs: dict[str, Any] = {
            "api_key": key,
            "base_url": base_url,
            "models": {ModelRole.FAST: fast, ModelRole.STRONG: strong},
            "timeout_seconds": settings.ai_request_timeout_seconds,
            "retry_policy": retry_policy_from(settings),
            "max_repair_attempts": settings.ai_max_repair_attempts,
            "max_output_tokens": settings.ai_max_output_tokens,
        }
        return cls(**(kwargs | overrides))

    @property
    def name(self) -> str:
        return PROVIDER_NAME

    def model_for(self, role: ModelRole) -> str:
        return self._models[role]

    async def aclose(self) -> None:
        await self._transport.aclose()

    # --- structured chat ---------------------------------------------------

    async def chat_structured[T: BaseModel](
        self,
        messages: Sequence[ChatMessage],
        schema: type[T],
        *,
        role: ModelRole,
        temperature: float | None = None,
        thinking: bool | None = None,
    ) -> StructuredResult[T]:
        spend = Spend(model=self.model_for(role))
        conversation: list[dict[str, str]] = [
            {"role": m.role, "content": m.content} for m in messages
        ]
        request: dict[str, Any] = {
            "model": spend.model,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": schema.__name__,
                    "schema": schema.model_json_schema(),
                    "strict": True,
                },
            },
            "max_tokens": self._max_output_tokens,
        }
        if temperature is not None:
            request["temperature"] = temperature
        if thinking is not None:
            # Nemotron's chat-template switch (docs/experiments/model-catalog.md):
            # not part of the OpenAI API, so it travels in extra_body.
            request["extra_body"] = {"chat_template_kwargs": {"enable_thinking": thinking}}

        for repair_round in range(self._max_repair_attempts + 1):
            spend.repair_rounds = repair_round
            # partial over a snapshot: every retry resends exactly this turn.
            send = functools.partial(
                self._client.chat.completions.create, messages=list(conversation), **request
            )
            completion = await self._transport.with_retries(spend, send)
            content = ""
            try:
                content = self._answer_content(spend, completion)
                output = schema.model_validate_json(content)
            except AIProviderError:
                raise
            except ValidationError as exc:
                # Input-free summary: safe to log, store and show the model.
                problems = _describe_validation_errors(exc)
                logger.warning(
                    "ai.output_invalid",
                    extra={
                        "provider": PROVIDER_NAME,
                        "model": spend.model,
                        "schema": schema.__name__,
                        "repair_round": repair_round,
                        "error_count": exc.error_count(),
                        "problems": problems,
                        "content_chars": len(content),
                    },
                )
                if repair_round == self._max_repair_attempts:
                    # from None: the ValidationError's repr contains model output.
                    raise spend.fail(Category.INVALID_OUTPUT, problems) from None
                conversation += [
                    {"role": "assistant", "content": content[:_MAX_ECHOED_OUTPUT_CHARS]},
                    {"role": "user", "content": _repair_instruction(schema.__name__, problems)},
                ]
                continue
            except Exception as exc:
                # Tokens were already spent: surface as a classified failure
                # (so it is recorded), with the traceback in the logs.
                logger.exception("ai.response_handling_failed", extra={"model": spend.model})
                raise spend.fail(Category.UNEXPECTED, self._transport.describe(exc)) from exc
            return StructuredResult(
                output=output,
                model=spend.served_model or spend.model,
                usage=spend.usage or TokenUsage(),
                attempts=spend.attempts,
                repair_rounds=spend.repair_rounds,
                request_id=spend.request_id,
            )
        raise AssertionError("unreachable: the loop returns or raises")

    def _answer_content(self, spend: Spend, completion: object) -> str:
        """Account for the response, then return its content only if it is a
        complete answer; otherwise raise the matching (non-repairable) failure."""
        if not isinstance(completion, ChatCompletion):
            # e.g. a proxy's HTML page with HTTP 200: the SDK returns a str.
            raise spend.fail(
                Category.UNEXPECTED, f"response was not a chat completion ({type(completion)})"
            )
        spend.add_usage(_usage(completion))
        spend.request_id = getattr(completion, "_request_id", None) or spend.request_id
        spend.served_model = completion.model or spend.served_model
        if not completion.choices or completion.choices[0].message is None:
            raise spend.fail(Category.UNEXPECTED, "response contained no message")
        choice = completion.choices[0]
        if choice.finish_reason == "length":
            # Reasoning shares the token budget: truncated content can be
            # partial thinking prose. Never parse it; never repair it.
            raise spend.fail(
                Category.TRUNCATED,
                f"output hit max_tokens={self._max_output_tokens} before completing",
            )
        refusal = getattr(choice.message, "refusal", None)
        if choice.finish_reason == "content_filter" or refusal:
            raise spend.fail(
                Category.REFUSED,
                f"finish_reason={choice.finish_reason}; refusal={(refusal or '')[:200]!r}",
            )
        if choice.finish_reason != "stop":
            raise spend.fail(
                Category.UNEXPECTED, f"unexpected finish_reason={choice.finish_reason!r}"
            )
        return choice.message.content or ""

    # --- model metadata ----------------------------------------------------

    async def list_models(self) -> list[ModelInfo]:
        spend = Spend(model="*")
        page = await self._transport.with_retries(
            spend, lambda: self._client.models.list(extra_query={"verbose": "true"})
        )
        return [_model_info(model.id, model.model_extra or {}) for model in page.data]


def _usage(completion: ChatCompletion) -> TokenUsage:
    usage = completion.usage
    if usage is None:
        return TokenUsage()
    details = usage.completion_tokens_details
    return TokenUsage(
        prompt_tokens=usage.prompt_tokens,
        completion_tokens=usage.completion_tokens,
        reasoning_tokens=details.reasoning_tokens if details else None,
    )


def _describe_validation_errors(exc: ValidationError) -> str:
    # include_input=False: never echo document-derived values into logs/DB.
    errors = exc.errors(include_input=False, include_url=False)
    lines = [
        f"{'.'.join(str(p) for p in err['loc']) or '<root>'}: {err['msg']}"
        for err in errors[:_MAX_REPORTED_VALIDATION_ERRORS]
    ]
    if len(errors) > _MAX_REPORTED_VALIDATION_ERRORS:
        lines.append(f"... and {len(errors) - _MAX_REPORTED_VALIDATION_ERRORS} more")
    return "; ".join(lines)[:_MAX_DETAIL_CHARS]


def _repair_instruction(schema_name: str, problems: str) -> str:
    return (
        f"Your previous response did not validate against the {schema_name} schema: "
        f"{problems}. Return a corrected JSON object only, using information from "
        "the original source text. Do not invent values."
    )


def _price_per_million(model_id: str, value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value)) * 1_000_000
    except InvalidOperation:
        logger.warning(
            "ai.catalog_value_unparseable",
            extra={"model": model_id, "field": "pricing", "value_type": type(value).__name__},
        )
        return None


def _model_info(model_id: str, extra: Mapping[str, Any]) -> ModelInfo:
    pricing = extra.get("pricing") or {}
    context = extra.get("context_length")
    if context is not None and not isinstance(context, int):
        logger.warning(
            "ai.catalog_value_unparseable",
            extra={
                "model": model_id,
                "field": "context_length",
                "value_type": type(context).__name__,
            },
        )
    return ModelInfo(
        id=model_id,
        context_length=context if isinstance(context, int) else None,
        input_usd_per_million=_price_per_million(model_id, pricing.get("prompt")),
        output_usd_per_million=_price_per_million(model_id, pricing.get("completion")),
    )
