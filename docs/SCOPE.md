# CourseFlow v0.1.0 — Frozen Scope

**Status:** Frozen on 5 October 2026 for the Nebius × NVIDIA Global AI Hackathon build (5–29 October 2026).

CourseFlow is an academic navigation system. It converts fragmented LMS content into a trustworthy, evidence-backed academic execution graph: what to learn, what to do, where the source is, when it matters, and why the system believes it. It is not an AI tutor, essay generator or generic chatbot.

## Change rule

This file is the scope contract for v0.1.0.

- **P0** must work in the submitted application.
- **P1** is added only when P0 is healthy.
- **P2** is deliberately deferred. A P2 item may not be implemented unless this file is deliberately edited first, with the reason recorded in the commit message and, if architectural, in an ADR under [`docs/decisions/`](decisions/).

---

## P0 — must work in the submitted application

- Synthetic demo institution with at least two realistic courses.
- Import pipeline for PDF, DOCX, PPTX and simple HTML/Moodle-like pages.
- Versioned source model with hashes and exact page/slide/section provenance.
- Typed extraction of assessments, deadlines, requirements, obligations and source evidence.
- Requirement-to-learning-material mapping using retrieval plus model verification.
- Evidence viewer for every consequential AI claim.
- Dependency reconstruction for academic prerequisites.
- Conflict detection for contradictory consequential facts.
- Cross-course study plan with deterministic priority features and a user-readable `why now?` explanation.
- Source v2/change simulation that invalidates and recomputes affected downstream state.
- Real runtime calls to NVIDIA Nemotron through Nebius Token Factory.
- Stateful multi-step orchestration, not one opaque LLM call.
- Reproducible evaluation/golden set with real measured metrics.
- Public working demo, public repository, open-source license, README and <3 minute demo video.

## P1 — add when P0 is healthy

- Explicit LangGraph checkpoints/retries and resumability.
- Hybrid retrieval (vector + PostgreSQL lexical/full-text search) and retrieval evaluation.
- Model routing between fast/cheap and strong reasoning routes.
- Async/background execution using Celery + Redis or a clean equivalent.
- Langfuse or OpenTelemetry tracing, plus token/latency/cost accounting.
- Prompt-injection, malformed-document and tool-safety red-team tests.
- CI evaluation regression job.

## P2 — deliberately defer

- Real university SSO/MFA and production OAuth.
- Multiple real universities or multiple LMS platforms.
- Payments/subscriptions.
- Native mobile app.
- Kubernetes/microservices/service mesh.
- Fine-tuning/LoRA without eval evidence that prompting/retrieval is insufficient.
- Neo4j/GraphRAG migration.
- Generic chatbot, essay generation, flashcards, quiz generator or automatic submission.

---

## Product non-goals (permanent, not just deferred)

These protect the product identity regardless of release:

- No essay or coursework writing, and no "generate my answer" function.
- No generic AI chatbot as the centre of the product.
- No lecture summarisation as a core feature.
- No flashcard factory, generic quiz generator or gamification.
- No assumption that opening a document equals learning it.
- No autonomous submission or editing of university systems (read-only by default).
- No silent resolution of conflicting deadlines or requirements.
