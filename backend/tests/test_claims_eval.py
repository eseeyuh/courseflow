# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""The requirement and obligation evaluation without a model: the plan, the
call limit, and scoring by evidence location (labelled recall, unlabelled
claims for review, never a precision against incomplete labels)."""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import pytest

from app.ai.provider import ModelInfo
from app.domain.enums import ModelRole
from app.evaluation.claims.run import (
    EXPECTED_OBLIGATIONS,
    CallLimitExceeded,
    ClaimRow,
    LimitedProvider,
    LinkRow,
    Location,
    describe_plan,
    plan_calls,
    score_obligations,
    score_requirements,
)
from app.evaluation.northbridge.labels import GoldenDataset, load_golden
from app.extraction.obligations import ObligationExtraction

INFO = ModelInfo(
    id="nvidia/Nemotron-3_5-Lightning",
    context_length=None,
    input_usd_per_million=Decimal("0.06"),
    output_usd_per_million=Decimal("0.24"),
)


@pytest.fixture(scope="module")
def golden() -> GoldenDataset:
    return load_golden()


def test_the_plan_is_fifteen_calls_in_live_order(golden: GoldenDataset) -> None:
    planned = plan_calls(golden)
    assert [p.task for p in planned].count("requirements") == 2
    assert [p.task for p in planned].count("obligations") == 12
    assert planned[-1].task == "control" and len(planned) == 15
    # Requirements read only the briefs, and only their first versions.
    assert {p.where[1] for p in planned if p.task == "requirements"} == {
        "coursework-1-brief",
        "project-specification-brief",
    }
    plan = describe_plan(planned, INFO, attempts=3, repair_rounds=1, spend_cap=Decimal("0.03"))
    assert plan["calls"] == 15 and plan["problems"] == []
    assert Decimal(plan["expected_spend_usd"]) < Decimal("0.01")
    tight = describe_plan(planned, INFO, attempts=3, repair_rounds=1, spend_cap=Decimal("0.0001"))
    assert tight["problems"]


@pytest.mark.anyio
async def test_a_call_beyond_the_plan_is_refused_unsent() -> None:
    class Inner:
        name = "fake"
        sent = 0

        def model_for(self, role: ModelRole) -> str:
            return "m"

        async def chat_structured(self, *args: Any, **kwargs: Any) -> Any:
            Inner.sent += 1
            return None

    provider = LimitedProvider(Inner(), max_calls=1)  # type: ignore[arg-type]
    await provider.chat_structured([], ObligationExtraction, role=ModelRole.FAST)
    with pytest.raises(CallLimitExceeded):
        await provider.chat_structured([], ObligationExtraction, role=ModelRole.FAST)
    assert Inner.sent == 1


def test_expected_values_exist_for_every_labelled_obligation(golden: GoldenDataset) -> None:
    assert set(EXPECTED_OBLIGATIONS) == {o.label_id for o in golden.by_kind("obligation")}


SPAN = uuid.uuid4()


def _claim(start: int, end: int, *, status: str = "verified", **values: Any) -> ClaimRow:
    link = LinkRow(Location(SPAN, start, end), "x", status, "res", 1, "page 2", "quote")
    return ClaimRow(
        id=uuid.uuid4(),
        status=status,
        review_reason=values.get("review_reason"),
        assessment=values.get("assessment"),
        text=values.get("text", "t"),
        category=values.get("category"),
        modality=values.get("modality"),
        links=(link,),
    )


def _locations(golden: GoldenDataset, at: dict[str, tuple[int, int]]) -> dict[str, list[Location]]:
    """Every label at an offset of its own (far apart), some at chosen ones."""
    out = {
        label.label_id: [Location(SPAN, 10_000 + 100 * i, 10_000 + 100 * i + 50)]
        for i, label in enumerate(golden.labels)
    }
    for label_id, (start, end) in at.items():
        out[label_id] = [Location(SPAN, start, end)]
    return out


def test_requirements_match_by_evidence_and_extras_are_only_listed(golden: GoldenDataset) -> None:
    locations = _locations(golden, {"msd-spec-s1-research-question": (0, 30)})
    claims = [
        _claim(5, 20, assessment="project specification"),  # overlaps the label's quote
        _claim(500, 520, assessment="project specification", text="an unlabelled extra"),
    ]
    result = score_requirements(golden, claims, locations)
    assert result["recall_verified"] == 1 and result["labelled"] == 11
    assert result["labels_exhaustive"] is False and "precision" not in result
    assert [r["text"] for r in result["unlabelled_for_review"]] == ["an unlabelled extra"]
    # The right place but the wrong assessment is not a match.
    wrong = score_requirements(golden, [_claim(5, 20, assessment="final dissertation")], locations)
    assert wrong["recall_verified"] == 0
    (row,) = [p for p in wrong["per_label"] if p["label"] == "msd-spec-s1-research-question"]
    assert row["wrong_assessment"] == ["final dissertation"]


def test_obligations_report_the_killer_case_guards_and_extras(golden: GoldenDataset) -> None:
    locations = _locations(
        golden,
        {
            "msd-supervisor-review-v1": (0, 30),
            "msd-supervisor-review-v2": (40, 70),
            "msd-progress-note-ambiguous": (100, 130),
        },
    )
    supervisor = {
        "category": "supervisor_review",
        "modality": "mandatory",
        "assessment": "project specification",
    }
    claims = [
        _claim(0, 10, **supervisor),
        _claim(45, 50, **supervisor),
        _claim(100, 110, status="needs_review", review_reason="permissive_wording"),
        _claim(900, 910, text="an unlabelled extra", category="other", modality="mandatory"),
    ]
    for claim in claims[:2]:
        object.__setattr__(
            claim,
            "links",
            tuple(
                LinkRow(link.location, "obligation", "verified", "dissertation-handbook", 1,
                        "page 2", "q")
                for link in claim.links
            ),
        )  # fmt: skip
    result = score_obligations(golden, claims, locations)
    assert result["recall_verified"] == 2 and result["labelled"] == 3
    assert result["supervisor_review_killer_case"] is True
    assert result["category_correct"] == "2/2" and result["modality_correct"] == "2/2"
    assert result["ambiguous_never_verified"] is True
    assert result["generic_never_verified"] is True
    # Not an obligation label, so listed for review, with the label it overlaps.
    ambiguous, extra = result["unlabelled_for_review"]
    assert ambiguous["overlaps_labels"] == {"needs_review": ["msd-progress-note-ambiguous"]}
    assert extra["text"] == "an unlabelled extra" and extra["overlaps_labels"] == {}
    assert "precision" not in result and "f1" not in result
    # A verified claim on an ambiguous statement fails the guard.
    claims[2] = _claim(100, 110)
    assert score_obligations(golden, claims, locations)["ambiguous_never_verified"] is False
