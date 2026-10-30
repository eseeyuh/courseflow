# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Requirement -> teaching material mapping, deterministic parts: the
candidate contract, the gate on the verifier's IDs, the schema and
persistence. A final mapping is always one of the retrieved candidates,
with the candidate's own exact evidence. No model is called."""

from __future__ import annotations

import itertools
import json
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import (
    AssessmentRequirement,
    MaterialMapping,
    ModelCall,
    Resource,
    ResourceVersion,
    SourceSpan,
    WorkflowRun,
)
from app.domain.enums import (
    BlockKind,
    MappingRelation,
    ModelCallKind,
    ModelCallStatus,
    ModelRole,
    ResourceType,
    VerificationStatus,
    WorkflowRunStatus,
)
from app.evaluation.mapping.split import Expect, Split, load_split
from app.evaluation.northbridge.labels import LabelError, load_golden
from app.evaluation.retrieval.resolve import production_limits
from app.ingestion.schemas import RawSource
from app.ingestion.service import ingest_file
from app.ingestion.storage import LocalRawObjectStore
from app.mapping.candidates import (
    MAX_CANDIDATES,
    RATIONALE_MAX,
    Candidate,
    RetrievalContractError,
    build_candidates,
    navigation_rationale,
)
from app.mapping.persist import MappingIntegrityError, MappingLineage, replace_mappings
from app.mapping.selection import MappingSelection, SelectionRejection, gate_selection
from app.retrieval.search import ResourceRef, SearchHit, SpanEvidence, VersionRef
from tests.test_assessment_schema import _add, _assessment, _course

pytestmark = pytest.mark.anyio

KEY = "a" * 64
LECTURE = (
    b"<!DOCTYPE html><html><body><h1>Week 7: Imbalanced Data</h1>"
    b"<h2>Accuracy</h2><p>With 7% positives, always predicting the majority class gives "
    b"93% accuracy.</p><h2>Resampling</h2>"
    b"<p>Resample only the training split, never the test set.</p></body></html>"
)
LECTURE_V2 = LECTURE.replace(b"93% accuracy", b"93 percent accuracy")
BRIEF = b"<!DOCTYPE html><html><body><h1>Coursework 1</h1><p>Discuss imbalance.</p></body></html>"


# --- candidates (pure) --------------------------------------------------------------------


def _evidence(ordinal: int, excerpt: str, start: int = 0, end: int | None = None) -> SpanEvidence:
    end = len(excerpt) if end is None else end
    return SpanEvidence(
        span_id=uuid.UUID(int=ordinal + 1),
        ordinal=ordinal,
        block_kind=BlockKind.PARAGRAPH,
        page_number=None,
        slide_number=ordinal + 1,
        section_path=None,
        span_start=0,
        span_end=len(excerpt),
        overlap_start=start,
        overlap_end=end,
        relative_start=start,
        relative_end=end,
        coverage="full" if (start, end) == (0, len(excerpt)) else "partial",
        excerpt=excerpt,
    )


def _hit(rank: int, *spans: SpanEvidence, kind: ResourceType = ResourceType.LECTURE) -> SearchHit:
    return SearchHit(
        rank=rank,
        score=1 / (60 + rank),
        chunk_id=uuid.uuid4(),
        chunk_ordinal=rank - 1,
        start_offset=0,
        end_offset=1,
        text="chunk text",
        resource=ResourceRef(uuid.UUID(int=99), "Week 7: Imbalanced Data", kind, "upload:l7"),
        version=VersionRef(uuid.UUID(int=98), 1),
        spans=spans,
    )


