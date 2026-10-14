# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Seed courses and resource versions for retrieval tests, laid out exactly as
ingestion lays them out (span excerpts joined by a blank line)."""

import hashlib
import uuid
from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import Course, Institution, Resource, ResourceVersion, SourceSpan
from app.domain.enums import BlockKind, LmsType, MediaType, ResourceType
from app.ingestion.hashing import text_hash
from tests.chunking_cases import Block, layout, sentences

P, H = BlockKind.PAGE, BlockKind.HEADING


def paragraphs(first: int, count: int) -> list[Block]:
    """Pages of two sentences each (~23 estimated tokens): about one chunk each."""
    return [(P, sentences(2 * i, 2), None) for i in range(first, first + count)]


async def seed_course(
    sf: async_sessionmaker[AsyncSession], title: str = "Data Systems"
) -> uuid.UUID:
    async with sf() as session, session.begin():
        course = Course(
            institution=Institution(name="Northbridge", lms_type=LmsType.DEMO), title=title
        )
        session.add(course)
    return course.id


async def seed_version(
    sf: async_sessionmaker[AsyncSession],
    course_id: uuid.UUID,
    blocks: Sequence[Block],
    *,
    source_uri: str = "upload:notes.pdf",
    resource_type: ResourceType = ResourceType.LECTURE,
    title: str = "Notes",
) -> uuid.UUID:
    """A new current version of the course's resource ``source_uri``, laid out
    exactly as ingestion would (excerpts joined by a blank line)."""
    text, spans = layout(blocks)
    async with sf() as session, session.begin():
        resource = await session.scalar(
            select(Resource).where(
                Resource.course_id == course_id, Resource.source_uri == source_uri
            )
        )
        if resource is None:
            resource = Resource(
                course_id=course_id, type=resource_type, title=title, source_uri=source_uri
            )
            session.add(resource)
            await session.flush()
        number = await session.scalar(
            select(func.max(ResourceVersion.version_number)).where(
                ResourceVersion.resource_id == resource.id
            )
        )
        content_hash = hashlib.sha256(
            f"{course_id}|{source_uri}|{number}|{text}".encode()
        ).hexdigest()
        version = ResourceVersion(
            resource_id=resource.id,
            version_number=(number or 0) + 1,
            content_hash=content_hash,
            raw_object_ref=f"sha256:{content_hash}",
            media_type=MediaType.PDF,
            byte_size=len(text),
            display_name="notes.pdf",
            parser_name="test",
            parser_version="1",
            extracted_text=text,
            text_hash=text_hash(text),
        )
        session.add(version)
        await session.flush()
        for span in spans:
            kind = span.block_kind
            session.add(
                SourceSpan(
                    resource_version_id=version.id,
                    ordinal=span.ordinal,
                    block_kind=kind,
                    page_number=span.ordinal + 1 if kind is P else None,
                    section_path="Notes" if kind in (H, BlockKind.PARAGRAPH) else None,
                    start_offset=span.start_offset,
                    end_offset=span.end_offset,
                    excerpt=text[span.start_offset : span.end_offset],
                )
            )
        await session.flush()
        resource.current_version_id = version.id
    return version.id
