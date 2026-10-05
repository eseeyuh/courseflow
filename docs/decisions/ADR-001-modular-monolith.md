# ADR-001 — Modular monolith with separate frontend and explicit connector/provider interfaces

- **Status:** Accepted
- **Date:** 2026-10-05

## Context

CourseFlow v0.1.0 has to do three things at once:

1. Ship a reliable end-to-end hackathon demo within 25 days.
2. Act as a credible AI-engineering portfolio project.
3. Survive as the foundation of a future product, so the code isn't thrown away afterwards.

The backend runs a tightly coupled, multi-stage pipeline: parse → extract → retrieve → verify → infer dependencies → detect conflicts → plan → persist. Each stage reads the previous stage's output and writes to the same domain model (courses, resources, source spans, assessments, obligations, edges, study tasks). One developer builds and runs it.

Two integration points are known to change after the hackathon:

- **The LMS.** The demo uses a synthetic connector. Real Moodle comes later, possibly followed by Canvas, Blackboard and Brightspace.
- **The LLM provider and models.** Nebius Token Factory with NVIDIA Nemotron is used now, but model IDs, routes and possibly providers will change.

## Decision

Build v0.1.0 as a **modular monolith**:

- **One backend deployable** (FastAPI, Python), split internally into modules with clear ownership: `api`, `domain`, `db`, `connectors`, `ingestion`, `retrieval`, `ai` (provider, graph, tools, prompts, schemas), `planning`, `changes`, `observability`.
- **A separate frontend deployable** (Next.js, TypeScript), talking to the backend only through a versioned HTTP API. The frontend never decides authorisation or domain truth.
- **Explicit interfaces at the volatile boundaries:**
  - `LMSConnector`: returns normalised CourseFlow source objects. The intelligence layer never sees vendor-specific payloads such as Moodle JSON.
  - `AIProvider`: wraps the OpenAI-compatible Token Factory API, with model roles (`MODEL_FAST`, `MODEL_STRONG`, `EMBEDDING_MODEL`) set through configuration instead of hard-coded IDs.
- **Modules communicate through typed Pydantic/domain schemas**, never through raw LLM text or shared mutable globals.

## Alternatives considered

| Alternative | Why not for v0.1.0 |
|---|---|
| **Microservices** (separate ingestion, AI, planning services) | Adds network failure modes, cross-service data consistency problems, distributed tracing, and extra deployment and versioning work. Benefits such as independent team ownership and independent scaling don't apply to a one-developer, single-domain system. The pipeline stages share one transactional domain model, so splitting them would mean coordinating writes across the network. |
| **Unstructured monolith** (no internal boundaries) | Fastest at first, but coupling Moodle payloads or a specific model API into business logic would make the post-hackathon connector and provider changes a rewrite. |
| **Single full-stack framework** (frontend and backend in one Next.js app) | The AI and data stack (PyMuPDF, pgvector tooling, LangGraph, evaluation) lives in Python. A Python backend with a separate TypeScript UI keeps each side in its natural ecosystem. |

## Consequences

**Positive**

- One process to run, test, debug and deploy, so local development and CI are simple.
- Each pipeline stage can run in the same database transaction scope where needed.
- Module boundaries and typed schemas keep the code ready for extraction later. A module with a stable interface can become a separate service if a measured need appears.
- The connector and provider interfaces are the main reason this code can become the real product.

**Negative / risks**

- Boundaries are enforced by convention and code review, not by the network. Discipline is needed to stop modules reaching into each other's internals.
- Everything scales together. Heavy document processing could compete with API latency until async workers arrive (planned as P1).

## Revisit when

- Independent scaling of a component (for example ingestion workers) is measured to be necessary and can't be met with a background job runner, **or**
- Multiple teams need to own and deploy parts of the system independently, **or**
- A real institutional integration needs an isolated security or deployment boundary.
