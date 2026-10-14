# ADR-007 — Provenance-preserving chunking, embedding provider and vector search

- **Status:** Accepted
- **Date:** 2026-10-14

## Context

CourseFlow has to find the original course material that relates to a requirement or a question, and then show *exactly where* it is. Ingestion already stores each source as an immutable `ResourceVersion` with structural `SourceSpan`s: pages, slides and sections with exact offsets ([ADR-006](ADR-006-document-ingestion-and-raw-storage.md), [data contract](../architecture/data-contract.md)).

Retrieval needs units of a useful size, vectors from an embedding model, and similarity search. These must not weaken the evidence chain:

- a retrieved unit must resolve exactly to its spans;
- one course must never see another course's material;
- a superseded version must never be returned;
- an index that is incomplete or inconsistent must never be searched as if it were complete;
- paid model calls must be recorded, and must not be repeated needlessly.

The scale for v0.1 is a few synthetic courses: tens of documents and thousands of chunks at most. Storage is PostgreSQL with pgvector ([ADR-002](ADR-002-postgres-pgvector.md)), and model access goes through the provider boundary of [ADR-005](ADR-005-ai-provider-boundary.md).

## Decision

### 1. Two units with different jobs

- **`SourceSpan` is the evidence unit and the source of truth.** It is created once by ingestion and never changed.
- **`Chunk` is a disposable retrieval unit.** It is derived deterministically from one version's text and spans, and identified by its chunker version.

A chunk is only ever a pair of offsets into the version's `extracted_text`. It stores a range of span ordinals (first to last), never its own copy of course or resource identity: those come through its version. Evidence presented to a user is always a `SourceSpan` plus a sub-range, never a chunk.

### 2. Chunker `span-pack/v1`

- **Greedy packing of whole spans**, bounded in *estimated* tokens: `ceil(chars / 4)` is a deterministic packing heuristic, not a tokenizer count.
  - target 800, hard maximum 1,000;
  - overlap up to 120;
  - spans above 880 are cut inside themselves at the best boundary (paragraph, line, sentence, whitespace; a hard cut only for text with no whitespace);
  - a final chunk below 200 merges backward when it fits.
- **Structure:** a slide's text and notes stay together, and headings attach to the content that follows. Overlap is added only where text flows on: never before a heading or a new slide, but across page breaks.
- **Checked on every run:** every chunk is re-checked (exact slice, containment in its first and last span, hard maximum, no edge whitespace), together with whole-version coverage: every span with visible text, and every non-whitespace character, is in some chunk.
- **Determinism:** the output is pinned by golden tests, including in a separate process.
- **Versioning:** changing any parameter or rule requires a new chunker version.

### 3. Embedding provider and space

- **Provider and model:** Nebius Token Factory over its OpenAI-compatible API, model **`Qwen/Qwen3-Embedding-8B`**, called through a separate `EmbeddingProvider` boundary.
- **Dimensions:** **4096**, always sent explicitly, never left to a provider default. Float encoding is always requested.
- **`EmbeddingSpace`** = provider + *configured* model ID + dimensions + `space_version`. `space_version` is CourseFlow's own compatibility revision, not an upstream model revision: the provider controls what runs behind a model ID, and CourseFlow cannot detect a change there, so bumping `space_version` is how stored vectors are deliberately invalidated. A model ID that a response reports is kept in telemetry only.
- **Asymmetric input formats in the same space:**
  - `document/v1` embeds chunk text unchanged;
  - `query/v1` prefixes the question with a fixed task instruction, as the model expects.

  The formats are versioned and hashed exactly as sent; changing either means a new format version.
- **Validation:** every response is checked (count, indices, length, finite and non-zero values). A violation is recorded as `invalid_output` and never retried.

### 4. Storage

- **`Embedding`:** a global, content-addressed cache, with one vector per (space, input format, SHA-256 of the exact input), reused across chunks, versions and courses. It lives in one `vector` column without a fixed dimension, so spaces of different sizes can coexist. Each row must match its space's dimensions and must not be all zeros, both enforced by the database.
- **`ChunkEmbedding`:** the explicit link from a chunk to a document-format vector. There is at most one per (chunk, space, format); query vectors can never be linked.
- **Provenance checks:** a database trigger re-checks each chunk's provenance (exact slice and span containment) as a backstop behind the same checks in the application.
- **`ModelCall`:** gains `call_kind` (`chat_structured` or `embedding`). Embedding calls have no model role.

### 5. Indexing transactions

A run indexes the course's current versions as of its start, a snapshot recorded as its inputs.

- **Paid vectors:** each successful provider request is committed together with the vectors it produced, independently of any version.
- **Atomic publication:** each version's chunk set and link set are published in one locked transaction (`FOR NO KEY UPDATE` on the version row), so a version is never partly searchable.
- **No repair:** inconsistent existing chunks or links are reported, never repaired silently.
- **No network inside a transaction:** no transaction is held across a provider call.
- **Concurrency:** persistence is strictly idempotent. Concurrent cache misses **may make duplicate paid provider calls**; each is recorded, and the database keeps one canonical vector per key.
- **Run record:** `WorkflowRun.summary` (`index/v1`) records the counts.

