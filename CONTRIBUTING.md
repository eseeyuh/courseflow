# Contributing to CourseFlow

CourseFlow is in early, fast-moving development for a hackathon. Issues and suggestions are welcome. Larger pull requests should start with an issue so scope can be agreed first.

## Ground rules

1. **Respect the scope.** [docs/SCOPE.md](docs/SCOPE.md) is the v0.1.0 contract. P2 items and permanent non-goals (chatbot, essay or answer generation, summarisation, quizzes) are not accepted.
2. **Evidence before assertion.** Code that produces consequential claims (deadlines, obligations, dependencies, mappings, conflicts) must attach a valid `SourceSpan` through an `EvidenceLink`, or mark the claim `needs_review`. See [docs/architecture/data-contract.md](docs/architecture/data-contract.md).
3. **Canonical names only.** Use the domain vocabulary in the data contract (for example `SourceSpan`, `DependencyEdge`, `EvidenceLink`, `WorkflowRun`).
4. **Typed boundaries.** Pydantic/domain schemas at module boundaries; validate all model output.
5. **Course content is untrusted input.** Never let document text act as instructions or reach tools unvalidated.
6. **No secrets, no real course data.** Never commit `.env` files, keys, real university material or personal data. Demo data must be synthetic.
7. **Architecture changes need an ADR** in [docs/decisions/](docs/decisions/), numbered sequentially.

## Workflow

- Branch from `main`. Keep `main` deployable.
- Use [Conventional Commits](https://www.conventionalcommits.org/), e.g. `feat(ingestion): add PDF parser with page provenance`.
- Before opening a pull request: lint, type-check and run the relevant tests (tooling is added as the codebase grows).
- Text files use LF line endings (enforced by `.gitattributes`).

## Licence

By contributing, you agree that your contributions are licensed under the [Mozilla Public License 2.0](LICENSE).
