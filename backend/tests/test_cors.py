# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""CORS: only explicitly allowed browser origins may read API responses."""

from collections.abc import AsyncIterator

import httpx
import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.main import create_app
from tests.conftest import UNREACHABLE_DATABASE_URL

pytestmark = pytest.mark.anyio

FRONTEND = "http://localhost:3000"
OTHER_SITE = "https://evil.example"


@pytest.fixture
async def cors_client() -> AsyncIterator[httpx.AsyncClient]:
    # Liveness needs no database, so an unreachable DB keeps this test independent.
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        database_url=UNREACHABLE_DATABASE_URL,  # type: ignore[arg-type]
        cors_allowed_origins=[FRONTEND],
    )
    transport = httpx.ASGITransport(app=create_app(settings))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def test_allowed_origin_can_read_response(cors_client: httpx.AsyncClient) -> None:
    response = await cors_client.get("/health/live", headers={"Origin": FRONTEND})

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == FRONTEND


async def test_other_origin_gets_no_cors_grant(cors_client: httpx.AsyncClient) -> None:
    response = await cors_client.get("/health/live", headers={"Origin": OTHER_SITE})

    # The server still answers; the BROWSER blocks the page from reading it.
    assert "access-control-allow-origin" not in response.headers


async def test_preflight_allows_get_from_frontend_only(cors_client: httpx.AsyncClient) -> None:
    allowed = await cors_client.options(
        "/api/v1/health",
        headers={"Origin": FRONTEND, "Access-Control-Request-Method": "GET"},
    )
    rejected = await cors_client.options(
        "/api/v1/health",
        headers={"Origin": OTHER_SITE, "Access-Control-Request-Method": "GET"},
    )
    wrong_method = await cors_client.options(
        "/api/v1/health",
        headers={"Origin": FRONTEND, "Access-Control-Request-Method": "DELETE"},
    )

    assert allowed.status_code == 200
    assert allowed.headers["access-control-allow-origin"] == FRONTEND
    assert rejected.status_code == 400
    assert wrong_method.status_code == 400


def test_origins_parsed_from_comma_separated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "http://localhost:3000, http://127.0.0.1:3000")

    settings = Settings(_env_file=None, database_url=UNREACHABLE_DATABASE_URL)  # type: ignore[call-arg, arg-type]

    assert settings.cors_allowed_origins == ["http://localhost:3000", "http://127.0.0.1:3000"]


def test_cors_defaults_to_no_origins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CORS_ALLOWED_ORIGINS", raising=False)

    settings = Settings(_env_file=None, database_url=UNREACHABLE_DATABASE_URL)  # type: ignore[call-arg, arg-type]

    assert settings.cors_allowed_origins == []


@pytest.mark.parametrize(
    "value",
    ["*", "http://localhost:3000/", "localhost:3000", "http://localhost:3000/app"],
    ids=["wildcard", "trailing-slash", "no-scheme", "with-path"],
)
def test_invalid_origins_rejected_at_startup(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", value)

    with pytest.raises(ValidationError, match="cors_allowed_origins"):
        Settings(_env_file=None, database_url=UNREACHABLE_DATABASE_URL)  # type: ignore[call-arg, arg-type]
