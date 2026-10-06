# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import SessionDep
from app.api.schemas.course import CourseRead
from app.db.courses import list_courses

router = APIRouter(prefix="/courses", tags=["courses"])


@router.get("")
async def get_courses(
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[CourseRead]:
    """Courses known to CourseFlow (temporary listing until course import exists)."""
    courses = await list_courses(session, limit=limit)
    # Convert while the session is open; ORM objects never leave this function.
    return [CourseRead.model_validate(course) for course in courses]
