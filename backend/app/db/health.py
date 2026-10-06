# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Readiness checks shared by every health endpoint."""

import asyncio
import logging
from typing import Literal

from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

logger = logging.getLogger(__name__)

CheckStatus = Literal["ok", "unavailable"]


class ReadinessChecks(BaseModel):
    database: CheckStatus


class ReadinessReport(BaseModel):
    checks: ReadinessChecks

    @property
    def ready(self) -> bool:
        return all(status == "ok" for status in self.checks.model_dump().values())


async def check_database(engine: AsyncEngine, timeout_seconds: float) -> CheckStatus:
    """Run ``SELECT 1`` within a hard time bound.

    Any failure means "not ready", whatever its cause (refused connection,
    DNS, authentication, hang), so all exceptions are caught here. They are
    logged with their type, never silently swallowed.
    """
    try:
        async with asyncio.timeout(timeout_seconds):
            async with engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
    except TimeoutError:
        logger.warning("health.database.timeout", extra={"timeout_seconds": timeout_seconds})
        return "unavailable"
    except Exception as exc:  # noqa: BLE001 - any failure means "not ready"; logged below
        logger.warning(
            "health.database.unavailable",
            extra={"error_type": type(exc).__name__, "error": str(exc)},
        )
        return "unavailable"
    return "ok"


async def check_readiness(engine: AsyncEngine, timeout_seconds: float) -> ReadinessReport:
    database = await check_database(engine, timeout_seconds)
    return ReadinessReport(checks=ReadinessChecks(database=database))
