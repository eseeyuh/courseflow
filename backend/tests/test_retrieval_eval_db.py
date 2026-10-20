# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""The evaluation database: destruction guards, and the real stale-version
lifecycle seeded with replayed vectors (from a snapshot recorded with a fake
provider). Nothing is searched and nothing is scored."""

from __future__ import annotations

import ast
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path

import anyio
import pytest
from sqlalchemy import URL, select
from sqlalchemy.ext.asyncio import create_async_engine

from app.ai.embeddings import (
    DOCUMENT_INPUT_FORMAT,
    EmbeddingInput,
    EmbeddingResult,
    EmbeddingSpace,
)
from app.db.models import ModelCall
from app.db.session import create_session_factory
from app.evaluation.retrieval import eval_db
from app.evaluation.retrieval.dataset import Dataset, load_dataset
from app.evaluation.retrieval.eval_db import (
    EVAL_DATABASE_ENV,
    EvalDatabase,
    EvalDatabaseRefused,
    prepare_eval_database,
    recreate_eval_database,
    require_eval_database,
)
from app.evaluation.retrieval.lifecycle import (
    VersionState,
    corpus_state,
    expected_state_problems,
    seed_and_index,
)
from app.evaluation.retrieval.record import plan_recording, record_snapshot
from app.evaluation.retrieval.replay import (
    REPLAY_REQUEST_ID_PREFIX,
    ReplayEmbeddingProvider,
    load_replay_provider,
)
from app.evaluation.retrieval.snapshot import ExpectedInputs, expected_inputs, input_key
from app.ingestion.storage import LocalRawObjectStore
from tests.test_retrieval_snapshot import CLOCK, DATASET_DIR, SPACE, FakeProvider

pytestmark = pytest.mark.anyio

APP_URL = "postgresql+asyncpg://courseflow:secret@127.0.0.1:5432/courseflow"
EVAL_URL = "postgresql+asyncpg://courseflow:secret@127.0.0.1:5432/courseflow_eval"
STALE = ("ds101", "coursework-brief", 1)
CURRENT = ("ds101", "coursework-brief", 2)


def guard(eval_url: str | None, app_url: str = APP_URL) -> EvalDatabase:
    environ = {} if eval_url is None else {EVAL_DATABASE_ENV: eval_url}
    return require_eval_database(environ, application_database_url=app_url)


# --- guards -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "courseflow",
        "courseflow_test",
        "postgres",
        "template0",
        "template1",
        "courseflow_eval_test",
        "courseflow_eval2",
        "Courseflow_eval",
        "courseflow-eval",
        "",
    ],
)
def test_any_database_but_courseflow_eval_is_refused(name: str) -> None:
    with pytest.raises(EvalDatabaseRefused, match="must be named"):
        guard(f"postgresql+asyncpg://courseflow:secret@127.0.0.1:5432/{name}")


@pytest.mark.parametrize(
    ("url", "message"),
    [
        (None, "is not set"),
        ("   ", "is not set"),
        ("not a url", "not a database URL"),
        ("sqlite+aiosqlite:///courseflow_eval", "must be PostgreSQL"),
        ("postgresql+asyncpg://u:p@db.example.com/courseflow_eval", "is not loopback"),
        ("postgresql+asyncpg://u:p@10.0.0.5/courseflow_eval", "is not loopback"),
        ("postgresql+asyncpg://u:p@dbserver/courseflow_eval", "is not loopback"),
        ("postgresql+asyncpg:///courseflow_eval", "is not loopback"),
        # Driver-applied query parameters override the checked URL fields.
        ("postgresql+asyncpg://u:p@127.0.0.1/courseflow_eval?host=prod.example.com", "no query"),
        ("postgresql+asyncpg://u:p@127.0.0.1/courseflow_eval?database=courseflow", "no query"),
        ("postgresql+asyncpg://u:p@127.0.0.1/courseflow_eval?port=6543", "no query"),
        ("postgresql+asyncpg://u:p@127.0.0.1/courseflow_eval?ssl=disable", "no query"),
    ],
)
def test_unsafe_evaluation_urls_are_refused(url: str | None, message: str) -> None:
    with pytest.raises(EvalDatabaseRefused, match=message):
        guard(url)


@pytest.mark.parametrize(
    "app_url",
    [
        EVAL_URL,
        "postgresql://courseflow:other@localhost/courseflow_eval",  # same after normalising
        "postgresql+psycopg://courseflow@[::1]:5432/courseflow_eval",
    ],
)
def test_the_application_database_is_refused_even_when_named_courseflow_eval(app_url: str) -> None:
    with pytest.raises(EvalDatabaseRefused, match="application's DATABASE_URL"):
        guard(EVAL_URL, app_url)


@pytest.mark.parametrize(
    "url",
    [
        EVAL_URL,
        "postgresql+asyncpg://courseflow:secret@localhost/courseflow_eval",
        "postgresql+asyncpg://courseflow:secret@db:5432/courseflow_eval",
        "postgresql+asyncpg://courseflow:secret@[::1]/courseflow_eval",
    ],
)
def test_a_local_courseflow_eval_database_is_accepted(url: str) -> None:
    assert guard(url).url.database == "courseflow_eval"


def test_a_guarded_url_never_carries_overriding_query_parameters() -> None:
    database = guard(EVAL_URL)
    assert not database.url.query
    tampered = EvalDatabase.__new__(EvalDatabase)
    object.__setattr__(tampered, "url", database.url.update_query_dict({"host": "x"}))
    object.__setattr__(tampered, "_token", database._token)
    with pytest.raises(EvalDatabaseRefused, match="not a guarded"):
        recreate_eval_database(tampered)


def test_destructive_helpers_accept_only_a_guarded_database() -> None:
    with pytest.raises(EvalDatabaseRefused, match="only built by"):
        EvalDatabase(url=URL.create("postgresql", database="courseflow_eval"), _token=object())
    for target in [URL.create("postgresql+asyncpg", database="courseflow_test"), APP_URL]:
        with pytest.raises(EvalDatabaseRefused, match="not a guarded"):
            recreate_eval_database(target)  # type: ignore[arg-type]


async def test_the_normal_test_database_cannot_be_destroyed(test_database_url: URL) -> None:
    rendered = test_database_url.render_as_string(hide_password=False)
    with pytest.raises(EvalDatabaseRefused):
        guard(rendered, rendered)  # the test database as the evaluation database
    with pytest.raises(EvalDatabaseRefused):
        guard(test_database_url.set(database="courseflow").render_as_string(hide_password=False))
    engine = create_async_engine(test_database_url)  # still there, untouched
    try:
        async with engine.connect() as connection:
            assert await connection.scalar(select(1)) == 1
    finally:
        await engine.dispose()


def _imports(path: Path) -> list[str]:
    names: list[str] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom):
            names.append(node.module or "")
        elif isinstance(node, ast.Import):
            names.extend(a.name for a in node.names)
    return names


RUNTIME = ("api", "core", "db", "domain", "ai", "ingestion", "retrieval", "connectors")


def test_the_destructive_helpers_stay_in_evaluation_tooling() -> None:
    """Only evaluation tooling may import the evaluation database helpers, and
    the runtime path (the API and the packages it uses) imports no evaluation
    or demo tooling at all."""
    app_dir = Path(eval_db.__file__).resolve().parents[2]
    offenders = []
    for path in app_dir.rglob("*.py"):
        parts = path.relative_to(app_dir).parts
        names = _imports(path)
        if parts[0] != "evaluation" and any(n.endswith("eval_db") for n in names):
            offenders.append(f"{path.name} imports eval_db")
        runtime = parts[0] in RUNTIME or parts == ("main.py",)
        if runtime and any(n.startswith(("app.evaluation", "app.demo")) for n in names):
            offenders.append(f"{'/'.join(parts)} imports tooling")
    assert offenders == []


# --- the real stale-version lifecycle -----------------------------------------------------


class SpyProvider:
    """Forwards to the replay provider, keeping every input production sends."""

    def __init__(self, inner: ReplayEmbeddingProvider) -> None:
        self.inner = inner
        self.sent: list[EmbeddingInput] = []

    @property
    def space(self) -> EmbeddingSpace:
        return self.inner.space

    @property
    def max_batch_items(self) -> int:
        return self.inner.max_batch_items

    async def embed(self, inputs: Sequence[EmbeddingInput]) -> EmbeddingResult:
        self.sent.extend(inputs)
        return await self.inner.embed(inputs)

    async def aclose(self) -> None:
        await self.inner.aclose()


@pytest.fixture(scope="module")
def dataset() -> Dataset:
    return load_dataset(DATASET_DIR)


@pytest.fixture(scope="module")
def inputs(dataset: Dataset) -> ExpectedInputs:
    return expected_inputs(dataset)


async def test_the_stale_version_is_indexed_while_current_then_superseded(
    test_database_url: URL, dataset: Dataset, inputs: ExpectedInputs, tmp_path: Path
) -> None:
    database = guard(
        test_database_url.set(database="courseflow_eval").render_as_string(hide_password=False),
        test_database_url.render_as_string(hide_password=False),
    )
    head = await anyio.to_thread.run_sync(prepare_eval_database, database)
    assert head

    snapshot = tmp_path / "snapshot.jsonl"
    plan = plan_recording(
        dataset,
        SPACE,
        batch_max_items=64,
        batch_max_estimated_tokens=32_000,
        attempts_per_request=3,
        price_usd_per_million=Decimal("0.01"),
        output=snapshot,
        inputs=inputs,
    )
    await record_snapshot(plan, FakeProvider(), now=lambda: CLOCK)
    replay = load_replay_provider(
        snapshot, dataset=dataset, active_space=SPACE, max_batch_items=64, inputs=inputs
    )
    spy = SpyProvider(replay)
    raw_root = tmp_path / "raw"
    raw_root.mkdir()

    engine = create_async_engine(database.url)
    try:
        sessions = create_session_factory(engine)
        seeded = await seed_and_index(
            sessions,
            LocalRawObjectStore(raw_root),
            dataset,
            spy,
            batch_max_items=64,
            batch_max_estimated_tokens=32_000,
        )

        # A-C: v1 was ingested, indexed while current, every chunk linked.
        first, second = seeded.rounds
        assert first.round == 1 and len(first.ingested) == 13 and STALE in first.ingested
        assert first.to_be_superseded == (VersionState(STALE, True, 2, 2),)
        # D-E: v2 was ingested in its own round, then the corpus re-indexed.
        assert second.ingested == (CURRENT,)
        assert all(not run.failures for run in first.index_runs + second.index_runs)

        async with sessions() as session:
            state = await corpus_state(session, seeded, SPACE)
            calls = (await session.scalars(select(ModelCall))).all()

        assert expected_state_problems(state, dataset, inputs) == []
        assert (state.courses, state.resources, state.resource_versions) == (2, 13, 14)
        assert (state.chunks, state.current_version_chunks) == (41, 39)
        assert (state.document_embeddings, state.other_embeddings) == (41, 0)  # no query vectors
        by_key = {v.key: v for v in state.versions}
        # F-G: the stale chunks and their links survive; only currency changed.
        assert by_key[STALE] == VersionState(STALE, current=False, chunks=2, linked_chunks=2)
        assert by_key[CURRENT].current and by_key[CURRENT].fully_linked
        assert sum(v.current for v in state.versions) == 13

        # Production indexing sent exactly the snapshot's document inputs, byte for byte.
        assert {item.input_format for item in spy.sent} == {DOCUMENT_INPUT_FORMAT}
        assert {input_key(item) for item in spy.sent} == {input_key(i) for i in inputs.documents}
        assert {item.text for item in spy.sent} == {i.text for i in inputs.documents}

        # Replayed calls are marked as replay and claim no paid tokens.
        assert len(calls) == replay.calls > 0
        assert all(
            c.provider_request_id == f"{REPLAY_REQUEST_ID_PREFIX}{replay.snapshot_sha256}"
            for c in calls
        )
        assert all(c.prompt_tokens is None for c in calls)
        live_provider_calls = sum(
            not (c.provider_request_id or "").startswith(REPLAY_REQUEST_ID_PREFIX) for c in calls
        )
        assert live_provider_calls == 0
    finally:
        await engine.dispose()


async def test_the_expected_state_check_reports_a_stale_version_without_links(
    dataset: Dataset, inputs: ExpectedInputs
) -> None:
    from app.evaluation.retrieval.lifecycle import CorpusState

    healthy = [
        VersionState(
            (c.key, r.key, v.version),
            current=v.version == r.latest,
            chunks=2,
            linked_chunks=2,
        )
        for c in dataset.manifest.courses
        for r in c.resources
        for v in r.versions
    ]
    unindexed = [
        VersionState(s.key, s.current, s.chunks, 0) if s.key == STALE else s for s in healthy
    ]
    state = CorpusState(
        courses=2,
        resources=13,
        resource_versions=14,
        chunks=41,
        current_version_chunks=39,
        document_embeddings=41,
        other_embeddings=0,
        document_input_hashes=frozenset(i.input_hash for i in inputs.documents),
        versions=tuple(unindexed),
    )
    assert expected_state_problems(state, dataset, inputs) == [
        "ds101/coursework-brief/1: 0/2 chunks have embedding links"
    ]


# --- the evaluation on the committed snapshot ---------------------------------------------

SNAPSHOT = (
    DATASET_DIR.parents[2]
    / "snapshots"
    / "retrieval"
    / "v1"
    / "nebius-qwen3-embedding-8b-d4096-sv1.jsonl"
)
SNAPSHOT_SHA = "56509e7d364dbd403a6932ea98272a5334cc0f0a2bdb0d468645632429371941"


def test_the_replayed_evaluation_keeps_the_critical_case_and_every_invariant(
    test_database_url: URL, dataset: Dataset, tmp_path: Path
) -> None:
    """The critical regression: in the selected default mode the current CW1
    deadline must stay in the top 3, no mode may leak a stale or foreign
    version, and the metrics must not change between two fresh runs."""
    from app.evaluation.retrieval.runner import (
        RunInputs,
        failures,
        metric_view,
        run_once,
        write_reports,
    )

    run = RunInputs(
        dataset=dataset,
        snapshot=SNAPSHOT,
        snapshot_sha256=SNAPSHOT_SHA,
        database=guard(
            test_database_url.set(database="courseflow_eval").render_as_string(hide_password=False),
            test_database_url.render_as_string(hide_password=False),
        ),
        space=SPACE,
        batch_max_items=64,
        batch_max_estimated_tokens=32_000,
    )
    first, second = run_once(run), run_once(run)  # two independently seeded fresh databases
    # Identical complete ordered hit lists, ranks, metrics and selection.
    assert first == second
    assert metric_view(first) == metric_view(second)
    for mode in first["modes"].values():
        assert all(case["hits"] for case in mode["cases"])
    meta = {"repeat": 2, "identical": True, "metrics_identical": True,
            "finished_at": "t", "started_at": "t"}  # fmt: skip
    one, two = tmp_path / "one", tmp_path / "two"
    paths = write_reports(first, meta, [one]) + write_reports(second, meta, [two])
    assert [p.read_bytes() for p in paths[:2]] == [p.read_bytes() for p in paths[2:]]
    assert failures(first) == []
    assert first["provider_calls"]["live"] == 0
    selected = first["selection"]["selected"]
    critical = next(c for c in first["modes"][selected]["cases"] if c["critical"])
    assert critical["case_id"] == dataset.manifest.critical_case.case_id
    assert critical["first_relevant_rank"] is not None and critical["first_relevant_rank"] <= 3
    for data in first["modes"].values():
        assert data["metrics"]["positive_cases"] == 12  # invariant cases excluded

    text = b"".join(p.read_bytes() for p in paths)
    for case in dataset.cases:  # no query or target text in any report
        assert case.query.encode() not in text
        for group in case.target_groups:
            assert all(alt.quote.encode() not in text for alt in group.alternatives)
