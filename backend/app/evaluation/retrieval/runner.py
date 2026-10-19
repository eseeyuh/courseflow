# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Evaluate vector, lexical and hybrid retrieval on a frozen dataset, offline.

    python -m app.evaluation.retrieval.runner --dataset <dir> --snapshot <file.jsonl> \\
        --expect-dataset-sha <sha256> --expect-snapshot-sha <sha256> [--repeat 2]

Each run, from zero:

1. load the frozen dataset and the replay snapshot; both SHA-256s must equal
   the expected ones;
2. recreate and migrate the guarded evaluation database
   (``COURSEFLOW_EVAL_DATABASE_URL``, never the application database);
3. seed it through the production lifecycle with replayed vectors and check
   the exact expected state (stale versions indexed, then superseded);
4. resolve every target, then run every case in every mode through the
   production ``SearchService`` (top ``SEARCH_K``), queries embedded by replay;
5. score (``metrics``), check hard invariants, select the default
   (``selection``), and count live provider calls, which must be 0.

``--repeat n`` runs the whole evaluation n times, each on a freshly seeded
database, and requires byte-identical ``results``: every ordered hit list,
rank, metric and the selection (only ``run`` metadata such as timestamps may
differ). Reports (JSON and Markdown) go to a timestamped directory under
``evals/runs/`` (ignored by git) and, as the latest curated copy, to
``evals/reports/``. No chunk or source text is written anywhere.

Exit codes: 0 ok; 1 unexpected error; 3 invalid input; 6 evaluation failed
(invariant failure, no eligible default, or the critical rule not met);
7 runs were not identical; 64 usage.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from app.ai.embeddings import EmbeddingSpace
from app.core.config import Settings
from app.db.models import ModelCall
from app.db.session import create_session_factory
from app.evaluation.retrieval.dataset import Dataset, DatasetError, load_dataset
from app.evaluation.retrieval.eval_db import (
    EvalDatabase,
    EvalDatabaseRefused,
    prepare_eval_database,
    require_eval_database,
)
from app.evaluation.retrieval.lifecycle import corpus_state, expected_state_problems, seed_and_index
from app.evaluation.retrieval.metrics import (
    MRR_K,
    RECALL_KS,
    SEARCH_K,
    CaseResult,
    Hit,
    ModeMetrics,
    mode_metrics,
    score_case,
)
from app.evaluation.retrieval.replay import REPLAY_REQUEST_ID_PREFIX, load_replay_provider
from app.evaluation.retrieval.resolve import resolve_dataset
from app.evaluation.retrieval.selection import (
    MODES,
    SELECTION_RULE,
    Selection,
    select_default,
)
from app.evaluation.retrieval.snapshot import SnapshotError, expected_inputs
from app.ingestion.storage import LocalRawObjectStore
from app.retrieval.search import SearchMode, SearchRequest, SearchService

RUNNER_VERSION = "retrieval-eval/v1"
REPO_ROOT = Path(__file__).resolve().parents[4]

EXIT_OK = 0
EXIT_UNEXPECTED = 1
EXIT_INVALID = 3
EXIT_FAILED = 6
EXIT_NOT_DETERMINISTIC = 7
EXIT_USAGE = 64


class EvaluationError(RuntimeError):
    """The evaluation could not run on the state it requires."""


@dataclass(frozen=True, slots=True)
class RunInputs:
    dataset: Dataset
    snapshot: Path
    snapshot_sha256: str
    database: EvalDatabase
    space: EmbeddingSpace
    batch_max_items: int
    batch_max_estimated_tokens: int


def _fraction(value: Fraction) -> dict[str, Any]:
    return {"exact": f"{value.numerator}/{value.denominator}", "value": f"{float(value):.6f}"}


