# CourseFlow

**Moodle stores the information. CourseFlow reconstructs the academic path hidden inside it.**

CourseFlow is an academic navigation system. It turns fragmented LMS content (course pages, assignment briefs, handbooks, announcements, slides) into an **evidence-backed academic execution graph**. It tells a student what to learn, where exactly to learn it, what to do, when it is due, and **how the system knows**.

> **Status:** early development (v0.1.0 in progress) for the Nebius × NVIDIA Global AI Hackathon, *Best Apps & Agents* track. The repository currently holds the engineering foundation: scope, architecture and decisions. Application code, the demo and evaluation results will be added as they are built. No feature below should be read as finished until it appears in a release.

---

## Why CourseFlow is not another AI tutor

CourseFlow points students to the university's **original** material rather than replacing learning with generated answers.

- **Evidence before assertion.** Every consequential claim (deadline, obligation, dependency, requirement → material mapping) links to the exact source page, slide or section.
- **AI discovers; the source decides.** When sources disagree, CourseFlow surfaces a conflict instead of guessing.
- **Read-only.** It never submits coursework, edits LMS content or changes grades.
- **No fake mastery.** Study progress is explicit (`NOT_STARTED`, `STUDYING`, `STUDIED`), not "opened the file".
- **Not a chatbot.** No essay writing, answer generation, lecture summarisation or quiz factories.

See [docs/SCOPE.md](docs/SCOPE.md) for the frozen v0.1.0 scope and permanent non-goals.

## Planned architecture

A modular monolith: Next.js frontend, FastAPI backend, PostgreSQL + pgvector, and a stateful multi-step AI workflow using **NVIDIA Nemotron via Nebius Token Factory**.

- [System context](docs/architecture/system-context.md): actors, boundaries, internal parts, data flows
- [Data contract](docs/architecture/data-contract.md): canonical domain objects and the provenance chain

## Architecture decisions

| ADR | Decision |
|---|---|
| [ADR-001](docs/decisions/ADR-001-modular-monolith.md) | Modular monolith with separate frontend and explicit connector/provider interfaces |
| [ADR-002](docs/decisions/ADR-002-postgres-pgvector.md) | Relational graph model in PostgreSQL + pgvector (no separate graph/vector DB) |
| [ADR-003](docs/decisions/ADR-003-mpl-2.0-licence.md) | Mozilla Public License 2.0 |

## Evaluation

CourseFlow will be evaluated against a reproducible synthetic golden dataset (deadline exact match, obligation F1, dependency-edge F1, evidence-location accuracy, retrieval Recall@K, conflict detection, workflow completion, latency and cost).

**No results yet.** Numbers will appear here only from saved benchmark runs.

## Quickstart

_Not available yet._ Docker-based setup instructions will be added with the first runnable backend. Configuration placeholders are in [.env.example](.env.example).

## Demo data

All demo content will be **fully synthetic** (a fictional "Northbridge University"). The repository contains no real university material and no student data.

## Contributing and security

- [CONTRIBUTING.md](CONTRIBUTING.md)
- [SECURITY.md](SECURITY.md)

## License

[Mozilla Public License 2.0](LICENSE). See [ADR-003](docs/decisions/ADR-003-mpl-2.0-licence.md) for the reasoning.
