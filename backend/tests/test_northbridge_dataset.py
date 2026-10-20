# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""The committed Northbridge demo institution: byte-stable generated files,
a valid manifest that lists exactly them, and the intended v1/v2 timeline."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from app.connectors.base import ChangeKind
from app.connectors.demo import DemoConnector
from app.demo.generate import DATA_ROOT, build_all, differences
from app.demo.northbridge_content import DOCUMENTS
from app.evaluation.retrieval.resolve import production_limits
from app.ingestion.dispatch import parse_source
from app.ingestion.schemas import RawSource

pytestmark = pytest.mark.anyio


def test_committed_files_are_exactly_a_fresh_render() -> None:
    assert differences(DATA_ROOT, build_all()) == []


def test_rendering_is_byte_stable_in_a_separate_process() -> None:
    """A fresh interpreter (new hash seed, new import state) renders the same bytes."""
    code = (
        "import hashlib, json; from app.demo.generate import build_all; "
        "print(json.dumps({k: hashlib.sha256(v).hexdigest() for k, v in build_all().items()}))"
    )
    backend = Path(__file__).resolve().parents[1]
    out = subprocess.run(
        [sys.executable, "-c", code], cwd=backend, capture_output=True, text=True, check=True
    ).stdout
    here = {k: hashlib.sha256(v).hexdigest() for k, v in build_all().items()}
    assert json.loads(out) == here


def test_the_manifest_lists_exactly_the_generated_files() -> None:
    connector = DemoConnector(DATA_ROOT)
    listed = {v.file for c in connector.manifest.courses for r in c.resources for v in r.versions}
    assert listed == set(DOCUMENTS)
    assert connector.manifest.institution.fictional is True
    assert connector.manifest.institution.name == "Northbridge University"


def test_every_document_parses_with_the_production_parsers() -> None:
    for path, data in build_all().items():
        parsed = parse_source(
            RawSource(content=data, display_name=Path(path).name, source_uri="demo://n/x/y"),
            production_limits(),
        )
        assert parsed.extracted_text.strip(), path


async def test_the_timeline_has_an_initial_state_and_two_later_versions() -> None:
    connector = DemoConnector(DATA_ROOT)
    initial = {
        course.ref: [
            (d.source.source_uri.rsplit("/", 1)[1], d.version)
            for d in await connector.fetch_course_sources(course.ref)
        ]
        for course in connector.manifest.courses
    }
    assert initial == {
        "aml7001": [
            ("module-handbook", 1),
            ("lecture-03-clustering", 1),
            ("coursework-1-brief", 1),
            ("announcements", 1),
            ("lecture-07-class-imbalance", 1),
            ("workshop-07", 1),
        ],
        "msd7000": [
            ("dissertation-handbook", 1),
            ("ethics-guidance", 1),
            ("project-specification-brief", 1),
            ("research-methods-lecture", 1),
        ],
    }
    since = connector.manifest.initial_sync_at
    aml = await connector.fetch_changes("aml7001", since)
    msd = await connector.fetch_changes("msd7000", since)
    assert [(c.kind, c.document.source.display_name) for c in aml] == [
        (ChangeKind.UPDATED, "AML_Announcement_v2.html")
    ]
    assert [(c.kind, c.document.source.display_name) for c in msd] == [
        (ChangeKind.UPDATED, "Dissertation_Handbook_v2.pdf")
    ]


async def test_v2_keeps_resource_identity_with_a_different_hash() -> None:
    connector = DemoConnector(DATA_ROOT)
    since = connector.manifest.initial_sync_at
    for course_ref, key in (("aml7001", "announcements"), ("msd7000", "dissertation-handbook")):
        v1 = next(
            d
            for d in await connector.fetch_course_sources(course_ref)
            if d.source.source_uri.endswith("/" + key)
        )
        (change,) = await connector.fetch_changes(course_ref, since)
        v2 = change.document
        assert v2.source.source_uri == v1.source.source_uri
        assert (v1.version, v2.version) == (1, 2)
        assert (
            hashlib.sha256(v1.source.content).digest() != hashlib.sha256(v2.source.content).digest()
        )
