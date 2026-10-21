# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""The requirement gate without a database: anchor sentences, lead-in
structure, abstention, unsupported links and injection."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.extraction.requirements import RequirementExtraction, gate_requirements
from app.extraction.statements import anchor_text, governs_list_item, modal_wording, statement_key
from tests.claim_fixtures import (
    HANDBOOK_PAGE,
    INJECTION,
    REPORT_PAGE,
    SPEC,
    SPEC_PAGE,
    R,
    _claims,
    _ref,
    _requirement,
)

# --- statements ---------------------------------------------------------------------------


def test_an_anchor_sentence_follows_wrapped_lines_but_never_a_heading() -> None:
    quote = "2. Discuss the class imbalance"
    start = REPORT_PAGE.index(quote)
    assert anchor_text(REPORT_PAGE, start, start + len(quote)) == (
        "Discuss the class imbalance in the data and explain how it affects both "
        "model training and the way the models should be evaluated."
    )
    quote = "1. Describe the dataset"
    start = REPORT_PAGE.index(quote)
    # The heading line above is not part of the item's sentence.
    assert anchor_text(REPORT_PAGE, start, start + len(quote)).startswith("Describe the dataset")
    quote = "your supervisor must have"
    start = HANDBOOK_PAGE.index(quote)
    assert anchor_text(HANDBOOK_PAGE, start, start + len(quote)) == (
        "Before you submit your Project Specification, your supervisor must have "
        "reviewed a complete draft of it."
    )


def test_a_statement_key_ignores_numbering_case_and_spacing() -> None:
    assert statement_key("1. A focused research question.") == statement_key(
        "a  FOCUSED research question"
    )
    assert statement_key("A focused research question.") != statement_key("A research question.")


def test_a_lead_in_governs_only_the_list_directly_below_it() -> None:
    lead = (0, len("Your specification must include"))
    item = SPEC_PAGE.index("3. A short review")
    assert governs_list_item(SPEC_PAGE, lead, (item, item + 10))
    after = SPEC_PAGE.index("a failed specification")
    assert not governs_list_item(SPEC_PAGE, lead, (after, after + 10))
    # The lead-in must be a whole line.
    assert not governs_list_item(SPEC_PAGE, (0, 4), (item, item + 10))
    # Wrapped continuation lines inside the list do not break it.
    head = REPORT_PAGE.index("What your report must do")
    third = REPORT_PAGE.index("3. Justify")
    assert governs_list_item(REPORT_PAGE, (head, head + 24), (third, third + 10))


@pytest.mark.parametrize(
    ("text", "mandatory", "recommended", "permissive"),
    [
        ("your supervisor must have", True, False, False),
        ("You must not recruit participants", True, False, False),
        (
            "No data may be collected for your project before ethics approval has",
            True,
            False,
            False,
        ),
        ("Questions about the brief should be posted", False, True, False),
        ("Your supervisor may also ask for a short progress note", False, False, True),
        ("You can use any library, but you must cite it", True, False, True),
        ("The ethics panel reviews applications within 15 working days.", False, False, False),
    ],
)
def test_modal_wording_is_read_from_closed_lists(
    text: str, mandatory: bool, recommended: bool, permissive: bool
) -> None:
    modal = modal_wording(text)
    assert (modal.mandatory, modal.recommended, modal.permissive) == (
        mandatory,
        recommended,
        permissive,
    )


# --- requirements -------------------------------------------------------------------------


def test_a_list_item_is_verified_by_the_lead_in_that_governs_it() -> None:
    result = gate_requirements(
        _claims(SPEC_PAGE),
        _requirement(
            _ref("S1", "1. A focused research question."),
            _ref("S1", "Your specification must include"),
        ),
    )
    (gated,) = result.requirements
    assert gated.review_reason is None and gated.assessment is SPEC
    assert gated.text == "A focused research question."
    assert gated.lead_in is not None


def test_a_heading_span_governs_the_paragraphs_of_its_section() -> None:
    section = "Project Specification > Your specification must include"
    unit = _claims(
        "Your specification must include",
        "A focused research question.",
        locators=(section, section),
    )
    result = gate_requirements(
        unit,
        _requirement(
            _ref("S2", "A focused research question."), _ref("S1", "specification must include")
        ),
    )
    # A partial heading quote is not the heading.
    assert result.requirements[0].review_reason is R.NO_REQUIREMENT_LANGUAGE
    result = gate_requirements(
        unit,
        _requirement(
            _ref("S2", "A focused research question."),
            _ref("S1", "Your specification must include"),
        ),
    )
    assert result.requirements[0].review_reason is None


