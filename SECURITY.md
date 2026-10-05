# Security Policy

## Supported versions

CourseFlow is pre-release (v0.1.0 in development). Only the latest `main` branch is supported.

## Reporting a vulnerability

Please **do not open a public issue** for security problems.

Report privately through GitHub's **[private vulnerability reporting](../../security/advisories/new)** for this repository (Security tab → "Report a vulnerability"). Include steps to reproduce, the affected component and the potential impact. You should receive an acknowledgement within a few days.

## Security model (summary)

- **Course content is untrusted.** Text in uploaded or imported documents is treated as data, never as instructions. Prompt-injection content must not change workflow control flow or tool use.
- **Allowlisted, typed tools only.** The AI workflow has no shell access, no arbitrary SQL and no open internet access. Tool arguments are validated server-side.
- **Secrets stay out of prompts and git.** Credentials live in environment variables only. `.env` files are git-ignored, and only [.env.example](.env.example) with placeholders is committed.
- **Read-only towards the LMS.** CourseFlow never submits, edits or grades university content.
- **Synthetic demo data.** The repository contains no real university material or student data.

A dedicated threat model will be added as the system is implemented.
