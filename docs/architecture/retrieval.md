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
