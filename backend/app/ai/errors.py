# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Provider-neutral failures of model calls."""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.domain.enums import ModelCallErrorCategory

if TYPE_CHECKING:
    from app.ai.provider import TokenUsage

# Transient: the same request may succeed later, so it is retried with backoff.
# Everything else is deterministic for an identical request and fails fast.
RETRIABLE_CATEGORIES = frozenset(
    {
        ModelCallErrorCategory.TIMEOUT,
        ModelCallErrorCategory.CONNECTION,
        ModelCallErrorCategory.RATE_LIMITED,
        ModelCallErrorCategory.SERVER_ERROR,
    }
)


class AIConfigurationError(RuntimeError):
    """The provider cannot be built: missing key, base URL or model ID."""


class AIProviderError(Exception):
    """A model call failed after the provider's retry and repair budget.

    Carries what was spent before failing (attempts, tokens) so the failure
    can be recorded as faithfully as a success.
    """

    def __init__(
        self,
        category: ModelCallErrorCategory,
        detail: str,
        *,
        model: str,
        attempts: int,
        usage: TokenUsage,
        repair_rounds: int = 0,
        request_id: str | None = None,
    ) -> None:
        super().__init__(f"{category.value}: {detail}")
        self.category = category
        self.detail = detail
        self.model = model
        self.attempts = attempts
        self.repair_rounds = repair_rounds
        self.usage = usage
        self.request_id = request_id

    @property
    def retriable(self) -> bool:
        return self.category in RETRIABLE_CATEGORIES
