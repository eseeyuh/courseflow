# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""The chunk-size variant is frozen, production span-pack/v1 is unchanged,
and the placeholder vectors can never be mistaken for a real space."""

from __future__ import annotations

import dataclasses

import pytest

from app.ai.embeddings import document_input
from app.ai.nebius_transport import PROVIDER_NAME
from app.evaluation.retrieval.chunk_variant import (
    CONFIGS,
    MODE,
    SPAN_PACK_EVAL_SMALL_V1,
    PlaceholderEmbeddingProvider,
)
from app.retrieval.chunking import SPAN_PACK_V1
from app.retrieval.search import SearchMode

SIZING = {
    "target_estimated_tokens",
    "split_above_estimated_tokens",
    "hard_max_estimated_tokens",
    "overlap_estimated_tokens",
    "min_estimated_tokens",
}


def test_the_variant_is_frozen_and_differs_from_production_only_in_sizing() -> None:
    assert dataclasses.asdict(SPAN_PACK_EVAL_SMALL_V1) == {
        "version": "span-pack-eval-small/v1",
        "target_estimated_tokens": 600,
        "split_above_estimated_tokens": 680,
        "hard_max_estimated_tokens": 800,
        "overlap_estimated_tokens": 100,
        "min_estimated_tokens": 160,
        "split_piece_min_estimated_tokens": 400,
        "chars_per_estimated_token": 4,
    }
    prod, variant = dataclasses.asdict(SPAN_PACK_V1), dataclasses.asdict(SPAN_PACK_EVAL_SMALL_V1)
    assert {k for k in prod if prod[k] != variant[k]} == SIZING | {"version"}
    assert CONFIGS == (SPAN_PACK_V1, SPAN_PACK_EVAL_SMALL_V1)


def test_production_span_pack_v1_is_unchanged() -> None:
    assert dataclasses.astuple(SPAN_PACK_V1) == ("span-pack/v1", 800, 1000, 120, 880, 200, 400, 4)


def test_the_experiment_scores_only_the_mode_that_never_reads_vectors() -> None:
    assert MODE is SearchMode.LEXICAL


@pytest.mark.anyio
async def test_placeholder_vectors_are_deterministic_and_in_their_own_space() -> None:
    provider = PlaceholderEmbeddingProvider()
    assert provider.space.provider == "placeholder" != PROVIDER_NAME
    item = document_input("some chunk text")
    first, second = await provider.embed([item]), await provider.embed([item])
    assert first.vectors == second.vectors and any(first.vectors[0])
    assert len(first.vectors[0]) == provider.space.dimensions
    assert first.usage.prompt_tokens is None and provider.calls == 2
