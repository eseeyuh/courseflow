# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Obligation extraction: what the model may say, and the gate that decides.

The model proposes an action in its own words, one of the narrow
categories, optionally the assessment it concerns (local ID) and an
explicit due date, and the quotes that state it. It never states the
modality: the gate (``obligation-gate/v1``) derives it from the cited
quotes alone, and decides each candidate on its own:

1. each quote must be located exactly; a failing quote, injected text
   included, is dropped on its own and the candidate is judged on the
   quotes left, so injected text can neither support a claim nor sink a
   valid one; no quote left rejects it (``no_evidence``);
2. modality, from closed word lists (``statements.modal_wording``):
   must / required / must not / "no ... may" -> mandatory;
   should / recommended -> recommended; may / can only -> needs_review
   (``permissive_wording``, no modality); more than one kind ->
   needs_review (``mixed_modality``, no modality); none -> rejected
   (``no_obligation_wording``): an informational statement or a bare
   imperative is not a verified obligation in this version;
3. a specific category needs its own word in a cited quote (supervisor,
   approve, ethics/form, a precondition word, submit), else needs_review
   (``category_unsupported``); ``other`` needs none;
4. an assessment link is supported when the assessment has title evidence
   in this resource or its designator words are in a cited quote, else
   needs_review (``assessment_link_unsupported``) and the link is not kept;
   an ID not issued for the unit is treated the same;
5. a due date must be an explicit calendar date checked exactly as an
   assessment deadline is (``gate._deadline``); a relative lead time
   ("5 working days before the deadline") has no date and is never one.
   A rejected due date drops only the due date;
6. non-unique quote locations keep every occurrence, needs_review.

Identity is the anchor sentence of the earliest quote, in source order,
that has modal wording (``canonical_location``, ``statements.statement_key``):
never the model's wording or the order of its quotes.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Annotated, Literal, Self

from pydantic import Field, StringConstraints, model_validator

from app.domain.enums import (
    ObligationCategory,
    ObligationModality,
    RejectionReason,
    ReviewReason,
    VerificationStatus,
)
from app.extraction.claim_units import AssessmentRef, ClaimUnit
from app.extraction.gate import (
    GatedDeadline,
    GatedEvidence,
    Located,
    Rejected,
    check_deadline,
    locate,
)
from app.extraction.requirements import LocalAssessmentId
from app.extraction.schema import DeadlineOut, EvidenceRef, StrictModel
from app.extraction.statements import anchor_text, modal_wording, statement_key, words
from app.extraction.units import ExtractionUnit

GATE_VERSION = "obligation-gate/v1"

V = VerificationStatus
R = ReviewReason
C = ObligationCategory

CategoryOut = Literal[
    "supervisor_review",
    "approval",
    "ethics_admin_form",
    "formative_prerequisite",
    "submission_prerequisite",
    "other",
]


class DueOut(StrictModel):
    # stated: a full calendar date to do the action by. relative: only a lead
    # time against another date ("5 working days before the deadline").
    status: Literal["stated", "relative", "not_stated", "ambiguous"]
    due_date: Annotated[str, StringConstraints(pattern=r"^\d{4}-\d{2}-\d{2}$")] | None
    due_time: Annotated[str, StringConstraints(pattern=r"^\d{2}:\d{2}$")] | None
    evidence: EvidenceRef | None

    @model_validator(mode="after")
    def _stated_has_value_and_evidence(self) -> Self:
        if self.status == "stated" and (self.due_date is None or self.evidence is None):
            raise ValueError("a stated due date needs due_date and evidence")
        if self.status != "stated" and (self.due_date is not None or self.due_time is not None):
            raise ValueError("an abstention has no due_date or due_time")
        return self


class ObligationOut(StrictModel):
    action: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    category: CategoryOut
    assessment: LocalAssessmentId | None
    evidence: list[EvidenceRef] = Field(min_length=1, max_length=4)
    due: DueOut


class ObligationExtraction(StrictModel):
    obligations: list[ObligationOut] = Field(max_length=20)


