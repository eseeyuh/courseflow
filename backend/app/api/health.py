# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Unversioned infrastructure probes (Docker, load balancers, orchestrators)."""

from fastapi import APIRouter, Response, status

from app.api.deps import ReadinessDep
from app.api.schemas.health import LivenessResponse, ReadinessResponse

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/live")
async def live() -> LivenessResponse:
    """The process is up and serving HTTP. Deliberately has no dependencies:
    restarting the API cannot fix a database outage."""
    return LivenessResponse()


@router.get(
    "/ready",
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ReadinessResponse}},
)
async def ready(readiness: ReadinessDep, response: Response) -> ReadinessResponse:
    """Dependencies needed to serve real traffic are reachable."""
    if not readiness.ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return ReadinessResponse(
        status="ready" if readiness.ready else "not_ready",
        checks=readiness.checks,
    )
