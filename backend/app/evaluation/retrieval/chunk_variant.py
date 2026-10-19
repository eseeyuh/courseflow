# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""One chunk-size variant experiment on retrieval benchmark v1 (``chunk-variant/v1``).

Frozen before any score: ``SPAN_PACK_EVAL_SMALL_V1`` is the production
span-pack algorithm with smaller sizing parameters only. Production
``span-pack/v1`` is unchanged and remains the only production chunker.

Different chunk boundaries mean different ``document/v1`` inputs, which the
committed Qwen snapshot (recorded for span-pack/v1 chunks) does not hold.
No live call is made and no vector is invented for a real embedding space,
so vector and hybrid retrieval cannot be compared here without a separate,
approved recording. What can be compared honestly without real vectors:

- **target containment**: for each positive case, whether any current
  chunk wholly contains a labelled target (an upper bound on Recall@k for
  any ranker over that chunking);
- **lexical retrieval**, which never reads vectors. Indexing still requires
  every chunk to be linked to a vector, so chunks are linked to
  ``PlaceholderEmbeddingProvider`` vectors, in a placeholder space that no
  real space can be mistaken for. As a control, the production config's
  lexical metrics from this path must equal the committed evaluation's.

Each config runs on a freshly recreated guarded evaluation database through
the production ingestion, indexing (stale version indexed, then superseded)
and search code.

    python -m app.evaluation.retrieval.chunk_variant --dataset <dir> \\
        --expect-dataset-sha <sha256> --report <evals/reports/retrieval-v1.json>
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import struct
import sys
import tempfile
from collections.abc import Sequence
from fractions import Fraction
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from app.ai.embeddings import (
    EmbeddingInput,
    EmbeddingResult,
    EmbeddingSpace,
    check_batch,
    document_input,
)
from app.ai.provider import TokenUsage
from app.db.models import Chunk
from app.db.session import create_session_factory
from app.evaluation.retrieval.dataset import Dataset, load_dataset
from app.evaluation.retrieval.eval_db import (
    EvalDatabase,
    prepare_eval_database,
    require_eval_database,
)
from app.evaluation.retrieval.lifecycle import seed_and_index, version_states
from app.evaluation.retrieval.metrics import SEARCH_K, Hit, mode_metrics, score_case
from app.evaluation.retrieval.resolve import resolve_dataset
from app.evaluation.retrieval.rules import contains
from app.evaluation.retrieval.snapshot import expected_inputs
from app.ingestion.storage import LocalRawObjectStore
from app.retrieval.chunking import SPAN_PACK_V1, ChunkingParams, estimate_tokens
from app.retrieval.search import SearchMode, SearchRequest, SearchService

EXPERIMENT = "chunk-variant/v1"

# The alternate configuration, frozen before any score: only the sizing
# parameters differ from span-pack/v1 (the cut-placement minimum and the
# chars-per-token estimate are production's).
SPAN_PACK_EVAL_SMALL_V1 = ChunkingParams(
    version="span-pack-eval-small/v1",
    target_estimated_tokens=600,
    hard_max_estimated_tokens=800,
    overlap_estimated_tokens=100,
    split_above_estimated_tokens=680,
    min_estimated_tokens=160,
    split_piece_min_estimated_tokens=SPAN_PACK_V1.split_piece_min_estimated_tokens,
    chars_per_estimated_token=SPAN_PACK_V1.chars_per_estimated_token,
)
CONFIGS = (SPAN_PACK_V1, SPAN_PACK_EVAL_SMALL_V1)
MODE = SearchMode.LEXICAL  # the only mode placeholder vectors cannot influence
PRICE_USD_PER_MILLION = Fraction(1, 100)  # catalog price verified 2026-10-19


class PlaceholderEmbeddingProvider:
    """Deterministic non-model vectors in a space of their own. They exist only
    so indexing can link every chunk; they mean nothing, and nothing that
    reads vectors may be scored with them."""

    space = EmbeddingSpace("placeholder", "sha256-unit-vector", 8, "v1")
    max_batch_items = 64

    def __init__(self) -> None:
        self.calls = 0

    async def embed(self, inputs: Sequence[EmbeddingInput]) -> EmbeddingResult:
        check_batch(inputs, self.max_batch_items)
        self.calls += 1
        return EmbeddingResult(
            vectors=tuple(_placeholder_vector(item.input_hash) for item in inputs),
            space=self.space,
            served_model=None,
            usage=TokenUsage(),
            attempts=1,
            request_id="placeholder",
        )

    async def aclose(self) -> None:
        return None


def _placeholder_vector(input_hash: str) -> tuple[float, ...]:
    raw = hashlib.sha256(input_hash.encode("ascii")).digest()
    return tuple(1.0 + b / 255 for b in struct.unpack("8B", raw[:8]))  # never all zero


def _fraction(value: Fraction) -> str:
    return f"{value.numerator}/{value.denominator}"


