# Obligation extraction: zero-shot vs few-shot (claim-gate/v3)

Both runs used `nvidia/Nemotron-3_5-Lightning`, thinking off, temperature 0, the same corpus and inputs, the same schema and the same gate `claim-gate/v3`. Assessments were replayed from the same recorded FAST run in both.

- **Zero-shot:** prompt `obligation-extraction/v1`, the existing live run ([report](claim-extraction-v1-fast-thinking-off-gate-v3.md)), not rerun.
- **Few-shot:** prompt `obligation-extraction/v2` ([report](obligation-extraction-v2-obligations-fast-thinking-off-gate-v3.md)). It is v1 plus three invented examples, frozen and committed before the call: a mandatory process obligation, a permissive statement, and assessment content that is not an obligation. None of them comes from the corpus.

The decision rule was fixed before the run: keep few-shot only if it improves labelled obligation recall without regressing the supervisor-review case, ambiguity handling, generic-text rejection, injection safety or the requirement/obligation boundary. The golden obligation labels are not exhaustive, so no precision or F1 is reported.

| | Zero-shot (v1) | Few-shot (v2) |
|---|---|---|
| Labelled obligation recall (verified) | 2/3 | 2/3 |
| Supervisor-review case (both versions, page 2) | pass | pass |
| Ambiguous: low-risk route | needs_review | needs_review |
| Ambiguous: progress note | needs_review | not extracted |
| Generic informational text verified | 0 | 0 |
| `wrong_claim_type` rejections / content verified as obligation | 1 / 0 | 1 / 0 |
| Injection: claims on it / control unchanged | 0 / yes | 0 / yes |
| Unlabelled for review (verified) | 6 (1) | 5 (1) |
| Calls / repair rounds / HTTP attempts | 15 / 0 / 15 (incl. 2 requirement calls) | 13 / 2 / 15 |
| Tokens, prompt / completion (zero-shot total includes its 2 requirement calls) | 18,913 / 3,008 | 25,231 / 3,641 |
| Cost | $0.001857 (all 15 calls) | $0.002388 |
| Latency per call, p50 | 8.7 s | 27.4 s |

## Decision: few-shot rejected

Few-shot did not improve labelled recall: both runs miss the resample obligation, which neither model output proposed. It also kept no safety property better, and the progress note went from needs_review to not extracted. That is not a verification regression, but it is less coverage for review. It cost more input tokens, needed two repair rounds, and roughly tripled median latency. `obligation-extraction/v1` stays the obligation prompt; v2 is kept only as the record of this ablation.
