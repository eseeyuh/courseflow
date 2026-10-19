# Retrieval evaluation

- Dataset: `courseflow-retrieval` v1, SHA-256 `07c11efaea7efd516f6d6ec0bdb27229008bd0f1972890fb82d4827e0ba45747`
- Replay snapshot SHA-256: `56509e7d364dbd403a6932ea98272a5334cc0f0a2bdb0d468645632429371941`
- Embedding space: `Qwen/Qwen3-Embedding-8B`, 4096 dimensions, space `v1`; chunker `span-pack/v1`
- Relevance: `full-target-containment/v1`; top 10 per case
- Cases: 14 (12 positive, 2 invariant-only, excluded from denominators)
- Live provider calls: 0
- Runs: 2; identical metrics: True; identical hit lists: True

## Metrics

| Mode | Recall@1 | Recall@3 | Recall@5 | MRR@10 | Invariant failures |
|---|---|---|---|---|---|
| vector | 9/12 | 11/12 | 12/12 | 0.840278 (121/144) | 0 |
| lexical | 9/12 | 11/12 | 12/12 | 0.850000 (17/20) | 0 |
| hybrid | 9/12 | 12/12 | 12/12 | 0.861111 (31/36) | 0 |

## Default selection

Rule `default-selection/v1` (fixed before any score). A mode passes with no invariant failure and the critical case in the top 3. A: lexical replaces vector iff it passes with Recall@5 and MRR@10 both >= vector's (a tie on both prefers lexical). B: hybrid replaces that single retriever iff it passes, is >= on both and strictly better on at least one.

| Mode | Passes | Critical rank | Recall@5 | MRR@10 |
|---|---|---|---|---|
| vector | True | 3 | 1/1 | 121/144 |
| lexical | True | 1 | 1/1 | 17/20 |
| hybrid | True | 2 | 1/1 | 31/36 |

**Selected: hybrid.** A: lexical replaces vector; B: hybrid replaces lexical.

## Per case

First relevant rank per mode (`-`: not in the top 10). Distractors are listed when one ranks above the first relevant hit, or anywhere for invariant cases.

| Case | Category | vector | lexical | hybrid | Distractors above target |
|---|---|---|---|---|---|
| ds101-kw-ec7-extension-form | keyword | 1 | 2 | 1 |  |
| ds101-kw-repeatable-read-phantoms | keyword | 1 | 1 | 1 |  |
| ds101-kw-explain-analyze-buffers | keyword | 1 | 1 | 1 |  |
| ds101-kw-bcnf-definition | keyword | 1 | 1 | 1 |  |
| ds101-para-update-anomaly | paraphrase | 4 | 5 | 3 | vector: same_course_semantic @1 |
| ds101-para-late-penalty | paraphrase | 1 | 1 | 1 |  |
| ds101-para-deadlock | paraphrase | 1 | 1 | 1 |  |
| ds101-para-composite-index-order | paraphrase | 1 | 1 | 1 |  |
| ds101-para-members-without-loans | paraphrase | 1 | 1 | 1 |  |
| ds101-mixed-cw1-deadline (critical) | mixed | 3 | 1 | 2 | vector: same_course_semantic @1; hybrid: same_course_semantic @1 |
| ds101-mixed-cw1-3nf-justification | mixed | 2 | 2 | 2 | vector: same_course_semantic @1; lexical: same_course_semantic @1; hybrid: same_course_semantic @1 |
| ds101-mixed-genai-declaration | mixed | 1 | 1 | 1 |  |
| ds101-inv-stale-group-submission | stale_version_invariant | n/a | n/a | n/a |  |
| ds101-inv-cross-course-loudness | cross_course_invariant | n/a | n/a | n/a |  |

Generated 2026-10-19T14:59:08Z by `retrieval-eval/v1`.
