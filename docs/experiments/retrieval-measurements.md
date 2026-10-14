# Retrieval measurements

- **Date:** 2026-10-14
- **Provider / model:** Nebius Token Factory, `Qwen/Qwen3-Embedding-8B`
- **Embedding space:** 4096 dimensions, `space_version` `v1`; input format `document/v1`
- **Chunker:** `span-pack/v1`

**These are observations from one development environment, not production performance claims.** Every number below comes from a single run with small samples. Network conditions were not controlled or characterised. Model IDs, prices and provider behaviour change; see the [model catalog snapshot](model-catalog.md).

All inputs were synthetic: generated word sequences, or invented sample documents. No course material, source text or query text was sent for these measurements or is recorded here.

## Environment

| | |
|---|---|
| Host | Windows 11 workstation, AMD x86-64 CPU (16 logical CPUs) |
| Containers | Docker Desktop 29.8.0, WSL2 kernel 6.18, 16 CPUs and about 7.7 GiB of memory available to containers |
| Database | PostgreSQL 17.11 with pgvector 0.8.6 (container) |
| Commands | run inside the API container (`docker compose run --rm --no-deps api python -m ...`) |
| Region | not recorded: the runtime does not report it |

## 1. Embedding throughput

Command: `python -m app.retrieval.measure throughput --assumed-usd-per-million-tokens 0.01`. Running `--plan-only` first printed the plan without sending anything.

**Definitions (fixed before the run):**

- **Inputs:** synthetic text of exactly 200 or 800 *estimated* tokens each. The estimate is CourseFlow's `ceil(chars / 4)`, not a tokenizer count; every input is unique.
- **Matrix:** for each input size × batch size (1, 8, 32, 64), one warm-up request (not measured), then 3 measured requests, sent sequentially.
- **Budget:** a hard cap of 40 external requests; a larger plan is refused before any request is sent. The plan needed 32.
- **`latency_ms`:** wall time of one provider request as observed by CourseFlow, around the provider call only (it would include provider retries and backoff; recording the `ModelCall` is excluded).
- **p50 / p95:** nearest-rank over measured requests that needed a single attempt. With 3 samples, p95 is the slowest of the three.
- **inputs/s:** inputs in the batch divided by latency, the median over the measured requests.
- **reported tokens/s:** provider-reported `prompt_tokens` divided by latency, the median. It would be shown as unavailable if usage were missing, never replaced by the estimate.
- **estimated ÷ reported:** the sum of CourseFlow's estimated tokens divided by the sum of provider-reported `prompt_tokens`.

**Run:** 32 of 32 requests completed, each recorded as a `ModelCall`. There were **0 retried requests** (retry rate 0 in every cell) and usage was reported for every request. Provider-reported prompt tokens totalled 444,979. At an *assumed* catalog price of $0.01 per 1M input tokens that computes to about $0.0044. This is an assumption, not a billing figure.

| Input size (est. tokens) | Batch | p50 latency (ms) | p95 latency (ms) | inputs/s | reported tokens/s | estimated ÷ reported |
|---:|---:|---:|---:|---:|---:|---:|
| 200 | 1 | 339 | 572 | 3.0 | 629 | 0.939 |
| 200 | 8 | 1,513 | 2,803 | 5.3 | 1,129 | 0.937 |
| 200 | 32 | 1,457 | 1,589 | 22.0 | 4,687 | 0.935 |
| 200 | 64 | 4,574 | 4,808 | 14.0 | 2,994 | 0.935 |
| 800 | 1 | 209 | 627 | 4.8 | 4,039 | 0.946 |
| 800 | 8 | 950 | 2,689 | 8.4 | 7,121 | 0.946 |
| 800 | 32 | 3,608 | 6,985 | 8.9 | 7,499 | 0.946 |
| 800 | 64 | 5,911 | 18,197 | 10.8 | 9,155 | 0.946 |

**Observations (not conclusions):**

- **Batching raised throughput in this run**, but not monotonically, and latency varied widely between repetitions (one 800 × 64 request took about 18 s). Three samples per cell cannot separate provider behaviour from network noise.
- **The chars/4 estimate was about 5–6% lower than the provider-reported tokens** for this synthetic text, so it is not a conservative bound on real tokens. Even so, a 1,000-estimated-token chunk stays far below the model's context length; see [ADR-007](../decisions/ADR-007-chunking-embeddings-and-vector-search.md).
- **The 800 × 64 cells exceeded CourseFlow's own default batch budget.** At about 51,200 estimated tokens per request they are above the 32,000 budget (`EMBEDDING_BATCH_MAX_ESTIMATED_TOKENS`). Indexing never sends such a request; the experiment deliberately probed beyond it, and the provider accepted it.

## 2. Chunker speed (offline)

Command: `python -m app.retrieval.measure chunker` (no provider). Median wall time of `chunk_version` over 5 repetitions, in the API container.

| Document | Characters | Spans | Chunks | Median wall time | Chunks/s |
|---|---:|---:|---:|---:|---:|
| Golden sample document (headings, Unicode, an oversized paragraph) | 20,311 | 11 | 9 | 4.1 ms | about 2,200 |
| Synthetic upper bound (pages of sentences) | 2,001,764 | 838 | 838 | 135 ms | about 6,200 |

Chunking itself is cheap even at the 2,000,000-character ingestion limit. Writing those chunks is dominated by the database provenance check (see the [retrieval architecture](../architecture/retrieval.md#cost-of-the-database-check)).

## 3. Live retrieval smoke run

Command: `python -m app.retrieval.smoke_test`, run twice. It uses invented courses: course A has a coursework brief with two versions, lecture notes and a lab sheet; course B is a distractor whose text closely matches the questions. Queries are identified by label only.

| Check | Run 1 | Run 2 (rerun) |
|---|---|---|
| Configured model and 4096 dimensions; stored vectors are 4096-dimensional | pass | pass |
| Document `ModelCall`s recorded (one per batch) | 4 calls | 0 calls: everything reused |
| Course isolation (course B never returned) | pass | pass |
| Stale version excluded (brief v1 never returned) | pass | pass |
| Active space only; span evidence on every hit | pass | pass |
| Labelled target in the top 5 (Q1, Q2, Q3) | rank 1, 1, 1 | rank 1, 1, 1 |
| Query `ModelCall`s recorded (standalone, `query/v1`) | 3 | 3 more (query vectors are never cached) |
| New courses, versions, chunks, vectors or links on rerun | n/a | none |

Single observations from run 1:
- indexing course A (3 versions, 4 chunks, 3 provider requests) took 987 ms;
- each query embedding took 124–162 ms;
- each whole search (two verifications, embedding, ranking, evidence) took 193–241 ms;
- target scores were 0.65, 0.84 and 0.64, against at most 0.33 for the next hit.

**Limitation:** course A has only 4 chunks, so "in the top 5" is easy to meet by count; rank 1 with a clear score gap is the meaningful signal here. This is a functional acceptance check, not a retrieval-quality evaluation.