def _metrics_json(metrics: ModeMetrics) -> dict[str, Any]:
    return {
        "positive_cases": metrics.positive_cases,
        **{
            f"recall@{k}": {"hits": metrics.hits_at[k], **_fraction(metrics.recall(k))}
            for k in RECALL_KS
        },
        f"mrr@{MRR_K}": {
            "reciprocal_rank_sum": _fraction(metrics.reciprocal_rank_sum)["exact"],
            **_fraction(metrics.mrr),
        },
    }


def _case_json(result: CaseResult) -> dict[str, Any]:
    return {
        "case_id": result.case_id,
        "category": result.category,
        "positive": result.positive,
        "critical": result.critical,
        "first_relevant_rank": result.first_relevant_rank,
        "first_relevant_tie": list(result.first_relevant_tie or []) or None,
        "hits": list(result.hits),
        "distractors": [
            {"kind": d.kind, "ref": d.ref, "first_rank": d.first_rank} for d in result.distractors
        ],
        "invariant_failures": list(result.invariant_failures),
    }


def _selection_json(selection: Selection) -> dict[str, Any]:
    return {
        "rule": selection.rule,
        "assessments": [
            {
                "mode": a.mode,
                "passes": a.passes,
                "invariant_failures": a.invariant_failures,
                "critical_rank": a.critical_rank,
                "recall@5": _fraction(a.recall_at_5)["exact"],
                "mrr@10": _fraction(a.mrr_at_10)["exact"],
            }
            for a in selection.assessments
        ],
        "preferred_single": selection.preferred_single,
        "lexical_replaces_vector": selection.lexical_replaces_vector,
        "hybrid_replaces_single": selection.hybrid_replaces_single,
        "selected": selection.selected,
        "reason": selection.reason,
    }


def _ordering_score(mode: str, hit: Any) -> object:
    """The part of each mode's order that is not a row-ID tie-break: hits equal
    on it are ordered by resource version ID, which differs between seeds."""
    if mode == "vector":
        return hit.vector_distance
    if mode == "lexical":
        return hit.lexical_score
    return (hit.fused_score, hit.vector_rank, hit.lexical_rank)


def metric_view(results: dict[str, Any]) -> dict[str, Any]:
    """What the metrics depend on: aggregates, each case's first relevant
    rank, invariant failures and the selection (not the full hit lists)."""
    return {
        "modes": {
            mode: {
                "metrics": data["metrics"],
                "invariant_failures": data["invariant_failures"],
                "first_relevant_ranks": [c["first_relevant_rank"] for c in data["cases"]],
            }
            for mode, data in results["modes"].items()
        },
        "selection": results["selection"],
    }


