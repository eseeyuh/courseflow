# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Course intelligence validation and adversarial model output, end to end
on PostgreSQL with a scripted fake model: a clean pipeline validates clean;
hallucinated spans, malformed dates and duplicate reruns never reach the
store; tampered evidence, lineage and values are reported as errors."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import (
    Assessment,
    AssessmentDeadline,
    AssessmentRequirement,
    EvidenceLink,
    Obligation,
    SourceSpan,
)
from app.domain.enums import (
    DeadlinePrecision,
    EvidenceField,
    EvidenceSubject,
    ResourceType,
    TimezoneSource,
    VerificationStatus,
)
from app.extraction.claims_service import ClaimExtractionService
from app.extraction.service import AssessmentExtractionService
from app.extraction.validation import IssueCode, validate_course_intelligence
from tests.test_assessment_extraction_service import FakeModel, _page
from tests.test_claim_extraction_service import (
    BRIEF,
    HANDBOOK_V1,
    INJECTION,
    LECTURE,
    SUPERVISOR,
    Course_,
    _cite,
    _obligation,
    obligations_script,
    requirements_script,
)

pytestmark = pytest.mark.anyio

V = VerificationStatus
DEADLINE = "Deadline: Monday 30 November 2026, 12:00 (UK time)."
BRIEF_WITH_DEADLINE = BRIEF.replace(b"</ol>", f"</ol><p>{DEADLINE}</p>".encode())


def _assessments(user: str, due_date: str = "2026-11-30") -> dict[str, Any]:
    if "Your specification must include" not in user:
        return {"assessments": []}
    return {
        "assessments": [
            {
                "title": "Project Specification",
                "title_evidence": _cite(user, "Project Specification"),
                "weighting": None,
                "deadline": {
                    "status": "stated",
                    "due_date": due_date,
                    "due_time": "12:00",
                    "evidence": _cite(user, DEADLINE),
                },
            }
        ]
    }


def respond(user: str) -> dict[str, Any]:
    """One fake for all three tasks, told apart by the task line."""
    if user.startswith("Extract the assessments"):
        return _assessments(user)
    if user.startswith("Extract the assessment requirements"):
        return requirements_script(user)
    return obligations_script()(user)


@pytest.fixture
def sessions(committed_session: AsyncSession) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(committed_session.bind, expire_on_commit=False)


@pytest.fixture
async def course(sessions: async_sessionmaker[AsyncSession], tmp_path: Path) -> Course_:
    course = Course_(sessions, tmp_path)
    await course.create()
    await course.ingest("brief", BRIEF_WITH_DEADLINE, ResourceType.BRIEF)
    await course.ingest("handbook", HANDBOOK_V1, ResourceType.HANDBOOK)
    await course.ingest("lecture", LECTURE, ResourceType.LECTURE)
    return course


async def _extract(sessions: async_sessionmaker[AsyncSession], course: Course_, fake: Any) -> None:
    model = FakeModel(fake)
    await AssessmentExtractionService(sessions, model).extract_course(course.id)
    for task in ("requirements", "obligations"):
        await ClaimExtractionService(sessions, model, task).extract_course(course.id)  # type: ignore[arg-type]


async def _validate(sessions: async_sessionmaker[AsyncSession], course: Course_) -> Any:
    async with sessions() as session:
        return await validate_course_intelligence(session, course.id)


async def _counts(sessions: async_sessionmaker[AsyncSession]) -> list[int]:
    async with sessions() as session:
        return [
            int(await session.scalar(select(func.count()).select_from(model)) or 0)
            for model in (
                Assessment, AssessmentDeadline, AssessmentRequirement, Obligation, EvidenceLink
            )
        ]  # fmt: skip


# --- a clean pipeline -------------------------------------------------------------------


async def test_a_full_extraction_validates_clean_and_a_rerun_adds_nothing(
    sessions: async_sessionmaker[AsyncSession], course: Course_
) -> None:
    await _extract(sessions, course, respond)
    report = await _validate(sessions, course)
    assert report.clean and report.issues == []
    assert report.claims["assessment"] == {"verified": 1}
    assert report.claims["assessment_deadline"] == {"verified": 1}
    assert report.claims["assessment_requirement"] == {"verified": 2}
    assert report.claims["obligation"] == {"needs_review": 1, "verified": 1}
    before = await _counts(sessions)

    await _extract(sessions, course, respond)  # the same output again
    assert await _counts(sessions) == before
    assert (await _validate(sessions, course)).clean


