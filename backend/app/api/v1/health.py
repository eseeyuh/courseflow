# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

from fastapi import APIRouter, Response, status

from app import __version__
from app.api.deps import ReadinessDep, SettingsDep
from app.api.schemas.health import HealthResponse

router = APIRouter(tags=["health"])


@router.get(
    "/health",
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"model": HealthResponse}},
)
async def health(
    readiness: ReadinessDep, settings: SettingsDep, response: Response
) -> HealthResponse:
    """Versioned service status for API clients. Same readiness logic as
    ``/health/ready``, plus version and environment metadata."""
    if not readiness.ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return HealthResponse(
        status="ok" if readiness.ready else "unavailable",
        version=__version__,
        environment=settings.app_env,
        checks=readiness.checks,
    )
