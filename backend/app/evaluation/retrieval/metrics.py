# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Score one retrieval mode on a frozen dataset. Pure: no database, no I/O.

A hit is reduced to (version key, [start, end)) plus whether that version is
current. Relevance is ``full-target-containment/v1`` (``rules``): a hit
satisfies a target group when it wholly contains any one of its
alternatives. A positive case's ``first_relevant_rank`` is the rank at which
every target group has been satisfied (v1 cases have exactly one group), or
None if that does not happen within the returned hits.

Metrics, over positive cases only (invariant cases are never in a
denominator), with exact rational arithmetic:

- Recall@k = |{cases with first_relevant_rank <= k}| / |positive cases|;
- MRR@10 = mean over positive cases of 1 / first_relevant_rank when it is
  <= 10, else 0.

Hard invariants (``invariant_rule`` in the manifest) are failures, not
metrics: a hit from another course or from a non-current version in any
case, or from a forbidden version in an invariant case.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction

from app.evaluation.retrieval.resolve import ResolvedCase, ResolvedText, VersionKey
from app.evaluation.retrieval.rules import contains
from app.evaluation.retrieval.schema import RetrievalCase

RECALL_KS = (1, 3, 5)
MRR_K = 10
SEARCH_K = 10  # hits requested per case: enough for MRR@10, Recall@5 is a prefix


@dataclass(frozen=True, slots=True)
class Hit:
    rank: int
    version: VersionKey | None  # None: a version the dataset did not ingest
    current: bool
    start: int
    end: int
    ordinal: int = 0  # the chunk's ordinal in its version
    # The mode's ordering score (distance, ts_rank_cd or exact RRF sum): used
    # only to find ties, which the rankers break by natural identity.
    score: object = None


@dataclass(frozen=True, slots=True)
class DistractorPosition:
    kind: str
    ref: str
    first_rank: int | None  # first hit containing the quote (or from the version)


@dataclass(frozen=True, slots=True)
class CaseResult:
    case_id: str
    category: str
    positive: bool
    critical: bool
    first_relevant_rank: int | None
    # Ranks of the hits sharing the first relevant hit's exact score: when
    # wider than one rank, the rank was decided by the natural-identity tie-break.
    first_relevant_tie: tuple[int, int] | None
    # Per rank: "course/resource/vN#ordinal" ("unknown" if not ingested).
    hits: tuple[str, ...]
    distractors: tuple[DistractorPosition, ...]
    invariant_failures: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ModeMetrics:
    positive_cases: int
    hits_at: Mapping[int, int]  # k -> cases with first_relevant_rank <= k
    reciprocal_rank_sum: Fraction  # sum of 1 / rank over ranks <= MRR_K

    def recall(self, k: int) -> Fraction:
        return Fraction(self.hits_at[k], self.positive_cases)

    @property
    def mrr(self) -> Fraction:
        return self.reciprocal_rank_sum / self.positive_cases


def _label(key: VersionKey | None) -> str:
    return "unknown" if key is None else f"{key[0]}/{key[1]}/v{key[2]}"


def _satisfies(hit: Hit, text: ResolvedText) -> bool:
    ref = text.ref
    return hit.version == (ref.course, ref.resource, ref.version) and contains(
        hit.start, hit.end, text.start, text.end
    )


def first_relevant_rank(
    hits: Sequence[Hit], groups: Sequence[Sequence[ResolvedText]]
) -> int | None:
    ranks = []
    for group in groups:
        rank = next((h.rank for h in hits if any(_satisfies(h, alt) for alt in group)), None)
        if rank is None:
            return None
        ranks.append(rank)
    return max(ranks) if ranks else None


def _distractor_rank(hits: Sequence[Hit], key: VersionKey, text: ResolvedText | None) -> int | None:
    if text is not None:
        return next((h.rank for h in hits if _satisfies(h, text)), None)
    return next((h.rank for h in hits if h.version == key), None)


def score_case(case: RetrievalCase, resolved: ResolvedCase, hits: Sequence[Hit]) -> CaseResult:
    ranks = [h.rank for h in hits]
    if ranks != list(range(1, len(hits) + 1)):
        raise ValueError(f"{case.case_id}: hit ranks must be 1..n in order")
    failures = []
    forbidden = set(resolved.forbidden_versions)
    for hit in hits:
        where = f"rank {hit.rank} ({_label(hit.version)})"
        if hit.version is None:
            failures.append(f"{where}: a version the dataset did not ingest")
            continue
        if hit.version[0] != case.course:
            failures.append(f"{where}: another course")
        if not hit.current:
            failures.append(f"{where}: a non-current version")
        if hit.version in forbidden:
            failures.append(f"{where}: a forbidden version")
    distractors = tuple(
        DistractorPosition(
            kind=d.kind.value,
            ref=_label((d.course, d.resource, d.version)),
            first_rank=_distractor_rank(hits, (d.course, d.resource, d.version), text),
        )
        for d, text in zip(case.distractors, resolved.distractors, strict=True)
    )
    rank = first_relevant_rank(hits, resolved.target_groups) if case.is_positive else None
    return CaseResult(
        case_id=case.case_id,
        category=case.category.value,
        positive=case.is_positive,
        critical=case.critical,
        first_relevant_rank=rank,
        first_relevant_tie=_tie(hits, rank),
        hits=tuple(
            "unknown" if h.version is None else f"{_label(h.version)}#{h.ordinal}" for h in hits
        ),
        distractors=distractors,
        invariant_failures=tuple(failures),
    )


def _tie(hits: Sequence[Hit], rank: int | None) -> tuple[int, int] | None:
    if rank is None or hits[rank - 1].score is None:
        return None
    tied = [h.rank for h in hits if h.score == hits[rank - 1].score]
    return (min(tied), max(tied))


def mode_metrics(results: Sequence[CaseResult]) -> ModeMetrics:
    positive = [r for r in results if r.positive]
    if not positive:
        raise ValueError("no positive cases to score")
    ranks = [r.first_relevant_rank for r in positive]
    return ModeMetrics(
        positive_cases=len(positive),
        hits_at={k: sum(1 for r in ranks if r is not None and r <= k) for k in RECALL_KS},
        reciprocal_rank_sum=sum(
            (Fraction(1, r) for r in ranks if r is not None and r <= MRR_K), Fraction(0)
        ),
    )