async def _evaluate(run: RunInputs, migration_head: str) -> dict[str, Any]:
    dataset = run.dataset
    inputs = expected_inputs(dataset)
    replay = load_replay_provider(
        run.snapshot,
        dataset=dataset,
        active_space=run.space,
        max_batch_items=run.batch_max_items,
        inputs=inputs,
    )
    if replay.snapshot_sha256 != run.snapshot_sha256:
        raise EvaluationError("the snapshot SHA-256 is not the expected one")
    engine = create_async_engine(run.database.url)
    try:
        sessions = create_session_factory(engine)
        with tempfile.TemporaryDirectory(prefix="courseflow-eval-raw-") as raw:
            seeded = await seed_and_index(
                sessions,
                LocalRawObjectStore(Path(raw).resolve()),
                dataset,
                replay,
                batch_max_items=run.batch_max_items,
                batch_max_estimated_tokens=run.batch_max_estimated_tokens,
            )
            async with sessions() as session:
                state = await corpus_state(session, seeded, run.space)
                problems = expected_state_problems(state, dataset, inputs)
                if problems:
                    raise EvaluationError("; ".join(problems))
                resolved = await resolve_dataset(session, dataset, seeded.versions)

            by_version = {v.version_id: v.key for v in seeded.versions.values()}
            current = {v.key: v.current for v in state.versions}
            courses = {v.key[0]: v.course_id for v in seeded.versions.values()}
            service = SearchService(sessions, run.space, replay)
            modes: dict[str, Any] = {}
            results: dict[str, list[CaseResult]] = {}
            metrics: dict[str, ModeMetrics] = {}
            components: dict[str, Any] = {}
            for mode in MODES:
                case_results = []
                for case, resolved_case in zip(dataset.cases, resolved.cases, strict=True):
                    request = SearchRequest.build(
                        course_id=courses[case.course],
                        query=case.query,
                        k=SEARCH_K,
                        resource_types=(
                            frozenset(case.resource_types) if case.resource_types else None
                        ),
                        mode=SearchMode(mode),
                    )
                    result = await service.search_course(request)
                    components[mode] = {
                        "score_kind": result.score_kind,
                        "query_format": result.query_format,
                        "document_format": result.document_format,
                        "lexical_version": result.lexical_version,
                        "fusion_version": result.fusion_version,
                    }
                    hits = []
                    for hit in result.hits:
                        key = by_version.get(hit.version.id)
                        hits.append(
                            Hit(
                                rank=hit.rank,
                                version=key,
                                current=bool(key and current.get(key)),
                                start=hit.start_offset,
                                end=hit.end_offset,
                                ordinal=hit.chunk_ordinal,
                                score=_ordering_score(mode, hit),
                            )
                        )
                    case_results.append(score_case(case, resolved_case, hits))
                results[mode] = case_results
                metrics[mode] = mode_metrics(case_results)
                modes[mode] = {
                    "components": components[mode],
                    "metrics": _metrics_json(metrics[mode]),
                    "invariant_failures": sum(len(r.invariant_failures) for r in case_results),
                    "cases": [_case_json(r) for r in case_results],
                }
            async with sessions() as session:
                calls = (await session.scalars(select(ModelCall))).all()
    finally:
        await engine.dispose()

    live = sum(
        not (c.provider_request_id or "").startswith(REPLAY_REQUEST_ID_PREFIX) for c in calls
    )
    selection = select_default(metrics, results)
    manifest = dataset.manifest
    return {
        "runner": RUNNER_VERSION,
        "dataset": {
            "name": manifest.dataset,
            "version": manifest.dataset_version,
            "sha256": dataset.sha256,
            "relevance_rule": manifest.relevance_rule,
            "target_span_rule": manifest.target_span_rule,
        },
        "snapshot": {"sha256": replay.snapshot_sha256, "file": run.snapshot.name},
        "embedding_space": {
            "provider": run.space.provider,
            "model": run.space.model,
            "dimensions": run.space.dimensions,
            "space_version": run.space.space_version,
        },
        "chunker": manifest.expected_chunker,
        "migration_head": migration_head,
        "search_k": SEARCH_K,
        "corpus_state": {
            "courses": state.courses,
            "resources": state.resources,
            "resource_versions": state.resource_versions,
            "chunks": state.chunks,
            "current_version_chunks": state.current_version_chunks,
            "document_embeddings": state.document_embeddings,
        },
        "cases": {
            "total": len(dataset.cases),
            "positive": manifest.positive_case_count,
            "invariant_only": manifest.invariant_case_count,
            "critical": manifest.critical_case.case_id,
        },
        "modes": modes,
        "selection": _selection_json(selection),
        "provider_calls": {
            "live": live,
            "replayed_model_calls": len(calls) - live,
            "replayed_batches": replay.calls,
        },
    }


def run_once(run: RunInputs) -> dict[str, Any]:
    head = prepare_eval_database(run.database)  # outside any event loop
    return asyncio.run(_evaluate(run, head))


def failures(results: dict[str, Any]) -> list[str]:
    problems = []
    for mode, data in results["modes"].items():
        if data["invariant_failures"]:
            problems.append(f"{mode}: {data['invariant_failures']} hard-invariant failure(s)")
    if results["selection"]["selected"] is None:
        problems.append("no eligible default mode")
    if results["provider_calls"]["live"]:
        problems.append("live provider calls were made")
    return problems