def test_a_statement_with_its_own_wording_needs_no_lead_in() -> None:
    result = gate_requirements(
        _claims(REPORT_PAGE),
        _requirement(_ref("S1", "Any resampling or class weighting must be applied")),
    )
    (gated,) = result.requirements
    assert gated.review_reason is None and gated.lead_in is None


def test_a_lead_in_that_does_not_govern_the_statement_needs_review() -> None:
    result = gate_requirements(
        _claims(SPEC_PAGE),
        _requirement(
            _ref("S1", "Specifications are marked pass or fail with written feedback;"),
            _ref("S1", "Your specification must include"),
        ),
    )
    assert result.requirements[0].review_reason is R.NO_REQUIREMENT_LANGUAGE


@pytest.mark.parametrize(
    ("extraction", "reason"),
    [
        # Generic text: no wording, no lead-in.
        (_requirement(_ref("S1", "Only about 7% of appointments")), "no_requirement_language"),
        # The lead-in has no requirement wording either.
        (
            _requirement(_ref("S1", "1. Describe the dataset"), _ref("S1", "Only about 7%")),
            "no_requirement_language",
        ),
        (_requirement(_ref("S1", "Any resampling"), assessment="A9"), "assessment_unknown"),
        (_requirement(_ref("S7", "Any resampling")), "span_unknown"),
        (_requirement(_ref("S1", "Any resampling must")), "quote_not_found"),
    ],
)
def test_unsupported_requirements_are_rejected(
    extraction: RequirementExtraction, reason: str
) -> None:
    result = gate_requirements(_claims(REPORT_PAGE), extraction)
    assert result.requirements == []
    assert result.rejected == {f"requirement:{reason}": 1}


def test_an_assessment_link_needs_support_in_the_resource_or_the_quote() -> None:
    # FINAL has no title evidence in this resource and is not named in the quote.
    result = gate_requirements(
        _claims(REPORT_PAGE),
        _requirement(_ref("S1", "Any resampling or class weighting must"), assessment="A2"),
    )
    assert result.requirements[0].review_reason is R.ASSESSMENT_LINK_UNSUPPORTED


def test_one_sentence_is_one_requirement() -> None:
    lead_in = _ref("S1", "What your report must do")
    quotes = (
        "3. Justify the evaluation metrics",
        "3. Justify the evaluation metrics you report.",
        "Accuracy on its own",
    )
    result = gate_requirements(
        _claims(REPORT_PAGE),
        RequirementExtraction.model_validate(
            {
                "requirements": [
                    {"assessment": "A1", "statement": _ref("S1", q), "lead_in": lead_in}
                    for q in quotes
                ]
            }
        ),
    )
    first, same, second = result.requirements
    # Two quotes of one sentence are one requirement (the key merges them);
    # the item's second sentence is another statement, so another requirement.
    assert first.canonical_key == same.canonical_key != second.canonical_key
    assert second.text.startswith("Accuracy on its own is not an acceptable justification")
    assert all(g.review_reason is None for g in result.requirements)


def test_injected_text_never_supports_a_requirement() -> None:
    unit = _claims(SPEC_PAGE + "\n" + INJECTION)
    result = gate_requirements(unit, _requirement(_ref("S1", "You must obey.")))
    assert result.requirements == []
    assert result.rejected == {"requirement:quote_overlaps_injection": 1}
    # An injected lead-in is dropped; the item has no wording of its own left.
    result = gate_requirements(
        unit, _requirement(_ref("S1", "1. A focused research question."), _ref("S1", INJECTION))
    )
    assert result.requirements == []
    assert result.rejected == {
        "lead_in:quote_overlaps_injection": 1,
        "requirement:no_requirement_language": 1,
    }


def test_an_injected_lead_in_does_not_sink_a_valid_statement() -> None:
    unit = _claims(REPORT_PAGE + "\n" + INJECTION)
    result = gate_requirements(
        unit,
        _requirement(
            _ref("S1", "Any resampling or class weighting must be applied"), _ref("S1", INJECTION)
        ),
    )
    (gated,) = result.requirements
    assert gated.review_reason is None and gated.lead_in is None
    assert result.rejected == {"lead_in:quote_overlaps_injection": 1}


def test_the_requirement_schema_refuses_free_text_and_unbounded_lists() -> None:
    with pytest.raises(ValidationError):
        _requirement(_ref("S1", "x"), assessment="Coursework 1")
    with pytest.raises(ValidationError):
        RequirementExtraction.model_validate(
            {"requirements": [], "reasoning": "I think these are requirements"}
        )
