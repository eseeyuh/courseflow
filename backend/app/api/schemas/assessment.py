# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

import uuid
from datetime import date, datetime

from pydantic import BaseModel

from app.domain.enums import (
    DeadlinePrecision,
    EvidenceField,
    TimezoneSource,
    VerificationStatus,
)


class EvidenceRead(BaseModel):
    """One piece of evidence: the exact supporting quote and where it is.

    ``quote`` is ``source_span.excerpt[quote_start:quote_end]``, never model
    text. ``current_version`` is false when the source has a newer version.
    """

    id: uuid.UUID
    field: EvidenceField
    verification_status: VerificationStatus
    quote: str
    source_span_id: uuid.UUID
    page_number: int | None
    slide_number: int | None
    section_path: str | None
    resource_id: uuid.UUID
    resource_title: str
    resource_version_id: uuid.UUID
    version_number: int
    current_version: bool
    model: str | None
    prompt_version: str | None
    model_call_id: uuid.UUID | None
    workflow_run_id: uuid.UUID | None


class DeadlineRead(BaseModel):
    """A deadline as its source states it. ``due_time`` is local ("HH:MM"),
    null for a date with no time; ``due_at`` is the instant, only when date,
    time and timezone are all known. Only ``verified`` is presented as fact."""

    id: uuid.UUID
    due_date: date
    due_time: str | None
    timezone: str | None
    timezone_source: TimezoneSource
    precision: DeadlinePrecision
    due_at: datetime | None
    status: VerificationStatus
    evidence: list[EvidenceRead]


class AssessmentRead(BaseModel):
    """An assessment with every deadline any source states for it. Several
    deadlines mean the sources disagree: they are all shown, none chosen."""

    id: uuid.UUID
    course_id: uuid.UUID
    title: str
    canonical_key: str
    weighting_percent: int | None
    status: VerificationStatus
    evidence: list[EvidenceRead]
    deadlines: list[DeadlineRead]
