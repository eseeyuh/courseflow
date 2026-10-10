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
our own ``RetryPolicy``. The SDK's built-in retries are disabled so every
attempt is visible in logs and counted.

Every failure leaves this module as an ``AIProviderError``. Error details are
sanitised: no API key, no raw provider bodies (they can echo the prompt), no
model output values.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import random
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Self

import openai
from openai.types.chat import ChatCompletion
from pydantic import BaseModel, SecretStr, ValidationError

from app.ai.errors import RETRIABLE_CATEGORIES, AIConfigurationError, AIProviderError
from app.ai.provider import ChatMessage, ModelInfo, StructuredResult, TokenUsage
from app.ai.retry import RetryPolicy, parse_retry_after
from app.core.config import Settings
from app.domain.enums import ModelCallErrorCategory as Category
from app.domain.enums import ModelRole

logger = logging.getLogger(__name__)

PROVIDER_NAME = "nebius-token-factory"
# Bounds on what is echoed back in a repair turn or stored as error detail.
_MAX_ECHOED_OUTPUT_CHARS = 4000
_MAX_DETAIL_CHARS = 500
_MAX_BODY_SUMMARY_CHARS = 200
_MAX_REPORTED_VALIDATION_ERRORS = 10
_REDACTED = "**********"


@dataclass(slots=True)
class _Spend:
    """What one logical call has consumed so far, across retries and repairs."""

    model: str
    attempts: int = 0
    repair_rounds: int = 0
    # Summed over responses received. None until a response reports usage;
    # failed attempts may also have consumed unreported tokens (lower bound).
    usage: TokenUsage | None = None
    request_id: str | None = None
    # The model the server reports having used (may differ from an alias).
    served_model: str | None = None

    def add_usage(self, usage: TokenUsage) -> None:
        self.usage = usage if self.usage is None else self.usage + usage

    def fail(self, category: Category, detail: str) -> AIProviderError:
        return AIProviderError(
            category,
            detail,
            model=self.served_model or self.model,
            attempts=self.attempts,
            repair_rounds=self.repair_rounds,
            usage=self.usage or TokenUsage(),
            request_id=self.request_id,
        )


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
        self._api_key = api_key
        self._models = dict(models)
        self._retry = retry_policy
        self._max_repair_attempts = max_repair_attempts
        self._max_output_tokens = max_output_tokens
        self._sleep = sleep
        self._rand = rand
        self._client = openai.AsyncOpenAI(
            api_key=api_key.get_secret_value(),
            base_url=base_url,
            timeout=timeout_seconds,
            max_retries=0,  # retries are ours: classified, bounded and logged
            http_client=http_client,
        )

    @classmethod
    def from_settings(cls, settings: Settings, **overrides: Any) -> Self:
        key, base_url = settings.nebius_api_key, settings.nebius_base_url
        fast, strong = settings.model_fast, settings.model_strong
        if key is None or base_url is None or fast is None or strong is None:
            missing = [
                name
                for name, value in (
                    ("NEBIUS_API_KEY", key),
                    ("NEBIUS_BASE_URL", base_url),
                    ("MODEL_FAST", fast),
                    ("MODEL_STRONG", strong),
                )
                if value is None
            ]
            raise AIConfigurationError(f"AI provider not configured; missing: {', '.join(missing)}")
        kwargs: dict[str, Any] = {
            "api_key": key,
            "base_url": base_url,
            "models": {ModelRole.FAST: fast, ModelRole.STRONG: strong},
            "timeout_seconds": settings.ai_request_timeout_seconds,
            "retry_policy": RetryPolicy(
                max_attempts=settings.ai_max_attempts,
                base_delay_seconds=settings.ai_backoff_base_seconds,
                max_delay_seconds=settings.ai_backoff_max_seconds,
            ),
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
        await self._client.close()

    # --- structured chat ---------------------------------------------------

    async def chat_structured[T: BaseModel](
        self,
        messages: Sequence[ChatMessage],
        schema: type[T],
        *,
        role: ModelRole,
        temperature: float | None = None,
    ) -> StructuredResult[T]:
        spend = _Spend(model=self.model_for(role))
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

        for repair_round in range(self._max_repair_attempts + 1):
            spend.repair_rounds = repair_round
            # partial over a snapshot: every retry resends exactly this turn.
            send = functools.partial(
                self._client.chat.completions.create, messages=list(conversation), **request
            )
            completion = await self._with_retries(spend, send)
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
                raise spend.fail(Category.UNEXPECTED, self._redact(_describe(exc))) from exc
            return StructuredResult(
                output=output,
                model=spend.served_model or spend.model,
                usage=spend.usage or TokenUsage(),
                attempts=spend.attempts,
                repair_rounds=spend.repair_rounds,
                request_id=spend.request_id,
            )
        raise AssertionError("unreachable: the loop returns or raises")

    def _answer_content(self, spend: _Spend, completion: object) -> str:
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
        spend = _Spend(model="*")
        page = await self._with_retries(
            spend, lambda: self._client.models.list(extra_query={"verbose": "true"})
        )
        return [_model_info(model.id, model.model_extra or {}) for model in page.data]

    # --- transport retries -------------------------------------------------

    async def _with_retries[R](self, spend: _Spend, send: Callable[[], Awaitable[R]]) -> R:
        for attempt in range(1, self._retry.max_attempts + 1):
            spend.attempts += 1
            started = time.perf_counter()
            try:
                response = await send()
            except Exception as exc:  # every failure is classified below
                category, retry_after, status = _classify(exc)
                request_id = getattr(exc, "request_id", None)
                spend.request_id = request_id or spend.request_id
                detail = self._redact(_describe(exc))
                delay = None
                if category in RETRIABLE_CATEGORIES and attempt < self._retry.max_attempts:
                    delay = self._retry.delay_before_retry(
                        attempt, retry_after=retry_after, rand=self._rand
                    )
                    if delay is None:
                        detail = (
                            f"{detail}; server asked to wait {retry_after}s, above the "
                            f"{self._retry.max_delay_seconds}s cap"
                        )
                detail = detail[:_MAX_DETAIL_CHARS]
                logger.warning(
                    "ai.attempt_failed",
                    extra={
                        "provider": PROVIDER_NAME,
                        "model": spend.model,
                        "attempt": attempt,
                        "call_attempt": spend.attempts,
                        "max_attempts": self._retry.max_attempts,
                        "error_category": category.value,
                        "error_detail": detail,
                        "http_status": status,
                        "request_id": request_id,
                        "latency_ms": _elapsed_ms(started),
                        "retry_in_seconds": None if delay is None else round(delay, 3),
                    },
                    # Unclassified errors are likely bugs: keep the traceback.
                    exc_info=category is Category.UNEXPECTED,
                )
                if delay is None:
                    # Status errors are not chained: their message embeds the raw
                    # response body, which can echo the request or the key.
                    cause = None if isinstance(exc, openai.APIStatusError) else exc
                    raise spend.fail(category, detail) from cause
                await self._sleep(delay)
            else:
                logger.info(
                    "ai.attempt_succeeded",
                    extra={
                        "provider": PROVIDER_NAME,
                        "model": spend.model,
                        "attempt": attempt,
                        "call_attempt": spend.attempts,
                        "request_id": getattr(response, "_request_id", None),
                        "latency_ms": _elapsed_ms(started),
                    },
                )
                return response
        raise AssertionError("unreachable: the last attempt returns or raises")

    def _redact(self, text: str) -> str:
        # Defence in depth: nothing we build should contain the key.
        return text.replace(self._api_key.get_secret_value(), _REDACTED)


def _classify(exc: BaseException) -> tuple[Category, float | None, int | None]:
    """Map an SDK/transport exception to (category, retry_after_seconds, http_status).

    Classified by exception class and HTTP status, not by error body:
    Token Factory error bodies are not OpenAI-shaped.
    """
    if isinstance(exc, openai.APITimeoutError):  # subclass of APIConnectionError
        return Category.TIMEOUT, None, None
    if isinstance(exc, openai.APIConnectionError):
        return Category.CONNECTION, None, None
    if isinstance(exc, openai.APIStatusError):
        status = exc.status_code
        retry_after = parse_retry_after(exc.response.headers)
        if status == 429:
            return Category.RATE_LIMITED, retry_after, status
        if status == 408:
            return Category.TIMEOUT, retry_after, status
        if status in (501, 505):  # not implemented / unsupported: deterministic
            return Category.INVALID_REQUEST, None, status
        if status >= 500:
            return Category.SERVER_ERROR, retry_after, status
        if status in (401, 403):
            return Category.AUTHENTICATION, None, status
        return Category.INVALID_REQUEST, None, status
    return Category.UNEXPECTED, None, None


def _describe(exc: BaseException) -> str:
    """A storable description of a failure, without raw provider bodies."""
    if isinstance(exc, openai.APIStatusError):
        detail = f"{type(exc).__name__}: HTTP {exc.status_code}"
        summary = _body_summary(exc.body)
        return f"{detail} - {summary}" if summary else detail
    detail = f"{type(exc).__name__}: {exc}"
    # The SDK's connection errors say only "Connection error."; the real
    # reason (DNS, TLS, refused, bad URL) is on the cause chain.
    cause, depth = exc.__cause__, 0
    while cause is not None and depth < 3:
        detail += f" (caused by {type(cause).__name__}: {cause})"
        cause, depth = cause.__cause__, depth + 1
    return detail


def _body_summary(body: object) -> str | None:
    """Keep the useful, non-echoing part of an error body.

    FastAPI-style validation errors keep only ``loc`` and ``msg`` (their
    ``input`` can be the request itself); free-text messages are truncated.
    """
    if not isinstance(body, dict):
        return None
    detail = body.get("detail", body.get("message"))
    if isinstance(detail, str):
        return detail[:_MAX_BODY_SUMMARY_CHARS]
    if isinstance(detail, list):
        parts = [
            f"{'.'.join(str(p) for p in item.get('loc', []))}: {item.get('msg', '')}"
            for item in detail[:5]
            if isinstance(item, dict)
        ]
        return "; ".join(parts)[:_MAX_BODY_SUMMARY_CHARS] or None
    return None


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


def _elapsed_ms(started: float) -> int:
    return round((time.perf_counter() - started) * 1000)
