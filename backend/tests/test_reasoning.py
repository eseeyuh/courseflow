# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Deterministic dependency rules and deadline-conflict detection: an edge
needs ordering wording, never mere topical overlap; a conflict needs a
different local date or time."""

from __future__ import annotations

import uuid
from datetime import date, time

from app.db.models import AssessmentDeadline
from app.domain.enums import (
    DependencyNodeKind,
    DependencyRelation,
    Milestone,
    ObligationModality,
)
from app.reasoning.conflicts import disagree
from app.reasoning.dependencies import (
    BEFORE_SUBMITTING,
    DATA_BEFORE_ETHICS,
    ObligationFacts,
    Quote,
    derive_edges,
)

SPEC = uuid.UUID(int=1)


def _facts(
    *texts: str,
    assessment: uuid.UUID | None = SPEC,
    key: str | None = "project specification",
    modality: ObligationModality | None = ObligationModality.MANDATORY,
) -> ObligationFacts:
    quotes = tuple(Quote(uuid.uuid4(), 0, len(t), t) for t in texts)
    return ObligationFacts(uuid.uuid4(), assessment, key, modality, quotes)


def test_a_review_before_submitting_the_assessment_is_required_for_it() -> None:
    o = _facts(
        "Before you submit your Project Specification, your supervisor must have\nreviewed it."
    )
    (edge,) = derive_edges([o])
    assert (edge.rule, edge.relation) == (BEFORE_SUBMITTING, DependencyRelation.REQUIRED_FOR)
    assert edge.prerequisite.kind is DependencyNodeKind.OBLIGATION
    assert edge.prerequisite.obligation_id == o.id
    assert (edge.dependent.kind, edge.dependent.assessment_id) == (
        DependencyNodeKind.ASSESSMENT,
        SPEC,
    )
    assert edge.evidence == o.quotes


def test_recommended_wording_orders_but_does_not_block() -> None:
    o = _facts("Before you submit your Project Specification, ask a peer to read it.",
               modality=ObligationModality.RECOMMENDED)  # fmt: skip
    (edge,) = derive_edges([o])
    assert edge.relation is DependencyRelation.BEFORE


def test_no_edge_without_a_linked_assessment_or_the_ordering_words() -> None:
    assert derive_edges([_facts("Before you submit your Project Specification, ask.",
                                assessment=None, key=None)]) == []  # fmt: skip
    # Another assessment named: not this obligation's assessment.
    assert derive_edges([_facts("Before you submit your Final dissertation, ask.")]) == []
    assert (
        derive_edges([_facts("Your Project Specification is reviewed by your supervisor.")]) == []
    )


def test_data_collection_waits_for_ethics_approval() -> None:
    texts = (
        "No data may be collected for your project before ethics approval has\nbeen granted.",
        (
            "You must not recruit participants or collect any data until your ethics "
            "application has been approved."
        ),
    )
    for text in texts:
        (edge,) = derive_edges([_facts(text, assessment=None, key=None)])
        assert edge.rule == DATA_BEFORE_ETHICS
        assert (edge.prerequisite.milestone, edge.dependent.milestone) == (
            Milestone.ETHICS_APPROVAL,
            Milestone.DATA_COLLECTION,
        )


def test_topical_overlap_is_not_an_ordering() -> None:
    for text in (
        "Ethics approval is described in the guidance.",
        "Data collection methods are covered in week 5.",
        "Collect data carefully and store it securely; ethics matters.",
    ):
        assert derive_edges([_facts(text, assessment=None, key=None)]) == []


def test_two_statements_of_one_ordering_are_one_edge_with_both_quotes() -> None:
    a = _facts("No data may be collected before ethics approval has been granted.", assessment=None)
    b = _facts("You must not collect any data until your ethics application is approved.",
               assessment=None)  # fmt: skip
    (edge,) = derive_edges([a, b])
    assert set(edge.evidence) == set(a.quotes) | set(b.quotes)


def _deadline(day: int, at: time | None, zone: str | None = "Europe/London") -> AssessmentDeadline:
    return AssessmentDeadline(due_date=date(2026, 11, day), due_time=at, timezone=zone)


def test_only_a_different_local_date_or_time_is_a_conflict() -> None:
    assert disagree([_deadline(12, time(14)), _deadline(13, time(23, 59))])
    assert disagree([_deadline(12, time(14)), _deadline(12, time(16))])
    assert not disagree([_deadline(12, time(14)), _deadline(12, time(14), zone=None)])
    assert not disagree([_deadline(12, time(14))])
    assert not disagree([])
