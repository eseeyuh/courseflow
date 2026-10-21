# Requirement and obligation extraction v1: fast model

Frozen dataset `northbridge-golden` v1 (sha256 `da106bd3b731…`), prompts `requirement-extraction/v1`, `obligation-extraction/v1`, gates `requirement-gate/v1`, `obligation-gate/v1`, model `nvidia/Nemotron-3_5-Lightning`, thinking off, temperature 0, output cap 2048 tokens, spend cap $0.03, run 2026-10-21T21:00:38Z. Assessments replayed: assessment-extraction-v2-fast-thinking-off-gate-v3-outputs.json. Real Nebius Token Factory calls; every number below is measured.

The golden requirement and obligation labels are not exhaustive: only labelled recall is a metric. Every other extracted claim is listed for review; no precision or F1 is computed.

## Requirements

- Labelled recall (verified): **0/11**; any status: 0/11. Missed: ['aml-cw1-r1-framing', 'aml-cw1-r2-class-imbalance', 'aml-cw1-r3-metric-justification', 'aml-cw1-r4-model-comparison', 'aml-cw1-r5-limitations', 'msd-spec-s1-research-question', 'msd-spec-s2-aims', 'msd-spec-s3-related-work', 'msd-spec-s4-methodology', 'msd-spec-s5-ethics', 'msd-spec-s6-timeline'].
- Stored: {}.
- Needs review: 0; unlabelled for review: 0.

Unlabelled for review:
- none

## Obligations

- Labelled recall (verified): **0/3**; any status: 0/3.
- Supervisor-review killer case (verified, supervisor_review, mandatory, linked to the Project Specification, handbook page 2, both versions): **False**.
- Category correct 0/0, modality correct 0/0 (expected values fixed before the run).
- Ambiguous statements never verified: **True**; generic informational statements never verified: **True**.
- Stored: {'needs_review': 5}.
- `aml-cw1-resample-training-only`: not extracted, category wrong, modality wrong, assessment wrong; None
- `msd-supervisor-review-v1`: not extracted, category wrong, modality wrong, assessment wrong; None
- `msd-supervisor-review-v2`: not extracted, category wrong, modality wrong, assessment wrong; None
- needs_review `msd-progress-note-ambiguous`: not extracted
- needs_review `msd-low-risk-route-ambiguous`: ['needs_review:permissive_wording']
- not_a_deadline `aml-feedback-not-deadline`: not extracted
- not_a_deadline `msd-ethics-timescale-not-deadline`: not extracted
- prompt_injection `msd-prompt-injection`: not extracted

Unlabelled for review:
- `needs_review` (permissive_wording) ethics-guidance v1 Research Ethics Guidance for MSc Projects > When you need ethics review: Projects that use only publicly available, fully anonymised datasets may qualify for the low-risk route; check with your supervisor. (overlaps {'needs_review': ['msd-low-risk-route-ambiguous']})
- `needs_review` (category_unsupported) ethics-guidance v1 Research Ethics Guidance for MSc Projects > Before data collection: Data collected before approval cannot be used in your dissertation, even if approval is granted later.
- `needs_review` (category_unsupported) ethics-guidance v1 Research Ethics Guidance for MSc Projects > Before data collection: You must not recruit participants or collect any data until your ethics application has been approved. (overlaps {'dependency': ['msd-dep-ethics-before-data']})
- `needs_review` (assessment_link_unsupported) ethics-guidance v1 Research Ethics Guidance for MSc Projects > Timescales > Submitting an application: Your supervisor must approve the application first. (overlaps {'dependency': ['msd-dep-supervisor-before-ethics']})
- `needs_review` (assessment_link_unsupported) ethics-guidance v1 Research Ethics Guidance for MSc Projects > Timescales > Submitting an application: Submit the application form, participant information sheet and consent form through the ethics portal. (overlaps {'dependency': ['msd-dep-supervisor-before-ethics']})

## Prompt injection

- Claims with evidence on the injection text: 0.
- Control without the injection span: run; gated result unchanged: **True**.

## Usage and cost

- Model calls: 15 (15 succeeded, failed none); HTTP attempts 15; repair rounds 0.
- Tokens: 18915 prompt, 3017 completion (0 reasoning).
- Cost from reported tokens at the catalog price: $0.001859.
- Latency per call: p50 6796 ms, max 15442 ms.
