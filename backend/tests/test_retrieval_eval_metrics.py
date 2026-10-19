# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Retrieval metrics, hard invariants and the default-selection rule, on
synthetic hits (no database, no search)."""

from __future__ import annotations

import json
import uuid
from fractions import Fraction

import pytest

from app.evaluation.retrieval.metrics import (
    CaseResult,
    Hit,
    ModeMetrics,
    first_relevant_rank,
    mode_metrics,
    score_case,
)
from app.evaluation.retrieval.resolve import ResolvedCase, ResolvedText
from app.evaluation.retrieval.schema import RetrievalCase, TextRef
from app.evaluation.retrieval.selection import select_default

V2 = ("ds101", "coursework-brief", 2)
V1 = ("ds101", "coursework-brief", 1)
OTHER = ("snd210", "portfolio-brief", 1)


def target(key: tuple[str, str, int], start: int, end: int) -> ResolvedText:
    return ResolvedText(
        ref=TextRef(course=key[0], resource=key[1], version=key[2], quote="q"),
        version_id=uuid.uuid4(),
        start=start,
        end=end,
        span_id=uuid.uuid4(),
        span_ordinal=0,
        relative_start=0,
        relative_end=end - start,
        containing_chunks=(),
        overlapping_chunks=(),
    )


def hit(
    rank: int, key: tuple[str, str, int] | None, start: int, end: int, *, current: bool = True
) -> Hit:
    return Hit(rank=rank, version=key, current=current, start=start, end=end)


def case(**overrides: object) -> RetrievalCase:
    values: dict[str, object] = {
        "case_id": "c",
        "schema_version": "retrieval-case/v1",
        "course": "ds101",
        "query": "q",
        "category": "keyword",
        "target_groups": [
            {
                "group_id": "g",
                "alternatives": [
                    {"course": "ds101", "resource": "coursework-brief", "version": 2, "quote": "q"}
                ],
            }
        ],
        "critical": False,
        "notes": "n",
    } | overrides
    return RetrievalCase.model_validate_json(json.dumps(values))


def resolved(
    groups: tuple[tuple[ResolvedText, ...], ...] = (), forbidden: tuple = ()
) -> ResolvedCase:
    return ResolvedCase("c", groups, (), forbidden, ())


def test_relevance_needs_full_containment_of_the_target_range() -> None:
    groups = [[target(V2, 100, 150)]]
    assert first_relevant_rank([hit(1, V2, 120, 400)], groups) is None  # partial overlap
    assert first_relevant_rank([hit(1, V2, 0, 149)], groups) is None  # one char short
    assert first_relevant_rank([hit(1, V2, 100, 150)], groups) == 1  # exact bounds
    assert first_relevant_rank([hit(1, V1, 0, 999)], groups) is None  # wrong version
    assert first_relevant_rank([hit(1, V2, 0, 99), hit(2, V2, 90, 160)], groups) == 2


def test_any_alternative_satisfies_a_group_and_every_group_is_needed() -> None:
    either = [[target(V2, 0, 10), target(V2, 500, 510)]]
    assert first_relevant_rank([hit(1, V2, 495, 600)], either) == 1
    both = [[target(V2, 0, 10)], [target(V2, 500, 510)]]
    assert first_relevant_rank([hit(1, V2, 0, 50), hit(2, V2, 400, 600)], both) == 2
    assert first_relevant_rank([hit(1, V2, 0, 50)], both) is None


def test_stale_foreign_forbidden_and_unknown_hits_are_invariant_failures() -> None:
    hits = [
        hit(1, V2, 0, 10),
        hit(2, V1, 0, 10, current=False),
        hit(3, OTHER, 0, 10),
        hit(4, None, 0, 10, current=False),
    ]
    result = score_case(case(), resolved(((target(V2, 0, 10),),)), hits)
    assert result.first_relevant_rank == 1
    assert result.invariant_failures == (
        "rank 2 (ds101/coursework-brief/v1): a non-current version",
        "rank 3 (snd210/portfolio-brief/v1): another course",
        "rank 4 (unknown): a version the dataset did not ingest",
    )
    invariant = case(
        category="stale_version_invariant",
        target_groups=[],
        forbidden=[
            {
                "kind": "stale_version",
                "course": "ds101",
                "resource": "coursework-brief",
                "version": 1,
            }
        ],
    )
    failed = score_case(invariant, resolved(forbidden=(V1,)), [hit(1, V1, 0, 10, current=False)])
    assert failed.first_relevant_rank is None and not failed.positive
    assert "a forbidden version" in failed.invariant_failures[-1]


def test_metrics_exclude_invariant_cases_and_use_exact_fractions() -> None:
    def result(rank: int | None, positive: bool = True) -> CaseResult:
        return CaseResult("c", "keyword", positive, False, rank, None, (), (), ())

    metrics = mode_metrics(
        [result(1), result(2), result(4), result(11), result(None), result(None, positive=False)]
    )
    assert metrics.positive_cases == 5
    assert dict(metrics.hits_at) == {1: 1, 3: 2, 5: 3}
    assert metrics.recall(3) == Fraction(2, 5)
    assert metrics.mrr == (Fraction(1) + Fraction(1, 2) + Fraction(1, 4)) / 5  # rank 11 counts 0


def test_hit_ranks_must_be_contiguous() -> None:
    with pytest.raises(ValueError, match="1..n"):
        score_case(case(), resolved(((target(V2, 0, 10),),)), [hit(2, V2, 0, 10)])


# --- selection ----------------------------------------------------------------------------


def results_with(critical_rank: int | None, failures: int = 0) -> list[CaseResult]:
    return [
        CaseResult("crit", "mixed", True, True, critical_rank, None, (), (), ("x",) * failures),
        CaseResult("other", "keyword", True, False, 1, None, (), (), ()),
    ]


def metrics_with(recall5_hits: int, rr: Fraction) -> ModeMetrics:
    return ModeMetrics(
        positive_cases=4, hits_at={1: 1, 3: 1, 5: recall5_hits}, reciprocal_rank_sum=rr
    )


PASS = results_with(1)


def choose(
    vector: ModeMetrics,
    lexical: ModeMetrics,
    hybrid: ModeMetrics,
    *,
    vector_results: list[CaseResult] = PASS,
    lexical_results: list[CaseResult] = PASS,
    hybrid_results: list[CaseResult] = PASS,
) -> str | None:
    return select_default(
        {"vector": vector, "lexical": lexical, "hybrid": hybrid},
        {"vector": vector_results, "lexical": lexical_results, "hybrid": hybrid_results},
    ).selected


def test_lexical_replaces_vector_on_an_exact_tie_or_better_on_both() -> None:
    same = metrics_with(3, Fraction(2))
    assert choose(same, same, same) == "lexical"  # tie: lexical; hybrid not strictly better
    assert choose(same, metrics_with(4, Fraction(2)), same) == "lexical"


def test_lexical_must_be_at_least_vector_on_both_metrics() -> None:
    vector = metrics_with(3, Fraction(2))
    worse_mrr = metrics_with(4, Fraction(19, 10))
    assert choose(vector, worse_mrr, vector) == "vector"
    assert choose(
        vector, metrics_with(3, Fraction(3)), vector, lexical_results=results_with(4)
    ) == (
        "vector"  # lexical fails the critical rule
    )


def test_hybrid_needs_both_at_least_and_one_strictly_better() -> None:
    single = metrics_with(3, Fraction(2))
    assert choose(single, metrics_with(2, Fraction(1)), metrics_with(3, Fraction(5, 2))) == "hybrid"
    assert choose(single, metrics_with(2, Fraction(1)), metrics_with(4, Fraction(2))) == "hybrid"
    assert choose(single, metrics_with(2, Fraction(1)), single) == "vector"  # equal: not enough
    better_recall_worse_mrr = metrics_with(4, Fraction(19, 10))
    assert choose(single, metrics_with(2, Fraction(1)), better_recall_worse_mrr) == "vector"


def test_hybrid_is_compared_with_the_preferred_single_retriever() -> None:
    vector = metrics_with(2, Fraction(1))
    lexical = metrics_with(4, Fraction(3))  # replaces vector
    hybrid = metrics_with(3, Fraction(2))  # beats vector, not lexical
    assert choose(vector, lexical, hybrid) == "lexical"


def test_a_failing_mode_never_replaces_and_a_failing_default_fails_the_run() -> None:
    m = metrics_with(3, Fraction(2))
    better = metrics_with(4, Fraction(3))
    assert choose(m, m, better, hybrid_results=results_with(1, failures=1)) == "lexical"
    assert choose(m, better, better, vector_results=results_with(None),
                  lexical_results=results_with(5), hybrid_results=results_with(4)) is None  # fmt: skip
