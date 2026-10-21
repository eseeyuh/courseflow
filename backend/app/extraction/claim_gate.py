# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Quote location for requirement and obligation claims (``claim-gate/v2``).

v2 changes one thing about locating a quote: a run of whitespace in the
model's quote matches any run of whitespace in the span (a PDF line break
where a sentence wraps, or several spaces). Nothing else is normalised:
case, punctuation, hyphens, letters and words must match exactly. The
evidence is always the span's ORIGINAL text between the returned offsets,
never the model's quote; offsets are code points into ``span.excerpt``.

Every distinct start offset at which the quote matches is a location, so a
quote that matches more than once is non-unique (needs_review); none is
chosen. Injection overlap is checked on the resolved original offsets,
exactly as for an exact match. The assessment gate (``assessment-gate/v3``)
keeps exact location.
"""

from __future__ import annotations

import re

from app.domain.enums import RejectionReason
from app.extraction.gate import GatedEvidence, Located, Rejected, injection_fragments
from app.extraction.schema import EvidenceRef
from app.extraction.units import ExtractionUnit

GATE_VERSION = "claim-gate/v2"
_WHITESPACE = re.compile(r"\s+")


def quote_pattern(quote: str) -> re.Pattern[str] | None:
    """The quote with each whitespace run matching any whitespace run; None
    for a quote that is only whitespace."""
    parts = _WHITESPACE.split(quote.strip())
    if parts == [""]:
        return None
    return re.compile(r"\s+".join(re.escape(part) for part in parts))


def locate_quote(unit: ExtractionUnit, ref: EvidenceRef) -> tuple[Located, str]:
    """Every location of the quote in its span, and the span's original text
    at the first one."""
    span = unit.resolve(ref.span)
    if span is None:
        raise Rejected(RejectionReason.SPAN_UNKNOWN)
    pattern = quote_pattern(ref.quote)
    if pattern is None:
        raise Rejected(RejectionReason.QUOTE_NOT_FOUND)
    found: dict[int, int] = {}
    for start in range(len(span.excerpt)):
        match = pattern.match(span.excerpt, start)
        if match is not None and match.end() > start:
            found.setdefault(start, match.end())
    if not found:
        raise Rejected(RejectionReason.QUOTE_NOT_FOUND)
    locations = tuple(GatedEvidence(span.span_id, a, b) for a, b in sorted(found.items()))
    fragments = injection_fragments(span.excerpt)
    # Injection text is inert content: it can never support a claim.
    if any(a < e.quote_end and e.quote_start < b for e in locations for a, b in fragments):
        raise Rejected(RejectionReason.QUOTE_OVERLAPS_INJECTION)
    first = locations[0]
    return Located(locations), span.excerpt[first.quote_start : first.quote_end]
