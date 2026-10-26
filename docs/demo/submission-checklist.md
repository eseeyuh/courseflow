# Submission checklist

## Repository: complete

- [x] Public repository on GitHub, licensed [MPL-2.0](../../LICENSE)
- [x] Judge-ready [README](../../README.md) with problem, features, architecture diagram, trust design and run instructions
- [x] Architecture: Mermaid diagram in the README, plus [docs/architecture/](../architecture/) and [ADRs](../decisions/)
- [x] Screenshots from the running demo ([docs/assets/screenshots/](../assets/screenshots/))
- [x] Three-minute [demo script](demo-script.md) and [recording checklist](recording-checklist.md)
- [x] [Judge summary](judge-summary.md) and paste-ready [submission copy](submission.md)
- [x] [Technical facts](technical-facts.md) and [NVIDIA / Nebius usage](nvidia-nebius.md)
- [x] One-command Docker demo stack (`infra/demo/compose.yml`), verified with the golden-path release smoke
- [x] Tests: backend pytest, frontend lint, typecheck and build, and the Playwright golden path, all passing
- [x] Secret scan: gitleaks, no leaks
- [x] Release v0.1.0: annotated tag and GitHub release

## External: manual, not done yet

- [ ] Record the demo video (under 3 minutes), following the recording checklist, and upload it
- [ ] If the event requires a public URL: get hosting credentials and run the Docker stack on a server behind HTTPS (README, *Hosting it*)
- [ ] Paste the submission copy into the hackathon portal
- [ ] Add the final video URL to the submission
- [ ] Add the public deployment URL, if one exists
- [ ] Submit before the deadline (30 Oct 2026, 10:00 PDT)
