# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""The assessments API: every deadline any source states, each fact with its
exact quote, location, source version and model lineage."""

from __future__ import annotations

import uuid
from pathlib import Path

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.extraction.service import PROMPT_VERSION, AssessmentExtractionService
from tests.test_assessment_extraction_service import BRIEF, HANDBOOK, FakeModel, _course, script

pytestmark = pytest.mark.anyio


async def test_a_course_lists_its_assessments_with_all_evidence(
    committed_session: AsyncSession, client: httpx.AsyncClient, tmp_path: Path
) -> None:
    sessions = async_sessionmaker(committed_session.bind, expire_on_commit=False)
    course_id, versions = await _course(sessions, tmp_path, {"brief": BRIEF, "handbook": HANDBOOK})
    await AssessmentExtractionService(sessions, FakeModel(script)).extract_course(course_id)

    response = await client.get(f"/api/v1/courses/{course_id}/assessments")
    assert response.status_code == 200
    (assessment,) = response.json()
    assert assessment["canonical_key"] == "coursework 1" and assessment["status"] == "verified"
    assert assessment["review_reason"] is None
    assert {e["review_reason"] for e in assessment["evidence"]} == {None}
    assert sorted(e["field"] for e in assessment["evidence"]) == [
        "title", "title", "weighting", "weighting",
    ]  # fmt: skip
    # Both sources' deadlines are shown; neither is chosen.
    first, second = assessment["deadlines"]
    assert (first["due_date"], first["due_time"], first["timezone_source"]) == (
        "2026-11-12", "14:00", "explicit_source",
    )  # fmt: skip
    assert (second["due_date"], second["due_time"], second["timezone_source"]) == (
        "2026-11-13", "23:59", "institution_default",
    )  # fmt: skip
    (evidence,) = first["evidence"]
    assert evidence["quote"] == "Thursday 12 November 2026, 14:00 (UK time)"
    assert evidence["resource_version_id"] == str(versions["brief"])
    assert evidence["current_version"] is True and evidence["version_number"] == 1
    assert (evidence["model"], evidence["prompt_version"]) == ("fake-fast", PROMPT_VERSION)

    one = await client.get(f"/api/v1/assessments/{assessment['id']}")
    assert one.status_code == 200 and one.json() == assessment


async def test_unknown_course_or_assessment_is_404(client: httpx.AsyncClient) -> None:
    for path in (
        f"/api/v1/courses/{uuid.uuid4()}/assessments",
        f"/api/v1/assessments/{uuid.uuid4()}",
    ):
        assert (await client.get(path)).status_code == 404


async def test_needs_review_claims_carry_their_reason(
    committed_session: AsyncSession, client: httpx.AsyncClient, tmp_path: Path
) -> None:
    from tests.test_assessment_extraction_service import EXERCISES, _spec_script

    sessions = async_sessionmaker(committed_session.bind, expire_on_commit=False)
    course_id, _ = await _course(sessions, tmp_path, {"workshop": EXERCISES})
    await AssessmentExtractionService(sessions, FakeModel(_spec_script)).extract_course(course_id)
    body = (await client.get(f"/api/v1/courses/{course_id}/assessments")).json()
    assert {(a["status"], a["review_reason"]) for a in body} == {
        ("needs_review", "no_verified_assessment_fact")
    }
    # Its title evidence itself is exact and unique, so verified.
    assert {e["verification_status"] for a in body for e in a["evidence"]} == {"verified"}


async def test_a_superseded_version_keeps_its_deadline_verified_but_not_current(
    committed_session: AsyncSession, client: httpx.AsyncClient, tmp_path: Path
) -> None:
    """Verified is about the source version, not freshness: once the brief has
    a newer version, its old deadline stays verified history, flagged as not
    supported by any current version."""
    from app.domain.enums import ResourceType
    from app.evaluation.retrieval.resolve import production_limits
    from app.ingestion.schemas import RawSource
    from app.ingestion.service import ingest_file
    from app.ingestion.storage import LocalRawObjectStore
    from tests.test_assessment_extraction_service import _page

    sessions = async_sessionmaker(committed_session.bind, expire_on_commit=False)
    course_id, versions = await _course(sessions, tmp_path, {"brief": BRIEF})
    await AssessmentExtractionService(sessions, FakeModel(script)).extract_course(course_id)
    newer = _page("Coursework 1: Model Evaluation Report", "This brief has been withdrawn.")
    async with sessions() as session:
        result = await ingest_file(
            session,
            LocalRawObjectStore(tmp_path / "raw"),
            course_id=course_id,
            resource_type=ResourceType.BRIEF,
            source=RawSource(content=newer, display_name="brief.html", source_uri="upload:brief"),
            limits=production_limits(),
        )
        await session.commit()
    assert result.version_id != versions["brief"]

    (assessment,) = (await client.get(f"/api/v1/courses/{course_id}/assessments")).json()
    (deadline,) = assessment["deadlines"]
    assert deadline["status"] == "verified" and deadline["review_reason"] is None
    assert deadline["supported_by_current_version"] is False
    assert [e["current_version"] for e in deadline["evidence"]] == [False]


async def test_a_current_deadline_is_flagged_current(
    committed_session: AsyncSession, client: httpx.AsyncClient, tmp_path: Path
) -> None:
    sessions = async_sessionmaker(committed_session.bind, expire_on_commit=False)
    course_id, _ = await _course(sessions, tmp_path, {"brief": BRIEF})
    await AssessmentExtractionService(sessions, FakeModel(script)).extract_course(course_id)
    (assessment,) = (await client.get(f"/api/v1/courses/{course_id}/assessments")).json()
    assert [d["supported_by_current_version"] for d in assessment["deadlines"]] == [True]
