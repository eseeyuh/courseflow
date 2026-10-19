# Evaluation

CourseFlow measures retrieval and AI quality on frozen, versioned datasets, so that a claim like "hybrid is better" is a reproducible result rather than an impression.

## Layout

| Path | Contents |
|---|---|
| `datasets/<task>/vN/` | Frozen datasets: corpus, cases, a manifest with versioned rules, and a SHA-256 |
| `snapshots/<task>/vN/` | Recorded model outputs (embeddings), replayed offline |
| `runners/` | Thin entry points; the logic lives in `backend/app/evaluation/` |
| `reports/` | Curated reports, committed deliberately |
| `runs/` | Raw timestamped run output (ignored by git) |

## Rules

- **Freeze before scoring.** A dataset is committed with its SHA-256 before any score is computed on it. It is never edited afterwards: a labelling error is fixed in a new version, and results for the old version stay valid for that version.
- **Declare decisions before results.** Metrics, comparisons and selection rules are fixed in code before a run. Parameters are not tuned after seeing scores; a new parameter set is a new experiment.
- **Hard invariants are not metrics.** Course isolation and current-version-only are pass/fail. A violation fails the run; it never just lowers a score.
- **Replay, never fall back.** Scored runs replay recorded embeddings. An input missing from the snapshot fails the run instead of triggering a live call.
- **No text in outputs.** Reports and logs contain IDs, ranks and counts, never source or query text.
- **No significance claims on small datasets.**

## Retrieval benchmark v1

**Dataset:** `datasets/retrieval/v1`, SHA-256 `07c11efaea7efd516f6d6ec0bdb27229008bd0f1972890fb82d4827e0ba45747`. Its [README](datasets/retrieval/v1/README.md) has the full description.

- **Corpus:** fully synthetic. Two fictional courses, 13 resources, 14 versions (one coursework brief is superseded), 39 current-version chunks and 41 in all. All documents are HTML.
- **Cases:** 14 in total. 12 positive cases (4 keyword, 5 paraphrase, 3 mixed) and 2 invariant-only probes (stale version, cross-course).
- **Relevance (`full-target-containment/v1`):** a hit counts only if the chunk wholly contains the labelled quote's range.
- **Metrics:** Recall@1/3/5 and MRR@10, computed in exact rationals over the 12 positive cases. The invariant-only cases are excluded from every denominator.
- **Critical case:** `ds101-mixed-cw1-deadline` must be in the top 3 in the selected default mode.

**Snapshot:** `snapshots/retrieval/v1/nebius-qwen3-embedding-8b-d4096-sv1.jsonl`, SHA-256 `56509e7d364dbd403a6932ea98272a5334cc0f0a2bdb0d468645632429371941`.

- It holds `Qwen/Qwen3-Embedding-8B` vectors (4096 dimensions, space `v1`) for exactly the 55 inputs the dataset needs: 41 `document/v1` and 14 `query/v1`.
- They were recorded in 2 provider requests, and the snapshot stores no text.
- The format, the `sha256-input-keys/v1` input-set hash and the `sha256-file-bytes/v1` snapshot ID are defined in `backend/app/evaluation/retrieval/snapshot.py`.

### Results

0 hard-invariant failures in every mode.

| Mode | Recall@1 | Recall@3 | Recall@5 | MRR@10 |
|---|---|---|---|---|
| vector | 9/12 | 11/12 | 12/12 | 121/144 (≈ 0.840) |
| lexical | 9/12 | 11/12 | 12/12 | 17/20 (= 0.850) |
| hybrid | 9/12 | 12/12 | 12/12 | 31/36 (≈ 0.861) |

**Selected default: hybrid,** by `default-selection/v1`, which was fixed before any score:

- Start with vector. Lexical replaces it if it passes (no invariant failure, critical case in the top 3) and is at least as good on Recall@5 and MRR@10.
- Hybrid then replaces the remaining single retriever if it passes, is at least as good on both metrics, and is strictly better on one.
- Here lexical replaced vector (equal Recall@5, higher MRR@10), and hybrid replaced lexical (equal Recall@5, higher MRR@10).

The benchmark is small, so the differences are not claimed to be statistically significant. Per-case ranks and distractor positions are in [reports/retrieval-v1.md](reports/retrieval-v1.md). The reasoning is in [ADR-008](../docs/decisions/ADR-008-hybrid-retrieval-and-evaluation.md).

### Running it

The evaluation drops and recreates a dedicated database on every run. It must be set explicitly, never derived from `DATABASE_URL`. The name must be `courseflow_eval`, the host must be loopback or the Compose `db` service, and the URL may have no query parameters (a driver could apply them over the checked host or database):

```bash
export COURSEFLOW_EVAL_DATABASE_URL=postgresql+asyncpg://courseflow:<password>@127.0.0.1:5432/courseflow_eval
cd backend
uv run python ../evals/runners/retrieval.py \
  --dataset ../evals/datasets/retrieval/v1 \
  --snapshot ../evals/snapshots/retrieval/v1/nebius-qwen3-embedding-8b-d4096-sv1.jsonl \
  --expect-dataset-sha 07c11efaea7efd516f6d6ec0bdb27229008bd0f1972890fb82d4827e0ba45747 \
  --expect-snapshot-sha 56509e7d364dbd403a6932ea98272a5334cc0f0a2bdb0d468645632429371941
```

Each run does the following:

1. Recreates and migrates the evaluation database.
2. Seeds the corpus through the production ingestion and indexing code with replayed vectors. A superseded version is indexed while it is current, then superseded.
3. Checks the exact expected database state.
4. Runs every case in every mode through the production search service.
5. Writes JSON and Markdown reports to `runs/retrieval/<timestamp>/` and to `reports/`.

It needs no API key and makes no network call. By default it runs twice (`--repeat 2`) and requires byte-identical results across the two freshly seeded databases. Only timestamps may differ.

Exit codes: 0 ok; 3 invalid input (dataset, snapshot or database guard); 6 the evaluation failed (an invariant failure, or no eligible default); 7 the runs were not identical; 1 unexpected error.

### Recording a snapshot

Recording is for a new dataset version, chunker or embedding space only. The committed v1 snapshot is never re-recorded.

```bash
uv run python -m app.evaluation.retrieval.record --dataset <dir> --snapshot <file.jsonl> \
  --expect-dataset-sha <sha256> --price-usd-per-million <current catalog price> [--live]
```

Without `--live` it prints the plan and makes no request. Before any live request it refuses a plan that needs more than 2 logical requests or 6 HTTP attempts, or whose planned maximum spend is $0.01 or more. It stops between batches if reported usage would breach that bound. It publishes the snapshot atomically and never overwrites an existing one.
