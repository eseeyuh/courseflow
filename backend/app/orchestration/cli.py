# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Run the offline course-intelligence workflow over the demo courses.

    uv run python -m app.orchestration.cli [--reset] [--steps ingest,assessments,...]

Uses the configured database and raw store, and the committed recordings
only: no network, no spend. Prints each course's run ID, status and trace.
Exit codes: 0 succeeded; 1 a run failed; 3 no raw store; 64 a step is not allowed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from typing import Any


def main(argv: Sequence[str] | None = None) -> int:
    from app.ai.nebius_embeddings import embedding_space_from_settings
    from app.core.config import get_settings
    from app.db.session import create_db_engine, create_session_factory
    from app.demo.intelligence import DemoReplay
    from app.ingestion.storage import LocalRawObjectStore, RawStoreError
    from app.orchestration.workflow import (
        CourseIntelligenceWorkflow,
        UnknownStepError,
        allowed_steps,
    )

    parser = argparse.ArgumentParser(prog="python -m app.orchestration.cli")
    parser.add_argument("--reset", action="store_true", help="remove the demo data first")
    parser.add_argument("--steps", help="comma-separated allowlisted steps (default: all)")
    args = parser.parse_args(argv)
    settings = get_settings()
    steps = [s.strip() for s in args.steps.split(",")] if args.steps else None
    try:
        allowed_steps(steps)  # refuse before touching the database or the store
        store = LocalRawObjectStore.from_settings(settings)
    except UnknownStepError as exc:
        print(json.dumps({"status": "refused", "steps": list(exc.names)}))
        return 64
    except RawStoreError as exc:
        print(json.dumps({"status": "error", "error": type(exc).__name__}))
        return 3

    async def run() -> dict[str, Any]:
        engine = create_db_engine(settings)
        try:
            sessions = create_session_factory(engine)
            replay = DemoReplay(sessions, store, embedding_space_from_settings(settings))
            result = await CourseIntelligenceWorkflow(sessions, replay).run(
                reset=args.reset, steps=steps
            )
            return {
                "succeeded": result.succeeded,
                "runs": {
                    ref: {"run_id": str(result.runs[ref]), "status": result.statuses[ref].value,
                          "trace": result.traces[ref].model_dump(mode="json")}
                    for ref in sorted(result.runs)
                },
            }  # fmt: skip
        finally:
            await engine.dispose()

    outcome = asyncio.run(run())
    print(json.dumps(outcome, indent=2))
    return 0 if outcome["succeeded"] else 1


if __name__ == "__main__":
    sys.exit(main())
