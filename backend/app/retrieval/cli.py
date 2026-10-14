# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Retrieval command line (development).

    docker compose run --rm --no-deps api python -m app.retrieval.cli index --course-id <uuid>
    docker compose run --rm --no-deps api python -m app.retrieval.cli status --course-id <uuid>
    docker compose run --rm --no-deps api python -m app.retrieval.cli stats --course-id <uuid>
    docker compose run --rm --no-deps api python -m app.retrieval.cli search \\
        --course-id <uuid> --query "When is coursework 1 due?" [-k 5] [--type brief] [--json]
    echo "When is coursework 1 due?" | docker compose run --rm --no-deps -T api \\
        python -m app.retrieval.cli search --course-id <uuid> --query -

``index`` chunks the course's current versions, embeds what is not cached
(Nebius Token Factory, configured by NEBIUS_* and EMBEDDING_*) and publishes
each version atomically. ``status`` re-derives each current version's index
state offline: it needs only the embedding space (EMBEDDING_MODEL,
EMBEDDING_DIMENSIONS, EMBEDDING_SPACE_VERSION), no API key, no provider
client and no network, and it writes nothing. ``stats`` reports chunk and
token-budget statistics of the course's current versions, also offline and
read-only (it does not even need the embedding settings). ``search`` embeds the question
(one recorded provider call) and ranks the course's verified, current chunks.

``index`` and ``status`` print one JSON object on stdout. ``stats`` and
``search`` print readable text, or one JSON object with ``--json``; ``search`` it deliberately shows
retrieved course text. Logs (JSON lines, stderr) never contain query text,
chunk text, excerpts or source URIs.

