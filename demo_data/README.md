# Demo data

**Everything here is synthetic.** It was written for CourseFlow's demo and evaluation. Northbridge University, its courses, staff, students, datasets, dates and documents are fictional. No text comes from any real institution, course or person. Any resemblance to a real one is accidental.

## Northbridge University (`northbridge/`)

| Path | Contents |
|---|---|
| `manifest.yaml` | Institution, courses, resources, each version's file and publication time, the intended active version, and which demo user sees which course (`demo-manifest/v1`) |
| `courses/aml7001/` | AML7001 Applied Machine Learning |
| `courses/msd7000/` | MSD7000 MSc Dissertation |

The documents (PDF, DOCX, PPTX and HTML) are **generated**, never edited by hand. Their text lives in `backend/app/demo/northbridge_content.py`, and the renderer pins all metadata, so the same content always produces the same bytes:

```bash
cd backend
uv run python -m app.demo.generate          # rewrite courses/
uv run python -m app.demo.generate --check  # verify the committed files byte for byte
```

The material deliberately contains what CourseFlow has to handle:

- deadlines and weightings;
- assessment requirements;
- a hidden obligation (a supervisor review that must come before the Project Specification);
- a prerequisite (ethics approval before data collection);
- a deadline in the module handbook that conflicts with the coursework brief;
- an irrelevant lecture;
- an ambiguous statement;
- a prompt-injection sentence.

That sentence is course text only and must never be followed as an instruction.

Two resources change after the initial import, and each keeps its identity as the same resource with a new version:

- the AML7001 announcements page moves the Coursework 1 deadline (v2);
- the dissertation handbook changes the supervisor-review lead time (v2).

`backend/app/connectors/demo.py` (`DemoConnector`) serves this institution through the same `LMSConnector` interface a real LMS connector will implement.

The documents are deliberately not phrased to make extraction easy. Evaluation labels are kept separately, under `evals/datasets/`.
