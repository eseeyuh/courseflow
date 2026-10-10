# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Ingestion is deterministic: no model calls, no AI code, no network clients."""

import ast
import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
INGESTION = BACKEND / "app" / "ingestion"

# Module prefixes that ingestion code must never import.
FORBIDDEN = (
    "app.ai", "openai", "anthropic", "httpx", "httpx2", "requests", "aiohttp", "urllib3",
    "urllib.request", "http.client", "socket",
)  # fmt: skip


def _imports(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def _forbidden(name: str) -> bool:
    return any(name == prefix or name.startswith(f"{prefix}.") for prefix in FORBIDDEN)


def test_no_ingestion_module_imports_ai_or_network_code() -> None:
    modules = sorted(INGESTION.rglob("*.py"))
    assert len(modules) >= 10  # the package was found

    offenders = {
        str(path.relative_to(BACKEND)): sorted(n for n in _imports(path) if _forbidden(n))
        for path in modules
    }
    assert {path: names for path, names in offenders.items() if names} == {}


_PARSE_EVERY_FIXTURE = """
import sys
from app.core.config import Settings
from app.ingestion.dispatch import parse_source
from app.ingestion.guards import IngestionLimits
from app.ingestion.schemas import RawSource
from tests.fixtures.ingestion.make_fixtures import FIXTURE_DIR, FIXTURES

settings = Settings(_env_file=None, database_url="postgresql+asyncpg://u:p@127.0.0.1/db")
limits = IngestionLimits.from_settings(settings)
for name in FIXTURES:
    source = RawSource(
        content=(FIXTURE_DIR / name).read_bytes(), display_name=name, source_uri="upload:" + name
    )
    try:
        parse_source(source, limits)
    except Exception as exc:  # empty fixtures raise IngestionError; that is fine here
        if type(exc).__name__ != "IngestionError":
            raise
loaded = sorted(
    m for m in sys.modules
    if m == "openai" or m.startswith(("openai.", "app.ai", "httpx"))
)
print(",".join(loaded))
"""


def test_parsing_every_fixture_loads_no_ai_or_http_client_module() -> None:
    # A fresh interpreter: other tests in this session may have imported app.ai.
    result = subprocess.run(
        [sys.executable, "-c", _PARSE_EVERY_FIXTURE],
        cwd=BACKEND,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ""
