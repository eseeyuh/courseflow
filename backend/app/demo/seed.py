# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Seed the Northbridge demo institution (development).

    uv run python -m app.demo.seed [--reset] [--state initial|final]

Imports every course of the demo institution through ``DemoConnector`` into
the configured database (``DATABASE_URL``) and raw store
(``RAW_STORAGE_DIR``). ``--state initial`` (the default) stops at the
connector's initial sync; ``final`` also applies every later change.
``--reset`` first removes the Northbridge institution and everything it
owns, and nothing else (see ``reset``). Re-running without ``--reset`` is
idempotent.

Exit codes: 0 ok; 1 unexpected error; 3 invalid demo data or a sync
conflict; 64 usage.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.connectors.demo import DemoConnector, ManifestError
from app.connectors.sync import SyncConflictError, apply_changes, import_course
from app.demo.generate import DATA_ROOT
from app.demo.reset import reset_demo_institution
from app.domain.enums import LmsType
from app.ingestion.guards import IngestionLimits
from app.ingestion.storage import RawObjectStore

DEMO_USER = "demo-student"


async def seed(
    session_factory: async_sessionmaker[AsyncSession],
    store: RawObjectStore,
    *,
    limits: IngestionLimits,
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
        courses.append(entry)
    summary["courses"] = courses
    return summary


def main(argv: Sequence[str] | None = None) -> int:
    from app.core.config import get_settings
    from app.db.session import create_db_engine, create_session_factory
    from app.ingestion.storage import LocalRawObjectStore

    parser = argparse.ArgumentParser(prog="python -m app.demo.seed")
    parser.add_argument("--reset", action="store_true", help="remove Northbridge data first")
    parser.add_argument("--state", choices=["initial", "final"], default="initial")
    args = parser.parse_args(argv)
    settings = get_settings()

    async def run() -> dict[str, Any]:
        engine = create_db_engine(settings)
        try:
            return await seed(
                create_session_factory(engine),
                LocalRawObjectStore.from_settings(settings),
                limits=IngestionLimits.from_settings(settings),
                reset=args.reset,
                state=args.state,
            )
        finally:
            await engine.dispose()

    try:
        print(json.dumps({"status": "ok", **asyncio.run(run())}, indent=2))
    except (ManifestError, SyncConflictError) as exc:
        print(json.dumps({"status": "error", "error": type(exc).__name__, "detail": str(exc)}))
        return 3
    except Exception as exc:  # noqa: BLE001 - report the type only
        print(json.dumps({"status": "error", "error": "unexpected", "type": type(exc).__name__}))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
