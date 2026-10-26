# Material mapping smoke run: material-mapping-v1-fast-thinking-off

Northbridge initial state; requirements from recorded FAST outputs; hybrid retrieval, k=5 chunks, teaching resources only; FAST verifier, thinking off, candidate IDs only.
Mode: live. Queries: 11. Verifier calls: 11. Spend: $0.00129284.

## Demo acceptance

- class_imbalance_maps_to_lecture_07: PASS
- irrelevant_lecture_rejected: PASS
- no_material_case_has_no_mapping: PASS
- mappings_subset_of_retrieved_candidates: PASS

Stored mappings: 58. Gate rejections: none.

## Cases (smoke, not a benchmark)

| split | label | expect | retrieval hit | mapping hit | selected | category |
|---|---|---|---|---|---|---|
| dev | aml-cw1-r1-framing | unlabelled | None | None | lecture-07-class-imbalance v1 slide 2; lecture-07-class-imbalance v1 slide 2; lecture-07-class-imbalance v1 slide 3; lecture-07-class-imbalance v1 slide 3; lecture-07-class-imbalance v1 slide 4; lecture-07-class-imbalance v1 slide 4; lecture-07-class-imbalance v1 slide 5; lecture-07-class-imbalance v1 slide 5; lecture-07-class-imbalance v1 slide 6; lecture-07-class-imbalance v1 slide 6 | unlabelled_for_review |
| dev | aml-cw1-r2-class-imbalance | materials | True | True | lecture-07-class-imbalance v1 slide 2; lecture-07-class-imbalance v1 slide 2; lecture-07-class-imbalance v1 slide 3; lecture-07-class-imbalance v1 slide 3; lecture-07-class-imbalance v1 slide 4; lecture-07-class-imbalance v1 slide 4; lecture-07-class-imbalance v1 slide 5; lecture-07-class-imbalance v1 slide 5; lecture-07-class-imbalance v1 slide 6; lecture-07-class-imbalance v1 slide 6 | ground_truth_ambiguity |
| dev | msd-spec-s1-research-question | materials | True | True | research-methods-lecture v1 slide 3; research-methods-lecture v1 slide 3; research-methods-lecture v1 slide 4; research-methods-lecture v1 slide 4 | ground_truth_ambiguity |
| dev | msd-spec-s6-timeline | no_material | None | None | — | correct |
| held_out | aml-cw1-r3-metric-justification | materials | True | True | lecture-07-class-imbalance v1 slide 2; lecture-07-class-imbalance v1 slide 2; lecture-07-class-imbalance v1 slide 4; lecture-07-class-imbalance v1 slide 4; lecture-07-class-imbalance v1 slide 5; lecture-07-class-imbalance v1 slide 9 | ground_truth_ambiguity |
| held_out | aml-cw1-r4-model-comparison | unlabelled | None | None | lecture-07-class-imbalance v1 slide 1; lecture-07-class-imbalance v1 slide 1; lecture-07-class-imbalance v1 slide 2; lecture-07-class-imbalance v1 slide 2; lecture-07-class-imbalance v1 slide 2; lecture-07-class-imbalance v1 slide 3; lecture-07-class-imbalance v1 slide 3; lecture-07-class-imbalance v1 slide 4; lecture-07-class-imbalance v1 slide 4; lecture-07-class-imbalance v1 slide 5 | unlabelled_for_review |
| held_out | aml-cw1-r5-limitations | unlabelled | None | None | lecture-07-class-imbalance v1 slide 2; lecture-07-class-imbalance v1 slide 2; lecture-07-class-imbalance v1 slide 4; lecture-07-class-imbalance v1 slide 4; lecture-07-class-imbalance v1 slide 5; lecture-07-class-imbalance v1 slide 8; lecture-07-class-imbalance v1 slide 9 | unlabelled_for_review |
| held_out | msd-spec-s2-aims | materials | True | True | research-methods-lecture v1 slide 5; research-methods-lecture v1 slide 5 | ground_truth_ambiguity |
| held_out | msd-spec-s3-related-work | no_material | None | None | — | correct |
| held_out | msd-spec-s4-methodology | materials | True | True | research-methods-lecture v1 slide 6; research-methods-lecture v1 slide 6; research-methods-lecture v1 slide 7; research-methods-lecture v1 slide 7 | ground_truth_ambiguity |
| held_out | msd-spec-s5-ethics | materials | True | True | ethics-guidance v1 Research Ethics Guidance for MSc Projects; ethics-guidance v1 Research Ethics Guidance for MSc Projects > Before data collection; ethics-guidance v1 Research Ethics Guidance for MSc Projects > Before data collection; ethics-guidance v1 Research Ethics Guidance for MSc Projects > When you need ethics review; ethics-guidance v1 Research Ethics Guidance for MSc Projects > When you need ethics review | ground_truth_ambiguity |
