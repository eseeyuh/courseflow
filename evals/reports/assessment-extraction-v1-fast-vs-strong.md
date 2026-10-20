# Assessment extraction v1: fast vs strong model

Status: **strong run not executed**, twice. First, a $0.01 cap could not cover one call's worst case. Then, with a $0.25 cap approved for an equivalent run on prompt v2, the required fresh fast baseline on prompt v2 ([`assessment-extraction-v2-fast-thinking-off-gate-v2.md`](assessment-extraction-v2-fast-thinking-off-gate-v2.md)) regressed two hard criteria: 5/6 deadlines and 1 invented deadline (a brief-release date). By the predeclared rule, the strong model was not called. The comparison was later completed on prompt v2 with gate v3: see [`assessment-extraction-v2-fast-vs-strong.md`](assessment-extraction-v2-fast-vs-strong.md). The comparison protocol below was frozen before any strong-model call. The strong run was stopped by the spend rule, so this report holds the fast baseline only. Every number is measured, not estimated, except the cost figures marked as estimates.

## Frozen protocol

The two runs share:
- **Data:** the frozen Northbridge corpus and golden labels (`northbridge-golden` v1, sha256 `da106bd3b731…`), with the same 13 extraction units (10 initial versions, 2 later versions, 1 injection control).
- **Extraction:** prompt `assessment-extraction/v1`, gate `assessment-gate/v2` and the strict `AssessmentExtraction` schema.
- **Decoding:** thinking off, temperature 0, output cap 2,048 tokens per request, 3 attempts, 1 repair round.

**Fast baseline:** the recorded gate-v2 run, [`assessment-extraction-v1-fast-thinking-off-gate-v2.md`](assessment-extraction-v1-fast-thinking-off-gate-v2.md). It is not rerun.

**Strong candidate:** `MODEL_STRONG` = `nvidia/nemotron-3-super-120b-a12b`. On 20 Oct 2026 it was listed in the provider catalog at $0.30 per million input tokens and $0.90 per million output tokens.

**Interpretation rule (fixed in advance):**
1. A model with any hard regression is not acceptable. Hard criteria: deadlines below 6/6, any invented deadline, any false verified assessment, an injection failure, or broken deduplication/provenance.
2. If both models pass the hard criteria, compare weighting recall and verified-assessment recall.
3. One run is never grounds to change the runtime model. `MODEL_FAST` stays the extraction default.

## Why the strong run was not executed

The spend cap for this run was $0.01. The rule was to stop if either the expected or the maximum cost exceeds it.

| | Estimate |
|---|---|
| Expected cost (harness plan: estimated input, 600 output tokens per call) | $0.0109 |
| Expected cost at the fast run's measured token counts (12,609 in, 2,690 out) | $0.0062 |
| Worst case of one call (3 attempts × 2 rounds, 2,048-token output cap) | $0.0175 |
| Worst case of the run without the runtime guard | $0.2206 |

A single call's worst case exceeds the whole cap, so the runtime spend guard would refuse even the first call.

Two smaller changes would not fix this:
- **A smaller output cap.** The fast run's largest answer was 489 completion tokens, so a cap low enough to fit (about 384) would risk truncation.
- **No retries or repair with a 512-token cap.** The run's worst case would still be about $0.011.

Each of these would also break the frozen configuration. Running the comparison as frozen needs a larger approved cap (for example $0.25). A cheaper alternative is an explicitly non-equivalent configuration (no retries or repair, a 512-token cap) with a cap of about $0.02.

## Fast baseline (measured)

| Criterion | Fast (`nvidia/Nemotron-3_5-Lightning`) | Strong |
|---|---|---|
| Deadline exact match | 6/6 | not run |
| Invented deadlines | 0 | not run |
| False verified assessments | 0 | not run |
| Prompt injection (no evidence on the injection span; control unchanged) | pass | not run |
| Unsupported or ambiguous dates abstained (month-only exam, 2 not-a-deadline statements) | pass | not run |
| Dedup and provenance (Coursework 1 across 4 versions; one dissertation deadline linked to both versions) | pass | not run |
| Golden assessments detected | 4/4 | not run |
| Golden assessments verified | 3/4 (exam: `no_verified_assessment_fact`) | not run |
| Golden weightings exact | 1/3 | not run |
| needs_review assessments | 6, all `no_verified_assessment_fact` | not run |
| Calls (succeeded / repairs / truncations / errors) | 13 / 0 / 0 / 0 | not run |
| Tokens (prompt / completion / reasoning) | 12,609 / 2,690 / 0 | not run |
| Latency per call (p50 / max) | 932 / 1,758 ms | not run |
| Cost | $0.001402 | not run |

**Run-to-run variance.** The previous fast run used the same inputs and configuration. Its recorded outputs, replayed offline through gate v2, give 3/3 weightings and 4/4 verified assessments. The live gate-v2 run above gave 1/3 and 3/4. A single run of either model therefore cannot show that one is better than the other.
