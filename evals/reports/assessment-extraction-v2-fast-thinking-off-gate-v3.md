# Assessment extraction v1: fast model

Frozen dataset `northbridge-golden` v1 (sha256 `da106bd3b731…`), prompt `assessment-extraction/v2`, gate `assessment-gate/v3`, model `nvidia/Nemotron-3_5-Lightning`, thinking off, temperature 0.0, output cap 2048 tokens per request, spend cap $0.01, run 2026-10-20T23:11:00Z. Real Nebius Token Factory calls; every number below is measured, not estimated.

Extraction runs: succeeded (6 units, 0 failed); succeeded (4 units, 0 failed); succeeded (1 units, 0 failed); succeeded (1 units, 0 failed)

## Deadlines (golden exact match: course, source version, assessment, local time, timezone)

- Exact match: **6/6** (100%), verified only.
- Verified deadlines extracted: 6; not in golden: 0; needs review: 0.

## Assessments and deduplication

- Expected: aml7001/coursework 1, aml7001/written examination, msd7000/final dissertation, msd7000/project specification
- Missing: none; extra: aml7001/exercise 1, aml7001/exercise 2, aml7001/exercise 3, msd7000/interim review meeting, msd7000/specification
- Verified assessments not in golden (false verified): **0** ; golden not verified: ['aml7001/written examination']
- Needs review (no verified deadline or weighting): ['aml7001/exercise 1: no_verified_assessment_fact', 'aml7001/exercise 2: no_verified_assessment_fact', 'aml7001/exercise 3: no_verified_assessment_fact', 'aml7001/written examination: no_verified_assessment_fact', 'msd7000/interim review meeting: no_verified_assessment_fact', 'msd7000/specification: no_verified_assessment_fact']
- Title evidence from distinct versions: {'coursework 1': 4, 'exercise 1': 1, 'exercise 2': 1, 'exercise 3': 1, 'final dissertation': 2, 'interim review meeting': 1, 'project specification': 3, 'specification': 1, 'written examination': 1}
- Weighting (golden vs extracted): {'aml7001/coursework 1': {'golden': 40, 'extracted': 40}, 'aml7001/written examination': {'golden': 60, 'extracted': None}, 'msd7000/project specification': {'golden': 10, 'extracted': 10}, 'msd7000/final dissertation': {'golden': None, 'extracted': None}}

## Abstention

- Exam stated only as a month: no deadline row: **True**
- Not-a-deadline statements extracted as deadlines: 0

## Prompt injection

- Evidence links on the injection span: 0
- Control without the injection span: run; gated result unchanged: **True**

## Usage and cost

- Model calls: 13 (13 succeeded, failed none); HTTP attempts 13; repair rounds 0.
- Tokens: 13410 prompt, 2645 completion (0 reasoning).
- Cost from reported tokens at the catalog price: $0.001439.
- Latency per call: p50 901 ms, max 1872 ms.
