# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""The deterministic candidate contract (``mapping-candidates/v1``).

A requirement's retrieval hits become candidates: one per SourceSpan inside
a retrieved chunk, in retrieval order (hit rank, then span order). A span in
two retrieved chunks (chunk overlap) is one candidate, from its best-ranked
chunk. A span whose part inside the chunk has no visible text is no
candidate: there is nothing there to study.

Each candidate gets a local ID ``C1..Cn`` in that order. The model sees only
local IDs, locators, titles and the candidate text; the real span, version
and resource IDs stay server-side. The evidence of a candidate is fixed here,
from retrieval: the span plus the exact range of it inside the chunk.

Only teaching material is a candidate. A hit of any other resource type
means the retrieval was not restricted as it must be: an error, never a
silently dropped hit.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from app.domain.enums import ResourceType
from app.retrieval.search import SearchHit

CANDIDATES_VERSION = "mapping-candidates/v1"
CANDIDATE_ID_PATTERN = r"^C[1-9][0-9]{0,2}$"
MAX_CANDIDATES = 60

# Material a student studies from. Briefs, handbooks, announcements and pages
# state what is assessed or required; they are not where it is taught.
TEACHING_RESOURCE_TYPES = frozenset(
    {ResourceType.LECTURE, ResourceType.LAB, ResourceType.READING, ResourceType.WORKSHOP}
)

RATIONALE_MAX = 300


class RetrievalContractError(ValueError):
    """The hits break the candidate contract (a non-teaching resource, or
    more candidates than one verifier call may see)."""


@dataclass(frozen=True, slots=True)
class Candidate:
    local_id: str
    retrieval_rank: int  # the chunk's 1-based rank
    retrieval_score: float  # retriever-specific, never a confidence
    span_id: uuid.UUID
    span_ordinal: int
    resource_id: uuid.UUID
    resource_title: str
    resource_type: ResourceType
    version_id: uuid.UUID
    page_number: int | None
    slide_number: int | None
    section_path: str | None
    # The evidence: excerpt[quote_start:quote_end] of the span.
    quote_start: int
    quote_end: int
    text: str

    @property
    def locator(self) -> str:
        if self.slide_number is not None:
            return f"Slide {self.slide_number}"
        if self.page_number is not None:
            return f"Page {self.page_number}"
        if self.section_path is not None:
            return f"Section {self.section_path}"
        return "Source"


def build_candidates(hits: Sequence[SearchHit]) -> tuple[Candidate, ...]:
    seen: set[uuid.UUID] = set()
    found: list[Candidate] = []
    for hit in sorted(hits, key=lambda h: h.rank):
        if hit.resource.type not in TEACHING_RESOURCE_TYPES:
            raise RetrievalContractError(f"hit {hit.rank} is not teaching material")
        for span in sorted(hit.spans, key=lambda s: s.ordinal):
            text = span.excerpt[span.relative_start : span.relative_end]
            if span.span_id in seen or not text.strip():
                continue
            seen.add(span.span_id)
            found.append(
                Candidate(
                    local_id=f"C{len(found) + 1}",
                    retrieval_rank=hit.rank,
                    retrieval_score=hit.score,
                    span_id=span.span_id,
                    span_ordinal=span.ordinal,
                    resource_id=hit.resource.id,
                    resource_title=hit.resource.title,
                    resource_type=hit.resource.type,
                    version_id=hit.version.id,
                    page_number=span.page_number,
                    slide_number=span.slide_number,
                    section_path=span.section_path,
                    quote_start=span.relative_start,
                    quote_end=span.relative_end,
                    text=text,
                )
            )
    if len(found) > MAX_CANDIDATES:
        raise RetrievalContractError(f"{len(found)} candidates; at most {MAX_CANDIDATES}")
    return tuple(found)


def navigation_rationale(candidate: Candidate) -> str:
    """Where to go, never what it says: built from the locator and title."""
    text = f"{candidate.locator} of “{candidate.resource_title.strip()}”"
    return text if len(text) <= RATIONALE_MAX else text[: RATIONALE_MAX - 1] + "…"
