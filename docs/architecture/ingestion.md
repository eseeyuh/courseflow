# Document ingestion

Ingestion turns an original course file into an immutable, hashed version and a list of **structural source spans**, each with an exact location. It is deterministic and never calls a model. The reasoning behind the design is in [ADR-006](../decisions/ADR-006-document-ingestion-and-raw-storage.md); the stored fields and their rules are in the [data contract](data-contract.md).

## Flow

```text
file bytes ─► RawSource (bytes, bare file name, logical source_uri)
   │
   ├─ course exists?
   ├─ content_hash = SHA-256(bytes)
   ├─ identical to the resource's current version?
   │     yes ─► no parse, no new rows; verify / restore the stored original ─► "unchanged"
   │
   ├─ guards: size ─► extension ─► DOCX/PPTX package checks
   ├─ parser for the media type ─► ordered blocks with locators (normalised per block)
   ├─ empty? ─► empty_text      ─ extracted text over the limit? ─► limit_exceeded
   ├─ check every block: extracted_text[start:end] == block text
   ├─ raw store: put(bytes) ─► sha256:<hex>
   └─ one SAVEPOINT:
        lock or create Resource(course_id, source_uri)
        re-check "identical to current" under the lock
        ResourceVersion(version_number = max + 1, hashes, extracted_text, metadata)
        SourceSpan × N (ordinal, block_kind, locator, offsets, excerpt)
        Resource.current_version_id ─► the new version
```

Code: `backend/app/ingestion/`: `dispatch.py` (parse), `parsers/`, `guards.py`, `normalize.py`, `storage.py`, `service.py` (persist), `cli.py`.

## Locators per format

| Format | One span per | Locator | Notes |
|---|---|---|---|
| PDF | physical page with a text layer | `page_number` (1-based, physical) | No OCR. Pages without text produce no span and are counted in the logs. |
| DOCX | body paragraph or table (including those inside content controls) | `section_path`: heading chain, e.g. `Coursework Brief > Submission` | Headings are "Title" and "Heading 1–9" styles; text before the first heading is `(preamble)`. Paragraph text includes tracked insertions, hyperlinks, smart tags and field results; tracked deletions and moved-away text are left out. Merged cells once; nested tables inside their cell. |
| PPTX | text-bearing shape; speaker notes separately (`slide_notes`) | `slide_number` (position in the deck, hidden slides included) | Group shapes read recursively; tables included. |
| HTML | block element (`p`, `li`, `div`, …); a table is one span | `section_path` from `h1`–`h6` | Browser whitespace rules; `<br>` is a line break. Pages nested deeper than 256 elements are rejected. |

**Not extracted in v0.1:** DOCX headers and footers, footnotes, comments and text boxes; PPTX charts, SmartArt, images and alt text; HTML `<head>` and any `<title>`, scripts, styles, `noscript`, frames and embedded objects, SVG/MathML, comments, elements with the `hidden` attribute (including table rows, row groups and captions), and image alt text.

## Rejections

A rejected file is detected before anything is written, so it writes nothing. Each rejection has exactly one category:

| Category | Meaning | Examples |
|---|---|---|
| `unsupported_type` | An unsupported file format, container variant, or intentionally unsupported subtype (not only an unsupported extension) | `.exe`, `.doc`, `.ppt`, `.docm`/`.pptm`, a macro project inside a `.docx`, an encrypted or legacy Office container named `.docx`/`.pptx` |
| `corrupt_file` | Supported format, but the bytes are malformed or unreadable as that format (v0.1 also reports a password-protected PDF here) | truncated PDF, password-protected PDF, broken XML, a ZIP member that lies about its size, non-UTF-8 HTML whose declared charset is missing or not a standard web encoding |
| `empty_text` | Parsed, but no usable text | a scanned PDF (`no text layer; OCR unsupported in v0.1`), a blank document |
| `limit_exceeded` | A safety limit was hit | file size, ZIP member count, uncompressed size and compression ratio (checked before parsing); PDF pages (once PDFium has opened the file); HTML nesting depth; extracted characters (after parsing) |
| `parser_failure` | Unexpected error in a supported file | detail is only the exception type |

