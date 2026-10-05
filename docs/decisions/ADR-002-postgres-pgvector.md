# ADR-002 — Relational graph model in PostgreSQL + pgvector (no separate graph or vector database)

- **Status:** Accepted
- **Date:** 2026-10-05

## Context

CourseFlow's core asset is a connected academic model:

- courses → resources → resource versions → source spans;
- assessments → requirements → mapped teaching material;
- obligations and assessments linked by ordering constraints ("A must happen before B");
- every consequential claim linked to its evidence;
- source changes linked to the objects they affect.

These relationships look like a graph, which raises the question of a graph database (Neo4j) or GraphRAG stack. Requirement-to-material mapping also needs embedding-based retrieval, which raises the question of a dedicated vector database.

The v0.1.0 data volume is small: a few synthetic courses with tens of documents, thousands of spans/chunks at most, and tens to hundreds of edges per course. The queries we need are:

- short, bounded traversals, such as "what does this obligation block?" or "which study tasks depend on claims from this changed span?";
- top-k similarity search filtered by course and resource type;
- lexical/full-text search for hybrid retrieval (P1);
- transactional writes that keep claims, evidence and edges consistent.

## Decision

Use **PostgreSQL as the single system of record**, with:

- **Domain objects as tables** (see [`docs/architecture/data-contract.md`](../architecture/data-contract.md)).
- **Graph relationships as explicit edge rows**. For example, `DependencyEdge(from_ref, relation, to_ref, evidence)` and `MaterialMapping(requirement_id, resource/span, score, verification_status)` are typed, constrained and joinable like any other row. Traversal uses SQL joins or recursive CTEs, or loads a small subgraph into memory (for example with NetworkX) when that's clearer.
- **pgvector** for chunk embeddings, stored next to their provenance (`ResourceVersion`, `SourceSpan`) so similarity search can be filtered by course, resource type and version in the same query.
- **PostgreSQL full-text search** (`tsvector`) as the lexical half of hybrid retrieval when that P1 work is scheduled.
- **SQLAlchemy 2.x + Alembic** for schema changes through migrations only.

A graph-shaped domain doesn't need a graph database. "Graph" describes the data model; the storage engine is a separate choice.

## Alternatives considered

| Alternative | Why not for v0.1.0 |
|---|---|
| **Neo4j / graph database** | Wins at deep, variable-length, pattern-heavy traversals over large graphs. Our traversals are shallow and bounded, and the graph is small. It would add a second datastore, cross-store consistency between claims, evidence and edges, another query language, and deployment cost, with no measured benefit. |
| **Dedicated vector DB** (Pinecone, Qdrant, Weaviate, Chroma) | Strong at very large ANN workloads. At our scale pgvector is sufficient, and keeping embeddings next to provenance and metadata avoids syncing IDs between stores. Filtered retrieval ("this course, lectures only, current version") stays a single SQL query. |
| **Document store / JSON blobs of model output** | Makes claims un-queryable, unjoinable and un-evaluable. It conflicts with the evidence contract and with incremental recomputation. |

## Consequences

**Positive**

- One transactional store: a claim, its evidence link and its edges commit or roll back together.
- Provenance, metadata filters and vectors are queryable together, which supports retrieval evaluation (Recall@K, MRR) and evidence checks.
- Fewer moving parts in Docker, CI and hosting.

**Negative / risks**

- Recursive traversals in SQL are more verbose than Cypher. Mitigation: keep traversals small, wrap them in tested repository functions, and load subgraphs into memory when clearer.
- pgvector index tuning (HNSW/IVFFlat parameters) will matter if the corpus grows by orders of magnitude.

## Revisit when

- Measured traversal queries (for example change-impact propagation across many courses or users) become a latency bottleneck that indexing and query design can't fix, **or**
- Retrieval corpus size or QPS makes pgvector a measured bottleneck, **or**
- A product feature needs graph algorithms (centrality, community detection) at a scale where in-memory processing is impractical.
