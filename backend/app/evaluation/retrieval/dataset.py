# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Load a frozen retrieval dataset from disk and check it without a database.

Pure file work: no database, no provider, no network. ``load_dataset``
parses ``manifest.json`` and ``cases.jsonl``, checks every rule that relates
them (``check_dataset``), checks the corpus files, and recomputes the
dataset SHA-256, which must equal ``dataset.sha256``. Every problem found is
reported at once (DatasetError), never just the first.

Dataset hash (``sha256-path-and-bytes/v1``, defined byte for byte in
``dataset_sha256`` and ``rules.DEFINITIONS``): SHA-256 over
``manifest.json``, ``cases.jsonl`` and every corpus file the manifest lists.
``dataset.sha256`` (the result) and ``README.md`` are not inputs. The hashed
files must be UTF-8 without carriage returns, so a checkout's line endings
cannot change the hash.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.evaluation.retrieval.rules import DATASET_HASH_SCHEME, DEFINITIONS
from app.evaluation.retrieval.schema import (
    CROSS_COURSE_DISTRACTORS,
    CaseCategory,
    CourseEntry,
    DistractorKind,
    ForbiddenKind,
    Manifest,
    ResourceEntry,
    RetrievalCase,
    TextRef,
)

MANIFEST_FILE = "manifest.json"
CASES_FILE = "cases.jsonl"
SHA_FILE = "dataset.sha256"
README_FILE = "README.md"
CORPUS_DIR = "corpus"
HASH_HEADER = DATASET_HASH_SCHEME.encode("ascii") + b"\n"


class DatasetError(ValueError):
    """The dataset is invalid. ``problems`` lists every rule it breaks."""

    def __init__(self, problems: Sequence[str]) -> None:
        super().__init__(f"{len(problems)} dataset problem(s): " + "; ".join(problems))
        self.problems = tuple(problems)


@dataclass(frozen=True, slots=True)
class Dataset:
    root: Path
    manifest: Manifest
    cases: tuple[RetrievalCase, ...]
    sha256: str

    def resource(self, course: str, resource: str) -> ResourceEntry:
        return _resources(self.manifest)[(course, resource)]

    def corpus_file(self, course: str, resource: str, version: int) -> Path:
        entry = self.resource(course, resource)
        (match,) = [v for v in entry.versions if v.version == version]
        return self.root / match.file


# --- loading --------------------------------------------------------------------------------


def load_dataset(root: Path, *, verify_recorded_sha: bool = True) -> Dataset:
    """Parse, check and hash the dataset at ``root``; DatasetError if anything
    is wrong. ``verify_recorded_sha=False`` is for authoring only: it computes
    the hash without comparing it with ``dataset.sha256``."""
    problems: list[str] = []
    manifest = _parse_manifest(root / MANIFEST_FILE, problems)
    cases = _parse_cases(root / CASES_FILE, problems)
    if manifest is None:
        raise DatasetError(problems)
    problems.extend(check_dataset(manifest, cases))
    problems.extend(_check_files(root, manifest))
    sha = None
    if not problems:
        sha = dataset_sha256(root, hashed_paths(manifest))
        if verify_recorded_sha:
            problems.extend(_check_recorded_sha(root, sha))
    if problems or sha is None:
        raise DatasetError(problems)
    return Dataset(root=root, manifest=manifest, cases=tuple(cases), sha256=sha)


def _parse_manifest(path: Path, problems: list[str]) -> Manifest | None:
    raw = _read_text(path, problems)
    if raw is None:
        return None
    if not _json_without_duplicate_keys(raw, MANIFEST_FILE, problems):
        return None
    try:
        return Manifest.model_validate_json(raw)
    except ValidationError as exc:
        problems.extend(f"{MANIFEST_FILE}: {_describe(error)}" for error in exc.errors())
        return None


def _parse_cases(path: Path, problems: list[str]) -> list[RetrievalCase]:
    raw = _read_text(path, problems)
    if raw is None:
        return []
    if not raw.endswith("\n"):
        problems.append(f"{CASES_FILE}: must end with a newline")
    lines = raw.split("\n")
    if raw.endswith("\n"):
        lines.pop()
    cases = []
    for number, line in enumerate(lines, start=1):
        where = f"{CASES_FILE} line {number}"
        if not line.strip():
            problems.append(f"{where}: blank lines are not allowed")
            continue
        if not _json_without_duplicate_keys(line, where, problems):
            continue
        try:
            cases.append(RetrievalCase.model_validate_json(line))
        except ValidationError as exc:
            problems.extend(f"{where}: {_describe(error)}" for error in exc.errors())
    return cases


def _read_text(path: Path, problems: list[str]) -> str | None:
    try:
        data = path.read_bytes()
    except OSError:
        problems.append(f"{path.name}: missing or unreadable")
        return None
    return _decode(data, path.name, problems)