# --- adversarial model output never reaches the store -----------------------------------


async def test_hallucinated_span_ids_are_rejected_and_nothing_is_stored(
    sessions: async_sessionmaker[AsyncSession], course: Course_
) -> None:
    def hallucinating(user: str) -> dict[str, Any]:
        if user.startswith("Extract the assessments"):
            answer = _assessments(user)
            for a in answer["assessments"]:
                a["title_evidence"] = {"span": "S99", "quote": "Project Specification"}
                a["deadline"]["evidence"] = {"span": "S99", "quote": DEADLINE}
            return answer
        if user.startswith("Extract the assessment requirements"):
            return {"requirements": []}
        return {
            "obligations": [
                _obligation({"span": "S99", "quote": SUPERVISOR}, category="other", assessment=None)
            ]
        }

    await _extract(sessions, course, hallucinating)
    assert await _counts(sessions) == [0, 0, 0, 0, 0]


@pytest.mark.parametrize("due_date", ["2026-13-01", "2026-02-30", "2026-11-31"])
async def test_a_malformed_model_date_never_persists(
    sessions: async_sessionmaker[AsyncSession], course: Course_, due_date: str
) -> None:
    result = await AssessmentExtractionService(
        sessions, FakeModel(lambda user: _assessments(user, due_date))
    ).extract_course(course.id)
    assert result.summary.deadlines_created == 0
    assert set(result.summary.rejected) <= {
        "deadline:model_date_invalid",
        "deadline:model_date_mismatch",
    }
    assert result.summary.rejected  # the bad date was refused, not crashed on

    stated = {"status": "stated", "due_date": due_date, "due_time": None}

    def bad_due(user: str) -> dict[str, Any]:
        if "Dissertation handbook" not in user:
            return {"obligations": []}
        return {
            "obligations": [
                _obligation(_cite(user, SUPERVISOR), category="supervisor_review", assessment=None)
                | {"due": stated | {"evidence": _cite(user, SUPERVISOR)}}
            ]
        }

    await ClaimExtractionService(sessions, FakeModel(bad_due), "obligations").extract_course(
        course.id
    )
    async with sessions() as session:
        (obligation,) = (await session.scalars(select(Obligation))).all()
    assert obligation.due_date is None  # the obligation stands; the bad date does not


# --- tampered state is reported -----------------------------------------------------------


async def _extracted(sessions: async_sessionmaker[AsyncSession], course: Course_) -> None:
    await _extract(sessions, course, respond)
    assert (await _validate(sessions, course)).clean


async def _codes(sessions: async_sessionmaker[AsyncSession], course: Course_) -> set[IssueCode]:
    return {issue.code for issue in (await _validate(sessions, course)).issues}


async def test_evidence_from_another_course_is_an_error(
    sessions: async_sessionmaker[AsyncSession], course: Course_, tmp_path: Path
) -> None:
    await _extracted(sessions, course)
    other = Course_(sessions, tmp_path)  # its own course; a shared raw store
    await other.create()
    other_version = await other.ingest("brief", BRIEF, ResourceType.BRIEF)
    async with sessions() as session, session.begin():
        foreign = await session.scalar(
            select(SourceSpan.id).where(SourceSpan.resource_version_id == other_version)
        )
        link = await session.scalar(
            select(EvidenceLink).where(EvidenceLink.obligation_id.is_not(None))
        )
        assert foreign is not None and link is not None
        session.add(
            EvidenceLink(
                subject_type=EvidenceSubject.OBLIGATION,
                obligation_id=link.obligation_id,
                field=EvidenceField.OBLIGATION,
                source_span_id=foreign,
                quote_start=0,
                quote_end=5,
                verification_status=V.VERIFIED,
                model_call_id=link.model_call_id,
                workflow_run_id=link.workflow_run_id,
            )
        )
    codes = await _codes(sessions, course)
    # Another course's span, and a version the run never read.
    assert {IssueCode.EVIDENCE_CROSS_COURSE, IssueCode.LINEAGE_INCONSISTENT} <= codes


