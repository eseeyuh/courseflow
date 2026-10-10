# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Persist one source as Resource -> ResourceVersion -> SourceSpans.

The caller owns the database transaction and decides to commit or roll
back. A rejected file (an input problem) is detected before anything is
written. Each file's rows are written inside one SAVEPOINT, so an error
while writing leaves no partial rows for that file. In a batch, rejected
files are recorded and the batch continues; any other error stops the batch
and the caller rolls back the whole transaction.

Guarantees:
- A failed import leaves no partial database rows.
- Raw-store writes are not part of the database transaction. If the rows
  are rolled back after the original bytes were stored, the stored object
  stays behind, unreferenced. It is harmless (content-addressed, reused by
  a retry); cleanup is deferred.
- Bytes identical to the resource's current version are not parsed and
  create no version or spans; the stored object is verified, and restored
  or repaired if needed, before the import reports "unchanged".

Logs carry IDs, hashes and counts only: never document text, file names,
source URIs or paths.
"""

import asyncio
import logging
import time
import uuid
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Course, Resource, ResourceVersion, SourceSpan
from app.domain.enums import BlockKind, IngestionErrorCategory, MediaType, ResourceType
from app.ingestion.dispatch import parse_source
from app.ingestion.errors import IngestionError
from app.ingestion.guards import IngestionLimits
from app.ingestion.hashing import sha256_hex
from app.ingestion.schemas import ParsedDocument, RawSource
from app.ingestion.storage import RawObjectIntegrityError, RawObjectStore, object_ref

logger = logging.getLogger(__name__)


class CourseNotFoundError(LookupError):
    """The target course does not exist. Stops a batch: it applies to every file."""


class SpanInvariantError(RuntimeError):
    """A span's offsets do not reproduce its excerpt. A CourseFlow bug, never input."""


class ConcurrentImportError(RuntimeError):
    """Another import allocated the same version number. Not retried."""


@dataclass(frozen=True, slots=True)
class IngestResult:
    """Outcome of one import.

    ``created=False`` means no new version was required relative to the
    current version *this import observed*; the returned version is that
    one. Under concurrent writes it does not promise that this version is
    still the resource's current version when the call returns: an
    overlapping import may commit a newer version, and the two calls are
    then ordered with this one first.
    """

    resource_id: uuid.UUID
    resource_created: bool
    version_id: uuid.UUID
    version_number: int
    created: bool
    content_hash: str
    text_hash: str
    raw_object_ref: str
    media_type: MediaType
    byte_size: int
    span_count: int
    spans_by_kind: dict[BlockKind, int]
    # The resource's stored type: set on first import, never changed by a re-import.
    resource_type: ResourceType
    # PDF only, and only when the file was parsed: pages in the file, and
    # pages that produced no text (e.g. scanned pages).
    total_page_count: int | None = None
    empty_page_count: int | None = None


@dataclass(frozen=True, slots=True)
class IngestFailure:
    """One file rejected for an input problem; it wrote no rows.

    ``content_hash`` matches the ``ingestion.rejected`` log entry.
    """

    source_uri: str
    content_hash: str
    category: IngestionErrorCategory
    detail: str


@dataclass(frozen=True, slots=True)
class IngestItem:
    source: RawSource
    resource_type: ResourceType
    title: str | None = None


async def ingest_file(
    session: AsyncSession,
    store: RawObjectStore,
    *,
    course_id: uuid.UUID,
    resource_type: ResourceType,
    source: RawSource,
    limits: IngestionLimits,
    title: str | None = None,
) -> IngestResult:
    """Import one source into a course. See the module docstring for guarantees.

    Raises ``IngestionError`` for input problems (nothing written), and
    ``CourseNotFoundError``, ``RawStoreError``, database errors,
    ``SpanInvariantError`` or ``ConcurrentImportError`` otherwise.
    """
    started = time.monotonic()
    await _require_course(session, course_id)
    content_hash = sha256_hex(source.content)

    # Shortcut for unchanged files (no parse). Not locked: the authoritative
    # check is repeated under the resource row lock below.
    found = await _find_resource(session, course_id, source.source_uri)
    if found is not None and found[1] is not None and found[1].content_hash == content_hash:
        resource, current = found[0], found[1]
        _warn_on_type_mismatch(resource, resource_type)
        await _ensure_raw_object(store, source.content, current)
        return await _unchanged(session, resource, current, resource_created=False)

    try:
        document = await asyncio.to_thread(parse_source, source, limits)
    except IngestionError as exc:
        logger.warning(
            "ingestion.rejected",
            extra={
                "course_id": str(course_id),
                "category": exc.category.value,
                "detail": exc.detail,
                "content_hash": content_hash,
                "byte_size": len(source.content),
            },
        )
        raise

    span_rows = build_span_rows(document)  # validated before anything is written
    ref = await asyncio.to_thread(store.put, source.content)
    if ref != object_ref(document.content_hash):
        raise RawObjectIntegrityError("raw store returned a reference for different bytes")

    try:
        async with session.begin_nested():
            resource, resource_created = await _lock_or_create_resource(
                session, course_id, resource_type, source, title
            )
            current = await _current_version(session, resource)
            if current is not None and current.content_hash == content_hash:
                # Another import committed the same bytes first; the object was
                # verified by store.put above.
                return await _unchanged(session, resource, current, resource_created)
            _warn_on_type_mismatch(resource, resource_type)
            version = await _create_version(session, resource, source, document, ref, span_rows)
    except IntegrityError as exc:
        if "uq_resource_versions_number" in str(exc.orig):
            raise ConcurrentImportError("another import created this version number") from None
        raise

    result = IngestResult(
        resource_id=resource.id,
        resource_created=resource_created,
        version_id=version.id,
        version_number=version.version_number,
        created=True,
        content_hash=content_hash,
        text_hash=document.text_hash,
        raw_object_ref=ref,
        media_type=document.media_type,
        byte_size=document.byte_size,
        span_count=len(span_rows),
        spans_by_kind=dict(Counter(row["block_kind"] for row in span_rows)),
        resource_type=resource.type,
        total_page_count=document.page_count,
        empty_page_count=document.empty_page_count,
    )
    _log_created(result, document, duration_ms=round((time.monotonic() - started) * 1000))
    return result


