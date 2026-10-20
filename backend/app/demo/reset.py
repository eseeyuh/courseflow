# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Remove one demo institution and everything it owns, and nothing else.

Never a truncate: rows are selected through the institution's own courses
(``institution -> course -> resource -> version``) and deleted in
foreign-key order in one transaction. Only an institution with
``lms_type = demo`` and the exact name can be removed.

Kept on purpose, because they are shared and content-addressed rather than
owned: ``Embedding`` rows (a cache any course may reuse), standalone search
``ModelCall`` rows, and raw source files in the raw store.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import Course, Institution, Resource, ResourceVersion, SourceSpan, WorkflowRun
from app.domain.enums import LmsType


@dataclass(frozen=True, slots=True)
class ResetSummary:
    institutions: int
    courses: int
    resources: int
    versions: int
    workflow_runs: int


async def reset_demo_institution(
    session_factory: async_sessionmaker[AsyncSession], name: str, lms_type: LmsType
) -> ResetSummary:
    if lms_type is not LmsType.DEMO:
        raise ValueError("only a demo institution can be reset")
    async with session_factory() as session, session.begin():
        institutions = (
            await session.scalars(
                select(Institution.id).where(
                    Institution.name == name, Institution.lms_type == LmsType.DEMO
                )
            )
        ).all()
        courses = (
            await session.scalars(select(Course.id).where(Course.institution_id.in_(institutions)))
        ).all()
        resources = (
            await session.scalars(select(Resource.id).where(Resource.course_id.in_(courses)))
        ).all()
        versions = (
            await session.scalars(
                select(ResourceVersion.id).where(ResourceVersion.resource_id.in_(resources))
            )
        ).all()
        runs = await session.execute(delete(WorkflowRun).where(WorkflowRun.course_id.in_(courses)))
        # Chunks and their embedding links cascade from their spans and versions.
        await session.execute(
            delete(SourceSpan).where(SourceSpan.resource_version_id.in_(versions))
        )
        await session.execute(
            update(Resource).where(Resource.id.in_(resources)).values(current_version_id=None)
        )
        await session.execute(delete(ResourceVersion).where(ResourceVersion.id.in_(versions)))
        await session.execute(delete(Resource).where(Resource.id.in_(resources)))
        await session.execute(delete(Course).where(Course.id.in_(courses)))
        await session.execute(delete(Institution).where(Institution.id.in_(institutions)))
    return ResetSummary(
        institutions=len(institutions),
        courses=len(courses),
        resources=len(resources),
        versions=len(versions),
        workflow_runs=runs.rowcount or 0,  # type: ignore[attr-defined]
    )
