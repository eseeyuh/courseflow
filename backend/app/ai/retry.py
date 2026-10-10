# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Bounded retry with capped exponential backoff and full jitter."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    # Total tries for one request, including the first. Never unbounded.
    max_attempts: int = 3
    base_delay_seconds: float = 1.0
    max_delay_seconds: float = 20.0

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if self.base_delay_seconds <= 0 or self.max_delay_seconds <= 0:
            raise ValueError("delays must be positive")

    def delay_before_retry(
        self,
        retry_number: int,
        *,
        retry_after: float | None,
        rand: Callable[[], float],
    ) -> float | None:
        """Seconds to wait before retry ``retry_number`` (1 = first retry), or
        ``None``: the server asked for longer than we are willing to wait.

        A server-provided Retry-After wins: retrying sooner than asked would
        almost certainly fail again. Otherwise: full jitter, uniform in
        [0, min(max, base * 2^(n-1))], so clients that failed together do not
        retry together.
        """
        if retry_after is not None:
            return None if retry_after > self.max_delay_seconds else max(retry_after, 0.0)
        ceiling = min(self.max_delay_seconds, self.base_delay_seconds * 2 ** (retry_number - 1))
        return rand() * ceiling


def parse_retry_after(headers: Mapping[str, str]) -> float | None:
    """Seconds from ``retry-after-ms`` or numeric ``retry-after``; ``None`` otherwise.

    HTTP-date values are ignored: our own backoff is used instead.
    """
    for name, scale in (("retry-after-ms", 0.001), ("retry-after", 1.0)):
        raw = headers.get(name)
        if raw is None:
            continue
        try:
            seconds = float(raw) * scale
        except ValueError:
            continue
        if seconds >= 0:
            return seconds
    return None
