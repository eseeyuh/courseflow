# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

import uuid

from fastapi import APIRouter, HTTPException

from app.api.deps import SessionDep
from app.api.schemas.claims import ObligationRead, RequirementRead
from app.db.claims import course_obligations, course_requirements
from app.db.models import Course

router = APIRouter(tags=["requirements", "obligations"])


@router.get("/courses/{course_id}/requirements")
async def get_course_requirements(
    session: SessionDep, course_id: uuid.UUID
) -> list[RequirementRead]:
    """Every assessment requirement of a course, with status, reasons and evidence."""
    if await session.get(Course, course_id) is None:
        raise HTTPException(status_code=404, detail="course not found")
    return await course_requirements(session, course_id)


@router.get("/courses/{course_id}/obligations")
async def get_course_obligations(session: SessionDep, course_id: uuid.UUID) -> list[ObligationRead]:
    """Every obligation of a course, with modality, status, reasons and evidence."""
    if await session.get(Course, course_id) is None:
        raise HTTPException(status_code=404, detail="course not found")
    return await course_obligations(session, course_id)
