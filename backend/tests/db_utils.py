# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Helpers for creating, dropping and migrating dedicated test databases.

All functions are synchronous on purpose: each runs its own short event loop
(``asyncio.run``), so they are safe to call from session-scoped fixtures and
from Alembic, which itself uses ``asyncio.run`` in ``alembic/env.py``.
"""

import asyncio
import re
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import URL, text
from sqlalchemy.ext.asyncio import create_async_engine

ALEMBIC_INI = Path(__file__).resolve().parents[1] / "alembic.ini"
# Tests create, wipe and drop databases: refuse any name not ending in _test.
SAFE_TEST_DB_NAME = re.compile(r"^[a-z_][a-z0-9_]*_test$")


def require_test_database_name(url: URL) -> str:
    if not url.database or not SAFE_TEST_DB_NAME.match(url.database):
        raise ValueError(
            f"Test database name must match {SAFE_TEST_DB_NAME.pattern!r}, got {url.database!r}"
        )
    return url.database


async def _admin_execute(url: URL, statements: list[str], *, exists_check: str | None) -> None:
    admin = create_async_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as conn:
            if exists_check is not None:
                exists = await conn.scalar(
                    text("SELECT 1 FROM pg_database WHERE datname = :name"),
                    {"name": exists_check},
                )
                if exists:
                    return
            for statement in statements:
                await conn.execute(text(statement))
    finally:
        await admin.dispose()


def create_database_if_missing(url: URL) -> None:
    name = require_test_database_name(url)
    # Identifiers cannot be bind parameters; the name was validated above.
    asyncio.run(_admin_execute(url, [f'CREATE DATABASE "{name}"'], exists_check=name))


def recreate_database(url: URL) -> None:
    name = require_test_database_name(url)
    asyncio.run(
        _admin_execute(
            url,
            [f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)', f'CREATE DATABASE "{name}"'],
            exists_check=None,
        )
    )


def drop_database(url: URL) -> None:
    name = require_test_database_name(url)
    asyncio.run(
        _admin_execute(url, [f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'], exists_check=None)
    )


def alembic_config(url: URL) -> Config:
    config = Config(str(ALEMBIC_INI))
    config.attributes["database_url"] = url.render_as_string(hide_password=False)
    config.attributes["configure_logging"] = False
    return config


def migrate(url: URL, revision: str = "head") -> None:
    command.upgrade(alembic_config(url), revision)


def downgrade(url: URL, revision: str = "base") -> None:
    command.downgrade(alembic_config(url), revision)
