# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""The frozen retrieval benchmark: file rules, the dataset hash, and target
resolution against the production ingestion pipeline. Nothing here embeds,
ranks or scores: dataset validation must never reveal retrieval quality."""

import json
import re
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import openai
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai.nebius_embeddings import NebiusEmbeddingProvider
from app.db.models import Base, ModelCall
from app.evaluation.retrieval import cli as dataset_cli
from app.evaluation.retrieval.dataset import (
    DatasetError,
    dataset_sha256,
    hashed_paths,
    load_dataset,
)
from app.evaluation.retrieval.resolve import (
    occurrences,
    production_limits,
    validate_in_database,
)
from app.evaluation.retrieval.schema import CaseCategory
from app.ingestion.dispatch import parse_source
from app.ingestion.schemas import RawSource
from app.ingestion.service import build_span_rows

DATASET = Path(__file__).resolve().parents[2] / "evals" / "datasets" / "retrieval" / "v1"
EVALUATION_SOURCES = Path(__file__).resolve().parents[1] / "app" / "evaluation"
CRITICAL = "ds101-mixed-cw1-deadline"


@pytest.fixture
def copy(tmp_path: Path) -> Path:
    root = tmp_path / "v1"
    shutil.copytree(DATASET, root)
    return root


def _refreeze(root: Path) -> None:
    """Record the copy's own hash, so a test reaches the rule it is about."""
    manifest = load_dataset(root, verify_recorded_sha=False).manifest
    sha = dataset_sha256(root, hashed_paths(manifest))
    (root / "dataset.sha256").write_bytes(f"{sha}\n".encode("ascii"))


def _cases(root: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in (root / "cases.jsonl").read_text("utf-8").splitlines()]


def _write_cases(root: Path, cases: list[dict[str, Any]]) -> None:
    body = "".join(json.dumps(case) + "\n" for case in cases)
    (root / "cases.jsonl").write_bytes(body.encode("utf-8"))


def _edit_case(root: Path, case_id: str, edit: Callable[[dict[str, Any]], None]) -> None:
    cases = _cases(root)
    (case,) = [c for c in cases if c["case_id"] == case_id]
    edit(case)
    _write_cases(root, cases)


def _edit_manifest(root: Path, edit: Callable[[dict[str, Any]], None]) -> None:
    manifest = json.loads((root / "manifest.json").read_text("utf-8"))
    edit(manifest)
    (root / "manifest.json").write_bytes((json.dumps(manifest, indent=2) + "\n").encode("utf-8"))


def _problems(root: Path, *, verify_recorded_sha: bool = False) -> tuple[str, ...]:
    with pytest.raises(DatasetError) as caught:
        load_dataset(root, verify_recorded_sha=verify_recorded_sha)
    return caught.value.problems


def _target(case: dict[str, Any]) -> dict[str, Any]:
    return case["target_groups"][0]["alternatives"][0]


# --- the frozen dataset -----------------------------------------------------------------------


def test_the_frozen_dataset_loads_and_matches_its_recorded_hash() -> None:
    dataset = load_dataset(DATASET)

    manifest = dataset.manifest
    assert (manifest.dataset, manifest.dataset_version) == ("courseflow-retrieval", "v1")
    assert manifest.expected_chunker == "span-pack/v1"
    assert len(dataset.cases) == manifest.case_count == 14
    assert {c: manifest.category_counts[c] for c in CaseCategory} == {
        CaseCategory.KEYWORD: 4,
        CaseCategory.PARAPHRASE: 5,
        CaseCategory.MIXED: 3,
        CaseCategory.STALE_VERSION_INVARIANT: 1,
        CaseCategory.CROSS_COURSE_INVARIANT: 1,
    }
    assert (manifest.positive_case_count, manifest.invariant_case_count) == (12, 2)
    assert [c.case_id for c in dataset.cases if c.critical] == [CRITICAL]
    assert manifest.critical_case.case_id == CRITICAL
    resources = [r for course in manifest.courses for r in course.resources]
    assert len(resources) == 13
    assert (DATASET / "dataset.sha256").read_text("ascii") == f"{dataset.sha256}\n"


