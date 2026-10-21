# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

import uuid

from fastapi import APIRouter, HTTPException

from app.api.deps import SessionDep
from app.api.schemas.assessment import AssessmentRead
from app.db.assessments import course_assessments
from app.db.models import Assessment, Course

router = APIRouter(tags=["assessments"])


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
