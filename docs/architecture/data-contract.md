# Data Contract — CourseFlow v0.1.0 (initial)

This document defines CourseFlow's **canonical domain vocabulary** and the **provenance chain** that every consequential claim must satisfy. Field lists are the minimum. Exact columns, types and constraints will be fixed in the SQLAlchemy models and Alembic migrations once implemented, and must stay consistent with this contract.

CourseFlow persists **typed domain objects and relationships**, not model responses. An LLM is one way of producing these objects; the objects, their evidence and their lineage are the product.

---

## 1. Canonical terminology

These names are canonical across code, API, database and docs. Do not introduce synonyms.

| Canonical name | Obsolete names (do not use) | Why this name |
|---|---|---|
| `SourceSpan` | `SourceLocation` | A span is an addressable piece of a specific source version (page, slide or section plus offsets and excerpt), not just a location. |
| `DependencyEdge` | `Dependency` | It is a directed, typed edge in the academic graph, with evidence. |
| `EvidenceLink` | `EvidenceClaim` | It *links* a claim to supporting spans; the claim itself is the domain object (deadline, obligation, mapping, ...). |
| `WorkflowRun` | `AgentRun`, `AIRun` | A run includes deterministic parsing, retrieval, validation and persistence as well as LLM inference, not only "AI" activity. |
| `Topic` | — | Needed for the Course Map. |
| `MaterialMapping` | — | Needed for requirement → teaching-material mapping. |

---

## 2. Provenance chain

```text
Consequential claim / domain object
  (Assessment deadline, AssessmentRequirement, Obligation,
   DependencyEdge, MaterialMapping, Conflict side, ChangeEvent impact)
        │
        v
  EvidenceLink          — which span(s) support this claim, and how it was verified
        │
        v
  SourceSpan            — page / slide / section, offsets, verbatim excerpt
        │
        v
  ResourceVersion       — immutable content snapshot identified by content hash
        │
        v
  Resource              — lecture, brief, handbook, announcement, page, ...
        │
        v
  Course  →  Institution
```

**Invariant:** If any link in this chain is missing or invalid, the claim **cannot be shown as verified**. It must be marked `needs_review` or rejected. Evidence locations are never invented by the model. They must refer to `SourceSpan`s that ingestion created.

---

## 3. Entities

### Source layer

| Entity | Meaning | Minimum fields |
|---|---|---|
| `Institution` | A university and its LMS connection. | id, name, lms_type |
| `Course` | One module/course, independent of LMS payload formats. | id, institution_id, title, external_id, status |
| `Topic` | A reconstructed learning topic grouping original materials (Course Map). | id, course_id, title, ordinal |
| `Resource` | An original item: lecture, lab, reading, workshop, handbook, brief, announcement, page. | id, course_id, type, title, source_uri, current_version_id |
| `ResourceVersion` | An immutable snapshot of a resource's content. Enables change detection and audit. | id, resource_id, content_hash, created_at, raw_text_ref |
| `SourceSpan` | An atomic, addressable piece of a specific version. | id, resource_version_id, location (page_number / slide_number / section_path / timestamp_seconds, at least one required), start_offset + end_offset (both or neither, `0 <= start < end`, positions in the extracted text), excerpt (required, non-empty) |

### Academic layer

| Entity | Meaning | Minimum fields |
|---|---|---|
| `Assessment` | An assessed deliverable or milestone. | id, course_id, title, deadline (nullable, may be in conflict), weight, status |
| `AssessmentRequirement` | One criterion or action from an assessment brief or rubric. | id, assessment_id, text, ordinal |
| `Obligation` | A required or recommended academic action outside or alongside an assessment (for example supervisor review, ethics approval). | id, course_id, title, mandatory, status |
| `DependencyEdge` | A directed ordering or support relationship. | id, from_ref, relation (`BEFORE`, `REQUIRED_FOR`, `SUPPORTED_BY`), to_ref |
| `MaterialMapping` | Requirement → teaching material, from retrieval plus model verification. | id, requirement_id, resource_id, source_span_id, retrieval_score, verification_status |

### Trust layer

| Entity | Meaning | Minimum fields |
|---|---|---|
| `EvidenceLink` | Connects a claim/object to supporting `SourceSpan`(s). | id, subject_type, subject_id, source_span_id, verification_status, workflow_run_id |
| `Conflict` | Two or more supported claims that disagree on a consequential fact. | id, conflict_type / field, subject_ref, claim_a + evidence, claim_b + evidence, status |
| `ChangeEvent` | A detected change in source material and its downstream impact. | id, resource_id, old_version_id, new_version_id, affected_objects, detected_at |

### Student layer

| Entity | Meaning | Minimum fields |
|---|---|---|
| `StudyState` | The student's explicit study state for an object. | student_id, object_ref, state (`NOT_STARTED`, `STUDYING`, `STUDIED`) |
| `StudyTask` | An actionable plan item with an explanation. | id, student_id, object_ref, action, reason_why_now, priority, estimated_minutes, due_at |

### Operational layer

| Entity | Meaning | Minimum fields |
|---|---|---|
| `WorkflowRun` | One execution of an import/analysis/change workflow, including deterministic and LLM steps. | id, workflow_version, course_id, input resource_version_ids, status, started_at, finished_at |

Per-step and per-model-call records (step name, model, prompt version, tokens, latency, retries, error class) belong to a `WorkflowRun`. Their exact shape is defined when the provider integration and observability are implemented.

---

## 4. Shared enums and states

| Enum | Values | Notes |
|---|---|---|
| `verification_status` | `verified`, `needs_review`, `rejected` | Only `verified` claims are presented as fact. |
| `StudyState.state` | `NOT_STARTED`, `STUDYING`, `STUDIED` | Opening a document never changes state automatically. |
| `DependencyEdge.relation` | `BEFORE`, `REQUIRED_FOR`, `SUPPORTED_BY` | Extended only via a documented contract change. |
| `Conflict.status` | `open`, `acknowledged`, `resolved_by_source` | "Resolved" only when a newer authoritative source settles it, never by silent choice. |

## 5. Evidence acceptance rules (per claim class)

| Claim class | Minimum acceptance condition |
|---|---|
| Deadline | Span contains a parsable date/time linked to the assessment or event. |
| Obligation | Span contains an explicit or strongly supported action directed at the student. |
| DependencyEdge | Evidence supports ordering or prerequisite meaning, not just topical closeness. |
| MaterialMapping | The material genuinely teaches or explains the requirement; the span identifies where. |
| ChangeEvent impact | The changed version can be traced to the affected object(s). |

## 6. Stability rules

The following must stay stable after v0.1.0. Changes need a migration, an explanation, and an ADR if architectural:

- domain object names and meanings in this document;
- the `SourceSpan` provenance contract and the chain in §2;
- the `LMSConnector` and `AIProvider` interfaces;
- `WorkflowRun` lineage and the `ChangeEvent` lineage model;
- the evaluation dataset format and metric definitions.
