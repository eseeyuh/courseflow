# Retrieval

Retrieval turns ingested versions into searchable **chunks** with vectors, while every chunk still resolves to exact source spans. The stored fields and their rules are in the [data contract](data-contract.md#retrieval-layer). The reasoning behind the design is in [ADR-007](../decisions/ADR-007-chunking-embeddings-and-vector-search.md).

## Storage

```text
ResourceVersion ─┬─ SourceSpan (ordinal 0..n)
                 └─ Chunk (start/end offsets, first/last span ordinal)
                       │
                       └─ ChunkEmbedding (chunk, embedding, space, document format)
                              │
                              └─ Embedding (space, input format, input hash, vector) ─ EmbeddingSpace
```

- A **chunk** belongs to exactly one resource version; its resource and course come through that version, never from columns of its own.
- An **embedding** is global and content-addressed: one row per (space, input format, input hash), reused by every chunk in any course whose provider input was identical.
- A **chunk embedding** is the explicit link. Only document-format embeddings (`document/vN`) can be linked; query embeddings exist only at search time.
- Vectors live in one `vector` column without a fixed dimension. Each row must match its space's dimensions and must not be all zeros. Search always filters to one space and uses exact cosine distance; there is no approximate (ANN) index.

## Chunking: `span-pack/v1`

`app/retrieval/chunking.py` turns one version into chunks deterministically: the same text, spans and chunker version always give identical offsets. A chunk is only ever a pair of offsets into `extracted_text`; nothing is concatenated.

- Sizes are **estimated tokens**, `ceil(characters / 4)`: a packing heuristic, not a tokenizer count. Target 800, hard maximum 1,000, overlap at most 120, spans above 880 are split inside themselves, and a final chunk below 200 merges backward when it fits. Changing any of these means a new chunker version.
- Whole spans are packed where possible. A slide's text and notes (consecutive spans of the same slide) stay together, and headings attach to the content that follows them.
- An oversized span is cut at its best internal boundary: paragraph break, line break, sentence end, whitespace; a hard cut only for text with no whitespace at all.
- A chunk may start a little before the end of the previous one (overlap) where text flows on, including across page breaks, but never before a heading or a new slide.
- **Coverage guarantee:** every span with visible (non-whitespace) text lies within at least one chunk's span range, and every non-whitespace character of `extracted_text` lies within at least one chunk. Whitespace-only spans (for example an NBSP-only block, see [ingestion](ingestion.md)) get no chunk of their own.
- Known edge: if a chain of headings plus the content after it is larger than the hard maximum, the headings that do not fit form a small chunk of their own; the next chunk starts with the nearest heading that fits.

## Chunk provenance: two layers

The rule that matters most is that a chunk is an exact, contiguous slice of its version's text that starts inside its first span and ends inside its last span. It is checked twice:

1. **Application:** `app/retrieval/provenance.py` checks every chunk before it is written, so a bad chunk fails early with a clear message.
2. **Database:** the `chunks_provenance` trigger (migration 0004) checks every inserted or updated row again. It is the backstop for other writers and future bugs, and it is never disabled or skipped by document size.

### Cost of the database check

The trigger runs once per chunk row; a multi-row `INSERT` does not batch it. Each run reads the version's text and takes the chunk's slice. Total cost is therefore roughly *chunk count × slice cost*, and the slice cost itself grows with the length of the version's text (the stored text is decompressed and UTF-8 characters are counted from the start). For one version, that is approximately quadratic in its size.

Measured on a development workstation (one multi-row insert per version, synthetic text, not a benchmark):

| Version text | Chunks | With trigger | Without trigger |
|---:|---:|---:|---:|
| 20,000 chars | 20 | ~6–10 ms | ~4 ms |
| 400,000 chars | 400 | ~550 ms | ~40 ms |
| 2,000,000 chars | 2,000 | ~11.7 s | ~0.16 s |

2,000,000 characters is the ingestion limit, a defensive upper bound rather than an expected document size; normal course documents are far smaller. Storing the text uncompressed did not help materially (~10.1 s at the extreme), because counting UTF-8 characters still scans from the start.

**Revisit when** indexing large documents becomes a measured bottleneck: a statement-level validator that reads each version once and checks its chunks in offset order, or a different representation of the text, would keep the same guarantee at linear cost.

## Indexing

`python -m app.retrieval.cli index --course-id <id>` (in the API container) indexes a course and needs the provider's credentials. `status --course-id <id>` re-derives each current version's index state offline: it needs only the embedding space's identity (model, dimensions, space version), never an API key, a provider client or the network, and it writes nothing.

An unknown course fails clearly (CLI exit code 3) and creates no run. A course with no current versions is a successful no-op run: zero inputs, no provider calls, no rows other than the run itself. Code: `app/retrieval/indexing.py` (service), `store.py` (SQL), `batching.py`, `cli.py`.

A run indexes the course's **current versions as of its start**: that snapshot is recorded as the run's inputs. For each version, in a stable order:

1. Load the version and its spans; run the chunker. Existing chunks for that chunker version must be exactly the chunker's output, or the version fails (`chunk_state`); existing links in the active embedding space must be absent or complete and correct, or it fails (`link_state`). Inconsistent state is reported, never silently repaired or deleted.
2. Build each chunk's `document/v1` input; its cache key is (space, input format, SHA-256 of the exact input). Keys already resolved in this run, or cached in the database, are reused; only the rest are sent to the provider, in batches bounded by both CourseFlow budgets (items and estimated tokens).
3. Publish the version in one transaction: lock the version row (`FOR NO KEY UPDATE`), insert the chunk set if absent, insert the link set if absent, verify, commit. A version is therefore never partly searchable.

"Indexed" is derived, never stored: the stored chunks are exactly the deterministic set for the chunker version, and every chunk has exactly one correct link in the space and document format. Links in an earlier embedding space are kept; switching spaces adds new vectors and links without re-chunking.

### Transactions and paid vectors

No database transaction is open during a provider call. Each successful provider request is recorded as one `ModelCall` (`call_kind = embedding`, no model role, `prompt_version` = input format, `output_schema = vector[<dimensions>]`) **in the same transaction as the vectors it produced**. That transaction is separate from the version's publication, so vectors already paid for survive a later failure as reusable cache rows (possibly unreferenced, like an unreferenced raw object).

If storing the vectors fails, CourseFlow makes a best-effort, independent attempt to persist the successful `ModelCall` alone, then stops the run (`store_vectors`). If the database refuses that too (for example, it is unavailable), the call's telemetry cannot be persisted; only safe structured log fields (counts, IDs, error types) are emitted. A pure cache hit makes no request and creates no `ModelCall`.

### Concurrency (v0.1)

- Database persistence is strongly idempotent: unique constraints guarantee one canonical `Embedding` row per cache key, one chunk set per (version, chunker version) and one link per (chunk, space, input format).
- Concurrent cache misses may make duplicate external provider calls. Every real provider call gets its own `ModelCall` when telemetry persistence succeeds; a call whose vectors lost the insert race keeps its `ModelCall`, and the canonical row keeps its original producer.
- The version row lock serialises publication of one version's chunk and link state.
- No lock is held across a network call.

### Failures

| Failure | Effect |
|---|---|
| Version cannot be chunked; inconsistent existing chunks or links; integrity failure while publishing that version | That version fails; the run continues with the others |
| Provider failure after retries, authentication or configuration error, invalid provider output (`invalid_output`: wrong vector count, indices or dimensions, non-finite or zero vectors) | The run stops; remaining versions are not attempted. These are typed provider failures, not application bugs (`unexpected` is reserved for unclassified errors) |
| Storing vectors fails; database connectivity; an unexpected error | The run stops |

A run **succeeds** only if every input version ended indexed (newly or already). A version that was indexed but stopped being current during the run (ingestion added a newer one) still counts as a success; the summary reports it as superseded, and search excludes it because it is no longer current.

### Run summary (`workflow_version = "index/v1"`)

Stored once in `workflow_runs.summary`; a change to these meanings needs a new workflow version. Counts and IDs only: no text, text hashes, file names or source URIs.

| Field | Meaning |
|---|---|
| `versions_considered / indexed / already_indexed / failed / not_attempted / superseded` | Snapshot inputs and their outcomes |
| `chunks_created / reused` | `Chunk` rows |
| `links_created / reused` | `ChunkEmbedding` rows |
| `embeddings_created` | Unique cache keys whose `Embedding` row this run inserted |
| `embeddings_reused` | Unique cache keys this run used whose row it did not insert |
| `embedding_inputs_deduplicated` | Chunk inputs that needed no vector of their own because another chunk in the run needed the same key |
| `provider_calls / failed_provider_calls` | Real external requests, and those that failed |
| `prompt_tokens` | Sum over successful requests; empty if any did not report usage. The `ModelCall` rows remain the source of truth |
| `failures` | Per failed version: its ID, the stage and an error category or type (never a message) |

## Search

`python -m app.retrieval.cli search --course-id <id> --query "<question>" [-k 5] [--type brief] [--json]` (or `--query -` to read the question from stdin, which keeps it out of shell history). Code: `app/retrieval/search.py`.

**Search runs only on complete, verified index states.** It verifies the corpus twice, never holding a lock across the provider call. The first verification, before any paid call, avoids paying for a search that cannot run and fails obvious corrupt state early. The second, after the question is embedded and before ranking, makes sure the corpus actually ranked is still trusted and current; it never triggers a second provider request. Only the versions verified the second time are ranked. Both check that the course exists and classify every *current* version with the same derived state `status` reports:

| State | Meaning | Effect |
|---|---|---|
| searchable | chunks exactly the chunker's output, and every chunk has one correct link in the active space and `document/v1` | searched |
| unindexed | no chunks; exact chunks with no links in this space; or valid spans with no visible text (for example an NBSP-only version: nothing to retrieve) | not searched; counted in `unindexed_versions` |
| corrupt | anything else: partial links, a wrong vector, chunks that differ from the chunker's output, or spans the chunker cannot safely use (ordinals not exactly 0..n-1, out of order, outside the text, or without offsets) | **the search fails** (`IndexStateError`, CLI exit 6) |

If corruption is found by the first verification, no provider call is made. If it appears during the provider call, the search still fails and the successful query `ModelCall` stays recorded as it was. A version that stopped being current during the call is never ranked; a version that became fully indexed during the call may be ranked, because it passed the second verification.

If nothing is searchable at the first verification, the result is empty and no provider call is made; if nothing is searchable at the second, the result is empty but still names the query's `ModelCall`. Otherwise the result reports `current_versions` and `searchable_versions`, so an incomplete corpus is visible rather than hidden.

The question is embedded as `query/v1` (stripped, 1 to 2,000 characters, CourseFlow's own limit) in the **same embedding space** as the documents: same provider, model, dimensions and space version. `query/v1` and `document/v1` are input formats (the query carries a task instruction, as the model expects), not different spaces. Each search is one real provider request, recorded as one standalone `ModelCall` (no workflow run); query vectors are never stored or cached, so repeating a search pays again.

Candidates are restricted to: the requested course; each resource's current version; the versions verified as searchable; the active chunker version; the active embedding space, on both the link and the embedding row; `document/v1` exactly; matching dimensions; and the optional resource types. Ranking is an exact cosine scan (no approximate index):

```sql
ORDER BY e.vector <=> :query, rv.id, c.ordinal   -- distance, then a stable tie-break
LIMIT :k                                          -- 1..50, default 5
```

`score = 1 - cosine distance` ranks results; it is not a probability or a confidence.

**Evidence.** A chunk is retrieval context; its evidence is its source spans. Every hit lists each span from `first_span_ordinal` to `last_span_ordinal` with its locator (page, slide or section), and the exact part of the span inside the chunk: absolute offsets in the version's text and code-point offsets into the span's excerpt (`coverage` is `full` or `partial`). Each hit is re-checked: the spans exist, overlap the chunk, and the excerpt's sub-range equals the matching part of the chunk text. Any inconsistency fails the whole search (`ProvenanceError`, CLI exit 6); a partly trustworthy result list is never returned.

Search output (stdout, `--json`) deliberately shows retrieved course text and resource references. Logs never contain the question, chunk text, excerpts or source URIs.

## Inspection, statistics and measurements

- `status --course-id <id>` shows each current version's derived index state (source content, chunk state, link state) and the classification search applies to it (`searchable`, `unindexed`, `corrupt`), with counts; it reports and exits 0, while `search` refuses to run on corrupt state; `stats --course-id <id>` reports chunk statistics: estimated-token distribution, chunks at the target and hard maximum, spans per chunk, split spans, the duplication factor caused by overlap, and visible-text coverage. Both are offline and read-only. Spans that cannot be chunked safely, or a chunkable version whose chunks do not cover all its visible text, are reported as integrity failures (exit code 6), never as ordinary statistics.
- `python -m app.retrieval.smoke_test` runs the whole path live (ingest, index, search) on invented courses with namespaced identities, and checks the guarantees above; reruns reuse every record and add only the new query calls.
- `python -m app.retrieval.measure throughput | chunker` measures embedding throughput (synthetic inputs, a hard request budget, metric definitions in the module) and chunker speed. Results from one development environment are in [retrieval measurements](../experiments/retrieval-measurements.md).
