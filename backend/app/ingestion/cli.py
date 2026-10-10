# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Ingestion command line.

In development it runs inside the backend container, where RAW_STORAGE_DIR
points at the shared ``rawdata`` volume (see docs/architecture/ingestion.md):

    docker compose run --rm --no-deps -v "<folder>:/input:ro" api \\
        python -m app.ingestion.cli import /input/brief.pdf --course-id <uuid> --type brief
    docker compose run --rm --no-deps api python -m app.ingestion.cli verify <version-id>
    docker compose run --rm --no-deps api python -m app.ingestion.cli check-store write

Every outcome, including failures, is one JSON object on stdout; logs (JSON
lines) go to stderr. Errors never print document text, file names or paths.

Exit codes: 0 ok; 1 unexpected or configuration error (see ``error``);
2 raw storage not configured or not usable; 3 file, course or version not
found, or file unreadable; 4 input rejected (``category``, or
``error: invalid_source`` for an invalid file name or --source-uri);
5 verification failed; 64 command-line usage error.
"""

import argparse
import asyncio
import json
import logging
import os
import stat
import sys
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.logging import configure_logging
from app.db.models import ResourceVersion, SourceSpan
from app.db.session import create_db_engine, create_session_factory
from app.domain.enums import ResourceType
from app.ingestion.errors import IngestionError
from app.ingestion.guards import IngestionLimits, check_size
from app.ingestion.hashing import sha256_hex, text_hash
from app.ingestion.schemas import RawSource
from app.ingestion.service import (
    ConcurrentImportError,
    CourseNotFoundError,
    IngestResult,
    ingest_file,
)
from app.ingestion.storage import (
    PROBE_CONTENT,
    LocalRawObjectStore,
    RawObjectIntegrityError,
    RawObjectNotFoundError,
    RawStoreError,
    errno_name,
    object_ref,
)

logger = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_UNEXPECTED = 1
EXIT_STORAGE = 2
EXIT_NOT_FOUND = 3
EXIT_REJECTED = 4
EXIT_VERIFY_FAILED = 5
EXIT_USAGE = 64  # sysexits EX_USAGE; argparse's own 2 would collide with EXIT_STORAGE


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, default=str, sort_keys=True))


def _result_payload(result: IngestResult) -> dict[str, Any]:
    return {
        "status": "created" if result.created else "unchanged",
        "resource_id": result.resource_id,
        "resource_created": result.resource_created,
        "version_id": result.version_id,
        "version_number": result.version_number,
        "content_hash": result.content_hash,
        "text_hash": result.text_hash,
        "raw_object_ref": result.raw_object_ref,
        "media_type": result.media_type.value,
        "byte_size": result.byte_size,
        "span_count": result.span_count,
        "spans_by_kind": {kind.value: n for kind, n in result.spans_by_kind.items()},
        "resource_type": result.resource_type.value,
        "total_page_count": result.total_page_count,
        "empty_page_count": result.empty_page_count,
    }


# --- import ---------------------------------------------------------------------------


async def _import(settings: Settings, args: argparse.Namespace) -> int:
    store = LocalRawObjectStore.from_settings(settings)  # fails before any work
    limits = IngestionLimits.from_settings(settings)
    path = Path(args.path)
    try:
        content = _read_input(path, limits)
    except FileNotFoundError:
        _emit({"status": "error", "error": "file_not_found"})
        return EXIT_NOT_FOUND
    except _NotARegularFileError:
        _emit({"status": "error", "error": "not_a_file"})
        return EXIT_NOT_FOUND
    except OSError as exc:  # e.g. permission denied; the path is not printed
        _emit({"status": "error", "error": "file_unreadable", "errno": errno_name(exc)})
        return EXIT_NOT_FOUND

    try:
        source = RawSource(
            content=content,
            display_name=path.name,
            source_uri=args.source_uri or f"upload:{path.name}",
        )
    except ValidationError as exc:
        fields = sorted({str(error["loc"][0]) for error in exc.errors()})
        _emit({"status": "rejected", "error": "invalid_source", "fields": fields})
        return EXIT_REJECTED
    engine = create_db_engine(settings)
    try:
        async with create_session_factory(engine)() as session:
            result = await ingest_file(
                session,
                store,
                course_id=args.course_id,
                resource_type=ResourceType(args.type),
                source=source,
                limits=limits,
                title=args.title,
            )
            await session.commit()
    finally:
        await engine.dispose()
    _emit(_result_payload(result))
    return EXIT_OK


class _NotARegularFileError(Exception):
    pass


def _read_input(path: Path, limits: IngestionLimits) -> bytes:
    """Read a regular file, never more than the size limit allows.

    Directories, devices and pipes are refused before opening (stat), and the
    read is bounded, so a file that grows or lies about its size cannot be
    loaded whole.
    """
    if not stat.S_ISREG(path.stat().st_mode):
        raise _NotARegularFileError
    check_size(path.stat().st_size, limits)
    with path.open("rb") as handle:
        content = handle.read(limits.max_bytes + 1)
    check_size(len(content), limits)
    return content


# --- verify ---------------------------------------------------------------------------


async def _verify(settings: Settings, args: argparse.Namespace) -> int:
    """Re-check one stored version end to end: original bytes and every span."""
    store = LocalRawObjectStore.from_settings(settings)
    engine = create_db_engine(settings)
    try:
        async with create_session_factory(engine)() as session:
            report = await _verify_version(session, store, args.version_id)
    finally:
        await engine.dispose()
    if report is None:
        _emit({"status": "error", "error": "version_not_found"})
        return EXIT_NOT_FOUND
    _emit(report)
    return EXIT_OK if report["status"] == "verified" else EXIT_VERIFY_FAILED


async def _verify_version(
    session: AsyncSession, store: LocalRawObjectStore, version_id: uuid.UUID
) -> dict[str, Any] | None:
    version = await session.get(ResourceVersion, version_id)
    if version is None:
        return None
    problems: list[str] = []

    try:
        content = store.open(version.raw_object_ref)  # hash-verified by the store
        if sha256_hex(content) != version.content_hash or len(content) != version.byte_size:
            problems.append("raw object does not match the version")
    except (RawObjectNotFoundError, RawObjectIntegrityError) as exc:
        problems.append(f"raw object: {type(exc).__name__}")
    if version.raw_object_ref != object_ref(version.content_hash):
        problems.append("raw object reference does not match content_hash")
    if text_hash(version.extracted_text) != version.text_hash:
        problems.append("text_hash does not match extracted_text")

    spans = list(
        await session.scalars(
            select(SourceSpan)
            .where(SourceSpan.resource_version_id == version.id)
            .order_by(SourceSpan.ordinal)
        )
    )
    if [s.ordinal for s in spans] != list(range(len(spans))):
        problems.append("span ordinals are not 0..n-1")
    mismatched = [
        s.ordinal
        for s in spans
        if s.start_offset is None
        or s.end_offset is None
        or version.extracted_text[s.start_offset : s.end_offset] != s.excerpt
    ]
    if mismatched:
        problems.append(f"{len(mismatched)} span(s) do not slice back to their excerpt")

    return {
        "status": "failed" if problems else "verified",
        "version_id": version.id,
        "version_number": version.version_number,
        "raw_object_ref": version.raw_object_ref,
        "byte_size": version.byte_size,
        "span_count": len(spans),
        "problems": problems,
    }


# --- check-store ----------------------------------------------------------------------


def _check_store(settings: Settings, args: argparse.Namespace) -> int:
    uid = os.getuid() if hasattr(os, "getuid") else None
    if uid == 0:
        _emit({"status": "error", "error": "running_as_root", "uid": uid})
        return EXIT_STORAGE
    store = LocalRawObjectStore.from_settings(settings)
    if args.mode == "write":
        report: dict[str, Any] = store.self_check()
    else:
        ref = object_ref(sha256_hex(PROBE_CONTENT))
        report = {"probe_ref": ref, "read": store.open(ref) == PROBE_CONTENT}
    _emit({"status": "ok", "mode": args.mode, "uid": uid, **report})
    return EXIT_OK


# --- entry point ----------------------------------------------------------------------


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> Any:  # argparse would exit with 2
        self.print_usage(sys.stderr)
        _emit({"status": "error", "error": "usage", "detail": message})
        sys.exit(EXIT_USAGE)


def _parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(prog="python -m app.ingestion.cli")
    commands = parser.add_subparsers(dest="command", required=True)

    imp = commands.add_parser("import", help="import one file into a course")
    imp.add_argument("path")
    imp.add_argument("--course-id", type=uuid.UUID, required=True)
    imp.add_argument("--type", required=True, choices=[t.value for t in ResourceType])
    imp.add_argument("--source-uri", help="logical identity; default upload:<file name>")
    imp.add_argument("--title", help="title for a new resource; default: the file name")

    verify = commands.add_parser("verify", help="re-check a stored version end to end")
    verify.add_argument("version_id", type=uuid.UUID)

    check = commands.add_parser("check-store", help="prove this process can use the raw store")
    check.add_argument("mode", choices=["write", "read"])
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
        return EXIT_STORAGE if "raw_storage_dir" in fields else EXIT_UNEXPECTED
    configure_logging(settings.log_level, stream=sys.stderr)
    try:
        if args.command == "import":
            return asyncio.run(_import(settings, args))
        if args.command == "verify":
            return asyncio.run(_verify(settings, args))
        return _check_store(settings, args)
    except IngestionError as exc:
        _emit({"status": "rejected", "category": exc.category.value, "detail": exc.detail})
        return EXIT_REJECTED
    except CourseNotFoundError:
        _emit({"status": "error", "error": "course_not_found"})
        return EXIT_NOT_FOUND
    except RawStoreError as exc:
        _emit({"status": "error", "error": "raw_storage", "detail": str(exc)})
        return EXIT_STORAGE
    except ConcurrentImportError:
        _emit({"status": "error", "error": "concurrent_import", "retry": True})
        return EXIT_UNEXPECTED
    except Exception as exc:  # noqa: BLE001 - one JSON result, never a traceback
        # The exception type only: messages may quote document text or paths.
        logger.error("cli.unexpected_error", extra={"error_type": type(exc).__name__})
        _emit({"status": "error", "error": "unexpected", "type": type(exc).__name__})
        return EXIT_UNEXPECTED


if __name__ == "__main__":
    sys.exit(main())