def test_the_dataset_hash_is_deterministic_and_independent_of_location(copy: Path) -> None:
    first, second = load_dataset(DATASET), load_dataset(DATASET)

    assert first.sha256 == second.sha256 == load_dataset(copy).sha256
    assert re.fullmatch(r"[0-9a-f]{64}", first.sha256)
    assert hashed_paths(first.manifest)[:2] == [
        "cases.jsonl",
        "corpus/ds101/careers-fair-notice/v1.html",
    ]
    assert "dataset.sha256" not in hashed_paths(first.manifest)
    assert "README.md" not in hashed_paths(first.manifest)


def test_changing_one_corpus_byte_changes_the_hash(copy: Path) -> None:
    path = copy / "corpus" / "ds101" / "module-handbook" / "v1.html"
    data = bytearray(path.read_bytes())
    index = data.index(b"ten percentage points")
    data[index] = ord("T")
    path.write_bytes(bytes(data))

    changed = load_dataset(copy, verify_recorded_sha=False).sha256

    assert changed != load_dataset(DATASET).sha256
    assert any("dataset.sha256" in p for p in _problems(copy, verify_recorded_sha=True))


def test_changing_one_query_changes_the_hash(copy: Path) -> None:
    _edit_case(copy, CRITICAL, lambda c: c.update(query=c["query"] + " exactly"))

    assert load_dataset(copy, verify_recorded_sha=False).sha256 != load_dataset(DATASET).sha256
    assert any("dataset.sha256" in p for p in _problems(copy, verify_recorded_sha=True))


def test_the_readme_is_not_part_of_the_dataset_identity(copy: Path) -> None:
    (copy / "README.md").write_text("Rewritten documentation.\n", encoding="utf-8")

    assert load_dataset(copy).sha256 == load_dataset(DATASET).sha256


