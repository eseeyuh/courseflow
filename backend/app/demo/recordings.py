# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""The frozen recordings the Northbridge demo is rebuilt from, offline.

Every file here is committed (``evals/recordings/`` and
``evals/snapshots/``), never under the gitignored ``evals/runs/``, so a
fresh clone rebuilds the same demo intelligence with no network and no
spend. They hold validated model outputs keyed by a nonce-free material
hash, or embedding vectors: no prompts, headers, credentials or logs.
"""

from __future__ import annotations

from app.demo.snapshot import SNAPSHOT_PATH
from app.evaluation.northbridge.labels import REPO_ROOT

RECORDINGS = REPO_ROOT / "evals" / "recordings"

# FAST, thinking off, assessment-extraction/v2 under assessment-gate/v3.
ASSESSMENT_OUTPUTS = (
    RECORDINGS
    / "assessment-extraction"
    / "assessment-extraction-v2-fast-thinking-off-gate-v3-outputs.json"
)
ASSESSMENT_PROMPT = "assessment-extraction/v2"
# FAST, thinking off, requirement- and obligation-extraction/v1 under claim-gate/v3.
CLAIM_OUTPUTS = (
    RECORDINGS / "claim-extraction" / "claim-extraction-v1-fast-thinking-off-gate-v3-outputs.json"
)
# FAST, thinking off, material-mapping/v1.
MAPPING_OUTPUTS = (
    RECORDINGS / "material-mapping" / "material-mapping-v1-fast-thinking-off-outputs.json"
)
# Document vectors (indexing) and mapping query vectors (search).
DOCUMENT_EMBEDDINGS = SNAPSHOT_PATH
MAPPING_QUERY_EMBEDDINGS = (
    SNAPSHOT_PATH.parent / "mapping-queries-nebius-qwen3-embedding-8b-d4096-sv1.jsonl"
)
# The supplementary courses' vectors, pinned to the supplement's own hash.
# Their extraction and mapping answers are authored, not recorded (supplement).
SUPPLEMENT_DOCUMENT_EMBEDDINGS = (
    SNAPSHOT_PATH.parent.parent / "supplement-v1" / "nebius-qwen3-embedding-8b-d4096-sv1.jsonl"
)
SUPPLEMENT_QUERY_EMBEDDINGS = (
    SUPPLEMENT_DOCUMENT_EMBEDDINGS.parent
    / "mapping-queries-nebius-qwen3-embedding-8b-d4096-sv1.jsonl"
)

ALL = (
    ASSESSMENT_OUTPUTS,
    CLAIM_OUTPUTS,
    MAPPING_OUTPUTS,
    DOCUMENT_EMBEDDINGS,
    MAPPING_QUERY_EMBEDDINGS,
    SUPPLEMENT_DOCUMENT_EMBEDDINGS,
    SUPPLEMENT_QUERY_EMBEDDINGS,
)
