# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""The WorkflowRun of one deterministic reasoning pass."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import WorkflowRun
from app.domain.enums import WorkflowRunStatus


async def start_run(
    sessions: async_sessionmaker[AsyncSession], course_id: uuid.UUID, workflow_version: str
) -> uuid.UUID:
    async with sessions() as session, session.begin():
        run = WorkflowRun(
            workflow_version=workflow_version,
            course_id=course_id,
            status=WorkflowRunStatus.RUNNING,
            started_at=datetime.now(UTC),
        )
        session.add(run)
        await session.flush()
        return run.id


async def finish_run(
    sessions: async_sessionmaker[AsyncSession],
    run_id: uuid.UUID,
    status: WorkflowRunStatus,
    summary: BaseModel,
) -> None:
    async with sessions() as session, session.begin():
        run = await session.get(WorkflowRun, run_id)
        if run is not None:
            run.status = status
            run.finished_at = datetime.now(UTC)
            run.summary = summary.model_dump(mode="json")