Limits are configurable through the `INGEST_*` settings, but `docker-compose.yml` does not pass them to the container yet, so container imports use the defaults. The ZIP checks are bounded defensive checks against expected zip-bomb and resource-exhaustion cases, not a guarantee against every malformed archive.

## Trust boundary

- File content is **data**. Text such as "ignore previous instructions" is stored verbatim and never interpreted.
- Nothing in a document is executed or fetched: no macros, scripts or external resources.
- The user's file name is used for display, type detection and, by default, the logical `source_uri` and title. It is never used as a file system path; storage paths are derived only from validated hashes.
- Logs carry IDs, hashes and counts. They never contain document text, file names, source URIs or local paths. Database errors never include statement parameters.

## Raw store

Original bytes live in a content-addressed store: `<root>/sha256/<hex[0:2]>/<hex[2:4]>/<hex>`, referenced as `sha256:<hex>`.

- **Writes:** temp file in the destination directory → fsync → `os.replace`. Readers never see a partial object. fsync improves durability after a crash as far as the platform and file system honour it; it is not an absolute guarantee.
- **Existing object:** matching hash → nothing to do. Readable but wrong hash → replaced from the incoming bytes (which match the object's name), then logged as `raw_store.object_repaired`. Unreadable (permissions, I/O error) → an error; it is never overwritten. When an unchanged re-import finds the original of a committed version missing, it restores it and logs `raw_store.object_restored`.
- **Errors** name the operating-system cause (for example `ENOSPC`, `EACCES`) but never a path.
- **Reads** re-hash the bytes. Missing → `RawObjectNotFoundError`; wrong hash → `RawObjectIntegrityError`.
- **Not transactional with the database.** If a file's rows are rolled back after its bytes were stored, the object remains unreferenced. That is harmless (same content, same name, reused by a retry); a cleanup job can remove such objects later.

### Where the store is in development

There is **one physical store for one database**: the Docker named volume `rawdata`, mounted at `/var/lib/courseflow/raw` in the `api` service only, with `RAW_STORAGE_DIR` set there. The container runs as the non-root user `courseflow` (uid 10001). The image creates the mount point owned by that user, and Docker copies that ownership into the new volume.

The host `.env` leaves `RAW_STORAGE_DIR` empty. Host-side ingestion against the shared development database therefore fails with a configuration error instead of writing originals to a second, invisible store. Tests use their own temporary store and the separate test database.

## Commands

Run from the repository root with the stack up. `--no-deps` skips starting `db` and re-running the one-shot `migrate` job.

```bash
# Import one file; the folder is mounted read-only at /input
docker compose run --rm --no-deps -v "$PWD/backend/tests/fixtures/ingestion:/input:ro" api \
  python -m app.ingestion.cli import /input/handbook.pdf --course-id <course-uuid> --type handbook

# Re-check a stored version end to end: original bytes, text hash, every span's offsets
docker compose run --rm --no-deps api python -m app.ingestion.cli verify <version-uuid>

# Prove the container user can create, read and atomically replace objects in the volume,
# and that another container sees them
docker compose run --rm --no-deps api python -m app.ingestion.cli check-store write
docker compose run --rm --no-deps api python -m app.ingestion.cli check-store read
```

`import` prints one JSON object on stdout: status (`created`/`unchanged`), resource and version IDs, version number, the resource's stored type, hashes, raw object reference, media type, size, span counts by kind and, for PDFs, total and empty page counts. Every failure is also one JSON object (`status: error` or `rejected`), never a traceback. Logs go to stderr as JSON lines.

| Exit code | Meaning |
|---|---|
| `0` | ok |
| `1` | unexpected error (`error: unexpected`, with only the exception type), a retryable concurrent-import conflict, or invalid configuration |
| `2` | raw storage not configured or not usable |
| `3` | file, course or version not found, or the file is not a regular, readable file |
| `4` | input rejected: `category`, or `error: invalid_source` for an invalid file name or `--source-uri` |
| `5` | verification failed |
| `64` | command-line usage error |

Optional flags: `--source-uri` sets the logical identity (default `upload:<file name>`, so pass it to keep the same resource when a file is renamed), and `--title` sets the title of a newly created resource. `--type` also applies only when the resource is created; a re-import never changes the stored type.
