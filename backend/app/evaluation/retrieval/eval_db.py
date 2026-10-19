# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""The dedicated, disposable evaluation database: guards and lifecycle.

Evaluation recreates its database from zero for every run, so it must never
be able to reach any other database. ``require_eval_database`` is the only
way to obtain an ``EvalDatabase``, and the destructive helpers accept
nothing else. It requires:

- ``COURSEFLOW_EVAL_DATABASE_URL`` to be set (it is never derived);
- a PostgreSQL URL whose database name is exactly ``courseflow_eval``;
- a host that is loopback or the Compose ``db`` service;
- that it is not the application's own ``DATABASE_URL`` (compared after
  normalised parsing: driver family, host, port, database, user).

Nothing in the API or runtime path imports this module.
"""

from __future__ import annotations

import asyncio
import ipaddress
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import URL, make_url, text
from sqlalchemy.exc import ArgumentError
from sqlalchemy.ext.asyncio import create_async_engine

EVAL_DATABASE_ENV = "COURSEFLOW_EVAL_DATABASE_URL"
EVAL_DATABASE_NAME = "courseflow_eval"
COMPOSE_DB_HOST = "db"
ALEMBIC_INI = Path(__file__).resolve().parents[3] / "alembic.ini"


class EvalDatabaseRefused(ValueError):
    """The URL is not a safe evaluation database; nothing was touched."""


@dataclass(frozen=True, slots=True)
class EvalDatabase:
    """A URL that passed every guard. Construct only via ``require_eval_database``."""

    url: URL
    _token: object

    def __post_init__(self) -> None:
        if self._token is not _GUARD_TOKEN:
            raise EvalDatabaseRefused("EvalDatabase is only built by require_eval_database")


_GUARD_TOKEN = object()


def _parse(raw: str, what: str) -> URL:
    try:
        return make_url(raw)
    except ArgumentError:
        raise EvalDatabaseRefused(f"{what} is not a database URL") from None


def _host(url: URL) -> str:
    """The host, lower-cased, with every loopback spelling as ``loopback``."""
    host = (url.host or "").lower().strip("[]")
    if host == "localhost":
        return "loopback"
    try:
        return "loopback" if ipaddress.ip_address(host).is_loopback else host
    except ValueError:
        return host


def _identity(url: URL) -> tuple[str, str, int, str, str]:
    return (
        url.get_backend_name(),
        _host(url),
        url.port or 5432,
        url.database or "",
        url.username or "",
    )


def require_eval_database(
    environ: Mapping[str, str], *, application_database_url: str
) -> EvalDatabase:
    raw = environ.get(EVAL_DATABASE_ENV, "").strip()
    if not raw:
        raise EvalDatabaseRefused(f"{EVAL_DATABASE_ENV} is not set")
    url = _parse(raw, EVAL_DATABASE_ENV)
    if url.get_backend_name() != "postgresql":
        raise EvalDatabaseRefused("the evaluation database must be PostgreSQL")
    if url.database != EVAL_DATABASE_NAME:
        raise EvalDatabaseRefused(
            f"the evaluation database must be named {EVAL_DATABASE_NAME!r}, got {url.database!r}"
        )
    if _host(url) not in {"loopback", COMPOSE_DB_HOST}:
        raise EvalDatabaseRefused(
            f"evaluation database host {url.host!r} is not loopback or {COMPOSE_DB_HOST!r}"
        )
    application = _parse(application_database_url, "DATABASE_URL")
    if _identity(url) == _identity(application):
        raise EvalDatabaseRefused("the evaluation database is the application's DATABASE_URL")
    return EvalDatabase(url=url, _token=_GUARD_TOKEN)


def _checked(database: EvalDatabase) -> URL:
    # Defence in depth: re-check what the destructive statement will name.
    if not isinstance(database, EvalDatabase) or database.url.database != EVAL_DATABASE_NAME:
        raise EvalDatabaseRefused("not a guarded evaluation database")
    return database.url


async def _admin(url: URL, statements: list[str]) -> None:
    admin = create_async_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as connection:
            for statement in statements:
                await connection.execute(text(statement))
    finally:
        await admin.dispose()


def recreate_eval_database(database: EvalDatabase) -> None:
    """Drop and create the evaluation database. Synchronous (own event loop),
    like Alembic: call it outside a running loop."""
    url = _checked(database)
    name = url.database
    asyncio.run(
        _admin(url, [f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)', f'CREATE DATABASE "{name}"'])
    )


def migrate_eval_database(database: EvalDatabase) -> str:
    """Migrate to head; returns the head revision."""
    from alembic import command
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    url = _checked(database)
    config = Config(str(ALEMBIC_INI))
    config.attributes["database_url"] = url.render_as_string(hide_password=False)
    config.attributes["configure_logging"] = False
    command.upgrade(config, "head")
    head = ScriptDirectory.from_config(config).get_current_head()
    if head is None:  # pragma: no cover - the repository always has migrations
        raise RuntimeError("no Alembic head revision")
    return head


def prepare_eval_database(database: EvalDatabase) -> str:
    """A fresh, migrated evaluation database; returns the migration head."""
    recreate_eval_database(database)
    return migrate_eval_database(database)