Exit codes: 0 ok (index: the run succeeded); 1 unexpected or configuration
error; 2 embedding provider not configured; 3 course not found; 4 invalid
search request; 5 provider failure (index: the run failed, see its summary;
search: the query embedding failed); 6 inconsistent index, provenance or
chunk state (search and stats refuse to report on it); 64 command-line usage.
"""

import argparse
import asyncio
import dataclasses
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
from app.domain.enums import ResourceType, WorkflowRunStatus
from app.retrieval.indexing import (
    CourseNotFoundError,
    IndexingService,
    VersionIndexReport,
    inspect_course,
)
from app.retrieval.search import (
    DEFAULT_K,
    IndexStateError,
    InvalidSearchRequest,
    ProvenanceError,
    SearchHit,
    SearchProviderError,
    SearchRequest,
    SearchResult,
    SearchService,
    SpanEvidence,
)
from app.retrieval.stats import ChunkStats, CourseStats, StatsIntegrityError, course_stats

logger = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_UNEXPECTED = 1
EXIT_NOT_CONFIGURED = 2
EXIT_NOT_FOUND = 3
EXIT_INVALID_REQUEST = 4
EXIT_RUN_FAILED = 5  # index run failed, or the search's provider call failed
EXIT_PROVIDER_FAILED = EXIT_RUN_FAILED
EXIT_INDEX_STATE = 6
EXIT_USAGE = 64

_PREVIEW_CHARS = 200


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, default=str, sort_keys=True))


def _provider(settings: Settings) -> EmbeddingProvider:
    return NebiusEmbeddingProvider.from_settings(settings)


# --- index and status -----------------------------------------------------------------------


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
                    "content": report.content.value,
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


# --- stats ----------------------------------------------------------------------------------


async def _run_stats(settings: Settings, args: argparse.Namespace) -> int:
    engine = create_db_engine(settings)  # offline: no provider, no embedding settings
    try:
        report = await course_stats(create_session_factory(engine), args.course_id)
    finally:
        await engine.dispose()
    if args.json:
        _emit({"status": "ok", **dataclasses.asdict(report)})
    else:
        _print_text(_render_stats(report))
    return EXIT_OK


def _render_stats(report: CourseStats) -> str:
    header = (
        f"Course {report.course_id} | chunker {report.chunker_version} "
        f"(target {report.target_estimated_tokens}, hard max "
        f"{report.hard_max_estimated_tokens} estimated tokens)"
    )
    note = (
        f"{len(report.versions)} current version(s); token figures are estimates "
        "(ceil(chars / 4)), not tokenizer counts."
    )
    lines = [header, note, "", "course total:", *_stats_lines(report.total)]
    for version in report.versions:
        title = (
            f"version {version.resource_version_id} ({version.content.value}; "
            f"stored chunks: {version.stored_chunk_state.value}):"
        )
        lines += ["", title, *_stats_lines(version.stats)]
    return "\n".join(lines)


def _stats_lines(stats: ChunkStats) -> list[str]:
    tokens = stats.estimated_tokens
    spread = "n/a"
    if tokens is not None:
        spread = f"min {tokens.min}, p50 {tokens.p50}, p95 {tokens.p95}, max {tokens.max}"
    mean = "n/a" if stats.spans_per_chunk_mean is None else f"{stats.spans_per_chunk_mean:.2f}"
    spans = f"{stats.spans} ({stats.visible_spans} with visible text)"
    budget = (
        f"at or above target: {stats.chunks_at_or_above_target}; "
        f"at hard max: {stats.chunks_at_hard_max}"
    )
    shape = (
        f"spans per chunk: mean {mean}, max {stats.spans_per_chunk_max or 'n/a'}; "
        f"split spans {stats.split_spans}; "
        f"chunk edges inside spans {stats.chunk_edges_inside_spans}"
    )
    coverage = (
        f"duplication factor {_ratio(stats.duplication_factor)} "
        f"(overlap extra {_ratio(stats.overlap_extra_ratio)}); "
        f"visible coverage {_ratio(stats.visible_coverage)}; "
        f"uncovered whitespace {stats.uncovered_whitespace_chars} chars"
    )
    return [
        f"  chunks {stats.chunks}; spans {spans}",
        f"  estimated tokens per chunk: {spread}",
        f"  {budget}",
        f"  {shape}",
        f"  {coverage}",
    ]


def _ratio(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.3f}"


# --- search ---------------------------------------------------------------------------------


async def _run_search(settings: Settings, args: argparse.Namespace) -> int:
    query = sys.stdin.read() if args.query == "-" else args.query
    request = SearchRequest.build(  # validated before any other work
        course_id=args.course_id,
        query=query,
        k=args.k,
        resource_types=frozenset(ResourceType(t) for t in args.types) if args.types else None,
    )
    provider = _provider(settings)
    engine = create_db_engine(settings)
    try:
        result = await SearchService(create_session_factory(engine), provider).search_course(
            request
        )
    finally:
        await provider.aclose()
        await engine.dispose()
    if args.json:
        _emit(_result_payload(result))  # JSON is ASCII-escaped: safe on any console
    else:
        _print_text(_render(result))
    return EXIT_OK


def _print_text(text: str) -> None:
    """Course text may hold characters a legacy console cannot encode: replace
    them rather than fail after a paid search."""
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(errors="replace")
    print(text)


def _result_payload(result: SearchResult) -> dict[str, Any]:
    payload = dataclasses.asdict(result)
    payload["space"] = result.space.model_dump(mode="json")
    return {"status": "ok", **payload}


def _render(result: SearchResult) -> str:
    space = result.space
    lines = [
        f"Course {result.course_id}",
        (
            f"Space {space.provider} {space.model} ({space.dimensions} dims, "
            f"{space.space_version}) | chunker {result.chunker_version}"
        ),
        f"Searched {result.searchable_versions} of {result.current_versions} current version(s)",
    ]
    if result.unindexed_versions:
        lines.append(
            f"  {result.unindexed_versions} current version(s) not indexed in this space: "
            f"run `index --course-id {result.course_id}`"
        )
    if not result.hits:
        lines.append("No results.")
        return "\n".join(lines)
    lines.append("Score is a ranking similarity (1 - cosine distance), not a confidence.")
    for hit in result.hits:
        lines.extend(_render_hit(hit))
    return "\n".join(lines)


def _render_hit(hit: SearchHit) -> list[str]:
    preview = " ".join(hit.text.split())
    if len(preview) > _PREVIEW_CHARS:
        preview = preview[:_PREVIEW_CHARS].rstrip() + "..."
    resource = hit.resource
    return [
        "",
        (
            f"#{hit.rank}  {hit.score:.4f}  {resource.title} ({resource.type.value}) "
            f"v{hit.version.version_number} | chunk {hit.chunk_ordinal} {hit.chunk_id} | "
            f"{resource.source_uri}"
        ),
        f"    {preview}",
        *(f"    evidence: {line}" for line in _evidence_lines(hit.spans)),
    ]


def _evidence_lines(spans: Sequence[SpanEvidence]) -> list[str]:
    """Runs of whole spans on one line; a partial span on its own line with its
    exact character range. (JSON output lists every span.)"""
    lines: list[str] = []
    run: list[SpanEvidence] = []

    def flush() -> None:
        if len(run) == 1:
            lines.append(_locator(run[0]))
        elif run:
            lines.append(
                f"spans {run[0].ordinal}-{run[-1].ordinal}: {_where(run[0])} to "
                f"{_where(run[-1])} (whole spans)"
            )
        run.clear()

    for span in spans:
        if span.coverage == "full":
            run.append(span)
            continue
        flush()
        lines.append(_locator(span))
    flush()
    return lines


def _where(span: SpanEvidence) -> str:
    if span.page_number is not None:
        return f"page {span.page_number}"
    if span.slide_number is not None:
        notes = " notes" if span.block_kind.value == "slide_notes" else ""
        return f"slide {span.slide_number}{notes}"
    return f"section {span.section_path}"


def _locator(span: SpanEvidence) -> str:
    where = _where(span)
    if span.coverage == "full":
        return f"span {span.ordinal}, {where} (whole span)"
    return (
        f"span {span.ordinal}, {where} (characters {span.relative_start}-{span.relative_end} "
        f"of {span.span_end - span.span_start})"
    )


# --- entry point ----------------------------------------------------------------------------


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
    stats = commands.add_parser("stats", help="chunk and token-budget statistics (offline)")
    stats.add_argument("--course-id", type=uuid.UUID, required=True)
    stats.add_argument("--json", action="store_true", help="one JSON object instead of text")
    search = commands.add_parser("search", help="rank a course's chunks for a question")
    search.add_argument("--course-id", type=uuid.UUID, required=True)
    search.add_argument("--query", required=True, help='the question, or "-" to read stdin')
    search.add_argument("-k", type=int, default=DEFAULT_K, help="results, 1-50 (default 5)")
    search.add_argument(
        "--type",
        dest="types",
        action="append",
        choices=[t.value for t in ResourceType],
        help="only this resource type (repeatable)",
    )
    search.add_argument("--json", action="store_true", help="one JSON object instead of text")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:  # usage error (64) or --help (0): returned, not raised
        return exc.code if isinstance(exc.code, int) else EXIT_USAGE
    human = args.command in {"search", "stats"} and not args.json

    def fail(code: int, error: str, **details: Any) -> int:
        if human:
            extra = "".join(f"; {key}: {value}" for key, value in details.items())
            print(f"error: {error}{extra}")
        else:
            _emit({"status": "error", "error": error, **details})
        return code

    try:
        settings = get_settings()
    except ValidationError as exc:
        fields = sorted({str(error["loc"][0]) for error in exc.errors()})
        return fail(EXIT_UNEXPECTED, "configuration", fields=fields)
    configure_logging(settings.log_level, stream=sys.stderr)
    try:
        if args.command == "index":
            return asyncio.run(_run_index(settings, args.course_id))
        if args.command == "status":
            return asyncio.run(_run_status(settings, args.course_id))
        if args.command == "stats":
            return asyncio.run(_run_stats(settings, args))
        return asyncio.run(_run_search(settings, args))
    except AIConfigurationError as exc:
        return fail(EXIT_NOT_CONFIGURED, "provider_not_configured", detail=str(exc))
    except CourseNotFoundError:
        return fail(EXIT_NOT_FOUND, "course_not_found")
    except InvalidSearchRequest as exc:
        return fail(EXIT_INVALID_REQUEST, "invalid_request", fields=list(exc.fields))
    except SearchProviderError as exc:
        return fail(
            EXIT_PROVIDER_FAILED,
            "query_embedding_failed",
            category=exc.category.value,
            model_call_id=exc.model_call_id,
        )
    except IndexStateError as exc:
        return fail(
            EXIT_INDEX_STATE,
            "index_state_inconsistent",
            corrupt_versions=[str(v) for v in exc.corrupt_version_ids],
            query_model_call_id=exc.query_model_call_id,
            hint="inspect with `status`; search refuses to run on this state",
        )
    except ProvenanceError as exc:
        return fail(EXIT_INDEX_STATE, "provenance_inconsistent", chunk_id=exc.chunk_id)
    except StatsIntegrityError as exc:
        return fail(
            EXIT_INDEX_STATE,
            "chunk_integrity_failed",
            versions=[str(v) for v in exc.version_ids],
        )
    except Exception as exc:  # noqa: BLE001 - one result, never a traceback
        # The exception type only: messages may quote document text.
        logger.error("cli.unexpected_error", extra={"error_type": type(exc).__name__})
        return fail(EXIT_UNEXPECTED, "unexpected", type=type(exc).__name__)


if __name__ == "__main__":
    sys.exit(main())
