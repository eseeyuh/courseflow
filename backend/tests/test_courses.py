# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""HTTP -> FastAPI -> AsyncSession -> PostgreSQL -> typed JSON, end to end."""

import httpx
import pytest
from pydantic import TypeAdapter
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas.course import CourseRead
from app.db.models import Course, Institution
from app.domain.enums import CourseStatus, LmsType

pytestmark = pytest.mark.anyio

COURSE_LIST = TypeAdapter(list[CourseRead])
PUBLIC_COURSE_FIELDS = {"id", "institution_id", "title", "external_id", "status", "created_at"}


async def test_courses_empty_list_when_database_has_no_courses(
    committed_session: AsyncSession, client: httpx.AsyncClient
) -> None:
    response = await client.get("/api/v1/courses")

    assert response.status_code == 200
    assert response.json() == []


async def test_course_persisted_in_database_is_read_through_the_api(
    committed_session: AsyncSession, client: httpx.AsyncClient
) -> None:
    # Arrange: persist (commit) a course through a separate session, the way
    # an import job would. The API reads it with its own per-request session.
    institution = Institution(name="Northbridge University", lms_type=LmsType.DEMO)
    course = Course(institution=institution, title="Data Systems", external_id="NB-DS101")
    committed_session.add(course)
    await committed_session.commit()
    await committed_session.refresh(course)

    # Act: cross the HTTP boundary.
    response = await client.get("/api/v1/courses")

    # Assert: status, exact public shape, and typed values.
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert set(body[0]) == PUBLIC_COURSE_FIELDS  # no ORM internals leak (e.g. updated_at)

    [read] = COURSE_LIST.validate_python(body)
    assert read.id == course.id
    assert read.institution_id == institution.id
    assert read.title == "Data Systems"
    assert read.external_id == "NB-DS101"
    assert read.status is CourseStatus.ACTIVE
    assert read.created_at == course.created_at
    assert read.created_at.tzinfo is not None


async def test_courses_limit_is_validated(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/v1/courses", params={"limit": 0})

    assert response.status_code == 422
