# Retrieval

Retrieval turns ingested versions into searchable **chunks** with vectors, while every chunk still resolves to exact source spans. The stored fields and their rules are in the [data contract](data-contract.md#retrieval-layer).

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