def test_loading_makes_no_network_call(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("dataset loading must not touch the network")

    import socket

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(httpx.Client, "send", forbidden)
    monkeypatch.setattr(httpx.AsyncClient, "send", forbidden)

    assert load_dataset(DATASET).sha256


def test_evaluation_code_cannot_rank_search_or_fuse() -> None:
    """The dataset is frozen, and the snapshot tooling may embed its inputs,
    but nothing in the evaluation package ranks, searches or fuses yet: no
    retrieval score can be computed by it."""
    names = ("rank_vector", "rank_lexical", "SearchService", "fuse_rrf", "app.retrieval.search",
             "app.retrieval.fusion")  # fmt: skip
    for source in EVALUATION_SOURCES.rglob("*.py"):
        content = source.read_text("utf-8")
        assert not [name for name in names if name in content], source.name


# --- file and cross-file rules --------------------------------------------------------------


def test_duplicate_case_ids_are_rejected(copy: Path) -> None:
    cases = _cases(copy)
    cases[1]["case_id"] = cases[0]["case_id"]
    _write_cases(copy, cases)

    assert any("is used 2 times" in p for p in _problems(copy))


@pytest.mark.parametrize(
    ("edit", "expected"),
    [
        (lambda c: c.update(course="ds999"), "unknown course"),
        (lambda c: _target(c).update(resource="lecture-99"), "unknown resource ds101/lecture-99"),
        (lambda c: _target(c).update(version=3), "has no version 3"),
        (lambda c: c["distractors"][0].update(resource="nope"), "unknown resource ds101/nope"),
    ],
)
def test_unknown_courses_resources_and_versions_are_rejected(
    copy: Path, edit: Callable[[dict[str, Any]], None], expected: str
) -> None:
    _edit_case(copy, CRITICAL, edit)

    assert any(expected in p for p in _problems(copy))


def test_an_invalid_resource_type_filter_is_rejected(copy: Path) -> None:
    _edit_case(
        copy, "ds101-kw-explain-analyze-buffers", lambda c: c.update(resource_types=["slides"])
    )

    assert any("resource_types" in p for p in _problems(copy))


def test_a_target_excluded_by_its_own_resource_type_filter_is_rejected(copy: Path) -> None:
    _edit_case(
        copy, "ds101-kw-explain-analyze-buffers", lambda c: c.update(resource_types=["lecture"])
    )

    assert any("excluded by the case's resource_types filter" in p for p in _problems(copy))


def test_an_invalid_resource_type_in_the_manifest_is_rejected(copy: Path) -> None:
    _edit_manifest(copy, lambda m: m["courses"][0]["resources"][0].update(type="poster"))

    assert any("courses.0.resources.0.type" in p for p in _problems(copy))


def test_an_invalid_category_is_rejected(copy: Path) -> None:
    _edit_case(copy, CRITICAL, lambda c: c.update(category="semantic"))

    assert any("category" in p for p in _problems(copy))


def test_a_target_on_a_superseded_version_is_rejected(copy: Path) -> None:
    _edit_case(copy, CRITICAL, lambda c: _target(c).update(version=1))

    assert any("is not the current version (v2)" in p for p in _problems(copy))


@pytest.mark.parametrize("critical", [[], [CRITICAL, "ds101-para-deadlock"]])
def test_exactly_one_case_is_critical(copy: Path, critical: list[str]) -> None:
    cases = _cases(copy)
    for case in cases:
        case["critical"] = case["case_id"] in critical
    _write_cases(copy, cases)

    assert any("exactly one case must be critical" in p for p in _problems(copy))


def test_an_invariant_case_cannot_be_critical(copy: Path) -> None:
    cases = _cases(copy)
    for case in cases:
        case["critical"] = case["case_id"] == "ds101-inv-stale-group-submission"
    _write_cases(copy, cases)

    assert any("the critical case must be a positive case" in p for p in _problems(copy))


@pytest.mark.parametrize(
    ("edit", "expected"),
    [
        (lambda m: m.update(case_count=15), "case_count"),
        (lambda m: m["category_counts"].update(keyword=5, paraphrase=4), "category_counts"),
        (lambda m: m.update(positive_case_count=11), "positive_case_count"),
        (lambda m: m["critical_case"].update(case_id="ds101-para-deadlock"), "critical_case"),
    ],
)
def test_manifest_counts_must_match_the_case_file(
    copy: Path, edit: Callable[[dict[str, Any]], None], expected: str
) -> None:
    _edit_manifest(copy, edit)

    assert any(expected in p for p in _problems(copy))


@pytest.mark.parametrize(
    ("case_id", "edit", "expected"),
    [
        (
            "ds101-inv-stale-group-submission",
            lambda c: c["forbidden"][0].update(version=2),
            "must name a superseded version",
        ),
        (
            "ds101-inv-cross-course-loudness",
            lambda c: c["forbidden"][0].update(course="ds101", resource="module-handbook"),
            "foreign_course must be in another course",
        ),
        ("ds101-inv-stale-group-submission", lambda c: c.update(forbidden=[]), "forbidden"),
        (
            CRITICAL,
            lambda c: c["distractors"][1].update(course="ds101", resource="module-handbook"),
            "must be in another course",
        ),
        (CRITICAL, lambda c: c["distractors"][0].update(version=2), "superseded version"),
    ],
)
def test_forbidden_and_distractor_references_must_fit_their_kind(
    copy: Path, case_id: str, edit: Callable[[dict[str, Any]], None], expected: str
) -> None:
    _edit_case(copy, case_id, edit)

    assert any(expected in p for p in _problems(copy))


def test_every_distractor_kind_must_be_labelled_somewhere(copy: Path) -> None:
    cases = _cases(copy)
    for case in cases:
        case["distractors"] = [
            d for d in case["distractors"] if d["kind"] != "cross_course_homonym"
        ]
    _write_cases(copy, cases)

    assert any("cross_course_homonym" in p for p in _problems(copy))


def test_corpus_files_must_be_listed_present_and_lf_only(copy: Path) -> None:
    (copy / "corpus" / "ds101" / "extra.html").write_text("<p>unlisted</p>\n", encoding="utf-8")
    (copy / "corpus" / "snd210" / "studio-handbook" / "v1.html").unlink()
    path = copy / "corpus" / "ds101" / "lab-02-sql-joins" / "v1.html"
    path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))

    problems = " | ".join(_problems(copy))

    assert "corpus/ds101/extra.html: not listed in the manifest" in problems
    assert "corpus/snd210/studio-handbook/v1.html: missing" in problems
    assert "lab-02-sql-joins/v1.html: carriage returns are not allowed" in problems


