# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Recording the supplementary courses' vectors: the only paid step for
them, made once (development).

    uv run python -m app.evaluation.northbridge.supplement_record documents --price-usd-per-million <price> [--live]
    uv run python -m app.evaluation.northbridge.supplement_record queries --price-usd-per-million <price> [--live]

``queries`` builds the extractions in the evaluation database
(``COURSEFLOW_EVAL_DATABASE_URL``, recreated first) to learn the mapping
queries. Without ``--live`` no request is made; the recorder's request and
spend bounds apply.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.ai.embeddings import EmbeddingInput
from app.demo.supplement import (
    DOCUMENT_EMBEDDINGS,
    QUERY_EMBEDDINGS,
    document_inputs,
    supplement_refs,
    supplement_subject,
)
from app.evaluation.retrieval.snapshot import ExpectedInputs


async def _record(settings: Any, inputs: ExpectedInputs, output: Path, price: Decimal,
                  live: bool) -> dict[str, Any]:  # fmt: skip
    from app.ai.nebius_embeddings import NebiusEmbeddingProvider, embedding_space_from_settings
    from app.evaluation.retrieval.record import existing_outputs, plan_recording, record_snapshot

    plan = plan_recording(
        supplement_subject(),
        embedding_space_from_settings(settings),
        batch_max_items=settings.embedding_batch_max_items,
        batch_max_estimated_tokens=settings.embedding_batch_max_estimated_tokens,
        attempts_per_request=settings.ai_max_attempts,
        price_usd_per_million=price,
        output=output,
        inputs=inputs,
    )
    report = plan.describe()
    report["existing_output"] = existing_outputs(output)
    if not live:
        return {"status": "planned", "live": False, "plan": report}
    provider = NebiusEmbeddingProvider.from_settings(settings)
    try:
        result = await record_snapshot(plan, provider)
    finally:
        await provider.aclose()
    return {"status": "recorded", "snapshot": result.snapshot.as_posix(),
            "snapshot_sha256": result.sha256,
            "spent_usd_from_reported_tokens": f"{result.spent_usd:.6f}"}  # fmt: skip


async def _queries(settings: Any) -> tuple[EmbeddingInput, ...]:
    """The supplement's mapping queries, from its extractions built in the
    evaluation database (recreated first)."""
    import os

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.ai.nebius_embeddings import embedding_space_from_settings
    from app.demo.intelligence import DemoReplay, query_inputs
    from app.evaluation.retrieval.eval_db import (
        migrate_eval_database,
        recreate_eval_database,
        require_eval_database,
    )
    from app.ingestion.storage import LocalRawObjectStore

    database = require_eval_database(
        os.environ, application_database_url=settings.database_url.get_secret_value()
    )
    await asyncio.to_thread(recreate_eval_database, database)
    await asyncio.to_thread(migrate_eval_database, database)
    engine = create_async_engine(database.url)
    try:
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        with tempfile.TemporaryDirectory() as raw:
            replay = DemoReplay(sessions, LocalRawObjectStore(Path(raw)),
                                embedding_space_from_settings(settings))  # fmt: skip
            courses, _ = await replay.ingest(reset=False)
            ours = {ref: courses[ref] for ref in sorted(courses) if ref in supplement_refs()}
            for ref in sorted(ours):
                await replay.assessments(ours[ref])
                await replay.claims(ours[ref])
            return await query_inputs(sessions, ours)
    finally:
        await engine.dispose()


def main(argv: Sequence[str] | None = None) -> int:
    from app.core.config import get_settings
    from app.evaluation.retrieval.record import RecordingRefused, parse_price

    parser = argparse.ArgumentParser(prog="python -m app.evaluation.northbridge.supplement_record")
    parser.add_argument("what", choices=["documents", "queries"])
    parser.add_argument("--price-usd-per-million", type=parse_price, required=True)
    parser.add_argument("--live", action="store_true", help="make the paid provider requests")
    args = parser.parse_args(argv)
    settings = get_settings()
    price = Decimal(args.price_usd_per_million)

    async def run() -> dict[str, Any]:
        if args.what == "documents":
            return await _record(settings, document_inputs(), DOCUMENT_EMBEDDINGS, price, args.live)
        queries = await _queries(settings)
        return await _record(settings, ExpectedInputs((), queries), QUERY_EMBEDDINGS, price,
                             args.live)  # fmt: skip

    try:
        print(json.dumps(asyncio.run(run()), indent=2, sort_keys=True))
    except RecordingRefused as exc:
        print(json.dumps({"status": "refused", "problems": list(exc.problems)}))
        return 4
    return 0


if __name__ == "__main__":
    sys.exit(main())
