# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

import uuid

from pydantic import BaseModel

from app.api.schemas.assessment import DeadlineRead, EvidenceRead
from app.domain.enums import (
    ConflictField,
    ConflictStatus,
    DependencyNodeKind,
    DependencyRelation,
    Milestone,
    VerificationStatus,
)


class DependencyEndRead(BaseModel):
    """One end of a dependency: a stored assessment or obligation (``id``,
    its title or action as ``label``, its ``status``), or a milestone."""

    kind: DependencyNodeKind
    id: uuid.UUID | None
    milestone: Milestone | None
    label: str
    status: VerificationStatus | None  # None for a milestone


class DependencyEdgeRead(BaseModel):
    """``prerequisite`` comes before ``dependent``; ``required_for``: the
    dependent is blocked without it. ``evidence`` is the exact source wording
    that states the ordering; ``rule`` is the deterministic rule that read it."""

    id: uuid.UUID
    course_id: uuid.UUID
    relation: DependencyRelation
    prerequisite: DependencyEndRead
    dependent: DependencyEndRead
    rule: str
    derived_from_obligation_id: uuid.UUID
    workflow_run_id: uuid.UUID
    evidence: list[EvidenceRead]


class ConflictRead(BaseModel):
    """Sources that disagree on one fact of an assessment. Every side is a
    full deadline with its own evidence; none is chosen, so there is no
    resolved value."""

    id: uuid.UUID
    course_id: uuid.UUID
    assessment_id: uuid.UUID
    assessment_title: str
    field: ConflictField
    status: ConflictStatus
    workflow_run_id: uuid.UUID
    sides: list[DeadlineRead]
