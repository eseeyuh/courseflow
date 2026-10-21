# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

import uuid
from datetime import date, datetime

from pydantic import BaseModel

from app.api.schemas.assessment import EvidenceRead
from app.domain.enums import (
    DeadlinePrecision,
    ObligationCategory,
    ObligationModality,
    ReviewReason,
    TimezoneSource,
    VerificationStatus,
)


class RequirementRead(BaseModel):
    """A requirement of an assessment: ``text`` is the source statement itself,
    never a paraphrase. Only ``verified`` is presented as fact;
    ``supported_by_current_version`` is false when all its evidence is in
    superseded versions."""

    id: uuid.UUID
    assessment_id: uuid.UUID
    assessment_title: str
    text: str
    status: VerificationStatus
    review_reason: ReviewReason | None
    supported_by_current_version: bool
    evidence: list[EvidenceRead]


class DueRead(BaseModel):
    """An obligation's explicit due date, with deadline semantics."""

    due_date: date
    due_time: str | None
    timezone: str | None
    timezone_source: TimezoneSource
    precision: DeadlinePrecision
    due_at: datetime | None


class ObligationRead(BaseModel):
    """An academic action a student must or should perform. ``canonical_action``
    is the model's wording and ``category`` its classification; ``modality``
    comes from the source wording (null when it does not determine one).
    Only ``verified`` is presented as fact."""

    id: uuid.UUID
    course_id: uuid.UUID
    assessment_id: uuid.UUID | None
    canonical_action: str
    category: ObligationCategory
    modality: ObligationModality | None
    due: DueRead | None
    status: VerificationStatus
    review_reason: ReviewReason | None
    supported_by_current_version: bool
    evidence: list[EvidenceRead]
