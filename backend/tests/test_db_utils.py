# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""The guard that stops tests from wiping or dropping a non-test database."""

import pytest
from sqlalchemy import URL

from tests.db_utils import require_test_database_name


def _url(database: str | None) -> URL:
    return URL.create("postgresql+asyncpg", host="127.0.0.1", database=database)


@pytest.mark.parametrize("name", ["courseflow_test", "courseflow_migrations_test"])
def test_test_databases_are_accepted(name: str) -> None:
    assert require_test_database_name(_url(name)) == name


@pytest.mark.parametrize(
    "name",
    [
        "courseflow",
        "postgres",
        "courseflow_test_backup",
        "test",
        "Courseflow_test",
        "courseflow_test\n",
        "x_test; DROP DATABASE courseflow",
        None,
    ],
)
def test_non_test_databases_are_refused(name: str | None) -> None:
    with pytest.raises(ValueError, match="_test"):
        require_test_database_name(_url(name))
