# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Seed the Northbridge demo institution (development).

    uv run python -m app.demo.seed [--reset] [--state initial|final]

Imports every course of the demo institution through ``DemoConnector`` into
the configured database (``DATABASE_URL``) and raw store
(``RAW_STORAGE_DIR``), then indexes each course. ``--state initial`` (the
default) stops at the connector's initial sync; ``final`` also applies every
later change and indexes again, so only the changed versions are new work.
``--reset`` first removes the Northbridge institution and everything it
owns, and nothing else (see ``reset``). Re-running without ``--reset`` is
idempotent.

Indexing replays the committed Northbridge embedding snapshot (see
``snapshot``): no API key, no network, no spend. It needs only the
non-secret embedding space settings, which must match the snapshot's. A
corpus that no longer matches the frozen golden dataset is refused.

Run it where the raw store is (as every import):

    docker compose run --rm api python -m app.demo.seed --reset

Exit codes: 0 ok; 1 unexpected error; 3 invalid demo data, an unusable
snapshot, an unusable raw store or a sync conflict; 5 indexing did not
succeed; 64 usage.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import sys
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.connectors.demo import DemoConnector, ManifestError
from app.connectors.sync import SyncConflictError, apply_changes, import_course
from app.demo.generate import DATA_ROOT
from app.demo.reset import reset_demo_institution
from app.domain.enums import LmsType, WorkflowRunStatus
from app.evaluation.northbridge.labels import LabelError
from app.evaluation.retrieval.snapshot import SnapshotError
from app.ingestion.guards import IngestionLimits
from app.ingestion.storage import RawObjectStore, RawStoreError
from app.retrieval.indexing import IndexingService

DEMO_USER = "demo-student"


class IndexingIncomplete(RuntimeError):
    """An index run of a seeded course did not succeed."""


async def _index(indexer: IndexingService, course_id: uuid.UUID, course_ref: str) -> dict[str, Any]:
    result = await indexer.index_course(course_id)
    if result.status is not WorkflowRunStatus.SUCCEEDED:
        raise IndexingIncomplete(f"{course_ref}: index run {result.run_id} {result.status.value}")
    summary = result.summary
    return {
        "run_id": str(result.run_id),
        "versions_indexed": summary.versions_indexed,
        "versions_already_indexed": summary.versions_already_indexed,
        "chunks_created": summary.chunks_created,
        "provider_calls": summary.provider_calls,
    }


async def seed(
    session_factory: async_sessionmaker[AsyncSession],
    store: RawObjectStore,
    *,
    limits: IngestionLimits,
    indexer: IndexingService,
    root: Path = DATA_ROOT,
    reset: bool = False,
    state: str = "initial",
) -> dict[str, Any]:
    if state not in {"initial", "final"}:
        raise ValueError("state must be initial or final")
    connector = DemoConnector(root)
    institution = connector.manifest.institution.name
    summary: dict[str, Any] = {"institution": institution, "state": state}
    if reset:
        removed = await reset_demo_institution(session_factory, institution, LmsType.DEMO)
        summary["reset"] = dataclasses.asdict(removed)
    courses = []
    for stub in await connector.list_courses(DEMO_USER):
        imported = await import_course(
            session_factory,
            store,
            connector,
            stub,
            institution_name=institution,
            lms_type=LmsType.DEMO,
            limits=limits,
        )
        entry: dict[str, Any] = {
            "course": stub.course_ref,
            "course_id": str(imported.course_id),
            "initial_versions_created": imported.versions_created,
            "initial_documents": len(imported.documents),
            "initial_index": await _index(indexer, imported.course_id, stub.course_ref),
        }
        if state == "final":
            changed = await apply_changes(
                session_factory,
                store,
                connector,
                imported.course_id,
                stub.course_ref,
                connector.manifest.initial_sync_at,
                limits=limits,
            )
            entry["change_versions_created"] = changed.versions_created
            entry["changes"] = len(changed.documents)
            entry["final_index"] = await _index(indexer, imported.course_id, stub.course_ref)
        courses.append(entry)
    summary["courses"] = courses
    return summary


def main(argv: Sequence[str] | None = None) -> int:
    from app.ai.nebius_embeddings import embedding_space_from_settings
    from app.core.config import get_settings
    from app.db.session import create_db_engine, create_session_factory
    from app.demo.snapshot import load_demo_replay
    from app.ingestion.storage import LocalRawObjectStore

    parser = argparse.ArgumentParser(prog="python -m app.demo.seed")
    parser.add_argument("--reset", action="store_true", help="remove Northbridge data first")
    parser.add_argument("--state", choices=["initial", "final"], default="initial")
    args = parser.parse_args(argv)
    settings = get_settings()

    async def run() -> dict[str, Any]:
        replay = load_demo_replay(
            embedding_space_from_settings(settings),
            max_batch_items=settings.embedding_batch_max_items,
        )
        engine = create_db_engine(settings)
        try:
            sessions = create_session_factory(engine)
            indexer = IndexingService(
                sessions,
                replay,
                batch_max_items=settings.embedding_batch_max_items,
                batch_max_estimated_tokens=settings.embedding_batch_max_estimated_tokens,
            )
            summary = await seed(
                sessions,
                LocalRawObjectStore.from_settings(settings),
                limits=IngestionLimits.from_settings(settings),
                indexer=indexer,
                reset=args.reset,
                state=args.state,
            )
            return {"embeddings": f"replay:{replay.snapshot_sha256}", **summary}
        finally:
            await engine.dispose()

    try:
        print(json.dumps({"status": "ok", **asyncio.run(run())}, indent=2))
    except (ManifestError, SyncConflictError) as exc:
        print(json.dumps({"status": "error", "error": type(exc).__name__, "detail": str(exc)}))
        return 3
    except (LabelError, SnapshotError) as exc:
        print(
            json.dumps({"status": "error", "error": type(exc).__name__, "problems": exc.problems})
        )
        return 3
    except RawStoreError as exc:
        print(json.dumps({"status": "error", "error": type(exc).__name__, "detail": str(exc)}))
        return 3
    except IndexingIncomplete as exc:
        print(json.dumps({"status": "error", "error": "indexing_incomplete", "detail": str(exc)}))
        return 5
    except Exception as exc:  # noqa: BLE001 - report the type only
        print(json.dumps({"status": "error", "error": "unexpected", "type": type(exc).__name__}))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
