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
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, time

from app.domain.enums import RejectionReason, ReviewReason, VerificationStatus
from app.extraction.dates import (
    DeadlineRejected,
    NormalisedDeadline,
    date_spans,
    normalise,
    parse_deadline_quote,
)
from app.extraction.schema import AssessmentExtraction, AssessmentOut, DeadlineOut, EvidenceRef
from app.extraction.units import ExtractionUnit, UnitSpan

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


class Rejected(Exception):
    def __init__(self, reason: RejectionReason) -> None:
        self.reason = reason


# v2: fact-level gating, derived assessment status, typed review reasons.
# v3: the temporal role of a deadline quote; identity from sibling evidence.
GATE_VERSION = "assessment-gate/v3"

# Temporal roles (v3, conservative). Explicit non-deadline wording overrides
# everything; a deadline cue, or the labelled form "<assessment title>:
# <date>", establishes a deadline; anything else does not.
NON_DEADLINE_ROLE = re.compile(
    r"\b(?:releas\w*|publish\w*|publication|issued|available|opens|opened|opening"
    r"|feedback|returned)\b",
    re.IGNORECASE,
)
DEADLINE_ROLE = re.compile(
    r"\b(?:deadlines?|due|submit\w*|submission|hand(?:ed)?\s+in|no\s+later\s+than"
    r"|closes|closing)\b",
    re.IGNORECASE,
)

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
        return V.VERIFIED if self.review_reason is None else V.NEEDS_REVIEW

    @property
    def review_reason(self) -> ReviewReason | None:
        """The value's first reason, else the location's; None when verified."""
        reasons = list(self.value.review_reasons)
        if self.evidence.status is not V.VERIFIED:
            reasons.append(ReviewReason.NON_UNIQUE_QUOTE_LOCATION)
        return reasons[0] if reasons else None


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
    # Where each persisted candidate's identity is supported: "title" (its own
    # title evidence) or "<fact>_quote" / "<fact>_span" / "<fact>_heading".
    identity: Counter[str] = field(default_factory=Counter)


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
        raise Rejected(RejectionReason.SPAN_UNKNOWN)
    starts = [m.start() for m in re.finditer(f"(?={re.escape(ref.quote)})", span.excerpt)]
    if not starts:
        raise Rejected(RejectionReason.QUOTE_NOT_FOUND)
    locations = tuple(GatedEvidence(span.span_id, a, a + len(ref.quote)) for a in starts)
    fragments = injection_fragments(span.excerpt)
    # Injection text is inert content: it can never support a claim.
    if any(a < e.quote_end and e.quote_start < b for e in locations for a, b in fragments):
        raise Rejected(RejectionReason.QUOTE_OVERLAPS_INJECTION)
    return Located(locations), ref.quote


def _title(unit: ExtractionUnit, proposed: AssessmentOut) -> tuple[str, Located]:
    evidence, quote = locate(unit, proposed.title_evidence)
    key = canonical_key(proposed.title)
    if not key or not set(key.split()) <= set(_words(quote)):
        raise Rejected(RejectionReason.TITLE_UNSUPPORTED)
    return key, evidence


def _weighting(unit: ExtractionUnit, proposed: AssessmentOut) -> tuple[GatedWeighting, str] | None:
    if proposed.weighting is None:
        return None
    evidence, quote = locate(unit, proposed.weighting.evidence)
    stated = {int(m.group(1)) for m in _PERCENT.finditer(quote)}
    if stated != {proposed.weighting.percent}:
        raise Rejected(RejectionReason.WEIGHTING_UNSUPPORTED)
    return GatedWeighting(proposed.weighting.percent, evidence), quote


def _role(quote: str, key: str) -> None:
    """Reject a quote whose date is not established as a deadline (v3)."""
    if NON_DEADLINE_ROLE.search(quote):
        raise Rejected(RejectionReason.NON_DEADLINE_DATE)
    for start, end in date_spans(quote):
        before, after = quote[:start].rstrip(), quote[end:].lstrip()
        if before.endswith("(") and after.startswith(")"):
            raise Rejected(RejectionReason.NON_DEADLINE_DATE)  # a notice's own date
    if DEADLINE_ROLE.search(quote):
        return
    label, colon, rest = quote.partition(":")
    labelled = (
        colon
        and key
        and set(key.split()) <= set(_words(label))
        and date_spans(rest)
        and date_spans(rest)[0][0] == len(rest) - len(rest.lstrip())
    )
    if not labelled:
        raise Rejected(RejectionReason.NO_DEADLINE_SEMANTICS)


