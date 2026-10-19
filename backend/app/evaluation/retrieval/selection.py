# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""The default retrieval mode, chosen by ``default-selection/v1``, a rule fixed
before any score existed.

A mode *passes* when it has no hard-invariant failure and the critical case
is satisfied within the top 3 (the manifest's critical rule).

A. Preferred single retriever: start with vector. Lexical replaces vector
   iff lexical passes, Recall@5(lexical) >= Recall@5(vector) and
   MRR@10(lexical) >= MRR@10(vector). An exact tie on both prefers lexical.
B. Hybrid replaces the preferred single retriever iff hybrid passes,
   Recall@5(hybrid) >= Recall@5(preferred), MRR@10(hybrid) >=
   MRR@10(preferred), and it is strictly better on at least one of the two.

The result is the default. If the default itself does not pass (vector was
kept but fails), there is no eligible default and the run fails. All
comparisons are exact (rationals).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction

from app.evaluation.retrieval.metrics import CaseResult, ModeMetrics

SELECTION_RULE = "default-selection/v1"
MODES = ("vector", "lexical", "hybrid")
CRITICAL_MAX_RANK = 3


@dataclass(frozen=True, slots=True)
class ModeAssessment:
    mode: str
    invariant_failures: int
    critical_rank: int | None
    recall_at_5: Fraction
    mrr_at_10: Fraction

    @property
    def critical_passes(self) -> bool:
        return self.critical_rank is not None and self.critical_rank <= CRITICAL_MAX_RANK

    @property
    def passes(self) -> bool:
        return self.invariant_failures == 0 and self.critical_passes

    def at_least(self, other: ModeAssessment) -> bool:
        return self.recall_at_5 >= other.recall_at_5 and self.mrr_at_10 >= other.mrr_at_10

    def strictly_better_somewhere(self, other: ModeAssessment) -> bool:
        return self.recall_at_5 > other.recall_at_5 or self.mrr_at_10 > other.mrr_at_10


@dataclass(frozen=True, slots=True)
class Selection:
    rule: str
    assessments: tuple[ModeAssessment, ...]
    preferred_single: str
    lexical_replaces_vector: bool
    hybrid_replaces_single: bool
    selected: str | None  # None: the resulting default does not pass
    reason: str


def assess(mode: str, metrics: ModeMetrics, results: Sequence[CaseResult]) -> ModeAssessment:
    critical = [r for r in results if r.critical]
    if len(critical) != 1:
        raise ValueError("exactly one critical case is required")
    return ModeAssessment(
        mode=mode,
        invariant_failures=sum(len(r.invariant_failures) for r in results),
        critical_rank=critical[0].first_relevant_rank,
        recall_at_5=metrics.recall(5),
        mrr_at_10=metrics.mrr,
    )


def select_default(
    metrics: Mapping[str, ModeMetrics], results: Mapping[str, Sequence[CaseResult]]
) -> Selection:
    if set(metrics) != set(MODES) or set(results) != set(MODES):
        raise ValueError(f"every mode of {MODES} must be scored")
    by_mode = {m: assess(m, metrics[m], results[m]) for m in MODES}
    vector, lexical, hybrid = by_mode["vector"], by_mode["lexical"], by_mode["hybrid"]

    lexical_wins = lexical.passes and lexical.at_least(vector)
    single = lexical if lexical_wins else vector
    hybrid_wins = (
        hybrid.passes and hybrid.at_least(single) and hybrid.strictly_better_somewhere(single)
    )
    default = hybrid if hybrid_wins else single

    steps = [
        f"A: lexical {'replaces' if lexical_wins else 'does not replace'} vector",
        f"B: hybrid {'replaces' if hybrid_wins else 'does not replace'} {single.mode}",
    ]
    if not default.passes:
        steps.append(f"{default.mode} does not pass the invariants and critical rule")
    return Selection(
        rule=SELECTION_RULE,
        assessments=tuple(by_mode[m] for m in MODES),
        preferred_single=single.mode,
        lexical_replaces_vector=lexical_wins,
        hybrid_replaces_single=hybrid_wins,
        selected=default.mode if default.passes else None,
        reason="; ".join(steps),
    )
