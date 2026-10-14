# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Retrieval command line (development).

    docker compose run --rm --no-deps api python -m app.retrieval.cli index --course-id <uuid>
    docker compose run --rm --no-deps api python -m app.retrieval.cli status --course-id <uuid>

``index`` chunks the course's current versions, embeds what is not cached
(Nebius Token Factory, configured by NEBIUS_* and EMBEDDING_*) and publishes
each version atomically. ``status`` re-derives each current version's index
state offline: it needs only the embedding space (EMBEDDING_MODEL,
EMBEDDING_DIMENSIONS, EMBEDDING_SPACE_VERSION), no API key, no provider
client and no network, and it writes nothing.

Every outcome is one JSON object on stdout; logs (JSON lines) go to stderr.
Output holds IDs and counts only, never document text.

Exit codes: 0 ok (index: the run succeeded); 1 unexpected or configuration
error; 2 embedding provider not configured; 3 course not found; 5 the index
run failed (its summary says which versions and why); 64 command-line usage.
"""

import argparse
import asyncio
import json
import logging
import sys
import uuid
from collections.abc import Sequence
from typing import Any

from pydantic import ValidationError

from app.ai.embeddings import EmbeddingProvider
from app.ai.errors import AIConfigurationError
from app.ai.nebius_embeddings import NebiusEmbeddingProvider, embedding_space_from_settings
from app.core.config import Settings, get_settings
from app.core.logging import configure_logging
from app.db.session import create_db_engine, create_session_factory
from app.domain.enums import WorkflowRunStatus
from app.retrieval.indexing import (
    CourseNotFoundError,
    IndexingService,
    VersionIndexReport,
    inspect_course,
)

logger = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_UNEXPECTED = 1
EXIT_NOT_CONFIGURED = 2
EXIT_NOT_FOUND = 3
EXIT_RUN_FAILED = 5
EXIT_USAGE = 64


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, default=str, sort_keys=True))


def _provider(settings: Settings) -> EmbeddingProvider:
    return NebiusEmbeddingProvider.from_settings(settings)


async def _run_index(settings: Settings, course_id: uuid.UUID) -> int:
    provider = _provider(settings)  # needs credentials; fails before any database work
    engine = create_db_engine(settings)
    try:
        service = IndexingService(
            create_session_factory(engine),
            provider,
            batch_max_items=settings.embedding_batch_max_items,
            batch_max_estimated_tokens=settings.embedding_batch_max_estimated_tokens,
        )
        return await _index(service, course_id)
    finally:
        await provider.aclose()
        await engine.dispose()


async def _run_status(settings: Settings, course_id: uuid.UUID) -> int:
    space = embedding_space_from_settings(settings)  # identity only: no client, no key
    engine = create_db_engine(settings)
    try:
        reports = await inspect_course(create_session_factory(engine), course_id, space)
    finally:
        await engine.dispose()
    return _status(course_id, reports)


async def _index(service: IndexingService, course_id: uuid.UUID) -> int:
    result = await service.index_course(course_id)
    _emit(
        {
            "status": result.status.value,
            "run_id": result.run_id,
            "summary": result.summary.model_dump(mode="json"),
        }
    )
    return EXIT_OK if result.status is WorkflowRunStatus.SUCCEEDED else EXIT_RUN_FAILED


def _status(course_id: uuid.UUID, reports: Sequence[VersionIndexReport]) -> int:
    _emit(
        {
            "status": "ok",
            "course_id": course_id,
            "indexed": sum(report.indexed for report in reports),
            "versions": [
                {
                    "resource_version_id": report.resource_version_id,
                    "chunk_state": report.chunk_state and report.chunk_state.value,
                    "link_state": report.link_state and report.link_state.value,
                    "chunks": report.chunks,
                    "indexed": report.indexed,
                }
                for report in reports
            ],
        }
    )
    return EXIT_OK


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> Any:  # argparse would exit with 2
        self.print_usage(sys.stderr)
        _emit({"status": "error", "error": "usage", "detail": message})
        sys.exit(EXIT_USAGE)


def _parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(prog="python -m app.retrieval.cli")
    commands = parser.add_subparsers(dest="command", required=True)
    index = commands.add_parser("index", help="index a course's current versions")
    index.add_argument("--course-id", type=uuid.UUID, required=True)
    status = commands.add_parser("status", help="show each current version's index state")
    status.add_argument("--course-id", type=uuid.UUID, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:  # usage error (64) or --help (0): returned, not raised
        return exc.code if isinstance(exc.code, int) else EXIT_USAGE
    try:
        settings = get_settings()
    except ValidationError as exc:
        fields = sorted({str(error["loc"][0]) for error in exc.errors()})
        _emit({"status": "error", "error": "configuration", "fields": fields})
        return EXIT_UNEXPECTED
    configure_logging(settings.log_level, stream=sys.stderr)
    try:
        if args.command == "index":
            return asyncio.run(_run_index(settings, args.course_id))
        return asyncio.run(_run_status(settings, args.course_id))
    except AIConfigurationError as exc:
        _emit({"status": "error", "error": "provider_not_configured", "detail": str(exc)})
        return EXIT_NOT_CONFIGURED
    except CourseNotFoundError:
        _emit({"status": "error", "error": "course_not_found"})
        return EXIT_NOT_FOUND
    except Exception as exc:  # noqa: BLE001 - one JSON result, never a traceback
        # The exception type only: messages may quote document text.
        logger.error("cli.unexpected_error", extra={"error_type": type(exc).__name__})
        _emit({"status": "error", "error": "unexpected", "type": type(exc).__name__})
        return EXIT_UNEXPECTED


if __name__ == "__main__":
    sys.exit(main())