# --- reports ------------------------------------------------------------------------------


def markdown(results: dict[str, Any], run: dict[str, Any]) -> str:
    modes = results["modes"]
    lines = [
        "# Retrieval evaluation",
        "",
        (
            f"- Dataset: `{results['dataset']['name']}` {results['dataset']['version']}, "
            f"SHA-256 `{results['dataset']['sha256']}`"
        ),
        f"- Replay snapshot SHA-256: `{results['snapshot']['sha256']}`",
        (
            f"- Embedding space: `{results['embedding_space']['model']}`, "
            f"{results['embedding_space']['dimensions']} dimensions, space "
            f"`{results['embedding_space']['space_version']}`; chunker `{results['chunker']}`"
        ),
        f"- Relevance: `{results['dataset']['relevance_rule']}`; top {results['search_k']} per case",
        (
            f"- Cases: {results['cases']['total']} ({results['cases']['positive']} positive, "
            f"{results['cases']['invariant_only']} invariant-only, excluded from denominators)"
        ),
        f"- Live provider calls: {results['provider_calls']['live']}",
        (
            f"- Runs: {run['repeat']}; identical metrics: {run['metrics_identical']}; "
            f"identical hit lists: {run['identical']}"
        ),
        "",
        "## Metrics",
        "",
        "| Mode | Recall@1 | Recall@3 | Recall@5 | MRR@10 | Invariant failures |",
        "|---|---|---|---|---|---|",
    ]
    for mode in MODES:
        m = modes[mode]["metrics"]
        cells = [f"{m[f'recall@{k}']['hits']}/{m['positive_cases']}" for k in RECALL_KS]
        lines.append(
            f"| {mode} | {' | '.join(cells)} | {m['mrr@10']['value']} "
            f"({m['mrr@10']['exact']}) | {modes[mode]['invariant_failures']} |"
        )
    sel = results["selection"]
    lines += [
        "",
        "## Default selection",
        "",
        (
            f"Rule `{sel['rule']}` (fixed before any score). A mode passes with no invariant "
            "failure and the critical case in the top 3. A: lexical replaces vector iff it "
            "passes with Recall@5 and MRR@10 both >= vector's (a tie on both prefers lexical). "
            "B: hybrid replaces that single retriever iff it passes, is >= on both and strictly "
            "better on at least one."
        ),
        "",
        "| Mode | Passes | Critical rank | Recall@5 | MRR@10 |",
        "|---|---|---|---|---|",
    ]
    for a in sel["assessments"]:
        lines.append(
            f"| {a['mode']} | {a['passes']} | {a['critical_rank']} | {a['recall@5']} "
            f"| {a['mrr@10']} |"
        )
    lines += ["", f"**Selected: {sel['selected']}.** {sel['reason']}.", "", "## Per case", ""]
    lines += [
        (
            "First relevant rank per mode (`-`: not in the top 10). Distractors are listed "
            "when one ranks above the first relevant hit, or anywhere for invariant cases."
        ),
        "",
        "| Case | Category | " + " | ".join(MODES) + " | Distractors above target |",
        "|---|---|" + "---|" * len(MODES) + "---|",
    ]
    for index, case in enumerate(modes["vector"]["cases"]):
        ranks, notes = [], []
        for mode in MODES:
            c = modes[mode]["cases"][index]
            rank = c["first_relevant_rank"]
            ranks.append("n/a" if not c["positive"] else str(rank) if rank else "-")
            for d in c["distractors"]:
                limit = rank if rank else results["search_k"] + 1
                if d["first_rank"] is not None and (not c["positive"] or d["first_rank"] < limit):
                    notes.append(f"{mode}: {d['kind']} @{d['first_rank']}")
            for failure in c["invariant_failures"]:
                notes.append(f"{mode}: INVARIANT {failure}")
        name = case["case_id"] + (" (critical)" if case["critical"] else "")
        lines.append(
            f"| {name} | {case['category']} | {' | '.join(ranks)} | {'; '.join(notes) or ''} |"
        )
    lines += ["", f"Generated {run['finished_at']} by `{results['runner']}`.", ""]
    return "\n".join(lines)


