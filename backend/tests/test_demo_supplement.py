# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""The supplementary Northbridge courses: kept out of the frozen evaluation
corpus, every authored quote in its document, answers that claim only what
the material shows, and committed vectors that match the documents."""

from __future__ import annotations

import pytest

from app.ai.provider import ChatMessage
from app.connectors.demo import DemoConnector, load_manifest
from app.demo.generate import DATA_ROOT
from app.demo.supplement import (
    AUTHORED_MODEL,
    AuthoredProvider,
    load_document_replay,
    supplement_refs,
)
from app.demo.supplement_content import BRIEFS, SUPPLEMENT_DOCUMENTS
from app.domain.enums import ModelRole
from app.evaluation.retrieval.resolve import production_limits
from app.extraction.obligations import ObligationExtraction
from app.extraction.schema import AssessmentExtraction
from app.ingestion.dispatch import parse_source
from app.ingestion.schemas import RawSource
from app.ingestion.service import build_span_rows
from app.mapping.selection import MappingSelection
from tests.test_demo_replay import SPACE

pytestmark = pytest.mark.anyio


def _norm(text: str) -> str:
    return " ".join(text.split())


def _excerpts() -> list[str]:
    out = []
    for path in SUPPLEMENT_DOCUMENTS:
        source = RawSource(content=(DATA_ROOT / path).read_bytes(),
                           display_name=path.rsplit("/", 1)[1], source_uri="demo://n/c/r")  # fmt: skip
        out += [
            row["excerpt"] for row in build_span_rows(parse_source(source, production_limits()))
        ]
    return out


def test_the_frozen_corpus_never_includes_the_supplement() -> None:
    assert [c.ref for c in load_manifest(DATA_ROOT).courses] == ["aml7001", "msd7000"]
    merged = load_manifest(DATA_ROOT, supplement=True)
    assert [c.ref for c in merged.courses] == ["aml7001", "msd7000", "aif7002", "dvs7003"]
    assert supplement_refs() == {"aif7002", "dvs7003"}
    # Nothing it adds is published after the initial sync: no new changes.
    extra = [c for c in merged.courses if c.ref in supplement_refs()]
    assert all(v.published_at <= merged.initial_sync_at
               for c in extra for r in c.resources for v in r.versions)  # fmt: skip


async def test_the_demo_student_sees_all_four_courses() -> None:
    courses = await DemoConnector(DATA_ROOT, supplement=True).list_courses("demo-student")
    assert [c.code for c in courses] == ["AML7001", "MSD7000", "AIF7002", "DVS7003"]
    assert len(await DemoConnector(DATA_ROOT).list_courses("demo-student")) == 2


def test_every_authored_quote_occurs_once_in_its_document() -> None:
    excerpts = _excerpts()
    for brief in BRIEFS:
        for quote in (brief.title_quote, brief.weighting_quote, brief.due_quote):
            assert sum(quote in e for e in excerpts) == 1, quote
        for requirement in brief.requirements:
            assert sum(_norm(requirement.statement) in _norm(e) for e in excerpts) == 1
            for _, phrase in requirement.taught_in:
                assert sum(_norm(phrase) in _norm(e) for e in excerpts) == 1, phrase


def _material(spans: str) -> list[ChatMessage]:
    user = (f"Extract.\n<<<COURSE_MATERIAL n0>>>\nResource type: brief\nResource title: T\n\n"
            f"{spans}\n<<<END_COURSE_MATERIAL n0>>>")  # fmt: skip
    return [ChatMessage("system", "rules"), ChatMessage("user", user)]


async def test_authored_answers_claim_only_what_the_material_shows() -> None:
    provider = AuthoredProvider()
    brief = BRIEFS[0]
    shown = f"[S1] Page 1\n{brief.title_quote}\n{brief.weighting_quote}\n{brief.due_quote}"
    result = await provider.chat_structured(
        _material(shown), AssessmentExtraction, role=ModelRole.FAST
    )
    assert result.model == AUTHORED_MODEL
    [assessment] = result.output.assessments
    assert assessment.title == brief.title and assessment.deadline.status == "stated"
    assert assessment.deadline.due_date == brief.due_date
    # Without the deadline quote, no deadline is claimed; without the title, nothing.
    partial = f"[S1] Page 1\n{brief.title_quote}"
    [only] = (await provider.chat_structured(_material(partial), AssessmentExtraction,
                                             role=ModelRole.FAST)).output.assessments  # fmt: skip
    assert only.deadline.status == "not_stated" and only.weighting is None
    unrelated = await provider.chat_structured(_material("[S1] Page 1\nWelcome."),
                                               AssessmentExtraction, role=ModelRole.FAST)  # fmt: skip
    assert unrelated.output.assessments == []
    obligations = await provider.chat_structured(_material(shown), ObligationExtraction,
                                                 role=ModelRole.FAST)  # fmt: skip
    assert obligations.output.obligations == []


async def test_authored_mapping_selects_only_the_teaching_spans_it_names() -> None:
    requirement = BRIEFS[0].requirements[0]
    title, phrase = requirement.taught_in[0]
    text = requirement.statement.split(". ", 1)[1]
    user = (
        "Select.\n<<<MAPPING_MATERIAL n0>>>\nCourse: C\nAssessment: A\n"
        f"Requirement: {text}\n\nCandidates:\n"
        f"[C1] Slide 2 — {title}\nA model is not a system\n\n"
        f"[C2] Slide 3 — {title}\n{phrase}\n\n"
        f"[C3] Slide 3 — Another resource\n{phrase}\n<<<END_MAPPING_MATERIAL n0>>>"
    )
    messages = [ChatMessage("system", "rules"), ChatMessage("user", user)]
    picked = await AuthoredProvider().chat_structured(
        messages, MappingSelection, role=ModelRole.FAST
    )
    assert picked.output.candidate_ids == ["C2"]
    unknown = user.replace(f"Requirement: {text}", "Requirement: Something else entirely.")
    messages[1] = ChatMessage("user", unknown)
    none = await AuthoredProvider().chat_structured(messages, MappingSelection, role=ModelRole.FAST)
    assert none.output.decision == "none"


def test_the_committed_vectors_match_the_supplement_documents() -> None:
    # Validates the snapshot header, its hash and every expected input.
    replay = load_document_replay(SPACE, max_batch_items=64)
    assert replay.space == SPACE