def test_candidates_follow_retrieval_order_and_get_sequential_local_ids() -> None:
    a, b, c = _evidence(0, "Slide one."), _evidence(1, "Slide two."), _evidence(2, "Slide three.")
    candidates = build_candidates([_hit(2, a), _hit(1, c, b)])  # given out of order
    assert [x.local_id for x in candidates] == ["C1", "C2", "C3"]
    assert [x.span_ordinal for x in candidates] == [1, 2, 0]  # rank 1 (by ordinal), then rank 2
    assert [x.retrieval_rank for x in candidates] == [1, 1, 2]
    assert candidates[0].text == "Slide two." and candidates[0].locator == "Slide 2"


def test_a_span_in_two_chunks_is_one_candidate_from_its_best_ranked_chunk() -> None:
    whole = _evidence(0, "A long slide that two chunks share.")
    tail = _evidence(0, "A long slide that two chunks share.", start=7)
    candidates = build_candidates([_hit(1, whole), _hit(2, tail)])
    assert len(candidates) == 1
    assert (candidates[0].retrieval_rank, candidates[0].quote_start) == (1, 0)


def test_a_span_part_without_visible_text_is_no_candidate() -> None:
    blank = _evidence(0, "Text   \n  ", start=4)
    assert build_candidates([_hit(1, blank)]) == ()


@pytest.mark.parametrize(
    "kind", [ResourceType.BRIEF, ResourceType.HANDBOOK, ResourceType.ANNOUNCEMENT]
)
def test_a_non_teaching_hit_breaks_the_contract_and_is_never_dropped_silently(
    kind: ResourceType,
) -> None:
    with pytest.raises(RetrievalContractError, match="not teaching material"):
        build_candidates([_hit(1, _evidence(0, "Due Friday."), kind=kind)])


def test_more_candidates_than_one_call_may_see_break_the_contract() -> None:
    spans = [_evidence(i, f"Slide {i}.") for i in range(MAX_CANDIDATES + 1)]
    with pytest.raises(RetrievalContractError, match="candidates"):
        build_candidates([_hit(1, *spans)])


def test_the_rationale_is_navigation_only_and_bounded() -> None:
    (candidate,) = build_candidates([_hit(1, _evidence(1, "With 7% positives ..."))])
    assert navigation_rationale(candidate) == "Slide 2 of “Week 7: Imbalanced Data”"
    section = replace(candidate, slide_number=None, section_path="Workshop 7 > Exercise 1")
    assert (
        navigation_rationale(section)
        == "Section Workshop 7 > Exercise 1 of “Week 7: Imbalanced Data”"
    )
    page = replace(candidate, slide_number=None, page_number=3, resource_title="x" * 500)
    text = navigation_rationale(page)
    assert text.startswith("Page 3 of “x") and len(text) == RATIONALE_MAX and text.endswith("…")


# --- the verifier's output and the gate (pure) --------------------------------------------


