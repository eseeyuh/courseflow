# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

import uuid

from fastapi import APIRouter, HTTPException

from app.api.deps import SessionDep
from app.api.schemas.reasoning import ConflictRead, DependencyEdgeRead
from app.db.models import Assessment, Course
from app.db.reasoning import course_conflicts, course_dependencies

router = APIRouter(tags=["dependencies", "conflicts"])


@router.get("/courses/{course_id}/dependencies")
async def get_course_dependencies(
    session: SessionDep, course_id: uuid.UUID
) -> list[DependencyEdgeRead]:
    """Every dependency edge of a course, with both ends and exact evidence."""
    if await session.get(Course, course_id) is None:
        raise HTTPException(status_code=404, detail="course not found")
    return await course_dependencies(session, course_id)


@router.get("/courses/{course_id}/conflicts")
async def get_course_conflicts(session: SessionDep, course_id: uuid.UUID) -> list[ConflictRead]:
    """Every conflict of a course, each with all its disagreeing sides."""
    if await session.get(Course, course_id) is None:
        raise HTTPException(status_code=404, detail="course not found")
    return await course_conflicts(session, course_id)


@router.get("/assessments/{assessment_id}/conflicts")
async def get_assessment_conflicts(
    session: SessionDep, assessment_id: uuid.UUID
) -> list[ConflictRead]:
    """One assessment's conflicts, each with all its disagreeing sides."""
    assessment = await session.get(Assessment, assessment_id)
    if assessment is None:
        raise HTTPException(status_code=404, detail="assessment not found")
    return await course_conflicts(session, assessment.course_id, assessment_id=assessment_id)
