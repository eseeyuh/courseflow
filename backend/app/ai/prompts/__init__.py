# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Versioned prompt files: ``<task>/v<N>/system.md`` and ``user.md``.

A prompt version is ``<task>/v<N>`` (with dashes, e.g.
``assessment-extraction/v1``). Wording that can change model output is a
new version, never an edit in place.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from string import Template

PROMPTS_DIR = Path(__file__).resolve().parent
_VERSION = re.compile(r"^(?P<task>[a-z][a-z0-9-]*)/v(?P<n>[1-9][0-9]*)$")


@dataclass(frozen=True, slots=True)
class PromptFiles:
    version: str
    system: str
    user: Template  # $placeholders; substituted values are never re-parsed


def load_prompt(version: str) -> PromptFiles:
    match = _VERSION.fullmatch(version)
    if match is None:
        raise ValueError(f"not a prompt version: {version!r}")
    directory = PROMPTS_DIR / match["task"].replace("-", "_") / f"v{match['n']}"
    system = (directory / "system.md").read_text(encoding="utf-8").strip()
    user = (directory / "user.md").read_text(encoding="utf-8").strip()
    return PromptFiles(version, system, Template(user))
