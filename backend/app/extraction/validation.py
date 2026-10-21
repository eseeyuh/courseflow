# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Validate a course's stored intelligence: every persisted claim and every
piece of evidence, after the fact, with the same primitives the gates use.

    uv run python -m app.extraction.validation <course-id>

The gates decide what may be stored; this checks what *is* stored, so a bug
in a gate, a manual write or a later change cannot leave an unsupported
fact looking verified. It is read-only and deterministic, and it never
calls a model. Errors break the trust contract; warnings are true but need
attention (e.g. evidence only in superseded versions). Issues carry IDs and
codes only, never document text.

Claim status stays ``VerificationStatus``: ``verified`` and ``needs_review``
are stored; ``rejected`` exists only at the gate (never stored). A claim
whose evidence is all in superseded versions is reported as
``stale_source_version`` (and flagged by the API), not a stored state, until
change propagation exists. Uniqueness of claims and links is enforced by the
database (canonical-key and fact-quote constraints), so it is not re-checked
here.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from collections import Counter
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Assessment,
    AssessmentDeadline,
    AssessmentRequirement,
    EvidenceLink,
    ModelCall,
    Obligation,
    Resource,
    ResourceVersion,
    SourceSpan,
    WorkflowRun,
    workflow_run_inputs,
)
from app.domain.enums import EvidenceField, VerificationStatus
from app.extraction.gate import canonical_key, injection_fragments
from app.extraction.statements import statement_key

V = VerificationStatus


class Severity(StrEnum):
    ERROR = "error"
    WARNING = "warning"


class IssueCode(StrEnum):
    # Errors: the stored state breaks the evidence contract.
    EVIDENCE_CROSS_COURSE = "evidence_cross_course"  # the span is in another course
    QUOTE_OUT_OF_BOUNDS = "quote_out_of_bounds"  # offsets beyond the span's excerpt
    QUOTE_BLANK = "quote_blank"  # the quoted text is only whitespace
    EVIDENCE_ON_INJECTION = "evidence_on_injection"  # verified evidence on injected text
    CLAIM_WITHOUT_EVIDENCE = "claim_without_evidence"
    VERIFIED_WITHOUT_VERIFIED_EVIDENCE = "verified_without_verified_evidence"
    LINEAGE_MISSING = "lineage_missing"  # no ModelCall or WorkflowRun recorded
    # The call, the run, the claim's course and the run's input versions disagree.
    LINEAGE_INCONSISTENT = "lineage_inconsistent"
    INSTANT_MISMATCH = "instant_mismatch"  # due_at is not the local date/time in its zone
    CANONICAL_KEY_MISMATCH = "canonical_key_mismatch"  # the key is not its text's key
    # Warnings.
    STALE_SOURCE_VERSION = "stale_source_version"  # verified only by superseded versions


SEVERITY = {code: Severity.WARNING for code in (IssueCode.STALE_SOURCE_VERSION,)}


@dataclass(frozen=True, slots=True)
class Issue:
    code: IssueCode
    subject_type: str  # assessment, assessment_deadline, assessment_requirement, obligation
    subject_id: uuid.UUID
    link_id: uuid.UUID | None = None

    @property
    def severity(self) -> Severity:
        return SEVERITY.get(self.code, Severity.ERROR)


@dataclass(slots=True)
class ValidationReport:
    course_id: uuid.UUID
    claims: dict[str, dict[str, int]] = field(default_factory=dict)  # kind -> status -> n
    issues: list[Issue] = field(default_factory=list)

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.severity is Severity.ERROR]

    @property
    def warnings(self) -> list[Issue]:
        return [i for i in self.issues if i.severity is Severity.WARNING]

    @property
    def clean(self) -> bool:
        return not self.errors

    def counts(self) -> dict[str, int]:
        return dict(sorted(Counter(i.code.value for i in self.issues).items()))

    def as_dict(self) -> dict[str, Any]:
        return {
            "course_id": str(self.course_id),
            "clean": self.clean,
            "claims": self.claims,
            "counts": self.counts(),
            "errors": [_issue(i) for i in self.errors],
            "warnings": [_issue(i) for i in self.warnings],
        }


def _issue(issue: Issue) -> dict[str, Any]:
    out = asdict(issue)
    return {k: str(v) if isinstance(v, uuid.UUID) else v for k, v in out.items()}


@dataclass(frozen=True, slots=True)
class _Link:
    link: EvidenceLink
    subject_type: str
    subject_id: uuid.UUID
    excerpt: str
    version_id: uuid.UUID
    resource_course_id: uuid.UUID
    current: bool


