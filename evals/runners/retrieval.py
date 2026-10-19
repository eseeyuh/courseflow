# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Thin entry point for the retrieval evaluation; the logic lives in
``backend/app/evaluation/retrieval/runner.py``. From ``backend/``:

    uv run python ../evals/runners/retrieval.py --dataset ../evals/datasets/retrieval/v1 \
        --snapshot ../evals/snapshots/retrieval/v1/nebius-qwen3-embedding-8b-d4096-sv1.jsonl \
        --expect-dataset-sha <sha256> --expect-snapshot-sha <sha256>
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))

from app.evaluation.retrieval.runner import main

if __name__ == "__main__":
    sys.exit(main())
