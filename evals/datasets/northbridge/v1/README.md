# Northbridge golden labels v1

This is the expected truth about the synthetic Northbridge University demo institution (`demo_data/northbridge/`, fictional). It was frozen before any extraction prompt, model output or score existed. A frozen version is never edited: corrections go into a new version (`v2`), and results reported against v1 stay valid for v1.

| File | Contents |
|---|---|
| `golden.jsonl` | 36 labels, one JSON object per line (`northbridge-label/v1`) |
| `manifest.json` | Counts, rules and the hash definition (`northbridge-dataset/v1`) |
| `dataset.sha256` | The freeze hash (not itself a hash input) |

## Label kinds

| Kind | Count | Expected truth |
|---|---|---|
| `assessment` | 4 | Each assessment and its weighting |
| `deadline_claim` | 5 | Every source statement of a deadline, the states in which it is in force, and what supersedes it |
| `conflict` | 2 | The Coursework 1 deadline conflict, before and after the announcement changes the date |
| `requirement` | 11 | Assessment requirements, with the original teaching material that supports them (mapping and retrieval targets) |
| `obligation` | 3 | Including the hidden supervisor review, in its v1 and v2 forms |
| `dependency` | 3 | Prerequisites, such as ethics approval before any data collection |
| `change` | 2 | What each v2 changes, with the before and after evidence and the labels it affects |
| `needs_review` | 2 | Ambiguous statements that must produce abstention, never confident facts |
| `prompt_injection` | 1 | Instruction-like course text that must never be followed or extracted |
| `irrelevant_resource` | 1 | A lecture that supports none of Coursework 1's requirements |
| `not_a_deadline` | 2 | Time statements that look like deadlines but are not |

"Initial" means the state at the corpus manifest's `initial_sync_at`. "Final" means the state after every version.

## Evidence

Every piece of evidence is an exact quote from one resource version, with the expected PDF page, slide or section path. The loader checks each one against the production parser's output:

- the quote occurs exactly once in that version's text;
- it lies inside exactly one SourceSpan;
- that span's locator is the labelled one.

It also checks that every label referenced by id exists, and that each conflict links deadline claims in force in its state.

## Freeze hash

The hash uses `sha256-path-and-bytes/v1`, the retrieval benchmark's scheme. It covers this directory's `manifest.json` and `golden.jsonl`, `demo_data/northbridge/manifest.yaml`, and every corpus file that manifest lists, in bytewise order of their repository-relative POSIX paths. Regenerating the corpus with different bytes breaks the hash, which is intentional.

```bash
cd backend
uv run python -m app.evaluation.northbridge.labels check
```
