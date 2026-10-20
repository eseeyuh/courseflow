# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""The evidence gate: deterministic decisions over a model's proposals.

For every proposed fact:

1. its local span ID must have been issued for this unit (so the span is
   one of this current version's spans, of this course), and the span must
   not contain instruction-like text (``INSTRUCTION_LIKE``);
2. its quote must occur exactly once in that span's excerpt, giving exact
   code-point offsets (a repeated quote has no unique location);
3. the quote must support the value: the title's designator words, the
   stated percentage, or the deadline parsed from the quote itself (the
   model's date must equal it).

A fact that fails is rejected and not persisted; an assessment whose title
evidence fails is rejected with all its facts. A deadline that passes is
verified or needs_review as ``dates.normalise`` decides. Abstentions
(not_stated, ambiguous) are counted, never turned into a date. Reason codes
are stable; they are counted in the run summary.
"""

from __future__ import annotations

import re
import unicodedata
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, time

from app.extraction.dates import (
    DeadlineRejected,
    NormalisedDeadline,
    normalise,
    parse_deadline_quote,
)
from app.extraction.schema import AssessmentExtraction, AssessmentOut, DeadlineOut, EvidenceRef
from app.extraction.units import ExtractionUnit

_WORD = re.compile(r"[^\W_]+")
# Text addressed to a model rather than a student. A span containing it is
# never evidence, whatever dates it states (even in a quote that avoids the
# instruction itself): such text is inert content.
INSTRUCTION_LIKE = re.compile(
    r"\b(?:ignore|disregard|forget|override)\s+(?:all\s+|any\s+|the\s+)?"
    r"(?:previous|prior|above|earlier|your)\s+(?:instructions|prompts?|rules)\b"
    r"|\byou\s+are\s+now\b"
    r"|\b(?:reveal|print|show|output|leak)\s+(?:the\s+|your\s+)?"
    r"(?:api\s+key|system\s+prompt|password|secrets?|credentials)\b"
    r"|^\s*(?:system|assistant|developer)\s*:",
    re.IGNORECASE | re.MULTILINE,
)
_PERCENT = re.compile(r"(?<![\d.])(\d{1,3})\s*(?:%|per\s?cent\b)", re.IGNORECASE)


class _Rejected(Exception):
    def __init__(self, reason: str) -> None:
        self.reason = reason


@dataclass(frozen=True, slots=True)
class GatedEvidence:
    span_id: uuid.UUID
    quote_start: int  # code points into the span's excerpt
    quote_end: int


@dataclass(frozen=True, slots=True)
class GatedWeighting:
    percent: int
    evidence: GatedEvidence


@dataclass(frozen=True, slots=True)
class GatedDeadline:
    value: NormalisedDeadline
    evidence: GatedEvidence


@dataclass(frozen=True, slots=True)
class GatedAssessment:
    title: str
    canonical_key: str
    title_evidence: GatedEvidence
    weighting: GatedWeighting | None
    deadline: GatedDeadline | None


@dataclass(slots=True)
class GateResult:
    assessments: list[GatedAssessment] = field(default_factory=list)
    rejected: Counter[str] = field(default_factory=Counter)  # "<fact>:<reason>"
    abstained: Counter[str] = field(default_factory=Counter)  # "deadline:<status>"
    review_reasons: Counter[str] = field(default_factory=Counter)


def _words(text: str) -> list[str]:
    return _WORD.findall(unicodedata.normalize("NFKC", text).casefold())


def canonical_key(title: str) -> str:
    """The deterministic identity of an assessment within a course.

    A numbered designator near the start ("Coursework 1", "Assignment 2")
    is the identity, so "Coursework 1: Model Evaluation Report" and
    "Coursework 1" are one assessment; otherwise the whole normalised title.
    """
    words = _words(title)
    for i, word in enumerate(words[:4]):
        if i > 0 and word.isdigit():
            return " ".join(words[: i + 1])
    return " ".join(words)


def locate(unit: ExtractionUnit, ref: EvidenceRef) -> tuple[GatedEvidence, str]:
    span = unit.resolve(ref.span)
    if span is None:
        raise _Rejected("span_unknown")
    starts = [m.start() for m in re.finditer(f"(?={re.escape(ref.quote)})", span.excerpt)]
    if not starts:
        raise _Rejected("quote_not_found")
    if len(starts) > 1:
        raise _Rejected("quote_not_unique")
    if INSTRUCTION_LIKE.search(span.excerpt):
        raise _Rejected("instruction_like_span")
    return GatedEvidence(span.span_id, starts[0], starts[0] + len(ref.quote)), ref.quote


def _title(unit: ExtractionUnit, proposed: AssessmentOut) -> tuple[str, GatedEvidence]:
    evidence, quote = locate(unit, proposed.title_evidence)
    key = canonical_key(proposed.title)
    if not key or not set(key.split()) <= set(_words(quote)):
        raise _Rejected("title_unsupported")
    return key, evidence


def _weighting(unit: ExtractionUnit, proposed: AssessmentOut) -> GatedWeighting | None:
    if proposed.weighting is None:
        return None
    evidence, quote = locate(unit, proposed.weighting.evidence)
    stated = {int(m.group(1)) for m in _PERCENT.finditer(quote)}
    if stated != {proposed.weighting.percent}:
        raise _Rejected("weighting_unsupported")
    return GatedWeighting(proposed.weighting.percent, evidence)


def _deadline(
    unit: ExtractionUnit, proposed: DeadlineOut, institution_timezone: str | None
) -> GatedDeadline:
    if proposed.evidence is None or proposed.due_date is None:  # the schema forbids it
        raise _Rejected("schema_violation")
    evidence, quote = locate(unit, proposed.evidence)
    try:
        parsed = parse_deadline_quote(quote)
        value = normalise(parsed, institution_timezone)
    except DeadlineRejected as rejected:
        raise _Rejected(rejected.reason) from None
    if date.fromisoformat(proposed.due_date) != value.due_date:
        raise _Rejected("model_date_mismatch")
    if proposed.due_time is not None:
        try:
            claimed = time.fromisoformat(proposed.due_time)
        except ValueError:
            raise _Rejected("model_time_invalid") from None
        if claimed != value.due_time:
            raise _Rejected("model_time_mismatch")
    # A time the quote states but the model omitted is still the quote's: the
    # evidence, not the model, determines the value.
    return GatedDeadline(value, evidence)


def gate(
    unit: ExtractionUnit, extraction: AssessmentExtraction, institution_timezone: str | None
) -> GateResult:
    result = GateResult()
    for proposed in extraction.assessments:
        try:
            key, title_evidence = _title(unit, proposed)
        except _Rejected as rejected:
            result.rejected[f"assessment:{rejected.reason}"] += 1
            continue
        try:
            weighting = _weighting(unit, proposed)
        except _Rejected as rejected:
            result.rejected[f"weighting:{rejected.reason}"] += 1
            weighting = None
        deadline = None
        if proposed.deadline.status != "stated":
            result.abstained[f"deadline:{proposed.deadline.status}"] += 1
        else:
            try:
                deadline = _deadline(unit, proposed.deadline, institution_timezone)
            except _Rejected as rejected:
                result.rejected[f"deadline:{rejected.reason}"] += 1
            else:
                result.review_reasons.update(deadline.value.review_reasons)
        result.assessments.append(
            GatedAssessment(proposed.title.strip(), key, title_evidence, weighting, deadline)
        )
    return result
