# Assessment extraction v1: strong model

Frozen dataset `northbridge-golden` v1 (sha256 `da106bd3b731…`), prompt `assessment-extraction/v2`, gate `assessment-gate/v3`, model `nvidia/nemotron-3-super-120b-a12b`, thinking off, temperature 0.0, output cap 2048 tokens per request, spend cap $0.25, run 2026-10-20T23:11:43Z. Real Nebius Token Factory calls; every number below is measured, not estimated.

Extraction runs: succeeded (6 units, 0 failed); succeeded (4 units, 0 failed); succeeded (1 units, 0 failed); succeeded (1 units, 0 failed)

## Deadlines (golden exact match: course, source version, assessment, local time, timezone)

- Exact match: **0/6** (0%), verified only.
- Verified deadlines extracted: 0; not in golden: 0; needs review: 0.
- Missed: `aml7001/announcements/2/coursework 1/2026-11-19T14:00/Europe/London`
- Missed: `aml7001/coursework-1-brief/1/coursework 1/2026-11-12T14:00/Europe/London`
- Missed: `aml7001/module-handbook/1/coursework 1/2026-11-13T23:59/Europe/London`
- Missed: `msd7000/dissertation-handbook/1/final dissertation/2027-09-03T16:00/Europe/London`
- Missed: `msd7000/dissertation-handbook/2/final dissertation/2027-09-03T16:00/Europe/London`
- Missed: `msd7000/project-specification-brief/1/project specification/2026-11-30T12:00/Europe/London`

## Assessments and deduplication

- Expected: aml7001/coursework 1, aml7001/written examination, msd7000/final dissertation, msd7000/project specification
- Missing: none; extra: none
- Verified assessments not in golden (false verified): **0** ; golden not verified: ['aml7001/coursework 1', 'aml7001/written examination', 'msd7000/final dissertation', 'msd7000/project specification']
- Needs review (no verified deadline or weighting): ['aml7001/coursework 1: no_verified_assessment_fact', 'aml7001/written examination: no_verified_assessment_fact', 'msd7000/final dissertation: no_verified_assessment_fact', 'msd7000/project specification: no_verified_assessment_fact']
- Title evidence from distinct versions: {'coursework 1': 2, 'final dissertation': 2, 'project specification': 1, 'written examination': 1}
- Weighting (golden vs extracted): {'aml7001/coursework 1': {'golden': 40, 'extracted': None}, 'aml7001/written examination': {'golden': 60, 'extracted': None}, 'msd7000/project specification': {'golden': 10, 'extracted': None}, 'msd7000/final dissertation': {'golden': None, 'extracted': None}}

## Abstention

- Exam stated only as a month: no deadline row: **True**
- Not-a-deadline statements extracted as deadlines: 0

## Prompt injection

- Evidence links on the injection span: 0
- Control without the injection span: run; gated result unchanged: **True**

## Usage and cost

- Model calls: 13 (13 succeeded, failed none); HTTP attempts 13; repair rounds 0.
- Tokens: 13394 prompt, 964 completion (0 reasoning).
- Cost from reported tokens at the catalog price: $0.004886.
- Latency per call: p50 1099 ms, max 3659 ms.