# A specific category needs one of its words in a cited quote.
_CATEGORY_WORDS: dict[ObligationCategory, re.Pattern[str]] = {
    C.SUPERVISOR_REVIEW: re.compile(
        r"\b(?:supervisors?|tutors?|advisers?|advisors?)\b", re.IGNORECASE
    ),
    C.APPROVAL: re.compile(r"\b(?:approv\w*)\b", re.IGNORECASE),
    C.ETHICS_ADMIN_FORM: re.compile(
        r"\b(?:ethic\w*|forms?|consent|registration|register\w*)\b", re.IGNORECASE
    ),
    C.FORMATIVE_PREREQUISITE: re.compile(
        r"\b(?:before|prior\s+to|until|first|formative|draft)\b", re.IGNORECASE
    ),
    C.SUBMISSION_PREREQUISITE: re.compile(r"\b(?:submit\w*|submission)\b", re.IGNORECASE),
}


# The order in which an obligation's reasons are reported (the first is
# stored). A category the evidence does not support always comes first, so
# a stored row tells whether its category is supported.
REASON_ORDER: tuple[ReviewReason, ...] = (
    R.CATEGORY_UNSUPPORTED,
    R.PERMISSIVE_WORDING,
    R.MIXED_MODALITY,
    R.ASSESSMENT_LINK_UNSUPPORTED,
    R.MISSING_TIMEZONE,
    R.UNRECOGNISED_TIMEZONE,
    R.CONFLICTING_TIMEZONE,
    R.WEEKDAY_MISMATCH,
    R.AMBIGUOUS_TIME,
    R.AMBIGUOUS_LOCAL_TIME,
    R.NON_UNIQUE_QUOTE_LOCATION,
)


@dataclass(frozen=True, slots=True)
class GatedObligation:
    """A candidate that passed the gate. Values the evidence does not support
    are absent: ``modality`` when the wording does not determine it,
    ``assessment`` when the link is unsupported; ``reasons`` says why. The
    category is always the proposed one; ``category_supported`` says whether
    the evidence has its word. ``due`` may need review (its own status)."""

    action: str
    category: ObligationCategory
    modality: ObligationModality | None
    assessment: AssessmentRef | None
    canonical_key: str
    evidence: tuple[Located, ...]
    due: GatedDeadline | None
    reasons: tuple[ReviewReason, ...]  # in REASON_ORDER

    @property
    def review_reason(self) -> ReviewReason | None:
        return self.reasons[0] if self.reasons else None

    @property
    def status(self) -> VerificationStatus:
        return V.VERIFIED if not self.reasons else V.NEEDS_REVIEW

    @property
    def category_supported(self) -> bool:
        return R.CATEGORY_UNSUPPORTED not in self.reasons


@dataclass(slots=True)
class ObligationGateResult:
    obligations: list[GatedObligation] = field(default_factory=list)
    rejected: Counter[str] = field(default_factory=Counter)  # "<fact>:<reason>"
    abstained: Counter[str] = field(default_factory=Counter)  # "due:<status>"
    review_reasons: Counter[str] = field(default_factory=Counter)


def _modality(quotes: list[str]) -> tuple[ObligationModality | None, ReviewReason | None]:
    modal = modal_wording("\n".join(quotes))
    if modal.kinds == 0:
        raise Rejected(RejectionReason.NO_OBLIGATION_WORDING)
    if modal.kinds > 1:
        return None, R.MIXED_MODALITY
    if modal.permissive:
        return None, R.PERMISSIVE_WORDING
    if modal.mandatory:
        return ObligationModality.MANDATORY, None
    return ObligationModality.RECOMMENDED, None


def _located(
    claim_unit: ClaimUnit, proposed: ObligationOut, result: ObligationGateResult
) -> tuple[list[Located], list[str]]:
    """The quotes that pass ``locate``. A failing quote, injected text
    included, is dropped on its own: it can neither support the candidate nor
    take valid sibling evidence down with it."""
    evidence, quotes = [], []
    for ref in proposed.evidence:
        try:
            located, quote = locate(claim_unit.unit, ref)
        except Rejected as rejected:
            result.rejected[f"evidence:{rejected.reason}"] += 1
            continue
        evidence.append(located)
        quotes.append(quote)
    if not evidence:
        raise Rejected(RejectionReason.NO_EVIDENCE)
    return evidence, quotes


