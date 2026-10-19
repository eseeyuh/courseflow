# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""The Northbridge snapshot plan: its inputs are exactly the production
document inputs of every corpus version, pinned to the frozen labels."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from app.ai.embeddings import DOCUMENT_INPUT_FORMAT, EmbeddingSpace
from app.ai.nebius_transport import PROVIDER_NAME
from app.demo.snapshot import corpus_inputs, snapshot_subject
from app.evaluation.northbridge.labels import load_golden
from app.evaluation.retrieval.record import plan_recording

SPACE = EmbeddingSpace(PROVIDER_NAME, "Qwen/Qwen3-Embedding-8B", 4096, "v1")


def test_inputs_are_every_version_s_document_inputs_and_no_queries() -> None:
    golden = load_golden()
    inputs = corpus_inputs(golden.corpus)
    assert inputs == corpus_inputs(golden.corpus)  # deterministic
    assert inputs.queries == ()
    assert {i.input_format for i in inputs.documents} == {DOCUMENT_INPUT_FORMAT}
    versions = sum(len(r.versions) for c in golden.corpus.courses for r in c.resources)
    assert len(inputs.documents) == versions == 12  # each short document is one chunk


def test_the_plan_is_pinned_to_the_frozen_labels_and_inside_the_bounds(tmp_path: Path) -> None:
    golden = load_golden()
    plan = plan_recording(
        snapshot_subject(golden),
        SPACE,
        batch_max_items=64,
        batch_max_estimated_tokens=32_000,
        attempts_per_request=3,
        price_usd_per_million=Decimal("0.01"),
        output=tmp_path / "s.jsonl",
        inputs=corpus_inputs(golden.corpus),
    )
    report = plan.describe()
    assert report["dataset"] == {
        "name": "northbridge-golden",
        "version": "v1",
        "sha256": golden.sha256,
    }
    assert (plan.logical_requests, plan.max_http_attempts) == (1, 3)
    assert plan.problems() == []
