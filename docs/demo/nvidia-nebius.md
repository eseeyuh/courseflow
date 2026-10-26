# How CourseFlow uses NVIDIA Nemotron and Nebius Token Factory

CourseFlow calls models through **Nebius Token Factory**'s OpenAI-compatible API (`https://api.tokenfactory.nebius.com/v1/`) with the official `openai` SDK, behind one provider boundary ([ADR-005](../decisions/ADR-005-ai-provider-boundary.md)). Code asks for a **role**, never a model ID; the IDs are configuration and were checked against the live catalog ([model catalog](../experiments/model-catalog.md)).

| Role | Model | Used for |
|---|---|---|
| `MODEL_FAST` | `nvidia/Nemotron-3_5-Lightning` | Every model step in the workflow (below) |
| `MODEL_STRONG` | `nvidia/nemotron-3-super-120b-a12b` | Evaluated against FAST; not used by the demo |
| Embeddings | `Qwen/Qwen3-Embedding-8B` (4,096 dimensions) | Chunk and query vectors for hybrid retrieval |

## Where the model works

| Step | Prompt | What Nemotron proposes |
|---|---|---|
| Assessment extraction | `assessment-extraction/v2` | Assessments, deadlines (local date, time, timezone) and weightings, each with a quote and a span ID |
| Requirement extraction | `requirement-extraction/v1` | The requirements an assessment brief states |
| Obligation extraction | `obligation-extraction/v1` | Obligations anywhere in the course (for example, the handbook's supervisor review), with category and modality |
| Material verifier | `material-mapping/v1` | Which retrieved teaching chunks cover a requirement: candidate IDs from the list it was given, or `none` |

Every call uses native structured output (`response_format: json_schema`, `strict: true`), temperature 0 and thinking off (`chat_template_kwargs.enable_thinking=false`), with bounded retries and one repair round. Course text goes only into the user turn, between per-call random nonce markers; no tools are sent.

## What the model does not decide

The model proposes; deterministic code decides.

- **Evidence gates.** A proposed fact is kept only if its quote occurs exactly and uniquely in a span from the same course and version, and the quote supports the value (for example, the parsed date equals the claimed date). Otherwise it is rejected or marked `needs_review`. Model confidence never verifies anything.
- **Identifiers.** The model sees only local IDs issued for that call (`S1`, `S2`, … or candidate IDs) and cannot invent a location.
- **Consequences.** Dependencies, conflicts, deadline supersession, change impact and the study plan are deterministic, versioned rules (`dependency-rules/v1`, `conflict-rules/v1`, `source-supersession/v1`, `study-plan/v1`), not prompts.

## Measured, not estimated

From real Token Factory runs on the frozen synthetic golden dataset, each under a spend cap (reports in [evals/reports/](../../evals/reports/)):

| Run | Calls | Cost | Result |
|---|---|---|---|
| Assessment extraction (FAST) | 13 | $0.0014 | Deadline exact match 6/6; p50 latency 0.9 s |
| Requirement and obligation extraction (FAST) | 15 | $0.0019 | Requirements 11/11; obligations 2/3, including the supervisor-review case |
| Material mapping verifier (FAST) | 11 | $0.0013 | Class imbalance → Lecture 07 pass; irrelevant lecture rejected |
| Assessment extraction (STRONG, same protocol) | 13 | $0.0049 | Deadlines 0/6: correct dates, but quotes the gate rejected |

FAST stays the default by a rule declared before the comparison. One run per model does not show that either model is generally better ([comparison](../../evals/reports/assessment-extraction-v2-fast-vs-strong.md)).

## Why the demo replays recordings

The validated model outputs of those runs are committed (`evals/recordings/`, with no prompts, headers or credentials), together with the embedding snapshots (`evals/snapshots/northbridge/v1/`). The judge demo replays them through the same gates, rules and database writes, so it is identical on every run and needs no API key, network or spend. Replay serves demo reliability and reproducibility. The intelligence itself came from real Nemotron calls, and the evaluation runners (`--live`) make those calls again with a key.