async def _run(database: EvalDatabase, dataset: Dataset, params: ChunkingParams) -> dict[str, Any]:
    provider = PlaceholderEmbeddingProvider()
    engine = create_async_engine(database.url)
    try:
        sessions = create_session_factory(engine)
        with tempfile.TemporaryDirectory(prefix="courseflow-chunk-variant-") as raw:
            seeded = await seed_and_index(
                sessions,
                LocalRawObjectStore(Path(raw).resolve()),
                dataset,
                provider,
                batch_max_items=64,
                batch_max_estimated_tokens=32_000,
                params=params,
            )
        async with sessions() as session:
            states = await version_states(
                session, list(seeded.versions.values()), provider.space, params
            )
            # Target offsets depend on the text only, not on any chunker.
            resolved = await resolve_dataset(session, dataset, seeded.versions)
            rows = (
                await session.execute(
                    select(
                        Chunk.resource_version_id,
                        Chunk.start_offset,
                        Chunk.end_offset,
                        Chunk.text,
                    ).where(Chunk.chunker_version == params.version)
                )
            ).all()
        lifecycle_ok = all(
            s.fully_linked and s.current == seeded.versions[s.key].current for s in states
        )

        by_id = {v.version_id: v for v in seeded.versions.values()}
        chunks = {(by_id[r[0]].key, r[1], r[2], r[3]) for r in rows}
        # Currency as the lifecycle left it (checked against the database above).
        current_chunks = [c for c in chunks if seeded.versions[c[0]].current]
        known = {item.input_hash for item in expected_inputs(dataset).documents}
        inputs = {document_input(c[3]).input_hash: c[3] for c in chunks}
        missing = [text for digest, text in inputs.items() if digest not in known]

        contained = 0
        for case, resolved_case in zip(dataset.cases, resolved.cases, strict=True):
            if case.is_positive and all(
                any(
                    c[0] == (t.ref.course, t.ref.resource, t.ref.version)
                    and contains(c[1], c[2], t.start, t.end)
                    for c in current_chunks
                    for t in group
                )
                for group in resolved_case.target_groups
            ):
                contained += 1

        service = SearchService(sessions, provider.space, None, params=params)
        courses = {v.key[0]: v.course_id for v in seeded.versions.values()}
        results = []
        for case, resolved_case in zip(dataset.cases, resolved.cases, strict=True):
            request = SearchRequest.build(
                course_id=courses[case.course],
                query=case.query,
                k=SEARCH_K,
                resource_types=frozenset(case.resource_types) if case.resource_types else None,
                mode=MODE,
            )
            result = await service.search_course(request)
            hits = [
                Hit(
                    rank=h.rank,
                    version=by_id[h.version.id].key if h.version.id in by_id else None,
                    current=h.version.id in by_id and by_id[h.version.id].current,
                    start=h.start_offset,
                    end=h.end_offset,
                    ordinal=h.chunk_ordinal,
                )
                for h in result.hits
            ]
            results.append(score_case(case, resolved_case, hits))
    finally:
        await engine.dispose()

    metrics = mode_metrics(results)
    missing_tokens = sum(estimate_tokens(text) for text in missing)
    return {
        "chunker": params.version,
        "parameters": {
            "target_estimated_tokens": params.target_estimated_tokens,
            "split_above_estimated_tokens": params.split_above_estimated_tokens,
            "hard_max_estimated_tokens": params.hard_max_estimated_tokens,
            "overlap_estimated_tokens": params.overlap_estimated_tokens,
            "min_estimated_tokens": params.min_estimated_tokens,
            "split_piece_min_estimated_tokens": params.split_piece_min_estimated_tokens,
        },
        "chunks": {"current_versions": len(current_chunks), "all_versions": len(chunks)},
        "stale_lifecycle_ok": lifecycle_ok,
        "document_inputs": {
            "unique": len(inputs),
            "in_committed_snapshot": len(inputs) - len(missing),
            "missing_from_snapshot": len(missing),
            "missing_estimated_tokens": missing_tokens,
            "missing_estimated_cost_usd": f"{float(missing_tokens * PRICE_USD_PER_MILLION / 1_000_000):.6f}",
        },
        "target_containment": {"cases": contained, "of": metrics.positive_cases},
        "lexical": {
            "recall@1": _fraction(metrics.recall(1)),
            "recall@3": _fraction(metrics.recall(3)),
            "recall@5": _fraction(metrics.recall(5)),
            "mrr@10": _fraction(metrics.mrr),
            "critical_rank": next(r.first_relevant_rank for r in results if r.critical),
            "invariant_failures": sum(len(r.invariant_failures) for r in results),
            "first_relevant_ranks": {
                r.case_id: r.first_relevant_rank for r in results if r.positive
            },
        },
    }


def run(database: EvalDatabase, dataset: Dataset) -> dict[str, Any]:
    outputs = {}
    for params in CONFIGS:
        prepare_eval_database(database)
        outputs[params.version] = asyncio.run(_run(database, dataset, params))
    return outputs