@pytest.mark.parametrize(
    "output",
    [
        {"decision": "none", "candidate_ids": ["C1"]},
        {"decision": "selected", "candidate_ids": []},
        {"decision": "selected", "candidate_ids": ["S1"]},  # a span ID, not a candidate
        {"decision": "selected", "candidate_ids": ["C0"]},
        {"decision": "selected", "candidate_ids": [str(uuid.uuid4())]},  # a real-looking ID
        {"decision": "selected", "candidate_ids": [f"C{i}" for i in range(1, 12)]},
        {"decision": "maybe", "candidate_ids": []},
        {"decision": "none", "candidate_ids": [], "rationale": "free text"},
    ],
)
def test_malformed_verifier_output_is_a_schema_violation(output: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        MappingSelection.model_validate(output)


def _three() -> tuple[Candidate, ...]:
    return build_candidates(
        [_hit(1, _evidence(0, "One."), _evidence(1, "Two.")), _hit(2, _evidence(2, "Three."))]
    )


def test_none_is_a_valid_answer_and_maps_nothing() -> None:
    gated = gate_selection(MappingSelection(decision="none", candidate_ids=[]), _three())
    assert gated.is_none and gated.rejected == ()


def test_invented_ids_are_rejected_on_their_own_and_order_is_retrieval_order() -> None:
    output = MappingSelection(decision="selected", candidate_ids=["C3", "C9", "C1", "C3"])
    gated = gate_selection(output, _three())
    assert [c.local_id for c in gated.selected] == ["C1", "C3"]
    assert gated.rejected == (
        ("C9", SelectionRejection.CANDIDATE_UNKNOWN),
        ("C3", SelectionRejection.CANDIDATE_REPEATED),
    )


def test_only_invented_ids_map_nothing() -> None:
    gated = gate_selection(MappingSelection(decision="selected", candidate_ids=["C4"]), _three())
    assert gated.is_none and gated.rejected == (("C4", SelectionRejection.CANDIDATE_UNKNOWN),)


def test_every_possible_selection_is_a_subset_of_the_retrieved_candidates() -> None:
    candidates = _three()
    issued = {c.local_id for c in candidates}
    pool = ["C1", "C2", "C3", "C4", "C7", "C50"]
    for size in range(1, 5):
        for ids in itertools.product(pool, repeat=size):
            gated = gate_selection(
                MappingSelection(decision="selected", candidate_ids=list(ids)), candidates
            )
            chosen = {c.local_id for c in gated.selected}
            assert chosen <= issued and chosen == set(ids) & issued
            assert all(c in candidates for c in gated.selected)


# --- the dev / held-out split ---------------------------------------------------------------


def test_the_split_names_every_requirement_once_with_its_label_expectation() -> None:
    cases = load_split(load_golden())
    by_split = {split: [c for c in cases if c.split is split] for split in Split}
    assert len(cases) == 11 and len({c.label_id for c in cases}) == 11
    assert "aml-cw1-r2-class-imbalance" in {c.label_id for c in by_split[Split.DEV]}
    held_out = {c.expect for c in by_split[Split.HELD_OUT]}
    assert held_out == {Expect.MATERIALS, Expect.NO_MATERIAL, Expect.UNLABELLED}
    assert len(by_split[Split.HELD_OUT]) > len(by_split[Split.DEV])


def test_a_split_that_disagrees_with_the_labels_is_refused(tmp_path: Path) -> None:
    golden = load_golden()
    raw = json.loads(
        (
            Path(__file__).resolve().parents[2]
            / "evals/datasets/northbridge/splits/material-mapping-v2.json"
        ).read_text(encoding="utf-8")
    )
    raw["dev"][2]["expect"] = "unlabelled"  # s6 states materials: []
    wrong = tmp_path / "split.json"
    wrong.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(LabelError, match="expectation differs"):
        load_split(golden, wrong)
    raw["dev"][2]["expect"] = "no_material"
    raw["held_out"].pop()
    wrong.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(LabelError, match="exactly once"):
        load_split(golden, wrong)


# --- schema and persistence (database) -----------------------------------------------------


@pytest.fixture
def sessions(committed_session: AsyncSession) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(committed_session.bind, expire_on_commit=False)


@dataclass
class World:
    course_id: uuid.UUID
    requirement_id: uuid.UUID
    lecture: tuple[Candidate, ...]  # current teaching spans as candidates
    brief: Candidate
    stale: Candidate  # a span of a superseded lecture version
    foreign: Candidate  # teaching material of another course
    lineage: MappingLineage
    other_run_id: uuid.UUID


async def _ingest(
    sessions: async_sessionmaker[AsyncSession],
    root: Path,
    course_id: uuid.UUID,
    kind: ResourceType,
    content: bytes,
    uri: str,
) -> uuid.UUID:
    async with sessions() as session:
        result = await ingest_file(
            session,
            LocalRawObjectStore(root),
            course_id=course_id,
            resource_type=kind,
            source=RawSource(content=content, display_name=f"{uri}.html", source_uri=uri),
            limits=production_limits(),
        )
        await session.commit()
    return result.version_id


async def _candidates(
    sessions: async_sessionmaker[AsyncSession], version_id: uuid.UUID
) -> tuple[Candidate, ...]:
    """The version's spans as candidates, as retrieval would issue them."""
    async with sessions() as session:
        rows = await session.execute(
            select(SourceSpan, ResourceVersion, Resource)
            .join(ResourceVersion, ResourceVersion.id == SourceSpan.resource_version_id)
            .join(Resource, Resource.id == ResourceVersion.resource_id)
            .where(SourceSpan.resource_version_id == version_id)
            .order_by(SourceSpan.ordinal)
        )
        return tuple(
            Candidate(
                local_id=f"C{i}",
                retrieval_rank=1,
                retrieval_score=0.032,
                span_id=span.id,
                span_ordinal=span.ordinal,
                resource_id=resource.id,
                resource_title=resource.title,
                resource_type=resource.type,
                version_id=version.id,
                page_number=span.page_number,
                slide_number=span.slide_number,
                section_path=span.section_path,
                quote_start=0,
                quote_end=len(span.excerpt),
                text=span.excerpt,
            )
            for i, (span, version, resource) in enumerate(rows, start=1)
        )


def _call(kind: ModelCallKind, run_id: uuid.UUID | None) -> ModelCall:
    chat = kind is ModelCallKind.CHAT_STRUCTURED
    return ModelCall(
        workflow_run_id=run_id,
        call_kind=kind,
        provider="test",
        model="test-model",
        model_role=ModelRole.FAST if chat else None,
        prompt_version="material-mapping/v1" if chat else "query/v1",
        output_schema="MappingSelection" if chat else "vector[8]",
        status=ModelCallStatus.SUCCEEDED,
        output_summary="{}",
        attempts=1,
        latency_ms=1,
        started_at=datetime.now(UTC),
        finished_at=datetime.now(UTC),
    )


@pytest.fixture
async def world(sessions: async_sessionmaker[AsyncSession], tmp_path: Path) -> World:
    root = tmp_path / "raw"
    root.mkdir()
    course_id = await _course(sessions)
    other_course = await _course(sessions, "Other University")
    old = await _ingest(sessions, root, course_id, ResourceType.LECTURE, LECTURE, "upload:l7")
    stale = (await _candidates(sessions, old))[1]
    current = await _ingest(
        sessions, root, course_id, ResourceType.LECTURE, LECTURE_V2, "upload:l7"
    )
    brief = await _ingest(sessions, root, course_id, ResourceType.BRIEF, BRIEF, "upload:cw1")
    foreign = await _ingest(sessions, root, other_course, ResourceType.LECTURE, LECTURE, "upload:x")
    assessment = _assessment(course_id)
    await _add(sessions, assessment)
    requirement = AssessmentRequirement(
        assessment_id=assessment.id,
        canonical_key=KEY,
        text="Discuss the class imbalance.",
        status=VerificationStatus.VERIFIED,
    )
    run = WorkflowRun(
        workflow_version="material-mapping/v1",
        course_id=course_id,
        status=WorkflowRunStatus.RUNNING,
    )
    other_run = WorkflowRun(
        workflow_version="material-mapping/v1",
        course_id=other_course,
        status=WorkflowRunStatus.RUNNING,
    )
    await _add(sessions, requirement, run, other_run)
    verifier = _call(ModelCallKind.CHAT_STRUCTURED, run.id)
    query = _call(ModelCallKind.EMBEDDING, run.id)
    await _add(sessions, verifier, query)
    return World(
        course_id=course_id,
        requirement_id=requirement.id,
        lecture=await _candidates(sessions, current),
        brief=(await _candidates(sessions, brief))[0],
        stale=stale,
        foreign=(await _candidates(sessions, foreign))[1],
        lineage=MappingLineage(
            retrieval_mode="hybrid",
            query_model_call_id=query.id,
            verifier_model_call_id=verifier.id,
            workflow_run_id=run.id,
        ),
        other_run_id=other_run.id,
    )


async def _replace(
    sessions: async_sessionmaker[AsyncSession],
    world: World,
    selected: tuple[Candidate, ...],
    lineage: MappingLineage | None = None,
) -> Any:
    async with sessions() as session, session.begin():
        return await replace_mappings(
            session, world.requirement_id, selected, lineage or world.lineage
        )


async def _mappings(sessions: async_sessionmaker[AsyncSession]) -> list[MaterialMapping]:
    async with sessions() as session:
        return list(
            (
                await session.scalars(select(MaterialMapping).order_by(MaterialMapping.quote_end))
            ).all()
        )


async def test_a_selection_is_stored_with_the_candidates_exact_evidence_and_lineage(
    sessions: async_sessionmaker[AsyncSession], world: World
) -> None:
    target = world.lecture[1]
    counts = await _replace(sessions, world, (target,))
    assert (counts.created, counts.kept, counts.removed) == (1, 0, 0)
    (row,) = await _mappings(sessions)
    async with sessions() as session:
        span = await session.get(SourceSpan, row.source_span_id)
    assert span is not None and span.excerpt[row.quote_start : row.quote_end] == target.text
    assert row.relation is MappingRelation.SUPPORTED_BY
    assert row.rationale == navigation_rationale(target)
    assert (row.retrieval_mode, row.retrieval_rank, row.retrieval_score) == ("hybrid", 1, 0.032)
    assert row.verifier_model_call_id == world.lineage.verifier_model_call_id
    assert row.query_model_call_id == world.lineage.query_model_call_id
    assert row.workflow_run_id == world.lineage.workflow_run_id


async def test_rerunning_the_same_selection_keeps_rows_and_ids(
    sessions: async_sessionmaker[AsyncSession], world: World
) -> None:
    selected = world.lecture[1:3]
    await _replace(sessions, world, selected)
    before = {(m.id, m.source_span_id) for m in await _mappings(sessions)}
    counts = await _replace(sessions, world, tuple(reversed(selected)))
    assert (counts.created, counts.kept, counts.removed) == (0, 2, 0)
    assert {(m.id, m.source_span_id) for m in await _mappings(sessions)} == before


async def test_a_changed_selection_replaces_and_none_clears(
    sessions: async_sessionmaker[AsyncSession], world: World
) -> None:
    first, second, third = world.lecture[1], world.lecture[2], world.lecture[3]
    await _replace(sessions, world, (first, second))
    counts = await _replace(sessions, world, (second, third))
    assert (counts.created, counts.kept, counts.removed) == (1, 1, 1)
    assert {m.source_span_id for m in await _mappings(sessions)} == {second.span_id, third.span_id}
    counts = await _replace(sessions, world, ())
    assert (counts.created, counts.kept, counts.removed) == (0, 0, 2)
    assert await _mappings(sessions) == []


@pytest.mark.parametrize(
    ("target", "message"),
    [
        ("brief", "not current teaching material"),
        ("stale", "not current teaching material"),
        ("foreign", "not current teaching material"),
        ("wrong_version", "not current teaching material"),
        ("wrong_text", "not exact"),
        ("out_of_bounds", "not exact"),
        ("unknown_span", "does not exist"),
    ],
)
async def test_a_target_outside_the_contract_writes_nothing(
    sessions: async_sessionmaker[AsyncSession], world: World, target: str, message: str
) -> None:
    good = world.lecture[1]
    bad = {
        "brief": world.brief,
        "stale": world.stale,
        "foreign": world.foreign,
        "wrong_version": replace(good, version_id=world.stale.version_id),
        "wrong_text": replace(good, text=good.text + "!"),
        "out_of_bounds": replace(good, quote_end=good.quote_end + 1, text=good.text),
        "unknown_span": replace(good, span_id=uuid.uuid4()),
    }[target]
    await _replace(sessions, world, (world.lecture[2],))
    with pytest.raises(MappingIntegrityError, match=message):
        await _replace(sessions, world, (world.lecture[3], bad))
    # The earlier mapping is untouched: nothing was written or removed.
    assert [m.source_span_id for m in await _mappings(sessions)] == [world.lecture[2].span_id]


@pytest.mark.parametrize("broken", ["other_run", "verifier_is_embedding", "query_is_chat", "mode"])
async def test_broken_lineage_writes_nothing(
    sessions: async_sessionmaker[AsyncSession], world: World, broken: str
) -> None:
    lineage = world.lineage
    lineage = {
        "other_run": replace(lineage, workflow_run_id=world.other_run_id),
        "verifier_is_embedding": replace(
            lineage, verifier_model_call_id=lineage.query_model_call_id
        ),
        "query_is_chat": replace(lineage, query_model_call_id=lineage.verifier_model_call_id),
        "mode": replace(lineage, retrieval_mode="lexical"),  # yet a query call is named
    }[broken]
    with pytest.raises(MappingIntegrityError):
        await _replace(sessions, world, (world.lecture[1],), lineage)
    assert await _mappings(sessions) == []


async def _row(world: World, **values: Any) -> MaterialMapping:
    target = world.lecture[1]
    fields: dict[str, Any] = {
        "requirement_id": world.requirement_id,
        "relation": MappingRelation.SUPPORTED_BY,
        "source_span_id": target.span_id,
        "quote_start": 0,
        "quote_end": 5,
        "rationale": "Slide 1 of “Lecture”",
        "retrieval_mode": "hybrid",
        "retrieval_rank": 1,
        "retrieval_score": 0.03,
        "query_model_call_id": world.lineage.query_model_call_id,
        "verifier_model_call_id": world.lineage.verifier_model_call_id,
        "workflow_run_id": world.lineage.workflow_run_id,
    }
    return MaterialMapping(**(fields | values))


@pytest.mark.parametrize(
    ("values", "constraint"),
    [
        ({"quote_start": 5, "quote_end": 5}, "ck_material_mappings_quote_ordered"),
        ({"quote_start": -1}, "ck_material_mappings_quote_ordered"),
        ({"rationale": "  "}, "ck_material_mappings_rationale_nonblank_bounded"),
        ({"rationale": "x" * 301}, "ck_material_mappings_rationale_nonblank_bounded"),
        ({"retrieval_rank": 0}, "ck_material_mappings_retrieval_rank_positive"),
        ({"retrieval_mode": "rerank"}, "ck_material_mappings_retrieval_mode_known"),
        ({"query_model_call_id": None}, "ck_material_mappings_query_call_iff_embedded"),
        ({"retrieval_mode": "lexical"}, "ck_material_mappings_query_call_iff_embedded"),
    ],
)
async def test_the_schema_refuses_impossible_mappings(
    sessions: async_sessionmaker[AsyncSession],
    world: World,
    values: dict[str, Any],
    constraint: str,
) -> None:
    with pytest.raises(IntegrityError, match=constraint):
        await _add(sessions, await _row(world, **values))


async def test_one_row_per_target_and_mappings_go_with_their_requirement(
    sessions: async_sessionmaker[AsyncSession], world: World
) -> None:
    await _add(sessions, await _row(world))
    with pytest.raises(IntegrityError, match="uq_material_mappings_target"):
        await _add(sessions, await _row(world, quote_end=7))
    # A span cannot be deleted while a mapping uses it.
    with pytest.raises(IntegrityError, match="fk_material_mappings_source_span_id"):
        async with sessions() as session, session.begin():
            await session.execute(
                delete(SourceSpan).where(SourceSpan.id == world.lecture[1].span_id)
            )
    async with sessions() as session, session.begin():
        await session.execute(
            delete(AssessmentRequirement).where(AssessmentRequirement.id == world.requirement_id)
        )
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(MaterialMapping)) == 0
