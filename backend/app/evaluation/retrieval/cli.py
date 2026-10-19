# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Retrieval benchmark dataset tools (development).

    python -m app.evaluation.retrieval.cli hash --dataset <dir>
    python -m app.evaluation.retrieval.cli check --dataset <dir>
    python -m app.evaluation.retrieval.cli validate --dataset <dir>

``hash`` prints the dataset SHA-256 computed from the files (authoring: it
does not compare it with ``dataset.sha256``). ``check`` validates the files
offline, including the recorded hash. ``validate`` also ingests the corpus
with the production pipeline and resolves every quote, inside one database
transaction that is always rolled back, and prints a JSON review report:
inventory, resolved targets, distractors, forbidden versions, chunk counts
and the lexical-overlap audit.

None of these commands embeds, ranks or scores anything.

Exit codes: 0 ok; 1 unexpected error; 3 invalid dataset; 64 usage.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import sys
from collections.abc import Sequence
from fractions import Fraction
from pathlib import Path
from typing import Any

from app.core.config import get_settings
from app.db.session import create_db_engine, create_session_factory
from app.evaluation.retrieval.audit import lexical_overlap
from app.evaluation.retrieval.dataset import Dataset, DatasetError, load_dataset
from app.evaluation.retrieval.resolve import ResolvedDataset, validate_in_database

EXIT_OK = 0
EXIT_UNEXPECTED = 1
EXIT_INVALID_DATASET = 3
EXIT_USAGE = 64


def _json_default(value: object) -> object:
    if isinstance(value, Fraction):
        return f"{value.numerator}/{value.denominator}"
    return str(value)


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, default=_json_default, indent=2, sort_keys=True))


def inventory(dataset: Dataset) -> list[dict[str, Any]]:
    return [
        {
            "course": course.key,
            "resource": resource.key,
            "type": resource.type.value,
            "title": resource.title,
            "versions": [v.version for v in resource.versions],
            "current_version": resource.latest,
        }
        for course in dataset.manifest.courses
        for resource in course.resources
    ]


async def _validate(dataset: Dataset) -> dict[str, Any]:
    engine = create_db_engine(get_settings())
    try:
        session_factory = create_session_factory(engine)
        resolved = await validate_in_database(session_factory, dataset)
        async with session_factory() as session:
            audit = await lexical_overlap(session, dataset)
    finally:
        await engine.dispose()
    return _report(dataset, resolved, audit)


def _report(dataset: Dataset, resolved: ResolvedDataset, audit: Sequence[Any]) -> dict[str, Any]:
    by_id = {case.case_id: case for case in dataset.cases}

    def text(resolved_text: Any) -> dict[str, Any] | None:
        if resolved_text is None:
            return None
        ref = resolved_text.ref
        return {
            "target": f"{ref.course}/{ref.resource}/v{ref.version}",
            "quote": ref.quote,
            "offsets": [resolved_text.start, resolved_text.end],
            "span_ordinal": resolved_text.span_ordinal,
            "span_range": [resolved_text.relative_start, resolved_text.relative_end],
            "containing_chunks": list(resolved_text.containing_chunks),
            "overlapping_chunks": list(resolved_text.overlapping_chunks),
        }

    cases = []
    for case in resolved.cases:
        source = by_id[case.case_id]
        cases.append(
            {
                "case_id": case.case_id,
                "category": source.category.value,
                "query": source.query,
                "critical": source.critical,
                "resource_types": [t.value for t in source.resource_types or ()] or None,
                "target_groups": [[text(t) for t in group] for group in case.target_groups],
                "distractors": [
                    {"kind": d.kind.value, "ref": f"{d.course}/{d.resource}/v{d.version}"}
                    | ({"resolved": text(r)} if r is not None else {})
                    for d, r in zip(source.distractors, case.distractors, strict=True)
                ],
                "forbidden": ["/".join(map(str, key)) for key in case.forbidden_versions],
            }
        )
    return {
        "status": "ok",
        "dataset": dataset.manifest.dataset,
        "dataset_version": dataset.manifest.dataset_version,
        "sha256": dataset.sha256,
        "chunker": dataset.manifest.expected_chunker,
        "chunks": {
            "current_versions": resolved.chunks_current,
            "all_versions": resolved.chunks_all,
            "per_version": {"/".join(map(str, k)): n for k, n in resolved.chunk_counts.items()},
        },
        "inventory": inventory(dataset),
        "cases": cases,
        "lexical_overlap_audit": [dataclasses.asdict(row) for row in audit],
    }


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> Any:  # argparse would exit with 2
        _emit({"status": "error", "error": "usage", "detail": message})
        raise SystemExit(EXIT_USAGE)


def _parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(prog="python -m app.evaluation.retrieval.cli")
    commands = parser.add_subparsers(dest="command", required=True, parser_class=_ArgumentParser)
    for name, help_text in (
        ("hash", "print the computed dataset SHA-256"),
        ("check", "validate the dataset files offline"),
        ("validate", "ingest and resolve in a rolled-back transaction; print a review report"),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--dataset", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    root = args.dataset.resolve()
    try:
        if args.command == "hash":
            dataset = load_dataset(root, verify_recorded_sha=False)
            _emit({"status": "ok", "sha256": dataset.sha256})
        elif args.command == "check":
            dataset = load_dataset(root)
            _emit({"status": "ok", "sha256": dataset.sha256, "cases": len(dataset.cases)})
        else:
            _emit(asyncio.run(_validate(load_dataset(root))))
    except DatasetError as exc:
        _emit({"status": "error", "error": "invalid_dataset", "problems": list(exc.problems)})
        return EXIT_INVALID_DATASET
    except Exception as exc:  # noqa: BLE001 - report the type only
        _emit({"status": "error", "error": "unexpected", "type": type(exc).__name__})
        return EXIT_UNEXPECTED
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