@pytest.mark.parametrize(
    ("tamper", "code"),
    [
        ("quote_beyond_span", IssueCode.QUOTE_OUT_OF_BOUNDS),
        ("no_lineage", IssueCode.LINEAGE_MISSING),
        ("evidence_demoted", IssueCode.VERIFIED_WITHOUT_VERIFIED_EVIDENCE),
        ("evidence_removed", IssueCode.CLAIM_WITHOUT_EVIDENCE),
        ("wrong_instant", IssueCode.INSTANT_MISMATCH),
        ("edited_text", IssueCode.CANONICAL_KEY_MISMATCH),
        ("verified_on_injection", IssueCode.EVIDENCE_ON_INJECTION),
    ],
)
async def test_tampered_state_is_reported(
    sessions: async_sessionmaker[AsyncSession], course: Course_, tamper: str, code: IssueCode
) -> None:
    await _extracted(sessions, course)
    async with sessions() as session, session.begin():
        requirement_links = EvidenceLink.assessment_requirement_id.is_not(None)
        if tamper == "quote_beyond_span":
            link = await session.scalar(select(EvidenceLink).where(requirement_links).limit(1))
            assert link is not None
            link.quote_end = 10_000
        elif tamper == "no_lineage":
            link = await session.scalar(select(EvidenceLink).where(requirement_links).limit(1))
            assert link is not None
            link.model_call_id = None
        elif tamper == "evidence_demoted":
            await session.execute(
                update(EvidenceLink)
                .where(requirement_links)
                .values(
                    verification_status=V.NEEDS_REVIEW, review_reason="non_unique_quote_location"
                )
            )
        elif tamper == "evidence_removed":
            await session.execute(
                EvidenceLink.__table__.delete().where(EvidenceLink.obligation_id.is_not(None))
            )
        elif tamper == "wrong_instant":
            deadline = await session.scalar(select(AssessmentDeadline))
            assert deadline is not None and deadline.due_at is not None
            deadline.due_at += timedelta(hours=1)
        elif tamper == "edited_text":
            requirement = await session.scalar(select(AssessmentRequirement).limit(1))
            assert requirement is not None
            requirement.text = "A different statement."
        elif tamper == "verified_on_injection":
            span = await session.scalar(
                select(SourceSpan).where(SourceSpan.excerpt.contains("IGNORE ALL PREVIOUS"))
            )
            link = await session.scalar(select(EvidenceLink).where(requirement_links).limit(1))
            assert span is not None and link is not None
            link.source_span_id, link.quote_start = span.id, 0
            link.quote_end = len(INJECTION.split(".")[0])
    codes = await _codes(sessions, course)
    assert code in codes


async def test_a_claim_only_a_superseded_version_supports_is_a_warning(
    sessions: async_sessionmaker[AsyncSession], course: Course_
) -> None:
    await _extracted(sessions, course)
    # A new handbook version that no longer says it: the old evidence stays.
    await course.ingest(
        "handbook", _page("Dissertation handbook", "Nothing to see."), ResourceType.HANDBOOK
    )
    report = await _validate(sessions, course)
    assert report.clean  # true, but needs attention
    assert {w.code for w in report.warnings} == {IssueCode.STALE_SOURCE_VERSION}


async def test_an_obligation_due_date_must_match_its_zone(
    sessions: async_sessionmaker[AsyncSession], course: Course_
) -> None:
    await _extracted(sessions, course)
    async with sessions() as session, session.begin():
        obligation = await session.scalar(select(Obligation).where(Obligation.status == V.VERIFIED))
        assert obligation is not None
        obligation.due_date, obligation.due_time = date(2026, 11, 16), time(12, 0)
        obligation.due_timezone = "Europe/London"
        obligation.due_timezone_source = TimezoneSource.EXPLICIT_SOURCE
        obligation.due_precision = DeadlinePrecision.DATETIME
        obligation.due_at = datetime(2026, 11, 16, 13, 0, tzinfo=UTC)  # GMT: should be 12:00
    assert IssueCode.INSTANT_MISMATCH in await _codes(sessions, course)


# --- validator metrics (P1-B) -------------------------------------------------------------


