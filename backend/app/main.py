# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""FastAPI application factory.

Run with: ``uvicorn app.main:create_app --factory``
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy.engine import make_url

from app import __version__
from app.api import health
from app.api.v1.router import api_router
from app.core.config import Settings, get_settings
from app.core.logging import configure_logging
from app.db.session import create_db_engine, create_session_factory

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Process-wide resources: created once before serving, released on shutdown."""
    settings: Settings = app.state.settings
    engine = create_db_engine(settings)
    app.state.engine = engine
    app.state.session_factory = create_session_factory(engine)

    database = make_url(settings.database_url.get_secret_value())
    logger.info(
        "app.startup",
        extra={
            "version": __version__,
            "app_env": settings.app_env,
            "database": database.render_as_string(hide_password=True),
        },
    )
    try:
        yield
    finally:
        await engine.dispose()
        logger.info("app.shutdown")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    app = FastAPI(title="CourseFlow API", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.include_router(health.router)
    app.include_router(api_router)
    return app
