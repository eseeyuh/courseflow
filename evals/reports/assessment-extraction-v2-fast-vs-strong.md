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

## Open question for review

Gate v3 reads a deadline's temporal role from the quote alone, so a correct but bare date quote cannot verify. The other option is to read it from the quote's containing sentence or line in the same span: "Deadline: Monday 30 November 2026, 12:00 (UK time)." would then verify for a quote of just the date. Choosing between them is a gate-design decision for review, not a tweak after seeing these results.
