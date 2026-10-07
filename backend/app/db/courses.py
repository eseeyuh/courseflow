# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Course queries. Routes call these instead of building SQL themselves."""

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Course


async def list_courses(session: AsyncSession, *, limit: int) -> Sequence[Course]:
    """Courses in a stable order (oldest first, id as tie-breaker)."""
    statement = select(Course).order_by(Course.created_at, Course.id).limit(limit)
    return (await session.scalars(statement)).all()
