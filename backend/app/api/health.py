# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Unversioned infrastructure probes (Docker, load balancers, orchestrators)."""

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter(prefix="/health", tags=["health"])


class LivenessResponse(BaseModel):
    status: Literal["alive"] = "alive"


@router.get("/live")
async def live() -> LivenessResponse:
    """The process is up and serving HTTP. Deliberately checks no dependencies:
    restarting the API cannot fix a database outage."""
    return LivenessResponse()
