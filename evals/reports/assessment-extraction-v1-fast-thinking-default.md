# Assessment extraction v1: fast model

Frozen dataset `northbridge-golden` v1 (sha256 `da106bd3b731…`), prompt `assessment-extraction/v1`, model `nvidia/Nemotron-3_5-Lightning`, thinking model default, temperature 0.0, output cap 2048 tokens per request, spend cap $0.01, run 2026-10-20T21:26:27Z. Real Nebius Token Factory calls; every number below is measured, not estimated.

Extraction runs: failed (6 units, 4 failed: {'truncated': 4}); failed (4 units, 4 failed: {'truncated': 4}); failed (1 units, 1 failed: {'truncated': 1}); failed (1 units, 1 failed: {'unexpected': 1})

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
- Missing: aml7001/coursework 1, aml7001/written examination, msd7000/final dissertation, msd7000/project specification; extra: none
- Title evidence from distinct versions: {}
- Weighting (golden vs extracted): {'aml7001/coursework 1': {'golden': 40, 'extracted': None}, 'aml7001/written examination': {'golden': 60, 'extracted': None}, 'msd7000/project specification': {'golden': 10, 'extracted': None}, 'msd7000/final dissertation': {'golden': None, 'extracted': None}}

## Abstention

- Exam stated only as a month: no deadline row: **None**
- Not-a-deadline statements extracted as deadlines: 0

## Prompt injection

- Evidence links on the injection span: 0
- Control without the injection span: not_run: refused by the spend guard (see usage.failed); gated result unchanged: **not measured**

## Usage and cost

- Model calls: 13 (2 succeeded, failed {'truncated': 9, 'unexpected': 2}); HTTP attempts 13; repair rounds 0.
- Tokens: 10745 prompt, 22234 completion (22216 reasoning).
- Cost from reported tokens at the catalog price: $0.005981.
- Latency per call: p50 7306 ms, max 18183 ms.
