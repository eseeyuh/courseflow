# System Context — CourseFlow v0.1.0

This document describes CourseFlow's boundaries, its external actors and systems, and its main internal parts. The architectural style is explained in [ADR-001](../decisions/ADR-001-modular-monolith.md). The data layer is explained in [ADR-002](../decisions/ADR-002-postgres-pgvector.md).

## 1. Context diagram

```text
                    ┌──────────────┐
                    │   Student    │
                    └──────┬───────┘
                           │ HTTPS (browser)
                           v
┌────────────────────────────────────────────────────────────────────┐
│ CourseFlow                                                         │
│                                                                    │
│  ┌──────────────────────────┐                                      │
│  │ CourseFlow Web           │  Next.js + TypeScript                │
│  │ (presentation only)      │                                      │
│  └────────────┬─────────────┘                                      │
│               │ versioned JSON HTTP API                            │
│               v                                                    │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │ CourseFlow API  (FastAPI, modular monolith)                  │  │
│  │                                                              │  │
│  │  connectors ─> ingestion ─> retrieval ─> AI workflow         │  │
│  │  (LMSConnector) (parse,     (embeddings,  (extract, verify,  │  │
│  │                 spans,      vector +      map, dependencies, │  │
│  │                 hashes)     lexical)      conflicts, plan,   │  │
│  │                                           evidence gate)     │  │
│  │                         planning · changes · observability   │  │
│  └───────┬──────────────────────────────────────────┬───────────┘  │
│          │ SQL (SQLAlchemy)                         │ AIProvider   │
│          v                                          │ interface    │
│  ┌──────────────────────────┐                       │              │
│  │ PostgreSQL + pgvector    │                       │              │
│  │ domain rows, edges,      │                       │              │
│  │ spans, embeddings, runs  │                       │              │
│  └──────────────────────────┘                       │              │
└─────────────────────────────────────────────────────┼──────────────┘
        ^                                             │ HTTPS (OpenAI-compatible)
        │ LMSConnector (read-only)                    v
┌───────┴──────────────────────┐        ┌──────────────────────────────┐
│ LMS / course sources         │        │ Nebius Token Factory         │
│ v0.1.0: synthetic demo       │        │ NVIDIA Nemotron models       │
│ connector + uploads          │        │ (+ embedding route if chosen)│
│ later: Moodle, Canvas, ...   │        └──────────────────────────────┘
└──────────────────────────────┘
```

## 2. Actors and external systems

| Actor / system | Relationship to CourseFlow | Trust level |
|---|---|---|
| **Student** | Uses the web UI to import courses, inspect evidence, track study state and follow the study plan. | Authenticated user. The backend owns all authorisation decisions. |
| **LMS / course sources** | Provide course pages, assessments, announcements and documents. v0.1.0 uses a synthetic demo institution through a demo/upload connector. A real Moodle connector is planned after v0.1.0. | **Untrusted content.** Text inside documents is data, never instructions. |
| **Nebius Token Factory** | Hosts NVIDIA Nemotron models through an OpenAI-compatible API. Used at runtime for extraction, verification, dependency and conflict reasoning, and planning. | External dependency. Responses are validated against typed schemas before use. |

## 3. Internal parts

| Part | Responsibility | Key rule |
|---|---|---|
| **CourseFlow Web** | Presentation: workspace, course map, assessments, evidence viewer, conflicts, changes, study plan. | Shows evidence for every consequential claim. Never decides domain truth or access. |
| **API** | Versioned HTTP endpoints, request validation, job creation. | Typed request/response schemas. |
| **connectors** | `LMSConnector` implementations that return normalised CourseFlow source objects. | The intelligence layer never sees vendor-specific payloads. |
| **ingestion** | Parse PDF, DOCX, PPTX and HTML into `ResourceVersion` + `SourceSpan` with page, slide or section provenance and content hashes. | Provenance is preserved from the first step. |
| **retrieval** | Chunk embeddings (pgvector), lexical search for hybrid retrieval (P1), candidate generation for requirement → material mapping. | Retrieval quality is measured separately from LLM verification. |
| **AI workflow** | Stateful, multi-step pipeline: extract → validate → retrieve → verify mappings → infer dependencies → detect conflicts → plan → evidence gate → persist. | Structured outputs only; bounded retries; allowlisted typed tools; no shell, no raw SQL, no open internet. |
| **planning** | Cross-course study plan with deterministic priority features and a "why now?" explanation. | Every task traces back to evidence. |
| **changes** | Detect new `ResourceVersion`s by hash, emit `ChangeEvent`s, invalidate affected claims, recompute affected downstream state only. | Recompute narrowly; widen only when lineage is unclear. |
| **observability** | `WorkflowRun` lineage, per-step status, model, prompt version, tokens, latency, retries, failures. | Every run can answer "why did this output happen?" |
| **PostgreSQL + pgvector** | Single system of record: domain objects, edges, spans, embeddings, runs. | Schema changes through migrations only. |

## 4. Model roles

Model IDs are configuration, never hard-coded, and must be verified against the live Token Factory catalog:

| Role | Intended use |
|---|---|
| `MODEL_FAST` | High-volume, routine structured extraction and classification. |
| `MODEL_STRONG` | Harder reasoning: ambiguous mappings, dependency inference, conflict analysis, complex planning. |
| `EMBEDDING_MODEL` | Chunk and query embeddings for retrieval. It may be a local open embedding model or a Token Factory route, chosen after testing. |

## 5. Main data flows

1. **Import:** Student selects a demo course → connector returns normalised sources → ingestion creates `Resource`/`ResourceVersion`/`SourceSpan` rows and embeddings.
2. **Analyse:** AI workflow extracts typed objects, maps requirements to material, infers dependencies, detects conflicts, and persists only evidence-backed claims (others are marked `needs_review` or rejected). Each run is recorded as a `WorkflowRun`.
3. **Plan:** The planner builds `StudyTask`s from deadlines, dependencies and `StudyState`, each with a "why now?" reason.
4. **Change:** A new source version arrives → hash differs → `ChangeEvent` → affected claims superseded → affected nodes and tasks recomputed → change shown in the UI.

## 6. Boundaries and security notes

- **Read-only towards the LMS.** CourseFlow never submits, edits or grades.
- **Untrusted content** is delimited and labelled in prompts. Prompt-injection text in a document must not change control flow or tool use.
- **Secrets** live only in environment variables (see [`.env.example`](../../.env.example)) and are never put into model prompts.
- **Demo data** is fully synthetic. No real university or student data is in the repository.