async def ingest_files(
    session: AsyncSession,
    store: RawObjectStore,
    *,
    course_id: uuid.UUID,
    items: Sequence[IngestItem],
    limits: IngestionLimits,
) -> list[IngestResult | IngestFailure]:
    """Import several files into one course, each in its own SAVEPOINT.

    An input problem with one file is recorded as ``IngestFailure`` and the
    batch continues. Any other error stops the batch and propagates; the
    caller then rolls back its transaction.

    Files are processed in ``source_uri`` order, so concurrent batches take
    resource row locks in the same order and cannot deadlock each other.
    Outcomes are returned in input order.
    """
    outcomes: dict[int, IngestResult | IngestFailure] = {}
    for index in sorted(range(len(items)), key=lambda i: items[i].source.source_uri):
        item = items[index]
        try:
            outcomes[index] = await ingest_file(
                session,
                store,
                course_id=course_id,
                resource_type=item.resource_type,
                source=item.source,
                limits=limits,
                title=item.title,
            )
        except IngestionError as exc:
            outcomes[index] = IngestFailure(
                item.source.source_uri, sha256_hex(item.source.content), exc.category, exc.detail
            )
    return [outcomes[index] for index in range(len(items))]


def build_span_rows(document: ParsedDocument) -> list[dict[str, Any]]:
    """SourceSpan column values for each block, with the offset invariant checked."""
    text = document.extracted_text
    rows = []
    for ordinal, (block, (start, end)) in enumerate(
        zip(document.blocks, document.block_offsets(), strict=True)
    ):
        if text[start:end] != block.text:
            raise SpanInvariantError(f"span {ordinal}: offsets do not reproduce the excerpt")
        rows.append(
            {
                "ordinal": ordinal,
                "block_kind": block.kind,
                "page_number": block.locator.page_number,
                "slide_number": block.locator.slide_number,
                "section_path": block.locator.section_path,
                "start_offset": start,
                "end_offset": end,
                "excerpt": block.text,
            }
        )
    return rows


# --- steps ----------------------------------------------------------------------------


async def _require_course(session: AsyncSession, course_id: uuid.UUID) -> None:
    if await session.scalar(select(Course.id).where(Course.id == course_id)) is None:
        raise CourseNotFoundError(f"course {course_id} does not exist")


async def _find_resource(
    session: AsyncSession, course_id: uuid.UUID, source_uri: str
) -> tuple[Resource, ResourceVersion | None] | None:
    row = (
        await session.execute(
            select(Resource, ResourceVersion)
            .outerjoin(ResourceVersion, Resource.current_version_id == ResourceVersion.id)
            .where(Resource.course_id == course_id, Resource.source_uri == source_uri)
        )
    ).one_or_none()
    return None if row is None else (row[0], row[1])


async def _ensure_raw_object(
    store: RawObjectStore, content: bytes, current: ResourceVersion
) -> None:
    """Verify (and restore or repair) the stored original of an unchanged file."""
    was_missing = not await asyncio.to_thread(store.exists, current.raw_object_ref)
    ref = await asyncio.to_thread(store.put, content)
    if ref != current.raw_object_ref:
        raise RawObjectIntegrityError("stored reference does not match the current version")
    if was_missing:
        # A committed version's original had disappeared: worth an alert.
        logger.warning(
            "raw_store.object_restored",
            extra={"content_hash": current.content_hash, "version_id": str(current.id)},
        )


