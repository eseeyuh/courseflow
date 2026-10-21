# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""The requirements and obligations API: status, reasons and exact evidence
with its location, source version and model lineage."""

from __future__ import annotations

import uuid
from pathlib import Path

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.enums import ResourceType
from app.extraction.claims_service import ClaimExtractionService
from tests.test_assessment_extraction_service import FakeModel
from tests.test_claim_extraction_service import (
    BRIEF,
    HANDBOOK_V1,
    SUPERVISOR,
    Course_,
    obligations_script,
    requirements_script,
)

pytestmark = pytest.mark.anyio


async def _extracted(committed_session: AsyncSession, tmp_path: Path) -> Course_:
    sessions = async_sessionmaker(committed_session.bind, expire_on_commit=False)
    course = Course_(sessions, tmp_path)
    await course.create()
    brief = await course.ingest("brief", BRIEF, ResourceType.BRIEF)
    await course.ingest("handbook", HANDBOOK_V1, ResourceType.HANDBOOK)
    await course.assessment_with_title_evidence(brief)
    for task, script in (("requirements", requirements_script), ("obligations", None)):
        respond = script or obligations_script()
        await ClaimExtractionService(sessions, FakeModel(respond), task).extract_course(course.id)  # type: ignore[arg-type]
    return course


async def test_a_course_lists_its_requirements_with_evidence(
    committed_session: AsyncSession, client: httpx.AsyncClient, tmp_path: Path
) -> None:
    course = await _extracted(committed_session, tmp_path)
    response = await client.get(f"/api/v1/courses/{course.id}/requirements")
    assert response.status_code == 200
    first, second = response.json()
    assert (first["text"], second["text"]) == (
        "A focused research question.",
        "A timeline to the final submission.",
    )
    assert first["status"] == "verified" and first["review_reason"] is None
    assert first["assessment_title"] == "Project Specification"
    assert first["supported_by_current_version"] is True
    quotes = sorted(e["quote"] for e in first["evidence"])
    assert quotes == ["A focused research question.", "Your specification must include"]
    evidence = first["evidence"][0]
    assert evidence["resource_version_id"] == str(course.versions["brief"])
    assert (evidence["model"], evidence["prompt_version"]) == (
        "fake-fast",
        "requirement-extraction/v1",
    )


async def test_a_course_lists_its_obligations_with_modality_and_reasons(
    committed_session: AsyncSession, client: httpx.AsyncClient, tmp_path: Path
) -> None:
    course = await _extracted(committed_session, tmp_path)
    response = await client.get(f"/api/v1/courses/{course.id}/obligations")
    assert response.status_code == 200
    by_status = {o["status"]: o for o in response.json()}
    assert set(by_status) == {"verified", "needs_review"}
    supervisor = by_status["verified"]
    assert (supervisor["category"], supervisor["modality"]) == ("supervisor_review", "mandatory")
    assert supervisor["assessment_id"] is not None and supervisor["due"] is None
    assert [e["quote"] for e in supervisor["evidence"]] == [SUPERVISOR]
    assert supervisor["evidence"][0]["prompt_version"] == "obligation-extraction/v1"
    review = by_status["needs_review"]
    assert (review["modality"], review["review_reason"]) == (None, "permissive_wording")


async def test_an_unknown_course_is_404(client: httpx.AsyncClient) -> None:
    for kind in ("requirements", "obligations"):
        response = await client.get(f"/api/v1/courses/{uuid.uuid4()}/{kind}")
        assert response.status_code == 404
