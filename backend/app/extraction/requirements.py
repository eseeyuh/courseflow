# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Requirement extraction: what the model may say, and the gate that decides.

The model proposes, for an assessment it names by local ID (A1, ...), a
statement quote and optionally a lead-in. The gate (``claim-gate/v3``)
decides each candidate on its own:

1. the assessment ID must have been issued for the unit, else rejected
   (``assessment_unknown``);
2. the statement quote is located by ``claim_gate.locate_quote`` (whitespace
   runs match any whitespace; exact original offsets; outside any injection
   fragment), else rejected; a model lead-in that fails is dropped alone;
3. one source requirement unit is one requirement (``source_units``): a
   list item (its marker line and wrapped lines), a paragraph under a
   DOCX/HTML heading, or else one sentence. A quote covering a lead-in and
   several items is split by the source's own list structure into one
   candidate per item, in source order, each with exact offsets and the same
   lead-in; text outside the items is not split further. The text is the
   source unit itself and the identity its key, never the model's quote;
4. requirement wording (``statements.MANDATORY`` or ``RECOMMENDED``) must be
   in that unit's part of the quote, or in the lead-in the source
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

The requirement's text is its source unit, verbatim apart from whitespace
and without its list marker; its identity is that unit's key, so the same
statement repeated anywhere merges.
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
    LIST_MARKER,
    MANDATORY,
    RECOMMENDED,
    governing_lead_in,
    list_items_in,
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


def heading_span(unit: ExtractionUnit, span: UnitSpan) -> UnitSpan | None:
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
    heading = heading_span(unit, span)
    if heading is None:
        return None
    stripped = heading.excerpt.strip()
    start = heading.excerpt.index(stripped)
    location = GatedEvidence(heading.span_id, start, start + len(stripped))
    return Located((location,)), stripped


@dataclass(frozen=True, slots=True)
class _Unit:
    """One source requirement unit a located quote is evidence for."""

    span: UnitSpan
    start: int  # the unit's own bounds in the span (its text)
    end: int
    evidence: GatedEvidence  # the part of the quote inside the unit


def source_units(unit: ExtractionUnit, location: GatedEvidence) -> list[_Unit]:
    """The source requirement units one located quote covers, in source order:
    each list item it touches (a quote merged across a lead-in and several
    items is split by the source's own list structure; wrapped lines stay in
    their item); else, in a DOCX/HTML section under a heading, its
    paragraph; else the one sentence it starts in. Never a semantic split."""
    span = unit.resolve_by_id(location.span_id)
    text, qs, qe = span.excerpt, location.quote_start, location.quote_end
    items = list_items_in(text, qs, qe)
    if items:
        return [
            _Unit(span, a, b, GatedEvidence(span.span_id, max(qs, a), min(qe, b)))
            for a, b in items
            if max(qs, a) < min(qe, b)
        ]
    if heading_span(unit, span) is not None:
        stripped = text.strip()
        a = text.index(stripped)
        return [_Unit(span, a, a + len(stripped), location)]
    a, b = sentence_bounds(text, qs)
    return [_Unit(span, a, b, GatedEvidence(span.span_id, qs, min(qe, b)))]


def _linked(assessment: AssessmentRef, quotes: list[str]) -> bool:
    if assessment.evidenced_here:
        return True
    key = set(assessment.canonical_key.split())
    return bool(key) and any(key <= set(words(quote)) for quote in quotes)


def _unit_text(source: _Unit) -> str:
    """The unit verbatim, whitespace collapsed, without its list marker."""
    text = " ".join(source.span.excerpt[source.start : source.end].split())
    return LIST_MARKER.sub("", text, count=1)


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
        # Every occurrence's units, grouped by the unit's identity: one candidate
        # per source unit, in source order of the first occurrence.
        grouped: dict[str, list[_Unit]] = {}
        for location in located.locations:
            for source in source_units(unit, location):
                grouped.setdefault(statement_key(_unit_text(source)), []).append(source)
        for key, sources in grouped.items():
            gated = _gate_unit(unit, assessment, key, sources, located, model_lead_in, result)
            if gated is not None:
                result.requirements.append(gated)
    return result


def _gate_unit(
    unit: ExtractionUnit,
    assessment: AssessmentRef,
    key: str,
    sources: list[_Unit],
    located: Located,
    model_lead_in: tuple[Located, str] | None,
    result: RequirementGateResult,
) -> GatedRequirement | None:
    first = sources[0]
    statement = Located(tuple(s.evidence for s in sources))
    supporting = first.span.excerpt[first.evidence.quote_start : first.evidence.quote_end]
    reasons: list[ReviewReason] = []
    if located.status is not V.VERIFIED:
        reasons.append(R.NON_UNIQUE_QUOTE_LOCATION)
    lead_in: Located | None = None
    quotes = [supporting]
    derived = _derived_lead_in(unit, first.span, first.start)
    if _has_wording(supporting):
        pass  # the unit supports itself; a lead-in adds nothing
    elif derived is not None and _has_wording(derived[1]):
        lead_in = derived[0]
        quotes.append(derived[1])
    elif model_lead_in is not None and _has_wording(model_lead_in[1]):
        # Wording the source structure does not tie to this unit.
        lead_in = model_lead_in[0]
        quotes.append(model_lead_in[1])
        reasons.append(R.NO_REQUIREMENT_LANGUAGE)
    else:
        result.rejected[f"requirement:{RejectionReason.NO_REQUIREMENT_LANGUAGE}"] += 1
        return None
    if not _linked(assessment, quotes):
        reasons.append(R.ASSESSMENT_LINK_UNSUPPORTED)
    text = _unit_text(first)
    if len(text) > MAX_TEXT:
        result.rejected[f"requirement:{RejectionReason.STATEMENT_TOO_LONG}"] += 1
        return None
    reason = reasons[0] if reasons else None
    if reason is not None:
        result.review_reasons[f"requirement:{reason}"] += 1
    return GatedRequirement(assessment, key, text, statement, lead_in, reason)