def _link(
    claim_unit: ClaimUnit, proposed: ObligationOut, quotes: list[str]
) -> tuple[AssessmentRef | None, ReviewReason | None]:
    """The supported assessment link, or None and why there is none."""
    if proposed.assessment is None:
        return None, None
    assessment = claim_unit.resolve_assessment(proposed.assessment)
    if assessment is None:
        return None, R.ASSESSMENT_LINK_UNSUPPORTED
    key = set(assessment.canonical_key.split())
    named = bool(key) and any(key <= set(words(quote)) for quote in quotes)
    if assessment.evidenced_here or named:
        return assessment, None
    return None, R.ASSESSMENT_LINK_UNSUPPORTED


def _due(
    claim_unit: ClaimUnit,
    proposed: ObligationOut,
    zone: str | None,
    key: str,
    result: ObligationGateResult,
) -> GatedDeadline | None:
    due = proposed.due
    if due.status != "stated":
        result.abstained[f"due:{due.status}"] += 1
        return None
    stated = DeadlineOut(
        status="stated", due_date=due.due_date, due_time=due.due_time, evidence=due.evidence
    )
    try:
        gated, _ = check_deadline(claim_unit.unit, stated, zone, key)
    except Rejected as rejected:
        result.rejected[f"due:{rejected.reason}"] += 1
        return None
    return gated


def canonical_location(
    unit: ExtractionUnit, evidence: list[Located], quotes: list[str]
) -> GatedEvidence:
    """The identity anchor: among the quotes with modal wording, the occurrence
    earliest in the source (span order, then offsets). Independent of the
    order in which the model listed its quotes."""
    order = {span.span_id: i for i, span in enumerate(unit.spans)}
    qualifying = [
        location
        for located, quote in zip(evidence, quotes, strict=True)
        if modal_wording(quote).kinds
        for location in located.locations
    ]
    return min(qualifying, key=lambda e: (order[e.span_id], e.quote_start, e.quote_end))


def gate_obligations(
    claim_unit: ClaimUnit, extraction: ObligationExtraction, institution_timezone: str | None
) -> ObligationGateResult:
    unit = claim_unit.unit
    result = ObligationGateResult()
    for proposed in extraction.obligations:
        try:
            evidence, quotes = _located(claim_unit, proposed, result)
            modality, modal_reason = _modality(quotes)
        except Rejected as rejected:
            result.rejected[f"obligation:{rejected.reason}"] += 1
            continue
        category = ObligationCategory(proposed.category)
        reasons = {modal_reason} if modal_reason else set()
        guard = _CATEGORY_WORDS.get(category)
        if guard is not None and not any(guard.search(quote) for quote in quotes):
            reasons.add(R.CATEGORY_UNSUPPORTED)
        assessment, link_reason = _link(claim_unit, proposed, quotes)
        if link_reason is not None:
            reasons.add(link_reason)
        due = _due(
            claim_unit,
            proposed,
            institution_timezone,
            assessment.canonical_key if assessment else "",
            result,
        )
        if due is not None and due.review_reason is not None:
            reasons.add(due.review_reason)
        if any(e.status is not V.VERIFIED for e in evidence):
            reasons.add(R.NON_UNIQUE_QUOTE_LOCATION)
        anchor = canonical_location(unit, evidence, quotes)
        span = unit.resolve_by_id(anchor.span_id)
        key = statement_key(anchor_text(span.excerpt, anchor.quote_start, anchor.quote_end))
        ordered = tuple(r for r in REASON_ORDER if r in reasons)
        if ordered:
            result.review_reasons[f"obligation:{ordered[0]}"] += 1
        result.obligations.append(
            GatedObligation(
                " ".join(proposed.action.split()),
                category,
                modality,
                assessment,
                key,
                tuple(evidence),
                due,
                ordered,
            )
        )
    return result