async def test_metrics_are_zero_filled_by_severity_and_code_when_clean(
    sessions: async_sessionmaker[AsyncSession], course: Course_
) -> None:
    await _extracted(sessions, course)
    metrics = (await _validate(sessions, course)).as_dict()["metrics"]
    assert set(metrics) == {"error", "warning"}
    assert sum(metrics["error"].values()) == sum(metrics["warning"].values()) == 0
    every = [*metrics["error"], *metrics["warning"]]
    assert sorted(every) == sorted(code.value for code in IssueCode)
    assert list(metrics["error"]) == sorted(metrics["error"])  # a fixed order


async def test_metrics_count_a_corrupted_fixture_exactly(
    sessions: async_sessionmaker[AsyncSession], course: Course_
) -> None:
    await _extracted(sessions, course)
    async with sessions() as session, session.begin():
        await session.execute(
            update(EvidenceLink)
            .where(EvidenceLink.assessment_requirement_id.is_not(None))
            .values(model_call_id=None)  # every requirement link: 2 requirements x 2 links
        )
        deadline = await session.scalar(select(AssessmentDeadline))
        assert deadline is not None and deadline.due_at is not None
        deadline.due_at += timedelta(minutes=30)
    report = await _validate(sessions, course)
    metrics = report.as_dict()["metrics"]
    assert metrics["error"]["lineage_missing"] == 4
    assert metrics["error"]["instant_mismatch"] == 1
    assert sum(metrics["error"].values()) == 5 and sum(metrics["warning"].values()) == 0
    assert report.as_dict() == (await _validate(sessions, course)).as_dict()  # deterministic


# --- review fixes: validator false negatives ----------------------------------------------


@pytest.mark.parametrize("tamper", ["weighting", "obligation_due", "foreign_call"])
async def test_values_and_proposing_calls_need_their_own_support(
    sessions: async_sessionmaker[AsyncSession], course: Course_, tmp_path: Path, tamper: str
) -> None:
    await _extracted(sessions, course)
    async with sessions() as session, session.begin():
        obligation = await session.scalar(select(Obligation).where(Obligation.status == V.VERIFIED))
        assessment = await session.scalar(select(Assessment))
        assert obligation is not None and assessment is not None
        if tamper == "weighting":
            assessment.weighting_percent = 50  # no weighting evidence anywhere
        elif tamper == "obligation_due":
            obligation.due_date = date(2026, 11, 16)
            obligation.due_timezone = "Europe/London"
            obligation.due_timezone_source = TimezoneSource.INSTITUTION_DEFAULT
            obligation.due_precision = DeadlinePrecision.DATE
    if tamper == "foreign_call":
        other = Course_(sessions, tmp_path)
        await other.create()
        await other.ingest("brief", BRIEF_WITH_DEADLINE, ResourceType.BRIEF)
        await AssessmentExtractionService(sessions, FakeModel(respond)).extract_course(other.id)
        async with sessions() as session, session.begin():
            call = await session.scalar(
                select(EvidenceLink.model_call_id)
                .join(Assessment, Assessment.id == EvidenceLink.assessment_id)
                .where(Assessment.course_id == other.id)
            )
            await session.execute(
                update(Obligation).where(Obligation.id == obligation.id).values(model_call_id=call)
            )
    codes = await _codes(sessions, course)
    expected = (
        IssueCode.LINEAGE_INCONSISTENT
        if tamper == "foreign_call"
        else IssueCode.VALUE_WITHOUT_EVIDENCE
    )
    assert expected in codes


async def test_needs_review_evidence_on_injected_text_is_an_error_too(
    sessions: async_sessionmaker[AsyncSession], course: Course_
) -> None:
    await _extracted(sessions, course)
    async with sessions() as session, session.begin():
        span = await session.scalar(
            select(SourceSpan).where(SourceSpan.excerpt.contains("IGNORE ALL PREVIOUS"))
        )
        link = await session.scalar(
            select(EvidenceLink).where(EvidenceLink.assessment_requirement_id.is_not(None)).limit(1)
        )
        assert span is not None and link is not None
        link.source_span_id, link.quote_start, link.quote_end = span.id, 0, 10
        link.verification_status = V.NEEDS_REVIEW
        link.review_reason = "non_unique_quote_location"  # type: ignore[assignment]
    assert IssueCode.EVIDENCE_ON_INJECTION in await _codes(sessions, course)


