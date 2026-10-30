# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

import asyncio
import uuid
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select

from app.ai.errors import AIConfigurationError
from app.ai.model_calls import RecordedCallError
from app.ai.provider import AIProvider
from app.api.deps import SessionDep, SettingsDep
from app.api.schemas.assessment import (
    AssessmentRead,
    BriefPageRead,
    BriefRead,
    LiveRequirementRead,
    LiveRequirementsRead,
    LiveTokenUsage,
)
from app.api.schemas.claims import RequirementRead
from app.db.assessments import course_assessments
from app.db.claims import course_requirements
from app.db.models import Assessment, Course, SourceSpan
from app.domain.enums import ModelRole
from app.extraction.live_preview import (
    LIVE_MODEL,
    PROMPT_VERSION,
    BriefTooLongError,
    EmptyBriefError,
    current_brief,
    preview_requirements,
)

router = APIRouter(tags=["assessments"])

LIVE_PROVIDER_LABEL = "Nebius Token Factory"
# One live analysis at a time: a second request is refused, never queued.
_LIVE_LOCK = asyncio.Lock()


@router.get("/courses/{course_id}/assessments")
async def get_course_assessments(session: SessionDep, course_id: uuid.UUID) -> list[AssessmentRead]:
    """A course's assessments, each with every stated deadline and all evidence."""
    if await session.get(Course, course_id) is None:
        raise HTTPException(status_code=404, detail="course not found")
    return await course_assessments(session, course_id)


@router.get("/assessments/{assessment_id}")
async def get_assessment(session: SessionDep, assessment_id: uuid.UUID) -> AssessmentRead:
    """One assessment with its deadlines and the evidence for each fact."""
    assessment = await session.get(Assessment, assessment_id)
    if assessment is None:
        raise HTTPException(status_code=404, detail="assessment not found")
    (read,) = await course_assessments(session, assessment.course_id, assessment_id=assessment_id)
    return read


@router.get("/assessments/{assessment_id}/requirements")
async def get_assessment_requirements(
    session: SessionDep, assessment_id: uuid.UUID
) -> list[RequirementRead]:
    """One assessment's requirements, each with its evidence and the teaching
    material mapped to it (exact span, location, lineage)."""
    assessment = await session.get(Assessment, assessment_id)
    if assessment is None:
        raise HTTPException(status_code=404, detail="assessment not found")
    return await course_requirements(session, assessment.course_id, assessment_id=assessment_id)


@router.get("/assessments/{assessment_id}/brief")
async def get_assessment_brief(session: SessionDep, assessment_id: uuid.UUID) -> BriefRead:
    """The full stored text of the assessment's current brief, so a reader
    sees each quoted requirement in its surrounding paragraph."""
    if await session.get(Assessment, assessment_id) is None:
        raise HTTPException(status_code=404, detail="assessment not found")
    brief = await current_brief(session, assessment_id)
    if brief is None:
        raise HTTPException(status_code=404, detail="no brief found for this assessment")
    spans = await session.scalars(
        select(SourceSpan)
        .where(SourceSpan.resource_version_id == brief.version.id)
        .order_by(SourceSpan.ordinal)
    )
    return BriefRead(
        assessment_id=assessment_id,
        resource_id=brief.resource.id,
        resource_title=brief.resource.title,
        resource_version_id=brief.version.id,
        version_number=brief.version.version_number,
        pages=[BriefPageRead(page_number=s.page_number, text=s.excerpt) for s in spans],
    )


async def get_live_provider(settings: SettingsDep) -> AsyncIterator[AIProvider]:
    """The real Nebius provider, never a replay. Refused in production: an
    unauthenticated endpoint that spends tokens is a demo feature only."""
    from app.ai.nebius import NebiusTokenFactoryProvider

    if settings.app_env == "production":
        raise HTTPException(status_code=404, detail="not found")
    try:
        provider = NebiusTokenFactoryProvider.from_settings(
            settings, models={ModelRole.FAST: LIVE_MODEL}
        )
    except AIConfigurationError:
        raise HTTPException(status_code=503, detail="live inference is not configured") from None
    try:
        yield provider
    finally:
        await provider.aclose()


LiveProviderDep = Annotated[AIProvider, Depends(get_live_provider)]


@router.post("/assessments/{assessment_id}/live-requirements")
async def analyze_requirements_live(
    request: Request, session: SessionDep, provider: LiveProviderDep, assessment_id: uuid.UUID
) -> LiveRequirementsRead:
    """Ask the model, live, for the main requirements in the assessment's
    current brief. Nothing is persisted but the model call record; a failure
    is an error, never a replayed answer."""
    assessment = await session.get(Assessment, assessment_id)
    if assessment is None:
        raise HTTPException(status_code=404, detail="assessment not found")
    brief = await current_brief(session, assessment_id)
    if brief is None:
        raise HTTPException(status_code=404, detail="no brief found for this assessment")
    if _LIVE_LOCK.locked():
        raise HTTPException(status_code=409, detail="a live analysis is already running")
    async with _LIVE_LOCK:
        try:
            preview = await preview_requirements(
                provider,
                request.app.state.session_factory,
                assessment_title=assessment.title,
                brief=brief,
            )
        except (BriefTooLongError, EmptyBriefError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        except RecordedCallError as exc:
            # The category only: details stay in the logs and the call record.
            raise HTTPException(
                status_code=502,
                detail=f"live analysis failed ({exc.error.category.value})",
            ) from None
    result = preview.recorded.result
    return LiveRequirementsRead(
        assessment_id=assessment.id,
        resource_id=brief.resource.id,
        resource_title=brief.resource.title,
        resource_version_id=brief.version.id,
        version_number=brief.version.version_number,
        requirements=[
            LiveRequirementRead(
                requirement=line.requirement,
                quote=line.quote,
                quote_found=line.quote_found,
            )
            for line in preview.lines
        ],
        provider=LIVE_PROVIDER_LABEL,
        model=result.model,
        prompt_version=PROMPT_VERSION,
        model_call_id=preview.recorded.model_call_id,
        latency_ms=preview.latency_ms,
        usage=LiveTokenUsage(
            prompt_tokens=result.usage.prompt_tokens,
            completion_tokens=result.usage.completion_tokens,
            reasoning_tokens=result.usage.reasoning_tokens,
        ),
    )
