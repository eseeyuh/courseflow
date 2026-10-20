# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""The structured output of assessment extraction: what the model may say.

Every field is required (nullable rather than defaulted) for strict JSON
schema mode. Evidence is a server-issued local span ID plus an exact quote;
the model never names a real span, a location or a timezone. The values it
proposes (a date, a time, a percentage) are checked against the quote by
deterministic code, which alone decides what is verified.
"""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

LOCAL_SPAN_ID_PATTERN = r"^S[1-9][0-9]{0,3}$"

LocalSpanId = Annotated[str, StringConstraints(pattern=LOCAL_SPAN_ID_PATTERN)]
Quote = Annotated[str, StringConstraints(min_length=1, max_length=400)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EvidenceRef(_Strict):
    span: LocalSpanId
    quote: Quote


class DeadlineOut(_Strict):
    # stated: the material gives a full calendar date. not_stated / ambiguous
    # are abstentions (no date, or only a month, week or term).
    status: Literal["stated", "not_stated", "ambiguous"]
    due_date: Annotated[str, StringConstraints(pattern=r"^\d{4}-\d{2}-\d{2}$")] | None
    due_time: Annotated[str, StringConstraints(pattern=r"^\d{2}:\d{2}$")] | None
    evidence: EvidenceRef | None

    @model_validator(mode="after")
    def _stated_has_value_and_evidence(self) -> Self:
        if self.status == "stated" and (self.due_date is None or self.evidence is None):
            raise ValueError("a stated deadline needs due_date and evidence")
        if self.status != "stated" and (self.due_date is not None or self.due_time is not None):
            raise ValueError("an abstention has no due_date or due_time")
        return self


class WeightingOut(_Strict):
    percent: int = Field(ge=0, le=100)
    evidence: EvidenceRef


class AssessmentOut(_Strict):
    title: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    title_evidence: EvidenceRef
    weighting: WeightingOut | None
    deadline: DeadlineOut


class AssessmentExtraction(_Strict):
    assessments: list[AssessmentOut] = Field(max_length=10)
