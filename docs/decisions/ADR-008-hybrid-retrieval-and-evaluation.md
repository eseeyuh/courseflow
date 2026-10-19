# ADR-008 — Lexical and hybrid retrieval, chosen by a frozen benchmark

- **Status:** Accepted
- **Date:** 2026-10-19

## Context

[ADR-007](ADR-007-chunking-embeddings-and-vector-search.md) gave CourseFlow vector search over provenance-preserving chunks, restricted to a trusted corpus: one course, current versions only, and only versions whose index state is verified complete. Vector similarity alone has a known weakness for course material. Identifiers and exact terms, such as a form code (`EC7`), an isolation level (`REPEATABLE READ`) or a command option (`EXPLAIN (ANALYZE, BUFFERS)`), can rank below passages that are only semantically close. Lexical matching handles these well but misses paraphrased questions.

Two decisions were needed:

- how to add lexical and hybrid ranking without weakening the evidence chain or the trusted-corpus boundary;
- which mode becomes the runtime default, decided by measurement rather than preference.

## Decision

### 1. Lexical retrieval in PostgreSQL (`pg-fts-english/v2`)

- `chunks.text_tsv` is a stored **generated** column, `to_tsvector('english'::regconfig, text)`, with a GIN index. It is derived from the chunk text and never written by the application. Adding it backfilled existing rows automatically.
- The configuration is always the explicit `english` one, never `default_text_search_config`, so a server setting cannot change results.
- A query is normalised by PostgreSQL with the same configuration. Its distinct lexemes are combined with OR into a `tsquery` that is sent as a bound parameter, so query text can never change SQL structure. A query with no lexemes (only stop words or punctuation) returns nothing.
- Candidates match with `@@` and rank by `ts_rank_cd` without length normalisation. The score is retriever-specific diagnostic data, not a confidence.
- **This is English lexical retrieval.** English stemming and stop words apply to every chunk. Non-English text is indexed with English rules, so its lexical recall is poor.

### 2. Hybrid retrieval by reciprocal rank fusion (`rrf-k60-depth50/v2`)

- Hybrid runs the vector and lexical rankers over the same verified corpus. Each contributes at most 50 candidates.
- A chunk's fused score is the exact rational sum, over the rankings that returned it, of `1 / (60 + rank)`. Only ranks are fused: cosine distance and `ts_rank_cd` are not comparable, so they are carried through only as diagnostics.
- The fused order is total. After the fused score it uses the best single rank, then the vector rank, then the lexical rank.
- Fusion is pure: no database, no I/O, no clock. It refuses malformed rankings instead of repairing them.
- Hybrid needs the query embedding. If that call fails, the search fails; it never silently falls back to lexical-only results.

### 3. Content-stable tie-breaking

Every ranking (vector, lexical and hybrid) breaks remaining ties by the chunk's **natural identity**: `resources.source_uri` (bytewise, `COLLATE "C"`), then `resource_versions.version_number`, then `chunks.ordinal`.

- Existing unique constraints make this order total within one course's current corpus: `(course_id, source_uri)`, `(resource_id, version_number)`, and the ordinal within a version and chunker. No schema change was needed.
- No generated row ID takes part. Two databases seeded with the same content therefore rank identically.
- This replaced an earlier tie-break on random version UUIDs, which made the order of tied lexical and hybrid hits change between fresh databases. The lexical and fusion contract versions were bumped from v1 to v2. Vector ordering changes only for exactly equal distances.

### 4. The trusted corpus, evidence and the vector implementation are unchanged

- **SourceSpan remains the evidence truth.** In every mode a hit is a chunk, and its evidence is the exact SourceSpan sub-ranges it resolves to, re-checked on every search. A partly trustworthy result list is never returned.
- **Lexical and hybrid keep ADR-007's trusted-corpus boundary.** All modes rank only the course's current versions that are classified searchable for the active chunker and embedding space. Classification, ranking and evidence assembly share one read-only snapshot, so lexical search can never be used to bypass the index-state checks. Lexical search needs the space's identity but no provider client, credentials or network.
- **Exact scan remains the vector implementation.** Vector search is still an exact cosine scan with no approximate index. At v0.1 scale this is fast enough, and it keeps the vector ranking exactly reproducible, which the benchmark depends on.

### 5. A frozen benchmark decides the default

**Benchmark: retrieval/v1** (`evals/datasets/retrieval/v1`, SHA-256 `07c11efaea7efd516f6d6ec0bdb27229008bd0f1972890fb82d4827e0ba45747`).

- **Corpus:** fully synthetic. Two fictional courses, 13 resources (14 versions, including one superseded coursework brief), 39 current-version chunks and 41 in all.
- **Cases:** 14 in total. 12 positive cases (4 keyword, 5 paraphrase, 3 mixed) and 2 invariant-only probes (stale version, cross-course).
- **Distractors:** labelled same-course semantic, irrelevant, stale-version and cross-course ones.
- **Relevance:** `full-target-containment/v1`. A hit counts only if the chunk wholly contains the labelled quote's range; partial overlap gets no credit.
- **Freeze:** the dataset was committed before any retrieval score existed, and it is never edited. Corrections go into a new version.
- **Critical case:** `ds101-mixed-cw1-deadline` was chosen before scoring. The selected default must satisfy it within the top 3.

