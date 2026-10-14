# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Transport shared by every Nebius Token Factory provider (chat, embeddings).

One ``AsyncOpenAI`` client with the SDK's own retries disabled; failures are
classified here, and only transient ones are retried, by our ``RetryPolicy``,
so every attempt is visible in logs and counted.

Error details are sanitised: no API key, no raw provider bodies (they can
echo the request). Callers whose requests contain user or document text can
also drop free-text body messages (``body_text=False``).
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import openai
from pydantic import SecretStr

from app.ai.errors import RETRIABLE_CATEGORIES, AIConfigurationError, AIProviderError
from app.ai.provider import TokenUsage
from app.ai.retry import RetryPolicy, parse_retry_after
from app.core.config import Settings
from app.domain.enums import ModelCallErrorCategory as Category

PROVIDER_NAME = "nebius-token-factory"
_MAX_DETAIL_CHARS = 500
_MAX_BODY_SUMMARY_CHARS = 200
_REDACTED = "**********"


@dataclass(slots=True)
class Spend:
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


def missing_settings(**values: object) -> AIConfigurationError:
    """The error naming every setting in ``values`` that is unset."""
    missing = [name for name, value in values.items() if value is None]
    return AIConfigurationError(f"AI provider not configured; missing: {', '.join(missing)}")


def retry_policy_from(settings: Settings) -> RetryPolicy:
    return RetryPolicy(
        max_attempts=settings.ai_max_attempts,
        base_delay_seconds=settings.ai_backoff_base_seconds,
        max_delay_seconds=settings.ai_backoff_max_seconds,
    )


class NebiusTransport:
    def __init__(
        self,
        *,
        api_key: SecretStr,
        base_url: str,
        timeout_seconds: float,
        retry_policy: RetryPolicy,
        logger: logging.Logger,
        body_text: bool = True,
        http_client: openai.DefaultAsyncHttpx2Client | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rand: Callable[[], float] = random.random,
    ) -> None:
        self._api_key = api_key
        self._retry = retry_policy
        self._logger = logger
        self._body_text = body_text
        self._sleep = sleep
        self._rand = rand
        self.client = openai.AsyncOpenAI(
            api_key=api_key.get_secret_value(),
            base_url=base_url,
            timeout=timeout_seconds,
            max_retries=0,  # retries are ours: classified, bounded and logged
            http_client=http_client,
        )

    async def aclose(self) -> None:
        await self.client.close()

    async def with_retries[R](self, spend: Spend, send: Callable[[], Awaitable[R]]) -> R:
        for attempt in range(1, self._retry.max_attempts + 1):
            spend.attempts += 1
            started = time.perf_counter()
            try:
                response = await send()
            except Exception as exc:  # every failure is classified below
                category, retry_after, status = classify(exc)
                request_id = getattr(exc, "request_id", None)
                spend.request_id = request_id or spend.request_id
                detail = self.describe(exc)
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
                self._logger.warning(
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
                        "latency_ms": elapsed_ms(started),
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
                self._logger.info(
                    "ai.attempt_succeeded",
                    extra={
                        "provider": PROVIDER_NAME,
                        "model": spend.model,
                        "attempt": attempt,
                        "call_attempt": spend.attempts,
                        "request_id": getattr(response, "_request_id", None),
                        "latency_ms": elapsed_ms(started),
                    },
                )
                return response
        raise AssertionError("unreachable: the last attempt returns or raises")

    def describe(self, exc: BaseException) -> str:
        """A storable, redacted description of a failure."""
        return self.redact(describe(exc, body_text=self._body_text))

    def redact(self, text: str) -> str:
        # Defence in depth: nothing we build should contain the key.
        return text.replace(self._api_key.get_secret_value(), _REDACTED)


def classify(exc: BaseException) -> tuple[Category, float | None, int | None]:
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


def describe(exc: BaseException, *, body_text: bool = True) -> str:
    """A storable description of a failure, without raw provider bodies.

    ``body_text=False`` also drops free-text body messages, keeping only the
    status and validation-error locations.
    """
    if isinstance(exc, openai.APIStatusError):
        detail = f"{type(exc).__name__}: HTTP {exc.status_code}"
        summary = _body_summary(exc.body, body_text=body_text)
        return f"{detail} - {summary}" if summary else detail
    detail = f"{type(exc).__name__}: {exc}"
    # The SDK's connection errors say only "Connection error."; the real
    # reason (DNS, TLS, refused, bad URL) is on the cause chain.
    cause, depth = exc.__cause__, 0
    while cause is not None and depth < 3:
        detail += f" (caused by {type(cause).__name__}: {cause})"
        cause, depth = cause.__cause__, depth + 1
    return detail


def _body_summary(body: object, *, body_text: bool) -> str | None:
    """Keep the useful, non-echoing part of an error body.

    FastAPI-style validation errors keep only ``loc`` and ``msg`` (their
    ``input`` can be the request itself); free-text messages are truncated,
    or dropped entirely when ``body_text`` is false.
    """
    if not isinstance(body, dict):
        return None
    detail = body.get("detail", body.get("message"))
    if isinstance(detail, str):
        return detail[:_MAX_BODY_SUMMARY_CHARS] if body_text else None
    if isinstance(detail, list):
        parts = [
            _validation_item(item, body_text=body_text)
            for item in detail[:5]
            if isinstance(item, dict)
        ]
        return "; ".join(parts)[:_MAX_BODY_SUMMARY_CHARS] or None
    return None


def _validation_item(item: dict[str, object], *, body_text: bool) -> str:
    raw_loc = item.get("loc")
    loc = ".".join(str(p) for p in raw_loc) if isinstance(raw_loc, list) else ""
    return f"{loc}: {item.get('msg', '')}" if body_text else loc


def elapsed_ms(started: float) -> int:
    return round((time.perf_counter() - started) * 1000)
