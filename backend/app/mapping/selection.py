# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""What the verifier may say, and the deterministic gate on it.

The verifier sees one requirement and its retrieved candidates and answers
with candidate IDs only: ``selected`` with one or more IDs, or ``none``.
``none`` is a valid, expected answer. It never writes a quote, a location
or a resource ID: the evidence of a selection is the candidate's own,
fixed by retrieval (``candidates``).

The gate keeps only IDs issued for this call. An ID that is well formed but
was never issued (an invented candidate) is rejected on its own, never
guessed; a repeated ID counts once. The selection keeps retrieval order,
whatever order the model used.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import Field, StringConstraints, model_validator

from app.extraction.schema import StrictModel
from app.mapping.candidates import CANDIDATE_ID_PATTERN, Candidate

MAX_SELECTED = 10

CandidateId = Annotated[str, StringConstraints(pattern=CANDIDATE_ID_PATTERN)]


class MappingSelection(StrictModel):
    decision: Literal["selected", "none"]
    candidate_ids: list[CandidateId] = Field(max_length=MAX_SELECTED)

    @model_validator(mode="after")
    def _decision_fits_ids(self) -> Self:
        if self.decision == "selected" and not self.candidate_ids:
            raise ValueError("a selection needs at least one candidate ID")
        if self.decision == "none" and self.candidate_ids:
            raise ValueError("none has no candidate IDs")
        return self


class SelectionRejection(StrEnum):
    CANDIDATE_UNKNOWN = "candidate_unknown"  # well formed, never issued for this call
    CANDIDATE_REPEATED = "candidate_repeated"


@dataclass(frozen=True, slots=True)
class GatedSelection:
    selected: tuple[Candidate, ...]  # in retrieval order
    rejected: tuple[tuple[str, SelectionRejection], ...]

    @property
    def is_none(self) -> bool:
        return not self.selected


def gate_selection(output: MappingSelection, candidates: Sequence[Candidate]) -> GatedSelection:
    issued = {candidate.local_id: candidate for candidate in candidates}
    chosen: set[str] = set()
    rejected: list[tuple[str, SelectionRejection]] = []
    for local_id in output.candidate_ids:
        if local_id not in issued:
            rejected.append((local_id, SelectionRejection.CANDIDATE_UNKNOWN))
        elif local_id in chosen:
            rejected.append((local_id, SelectionRejection.CANDIDATE_REPEATED))
        else:
            chosen.add(local_id)
    selected = tuple(c for c in candidates if c.local_id in chosen)
    return GatedSelection(selected=selected, rejected=tuple(rejected))
