# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

import uuid
from datetime import date, datetime

from pydantic import BaseModel

from app.api.schemas.assessment import EvidenceRead
from app.domain.enums import (
    DeadlinePrecision,
    MappingRelation,
    ObligationCategory,
    ObligationModality,
    ResourceType,
    ReviewReason,
    TimezoneSource,
    VerificationStatus,
)


class MaterialMappingRead(BaseModel):
    """Where to study for a requirement: a retrieved teaching span the
    verifier selected. ``quote`` is ``source_span.excerpt[quote_start:quote_end]``
    (the part of the span retrieval returned), never model text; ``rationale``
    is navigation only. A suggestion with lineage, not a verified fact;
    ``retrieval_score`` is retriever-specific, never a confidence."""

    id: uuid.UUID
    relation: MappingRelation
    rationale: str
    quote: str
    source_span_id: uuid.UUID
    page_number: int | None
    slide_number: int | None
    section_path: str | None
    resource_id: uuid.UUID
    resource_title: str
    resource_type: ResourceType
    resource_version_id: uuid.UUID
    version_number: int
    current_version: bool
    retrieval_mode: str
    retrieval_rank: int
    retrieval_score: float
    query_model_call_id: uuid.UUID | None
    verifier_model_call_id: uuid.UUID
    verifier_model: str
    verifier_prompt_version: str
    workflow_run_id: uuid.UUID


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
    # Teaching material to study for it, in source order; empty when none
    # was found or the verifier selected none.
    materials: list[MaterialMappingRead] = []


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
    # Hidden: a verified obligation of an assessment that no brief states (it
    # is found only in other sources, e.g. a handbook). Derived from the
    # evidence's resource types, never from the model.
    hidden: bool = False
