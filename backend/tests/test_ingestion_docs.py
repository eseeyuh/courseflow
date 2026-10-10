# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""The public ingestion guide is the source of truth for the CLI's exit codes
and the rejection categories; the code must match it exactly."""

import re
from pathlib import Path

from app.domain.enums import IngestionErrorCategory
from app.ingestion import cli

GUIDE = Path(__file__).resolve().parents[2] / "docs" / "architecture" / "ingestion.md"


def _table_after(heading_text: str) -> list[str]:
    """First column of the first Markdown table after a line containing ``heading_text``."""
    lines = GUIDE.read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if heading_text in line)
    rows = []
    for line in lines[start + 1 :]:
        if line.startswith("|"):
            rows.append(line.split("|")[1].strip())
        elif rows:
            break
    return rows[2:]  # skip header and separator


def test_documented_exit_codes_are_exactly_the_cli_exit_codes() -> None:
    documented = {int(cell.strip("`")) for cell in _table_after("Logs go to stderr as JSON lines")}
    in_code = {value for name, value in vars(cli).items() if re.fullmatch(r"EXIT_[A-Z_]+", name)}
    assert documented == in_code == {0, 1, 2, 3, 4, 5, 64}


def test_documented_rejection_categories_are_exactly_the_taxonomy() -> None:
    documented = {cell.strip("`") for cell in _table_after("Each rejection has exactly one")}
    assert documented == {category.value for category in IngestionErrorCategory}
