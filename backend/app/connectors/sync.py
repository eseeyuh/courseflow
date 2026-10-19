# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Import a connector's course into CourseFlow, and apply its later changes.

Generic over ``LMSConnector``: the connector supplies normalised documents,
ingestion (``ingest_file``) creates Resources, ResourceVersions and
SourceSpans exactly as for any other source. Indexing is separate and
verified on its own (``IndexingService``); no provider is called here and
no transaction is held across one.

Atomicity: an initial import is one transaction per course, and a batch of
changes is one transaction. Either everything in it is stored, or nothing.

Idempotence: the connector's version numbers are the resource's version
numbers. A document whose version is already stored with the same content
is skipped, so re-running an import or a change batch writes nothing.
Stored content that differs from the connector's, or a gap in version
numbers, is a ``SyncConflictError`` (the course was changed by something
else); nothing is guessed.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.connectors.base import CourseStub, LMSConnector, SourceDocument
from app.db.models import Course, Institution, Resource, ResourceVersion
from app.domain.enums import LmsType
from app.ingestion.guards import IngestionLimits
from app.ingestion.hashing import sha256_hex
from app.ingestion.service import ingest_file
from app.ingestion.storage import RawObjectStore


class SyncConflictError(RuntimeError):
    """Stored course content disagrees with the connector's versions."""


@dataclass(frozen=True, slots=True)
class SyncedDocument:
    source_uri: str
    version: int
    version_id: uuid.UUID
    created: bool  # False: already stored, skipped


@dataclass(frozen=True, slots=True)
class SyncResult:
    course_id: uuid.UUID
    course_created: bool
    documents: tuple[SyncedDocument, ...]

    @property
    def versions_created(self) -> int:
        return sum(d.created for d in self.documents)


async def ensure_institution(session: AsyncSession, name: str, lms_type: LmsType) -> uuid.UUID:
    found = (
        await session.scalars(
            select(Institution.id).where(Institution.name == name, Institution.lms_type == lms_type)
        )
    ).all()
    if len(found) > 1:
        raise SyncConflictError(f"{len(found)} institutions are named {name!r} ({lms_type.value})")
    if found:
        return found[0]
    institution = Institution(name=name, lms_type=lms_type)
    session.add(institution)
    await session.flush()
    return institution.id


async def _ensure_course(
    session: AsyncSession, institution_id: uuid.UUID, stub: CourseStub
) -> tuple[uuid.UUID, bool]:
    course_id = await session.scalar(
        select(Course.id).where(
            Course.institution_id == institution_id, Course.external_id == stub.course_ref
        )
    )
    if course_id is not None:
        return course_id, False
    course = Course(
        institution_id=institution_id,
        title=f"{stub.code} {stub.title}",
        external_id=stub.course_ref,
    )
    session.add(course)
    await session.flush()
    return course.id, True


async def _stored_versions(
    session: AsyncSession, course_id: uuid.UUID, source_uri: str
) -> dict[int, tuple[uuid.UUID, str]]:
    rows = await session.execute(
        select(ResourceVersion.version_number, ResourceVersion.id, ResourceVersion.content_hash)
        .join(Resource, Resource.id == ResourceVersion.resource_id)
        .where(Resource.course_id == course_id, Resource.source_uri == source_uri)
    )
    return {number: (version_id, digest) for number, version_id, digest in rows}


async def _sync_document(
    session: AsyncSession,
    store: RawObjectStore,
    course_id: uuid.UUID,
    document: SourceDocument,
    limits: IngestionLimits,
) -> SyncedDocument:
    uri = document.source.source_uri
    stored = await _stored_versions(session, course_id, uri)
    digest = sha256_hex(document.source.content)
    if document.version in stored:
        version_id, stored_digest = stored[document.version]
        if stored_digest != digest:
            raise SyncConflictError(f"{uri} v{document.version}: stored content differs")
        return SyncedDocument(uri, document.version, version_id, created=False)
    expected = max(stored, default=0) + 1
    if document.version != expected:
        raise SyncConflictError(f"{uri}: got v{document.version}, the next version is v{expected}")
    result = await ingest_file(
        session,
        store,
        course_id=course_id,
        resource_type=document.resource_type,
        source=document.source,
        limits=limits,
        title=document.title,
    )
    if not result.created or result.version_number != document.version:
        raise SyncConflictError(
            f"{uri}: ingestion produced v{result.version_number} (created={result.created}), "
            f"expected a new v{document.version}"
        )
    return SyncedDocument(uri, document.version, result.version_id, created=True)


async def import_course(
    session_factory: async_sessionmaker[AsyncSession],
    store: RawObjectStore,
    connector: LMSConnector,
    stub: CourseStub,
    *,
    institution_name: str,
    lms_type: LmsType,
    limits: IngestionLimits,
) -> SyncResult:
    """The course as of the connector's initial sync, in one transaction."""
    documents = await connector.fetch_course_sources(stub.course_ref)
    async with session_factory() as session, session.begin():
        institution_id = await ensure_institution(session, institution_name, lms_type)
        course_id, created = await _ensure_course(session, institution_id, stub)
        synced = [
            await _sync_document(session, store, course_id, d, limits)
            for d in sorted(documents, key=lambda d: d.source.source_uri)
        ]
    return SyncResult(course_id, created, tuple(synced))


async def apply_changes(
    session_factory: async_sessionmaker[AsyncSession],
    store: RawObjectStore,
    connector: LMSConnector,
    course_id: uuid.UUID,
    course_ref: str,
    since: datetime,
    *,
    limits: IngestionLimits,
) -> SyncResult:
    """Every change after ``since``, oldest first, in one transaction."""
    changes = await connector.fetch_changes(course_ref, since)
    async with session_factory() as session, session.begin():
        synced: Sequence[SyncedDocument] = [
            await _sync_document(session, store, course_id, change.document, limits)
            for change in changes
        ]
    return SyncResult(course_id, False, tuple(synced))
