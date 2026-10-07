# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""FastAPI dependencies: the seams between routes and infrastructure.

Routes declare what they need (``SessionDep``, ``ReadinessDep``, ...) and
never construct infrastructure themselves. Tests replace any of these
functions through ``app.dependency_overrides``.
"""

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.config import Settings
from app.db.health import ReadinessReport, check_readiness


def get_app_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_engine(request: Request) -> AsyncEngine:
    return request.app.state.engine


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """One session per request; closed (connection returned to the pool)
    after the response, including when the handler raises."""
    async with request.app.state.session_factory() as session:
        yield session


SettingsDep = Annotated[Settings, Depends(get_app_settings)]
EngineDep = Annotated[AsyncEngine, Depends(get_engine)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]


async def get_readiness(engine: EngineDep, settings: SettingsDep) -> ReadinessReport:
    return await check_readiness(engine, settings.db_health_timeout_seconds)


ReadinessDep = Annotated[ReadinessReport, Depends(get_readiness)]