def markdown(result: dict[str, Any]) -> str:
    prod, variant = (result["configs"][p.version] for p in CONFIGS)
    lines = [
        "# Chunk-size variant experiment",
        "",
        (
            f"Experiment `{EXPERIMENT}` on retrieval benchmark v1 (dataset SHA-256 "
            f"`{result['dataset_sha256']}`). The variant was frozen before any score. "
            "Production `span-pack/v1` is unchanged."
        ),
        "",
        "| | span-pack/v1 (production) | span-pack-eval-small/v1 |",
        "|---|---|---|",
    ]
    for key in ("target_estimated_tokens", "split_above_estimated_tokens",
                "hard_max_estimated_tokens", "overlap_estimated_tokens", "min_estimated_tokens"):  # fmt: skip
        lines.append(f"| {key} | {prod['parameters'][key]} | {variant['parameters'][key]} |")
    rows = [
        ("Chunks (current / all versions)",
         lambda c: f"{c['chunks']['current_versions']} / {c['chunks']['all_versions']}"),
        ("Targets wholly inside some chunk",
         lambda c: f"{c['target_containment']['cases']}/{c['target_containment']['of']}"),
        ("Lexical Recall@1 / @3 / @5",
         lambda c: f"{c['lexical']['recall@1']} / {c['lexical']['recall@3']} / {c['lexical']['recall@5']}"),
        ("Lexical MRR@10", lambda c: c["lexical"]["mrr@10"]),
        ("Lexical critical-case rank", lambda c: str(c["lexical"]["critical_rank"])),
        ("Invariant failures", lambda c: str(c["lexical"]["invariant_failures"])),
        ("Document inputs missing from the Qwen snapshot",
         lambda c: f"{c['document_inputs']['missing_from_snapshot']} of {c['document_inputs']['unique']}"),
    ]  # fmt: skip
    lines += [f"| {name} | {get(prod)} | {get(variant)} |" for name, get in rows]
    lines += [
        "",
        (
            "**Hybrid (the selected method) is not compared.** The variant's chunk boundaries "
            "need document embeddings that the committed snapshot does not hold "
            f"({variant['document_inputs']['missing_from_snapshot']} inputs, about "
            f"{variant['document_inputs']['missing_estimated_tokens']} estimated tokens, "
            f"about ${variant['document_inputs']['missing_estimated_cost_usd']} at the verified "
            "price). They would need a separate, approved evaluation snapshot. No live call was "
            "made and no vectors were invented."
        ),
        "",
        "Lexical retrieval never reads vectors. Chunks were linked to placeholder vectors in "
        "a separate placeholder space only so that indexing classifies them searchable. As a "
        "control, the production config's lexical metrics through this path "
        + ("**match**" if result["control_matches_committed_lexical"] else "**DO NOT match**")
        + " the committed evaluation.",
        "",
        (
            "One small benchmark: no statistical significance is claimed, and production "
            "defaults are not changed by this experiment."
        ),
        "",
    ]
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    from app.core.config import get_settings

    parser = argparse.ArgumentParser(prog="python -m app.evaluation.retrieval.chunk_variant")
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--expect-dataset-sha", required=True)
    parser.add_argument("--report", type=Path, required=True, help="committed retrieval-v1.json")
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    dataset = load_dataset(args.dataset.resolve())
    if dataset.sha256 != args.expect_dataset_sha:
        print(json.dumps({"status": "error", "detail": "dataset sha256 mismatch"}))
        return 3
    database = require_eval_database(
        os.environ, application_database_url=get_settings().database_url.get_secret_value()
    )
    configs = run(database, dataset)
    committed = json.loads(args.report.read_text("utf-8"))["results"]["modes"]["lexical"]["metrics"]
    control = configs[SPAN_PACK_V1.version]["lexical"]
    matches = (
        all(control[f"recall@{k}"] == committed[f"recall@{k}"]["exact"] for k in (1, 3, 5))
        and control["mrr@10"] == committed["mrr@10"]["exact"]
    )
    result = {
        "experiment": EXPERIMENT,
        "dataset_sha256": dataset.sha256,
        "mode": MODE.value,
        "hybrid_compared": False,
        "control_matches_committed_lexical": matches,
        "configs": configs,
    }
    args.out_dir.mkdir(parents=True, exist_ok=True)
    (args.out_dir / "chunk-variant-v1.json").write_bytes(
        (json.dumps(result, indent=2, sort_keys=True) + "\n").encode("utf-8")
    )
    (args.out_dir / "chunk-variant-v1.md").write_bytes(markdown(result).encode("utf-8"))
    print(json.dumps({"status": "ok" if matches else "control_mismatch", "configs": {
        k: {"chunks": v["chunks"], "containment": v["target_containment"], "lexical": {
            m: v["lexical"][m] for m in ("recall@1", "recall@3", "recall@5", "mrr@10",
                                         "critical_rank", "invariant_failures")},
            "missing": v["document_inputs"]} for k, v in configs.items()}}, indent=1))  # fmt: skip
    sound = all(
        c["stale_lifecycle_ok"] and c["lexical"]["invariant_failures"] == 0
        for c in configs.values()
    )
    return 0 if matches and sound else 6


if __name__ == "__main__":
    sys.exit(main())
