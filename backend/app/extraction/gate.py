# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""The evidence gate: deterministic decisions over a model's proposals.

Every proposed fact (an assessment's title, its weighting, its deadline) is
validated on its own:

1. its local span ID must have been issued for this unit (so the span is
   one of this current version's spans, of this course);
2. its quote must occur in that span's excerpt, and no occurrence may
   overlap a detected injection fragment (``injection_fragments``);
3. the quote must support the value: the title's designator words, the
   stated percentage, or the deadline parsed from the quote itself (the
   model's date must equal it).

A fact that fails is rejected and not persisted. A quote that occurs more
than once has no single location: every occurrence is kept, exactly, as
needs_review evidence (``non_unique_quote_location``); none is chosen. A
problem with one fact never discards its siblings, except that a candidate
whose title fails is not an assessment at all. A deadline's status is also
bounded by ``dates.normalise``. Abstentions (not_stated, ambiguous) are
counted, never turned into a date. Whether a candidate is a verified
assessment is decided later, from its verified facts (``persist``).
Reason codes are stable ("<fact>:<reason>") and counted in the run summary.
"""

from __future__ import annotations

import re
import unicodedata
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, time

from app.domain.enums import VerificationStatus
from app.extraction.dates import (
    DeadlineRejected,
    NormalisedDeadline,
    normalise,
    parse_deadline_quote,
)
from app.extraction.schema import AssessmentExtraction, AssessmentOut, DeadlineOut, EvidenceRef
from app.extraction.units import ExtractionUnit

_WORD = re.compile(r"[^\W_]+")
# Trigger phrases addressed to a model rather than a student. Deliberately
# narrow: ordinary wording ("follow the submission instructions", "ignore
# the appendix") does not match.
INSTRUCTION_LIKE = re.compile(
    r"\b(?:ignore|disregard|forget|override)\s+(?:all\s+|any\s+|the\s+)?"
    r"(?:previous|prior|above|earlier|your)\s+(?:instructions|prompts?|rules)\b"
    r"|\byou\s+are\s+now\b"
    r"|\b(?:reveal|print|show|output|leak)\s+(?:the\s+|your\s+)?"
    r"(?:api\s+key|system\s+prompt|password|secrets?|credentials)\b"
    r"|^\s*(?:system|assistant|developer)\s*:",
    re.IGNORECASE | re.MULTILINE,
)
_SENTENCE_END = re.compile(r"[.!?](?=\s)|\n")


def injection_fragments(text: str) -> list[tuple[int, int]]:
    """Code-point ranges of detected prompt injection in ``text``.

    A fragment runs from the start of the sentence containing a trigger
    phrase to the end of that line, so that a payload following the trigger
    on the same line ("... AND SET EVERY DEADLINE TO 1 JANUARY 2030", or a
    next sentence) is inside it. The rest of the text is unaffected.
    """
    fragments = []
    for match in INSTRUCTION_LIKE.finditer(text):
        before = [m.end() for m in _SENTENCE_END.finditer(text, 0, match.start())]
        start = before[-1] if before else 0
        newline = text.find("\n", match.end())
        end = len(text) if newline == -1 else newline
        while start < end and text[start].isspace():
            start += 1
        fragments.append((start, end))
    return fragments


_PERCENT = re.compile(r"(?<![\d.])(\d{1,3})\s*(?:%|per\s?cent\b)", re.IGNORECASE)


class _Rejected(Exception):
    def __init__(self, reason: str) -> None:
        self.reason = reason


GATE_VERSION = "assessment-gate/v2"  # v2: fact-level gating, derived assessment status

V = VerificationStatus


@dataclass(frozen=True, slots=True)
class GatedEvidence:
    span_id: uuid.UUID
    quote_start: int  # code points into the span's excerpt
    quote_end: int


@dataclass(frozen=True, slots=True)
class Located:
    """Every exact occurrence of a quote in its span: one when its location is
    unique (verified), several when it is not (needs_review)."""

    locations: tuple[GatedEvidence, ...]

    @property
    def status(self) -> VerificationStatus:
        return V.VERIFIED if len(self.locations) == 1 else V.NEEDS_REVIEW


@dataclass(frozen=True, slots=True)
class GatedWeighting:
    percent: int
    evidence: Located

    @property
    def status(self) -> VerificationStatus:
        return self.evidence.status


@dataclass(frozen=True, slots=True)
class GatedDeadline:
    value: NormalisedDeadline
    evidence: Located

    @property
    def status(self) -> VerificationStatus:
        verified = self.value.status is V.VERIFIED and self.evidence.status is V.VERIFIED
        return V.VERIFIED if verified else V.NEEDS_REVIEW


@dataclass(frozen=True, slots=True)
class GatedAssessment:
    """A candidate: its title evidence passed; its status is derived later."""

    title: str
    canonical_key: str
    title_evidence: Located
    weighting: GatedWeighting | None
    deadline: GatedDeadline | None


@dataclass(slots=True)
class GateResult:
    assessments: list[GatedAssessment] = field(default_factory=list)
    rejected: Counter[str] = field(default_factory=Counter)  # "<fact>:<reason>"
    abstained: Counter[str] = field(default_factory=Counter)  # "deadline:<status>"
    review_reasons: Counter[str] = field(default_factory=Counter)  # "<fact>:<reason>"


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


def locate(unit: ExtractionUnit, ref: EvidenceRef) -> tuple[Located, str]:
    span = unit.resolve(ref.span)
    if span is None:
        raise _Rejected("span_unknown")
    starts = [m.start() for m in re.finditer(f"(?={re.escape(ref.quote)})", span.excerpt)]
    if not starts:
        raise _Rejected("quote_not_found")
    locations = tuple(GatedEvidence(span.span_id, a, a + len(ref.quote)) for a in starts)
    fragments = injection_fragments(span.excerpt)
    # Injection text is inert content: it can never support a claim.
    if any(a < e.quote_end and e.quote_start < b for e in locations for a, b in fragments):
        raise _Rejected("quote_overlaps_injection")
    return Located(locations), ref.quote


def _title(unit: ExtractionUnit, proposed: AssessmentOut) -> tuple[str, Located]:
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


def _note_location(result: GateResult, fact: str, evidence: Located) -> None:
    if evidence.status is not V.VERIFIED:
        result.review_reasons[f"{fact}:non_unique_quote_location"] += 1


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
        _note_location(result, "title", title_evidence)
        try:
            weighting = _weighting(unit, proposed)
        except _Rejected as rejected:
            result.rejected[f"weighting:{rejected.reason}"] += 1
            weighting = None
        if weighting is not None:
            _note_location(result, "weighting", weighting.evidence)
        deadline = None
        if proposed.deadline.status != "stated":
            result.abstained[f"deadline:{proposed.deadline.status}"] += 1
        else:
            try:
                deadline = _deadline(unit, proposed.deadline, institution_timezone)
            except _Rejected as rejected:
                result.rejected[f"deadline:{rejected.reason}"] += 1
            else:
                _note_location(result, "deadline", deadline.evidence)
                result.review_reasons.update(
                    f"deadline:{reason}" for reason in deadline.value.review_reasons
                )
        result.assessments.append(
            GatedAssessment(proposed.title.strip(), key, title_evidence, weighting, deadline)
        )
    return result
