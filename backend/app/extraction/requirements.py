# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Requirement extraction: what the model may say, and the gate that decides.

The model proposes, for an assessment it names by local ID (A1, ...), a
statement quote and optionally the lead-in that governs it (a heading or
line such as "Your specification must include"). The gate
(``requirement-gate/v1``) decides each candidate on its own:

1. the assessment ID must have been issued for the unit, else rejected
   (``assessment_unknown``);
2. the statement quote must be located exactly, outside any injection
   fragment, else rejected (gate.locate's reasons); a lead-in overlapping
   an injection fragment rejects the candidate too;
3. requirement wording (``statements.MANDATORY`` or ``RECOMMENDED``) must be
   in the statement quote itself, or in a lead-in quote that structurally
   governs it: in the same span, a whole line above the statement's list
   item with only list items and their wrapped lines between; in another
   span, the heading span whose text ends the statement span's section
   path. A lead-in with the wording that is not shown to govern the
   statement leaves it ``needs_review`` (``no_requirement_language``); no
   wording anywhere rejects it (``no_requirement_language``): generic text
   is not a requirement;
4. the link to the assessment is supported when the assessment has title
   evidence in this resource (it is the assessment's brief), or its
   designator words are in a cited quote; otherwise ``needs_review``
   (``assessment_link_unsupported``);
5. a quote that occurs more than once in its span keeps every occurrence,
   ``needs_review`` (``non_unique_quote_location``).

The requirement is the anchor sentence of the statement quote, verbatim
(``statements.anchor_text``): one sentence, one requirement. Its identity
is that sentence's key, so the same statement repeated anywhere merges.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Annotated

from pydantic import Field, StringConstraints

from app.domain.enums import RejectionReason, ReviewReason, VerificationStatus
from app.extraction.claim_units import AssessmentRef, ClaimUnit
from app.extraction.gate import Located, Rejected, locate
from app.extraction.schema import EvidenceRef, StrictModel
from app.extraction.statements import (
    MANDATORY,
    RECOMMENDED,
    anchor_text,
    governs_list_item,
    statement_key,
    words,
)
from app.extraction.units import ExtractionUnit

GATE_VERSION = "requirement-gate/v1"
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


def _governs(unit: ExtractionUnit, lead_in: Located, statement: Located) -> bool:
    """Whether the (unique) lead-in structurally introduces the statement."""
    if lead_in.status is not V.VERIFIED or statement.status is not V.VERIFIED:
        return False
    head, item = lead_in.locations[0], statement.locations[0]
    head_span, item_span = unit.resolve_by_id(head.span_id), unit.resolve_by_id(item.span_id)
    if head_span is item_span:
        return governs_list_item(
            item_span.excerpt,
            (head.quote_start, head.quote_end),
            (item.quote_start, item.quote_end),
        )
    # A heading span: its whole text is the last part of the item's section path.
    heading = head_span.excerpt.strip()
    return (
        head_span.excerpt[head.quote_start : head.quote_end].strip() == heading
        and head_span.locator == item_span.locator
        and item_span.locator.rsplit(" > ", 1)[-1] == heading
    )


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
            statement, statement_quote = locate(unit, proposed.statement)
        except Rejected as rejected:
            result.rejected[f"requirement:{rejected.reason}"] += 1
            continue
        lead_in: Located | None = None
        lead_in_quote = ""
        if proposed.lead_in is not None:
            try:
                lead_in, lead_in_quote = locate(unit, proposed.lead_in)
            except Rejected as rejected:
                if rejected.reason is RejectionReason.QUOTE_OVERLAPS_INJECTION:
                    # Citing injected text taints the whole candidate.
                    result.rejected[f"requirement:{rejected.reason}"] += 1
                    continue
                result.rejected[f"lead_in:{rejected.reason}"] += 1
        reasons: list[ReviewReason] = []
        if statement.status is not V.VERIFIED:
            reasons.append(R.NON_UNIQUE_QUOTE_LOCATION)
        quotes = [statement_quote]
        if _has_wording(statement_quote):
            lead_in = None  # the statement supports itself; a lead-in adds nothing
        elif lead_in is not None and _has_wording(lead_in_quote):
            quotes.append(lead_in_quote)
            if not _governs(unit, lead_in, statement):
                reasons.append(R.NO_REQUIREMENT_LANGUAGE)
        else:
            result.rejected[f"requirement:{RejectionReason.NO_REQUIREMENT_LANGUAGE}"] += 1
            continue
        if not _linked(assessment, quotes):
            reasons.append(R.ASSESSMENT_LINK_UNSUPPORTED)
        first = statement.locations[0]
        span = unit.resolve_by_id(first.span_id)
        text = anchor_text(span.excerpt, first.quote_start, first.quote_end)
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
