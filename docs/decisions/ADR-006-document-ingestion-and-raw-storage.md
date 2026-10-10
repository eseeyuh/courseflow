# ADR-006 — Deterministic document ingestion with a content-addressed raw store

- **Status:** Accepted
- **Date:** 2026-10-10

## Context

Every consequential claim CourseFlow shows must link to the exact page, slide or section it came from ([data contract](../architecture/data-contract.md)). That chain starts at ingestion: if the original file and the location of each piece of text are not captured faithfully, nothing later can be verified.

Ingestion has to:

- read PDF, DOCX, PPTX and simple HTML/Moodle-like pages ([scope](../SCOPE.md));
- keep the source's own boundaries (physical page, slide, heading path) instead of flattening everything into one text;
- identify a version by its content, so re-importing the same file is a no-op and a changed file is a new, immutable version;
- keep the original bytes, separately from the extracted text, so a better parser can re-read them and a user can see the original;
- treat every uploaded file as untrusted input.

The repository is public under MPL-2.0 and the application will be deployed as a network service, so library licences matter.

## Decision

1. **Deterministic, AI-free parsing.** Ingestion never calls a model. A test enforces that no ingestion module imports AI or HTTP-client code. Document text is data and is stored verbatim; normalisation changes only representation: line endings, invisible control characters, NFC (not NFKC), runs of spaces and tabs, spaces at line edges, and runs of blank lines.
2. **Permissively licensed parsers:** `pypdfium2` (PDFium; BSD-3/Apache-2.0) for PDF, `python-docx` and `python-pptx` (MIT) for Office files, `beautifulsoup4` (MIT) with the standard-library `html.parser` named explicitly for HTML. **PyMuPDF is not used:** it is licensed AGPL-3.0 or commercially, and including it would subject the deployed service to AGPL-3.0 network-source obligations (or require a commercial licence), contrary to the project's MPL-2.0 licensing.
3. **Structural spans.** Each parser emits blocks: one per PDF page, slide shape or speaker notes, DOCX paragraph or table, HTML block element or table. Each block carries its locator. The blocks become `SourceSpan`s with code-point offsets into the version's `extracted_text`. Evidence that needs a narrower range references a span plus sub-offsets; it does not change what a span is.
4. **Content identity.** `content_hash` is the SHA-256 of the original bytes and decides whether an import needs a new version, by comparison with the resource's *current* version; `text_hash` covers the extracted text. A version row itself is identified by `id` and `(resource_id, version_number)`. Bytes identical to the current version create nothing for the same `source_uri`, even if the file name (`display_name`) changed. Changed bytes create the next version; content that reverts (A → B → A) is a new version. A resource is identified by `(course_id, source_uri)`, where `source_uri` is a logical URI, never a local path. The CLI derives the default `source_uri` from the file name, so `--source-uri` keeps identity across renames.
5. **Content-addressed raw store** behind a `RawObjectStore` interface. References are `sha256:<hex>` and name the bytes, not a location. The local implementation stores objects at `sha256/<2>/<2>/<hex>` using a temp file plus atomic rename; it verifies hashes on read and repairs a corrupted object only from bytes that match its name.
6. **One physical store per database.** In development the store is the Docker named volume `rawdata`, mounted only into the backend container, which runs as a non-root user. Imports run inside that container. The host `.env` leaves `RAW_STORAGE_DIR` empty, so host-side ingestion against the shared database fails instead of creating a second store behind the same references.
7. **Failure isolation.** A rejected file (an input problem) is detected before anything is written; in a batch it is recorded and the batch continues. Each file's rows are written in one database savepoint, so an error while writing leaves no partial rows for that file; any non-input error stops the batch and the caller rolls back. Raw-store writes are not transactional with PostgreSQL; an object written before a rolled-back savepoint stays behind, unreferenced and harmless, until a later cleanup.

## Alternatives considered

| Alternative | Why not (for this version) |
|---|---|
| PyMuPDF for PDF | AGPL-3.0 or commercial licence; AGPL network-source obligations would apply to the deployed service. |
| `pypdf` / `pdfminer.six` for PDF | Also permissive. PDFium was preferred as a mature, widely deployed engine (Chrome's PDF engine); the libraries were not benchmarked against each other. Either can replace it behind the parser registry if the evaluation set shows extraction problems. |
| An LLM or document-AI service to extract text and locations | Not deterministic; can invent locations; sends documents to a third party. Locations must come from the source structure. |
| Unstructured / Docling-style toolkits | Heavier dependency trees (some with ML models) for a problem four focused parsers solve with exact control over locators. |
| Hash the extracted text as the version identity | A parser change would look like a source change, and two different files with the same text would collapse into one version. |
| Store original bytes in PostgreSQL (`bytea`) | Transactional with the rows, but mixes blobs into the relational database and its backups; the store interface keeps that option open. |
| Object storage (S3/MinIO) now | A new service and SDK for a single-node demo; `sha256:` references let it replace the local store later without data changes. |
| Host folder bind-mounted into the container | The same location configured twice (host and container paths), easy to mismatch silently; file ownership problems on Linux hosts. |

## Consequences

**Positive**
- Every span can be traced to a version, its original bytes and an exact location, and this is checkable: `python -m app.ingestion.cli verify <version-id>` re-hashes the original and re-slices every span.
- Re-import is idempotent, and ingestion never updates or deletes versions or spans, which is the basis for change detection.
- Parser output is reproducible and covered by golden tests with exact locators.
- No AGPL code in the application.

**Negative / risks**
- No OCR: pages without a text layer produce no span. Partly scanned PDFs are logged (`empty_page_count`); fully scanned ones are rejected as `empty_text`.
- v0.1 extraction is deliberately narrow: DOCX headers, footers, footnotes, comments and text boxes; PPTX charts, SmartArt and alt text; and HTML image alt text are not extracted. (DOCX tracked insertions, fields, smart tags and content controls are extracted; tracked deletions are not.) Heading detection in DOCX relies on built-in style names.
- PDFium is native code: a hostile file could crash the process. Only the size limit applies before PDFium reads the bytes (the page limit is checked once the document is open); process isolation is left to the threat model.
- Unreferenced raw objects can accumulate after failed imports until a cleanup job exists.
- Development imports must go through the container (`docker compose run`), which adds container start-up to each command.

## Revisit when

- Real course material needs OCR or text the v0.1 parsers skip (measured by evidence-location misses in the evaluation set).
- The application runs on more than one node, or storage outgrows a single volume: move to object storage behind the same interface.
- Parser crashes or timeouts are observed on real inputs: isolate parsing in a subprocess with time limits.
- Unreferenced objects become a measurable share of the store: add the cleanup job.
