# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""The DemoConnector and its manifest, on small temporary institutions."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.connectors.base import ChangeKind, ConnectorError, LMSConnector
from app.connectors.demo import DemoConnector, ManifestError, load_manifest
from app.domain.enums import ResourceType

pytestmark = pytest.mark.anyio

MANIFEST = """\
schema_version: demo-manifest/v1
institution:
  key: testbridge
  name: Testbridge University
  fictional: true
initial_sync_at: 2026-09-21T09:00:00Z
users:
  - user_ref: student-a
    courses: [alpha]
courses:
  - ref: alpha
    code: TST101
    title: Testing
    resources:
      - key: brief
        type: brief
        title: Coursework brief
        active_version: 2
        versions:
          - version: 1
            file: alpha/brief-v1.html
            published_at: 2026-09-20T09:00:00Z
          - version: 2
            file: alpha/brief-v2.html
            published_at: 2026-10-05T12:00:00Z
      - key: notice
        type: announcement
        title: Deadline notice
        active_version: 1
        versions:
          - version: 1
            file: alpha/notice-v1.html
            published_at: 2026-10-01T08:00:00Z
  - ref: beta
    code: TST102
    title: Hidden
    resources:
      - key: page
        type: page
        title: Page
        active_version: 1
        versions:
          - version: 1
            file: beta/page-v1.html
            published_at: 2026-09-01T09:00:00Z
"""
FILES = {
    "alpha/brief-v1.html": b"<html><body><p>Due 1 Nov.</p></body></html>",
    "alpha/brief-v2.html": b"<html><body><p>Due 8 Nov.</p></body></html>",
    "alpha/notice-v1.html": b"<html><body><p>Notice.</p></body></html>",
    "beta/page-v1.html": b"<html><body><p>Page.</p></body></html>",
}


def institution(root: Path, manifest: str = MANIFEST, files: dict[str, bytes] = FILES) -> Path:
    for name, content in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_bytes(content)
    (root / "manifest.yaml").write_text(manifest, encoding="utf-8", newline="\n")
    return root


def problems(root: Path) -> str:
    with pytest.raises(ManifestError) as caught:
        load_manifest(root)
    return "; ".join(caught.value.problems)


async def test_initial_sources_are_the_versions_current_at_initial_sync(tmp_path: Path) -> None:
    connector: LMSConnector = DemoConnector(institution(tmp_path))
    documents = await connector.fetch_course_sources("alpha")
    assert [(d.source.display_name, d.version) for d in documents] == [("brief-v1.html", 1)]
    brief = documents[0]
    assert brief.source.source_uri == "demo://testbridge/alpha/brief"
    assert brief.source.content == FILES["alpha/brief-v1.html"]
    assert brief.resource_type is ResourceType.BRIEF and brief.title == "Coursework brief"


async def test_changes_after_a_time_are_added_or_updated_oldest_first(tmp_path: Path) -> None:
    connector = DemoConnector(institution(tmp_path))
    changes = await connector.fetch_changes("alpha", connector.manifest.initial_sync_at)
    assert [(c.kind, c.document.source.display_name) for c in changes] == [
        (ChangeKind.ADDED, "notice-v1.html"),
        (ChangeKind.UPDATED, "brief-v2.html"),
    ]
    # Every version of a resource keeps its logical identity.
    v2 = changes[1].document
    assert v2.source.source_uri == "demo://testbridge/alpha/brief" and v2.version == 2
    later = await connector.fetch_changes("alpha", datetime(2026, 10, 5, 12, tzinfo=UTC))
    assert later == []  # strictly after


async def test_users_see_only_their_courses_and_unknowns_are_refused(tmp_path: Path) -> None:
    connector = DemoConnector(institution(tmp_path))
    assert [c.course_ref for c in await connector.list_courses("student-a")] == ["alpha"]
    with pytest.raises(ConnectorError, match="unknown user"):
        await connector.list_courses("nobody")
    with pytest.raises(ConnectorError, match="unknown course"):
        await connector.fetch_course_sources("gamma")
    with pytest.raises(ConnectorError, match="timezone-aware"):
        await connector.fetch_changes("alpha", datetime(2026, 10, 1))  # noqa: DTZ001


def test_duplicate_yaml_keys_are_refused(tmp_path: Path) -> None:
    text = MANIFEST.replace("  name: Testbridge University\n", "  name: A\n  name: B\n")
    assert "duplicate key 'name'" in problems(institution(tmp_path, text))


def test_yaml_python_object_tags_are_never_constructed(tmp_path: Path) -> None:
    text = MANIFEST.replace(
        "  name: Testbridge University", "  name: !!python/object/apply:os.getcwd []"
    )
    assert "not valid UTF-8 YAML" in problems(institution(tmp_path, text))


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ("fictional: true", "fictional: false", "institution.fictional"),
        ("file: alpha/brief-v1.html", "file: ../outside.html", "relative POSIX path"),
        ("file: alpha/brief-v1.html", "file: /etc/passwd", "relative POSIX path"),
        ("file: alpha/brief-v1.html", "file: alpha/missing.html", "missing, or outside"),
        ("active_version: 2", "active_version: 1", "active_version must be the latest"),
        ("published_at: 2026-10-05T12:00:00Z", "published_at: 2026-09-19T12:00:00Z", "strictly increase"),
        ("published_at: 2026-09-20T09:00:00Z", "published_at: 2026-09-20T09:00:00", "timezone"),
        ("    courses: [alpha]", "    courses: [alpha, zeta]", "unknown courses"),
        ("type: brief", "type: memo", "type"),
    ],
)  # fmt: skip
def test_invalid_manifests_are_refused(tmp_path: Path, old: str, new: str, message: str) -> None:
    assert MANIFEST.count(old) >= 1
    assert message in problems(institution(tmp_path, MANIFEST.replace(old, new, 1)))
