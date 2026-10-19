# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Engine and session factory construction.

One ``AsyncEngine`` (and its connection pool) per process, created in the
application lifespan. One ``AsyncSession`` per request, created from the
factory by the ``get_session`` dependency.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import Settings


def create_db_engine(settings: Settings) -> AsyncEngine:
    """Build the engine. No connection is opened until first use."""
    return create_async_engine(
        settings.database_url.get_secret_value(),
        # Validate pooled connections before use so a DB restart does not
        # surface as errors on the first requests afterwards.
        pool_pre_ping=True,
        # asyncpg's connect timeout: bounds how long a new connection may take.
        connect_args={"timeout": settings.db_connect_timeout_seconds},
        # Database errors never include statement parameters: they can hold
        # document text, file names or source URIs, and errors reach logs.
        hide_parameters=True,
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    # expire_on_commit=False: attributes stay readable after commit. Reloading
    # expired attributes would be implicit I/O, which async SQLAlchemy forbids.
    return async_sessionmaker(engine, expire_on_commit=False)


@asynccontextmanager
async def read_only_snapshot(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """One read-only REPEATABLE READ transaction: every statement in it reads
    the same snapshot, so what was verified is exactly what is read next.
    Always rolled back (it writes nothing). Keep it short, and never hold it
    across an external call."""
    async with session_factory() as session:
        # Isolation must be chosen before the transaction starts. asyncpg's
        # read-only flag needs SERIALIZABLE in SQLAlchemy, so READ ONLY is
        # set by the transaction's first statement instead.
        await session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
        await session.execute(text("SET TRANSACTION READ ONLY"))
        try:
            yield session
        finally:
            await session.rollback()
