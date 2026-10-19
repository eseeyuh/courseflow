# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""The Northbridge embedding snapshot: plan, record (only with ``--live``),
and the inputs a replayed seed needs.

    uv run python -m app.demo.snapshot --expect-dataset-sha <sha256> \\
        --price-usd-per-million <current catalog price> [--live]

The snapshot is pinned to the frozen Northbridge golden dataset (its SHA
covers the corpus, so any change to a document invalidates it) and holds one
``document/v1`` vector per distinct chunk of every version in the corpus,
built by the production parser, chunker (``span-pack/v1``) and
``document_input``: exactly what indexing the initial and final states
requests. Seeding never embeds queries, so the snapshot holds none.

Everything else is the retrieval snapshot tooling: the same format, the
same hard request, attempt and spend bounds, the same runtime usage guard,
atomic publication and no overwrite. Without ``--live`` no request is made.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.ai.embeddings import EmbeddingInput, document_input
from app.connectors.demo import DemoManifest
from app.evaluation.northbridge.labels import (
    CORPUS_DIR,
    REPO_ROOT,
    GoldenDataset,
    LabelError,
    load_golden,
)
from app.evaluation.retrieval.record import (
    RecordingFailed,
    RecordingRefused,
    existing_outputs,
    plan_recording,
    record_snapshot,
)
from app.evaluation.retrieval.snapshot import (
    ExpectedInputs,
    SnapshotError,
    SnapshotSubject,
    file_chunk_texts,
    input_key,
)
from app.retrieval.chunking import SPAN_PACK_V1

SNAPSHOT_PATH = (
    REPO_ROOT
    / "evals"
    / "snapshots"
    / "northbridge"
    / "v1"
    / "nebius-qwen3-embedding-8b-d4096-sv1.jsonl"
)


def snapshot_subject(golden: GoldenDataset) -> SnapshotSubject:
    return SnapshotSubject(
        name=golden.manifest.dataset,
        version=golden.manifest.dataset_version,
        sha256=golden.sha256,
        hash_scheme=golden.manifest.dataset_hash_scheme,
        chunker=SPAN_PACK_V1.version,
    )


def corpus_inputs(corpus: DemoManifest) -> ExpectedInputs:
    """Every distinct document input of every version, in manifest order."""
    documents: dict[tuple[str, str], EmbeddingInput] = {}
    for course in corpus.courses:
        for resource in course.resources:
            for version in resource.versions:
                where = f"{course.ref}/{resource.key} v{version.version}"
                for text in file_chunk_texts(CORPUS_DIR / version.file, SPAN_PACK_V1, where):
                    item = document_input(text)
                    documents.setdefault(input_key(item), item)
    return ExpectedInputs(tuple(documents.values()), ())


def main(argv: Sequence[str] | None = None) -> int:
    from app.ai.nebius_embeddings import NebiusEmbeddingProvider, embedding_space_from_settings
    from app.core.config import get_settings
    from app.evaluation.retrieval.record import parse_price

    parser = argparse.ArgumentParser(prog="python -m app.demo.snapshot")
    parser.add_argument("--expect-dataset-sha", required=True)
    parser.add_argument("--price-usd-per-million", type=parse_price, required=True)
    parser.add_argument("--snapshot", type=Path, default=SNAPSHOT_PATH)
    parser.add_argument("--live", action="store_true", help="make the paid provider requests")
    args = parser.parse_args(argv)

    def emit(payload: dict[str, Any]) -> None:
        print(json.dumps(payload, indent=2, sort_keys=True))

    try:
        golden = load_golden()
        if golden.sha256 != args.expect_dataset_sha:
            emit({"status": "error", "error": "dataset_mismatch", "detail": golden.sha256})
            return 3
        settings = get_settings()
        plan = plan_recording(
            snapshot_subject(golden),
            embedding_space_from_settings(settings),
            batch_max_items=settings.embedding_batch_max_items,
            batch_max_estimated_tokens=settings.embedding_batch_max_estimated_tokens,
            attempts_per_request=settings.ai_max_attempts,
            price_usd_per_million=Decimal(args.price_usd_per_million),
            output=args.snapshot,
            inputs=corpus_inputs(golden.corpus),
        )
        report = plan.describe()
        report["existing_output"] = existing_outputs(args.snapshot)
        if not args.live:
            refused = bool(report["problems"] or report["existing_output"])
            emit({"status": "refused" if refused else "planned", "live": False, "plan": report})
            return 4 if refused else 0

        async def live() -> Any:
            provider = NebiusEmbeddingProvider.from_settings(settings)
            try:
                return await record_snapshot(plan, provider)
            finally:
                await provider.aclose()

        result = asyncio.run(live())
        emit(
            {
                "status": "recorded",
                "live": True,
                "snapshot": result.snapshot.as_posix(),
                "snapshot_sha256": result.sha256,
                "recording": result.header.recording.model_dump(),
                "spent_usd_from_reported_tokens": f"{result.spent_usd:.6f}",
            }
        )
    except (LabelError, SnapshotError) as exc:
        emit({"status": "error", "error": "invalid", "problems": list(exc.problems)})
        return 3
    except RecordingRefused as exc:
        emit({"status": "refused", "problems": list(exc.problems)})
        return 4
    except RecordingFailed as exc:
        emit({"status": "error", "error": "recording_failed", "detail": str(exc)})
        return 5
    return 0


if __name__ == "__main__":
    sys.exit(main())
