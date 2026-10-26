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

## Demo UI

- **Mapping rationale is provenance, not prose.** The stored `rationale` is only a locator ("Slide 2 of …"), so the UI explains a mapping from its stored retrieval mode, rank and verifier prompt version. A per-mapping reason from the verifier would need a schema and prompt change, and an evaluation.
- **Broad mapping is visible.** Coursework 1's requirements each list four or five Week 7 slides (see Material mapping above); the class-imbalance requirement shows the intended slide among them, not alone.
- **No live source links.** The evidence viewer shows the stored title, version, slide, page or section and exact excerpt; the demo LMS has no URLs to open the original.
- **Client-side fetching.** Pages load from the API in the browser and refetch whole views after a demo action; fine for two courses, not for many.
- **Desktop-first layout.** Usable on a laptop; small screens are not tuned.
- **Studied state is per span.** Marking a study task studied sets every span it targets; partial progress inside a task is not shown.
- **Retrieval rank is uninformative.** Every mapping of Coursework 1 is stored with rank 1 and the same score, so the UI cannot (and does not) single out a best slide; it lists each slide with the first line of its own text. Store a per-requirement rank that orders the candidates.
- **Evidence drawer focus.** Focus moves into the drawer and back to its opener, but is not trapped inside it.
- **Demo script wording.** The draft script refers to the class imbalance requirement as "requirement 2"; the UI lists it in the brief's stored order, as the fifth.
