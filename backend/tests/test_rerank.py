# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""The reranker extension boundary: its contract, with a fake implementation,
and proof that nothing in the running system uses it."""

from __future__ import annotations

import ast
import uuid
from collections.abc import Sequence
from pathlib import Path

import pytest

from app.retrieval import rerank
from app.retrieval.rerank import (
    RerankCandidate,
    RerankContractError,
    Reranker,
    Reranking,
    check_reranking,
)

pytestmark = pytest.mark.anyio

IDS = [uuid.UUID(int=n) for n in range(1, 5)]
CANDIDATES = [RerankCandidate(chunk_id=c, rank=r, text=f"t{r}") for r, c in enumerate(IDS, 1)]


class ReverseReranker:
    """A fake: reverses the first-stage order."""

    version = "fake-reverse/v1"

    async def rerank(self, query: str, candidates: Sequence[RerankCandidate]) -> Reranking:
        return Reranking(self.version, tuple(c.chunk_id for c in reversed(candidates)))


async def test_a_conforming_reranker_returns_a_checked_permutation() -> None:
    reranker: Reranker = ReverseReranker()
    result = await reranker.rerank("q", CANDIDATES)
    check_reranking(CANDIDATES, result, version=reranker.version)
    assert result.order == tuple(reversed(IDS))


@pytest.mark.parametrize(
    ("order", "message"),
    [
        (tuple(IDS[:-1]), "adds, drops or repeats"),
        ((*IDS, uuid.UUID(int=99)), "adds, drops or repeats"),
        ((IDS[0], IDS[0], IDS[1], IDS[2]), "adds, drops or repeats"),
    ],
)
async def test_a_reranking_that_changes_the_candidate_set_is_refused(
    order: tuple[uuid.UUID, ...], message: str
) -> None:
    with pytest.raises(RerankContractError, match=message):
        check_reranking(CANDIDATES, Reranking("fake-reverse/v1", order), version="fake-reverse/v1")


async def test_malformed_input_or_a_foreign_version_is_refused() -> None:
    with pytest.raises(RerankContractError, match="another reranker version"):
        check_reranking(CANDIDATES, Reranking("other/v1", tuple(IDS)), version="fake-reverse/v1")
    unranked = [RerankCandidate(c.chunk_id, c.rank + 1, c.text) for c in CANDIDATES]
    with pytest.raises(RerankContractError, match="1..n"):
        check_reranking(unranked, Reranking("v", tuple(IDS)), version="v")


def test_the_reranker_is_not_wired_into_anything() -> None:
    app_dir = Path(rerank.__file__).resolve().parents[1]
    users = []
    for path in app_dir.rglob("*.py"):
        if path.name == "rerank.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").endswith("rerank"):
                users.append(path.name)
            if isinstance(node, ast.Import) and any(a.name.endswith("rerank") for a in node.names):
                users.append(path.name)
    assert users == []
