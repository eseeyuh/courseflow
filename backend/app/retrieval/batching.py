# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Split embedding inputs into provider requests under CourseFlow's own budgets.

A batch closes when adding the next input would exceed either the item limit
or the estimated-token budget. Both are CourseFlow's conservative budgets, not
verified provider limits; tokens are the same ``ceil(chars / 4)`` estimate
the chunker uses. Input order is preserved, so the same inputs always give
the same batches.
"""

from __future__ import annotations

from collections.abc import Sequence

from app.ai.embeddings import EmbeddingInput
from app.retrieval.chunking import estimate_tokens


def plan_batches(
    inputs: Sequence[EmbeddingInput], *, max_items: int, max_estimated_tokens: int
) -> list[list[EmbeddingInput]]:
    if max_items < 1 or max_estimated_tokens < 1:
        raise ValueError("batch limits must be positive")
    batches: list[list[EmbeddingInput]] = []
    current: list[EmbeddingInput] = []
    current_tokens = 0
    for position, item in enumerate(inputs):
        if not item.text.strip():
            raise ValueError(f"embedding input {position} is empty")
        tokens = estimate_tokens(item.text)
        if tokens > max_estimated_tokens:
            raise ValueError(
                f"embedding input {position} estimates {tokens} tokens; "
                f"the batch budget is {max_estimated_tokens}"
            )
        if current and (
            len(current) == max_items or current_tokens + tokens > max_estimated_tokens
        ):
            batches.append(current)
            current, current_tokens = [], 0
        current.append(item)
        current_tokens += tokens
    if current:
        batches.append(current)
    return batches