_SUBJECTS = {
    "assessment": EvidenceLink.assessment_id,
    "assessment_deadline": EvidenceLink.assessment_deadline_id,
    "assessment_requirement": EvidenceLink.assessment_requirement_id,
    "obligation": EvidenceLink.obligation_id,
}


async def _links(session: AsyncSession, subject_type: str, ids: Sequence[uuid.UUID]) -> list[_Link]:
    column = _SUBJECTS[subject_type]
    rows = await session.execute(
        select(EvidenceLink, SourceSpan.excerpt, ResourceVersion.id, Resource)
        .join(SourceSpan, SourceSpan.id == EvidenceLink.source_span_id)
        .join(ResourceVersion, ResourceVersion.id == SourceSpan.resource_version_id)
        .join(Resource, Resource.id == ResourceVersion.resource_id)
        .where(column.in_(ids))
    )
    return [
        _Link(
            link, subject_type, getattr(link, column.key), excerpt, version_id,
            resource.course_id, resource.current_version_id == version_id,
        )
        for link, excerpt, version_id, resource in rows
    ]  # fmt: skip


def _evidence_issues(link: _Link, course_id: uuid.UUID) -> list[Issue]:
    issues = []

    def add(code: IssueCode) -> None:
        issues.append(Issue(code, link.subject_type, link.subject_id, link.link.id))

    if link.resource_course_id != course_id:
        add(IssueCode.EVIDENCE_CROSS_COURSE)
    start, end = link.link.quote_start, link.link.quote_end
    if end > len(link.excerpt):
        add(IssueCode.QUOTE_OUT_OF_BOUNDS)
    elif not link.excerpt[start:end].strip():
        add(IssueCode.QUOTE_BLANK)
    elif link.link.verification_status is V.VERIFIED and any(
        a < end and start < b for a, b in injection_fragments(link.excerpt)
    ):
        add(IssueCode.EVIDENCE_ON_INJECTION)
    if link.link.model_call_id is None or link.link.workflow_run_id is None:
        add(IssueCode.LINEAGE_MISSING)
    return issues


async def _lineage_issues(
    session: AsyncSession, links: Sequence[_Link], course_id: uuid.UUID
) -> list[Issue]:
    """Each link's ModelCall belongs to its run, the run to the claim's course,
    and the evidence's version was one of the run's inputs."""
    call_ids = {lk.link.model_call_id for lk in links if lk.link.model_call_id}
    run_ids = {lk.link.workflow_run_id for lk in links if lk.link.workflow_run_id}
    call_runs = dict(
        (await session.execute(
            select(ModelCall.id, ModelCall.workflow_run_id).where(ModelCall.id.in_(call_ids))
        )).all()
    )  # fmt: skip
    run_courses = dict(
        (await session.execute(
            select(WorkflowRun.id, WorkflowRun.course_id).where(WorkflowRun.id.in_(run_ids))
        )).all()
    )  # fmt: skip
    inputs = set(
        (await session.execute(
            select(workflow_run_inputs.c.run_id, workflow_run_inputs.c.resource_version_id)
            .where(workflow_run_inputs.c.run_id.in_(run_ids))
        )).all()
    )  # fmt: skip
    issues = []
    for lk in links:
        call, run = lk.link.model_call_id, lk.link.workflow_run_id
        if call is None or run is None:
            continue  # reported as lineage_missing
        if (
            call_runs.get(call) != run
            or run_courses.get(run) != course_id
            or (run, lk.version_id) not in inputs
        ):
            issues.append(
                Issue(IssueCode.LINEAGE_INCONSISTENT, lk.subject_type, lk.subject_id, lk.link.id)
            )
    return issues


def _expected_instant(row: Any, date_field: str, time_field: str, zone_field: str) -> Any:
    local_date, local_time, zone = (getattr(row, f) for f in (date_field, time_field, zone_field))
    if local_date is None or local_time is None or zone is None:
        return None
    try:
        return datetime.combine(local_date, local_time, tzinfo=ZoneInfo(zone)).astimezone(UTC)
    except (ZoneInfoNotFoundError, ValueError):
        return None


def _status_counts(rows: Sequence[Any]) -> dict[str, int]:
    return dict(sorted(Counter(row.status.value for row in rows).items()))