# --- claim fingerprints (P1-A) ------------------------------------------------------------


async def _fingerprints(sessions: async_sessionmaker[AsyncSession], course: Course_) -> Any:
    from app.extraction.fingerprint import course_fingerprints

    async with sessions() as session:
        found = await course_fingerprints(session, course.id)
    return {kind: sorted(by_id.values()) for kind, by_id in found.items()}


async def test_a_fresh_extraction_gives_the_same_fingerprints_with_new_ids(
    sessions: async_sessionmaker[AsyncSession], course: Course_
) -> None:
    await _extract(sessions, course, respond)
    first = await _fingerprints(sessions, course)
    async with sessions() as session, session.begin():
        await session.execute(Obligation.__table__.delete())
        await session.execute(Assessment.__table__.delete())  # requirements, deadlines cascade
    await _extract(sessions, course, respond)  # new rows, new ModelCalls, new runs
    assert await _fingerprints(sessions, course) == first
    assert all(first.values()) and len({fp for fps in first.values() for fp in fps}) == sum(
        len(fps) for fps in first.values()
    )


async def test_more_evidence_or_a_new_version_leaves_the_fingerprint(
    sessions: async_sessionmaker[AsyncSession], course: Course_
) -> None:
    await _extract(sessions, course, respond)
    before = await _fingerprints(sessions, course)
    # The same handbook sentence in a new version: new spans, new links, same claims.
    await course.ingest("handbook", HANDBOOK_V1 + b" ", ResourceType.HANDBOOK)
    await ClaimExtractionService(sessions, FakeModel(respond), "obligations").extract_course(
        course.id
    )
    assert await _fingerprints(sessions, course) == before


def test_a_meaningful_change_changes_the_fingerprint() -> None:
    from app.extraction.fingerprint import (
        assessment_fingerprint,
        deadline_fingerprint,
        obligation_fingerprint,
        requirement_fingerprint,
    )
    from app.extraction.statements import statement_key

    course = ("lms", "demo", "Northbridge University", "msd7000")
    other = ("lms", "demo", "Northbridge University", "aml7001")
    noon, one = time(12, 0), time(13, 0)
    day = date(2026, 11, 30)
    key = statement_key("A focused research question.")
    assert assessment_fingerprint(course, "project specification") != assessment_fingerprint(
        other, "project specification"
    )
    assert deadline_fingerprint(course, "project specification", day, noon, "Europe/London") != (
        deadline_fingerprint(course, "project specification", day, one, "Europe/London")
    )
    assert deadline_fingerprint(course, "a", day, None, None) != deadline_fingerprint(
        course, "a", day, noon, None
    )
    assert requirement_fingerprint(course, "a", key) != requirement_fingerprint(
        course, "a", statement_key("A methodology.")
    )
    assert requirement_fingerprint(course, "a", key) != requirement_fingerprint(course, "b", key)
    assert obligation_fingerprint(course, key) != requirement_fingerprint(course, "", key)
    # Stable: the same inputs, the same digest, every time.
    assert obligation_fingerprint(course, key) == obligation_fingerprint(tuple(course), key)


async def test_a_failure_after_a_partial_write_leaves_nothing(
    sessions: async_sessionmaker[AsyncSession], course: Course_, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.db.models import WorkflowRun
    from app.extraction import claims_service

    original = claims_service.persist_obligations

    async def fail_after_writing(*args: Any, **kwargs: Any) -> Any:
        await original(*args, **kwargs)  # rows and links written in the unit's transaction
        raise RuntimeError("the database went away")

    monkeypatch.setattr(claims_service, "persist_obligations", fail_after_writing)
    service = ClaimExtractionService(sessions, FakeModel(respond), "obligations")
    with pytest.raises(RuntimeError, match="went away"):
        await service.extract_course(course.id)
    assert (await _counts(sessions))[3:] == [0, 0]  # no obligation, no link
    async with sessions() as session:
        (run,) = (await session.scalars(select(WorkflowRun))).all()
    assert run.status.value == "failed"