def write_reports(
    results: dict[str, Any], run: dict[str, Any], directories: Sequence[Path]
) -> list[Path]:
    payload = {"results": results, "run": run}
    data = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    text = markdown(results, run)
    written = []
    for directory in directories:
        directory.mkdir(parents=True, exist_ok=True)
        for name, content in (("retrieval-v1.json", data), ("retrieval-v1.md", text)):
            path = directory / name
            path.write_bytes(content.encode("utf-8"))
            written.append(path)
    return written


# --- command line -------------------------------------------------------------------------


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> Any:  # argparse would exit with 2
        _emit({"status": "error", "error": "usage", "detail": message})
        raise SystemExit(EXIT_USAGE)


def _parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(prog="python -m app.evaluation.retrieval.runner")
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--expect-dataset-sha", required=True)
    parser.add_argument("--expect-snapshot-sha", required=True)
    parser.add_argument("--repeat", type=int, default=2, choices=range(1, 6))
    parser.add_argument("--runs-dir", type=Path, default=REPO_ROOT / "evals" / "runs" / "retrieval")
    parser.add_argument("--reports-dir", type=Path, default=REPO_ROOT / "evals" / "reports")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    from app.ai.nebius_embeddings import embedding_space_from_settings
    from app.core.config import get_settings

    args = _parser().parse_args(argv)
    started = datetime.now(UTC)
    try:
        settings: Settings = get_settings()
        dataset = load_dataset(args.dataset.resolve())
        if dataset.sha256 != args.expect_dataset_sha:
            raise EvaluationError(f"dataset sha256 {dataset.sha256} is not the expected one")
        run = RunInputs(
            dataset=dataset,
            snapshot=args.snapshot.resolve(),
            snapshot_sha256=args.expect_snapshot_sha,
            database=require_eval_database(
                os.environ,
                application_database_url=settings.database_url.get_secret_value(),
            ),
            space=embedding_space_from_settings(settings),
            batch_max_items=settings.embedding_batch_max_items,
            batch_max_estimated_tokens=settings.embedding_batch_max_estimated_tokens,
        )
        outputs = [run_once(run) for _ in range(args.repeat)]
        identical = len({json.dumps(o, sort_keys=True) for o in outputs}) == 1
        metrics_identical = len({json.dumps(metric_view(o), sort_keys=True) for o in outputs}) == 1
        finished = datetime.now(UTC)
        meta = {
            "started_at": started.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "finished_at": finished.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "repeat": args.repeat,
            "identical": identical,
            "metrics_identical": metrics_identical,
            "selection_rule": SELECTION_RULE,
        }
        stamp = started.strftime("%Y%m%dT%H%M%SZ")
        written = write_reports(outputs[0], meta, [args.runs_dir / stamp, args.reports_dir])
    except (DatasetError, SnapshotError) as exc:
        _emit({"status": "error", "error": "invalid", "problems": list(exc.problems)})
        return EXIT_INVALID
    except (EvaluationError, EvalDatabaseRefused) as exc:
        _emit({"status": "error", "error": "invalid", "detail": str(exc)})
        return EXIT_INVALID
    except Exception as exc:  # noqa: BLE001 - report the type only
        _emit({"status": "error", "error": "unexpected", "type": type(exc).__name__})
        return EXIT_UNEXPECTED
    problems = failures(outputs[0])
    summary = {
        "status": "ok" if identical and not problems else "failed",
        "identical": identical,
        "metrics_identical": metrics_identical,
        "problems": problems,
        "selected": outputs[0]["selection"]["selected"],
        "reports": [p.as_posix() for p in written],
    }
    _emit(summary)
    if not identical:
        return EXIT_NOT_DETERMINISTIC
    return EXIT_FAILED if problems else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
