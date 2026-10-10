# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Scripted Token Factory responses for provider tests, without network.

The real ``openai`` SDK runs against an ``httpx2.MockTransport``, so tests
exercise the SDK's own HTTP-status -> exception mapping, not a hand-made
imitation of it.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx2
import openai
from pydantic import SecretStr

from app.ai.nebius import NebiusTokenFactoryProvider
from app.ai.retry import RetryPolicy
from app.domain.enums import ModelRole

API_KEY = "sk-test-secret-value-1234567890"
BASE_URL = "https://token-factory.test/v1/"
FAST_MODEL = "vendor/fast-model"
STRONG_MODEL = "vendor/strong-model"

VALID_FACT = {
    "kind": "deadline",
    "value": "23:59 on 31 October 2026",
    "evidence_text": "submitted via Moodle by 23:59 on 31 October 2026",
}

Step = Callable[[httpx2.Request], httpx2.Response]


def completion(
    content: str | None,
    *,
    finish_reason: str = "stop",
    prompt_tokens: int = 100,
    completion_tokens: int = 50,
    reasoning_tokens: int | None = 30,
    request_id: str | None = "req-ok",
    usage: bool = True,
    served_model: str | None = None,
    refusal: str | None = None,
) -> Step:
    """A chat completion; its model echoes the requested one unless overridden."""
    body: dict[str, Any] = {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 1_791_557_022,
        "model": served_model,
        "choices": [
            {
                "index": 0,
                "finish_reason": finish_reason,
                "message": {"role": "assistant", "content": content, "refusal": refusal},
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
            "completion_tokens_details": {"reasoning_tokens": reasoning_tokens},
        },
    }
    if not usage:
        del body["usage"]
    headers = {"x-request-id": request_id} if request_id else {}

    def respond(request: httpx2.Request) -> httpx2.Response:
        model = served_model or json.loads(request.content)["model"]
        return httpx2.Response(200, json=body | {"model": model}, headers=headers)

    return respond


def fact_completion(fact: dict[str, Any] | None = None, **kwargs: Any) -> Step:
    return completion(json.dumps(fact or VALID_FACT), **kwargs)


def status(code: int, headers: dict[str, str] | None = None, body: Any = None) -> Step:
    # Token Factory error bodies use FastAPI's {"detail": ...}, not OpenAI's shape.
    payload = body if body is not None else {"detail": f"scripted {code}"}
    return lambda request: httpx2.Response(code, json=payload, headers=headers or {})


def read_timeout() -> Step:
    def raise_timeout(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout("scripted read timeout", request=request)

    return raise_timeout


def connect_error() -> Step:
    def raise_connect(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("scripted connection failure", request=request)

    return raise_connect


@dataclass
class ScriptedServer:
    """Plays one step per HTTP request and records every request it received."""

    steps: list[Step]
    requests: list[httpx2.Request] = field(default_factory=list)

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if not self.steps:
            raise AssertionError(f"unexpected extra request #{len(self.requests)}")
        return self.steps.pop(0)(request)

    def bodies(self) -> list[dict[str, Any]]:
        return [json.loads(r.content) for r in self.requests]


@dataclass
class RecordedSleeps:
    delays: list[float] = field(default_factory=list)

    async def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


def make_provider(
    server: ScriptedServer,
    sleeps: RecordedSleeps | None = None,
    *,
    max_attempts: int = 3,
    max_repair_attempts: int = 1,
) -> NebiusTokenFactoryProvider:
    return NebiusTokenFactoryProvider(
        api_key=SecretStr(API_KEY),
        base_url=BASE_URL,
        models={ModelRole.FAST: FAST_MODEL, ModelRole.STRONG: STRONG_MODEL},
        timeout_seconds=5.0,
        retry_policy=RetryPolicy(
            max_attempts=max_attempts, base_delay_seconds=1.0, max_delay_seconds=8.0
        ),
        max_repair_attempts=max_repair_attempts,
        max_output_tokens=4096,
        http_client=openai.DefaultAsyncHttpx2Client(transport=httpx2.MockTransport(server)),
        sleep=sleeps or RecordedSleeps(),
        rand=lambda: 1.0,  # jitter at its ceiling: delays become deterministic
    )
