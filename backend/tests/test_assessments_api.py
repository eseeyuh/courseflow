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