**Replay snapshot** (`evals/snapshots/retrieval/v1`, SHA-256 `56509e7d364dbd403a6932ea98272a5334cc0f0a2bdb0d468645632429371941`).

- It holds the `Qwen/Qwen3-Embedding-8B` vectors (4096 dimensions) for all 55 inputs: 41 `document/v1` and 14 `query/v1`.
- They were recorded once, in 2 provider requests that reported 22,581 tokens. The snapshot stores no text.
- Evaluation runs replay it with zero live provider calls. They use a guarded, freshly recreated database and the production ingestion, indexing and search code. Each superseded version is indexed while still current and only then superseded, as in production.

**Selection rule `default-selection/v1`**, fixed before any score. A mode *passes* when it has no hard-invariant failure and satisfies the critical case within the top 3.

- A. Start with vector. Lexical replaces it if lexical passes and is at least as good on both Recall@5 and MRR@10. An exact tie on both prefers lexical.
- B. Hybrid replaces the resulting single retriever if hybrid passes, is at least as good on both metrics, and is strictly better on at least one.

## Results

These metrics come from the 12 positive cases only. The 2 invariant-only cases are excluded from every denominator. Every mode had **0 hard-invariant failures**: no hit from a stale version or from another course.

| Mode | Recall@1 | Recall@3 | Recall@5 | MRR@10 | Critical-case rank |
|---|---|---|---|---|---|
| vector | 9/12 | 11/12 | 12/12 | 121/144 (≈ 0.840) | 3 |
| lexical | 9/12 | 11/12 | 12/12 | 17/20 (= 0.850) | 1 |
| hybrid | 9/12 | 12/12 | 12/12 | 31/36 (≈ 0.861) | 2 |

- **Step A:** lexical passes, ties vector on Recall@5 (12/12), and has a higher MRR@10, so it replaces vector.
- **Step B:** hybrid passes, ties lexical on Recall@5, and has a strictly higher MRR@10, so it replaces lexical.
- **Selected default: hybrid.**

Two runs on independently seeded fresh databases produced identical ordered hit lists, ranks, metrics and selection. The full per-case report is [evals/reports/retrieval-v1.md](../../evals/reports/retrieval-v1.md).

The benchmark is intentionally small. The differences are one case at Recall@3 and a few hundredths of MRR, so **no statistical significance is claimed**. What the result supports is narrower:

- on this frozen benchmark, hybrid is at least as good as either single retriever on every reported metric;
- it is the only mode that places every positive case in the top 3;
- it was selected by a rule declared before any score, not by post-hoc tuning. RRF k, depth, queries and labels were not changed after scoring.

## Decision on the runtime default

`DEFAULT_SEARCH_MODE = hybrid` is the single place the default is set. Every request that names no mode uses it, including the CLI. Vector and lexical remain explicitly selectable. Two frozen regressions run on the replay snapshot through the default mode:

- the critical case must stay in the top 3;
- the stale-version and cross-course probes must return no forbidden hit.

## Not done

- **No LLM reranker** is part of P0. The benchmark shows no case outside the top 5 in any mode, so it gives no evidence that a reranker is needed. A reranker would add a paid, non-deterministic step to every search, and it should be added only if a future benchmark shows a gap it closes.
- **No approximate vector index**, for the reasons in §4.
- **No weighted score fusion.** Raw scores are not comparable across rankers, and any weights would have to be tuned on the same small benchmark.

## Consequences

- Every default search makes one embedding call, as vector search did, plus a full-text query that runs inside the same read-only snapshot.
- Ranking contracts are versioned (`pg-fts-english/v2`, `rrf-k60-depth50/v2`). Changing a rule, a parameter or the tie-break is a new version, and it needs a new evaluation before it can change the default.
- The benchmark and snapshot are fixed. A different chunker, embedding model or space version needs a new recording, and a changed dataset is a new version (retrieval/v2). Results for v1 stay valid for v1.
- The default is justified only for the benchmark's scope: English, HTML-sourced, synthetic course material.

## Limitations and future multilingual considerations

- Lexical retrieval is English-only. Courses in other languages would get weak lexical matching, and hybrid would then lean on vector similarity.
- **Language-aware lexical retrieval** would need the language recorded on each version, a matching text-search configuration (or `simple` for unknown languages), and a new lexical version. It would also need a multilingual benchmark before any default changed. The embedding model is multilingual, but nothing here has been measured on non-English text.
- The benchmark has one HTML parser, two courses and 12 positive cases. It does not cover PDF and slide extraction, larger corpora or adversarial queries.
- Weak cases remain. `ds101-para-update-anomaly` (a paraphrase) ranks 3rd to 5th across modes. Same-course semantic distractors outrank the target in several cases, including the critical case in vector and hybrid mode.

## Revisit when

- the corpus or courses grow beyond what an exact scan handles comfortably (then consider an ANN index, with its effect on reproducibility measured);
- non-English course material is in scope;
- a larger or harder benchmark (retrieval/v2) shows cases outside the top 5, which would make a reranker worth evaluating.
