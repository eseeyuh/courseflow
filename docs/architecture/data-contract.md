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

### Retrieval layer

Retrieval units are not evidence units. A `Chunk` is what similarity search ranks; evidence still points at `SourceSpan`s, which every chunk resolves to exactly.

```text
SourceSpan (first..last ordinal) <- Chunk -> ChunkEmbedding -> Embedding -> EmbeddingSpace
```

| Entity | Meaning | Minimum fields |
|---|---|---|
| `Chunk` | An exact slice of one `ResourceVersion`'s `extracted_text`, produced by a versioned chunker. Its resource and course come only through that version. Immutable. | id, resource_version_id, chunker_version, ordinal (unique per version and chunker version), start_offset, end_offset, first_span_ordinal, last_span_ordinal, text, text_hash, estimated_tokens, text_tsv (generated `tsvector` of `text`, English configuration; derived, never written) |
| `EmbeddingSpace` | The unit of vector comparability. `model` is the configured model ID; `space_version` is CourseFlow's own compatibility revision, not an upstream model version. | id, provider, model, dimensions (1–16,000), space_version (unique together) |
| `Embedding` | One vector, global and content-addressed: shared by every input that hashed identically in the same space and input format. | id, space_id, dimensions (equal to the space's), input_format (`document/vN` or `query/vN`), input_hash (SHA-256 of the exact UTF-8 provider input), vector (non-zero), model_call_id (nullable) |
| `ChunkEmbedding` | The explicit link from a chunk to its document-format vector in one space. | chunk_id, embedding_id, space_id, input_format (`document/vN` only); at most one per (chunk, space, input format) |

Rules:

- **Chunkable source:** span ordinals are exactly 0..n-1 in source order and every span has offsets inside the text (ingestion always writes them so); a version that breaks this cannot be chunked and is never searched.
- **Chunk provenance:** `text == extracted_text[start_offset:end_offset]`; the start lies inside span `first_span_ordinal` and the end inside span `last_span_ordinal`, both spans of the chunk's own version, with `first_span_ordinal <= last_span_ordinal`. A chunk that splits one oversized span has `first_span_ordinal == last_span_ordinal`. The chunk's spans are those with ordinals between the two. These rules are checked by the application before writing and again, for every row, by the database.
- **Coverage:** for each indexed version and chunker version, every `SourceSpan` that contains visible (non-whitespace) text lies within at least one chunk's span range, and every non-whitespace character of `extracted_text` lies within at least one chunk. A whitespace-only span gets no retrieval unit of its own; any text not covered by a chunk is whitespace only.
- **Vectors** are compared only within one `EmbeddingSpace`. The relevance score is `1 - cosine distance`; it ranks results and is never a confidence.
- **Query embeddings** (`query/vN`) are never linked to chunks.
- **Search** covers only the current version of each resource, and is scoped to a course through `Chunk -> ResourceVersion -> Resource -> Course`. This holds in every search mode (vector, lexical, hybrid), and results are ordered by score and then by natural identity (source URI, version number, chunk ordinal), never by a generated ID.
- **Deletion:** deleting a version's spans or the version deletes its chunks and their links; an embedding in use cannot be deleted; pruning a `ModelCall` keeps the vectors it produced.

### Academic layer

| Entity | Meaning | Minimum fields |
|---|---|---|
| `Assessment` | An assessed deliverable or milestone, once per course: sources that state it add evidence, not rows. `status` is derived: `verified` only with a verified deadline or a verified weighting; a candidate with title evidence alone is `needs_review`. | id, course_id, canonical_key (deterministic normal form of the title; unique per course), title, weighting_percent, status |
| `AssessmentDeadline` | One deadline value a source states for an assessment. Disagreeing sources give one row each (the disagreement is kept); agreeing sources share a row. | id, assessment_id, due_date (local), due_time (local, nullable), timezone (IANA, nullable), timezone_source, precision, due_at (instant, nullable), status |
| `AssessmentRequirement` | Something a student must satisfy in an assessed deliverable, once per assessment. `text` is the source statement verbatim (whitespace normalised), never a model paraphrase; one statement is one requirement. Order comes from its evidence (span and quote position), not a stored ordinal. | id, assessment_id, canonical_key (SHA-256 of the statement's normal form; unique per assessment), text, status |
| `Obligation` | An academic action a student must or should perform, possibly outside any deliverable (for example a supervisor review or an ethics form), once per course. `canonical_action` and `category` are the proposing model's words (`model_call_id`); `modality` and `status` come from the evidence gate, and a verified obligation always has a modality. An explicit due date is typed like an `AssessmentDeadline`; a relative lead time is not a due date. A related assessment must be of the same course. | id, course_id, assessment_id (nullable), canonical_key (SHA-256 of the anchor statement's normal form; unique per course), canonical_action, category, modality (nullable), due_date / due_time / due_timezone / due_timezone_source / due_precision / due_at (all nullable), status, model_call_id |
| `DependencyEdge` | A directed ordering or support relationship. | id, from_ref, relation (`BEFORE`, `REQUIRED_FOR`, `SUPPORTED_BY`), to_ref |
| `MaterialMapping` | Requirement → teaching material, from retrieval plus model verification. | id, requirement_id, resource_id, source_span_id, retrieval_score, verification_status |

### Trust layer

| Entity | Meaning | Minimum fields |
|---|---|---|
| `EvidenceLink` | Connects one fact of a claim/object to a supporting `SourceSpan`. | id, subject_type plus one typed subject reference (assessment_id, assessment_deadline_id, assessment_requirement_id or obligation_id, exactly one), field (`title` and `weighting` for an assessment, `deadline` for a deadline, `requirement` for a requirement, `obligation` and `due_date` for an obligation), source_span_id, quote_start / quote_end (the supporting excerpt inside the span), verification_status, model_call_id, workflow_run_id |
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
| `WorkflowRun` | One execution of an import/analysis/change workflow, including deterministic and LLM steps. | id, workflow_version (`<kind>/v<N>`, e.g. `index/v1`), course_id, input resource_version_ids, status, started_at, finished_at, summary (a JSON object of final counts whose shape is fixed by workflow_version; never document text) |
| `ModelCall` | One logical model call: its final outcome after retries and repair turns. | id, workflow_run_id (nullable: standalone calls such as evaluations and search queries), call_kind (`chat_structured`, `embedding`), provider, model, model_role, prompt_version, output_schema, status, error_category, attempts, repair_rounds, token counts, latency_ms, started_at, finished_at |

`call_kind` says what operation happened; `model_role` says why a generative model was chosen (`fast` or `strong`). Embedding calls have `call_kind = embedding`, no `model_role` and no repair rounds. Their `prompt_version` is the input format (`document/v1`, `query/v1`) and their `output_schema` is `vector[<dimensions>]`.

A `ModelCall` belongs to a `WorkflowRun` when made inside one. Per-attempt detail (each HTTP request, its latency and error) is in structured logs, not separate rows. A `ModelCall` never stores secrets or prompts; its output summary is the *validated* output only. See [ADR-005](../decisions/ADR-005-ai-provider-boundary.md). Per-step records (step name, node) are defined when workflow orchestration is implemented.

---

## 4. Shared enums and states

| Enum | Values | Notes |
|---|---|---|
| `ModelCall.status` / `error_category` | `succeeded`, `failed` / `timeout`, `connection`, `rate_limited`, `server_error`, `authentication`, `invalid_request`, `truncated`, `refused`, `invalid_output`, `unexpected` | The first four categories are transient and retried; the rest fail fast. |
| `ResourceVersion.media_type` | `application/pdf`, `application/vnd.openxmlformats-officedocument.wordprocessingml.document`, `application/vnd.openxmlformats-officedocument.presentationml.presentation`, `text/html` | The format of this version; a resource may change format between versions. |
| `SourceSpan.block_kind` | `page`, `heading`, `paragraph`, `table`, `slide_text`, `slide_notes` | Structural role; determines the canonical (required) locator. |
| Ingestion error category | `unsupported_type`, `corrupt_file`, `empty_text`, `limit_exceeded`, `parser_failure` | Why one file was not ingested. A failed file creates no rows. |
| `verification_status` | `verified`, `needs_review`, `rejected` | Only `verified` claims are presented as fact. A quote that occurs more than once in its span has no single location: every occurrence is linked, exactly, as `needs_review`. |
| `AssessmentDeadline.precision` | `datetime`, `date` | `date`: no time of day was stated; none is invented and there is no `due_at`. A month, week or term is not a deadline: no row. |
| `AssessmentDeadline.timezone_source` | `explicit_source`, `institution_default`, `unknown` | `explicit_source`: the cited quote names the timezone. `institution_default`: it names none, so the institution's own was applied (the quote is not claimed to state it). `unknown`: neither; no instant, and a local time cannot be verified. |
| `review_reason` (Assessment, AssessmentDeadline, AssessmentRequirement, Obligation, EvidenceLink) | `no_verified_assessment_fact`, `conflicting_weighting`, `non_unique_quote_location`, `missing_timezone`, `unrecognised_timezone`, `conflicting_timezone`, `weekday_mismatch`, `ambiguous_time`, `ambiguous_local_time`, `permissive_wording`, `mixed_modality`, `no_requirement_language`, `assessment_link_unsupported`, `category_unsupported`, `conflicting_category`, `conflicting_modality`, `conflicting_assessment_link`, `conflicting_due_date` | Why a row needs review: derived by the evidence gate, never a model confidence. Present exactly when the status is `needs_review`. |
| `Obligation.category` | `supervisor_review`, `approval`, `ethics_admin_form`, `formative_prerequisite`, `submission_prerequisite`, `other` | Deliberately narrow: anything else is `other`, never a new category. A model classification, not a verified fact. |
| `Obligation.modality` | `mandatory`, `recommended` | From the evidence wording (`must`, `required` / `should`, `recommended`). Permissive wording (`may`) is neither: no modality, so never verified. |
| Gate rejection reason (run summaries only) | `span_unknown`, `quote_not_found`, `quote_overlaps_injection`, `title_unsupported`, `weighting_unsupported`, `schema_violation`, `model_date_mismatch`, `model_time_mismatch`, `model_time_invalid`, `month_only`, `no_date`, `numeric_date`, `multiple_dates`, `impossible_date`, `impossible_time`, `multiple_times`, `local_time_nonexistent`, `non_deadline_date`, `no_deadline_semantics`, `identity_unsupported`, `assessment_unknown`, `no_requirement_language`, `statement_too_long`, `no_obligation_wording`, `no_evidence` | A rejected fact is never stored, so it can never be shown as fact. Counted per task as `<fact>:<reason>`. |
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

**Quote location.** Evidence is always the span's original text between exact code-point offsets, never the model's quote. Assessment facts (`assessment-gate/v3`) need an exact substring. Requirements and obligations (`claim-gate/v2`) also accept a quote whose whitespace differs: any run of whitespace in the quote matches any run of whitespace in the span (a wrapped PDF line), while case, punctuation, hyphens and characters must match exactly. Every distinct match is a location, so a quote that matches more than once is `needs_review` (`non_unique_quote_location`). Injection overlap is checked on the resolved original offsets.

**One sentence, one requirement.** A requirement is the one source sentence its quote is evidence for. A quote merged with the lead-in above its list item starts at the item, and each location is cut to that sentence, so merged quotation text never creates a merged requirement or supports a neighbouring one. A list item's requirement wording may come from the lead-in the source structure ties to it (the line introducing the list in the same span, or the section's heading span), derived deterministically, never from the model.

## 6. Stability rules

The following must stay stable after v0.1.0. Changes need a migration, an explanation, and an ADR if architectural:

- domain object names and meanings in this document;
- the `SourceSpan` provenance contract and the chain in §2;
- the `LMSConnector` and `AIProvider` interfaces;
- `WorkflowRun` lineage and the `ChangeEvent` lineage model;
- the evaluation dataset format and metric definitions.