async def _lock_or_create_resource(
    session: AsyncSession,
    course_id: uuid.UUID,
    resource_type: ResourceType,
    source: RawSource,
    title: str | None,
) -> tuple[Resource, bool]:
    """The resource for (course_id, source_uri), created if needed, row-locked."""
    inserted = await session.scalar(
        insert(Resource)
        .values(
            id=uuid.uuid4(),
            course_id=course_id,
            type=resource_type,
            title=title or source.display_name,
            source_uri=source.source_uri,
        )
        .on_conflict_do_nothing(index_elements=["course_id", "source_uri"])
        .returning(Resource.id)
    )
    resource = await session.scalar(
        select(Resource)
        .where(Resource.course_id == course_id, Resource.source_uri == source.source_uri)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if resource is None:  # pragma: no cover - the row was inserted or already existed
        raise RuntimeError("resource row vanished while importing")
    return resource, inserted is not None


async def _current_version(session: AsyncSession, resource: Resource) -> ResourceVersion | None:
    if resource.current_version_id is None:
        return None
    return await session.scalar(
        select(ResourceVersion)
        .where(ResourceVersion.id == resource.current_version_id)
        .execution_options(populate_existing=True)
    )


async def _create_version(
    session: AsyncSession,
    resource: Resource,
    source: RawSource,
    document: ParsedDocument,
    ref: str,
    span_rows: list[dict[str, Any]],
) -> ResourceVersion:
    highest = await session.scalar(
        select(func.max(ResourceVersion.version_number)).where(
            ResourceVersion.resource_id == resource.id
        )
    )
    version = ResourceVersion(
        id=uuid.uuid4(),
        resource_id=resource.id,
        version_number=(highest or 0) + 1,
        content_hash=document.content_hash,
        raw_object_ref=ref,
        media_type=document.media_type,
        byte_size=document.byte_size,
        display_name=source.display_name,
        parser_name=document.parser_name,
        parser_version=document.parser_version,
        extracted_text=document.extracted_text,
        text_hash=document.text_hash,
    )
    session.add(version)
    session.add_all(SourceSpan(resource_version_id=version.id, **row) for row in span_rows)
    await session.flush()
    # Only after the version row exists: the current-version FK checks it.
    resource.current_version_id = version.id
    await session.flush()
    return version


async def _unchanged(
    session: AsyncSession, resource: Resource, current: ResourceVersion, resource_created: bool
) -> IngestResult:
    counts = (
        await session.execute(
            select(SourceSpan.block_kind, func.count())
            .where(SourceSpan.resource_version_id == current.id)
            .group_by(SourceSpan.block_kind)
        )
    ).all()
    spans_by_kind = {BlockKind(kind): count for kind, count in counts}
    logger.info(
        "ingestion.unchanged",
        extra={
            "resource_id": str(resource.id),
            "version_id": str(current.id),
            "version_number": current.version_number,
            "content_hash": current.content_hash,
        },
    )
    return IngestResult(
        resource_id=resource.id,
        resource_created=resource_created,
        version_id=current.id,
        version_number=current.version_number,
        created=False,
        content_hash=current.content_hash,
        text_hash=current.text_hash,
        raw_object_ref=current.raw_object_ref,
        media_type=current.media_type,
        byte_size=current.byte_size,
        span_count=sum(spans_by_kind.values()),
        spans_by_kind=spans_by_kind,
        resource_type=resource.type,
    )


def _warn_on_type_mismatch(resource: Resource, requested: ResourceType) -> None:
    if resource.type != requested:
        logger.warning(
            "ingestion.resource_type_mismatch",
            extra={
                "resource_id": str(resource.id),
                "stored_type": resource.type.value,
                "requested_type": requested.value,
            },
        )


def _log_created(result: IngestResult, document: ParsedDocument, *, duration_ms: int) -> None:
    fields: dict[str, Any] = {
        "resource_id": str(result.resource_id),
        "resource_created": result.resource_created,
        "version_id": str(result.version_id),
        "version_number": result.version_number,
        "content_hash": result.content_hash,
        "text_hash": result.text_hash,
        "media_type": result.media_type.value,
        "byte_size": result.byte_size,
        "parser_version": document.parser_version,
        "span_count": result.span_count,
        "spans_by_kind": {kind.value: n for kind, n in result.spans_by_kind.items()},
        "duration_ms": duration_ms,
    }
    if document.page_count is not None:
        fields["total_page_count"] = document.page_count
        fields["empty_page_count"] = document.empty_page_count
    logger.info("ingestion.version_created", extra=fields)

    if document.empty_page_count:
        logger.warning(
            "ingestion.pdf_pages_without_text",
            extra={
                "resource_id": str(result.resource_id),
                "version_id": str(result.version_id),
                "total_page_count": document.page_count,
                "empty_page_count": document.empty_page_count,
            },
        )