def check_deadline(
    unit: ExtractionUnit,
    proposed: DeadlineOut,
    institution_timezone: str | None,
    key: str,
    locator: Callable[[ExtractionUnit, EvidenceRef], tuple[Located, str]] | None = None,
) -> tuple[GatedDeadline, str]:
    """``locator`` finds the quote (default: exact ``locate``, this gate's rule)."""
    if proposed.evidence is None or proposed.due_date is None:  # the schema forbids it
        raise Rejected(RejectionReason.SCHEMA_VIOLATION)
    evidence, quote = (locator or locate)(unit, proposed.evidence)
    try:
        parsed = parse_deadline_quote(quote)
        _role(quote, key)
        value = normalise(parsed, institution_timezone)
    except DeadlineRejected as rejected:
        raise Rejected(rejected.reason) from None
    if date.fromisoformat(proposed.due_date) != value.due_date:
        raise Rejected(RejectionReason.MODEL_DATE_MISMATCH)
    if proposed.due_time is not None:
        try:
            claimed = time.fromisoformat(proposed.due_time)
        except ValueError:
            raise Rejected(RejectionReason.MODEL_TIME_INVALID) from None
        if claimed != value.due_time:
            raise Rejected(RejectionReason.MODEL_TIME_MISMATCH)
    # A time the quote states but the model omitted is still the quote's: the
    # evidence, not the model, determines the value.
    return GatedDeadline(value, evidence), quote


def _note_location(result: GateResult, fact: str, evidence: Located) -> None:
    if evidence.status is not V.VERIFIED:
        result.review_reasons[f"{fact}:{ReviewReason.NON_UNIQUE_QUOTE_LOCATION}"] += 1


def _key_pattern(key: str) -> re.Pattern[str]:
    return re.compile(
        r"(?<![^\W_])" + r"[\W_]+".join(re.escape(w) for w in key.split()) + r"(?![^\W_])",
        re.IGNORECASE,
    )


def _occurrences(span: UnitSpan, key: str, within: tuple[int, int] | None = None) -> Located | None:
    start, end = within or (0, len(span.excerpt))
    fragments = injection_fragments(span.excerpt)
    found = [
        GatedEvidence(span.span_id, m.start(), m.end())
        for m in _key_pattern(key).finditer(span.excerpt, start, end)
        # Title words inside injected text are not identity evidence either.
        if not any(a < m.end() and m.start() < b for a, b in fragments)
    ]
    return Located(tuple(found)) if found else None


def _span_of(unit: ExtractionUnit, evidence: Located) -> UnitSpan | None:
    span_id = evidence.locations[0].span_id
    return next((s for s in unit.spans if s.span_id == span_id), None)


def _identity_from(
    unit: ExtractionUnit, key: str, fact: str, evidence: Located
) -> tuple[Located, str] | None:
    """The title words in a verified fact's own evidence: its quote, else its
    span, else the heading its span's section path names (the same document
    structure, never another part of the course). Exact offsets; None if absent."""
    if evidence.status is not V.VERIFIED or not key:
        return None
    span = _span_of(unit, evidence)
    if span is None:
        return None
    location = evidence.locations[0]
    quote = _occurrences(span, key, (location.quote_start, location.quote_end))
    if quote is not None:
        return quote, f"{fact}_quote"
    whole = _occurrences(span, key)
    if whole is not None:
        return whole, f"{fact}_span"
    heading_text = span.locator.rsplit(" > ", 1)[-1]
    for heading in unit.spans:
        if heading is not span and heading.excerpt == heading_text:
            found = _occurrences(heading, key)
            if found is not None:
                return found, f"{fact}_heading"
    return None


def gate(
    unit: ExtractionUnit, extraction: AssessmentExtraction, institution_timezone: str | None
) -> GateResult:
    result = GateResult()
    for proposed in extraction.assessments:
        key = canonical_key(proposed.title)
        title_failure: RejectionReason | None = None
        try:
            key, title_evidence = _title(unit, proposed)
        except Rejected as rejected:
            title_failure, title_evidence = rejected.reason, None
        if title_failure is RejectionReason.QUOTE_OVERLAPS_INJECTION or not key:
            result.rejected[f"assessment:{title_failure or RejectionReason.TITLE_UNSUPPORTED}"] += 1
            continue
        weighting = None
        try:
            found_weighting = _weighting(unit, proposed)
        except Rejected as rejected:
            result.rejected[f"weighting:{rejected.reason}"] += 1
        else:
            if found_weighting is not None:
                weighting, _ = found_weighting
        deadline = None
        if proposed.deadline.status != "stated":
            result.abstained[f"deadline:{proposed.deadline.status}"] += 1
        else:
            try:
                deadline, _ = check_deadline(unit, proposed.deadline, institution_timezone, key)
            except Rejected as rejected:
                result.rejected[f"deadline:{rejected.reason}"] += 1
        identity = "title"
        if title_evidence is None:
            # The dedicated title citation failed: a verified sibling fact may
            # still support the identity, from its own evidence only.
            support = None
            for fact, gated in (("deadline", deadline), ("weighting", weighting)):
                if gated is not None and gated.status is V.VERIFIED:
                    support = _identity_from(unit, key, fact, gated.evidence)
                    if support is not None:
                        break
            if support is None:
                result.rejected[f"assessment:{title_failure}"] += 1
                continue
            result.rejected[f"title:{title_failure}"] += 1
            title_evidence, identity = support
        result.identity[identity] += 1
        _note_location(result, "title", title_evidence)
        if weighting is not None:
            _note_location(result, "weighting", weighting.evidence)
        if deadline is not None:
            _note_location(result, "deadline", deadline.evidence)
            result.review_reasons.update(
                f"deadline:{reason}" for reason in deadline.value.review_reasons
            )
        result.assessments.append(
            GatedAssessment(proposed.title.strip(), key, title_evidence, weighting, deadline)
        )
    return result