### 6. Search

- **Exact cosine scan, with no approximate index.** `score = 1 - cosine distance` is a ranking similarity, never a confidence.
- **Scoping:** candidates are restricted to the requested course, each resource's current version, the active chunker version, the active space (on both the link and the vector row), exact `document/v1`, and optional resource types.
- **The trusted-state invariant:** only versions whose stored chunks are exactly the chunker's output, with complete and correct links, are searched. One inconsistent current version fails the whole search. The corpus is verified before the paid query call, so nothing is paid for a search that cannot run, and again after it, without holding any lock across it. Only versions verified the second time are ranked.
- **Ephemeral query vectors:** each search is one recorded provider call; the query vector is never stored or cached.
- **Evidence:** every hit carries its spans with exact overlap ranges, re-checked against the chunk. Any inconsistency fails the search rather than return a partly trustworthy list.

## Alternatives considered

| Alternative | Why not (for this version) |
|---|---|
| A local embedding model in the API container | Adds a large model and CPU or GPU work to the image. One provider boundary already exists and was verified live. The single embedding model in the provider's catalog fits the need. |
| Index spans directly (span = retrieval unit) | Spans range from a few words to whole pages: too small to carry meaning, or too large to embed well. A separate, disposable chunk keeps evidence units stable while retrieval units can change. |
| Fixed-size character windows | Cut through headings, slides and sentences, and lose structure. Packing whole spans keeps meaning and exact provenance. |
| Concatenating span texts into a chunk | Would create text that never existed in the source. Offset-only chunks are always an exact slice. |
| Vector stored on the chunk | Ties a chunk to one model and space. A separate cache plus explicit links allows several spaces per chunk and reuse of identical text. |
| Cache keyed by text hash only | Not safe across spaces or input formats. The full key is (space, format, input hash). |
| HNSW or IVFFlat index | Unnecessary at this scale, and changes results. pgvector's ANN indexes accept at most 2,000 dimensions for `vector` and 4,000 for `halfvec`, and need a fixed dimension (an expression index with a cast). 4096-dimension vectors as stored cannot be indexed without reduction or quantisation. |
| A separate vector database | Would split vectors from provenance and filters ([ADR-002](ADR-002-postgres-pgvector.md)). |
| Caching query vectors | Persists user questions (or their hashes) for little benefit at this scale. |
| Searching whatever chunks happen to have links | A partly indexed or damaged version would look searchable. The derived, verified state is the only trusted signal. |

## Consequences

**Positive**

- Every hit resolves to exact source spans, and evidence never depends on chunk boundaries.
- Re-indexing unchanged content costs no provider calls. Identical text is embedded once across versions and courses, and a new embedding space adds vectors without re-chunking.
- Course and version isolation, and the trusted-state rule, are enforced in SQL and covered by tests, including races during the provider call.
- Every paid request is recorded as a `ModelCall`, when the database can store it.

**Negative / risks**

- **The row-level provenance trigger re-reads the version text for every chunk row**, so its total cost grows roughly quadratically with one version's size. It was measured at about 11.7 s for a synthetic 2,000,000-character, 2,000-chunk version on a development machine ([retrieval architecture](../architecture/retrieval.md)). Normal documents are far smaller. It is accepted as a backstop and never disabled by document size.
- **Duplicate paid calls are possible under concurrent cache misses.** Accepted for v0.1, where the CLI is the only writer.
- **The `chars/4` estimate is not a token count.** For the synthetic text measured, the provider reported about 6% more tokens than the estimate ([measurements](../experiments/retrieval-measurements.md)). Even a hard-maximum chunk remains far below the model's context length, but the estimate is not a guaranteed real-token bound.
- **Exact search scans every candidate vector in the course**, so cost grows linearly with chunks.
- **Every search makes a paid call.**
- **Telemetry is best effort when storage fails.** If storing vectors fails after a successful paid call, CourseFlow makes a best-effort separate attempt to record that call; if the database is unavailable, the telemetry is lost.

## Revisit when

- **Search latency:** exact scan latency for a realistic course exceeds an agreed budget. Then consider an ANN index, which needs reduced dimensions (2,000 or fewer for `vector`, 4,000 or fewer for `halfvec`) or binary quantisation, as a new embedding space.
- **Large documents:** indexing them becomes a measured bottleneck. Then replace the row trigger with a statement-level validator, or change how the text is represented.
- **More than one writer:** concurrent indexing becomes normal (background workers). Then consider per-key or per-version advisory locks to avoid duplicate paid calls.
- **Evaluation results:** retrieval evaluation shows recall or ranking problems that chunk size, overlap or input formats could address. Then introduce a new chunker or format version.
- **Model changes:** the provider changes or retires the model. Then pick a new model and bump the space.
