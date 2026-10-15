# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Reciprocal rank fusion (RRF) of one vector and one lexical ranking.

Pure and deterministic: no database, no I/O, no clock. A chunk's fused score
is the exact rational sum, over the rankings that returned it, of

    1 / (RRF_K + rank)        rank: its 1-based rank in that ranking

so equal sums are exactly equal and the order never depends on float
rounding. Only ranks are fused: the raw scores (cosine distance,
``ts_rank_cd``) are not comparable with each other and are carried through
untouched, as diagnostics. The fused score orders candidates; it is not a
confidence or a probability.

The fusion is symmetric: one empty ranking leaves the other's order
unchanged, and two empty rankings fuse to nothing. Ties break by a frozen
total order (``fused_order_key``), so the whole fused order is deterministic
and any top-k is a prefix of it.

Inputs must be well-formed rankings (each ranker's own contract); anything
else is refused (FusionInputError), never silently repaired.
"""

from __future__ import annotations

import math
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, replace
from fractions import Fraction

from app.retrieval.rankers import RankedCandidate

RRF_K = 60
CANDIDATE_DEPTH = 50  # each ranker contributes at most this many candidates
# The fusion contract: RRF over ranks with RRF_K, CANDIDATE_DEPTH candidates
# per ranker and the tie-break of fused_order_key. Changing any part of it is
# a new version.
FUSION_VERSION = "rrf-k60-depth50/v1"


class FusionInputError(ValueError):
    """A ranking given to the fusion is not a well-formed ranking."""


@dataclass(frozen=True, slots=True)
class FusedCandidate:
    """One chunk in the fused order, with what each ranking said about it
    (None: that ranking did not return it)."""

    chunk_id: uuid.UUID
    resource_version_id: uuid.UUID
    chunk_ordinal: int
    rank: int  # 1-based, in the fused order
    fused_score: Fraction  # exact; an ordering value, never a confidence
    vector_rank: int | None
    lexical_rank: int | None
    vector_distance: float | None
    lexical_score: float | None


def rrf_contribution(rank: int) -> Fraction:
    """What one ranking adds for a chunk at ``rank`` (1-based)."""
    if rank < 1:
        raise FusionInputError("ranks start at 1")
    return Fraction(1, RRF_K + rank)


def fused_order_key(candidate: FusedCandidate) -> tuple[object, ...]:
    """The frozen fused order: fused score descending, then the best single
    rank, the vector rank, the lexical rank (a missing rank sorts last), the
    resource version, the chunk ordinal and the chunk ID."""
    ranks = [r for r in (candidate.vector_rank, candidate.lexical_rank) if r is not None]
    return (
        -candidate.fused_score,
        min(ranks),
        math.inf if candidate.vector_rank is None else candidate.vector_rank,
        math.inf if candidate.lexical_rank is None else candidate.lexical_rank,
        candidate.resource_version_id,
        candidate.chunk_ordinal,
        candidate.chunk_id,
    )


def fuse_rrf(
    vector: Sequence[RankedCandidate], lexical: Sequence[RankedCandidate]
) -> list[FusedCandidate]:
    """The whole fused order of both rankings (deduplicated by chunk ID),
    ranked from 1. Truncate it for a top-k: the order does not depend on k."""
    by_vector = _ranking(vector, "vector")
    by_lexical = _ranking(lexical, "lexical")
    unranked = [
        _fused(chunk_id, by_vector.get(chunk_id), by_lexical.get(chunk_id))
        for chunk_id in by_vector.keys() | by_lexical.keys()
    ]
    ordered = sorted(unranked, key=fused_order_key)
    return [replace(c, rank=rank) for rank, c in enumerate(ordered, start=1)]


def _fused(
    chunk_id: uuid.UUID, v: RankedCandidate | None, lex: RankedCandidate | None
) -> FusedCandidate:
    """One chunk's summed contributions (rank 0: not yet ordered)."""
    identity = v if v is not None else lex
    assert identity is not None
    if (
        v is not None
        and lex is not None
        and (
            (v.resource_version_id, v.chunk_ordinal) != (lex.resource_version_id, lex.chunk_ordinal)
        )
    ):
        raise FusionInputError(f"chunk {chunk_id} has a different identity in each ranking")
    return FusedCandidate(
        chunk_id=chunk_id,
        resource_version_id=identity.resource_version_id,
        chunk_ordinal=identity.chunk_ordinal,
        rank=0,
        fused_score=sum((rrf_contribution(c.rank) for c in (v, lex) if c is not None), Fraction(0)),
        vector_rank=None if v is None else v.rank,
        lexical_rank=None if lex is None else lex.rank,
        vector_distance=None if v is None else v.vector_distance,
        lexical_score=None if lex is None else lex.lexical_score,
    )


def _ranking(candidates: Sequence[RankedCandidate], kind: str) -> dict[uuid.UUID, RankedCandidate]:
    """One ranking by chunk ID, refusing anything that is not a well-formed
    ranking from that ranker: ranks 1..n in order, each chunk once, its own
    raw score set, at most CANDIDATE_DEPTH candidates."""
    if len(candidates) > CANDIDATE_DEPTH:
        raise FusionInputError(f"the {kind} ranking is deeper than {CANDIDATE_DEPTH}")
    by_chunk: dict[uuid.UUID, RankedCandidate] = {}
    for position, candidate in enumerate(candidates, start=1):
        if candidate.rank != position:
            raise FusionInputError(f"the {kind} ranking's ranks are not 1..n in order")
        if candidate.chunk_id in by_chunk:
            raise FusionInputError(f"chunk {candidate.chunk_id} is ranked twice by {kind}")
        own_score = candidate.vector_distance if kind == "vector" else candidate.lexical_score
        if own_score is None:
            raise FusionInputError(f"a {kind} candidate has no {kind} score")
        by_chunk[candidate.chunk_id] = candidate
    return by_chunk
