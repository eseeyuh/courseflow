# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

import pytest

from app.ai.provider import TokenUsage
from app.ai.retry import RetryPolicy, parse_retry_after

POLICY = RetryPolicy(max_attempts=6, base_delay_seconds=1.0, max_delay_seconds=10.0)


@pytest.mark.parametrize(("retry_number", "ceiling"), [(1, 1.0), (2, 2.0), (3, 4.0), (4, 8.0)])
def test_backoff_ceiling_doubles(retry_number: int, ceiling: float) -> None:
    assert POLICY.delay_before_retry(retry_number, retry_after=None, rand=lambda: 1.0) == ceiling


def test_backoff_is_capped() -> None:
    assert POLICY.delay_before_retry(10, retry_after=None, rand=lambda: 1.0) == 10.0


def test_full_jitter_scales_the_ceiling() -> None:
    assert POLICY.delay_before_retry(3, retry_after=None, rand=lambda: 0.25) == 1.0
    assert POLICY.delay_before_retry(3, retry_after=None, rand=lambda: 0.0) == 0.0


def test_retry_after_overrides_backoff() -> None:
    assert POLICY.delay_before_retry(1, retry_after=7.5, rand=lambda: 1.0) == 7.5
    assert POLICY.delay_before_retry(4, retry_after=0.5, rand=lambda: 1.0) == 0.5


def test_retry_after_beyond_the_cap_means_do_not_retry() -> None:
    assert POLICY.delay_before_retry(1, retry_after=600, rand=lambda: 1.0) is None


@pytest.mark.parametrize(
    "kwargs",
    [{"max_attempts": 0}, {"base_delay_seconds": 0}, {"max_delay_seconds": -1}],
)
def test_policy_rejects_unbounded_or_nonsense_values(kwargs: dict) -> None:
    with pytest.raises(ValueError):
        RetryPolicy(**kwargs)


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({"retry-after": "3"}, 3.0),
        ({"retry-after": "1.5"}, 1.5),
        ({"retry-after-ms": "250"}, 0.25),
        ({"retry-after-ms": "250", "retry-after": "9"}, 0.25),
        ({"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"}, None),
        ({"retry-after": "-1"}, None),
        ({}, None),
    ],
)
def test_parse_retry_after(headers: dict[str, str], expected: float | None) -> None:
    assert parse_retry_after(headers) == expected


def test_token_usage_adds_and_unknown_stays_unknown() -> None:
    assert TokenUsage(10, 5, 2) + TokenUsage(1, 2, 1) == TokenUsage(11, 7, 3)
    # A partial sum is never presented as complete.
    assert TokenUsage(10, 5, None) + TokenUsage(1, 2, 1) == TokenUsage(11, 7, None)
    assert TokenUsage(None, 3, 1) + TokenUsage(4, 2, 1) == TokenUsage(None, 5, 2)