def _decode(data: bytes, name: str, problems: list[str]) -> str | None:
    if b"\r" in data:
        problems.append(f"{name}: carriage returns are not allowed (LF line endings only)")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        problems.append(f"{name}: not valid UTF-8")
        return None


def _json_without_duplicate_keys(raw: str, where: str, problems: list[str]) -> bool:
    duplicates: list[str] = []

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        keys = [key for key, _ in items]
        duplicates.extend(key for key, count in Counter(keys).items() if count > 1)
        return dict(items)

    try:
        json.loads(raw, object_pairs_hook=pairs)
    except json.JSONDecodeError as exc:
        problems.append(f"{where}: invalid JSON ({exc.msg})")
        return False
    if duplicates:
        problems.append(f"{where}: duplicate JSON key(s) {sorted(set(duplicates))}")
        return False
    return True


def _describe(error: Any) -> str:
    location = ".".join(str(part) for part in error["loc"]) or "(root)"
    return f"{location}: {error['msg']}"


# --- cross-file rules -----------------------------------------------------------------------


def _resources(manifest: Manifest) -> dict[tuple[str, str], ResourceEntry]:
    return {
        (course.key, resource.key): resource
        for course in manifest.courses
        for resource in course.resources
    }


def check_dataset(manifest: Manifest, cases: Sequence[RetrievalCase]) -> list[str]:
    """Every rule relating the manifest and the cases; an empty list when the
    dataset is consistent."""
    problems = _check_manifest(manifest)
    resources = _resources(manifest)
    courses = {course.key for course in manifest.courses}

    ids = Counter(case.case_id for case in cases)
    problems.extend(f"case_id {cid!r} is used {n} times" for cid, n in ids.items() if n > 1)
    for case in cases:
        problems.extend(f"{case.case_id}: {p}" for p in _check_case(case, courses, resources))

    categories = Counter(case.category for case in cases)
    if len(cases) != manifest.case_count:
        problems.append(f"manifest case_count {manifest.case_count} != {len(cases)} cases")
    declared = {CaseCategory(k): v for k, v in manifest.category_counts.items()}
    actual = {category: categories.get(category, 0) for category in CaseCategory}
    if declared != actual:
        problems.append(f"manifest category_counts {declared} != actual {actual}")
    positive = sum(case.is_positive for case in cases)
    if manifest.positive_case_count != positive:
        problems.append(f"manifest positive_case_count != {positive}")
    if manifest.invariant_case_count != len(cases) - positive:
        problems.append(f"manifest invariant_case_count != {len(cases) - positive}")

    critical = [case.case_id for case in cases if case.critical]
    if len(critical) != 1:
        problems.append(f"exactly one case must be critical, found {len(critical)}")
    elif critical[0] != manifest.critical_case.case_id:
        problems.append(
            f"manifest critical_case {manifest.critical_case.case_id!r} != {critical[0]!r}"
        )

    kinds = {d.kind for case in cases for d in case.distractors}
    missing = sorted(kind.value for kind in DistractorKind if kind not in kinds)
    if missing:
        problems.append(f"the dataset labels no distractor of kind(s) {missing}")
    return problems


def _check_manifest(manifest: Manifest) -> list[str]:
    problems = []
    named = (manifest.relevance_rule, manifest.target_span_rule, manifest.dataset_hash_scheme)
    if manifest.definitions != {rule: DEFINITIONS[rule] for rule in named}:
        problems.append(
            f"manifest definitions must be exactly the canonical definitions of {list(named)}"
        )
    keys = Counter(course.key for course in manifest.courses)
    problems.extend(f"course key {k!r} is declared {n} times" for k, n in keys.items() if n > 1)
    for course in manifest.courses:
        problems.extend(_check_course(course))
    return problems


def _check_course(course: CourseEntry) -> list[str]:
    problems = []
    keys = Counter(resource.key for resource in course.resources)
    problems.extend(
        f"resource {course.key}/{k} is declared {n} times" for k, n in keys.items() if n > 1
    )
    for resource in course.resources:
        numbers = [v.version for v in resource.versions]
        if numbers != list(range(1, len(numbers) + 1)):
            problems.append(f"{course.key}/{resource.key}: versions must be 1..n in order")
        for entry in resource.versions:
            expected = f"{CORPUS_DIR}/{course.key}/{resource.key}/v{entry.version}.html"
            if entry.file != expected:
                problems.append(
                    f"{course.key}/{resource.key} v{entry.version}: file must be {expected}"
                )
    return problems


def _check_ref(
    ref: TextRef | Any,
    resources: dict[tuple[str, str], ResourceEntry],
) -> tuple[ResourceEntry | None, str | None]:
    resource = resources.get((ref.course, ref.resource))
    if resource is None:
        return None, f"unknown resource {ref.course}/{ref.resource}"
    if ref.version is not None and ref.version > resource.latest:
        return None, f"{ref.course}/{ref.resource} has no version {ref.version}"
    return resource, None


