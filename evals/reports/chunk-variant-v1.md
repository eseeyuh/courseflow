# Chunk-size variant experiment

Experiment `chunk-variant/v1` on retrieval benchmark v1 (dataset SHA-256 `07c11efaea7efd516f6d6ec0bdb27229008bd0f1972890fb82d4827e0ba45747`). The variant was frozen before any score. Production `span-pack/v1` is unchanged.

| | span-pack/v1 (production) | span-pack-eval-small/v1 |
|---|---|---|
| target_estimated_tokens | 800 | 600 |
| split_above_estimated_tokens | 880 | 680 |
| hard_max_estimated_tokens | 1000 | 800 |
| overlap_estimated_tokens | 120 | 100 |
| min_estimated_tokens | 200 | 160 |
| Chunks (current / all versions) | 39 / 41 | 48 / 51 |
| Targets wholly inside some chunk | 12/12 | 12/12 |
| Lexical Recall@1 / @3 / @5 | 3/4 / 11/12 / 1/1 | 7/12 / 3/4 / 5/6 |
| Lexical MRR@10 | 17/20 | 101/144 |
| Lexical critical-case rank | 1 | 1 |
| Invariant failures | 0 | 0 |
| Document inputs missing from the Qwen snapshot | 0 of 41 | 48 of 51 |

**Hybrid (the selected method) is not compared.** The variant's chunk boundaries need document embeddings that the committed snapshot does not hold (48 inputs, about 26052 estimated tokens, about $0.000261 at the verified price). They would need a separate, approved evaluation snapshot. No live call was made and no vectors were invented.

Lexical retrieval never reads vectors. Chunks were linked to placeholder vectors in a separate placeholder space only so that indexing classifies them searchable. As a control, the production config's lexical metrics through this path **match** the committed evaluation.

One small benchmark: no statistical significance is claimed, and production defaults are not changed by this experiment.
