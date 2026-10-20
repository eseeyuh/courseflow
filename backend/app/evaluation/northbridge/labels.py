# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Golden labels for the Northbridge demo institution (``northbridge-label/v1``).

The labels are the expected truth about the synthetic corpus in
``demo_data/northbridge``: assessments, every stated deadline, requirements
and the original material that supports them, obligations, dependencies,
conflicts, changes between versions, statements that must be marked
``needs_review``, the prompt-injection sentence, an irrelevant resource,
and statements that look like deadlines but are not.

Every piece of evidence is an exact quote in one resource version. The
loader proves each one against the production parser's output: the quote
occurs exactly once (or at an explicit ``occurrence``) in that version's
extracted text, lies inside exactly one SourceSpan, and that span's locator
(PDF page, slide, or document section path) is the labelled one.

Freeze (``sha256-path-and-bytes/v1``, the retrieval benchmark's scheme): the
SHA-256 covers this dataset's ``manifest.json`` and ``golden.jsonl``, the
demo ``manifest.yaml`` and every corpus file it lists, by repository-relative
POSIX path. A frozen version is never edited; corrections are a new version.

    python -m app.evaluation.northbridge.labels check|hash
"""

from __future__ import annotations

import enum
import json
import sys
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)

from app.connectors.demo import MANIFEST_FILE, DemoManifest, load_manifest
from app.evaluation.retrieval.dataset import dataset_sha256
from app.evaluation.retrieval.resolve import occurrences, production_limits
from app.evaluation.retrieval.rules import DATASET_HASH_SCHEME, contains
from app.ingestion.dispatch import parse_source
from app.ingestion.schemas import RawSource
from app.ingestion.service import build_span_rows

REPO_ROOT = Path(__file__).resolve().parents[4]
DATASET_DIR = REPO_ROOT / "evals" / "datasets" / "northbridge" / "v1"
CORPUS_DIR = REPO_ROOT / "demo_data" / "northbridge"
LABELS_FILE = "golden.jsonl"
MANIFEST_JSON = "manifest.json"
SHA_FILE = "dataset.sha256"

Key = Annotated[str, StringConstraints(pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$", max_length=96)]
Text = Annotated[str, StringConstraints(min_length=1, max_length=1_000)]


class LabelError(ValueError):
    def __init__(self, problems: Sequence[str]) -> None:
        super().__init__(f"{len(problems)} label problem(s): " + "; ".join(problems))
        self.problems = tuple(problems)


class _Strict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Locator(_Strict):
    """Exactly one of: PDF page, slide (body or notes), or section path."""

    page: int | None = Field(default=None, ge=1)
    slide: int | None = Field(default=None, ge=1)
    section: Text | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> Locator:
        if sum(v is not None for v in (self.page, self.slide, self.section)) != 1:
            raise ValueError("a locator names exactly one of page, slide or section")
        return self


class Evidence(_Strict):
    resource: Key
    version: int = Field(ge=1)
    quote: Text
    locator: Locator
    occurrence: int | None = Field(default=None, ge=1)


class State(enum.StrEnum):
    INITIAL = "initial"  # as of the manifest's initial_sync_at
    FINAL = "final"  # after every version in the manifest


class LocalTime(_Strict):
    local: Annotated[str, StringConstraints(pattern=r"^\d{4}-\d\d-\d\dT\d\d:\d\d$")]
    timezone: Literal["Europe/London"]


class _Label(_Strict):
    label_id: Key
    schema_version: Literal["northbridge-label/v1"]
    course: Key
    notes: Text | None = None


class AssessmentLabel(_Label):
    kind: Literal["assessment"]
    assessment: Key
    title: Text
    weighting_percent: int | None = Field(ge=0, le=100)
    evidence: tuple[Evidence, ...] = Field(min_length=1)


class DeadlineClaim(_Label):
    """One source's statement of an assessment deadline."""

    kind: Literal["deadline_claim"]
    assessment: Key
    deadline: LocalTime
    states: tuple[State, ...] = Field(min_length=1)  # when this statement is in force
    superseded_by: Key | None = None  # the claim that replaces it (final state)
    evidence: tuple[Evidence, ...] = Field(min_length=1)


class RequirementLabel(_Label):
    kind: Literal["requirement"]
    assessment: Key
    summary: Text
    evidence: tuple[Evidence, ...] = Field(min_length=1)
    # Original teaching material that supports it: retrieval/mapping targets.
    materials: tuple[Evidence, ...] = ()


class ObligationLabel(_Label):
    kind: Literal["obligation"]
    summary: Text
    applies_to: Key | None = None  # an assessment
    hidden: bool  # stated somewhere other than the assessment's own brief
    states: tuple[State, ...] = Field(min_length=1)
    evidence: tuple[Evidence, ...] = Field(min_length=1)


class DependencyLabel(_Label):
    kind: Literal["dependency"]
    prerequisite: Text
    dependent: Text
    evidence: tuple[Evidence, ...] = Field(min_length=1)


class ConflictLabel(_Label):
    kind: Literal["conflict"]
    assessment: Key
    field: Literal["deadline"]
    state: State
    claims: tuple[Key, ...] = Field(min_length=2)


class ChangeLabel(_Label):
    kind: Literal["change"]
    resource: Key
    from_version: int = Field(ge=1)
    to_version: int = Field(ge=2)
    summary: Text
    before: Evidence | None = None
    after: Evidence
    affects: tuple[Key, ...] = Field(min_length=1)


class NeedsReviewLabel(_Label):
    """A statement that must not become a confident fact: the expected
    outcome is ``needs_review`` (abstention), never an invented obligation."""

    kind: Literal["needs_review"]
    reason: Text
    evidence: tuple[Evidence, ...] = Field(min_length=1)


class InjectionLabel(_Label):
    """Text that reads like an instruction to the model: course data only."""

    kind: Literal["prompt_injection"]
    evidence: tuple[Evidence, ...] = Field(min_length=1)


class IrrelevantLabel(_Label):
    kind: Literal["irrelevant_resource"]
    resource: Key
    version: int = Field(ge=1)
    not_relevant_to: tuple[Key, ...] = Field(min_length=1)  # requirement label ids


class NotADeadlineLabel(_Label):
    """A time statement that must not be extracted as an assessment deadline."""

    kind: Literal["not_a_deadline"]
    reason: Text
    evidence: tuple[Evidence, ...] = Field(min_length=1)


Label = Annotated[
    AssessmentLabel
    | DeadlineClaim
    | RequirementLabel
    | ObligationLabel
    | DependencyLabel
    | ConflictLabel
    | ChangeLabel
    | NeedsReviewLabel
    | InjectionLabel
    | IrrelevantLabel
    | NotADeadlineLabel,
    Field(discriminator="kind"),
]


class _LabelLine(BaseModel):
    label: Label


class LabelManifest(_Strict):
    schema_version: Literal["northbridge-dataset/v1"]
    dataset: Literal["northbridge-golden"]
    dataset_version: Literal["v1"]
    label_schema_version: Literal["northbridge-label/v1"]
    synthetic: Literal[True]
    institution: Literal["Northbridge University (fictional)"]
    corpus_manifest: Literal["demo_data/northbridge/manifest.yaml"]
    labels_file: Literal["golden.jsonl"]
    frozen_on: Annotated[str, StringConstraints(pattern=r"^\d{4}-\d\d-\d\d$")]
    dataset_hash_scheme: Literal["sha256-path-and-bytes/v1"]
    hash_inputs: Text
    label_counts: dict[str, int]
    description: Text


@dataclass(frozen=True, slots=True)
class GoldenDataset:
    manifest: LabelManifest
    corpus: DemoManifest
    labels: tuple[Any, ...]
    sha256: str

    def by_kind(self, kind: str) -> list[Any]:
        return [label for label in self.labels if label.kind == kind]


# --- loading and checking -----------------------------------------------------------------


def _parse_labels(raw: str) -> tuple[list[Any], list[str]]:
    problems, labels = [], []
    if not raw.endswith("\n"):
        problems.append(f"{LABELS_FILE}: must end with a newline")
    for number, line in enumerate(
        raw.split("\n")[:-1] if raw.endswith("\n") else raw.split("\n"), 1
    ):
        if not line.strip():
            problems.append(f"{LABELS_FILE} line {number}: blank")
            continue
        try:
            labels.append(_LabelLine.model_validate({"label": json.loads(line)}).label)
        except (json.JSONDecodeError, ValidationError) as exc:
            problems.append(f"{LABELS_FILE} line {number}: {type(exc).__name__}: {exc}"[:400])
    return labels, problems


def version_texts(
    corpus: DemoManifest,
) -> dict[tuple[str, str, int], tuple[str, list[dict[str, Any]]]]:
    """Each version's production extracted text and span rows."""
    out = {}
    for course in corpus.courses:
        for resource in course.resources:
            for version in resource.versions:
                path = CORPUS_DIR / version.file
                document = parse_source(
                    RawSource(
                        content=path.read_bytes(),
                        display_name=path.name,
                        source_uri=f"demo://{corpus.institution.key}/{course.ref}/{resource.key}",
                    ),
                    production_limits(),
                )
                out[(course.ref, resource.key, version.version)] = (
                    document.extracted_text,
                    build_span_rows(document),
                )
    return out


def evidence_refs(label: Any) -> list[tuple[str, Evidence]]:
    """Every evidence reference a label makes, with where it is in the label."""
    where = label.label_id
    refs: list[tuple[str, Evidence]] = []
    for name in ("evidence", "materials"):
        refs += [(f"{where} {name}[{i}]", r) for i, r in enumerate(getattr(label, name, ()))]
    for name in ("before", "after"):
        if getattr(label, name, None) is not None:
            refs.append((f"{where} {name}", getattr(label, name)))
    return refs


def check_evidence(
    course: str, ref: Evidence, texts: dict[tuple[str, str, int], Any], where: str
) -> str | None:
    key = (course, ref.resource, ref.version)
    if key not in texts:
        return f"{where}: no {course}/{ref.resource} v{ref.version} in the corpus"
    text, spans = texts[key]
    found = occurrences(text, ref.quote)
    if ref.occurrence is None and len(found) != 1:
        return f"{where}: quote occurs {len(found)} times in {course}/{ref.resource} v{ref.version}"
    if ref.occurrence is not None and ref.occurrence > len(found):
        return f"{where}: quote has no occurrence {ref.occurrence}"
    start = found[(ref.occurrence or 1) - 1]
    end = start + len(ref.quote)
    inside = [
        SimpleNamespace(**s)
        for s in spans
        if contains(s["start_offset"], s["end_offset"], start, end)
    ]
    if len(inside) != 1:
        return f"{where}: quote is not inside exactly one SourceSpan"
    span, loc = inside[0], ref.locator
    actual = (
        span.page_number if loc.page is not None
        else span.slide_number if loc.slide is not None
        else span.section_path
    )  # fmt: skip
    expected = (
        loc.page if loc.page is not None else loc.slide if loc.slide is not None else loc.section
    )
    if actual != expected:
        return f"{where}: quote is at {actual!r}, labelled {expected!r}"
    return None


def check_labels(corpus: DemoManifest, labels: Sequence[Any]) -> list[str]:
    problems: list[str] = []
    ids = Counter(label.label_id for label in labels)
    problems.extend(f"label_id {i!r} is used {n} times" for i, n in ids.items() if n > 1)
    by_id = {label.label_id: label for label in labels}
    courses = {c.ref: c for c in corpus.courses}
    assessments = {(a.course, a.assessment) for a in labels if a.kind == "assessment"}
    texts = version_texts(corpus)

    for label in labels:
        where = label.label_id
        if label.course not in courses:
            problems.append(f"{where}: unknown course {label.course!r}")
            continue
        for ref_where, ref in evidence_refs(label):
            problem = check_evidence(label.course, ref, texts, ref_where)
            if problem:
                problems.append(problem)

        assessment = getattr(label, "assessment", None) or getattr(label, "applies_to", None)
        if (
            label.kind != "assessment"
            and assessment
            and (label.course, assessment) not in assessments
        ):
            problems.append(f"{where}: unknown assessment {assessment!r}")
        linked = (
            list(getattr(label, "claims", ()))
            + list(getattr(label, "affects", ()))
            + list(getattr(label, "not_relevant_to", ()))
            + ([label.superseded_by] if getattr(label, "superseded_by", None) else [])
        )
        problems.extend(f"{where}: unknown label {i!r}" for i in linked if i not in by_id)
        if label.kind == "conflict":
            for claim in label.claims:
                other = by_id.get(claim)
                if other is not None and (
                    other.kind != "deadline_claim" or label.state not in other.states
                ):
                    problems.append(
                        f"{where}: {claim} is not a deadline claim in force at {label.state}"
                    )
        if label.kind == "change":
            resource = next(
                (r for r in courses[label.course].resources if r.key == label.resource), None
            )
            if resource is None or label.to_version > resource.active_version:
                problems.append(f"{where}: {label.resource} has no version {label.to_version}")
        if label.kind == "irrelevant_resource":
            for target in label.not_relevant_to:
                other = by_id.get(target)
                if other is not None and other.kind != "requirement":
                    problems.append(f"{where}: {target} is not a requirement")
    return problems


def hashed_paths(corpus: DemoManifest) -> list[str]:
    root = DATASET_DIR.relative_to(REPO_ROOT).as_posix()
    corpus_root = CORPUS_DIR.relative_to(REPO_ROOT).as_posix()
    paths = [f"{root}/{MANIFEST_JSON}", f"{root}/{LABELS_FILE}", f"{corpus_root}/{MANIFEST_FILE}"]
    paths += [
        f"{corpus_root}/{v.file}" for c in corpus.courses for r in c.resources for v in r.versions
    ]
    return sorted(paths, key=lambda p: p.encode("utf-8"))


def load_golden(*, verify_recorded_sha: bool = True) -> GoldenDataset:
    problems: list[str] = []
    try:
        manifest = LabelManifest.model_validate_json((DATASET_DIR / MANIFEST_JSON).read_bytes())
    except (OSError, ValidationError) as exc:
        raise LabelError([f"{MANIFEST_JSON}: {exc}"[:400]]) from None
    corpus = load_manifest(CORPUS_DIR)
    data = (DATASET_DIR / LABELS_FILE).read_bytes()
    if b"\r" in data:
        problems.append(f"{LABELS_FILE}: carriage returns are not allowed")
    labels, parse_problems = _parse_labels(data.decode("utf-8"))
    problems += parse_problems
    if not problems:
        problems += check_labels(corpus, labels)
        counts = dict(sorted(Counter(label.kind for label in labels).items()))
        if manifest.label_counts != counts:
            problems.append(f"manifest label_counts {manifest.label_counts} != {counts}")
    for path in hashed_paths(corpus):
        if b"\r" in (REPO_ROOT / path).read_bytes() and not path.endswith(
            (".pdf", ".docx", ".pptx")
        ):
            problems.append(f"{path}: carriage returns are not allowed")
    sha = dataset_sha256(REPO_ROOT, hashed_paths(corpus))
    if verify_recorded_sha:
        recorded = (
            (DATASET_DIR / SHA_FILE).read_text("ascii")
            if (DATASET_DIR / SHA_FILE).is_file()
            else ""
        )
        if recorded != f"{sha}\n":
            problems.append(f"{SHA_FILE}: recorded hash does not match (computed {sha})")
    if problems:
        raise LabelError(problems)
    return GoldenDataset(manifest, corpus, tuple(labels), sha)


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args not in (["check"], ["hash"]):
        print("usage: python -m app.evaluation.northbridge.labels check|hash")
        return 64
    try:
        dataset = load_golden(verify_recorded_sha=args == ["check"])
    except LabelError as exc:
        print(json.dumps({"status": "error", "problems": list(exc.problems)}, indent=2))
        return 3
    print(json.dumps({"status": "ok", "sha256": dataset.sha256, "labels": len(dataset.labels),
                      "scheme": DATASET_HASH_SCHEME}, indent=2))  # fmt: skip
    return 0


if __name__ == "__main__":
    sys.exit(main())
