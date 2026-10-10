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
| `ModelCall` | `AIRun`, `LLMCall` | One model invocation (a step inside a run, or standalone); not a run. |
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
| `Resource` | An original item: lecture, lab, reading, workshop, handbook, brief, announcement, page. | id, course_id, type, title, source_uri (required, unique per course), current_version_id |
| `ResourceVersion` | An immutable snapshot of a resource's content. Enables change detection and audit. | id, resource_id, version_number, content_hash, raw_object_ref, media_type, byte_size, display_name, parser_name, parser_version, extracted_text, text_hash, created_at |
| `SourceSpan` | A structural block of a specific version, created by ingestion (a page, heading, paragraph, table, slide text or speaker notes). | id, resource_version_id, ordinal (0-based, unique per version), block_kind, location (page_number / slide_number / section_path / timestamp_seconds, at least one required, including the canonical locator of its block_kind), start_offset + end_offset (both or neither, `0 <= start < end`, `end - start = length(excerpt)`), excerpt (required, non-empty) |

### Source identity, versions and spans

- **`Resource.source_uri`** is the resource's logical identity inside its course, for example `upload:brief.pdf` or `demo://northbridge/ds101/brief`. It is never a local file system path. Importing the same `source_uri` again adds a version to the same resource.
- **`content_hash`** is the SHA-256 (lowercase hex) of the original bytes. It identifies the *content* and decides whether an import needs a new version; a version row is identified by `id` and `(resource_id, version_number)`, and the same hash can appear in several versions. The bytes are the source of truth: a parser upgrade must not look like a source change. Importing bytes identical to the current version of the same `source_uri` creates nothing, even if the file name changed; `display_name` is recorded per version for display only and never creates a version by itself. Changed bytes create the next version; content that reverts to an earlier state (A → B → A) is a new version.
- **`raw_object_ref`** is always `sha256:` + `content_hash`. It names the bytes, not where they are stored, so a storage backend can change without changing references.
- **`extracted_text`** is the normalised text of the whole version: the span excerpts in `ordinal` order, joined by one blank line (`"\n\n"`). **`text_hash`** is the SHA-256 of its UTF-8 bytes; the database verifies it. It lets change analysis tell a re-saved file (new `content_hash`, same `text_hash`) from changed content.
- **Offsets** are Unicode code-point positions in `extracted_text`; for every ingestion span, `extracted_text[start_offset:end_offset] == excerpt`. Clients that use UTF-16 (JavaScript) must convert.
- **Locators:** `page_number` is the physical 1-based page; `slide_number` the 1-based position in the presentation (hidden slides included); `section_path` is the heading chain joined with ` > `, or `(preamble)` for text before the first heading. Each block kind requires its canonical locator: `page` requires `page_number`; `heading`, `paragraph` and `table` require `section_path`; `slide_text` and `slide_notes` require `slide_number`. Additional locator fields are permitted by the provenance contract, though the v0.1 parsers normally emit only the locator relevant to the format. `timestamp_seconds` is reserved for media sources, which v0.1 does not ingest.
- **Evidence finer than a span** references a `SourceSpan` plus sub-offsets in the evidence layer. It does not create a different kind of `SourceSpan`.

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
| `ModelCall` | One logical model call: its final outcome after retries and repair turns. | id, workflow_run_id (nullable: standalone calls such as evaluations), provider, model, model_role, prompt_version, output_schema, status, error_category, attempts, repair_rounds, token counts, latency_ms, started_at, finished_at |

A `ModelCall` belongs to a `WorkflowRun` when made inside one. Per-attempt detail (each HTTP request, its latency and error) is in structured logs, not separate rows. A `ModelCall` never stores secrets or prompts; its output summary is the *validated* output only. See [ADR-005](../decisions/ADR-005-ai-provider-boundary.md). Per-step records (step name, node) are defined when workflow orchestration is implemented.

---

## 4. Shared enums and states

| Enum | Values | Notes |
|---|---|---|
| `ModelCall.status` / `error_category` | `succeeded`, `failed` / `timeout`, `connection`, `rate_limited`, `server_error`, `authentication`, `invalid_request`, `truncated`, `refused`, `invalid_output`, `unexpected` | The first four categories are transient and retried; the rest fail fast. |
| `ResourceVersion.media_type` | `application/pdf`, `application/vnd.openxmlformats-officedocument.wordprocessingml.document`, `application/vnd.openxmlformats-officedocument.presentationml.presentation`, `text/html` | The format of this version; a resource may change format between versions. |
| `SourceSpan.block_kind` | `page`, `heading`, `paragraph`, `table`, `slide_text`, `slide_notes` | Structural role; determines the canonical (required) locator. |
| Ingestion error category | `unsupported_type`, `corrupt_file`, `empty_text`, `limit_exceeded`, `parser_failure` | Why one file was not ingested. A failed file creates no rows. |
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
