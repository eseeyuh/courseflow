# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text

from app.api.deps import SessionDep

pytestmark = pytest.mark.anyio


async def test_session_per_request_returns_connection_to_pool(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    observed: dict[str, int] = {}

    @app.get("/_test/session-probe")
    async def probe(session: SessionDep) -> dict[str, int]:
        value = (await session.execute(text("SELECT 41 + 1"))).scalar_one()
        observed["checked_out_during_request"] = app.state.engine.pool.checkedout()
        return {"value": value}

    response = await client.get("/_test/session-probe")

    assert response.status_code == 200
    assert response.json() == {"value": 42}
    assert observed["checked_out_during_request"] == 1
    # The yield dependency closed the session: no connection leaked.
    assert app.state.engine.pool.checkedout() == 0


async def test_engine_is_shared_across_requests(app: FastAPI, client: httpx.AsyncClient) -> None:
    engine = app.state.engine

    await client.get("/health/ready")
    await client.get("/health/ready")

    assert app.state.engine is engine
