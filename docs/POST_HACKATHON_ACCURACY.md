# Post-hackathon accuracy work

Quality work deliberately deferred while the end-to-end prototype is built. Nothing here weakens provenance, evidence, same-course boundaries, invented-ID rejection, prompt-injection protection, deterministic demo reset or conflict preservation; these items are about accuracy, measurement and hardening.

## Material mapping

- **Broad selection.** The FAST verifier selects many slides of the relevant lecture for requirements whose material is unlabelled (framing, model comparison, limitations). Measure precision against complete labels before tuning.
- **Two spans per slide.** A slide's title and body are separate spans, so one slide can appear twice in a requirement's materials. Group by slide in the API or the UI, or map at slide level.
- **Held-out evaluation.** The held-out split has only been scored inside the smoke run. Run it as a separate, reported benchmark before changing k, the query format or the prompt.
- **Recall@K at span level** with K = 1, 3, 5. With so few teaching chunks per Northbridge course, chunk-level Recall@5 is near trivial.
- **Rerank and fast-vs-strong routing**, only if retrieval misses become the bottleneck.
- **Validator coverage.** `validate_course_intelligence` does not yet re-check mappings (same course, current version, teaching type, exact range, lineage).

## Extraction and validation

- Cross-course evidence is detected by the validator, not blocked by a database constraint.
- Obligation canonical keys are derived from evidence and not independently re-checked.
- Upload courses (no external ID) fingerprint by row ID.
- Requirement and obligation labels are incomplete: report numeric precision and F1 only once the labels are complete.
- The resample-training-only obligation was not proposed by the FAST model; the few-shot variant did not fix it.
