# CourseFlow v0.1.0: technical facts

One reference for submission copy and interviews. Only verified facts, with their sources. If a claim is not here, check before making it.

## Stack

| Layer | What |
|---|---|
| Frontend | Next.js 16.3.8, React 19.2.8, TypeScript, Tailwind CSS 4 |
| Backend | Python 3.12, FastAPI, SQLAlchemy 2 (async, asyncpg), Pydantic v2, Alembic (12 migrations) |
| Database | PostgreSQL 17 with pgvector 0.8.6 (image `pgvector/pgvector:0.8.6-pg17`): relational state, vectors and full-text search in one database |
| Model provider | Nebius Token Factory, OpenAI-compatible API, official `openai` SDK |
| Architecture | Modular monolith ([ADR-001](../decisions/ADR-001-modular-monolith.md)) |

## Models

| Role | Model | Used for |
|---|---|---|
| `MODEL_FAST` | `nvidia/Nemotron-3_5-Lightning` | Assessment/deadline extraction (`assessment-extraction/v2`), requirement and obligation extraction (`requirement-extraction/v1`, `obligation-extraction/v1`), material verifier (`material-mapping/v1`) |
| `MODEL_STRONG` | `nvidia/nemotron-3-super-120b-a12b` | Evaluated against FAST on assessment extraction; not used by the demo |
| Embeddings | `Qwen/Qwen3-Embedding-8B`, 4,096 dimensions | Chunk and query vectors |

Settings for every model call: native `json_schema` with `strict: true`, temperature 0, thinking off, bounded retries, one repair round. Source: [nvidia-nebius.md](nvidia-nebius.md).

## Retrieval

Hybrid search: pgvector cosine similarity and PostgreSQL full-text, fused by reciprocal rank fusion. It is the default mode, chosen by a rule declared before scoring ([ADR-008](../decisions/ADR-008-hybrid-retrieval-and-evaluation.md)).

Benchmark retrieval/v1, 12 positive cases, replayed embeddings ([report](../../evals/reports/retrieval-v1.md)):

| Mode | Recall@1 | Recall@3 | Recall@5 | MRR@10 |
|---|---|---|---|---|
| hybrid | 9/12 | 12/12 | 12/12 | 31/36 |

The material verifier sees the top 5 retrieved teaching chunks and answers candidate IDs or `none`; a deterministic subset gate rejects anything else.

## Evidence architecture

- **Chain:** `EvidenceLink` (exact quote offsets) → `SourceSpan` (page, slide or section) → `ResourceVersion` (content hash) → `Resource` → `Course`.
- **Statuses:** `verified`, `needs_review`, `rejected`. Rejected facts are not stored as rows.
- **Verified** requires the quote to be exact and unique in a span of the same course and version, and to support the value (for example, the parsed date equals the claimed date).
- **Deterministic rules:** `dependency-rules/v1`, `conflict-rules/v1`, `source-supersession/v1`, `study-plan/v1`.
- **Workflow:** `course-intelligence/v1`, with steps ingest, assessments, claims, mapping, dependencies, conflicts, validation, plan. Each course gets one traced `WorkflowRun`.

## Measured results

Real Token Factory calls on the frozen synthetic golden dataset `northbridge-golden` v1. Each was run once, under a spend cap. Reports are in [evals/reports/](../../evals/reports/).

| Run | Result | Calls | Cost |
|---|---|---|---|
| Assessment extraction, FAST ([report](../../evals/reports/assessment-extraction-v2-fast-thinking-off-gate-v3.md)) | Deadline exact match 6/6; 0 false verified assessments; injection and abstention checks pass; p50 latency 901 ms | 13 | $0.001439 |
| Assessment extraction, STRONG ([comparison](../../evals/reports/assessment-extraction-v2-fast-vs-strong.md)) | Deadline exact match 0/6 (correct dates, quotes rejected by the gate) | 13 | $0.004886 |
| Requirement and obligation extraction, FAST ([report](../../evals/reports/claim-extraction-v1-fast-thinking-off-gate-v3.md)) | Requirements labelled recall 11/11; obligations 2/3, supervisor-review case verified; ambiguous statements never verified | 15 | $0.001857 |
| Material mapping, FAST verifier ([report](../../evals/reports/material-mapping-v1-fast-thinking-off.md)) | 4/4 demo acceptance checks pass (class imbalance → Lecture 07; irrelevant lecture rejected; no-material case unmapped; mappings ⊆ retrieved candidates); 58 stored mappings | 11 | $0.00129 |

Labels are not exhaustive, so only labelled recall is reported, not precision or F1. A single run per model does not establish superiority.

## Demo and replay

- **Offline.** The demo rebuilds from committed recordings: model outputs in `evals/recordings/` and embedding snapshots in `evals/snapshots/northbridge/v1/`. It needs no API key, no network and no spend, and is deterministic. The replayed outputs pass through the same gates and rules.
- **Data.** Synthetic Northbridge University: Applied Machine Learning and MSc Dissertation. The demo clock runs from 5 Oct 2026, 08:00 UTC (initial) to 21 Oct 2026, 09:00 UTC (after the sync).
- **One command:** `docker compose -f infra/demo/compose.yml up -d --build --wait`, with `APP_ENV=demo`. The demo endpoints are refused only in `production`.
- **Golden-path test.** Playwright, 1 test covering reset, every demo step, sync and reset again. It passed 3 consecutive times on the development stack and once as the release smoke on the Docker stack.

## Tests

- **Backend:** 1,956 pytest tests pass. They never call a real model; the provider tests use a scripted transport.
- **Frontend:** ESLint, TypeScript `--noEmit` and the production build are clean.
- **Secrets:** gitleaks reports no leaks.

## Known limitations

- Simulated LMS (`DemoConnector`): no live Moodle connector, no links to original LMS pages.
- The demo replays recorded model outputs; new content needs an API key and the evaluation runners.
- One demo learner, no authentication; demo endpoints are unauthenticated.
- Material mapping lists several slides of the right lecture, and the stored retrieval ranks do not separate them.
- Obligation labelled recall is 2/3, and the labels are incomplete.
- Desktop-first UI. No public hosted URL is included.
