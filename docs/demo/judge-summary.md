# CourseFlow: judge summary

**CourseFlow turns a student's scattered LMS content into evidence-backed next steps: what is due, what it requires, where exactly to study for it, and how the system knows.**

## Problem

A course is spread across briefs, handbooks, slides and announcements. Deadlines disagree between sources. Requirements do not say which lecture teaches them. Obligations hide in handbooks the brief never mentions. Announcements change dates without saying what else changed. Students reconstruct all of this by hand, and miss things.

## Solution

CourseFlow ingests a course's LMS resources as versioned sources with exact page, slide and section locations. It extracts assessments, deadlines, requirements and obligations, and maps each requirement to the exact teaching material. It reconstructs dependencies, detects conflicts, and builds one cross-course study plan with a reason for every step. When a source changes, it reprocesses only what changed and shows the downstream effect. Every claim opens the exact sentence it came from.

## What is technically novel

- **Evidence as a hard contract.** A claim is verified only when deterministic code finds its exact quote, uniquely, in a stored span of the same course and version, and the quote supports the value. Model confidence never verifies anything.
- **Conflicts are first-class.** Disagreeing sources become two claims and a `Conflict`, never a silent winner. Only a newer source that says it replaces a date retires it.
- **Incremental change propagation.** A new resource version produces a `ChangeEvent`. Affected claims are superseded or reworded, conflicts recomputed, and the study plan diffed: before and after, per task.
- **Retrieval-constrained verification.** Hybrid retrieval (pgvector plus full-text, fused by RRF) finds candidates. The model may only choose among their IDs.

## NVIDIA and Nebius

The model steps run on **NVIDIA Nemotron** (`nvidia/Nemotron-3_5-Lightning`) through **Nebius Token Factory**, with strict JSON-schema outputs:

- assessment, deadline, requirement and obligation extraction;
- the material-mapping verifier.

Embeddings are `Qwen/Qwen3-Embedding-8B` on Token Factory. `nvidia/nemotron-3-super-120b-a12b` was evaluated under the same protocol; FAST stays the default. All runs used real calls under spend caps (cents in total). Details: [nvidia-nebius.md](nvidia-nebius.md).

## Agentic workflow

`course-intelligence/v1` is a traced multi-step workflow: `ingest → assessments → claims → mapping → dependencies → conflicts → validation → plan`. It writes one `WorkflowRun` per course, with step status, counts, child runs, model calls (model, prompt version, tokens, latency) and deterministic failure categories. Model steps propose; typed gates and versioned rules decide; a sync re-enters the workflow only for what changed. It is read-only toward the LMS.

## Evidence and trust design

- **Provenance chain:** `EvidenceLink → SourceSpan` (exact offsets) `→ ResourceVersion → Resource → Course`. Citations are never generated.
- **Status:** `verified` vs `needs_review`, shown apart and never presented as fact.
- **Prompt injection:** course text goes to the model only as nonce-delimited data. The demo corpus contains an injection attempt, and it is never extracted.
- **Constrained outputs:** the model cites only IDs issued for that call.

## Strongest demo flow (3 minutes)

1. Home.
2. Coursework 1's class imbalance requirement → *Week 7: Learning from Imbalanced Data*, Slide 2, with its exact text.
3. "Found by CourseFlow": the handbook's supervisor review, required for the Project Specification.
4. The deadline conflict (12 Nov vs 13 Nov), with both sources and "CourseFlow will not silently choose between conflicting university sources."
5. Simulate LMS sync: the conflict becomes 13 Nov vs 19 Nov, 12 Nov is replaced, a reworded obligation is marked, and the study plan updates.

The demo runs offline from recordings of the real runs: identical every time, no key needed. Script: [demo-script.md](demo-script.md).

## Limitations

- A synthetic university and a simulated LMS (no live Moodle connector).
- The demo replays recorded model outputs.
- One demo learner, no authentication.
- Mapping can list several slides of the right lecture.
- Obligation labelled recall is 2/3.
- Small evaluation sets: measured, not statistically significant.