async def validate_course_intelligence(
    session: AsyncSession, course_id: uuid.UUID
) -> ValidationReport:
    """Every stored claim of the course and its evidence, checked."""
    report = ValidationReport(course_id)
    assessments = (
        await session.scalars(select(Assessment).where(Assessment.course_id == course_id))
    ).all()
    assessment_ids = [a.id for a in assessments]
    deadlines = (
        await session.scalars(
            select(AssessmentDeadline).where(AssessmentDeadline.assessment_id.in_(assessment_ids))
        )
    ).all()
    requirements = (
        await session.scalars(
            select(AssessmentRequirement).where(
                AssessmentRequirement.assessment_id.in_(assessment_ids)
            )
        )
    ).all()
    obligations = (
        await session.scalars(select(Obligation).where(Obligation.course_id == course_id))
    ).all()
    claims: dict[str, Sequence[Any]] = {
        "assessment": assessments,
        "assessment_deadline": deadlines,
        "assessment_requirement": requirements,
        "obligation": obligations,
    }
    report.claims = {kind: _status_counts(rows) for kind, rows in claims.items()}
    links: dict[str, list[_Link]] = {
        kind: await _links(session, kind, [r.id for r in rows]) for kind, rows in claims.items()
    }
    every_link = [lk for kind_links in links.values() for lk in kind_links]
    for lk in every_link:
        report.issues += _evidence_issues(lk, course_id)
    report.issues += await _lineage_issues(session, every_link, course_id)

    def add(code: IssueCode, kind: str, row_id: uuid.UUID) -> None:
        report.issues.append(Issue(code, kind, row_id))

    by_subject: dict[uuid.UUID, list[_Link]] = {}
    for lk in every_link:
        by_subject.setdefault(lk.subject_id, []).append(lk)
    verified_deadlines = {
        d.assessment_id
        for d in deadlines
        if d.status is V.VERIFIED
        and any(lk.link.verification_status is V.VERIFIED for lk in by_subject.get(d.id, []))
    }
    for kind, rows in claims.items():
        for row in rows:
            own = by_subject.get(row.id, [])
            primary = [
                lk for lk in own
                if kind != "obligation" or lk.link.field is EvidenceField.OBLIGATION
            ]  # fmt: skip
            if kind == "assessment":
                primary = [lk for lk in own if lk.link.field is EvidenceField.TITLE]
            if not primary:
                add(IssueCode.CLAIM_WITHOUT_EVIDENCE, kind, row.id)
            if row.status is V.VERIFIED:
                if kind == "assessment":
                    supported = row.id in verified_deadlines or any(
                        lk.link.field is EvidenceField.WEIGHTING
                        and lk.link.verification_status is V.VERIFIED
                        for lk in own
                    )
                else:
                    supported = any(lk.link.verification_status is V.VERIFIED for lk in primary)
                if not supported:
                    add(IssueCode.VERIFIED_WITHOUT_VERIFIED_EVIDENCE, kind, row.id)
                if own and not any(lk.current for lk in own):
                    add(IssueCode.STALE_SOURCE_VERSION, kind, row.id)
    for a in assessments:
        if canonical_key(a.title) != a.canonical_key:
            add(IssueCode.CANONICAL_KEY_MISMATCH, "assessment", a.id)
    for r in requirements:
        if statement_key(r.text) != r.canonical_key:
            add(IssueCode.CANONICAL_KEY_MISMATCH, "assessment_requirement", r.id)
    for d in deadlines:
        expected = _expected_instant(d, "due_date", "due_time", "timezone")
        if d.due_at is not None and expected is not None and d.due_at != expected:
            add(IssueCode.INSTANT_MISMATCH, "assessment_deadline", d.id)
    for o in obligations:
        expected = _expected_instant(o, "due_date", "due_time", "due_timezone")
        if o.due_at is not None and expected is not None and o.due_at != expected:
            add(IssueCode.INSTANT_MISMATCH, "obligation", o.id)
        if o.model_call_id is None:
            add(IssueCode.LINEAGE_MISSING, "obligation", o.id)
    report.issues.sort(key=lambda i: (i.code.value, i.subject_type, str(i.subject_id)))
    return report


def main(argv: Sequence[str] | None = None) -> int:
    from app.core.config import get_settings
    from app.db.session import create_db_engine, create_session_factory

    parser = argparse.ArgumentParser(prog="python -m app.extraction.validation")
    parser.add_argument("course_id", type=uuid.UUID)
    args = parser.parse_args(argv)

    async def run() -> ValidationReport:
        engine = create_db_engine(get_settings())
        try:
            async with create_session_factory(engine)() as session:
                return await validate_course_intelligence(session, args.course_id)
        finally:
            await engine.dispose()

    report = asyncio.run(run())
    print(json.dumps(report.as_dict(), indent=2))
    return 0 if report.clean else 1


if __name__ == "__main__":
    raise SystemExit(main())
