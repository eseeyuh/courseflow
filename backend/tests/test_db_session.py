# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.api.deps import EngineDep, SessionDep
from app.core.config import Settings
from app.db.session import create_db_engine

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


async def test_session_is_released_when_the_handler_raises(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    @app.get("/_test/session-error")
    async def failing(session: SessionDep) -> None:
        await session.execute(text("SELECT 1"))  # holds a pooled connection
        raise RuntimeError("handler failure")

    # The in-process transport re-raises app exceptions (a real server would
    # answer 500); what matters is that the session's cleanup still ran.
    with pytest.raises(RuntimeError, match="handler failure"):
        await client.get("/_test/session-error")

    assert app.state.engine.pool.checkedout() == 0


async def test_every_request_receives_the_lifespan_engine(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    seen: list[AsyncEngine] = []

    @app.get("/_test/engine-probe")
    async def probe(engine: EngineDep) -> None:
        seen.append(engine)

    await client.get("/_test/engine-probe")
    await client.get("/_test/engine-probe")

    # One process-wide pool, never a new engine per request.
    assert len(seen) == 2
    assert seen[0] is seen[1] is app.state.engine


def test_engine_never_puts_statement_parameters_in_errors(settings: Settings) -> None:
    """Parameters can hold document text, file names or source URIs."""
    assert create_db_engine(settings).sync_engine.hide_parameters is True
