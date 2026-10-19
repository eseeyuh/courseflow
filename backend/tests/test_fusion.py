# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Reciprocal rank fusion as a pure function: hand-computed exact sums, the
symmetric single-ranking cases, deduplication, the frozen tie-break and the
refusal of malformed rankings. No database."""

import uuid
from collections.abc import Sequence
from fractions import Fraction

import pytest

from app.retrieval.fusion import (
    CANDIDATE_DEPTH,
    FUSION_VERSION,
    RRF_K,
    FusedCandidate,
    FusionInputError,
    fuse_rrf,
    fused_order_key,
    rrf_contribution,
)
from app.retrieval.rankers import RankedCandidate

VERSION = uuid.UUID(int=1_000)


def _id(n: int) -> uuid.UUID:
    return uuid.UUID(int=n)


def _ranked(
    chunk: uuid.UUID, rank: int, ordinal: int | None = None, **scores: float
) -> RankedCandidate:
    ordinal = chunk.int if ordinal is None else ordinal
    return RankedCandidate(
        chunk, VERSION, ordinal, rank=rank, source_uri="upload:a", version_number=1, **scores
    )


def _vector(*chunks: uuid.UUID) -> list[RankedCandidate]:
    return [_ranked(c, r, vector_distance=r / 100) for r, c in enumerate(chunks, start=1)]


def _lexical(*chunks: uuid.UUID) -> list[RankedCandidate]:
    return [_ranked(c, r, lexical_score=1 / r) for r, c in enumerate(chunks, start=1)]


def _order(fused: Sequence[FusedCandidate]) -> list[uuid.UUID]:
    return [c.chunk_id for c in fused]


A, B, C, D = _id(1), _id(2), _id(3), _id(4)


def test_the_contract_constants_are_frozen() -> None:
    assert (RRF_K, CANDIDATE_DEPTH, FUSION_VERSION) == (60, 50, "rrf-k60-depth50/v2")


def test_a_contribution_is_the_exact_reciprocal_of_k_plus_rank() -> None:
    assert rrf_contribution(1) == Fraction(1, 61)
    assert rrf_contribution(50) == Fraction(1, 110)
    with pytest.raises(FusionInputError):
        rrf_contribution(0)


def test_a_hand_computed_fusion_sums_both_rankings_exactly() -> None:
    #           vector  lexical  fused
    # A          1       2       1/61 + 1/62 = 123/3782
    # B          2       -       1/62
    # C          3       1       1/63 + 1/61 = 124/3843
    # D          -       3       1/63
    fused = fuse_rrf(_vector(A, B, C), _lexical(C, A, D))

    assert _order(fused) == [A, C, B, D]
    assert [c.fused_score for c in fused] == [
        Fraction(123, 3782),
        Fraction(124, 3843),
        Fraction(1, 62),
        Fraction(1, 63),
    ]
    assert all(type(c.fused_score) is Fraction for c in fused)
    assert [c.rank for c in fused] == [1, 2, 3, 4]
    assert [(c.vector_rank, c.lexical_rank) for c in fused] == [
        (1, 2),
        (3, 1),
        (2, None),
        (None, 3),
    ]


def test_raw_scores_are_carried_untouched_and_never_fused() -> None:
    vector, lexical = _vector(A, B), _lexical(B, C)

    by_id = {c.chunk_id: c for c in fuse_rrf(vector, lexical)}

    assert (by_id[A].vector_distance, by_id[A].lexical_score) == (0.01, None)
    assert (by_id[B].vector_distance, by_id[B].lexical_score) == (0.02, 1.0)
    assert (by_id[C].vector_distance, by_id[C].lexical_score) == (None, 0.5)
    assert by_id[B].fused_score == Fraction(1, 62) + Fraction(1, 61)


def test_vector_only_keeps_the_vector_order() -> None:
    fused = fuse_rrf(_vector(C, A, D, B), [])

    assert _order(fused) == [C, A, D, B]
    assert [c.fused_score for c in fused] == [Fraction(1, 60 + r) for r in (1, 2, 3, 4)]
    assert all(c.lexical_rank is None and c.lexical_score is None for c in fused)


def test_lexical_only_keeps_the_lexical_order() -> None:
    fused = fuse_rrf([], _lexical(D, B, C, A))

    assert _order(fused) == [D, B, C, A]
    assert [c.fused_score for c in fused] == [Fraction(1, 60 + r) for r in (1, 2, 3, 4)]
    assert all(c.vector_rank is None and c.vector_distance is None for c in fused)


def test_two_empty_rankings_fuse_to_nothing() -> None:
    assert fuse_rrf([], []) == []


def test_a_chunk_in_both_rankings_appears_once() -> None:
    fused = fuse_rrf(_vector(A, B), _lexical(B, A))

    assert sorted(_order(fused)) == [A, B]
    assert all(c.vector_rank and c.lexical_rank for c in fused)


def test_a_chunk_ranked_twice_by_one_ranker_is_refused() -> None:
    duplicated = [*_vector(A, B), _ranked(A, 3, vector_distance=0.5)]

    with pytest.raises(FusionInputError, match="ranked twice by vector"):
        fuse_rrf(duplicated, [])


@pytest.mark.parametrize(
    ("vector", "lexical", "message"),
    [
        ([_ranked(A, 2, 1, vector_distance=0.1)], [], "1..n"),
        ([_ranked(A, 1, 1)], [], "no vector score"),
        ([], [_ranked(A, 1, 1, vector_distance=0.1)], "no lexical score"),
        (_vector(*(_id(n) for n in range(1, CANDIDATE_DEPTH + 2))), [], "deeper than 50"),
        (
            [_ranked(A, 1, 1, vector_distance=0.1)],
            [_ranked(A, 1, 2, lexical_score=0.1)],
            "different identity",
        ),
    ],
)
def test_malformed_rankings_are_refused(
    vector: list[RankedCandidate], lexical: list[RankedCandidate], message: str
) -> None:
    with pytest.raises(FusionInputError, match=message):
        fuse_rrf(vector, lexical)


def test_a_full_depth_ranking_is_accepted() -> None:
    chunks = [_id(n) for n in range(1, CANDIDATE_DEPTH + 1)]

    assert _order(fuse_rrf(_vector(*chunks), _lexical(*chunks))) == chunks


# --- the frozen tie-break -------------------------------------------------------------------


def _fillers(start: int, count: int) -> list[uuid.UUID]:
    return [_id(n) for n in range(start, start + count)]


def test_equal_sums_break_by_best_rank_then_vector_rank() -> None:
    # 1/(60+3) + 1/(60+24) = 1/(60+12) + 1/(60+12) = 1/36 exactly.
    x, y, z = _id(900), _id(800), _id(700)  # chunk IDs deliberately in reverse
    vector = _fillers(1_000, 24)
    lexical = _fillers(2_000, 24)
    for chunk, v_rank, l_rank in ((x, 3, 24), (y, 12, 12), (z, 24, 3)):
        vector[v_rank - 1] = chunk
        lexical[l_rank - 1] = chunk

    fused = fuse_rrf(_vector(*vector), _lexical(*lexical))

    assert [c.fused_score for c in fused[:3]] == [Fraction(1, 36)] * 3
    # best rank: x and z (3) before y (12); then the vector rank: x (3) before z (24)
    assert _order(fused[:3]) == [x, z, y]


def test_a_vector_only_chunk_precedes_a_lexical_only_chunk_at_the_same_rank() -> None:
    vector_only, lexical_only = _id(9), _id(1)  # the lexical one has the lower ID

    fused = fuse_rrf(_vector(vector_only), _lexical(lexical_only))

    assert fused[0].fused_score == fused[1].fused_score == Fraction(1, 61)
    assert _order(fused) == [vector_only, lexical_only]


def _candidate(**overrides: object) -> FusedCandidate:
    values: dict[str, object] = {
        "chunk_id": _id(5),
        "resource_version_id": _id(50),
        "chunk_ordinal": 5,
        "rank": 0,
        "fused_score": Fraction(1, 61),
        "source_uri": "upload:m",
        "version_number": 1,
        "vector_rank": 1,
        "lexical_rank": None,
        "vector_distance": 0.1,
        "lexical_score": None,
    }
    return FusedCandidate(**{**values, **overrides})  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ({"fused_score": Fraction(1, 61)}, {"fused_score": Fraction(1, 62)}),
        ({"vector_rank": 1, "lexical_rank": 9}, {"vector_rank": 9, "lexical_rank": 2}),
        ({"vector_rank": 2, "lexical_rank": 2}, {"vector_rank": 3, "lexical_rank": 2}),
        ({"vector_rank": 2, "lexical_rank": None}, {"vector_rank": None, "lexical_rank": 2}),
        ({"vector_rank": 1, "lexical_rank": 2}, {"vector_rank": 1, "lexical_rank": 3}),
        ({"lexical_rank": 2}, {"lexical_rank": None}),
        ({"source_uri": "upload:B"}, {"source_uri": "upload:a"}),  # code points: B < a
        ({"source_uri": "upload:a"}, {"source_uri": "upload:é"}),
        ({"version_number": 1}, {"version_number": 2}),
        ({"chunk_ordinal": 4}, {"chunk_ordinal": 6}),
    ],
)
def test_the_order_key_is_total_through_to_the_natural_identity(
    first: dict[str, object], second: dict[str, object]
) -> None:
    """Each level of the frozen order decides, in its stated direction, once
    every earlier level is equal. Generated IDs are set to disagree, so they
    never decide anything."""
    first = {"chunk_id": _id(9), "resource_version_id": _id(90), **first}
    second = {"chunk_id": _id(1), "resource_version_id": _id(10), **second}
    a, b = _candidate(**first), _candidate(**second)

    assert fused_order_key(a) < fused_order_key(b)
    assert sorted([b, a], key=fused_order_key) == [a, b]


def test_the_fused_order_is_deterministic() -> None:
    vector = _vector(*_fillers(1, 30))
    lexical = _lexical(*reversed(_fillers(15, 30)))

    first, second = fuse_rrf(vector, lexical), fuse_rrf(vector, lexical)

    assert first == second
    assert [c.rank for c in first] == list(range(1, len(first) + 1))
