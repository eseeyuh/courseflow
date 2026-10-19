# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""An extension boundary for a future reranking stage. **Not an active stage.**

Nothing in search imports this module: no mode, setting or CLI option calls
a reranker, and there is no implementation (no model, no provider). It only
fixes the contract a future reranker must meet, so that adding one later
cannot weaken what search guarantees:

- a reranker reorders the candidates it is given; it never adds, drops or
  duplicates one, so it cannot widen the verified, course-scoped corpus;
- it sees chunk identities and text only; evidence (SourceSpans) is still
  assembled and verified by search afterwards;
- its output is a total order, checked by ``check_reranking`` before use.

A reranker would be wired in only after a benchmark shows a gap it closes
(the retrieval/v1 benchmark shows none: every positive case is in the top 5
in every mode), as a new, versioned search contract.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class RerankCandidate:
    """One first-stage hit offered for reordering."""

    chunk_id: uuid.UUID
    rank: int  # 1-based, in the first-stage order
    text: str


@dataclass(frozen=True, slots=True)
class Reranking:
    """The new order, as chunk IDs (best first), and the reranker's version."""

    reranker_version: str
    order: tuple[uuid.UUID, ...]


class Reranker(Protocol):
    @property
    def version(self) -> str:
        """The reranker's contract version, reported with any result it orders."""
        ...

    async def rerank(self, query: str, candidates: Sequence[RerankCandidate]) -> Reranking:
        """A permutation of ``candidates``; see ``check_reranking``."""
        ...


class RerankContractError(ValueError):
    """A reranker returned something other than a permutation of its input."""


def check_reranking(
    candidates: Sequence[RerankCandidate], reranking: Reranking, *, version: str
) -> None:
    """Refuse any reranking that is not exactly a reordering of ``candidates``."""
    if reranking.reranker_version != version:
        raise RerankContractError("the reranking names another reranker version")
    given = [c.chunk_id for c in candidates]
    if len(set(given)) != len(given):
        raise RerankContractError("the candidates repeat a chunk")
    if [c.rank for c in candidates] != list(range(1, len(candidates) + 1)):
        raise RerankContractError("the candidates are not ranked 1..n in order")
    if len(reranking.order) != len(given) or set(reranking.order) != set(given):
        raise RerankContractError("the reranking adds, drops or repeats a candidate")
