# CourseFlow: submission copy

Paste-ready text for the hackathon submission form. Every number is from [technical-facts.md](technical-facts.md).

## Project name

CourseFlow

## One-line pitch

CourseFlow turns scattered, changing LMS content into an evidence-backed study plan, and shows students the exact source sentence behind every claim.

## Short description

University courses are spread across briefs, handbooks, slides and announcements that do not always agree. CourseFlow reads them and finds what each assessment requires, maps every requirement to the exact slide or page that teaches it, and surfaces obligations hidden in handbooks. When sources disagree on a deadline, it shows both instead of guessing. It turns all of this into one cross-course study plan with a reason for each step. When the LMS changes, CourseFlow reprocesses only what changed and shows what that did to the plan. NVIDIA Nemotron on Nebius Token Factory does the extraction and verification; deterministic evidence gates decide what counts as fact. Every claim opens the exact sentence it came from.

## Problem

A student's course information is fragmented across the LMS: the assignment brief, the module handbook, lecture slides, workshop sheets and announcements. Deadlines differ between documents. Requirements say what to do but not where it is taught. Obligations that block submission sit in a handbook the brief never mentions. When an announcement changes a date, nothing says what else changed. Students reconstruct this by hand, and the cost of missing one sentence can be a missed deadline or a rejected submission.

## Solution

CourseFlow ingests a course's LMS resources as versioned sources with exact page, slide and section locations, and builds structured, evidence-backed course intelligence from them:

- assessments, deadlines, requirements and obligations;
- each requirement mapped to the teaching material that covers it;
- dependencies and source conflicts;
- one cross-course study plan with a "why now" for every task.

When a source changes, CourseFlow reprocesses only the affected claims and shows the downstream effect.

## Key features

- **Hidden obligation discovery:** finds requirements stated only in handbooks (for example, a mandatory supervisor review before the Project Specification) and links them to the assessment they block.
- **Requirement → exact learning material:** each requirement points to the slides or pages of the course's own teaching material that cover it.
- **Evidence-backed intelligence:** every claim opens its exact source excerpt, version and location, verified or marked for review.
- **Dependency and conflict detection:** reconstructs what must happen first, and shows disagreeing sources side by side without choosing one.
- **Cross-course study plan:** one ordered plan across courses, with explicit study states (Not started, Studying, Studied).
- **LMS change propagation:** a changed source yields new, replaced and reworded claims, an updated conflict and a recomputed plan.

## How it works

A Next.js frontend over a FastAPI backend and PostgreSQL with pgvector. Sources are parsed into versioned spans with exact locations. A traced, multi-step workflow then does four things:

- extracts assessments, deadlines, requirements and obligations with Nemotron, using strict JSON schemas;
- retrieves candidate teaching material with hybrid search (pgvector plus PostgreSQL full-text, fused by reciprocal rank fusion);
- asks Nemotron to verify which candidates cover each requirement;
- passes every proposal through deterministic evidence gates.

Versioned rules derive dependencies, conflicts, deadline supersession and the study plan. A source change creates a change event, and only the affected work is redone.

## NVIDIA / Nebius usage

- **Models:**
  - `nvidia/Nemotron-3_5-Lightning`, through Nebius Token Factory's OpenAI-compatible API, performs every model step: assessment and deadline extraction, requirement and obligation extraction, and the material-mapping verifier. It runs with strict JSON-schema outputs, temperature 0 and thinking off.
  - `Qwen/Qwen3-Embedding-8B`, also on Token Factory, embeds content for retrieval.
  - `nvidia/nemotron-3-super-120b-a12b` was evaluated under the same protocol; the fast model stayed the default.
- **Measured cost:** all runs were real calls under spend caps, costing well under one cent each.
- **Demo:** the judge demo replays those recorded outputs through the same gates, so it is reproducible and needs no API key.

## Agentic aspect

CourseFlow is one structured, stateful workflow, not a group of chatty agents: ingest → extract → map → dependencies → conflicts → validate → plan.

- Each course gets a traced run with per-step status, counts, the model calls (model, prompt version, tokens, latency) and a deterministic failure category if a step fails.
- The model proposes typed facts and may cite only the IDs it was given. Gates and versioned rules decide what is stored and what follows from it.
- When the LMS changes, the workflow re-enters for the changed versions only.
- It is read-only toward the LMS.

## What is novel

- **Not a chatbot.** There is no chat box: CourseFlow builds persistent, structured state (claims, edges, conflicts, a plan) and shows it.
- **Not an LMS summariser.** It points to the university's original sentences and slides instead of rewriting them.
- **Not generic RAG.** Retrieval only proposes candidates. A verifier constrained to candidate IDs chooses among them, and a deterministic gate checks every quote and value against the stored source. Disagreement becomes a first-class conflict, and change becomes an explicit diff of the plan.

## Trust and safety

- **Exact provenance:** claim → evidence link → source span (character offsets) → resource version → resource → course. Citations are never generated.
- **Evidence grounding:** a claim is verified only if its quote occurs exactly and uniquely in the cited span and supports the value. Model confidence never verifies anything.
- **needs_review:** ambiguous or unsupported claims are kept apart and never presented as fact.
- **Conflict preservation:** disagreeing sources keep both values; only a newer source that says it replaces a date retires one.
- **Candidate-ID constraints:** the model can cite only the span or candidate IDs issued for that call.
- **Prompt-injection handling:** course text is passed as nonce-delimited data. The demo corpus contains an injection attempt, and it is never extracted.

## Demo

A three-minute walk through the synthetic Northbridge University:

1. Home shows the next actions, a deadline conflict and an obligation "Found by CourseFlow".
2. Coursework 1's class imbalance requirement opens *Week 7: Learning from Imbalanced Data*, Slide 2, with its exact text.
3. The dissertation handbook's supervisor review, required for the Project Specification, opens with its page-2 sentence.
4. The brief (12 Nov, 14:00) and the handbook (13 Nov, 23:59) disagree: both are shown, each with its source.
5. A simulated LMS sync brings in an announcement and a new handbook version. The conflict becomes 13 Nov vs 19 Nov, the 12 Nov date is replaced, a reworded obligation is marked, and the study plan updates.

The repository runs the whole demo with one Docker command, offline.

## Current limitations

- Synthetic university and a simulated LMS: no live Moodle connector yet.
- The demo replays recorded model outputs; processing new content needs an API key.
- One demo learner, no authentication.
- Material mapping can list several slides of the right lecture.
- Evaluation sets are small; obligation labelled recall is 2/3.

## Future work

- A real Moodle connector, with links back to the original LMS pages.
- Finer material mapping, measured with complete labels.
- Multi-student accounts and authentication.
- Live processing of new course content in the hosted app.
