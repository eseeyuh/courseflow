# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""The embedding batch planner: both budgets, order, and refusals."""

import pytest

from app.ai.embeddings import DOCUMENT_INPUT_FORMAT, EmbeddingInput, document_input
from app.core.config import Settings
from app.retrieval.batching import plan_batches
from app.retrieval.chunking import SPAN_PACK_V1


def _inputs(*sizes: int) -> list[EmbeddingInput]:
    """Inputs of exactly ``size`` characters each (estimate = ceil(size / 4))."""
    return [document_input(f"{i}" + "x" * (size - 1)) for i, size in enumerate(sizes)]


def _shape(batches: list[list[EmbeddingInput]]) -> list[list[str]]:
    return [[item.text[0] for item in batch] for batch in batches]


def test_a_batch_closes_at_the_item_limit() -> None:
    batches = plan_batches(_inputs(4, 4, 4, 4, 4), max_items=2, max_estimated_tokens=100)

    assert _shape(batches) == [["0", "1"], ["2", "3"], ["4"]]


def test_a_batch_closes_before_exceeding_the_token_budget() -> None:
    # Estimates 3, 3, 4, 1: budget 7 -> [3, 3] (adding 4 would make 10), [4, 1].
    batches = plan_batches(_inputs(12, 12, 16, 4), max_items=10, max_estimated_tokens=7)

    assert _shape(batches) == [["0", "1"], ["2", "3"]]


def test_an_exact_fit_stays_in_one_batch() -> None:
    batches = plan_batches(_inputs(16, 12), max_items=2, max_estimated_tokens=7)

    assert _shape(batches) == [["0", "1"]]


def test_order_is_preserved_and_planning_is_deterministic() -> None:
    inputs = _inputs(*[(i * 7) % 40 + 1 for i in range(50)])

    first = plan_batches(inputs, max_items=8, max_estimated_tokens=30)
    second = plan_batches(inputs, max_items=8, max_estimated_tokens=30)

    assert first == second
    assert [item for batch in first for item in batch] == inputs


def test_no_inputs_means_no_batches() -> None:
    assert plan_batches([], max_items=4, max_estimated_tokens=10) == []


def test_an_input_above_the_token_budget_is_refused() -> None:
    with pytest.raises(ValueError, match="estimates 3 tokens; the batch budget is 2"):
        plan_batches(_inputs(12), max_items=4, max_estimated_tokens=2)


def test_blank_inputs_are_refused() -> None:
    with pytest.raises(ValueError, match="input 1 is empty"):
        plan_batches(
            [document_input("a"), EmbeddingInput(" ", DOCUMENT_INPUT_FORMAT)],
            max_items=4,
            max_estimated_tokens=10,
        )


@pytest.mark.parametrize("limits", [(0, 10), (4, 0)])
def test_limits_must_be_positive(limits: tuple[int, int]) -> None:
    with pytest.raises(ValueError, match="positive"):
        plan_batches([], max_items=limits[0], max_estimated_tokens=limits[1])


def test_any_span_pack_v1_chunk_fits_the_default_batch_budget() -> None:
    settings = Settings(_env_file=None, database_url="postgresql+asyncpg://u:p@h/db")  # type: ignore[call-arg, arg-type]

    assert SPAN_PACK_V1.hard_max_estimated_tokens < settings.embedding_batch_max_estimated_tokens
    biggest = document_input("y" * SPAN_PACK_V1.chars(SPAN_PACK_V1.hard_max_estimated_tokens))
    assert plan_batches(
        [biggest],
        max_items=settings.embedding_batch_max_items,
        max_estimated_tokens=settings.embedding_batch_max_estimated_tokens,
    ) == [[biggest]]
