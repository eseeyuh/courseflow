# ADR-003 — Mozilla Public License 2.0 for the public repository

- **Status:** Accepted
- **Date:** 2026-10-05

> This ADR records an engineering and product decision. It is **not legal advice**. Any commercial licensing strategy should be reviewed by a qualified lawyer before relying on it.

## Context

The hackathon requires a public repository under an open-source licence. CourseFlow may also become a commercial product later, for example with institutional LMS connectors or hosted features. The licence should therefore:

1. be a recognised OSI-approved open-source licence;
2. let anyone read, run, evaluate and build on the hackathon code;
3. make sure improvements to CourseFlow's own files stay open when redistributed;
4. not force every future component combined with this code to be open-sourced;
5. include an explicit patent grant.

## Decision

License the repository under the **Mozilla Public License 2.0 (MPL-2.0)**. The full text is in [`LICENSE`](../../LICENSE).

How MPL-2.0 works, in short:

- **File-level copyleft.** Anyone who distributes a modified version of an MPL-licensed file must make that file's source available under MPL-2.0.
- **Larger Works are allowed.** MPL-covered files can be combined with separate files under other licences, including proprietary ones. Those other files aren't pulled under MPL.
- **Patent grant.** Contributors grant a licence to their patent claims that read on their contributions.
- **GPL-compatible by default.** The licence text includes Exhibit B ("Incompatible With Secondary Licenses") for files that should opt out. CourseFlow does **not** use Exhibit B, so it stays combinable with the "Secondary Licenses" defined in MPL-2.0 §1.12: GPL 2.0, LGPL 2.1, AGPL 3.0, or any later versions of those licences.

### Boundary with possible future proprietary components

- Everything in this repository is MPL-2.0.
- Any future proprietary components (for example a production institutional connector) would live in **separate files and/or a separate repository**, integrating through the published interfaces (`LMSConnector`, `AIProvider`, the HTTP API). They would not be modifications of MPL-covered files.
- Changes to MPL-covered files that are distributed stay MPL-2.0.

### Notices

MPL-2.0 Exhibit A asks for a short licence notice in each Source Code Form file. Only "if it is not possible or desirable" to put the notice in a particular file may it live elsewhere, such as a `LICENSE` file. Therefore:

- From the first source file onward, new **source files** (Python, TypeScript) start with the standard Exhibit A header:
  ```text
  This Source Code Form is subject to the terms of the Mozilla Public
  License, v. 2.0. If a copy of the MPL was not distributed with this
  file, You can obtain one at https://mozilla.org/MPL/2.0/.
  ```
- Documentation and configuration files rely on the root `LICENSE` file.

## Alternatives considered

| Licence | Assessment |
|---|---|
| **Apache-2.0** | Permissive, with an explicit patent grant and very business-friendly. Downside for us: anyone may take and modify CourseFlow's core files in a closed product without sharing improvements. A strong, simple alternative if file-level obligations later prove a burden for contributors. |
| **MIT** | Shortest and most permissive, with no explicit patent grant and no copyleft. Same "improvements may stay closed" downside as Apache-2.0, plus weaker patent clarity. |
| **GPL-3.0 / AGPL-3.0** | Strong copyleft: combined or derivative works (and, for AGPL, network use) must be released under the same licence. That would constrain any future proprietary components built alongside CourseFlow, which conflicts with requirement 4. |
| **Source-available (BSL, Elastic)** | Not OSI-approved open source, so it doesn't satisfy the hackathon requirement. |

## Consequences

**Positive**

- Meets the hackathon's open-source requirement.
- Improvements to CourseFlow's own files stay open when redistributed.
- Leaves room for separate proprietary components without relicensing.
- Explicit patent grant, and recognised and accepted by most organisations.

**Negative / risks**

- Contributors and integrators need to understand file-level obligations, which is slightly more friction than MIT or Apache-2.0.
- The proprietary/open boundary depends on discipline: future proprietary work must not be built by modifying MPL-covered files.
- Relicensing later requires agreement from all copyright holders of the contributed code, which is simple while the project is single-author.

## Revisit when

- External contributors join. Consider a contributor licence agreement (CLA) or Developer Certificate of Origin (DCO) before accepting significant outside contributions.
- A commercial deployment or institutional partnership needs a licensing review (get legal advice).
- File-level copyleft blocks a concrete integration partner.