def _check_case(
    case: RetrievalCase,
    courses: set[str],
    resources: dict[tuple[str, str], ResourceEntry],
) -> list[str]:
    problems = []
    if case.course not in courses:
        return [f"unknown course {case.course!r}"]
    for group in case.target_groups:
        for ref in group.alternatives:
            resource, problem = _check_ref(ref, resources)
            if problem or resource is None:
                problems.append(f"target {group.group_id}: {problem}")
                continue
            if ref.course != case.course:
                problems.append(f"target {group.group_id}: targets must be in the case's course")
            if ref.version != resource.latest:
                problems.append(
                    f"target {group.group_id}: {ref.course}/{ref.resource} v{ref.version} is not "
                    f"the current version (v{resource.latest}); only current versions are searchable"
                )
            if case.resource_types is not None and resource.type not in case.resource_types:
                problems.append(
                    f"target {group.group_id}: resource type {resource.type.value} is excluded "
                    "by the case's resource_types filter"
                )
    for distractor in case.distractors:
        resource, problem = _check_ref(distractor, resources)
        if problem or resource is None:
            problems.append(f"distractor: {problem}")
            continue
        same_course = distractor.course == case.course
        current = distractor.version == resource.latest
        if distractor.kind in CROSS_COURSE_DISTRACTORS and same_course:
            problems.append(f"distractor {distractor.kind.value} must be in another course")
        if distractor.kind not in CROSS_COURSE_DISTRACTORS and not same_course:
            problems.append(f"distractor {distractor.kind.value} must be in the case's course")
        if (distractor.kind is DistractorKind.STALE_VERSION) == current:
            problems.append(
                f"distractor {distractor.kind.value} on {distractor.resource} v{distractor.version}: "
                "stale_version distractors must name a superseded version, others the current one"
            )
    for forbidden in case.forbidden:
        resource, problem = _check_ref(forbidden, resources)
        if problem or resource is None:
            problems.append(f"forbidden: {problem}")
            continue
        if forbidden.kind is ForbiddenKind.STALE_VERSION:
            if forbidden.course != case.course:
                problems.append("forbidden stale_version must be in the case's course")
            if forbidden.version is None or forbidden.version >= resource.latest:
                problems.append("forbidden stale_version must name a superseded version")
        elif forbidden.course == case.course:
            problems.append("forbidden foreign_course must be in another course")
    return problems


# --- files and hash -------------------------------------------------------------------------


def hashed_paths(manifest: Manifest) -> list[str]:
    """Relative POSIX paths of every hash input, in hash order."""
    paths = [MANIFEST_FILE, CASES_FILE]
    paths += [
        entry.file
        for course in manifest.courses
        for resource in course.resources
        for entry in resource.versions
    ]
    return sorted(paths, key=lambda p: p.encode("utf-8"))


def dataset_sha256(root: Path, paths: Iterable[str]) -> str:
    """``sha256-path-and-bytes/v1``: the header line, then for each path in
    ascending bytewise order of its UTF-8 encoding: the path bytes, NUL, the
    file's byte length in ASCII decimal (no leading zeros), NUL, the file's
    bytes. Paths cannot contain NUL and file contents are length-prefixed, so
    the encoding is unambiguous. Returns lowercase hexadecimal."""
    digest = hashlib.sha256(HASH_HEADER)
    for path in sorted(paths, key=lambda p: p.encode("utf-8")):
        data = (root / path).read_bytes()
        digest.update(path.encode("utf-8") + b"\0" + str(len(data)).encode("ascii") + b"\0")
        digest.update(data)
    return digest.hexdigest()


def _check_files(root: Path, manifest: Manifest) -> list[str]:
    """Listed corpus files exist as regular files, are UTF-8 with LF line
    endings, and nothing unlisted hides in the corpus directory."""
    problems: list[str] = []
    listed = {
        entry.file
        for course in manifest.courses
        for resource in course.resources
        for entry in resource.versions
    }
    for relative in sorted(listed):
        path = root / relative
        if path.is_symlink() or not path.is_file():
            problems.append(f"{relative}: missing or not a regular file")
            continue
        _decode(path.read_bytes(), relative, problems)
    corpus = root / CORPUS_DIR
    present = (
        {p.relative_to(root).as_posix() for p in corpus.rglob("*") if not p.is_dir()}
        if corpus.is_dir()
        else set()
    )
    problems.extend(f"{extra}: not listed in the manifest" for extra in sorted(present - listed))
    return problems


def _check_recorded_sha(root: Path, sha: str) -> list[str]:
    try:
        recorded = (root / SHA_FILE).read_text(encoding="ascii")
    except (OSError, UnicodeDecodeError):
        return [f"{SHA_FILE}: missing or unreadable"]
    if recorded != f"{sha}\n":
        return [f"{SHA_FILE}: recorded hash does not match the dataset (computed {sha})"]
    return []
