# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

from typing import Literal

from pydantic import BaseModel

from app.core.config import AppEnv
from app.db.health import ReadinessChecks


class LivenessResponse(BaseModel):
    status: Literal["alive"] = "alive"


class ReadinessResponse(BaseModel):
    status: Literal["ready", "not_ready"]
    checks: ReadinessChecks


class HealthResponse(BaseModel):
    """Client-facing service status (used by the web frontend)."""

    status: Literal["ok", "unavailable"]
    version: str
    environment: AppEnv
    checks: ReadinessChecks
