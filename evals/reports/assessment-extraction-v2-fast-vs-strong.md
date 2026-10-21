# Assessment extraction: fast vs strong (prompt v2, gate v3)

The protocol, frozen before any strong-model call, was identical for both models:
- **Data and extraction:** the Northbridge golden labels v1 and corpus, the same 13 units, prompt `assessment-extraction/v2`, gate `assessment-gate/v3` and the strict schema.
- **Decoding:** thinking off, temperature 0, 2,048 output tokens per request, 3 attempts and 1 repair round.

Reports: [fast](assessment-extraction-v2-fast-thinking-off-gate-v3.md) and [strong](assessment-extraction-v2-strong-thinking-off-gate-v3.md). Each model was run once, and every number below is measured.

| Criterion | FAST `nvidia/Nemotron-3_5-Lightning` | STRONG `nvidia/nemotron-3-super-120b-a12b` |
|---|---|---|
| **Hard:** deadline exact match | **6/6** | **0/6** |
| **Hard:** invented deadlines | 0 | 0 |
| **Hard:** false verified assessments | 0 | 0 |
| **Hard:** injection (evidence on the injection span; control unchanged) | pass | pass |
| **Hard:** abstention (month-only exam; not-a-deadline statements) | pass | pass |
| **Hard:** dedup and provenance | pass (Coursework 1 across 4 versions) | not exercised (no verified facts) |
| Golden assessments detected | 4/4 | 4/4 |
| Golden assessments verified | 3/4 | 0/4 |
| Golden weightings exact | 2/3 | 0/3 |
| needs_review assessments | 6 (all `no_verified_assessment_fact`) | 4 (all `no_verified_assessment_fact`) |
| Calls (succeeded / repairs / truncations / errors) | 13 / 0 / 0 / 0 | 13 / 0 / 0 / 0 |
| Tokens (prompt / completion / reasoning) | 13,410 / 2,645 / 0 | 13,394 / 964 / 0 |
| Latency per call (p50 / max) | 901 / 1,872 ms | 1,099 / 3,659 ms |
| Cost | $0.001439 | $0.004886 |

## Reading the result

By the predeclared rule the strong model is **not acceptable** in this run, because it regressed a hard criterion: 0/6 deadlines. The cause is specific and visible in its outputs:
- **Correct dates, bare quotes.** It proposed the correct date and time for all six golden deadlines, but quoted only the date ("Thursday 12 November 2026, 14:00 (UK time)") and left out the adjacent "Submission deadline:".
- **The gate judges the quote alone.** Gate v3 decides a deadline's temporal role from the quote only, so each of these was rejected as `no_deadline_semantics`.
- **No weightings.** It also returned no weightings and was much terser overall (964 completion tokens against 2,645).

The result measures how this model's quoting style interacts with this gate rule, as much as its extraction ability. It does not show that one model is generally better.

Run-to-run variance is large even for the fast model. Across its measured runs:
- **Weightings:** 3/3, 1/3, 3/3 (replay) and 2/3.
- **Verified assessments:** 4/4 and 3/4.

One run of each model cannot support a claim of superiority. `MODEL_FAST` stays the extraction default; this is P1 evidence, not a routing decision.

## Decision: gate v3 stays unchanged for this comparison

Gate v3 stays as it was, and neither model is rerun. A sentence-context rule was only thought of after seeing the strong model's failures. Changing the gate now would be post-hoc tuning, and it would invalidate this frozen comparison unless both models were rerun under a new gate version declared in advance.

The limitation of this result:
- **The quote must carry the semantics.** Gate v3 requires a deadline's temporal role to be supported by the evidence quote itself.
- **The strong model quoted bare dates.** It often returned an exact quote of the date alone and left out the adjacent "Deadline:" or "Submission deadline:" label.
- **What 0/6 measures.** The strong model's 0/6 measures how its quoting behaviour interacts with gate v3. It does not show that it cannot identify the dates.
- **No general claim.** Nothing here shows the fast model is generally better.

## Future design note: `assessment-gate/v4`, bounded evidence context

This is a candidate rule, not implemented. Any adoption needs a new gate version, declared before the runs, with both models rerun under it.

- **Anchor.** The exact quote remains the evidence anchor.
- **Bounded context.** Semantic-role validation may inspect only deterministic local context: the sentence or line containing the quote and, where structurally linked, its immediate label or section heading.
- **No unrestricted support.** Arbitrary neighbouring text, or the whole span, is never used as support.
- **Provenance unchanged.** Exact offsets and `SourceSpan` provenance remain required.
- **Negatives win.** Negative temporal semantics (for example "not a deadline") still override any positive inference.

Example that v4 could verify:

```text
Submission deadline: Thursday 12 November 2026, 14:00
```

where the model cites only `Thursday 12 November 2026, 14:00`. The label sits on the same line as the quote, so v4 could read the deadline role from it. Gate v3 rejects the same citation as `no_deadline_semantics`.
