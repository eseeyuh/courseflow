# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Requirement extraction: what the model may say, and the gate that decides.

The model proposes, for an assessment it names by local ID (A1, ...), a
statement quote and optionally a lead-in. The gate (``claim-gate/v2``)
decides each candidate on its own:

1. the assessment ID must have been issued for the unit, else rejected
   (``assessment_unknown``);
2. the statement quote is located by ``claim_gate.locate_quote`` (whitespace
   runs match any whitespace; exact original offsets; outside any injection
   fragment), else rejected; a model lead-in that fails is dropped alone;
3. one source sentence is one requirement: a quote merged with the lead-in
   line above its list item starts at the item, and every location is cut
   to that one sentence, so merged quotation text never makes a merged
   requirement or supports a neighbouring one;
4. requirement wording (``statements.MANDATORY`` or ``RECOMMENDED``) must be
   in that sentence's part of the quote, or in the lead-in the source
   structure ties to the item, derived deterministically, never taken from
   the model: in the same span, the line introducing the item's list
   (``statements.governing_lead_in``); in DOCX/HTML, the heading span that
   ends the item's section path. A model lead-in with the wording that the
   structure does not tie to the item leaves it ``needs_review``
   (``no_requirement_language``); no wording anywhere rejects it: generic
   text is not a requirement;