def test_duplicate_json_keys_are_rejected(copy: Path) -> None:
    lines = (copy / "cases.jsonl").read_text("utf-8").splitlines()
    lines[0] = lines[0][:-1] + ', "critical": true}'
    (copy / "cases.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

    assert any("duplicate JSON key" in p for p in _problems(copy))


def test_check_and_hash_commands(capsys: pytest.CaptureFixture[str], copy: Path) -> None:
    assert dataset_cli.main(["check", "--dataset", str(DATASET)]) == dataset_cli.EXIT_OK
    checked = json.loads(capsys.readouterr().out)
    _edit_case(copy, CRITICAL, lambda c: c.update(query="changed"))

    assert dataset_cli.main(["check", "--dataset", str(copy)]) == dataset_cli.EXIT_INVALID_DATASET
    invalid = json.loads(capsys.readouterr().out)
    assert dataset_cli.main(["hash", "--dataset", str(copy)]) == dataset_cli.EXIT_OK
    hashed = json.loads(capsys.readouterr().out)

    assert checked["sha256"] == load_dataset(DATASET).sha256 and checked["cases"] == 14
    assert invalid["error"] == "invalid_dataset"
    assert hashed["sha256"] != checked["sha256"]


# --- resolution against the production pipeline ----------------------------------------------


@pytest.fixture
def session_factory(committed_session: AsyncSession) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(committed_session.bind, expire_on_commit=False)


async def _row_counts(sf: async_sessionmaker[AsyncSession]) -> dict[str, int]:
    async with sf() as session:
        return {
            table.name: int(await session.scalar(select(func.count()).select_from(table)) or 0)
            for table in Base.metadata.sorted_tables
        }


def _extracted(path: Path) -> tuple[str, list[dict[str, Any]]]:
    """The production parser's text and spans for one corpus file."""
    document = parse_source(
        RawSource(content=path.read_bytes(), display_name=path.name, source_uri="eval:check"),
        production_limits(),
    )
    return document.extracted_text, build_span_rows(document)


@pytest.mark.anyio
async def test_every_target_resolves_to_one_span_sub_range_and_a_containing_chunk(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    dataset = load_dataset(DATASET)

    resolved = await validate_in_database(session_factory, dataset)

    assert (resolved.chunks_current, resolved.chunks_all) == (39, 41)
    is_positive = {case.case_id: case.is_positive for case in dataset.cases}
    positive = [case for case in resolved.cases if is_positive[case.case_id]]
    assert len(positive) == 12
    for case in positive:
        assert case.target_groups and all(case.target_groups)
        for group in case.target_groups:
            for target in group:
                ref = target.ref
                text_, spans = _extracted(
                    dataset.corpus_file(ref.course, ref.resource, ref.version)
                )
                assert text_[target.start : target.end] == ref.quote
                assert occurrences(text_, ref.quote) == [target.start]
                span = spans[target.span_ordinal]
                assert span["excerpt"][target.relative_start : target.relative_end] == ref.quote
                assert target.containing_chunks
                assert set(target.containing_chunks) <= set(target.overlapping_chunks)


@pytest.mark.anyio
async def test_forbidden_references_resolve_to_stale_and_foreign_versions(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    dataset = load_dataset(DATASET)

    resolved = await validate_in_database(session_factory, dataset)

    cases = {case.case_id: case for case in resolved.cases}
    stale = cases["ds101-inv-stale-group-submission"]
    foreign = cases["ds101-inv-cross-course-loudness"]
    assert stale.forbidden_versions == (("ds101", "coursework-brief", 1),)
    assert not resolved.versions[("ds101", "coursework-brief", 1)].current
    assert resolved.versions[("ds101", "coursework-brief", 2)].current
    assert stale.forbidden_version_ids == (
        resolved.versions[("ds101", "coursework-brief", 1)].version_id,
    )
    assert foreign.forbidden_versions == (
        ("snd210", "lecture-04-loudness-targets", 1),
        ("snd210", "portfolio-brief", 1),
    )
    ds101 = resolved.versions[("ds101", "module-handbook", 1)].course_id
    for key in foreign.forbidden_versions:
        assert resolved.versions[key].course_id != ds101
    assert all(
        not case.forbidden_versions for case in resolved.cases if case not in (stale, foreign)
    )


@pytest.mark.anyio
async def test_validation_leaves_nothing_behind_and_calls_no_provider(
    session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("dataset validation must not build a provider or call a network API")

    monkeypatch.setattr(NebiusEmbeddingProvider, "from_settings", forbidden)
    monkeypatch.setattr(NebiusEmbeddingProvider, "__init__", forbidden)
    monkeypatch.setattr(openai.AsyncOpenAI, "__init__", forbidden)
    monkeypatch.setattr(httpx.AsyncClient, "send", forbidden)
    before = await _row_counts(session_factory)

    await validate_in_database(session_factory, load_dataset(DATASET))

    assert await _row_counts(session_factory) == before
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(ModelCall)) == 0


async def _resolution_problems(sf: async_sessionmaker[AsyncSession], root: Path) -> tuple[str, ...]:
    _refreeze(root)
    with pytest.raises(DatasetError) as caught:
        await validate_in_database(sf, load_dataset(root))
    return caught.value.problems


@pytest.mark.anyio
async def test_an_ambiguous_quote_is_rejected_unless_an_occurrence_selects_one(
    session_factory: async_sessionmaker[AsyncSession], copy: Path
) -> None:
    _edit_case(copy, CRITICAL, lambda c: _target(c).update(quote="Coursework 1"))

    problems = await _resolution_problems(session_factory, copy)

    assert any("occurs" in p and "add an occurrence selector" in p for p in problems)
    _edit_case(copy, CRITICAL, lambda c: _target(c).update(quote="Coursework 1", occurrence=99))
    assert any(
        "occurrence 99 requested" in p for p in await _resolution_problems(session_factory, copy)
    )
    _edit_case(copy, CRITICAL, lambda c: _target(c).update(quote="Coursework 1", occurrence=2))
    _refreeze(copy)
    resolved = await validate_in_database(session_factory, load_dataset(copy))
    ((target,),) = next(c for c in resolved.cases if c.case_id == CRITICAL).target_groups
    text_, _ = _extracted(copy / "corpus" / "ds101" / "coursework-brief" / "v2.html")
    assert target.start == occurrences(text_, "Coursework 1")[1]  # the second, as selected


@pytest.mark.anyio
async def test_a_missing_quote_is_rejected(
    session_factory: async_sessionmaker[AsyncSession], copy: Path
) -> None:
    _edit_case(
        copy, CRITICAL, lambda c: _target(c).update(quote="This sentence is not in the brief.")
    )

    problems = await _resolution_problems(session_factory, copy)

    assert any(f"{CRITICAL} target current-deadline: quote not found" in p for p in problems)


@pytest.mark.anyio
async def test_a_quote_from_the_wrong_version_is_rejected(
    session_factory: async_sessionmaker[AsyncSession], copy: Path
) -> None:
    """The text exists, but only in the superseded brief: naming the current
    version as its source is a labelling error, never silently accepted."""
    v1_only = (
        "Coursework 1 may be completed in groups of up to three students, with one submission "
        "per group and a short statement of each member's contribution."
    )
    _edit_case(copy, CRITICAL, lambda c: _target(c).update(quote=v1_only))

    problems = await _resolution_problems(session_factory, copy)

    assert any("quote not found in ds101/coursework-brief v2" in p for p in problems)


@pytest.mark.anyio
async def test_a_quote_crossing_a_span_boundary_is_rejected(
    session_factory: async_sessionmaker[AsyncSession], copy: Path
) -> None:
    path = DATASET / "corpus" / "ds101" / "coursework-brief" / "v2.html"
    text_, spans = _extracted(path)
    boundary = spans[9]["end_offset"]
    crossing = text_[boundary - 20 : spans[10]["start_offset"] + 20]
    _edit_case(copy, CRITICAL, lambda c: _target(c).update(quote=crossing))

    problems = await _resolution_problems(session_factory, copy)

    assert any("not inside exactly one SourceSpan" in p for p in problems)


@pytest.mark.anyio
async def test_a_changed_production_chunk_count_is_rejected(
    session_factory: async_sessionmaker[AsyncSession], copy: Path
) -> None:
    _edit_manifest(copy, lambda m: m["expected_chunks"].update(current_versions=40))

    problems = await _resolution_problems(session_factory, copy)

    assert any("produced 39 chunks in current versions" in p for p in problems)