5. the link to the assessment is supported when the assessment has title
   evidence in this resource (it is the assessment's brief), or its
   designator words are in a cited quote; otherwise ``needs_review``
   (``assessment_link_unsupported``);
6. a quote that matches more than once keeps every location,
   ``needs_review`` (``non_unique_quote_location``).

The requirement's text is its sentence, verbatim apart from whitespace;
its identity is that sentence's key, so the same statement repeated
anywhere merges.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Annotated

from pydantic import Field, StringConstraints

from app.domain.enums import RejectionReason, ReviewReason, VerificationStatus
from app.extraction.claim_gate import GATE_VERSION as CLAIM_GATE_VERSION
from app.extraction.claim_gate import locate_quote
from app.extraction.claim_units import AssessmentRef, ClaimUnit
from app.extraction.gate import GatedEvidence, Located, Rejected
from app.extraction.schema import EvidenceRef, StrictModel
from app.extraction.statements import (
    MANDATORY,
    RECOMMENDED,
    anchor_text,
    first_list_item_after_lead_in,
    governing_lead_in,
    sentence_bounds,
    statement_key,
    words,
)
from app.extraction.units import ExtractionUnit, UnitSpan

GATE_VERSION = CLAIM_GATE_VERSION  # shared by requirements and obligations
LOCAL_ASSESSMENT_ID_PATTERN = r"^A[1-9][0-9]{0,2}$"
LocalAssessmentId = Annotated[str, StringConstraints(pattern=LOCAL_ASSESSMENT_ID_PATTERN)]

V = VerificationStatus
R = ReviewReason
MAX_TEXT = 2000  # ck_assessment_requirements_text_nonblank_bounded


class RequirementOut(StrictModel):
    assessment: LocalAssessmentId
    statement: EvidenceRef
    # The heading or line introducing a list the statement belongs to, when
    # the statement's own words do not say it is required.
    lead_in: EvidenceRef | None


class RequirementExtraction(StrictModel):
    requirements: list[RequirementOut] = Field(max_length=30)


@dataclass(frozen=True, slots=True)
class GatedRequirement:
    assessment: AssessmentRef
    canonical_key: str
    text: str
    statement: Located
    lead_in: Located | None  # stored as evidence only when it supports the wording
    review_reason: ReviewReason | None

    @property
    def status(self) -> VerificationStatus:
        return V.VERIFIED if self.review_reason is None else V.NEEDS_REVIEW


@dataclass(slots=True)
class RequirementGateResult:
    requirements: list[GatedRequirement] = field(default_factory=list)
    rejected: Counter[str] = field(default_factory=Counter)  # "<fact>:<reason>"
    review_reasons: Counter[str] = field(default_factory=Counter)


def _has_wording(quote: str) -> bool:
    return bool(MANDATORY.search(quote) or RECOMMENDED.search(quote))


def _heading_span(unit: ExtractionUnit, span: UnitSpan) -> UnitSpan | None:
    """The heading span of the section ``span`` is in (DOCX/HTML): before it in
    the unit, with the same section path, whose text is that path's last part."""
    if " > " not in span.locator and span.locator.split(" ", 1)[0] in {"page", "slide"}:
        return None
    heading = span.locator.rsplit(" > ", 1)[-1]
    for candidate in unit.spans:
        if candidate is span:
            return None
        if candidate.locator == span.locator and candidate.excerpt.strip() == heading:
            return candidate
    return None


def _derived_lead_in(
    unit: ExtractionUnit, span: UnitSpan, offset: int
) -> tuple[Located, str] | None:
    """The source's own lead-in for the item at ``offset``, from structure only:
    the governing line in the same span, else the section's heading span."""
    bounds = governing_lead_in(span.excerpt, offset)
    if bounds is not None:
        location = GatedEvidence(span.span_id, *bounds)
        return Located((location,)), span.excerpt[bounds[0] : bounds[1]]
    heading = _heading_span(unit, span)
    if heading is None:
        return None
    stripped = heading.excerpt.strip()
    start = heading.excerpt.index(stripped)
    location = GatedEvidence(heading.span_id, start, start + len(stripped))
    return Located((location,)), stripped


def _intended(text: str, start: int, end: int) -> tuple[int, int, int]:
    """For a located quote: where its intended statement starts (past a lead-in
    line it was merged with), and that one sentence's bounds."""
    begin = first_list_item_after_lead_in(text, start, end)
    begin = start if begin is None else begin
    return (begin, *sentence_bounds(text, begin))


def _clip(unit: ExtractionUnit, located: Located) -> Located:
    """Each location cut to its intended sentence: a merged quote supports only
    the one statement it is evidence for."""
    clipped = []
    for location in located.locations:
        text = unit.resolve_by_id(location.span_id).excerpt
        begin, _, end = _intended(text, location.quote_start, location.quote_end)
        clipped.append(GatedEvidence(location.span_id, begin, min(location.quote_end, end)))
    return Located(tuple(clipped))


def _linked(assessment: AssessmentRef, quotes: list[str]) -> bool:
    if assessment.evidenced_here:
        return True
    key = set(assessment.canonical_key.split())
    return bool(key) and any(key <= set(words(quote)) for quote in quotes)


def gate_requirements(
    claim_unit: ClaimUnit, extraction: RequirementExtraction
) -> RequirementGateResult:
    unit = claim_unit.unit
    result = RequirementGateResult()
    for proposed in extraction.requirements:
        assessment = claim_unit.resolve_assessment(proposed.assessment)
        if assessment is None:
            result.rejected[f"requirement:{RejectionReason.ASSESSMENT_UNKNOWN}"] += 1
            continue
        try:
            located, _ = locate_quote(unit, proposed.statement)
        except Rejected as rejected:
            result.rejected[f"requirement:{rejected.reason}"] += 1
            continue
        model_lead_in: tuple[Located, str] | None = None
        if proposed.lead_in is not None:
            try:
                model_lead_in = locate_quote(unit, proposed.lead_in)
            except Rejected as rejected:
                # Dropped on its own (injected text included): the statement
                # is judged without it.
                result.rejected[f"lead_in:{rejected.reason}"] += 1
        statement = _clip(unit, located)
        first = statement.locations[0]
        span = unit.resolve_by_id(first.span_id)
        supporting = span.excerpt[first.quote_start : first.quote_end]
        reasons: list[ReviewReason] = []
        if statement.status is not V.VERIFIED:
            reasons.append(R.NON_UNIQUE_QUOTE_LOCATION)
        lead_in: Located | None = None
        quotes = [supporting]
        derived = _derived_lead_in(unit, span, first.quote_start)
        if _has_wording(supporting):
            pass  # the statement supports itself; a lead-in adds nothing
        elif derived is not None and _has_wording(derived[1]):
            lead_in = derived[0]
            quotes.append(derived[1])
        elif model_lead_in is not None and _has_wording(model_lead_in[1]):
            # Wording the source structure does not tie to this statement.
            lead_in = model_lead_in[0]
            quotes.append(model_lead_in[1])
            reasons.append(R.NO_REQUIREMENT_LANGUAGE)
        else:
            result.rejected[f"requirement:{RejectionReason.NO_REQUIREMENT_LANGUAGE}"] += 1
            continue
        if not _linked(assessment, quotes):
            reasons.append(R.ASSESSMENT_LINK_UNSUPPORTED)
        text = anchor_text(span.excerpt, first.quote_start, first.quote_start + 1)
        if len(text) > MAX_TEXT:
            result.rejected[f"requirement:{RejectionReason.STATEMENT_TOO_LONG}"] += 1
            continue
        reason = reasons[0] if reasons else None
        if reason is not None:
            result.review_reasons[f"requirement:{reason}"] += 1
        result.requirements.append(
            GatedRequirement(assessment, statement_key(text), text, statement, lead_in, reason)
        )
    return result
