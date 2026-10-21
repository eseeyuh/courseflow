# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Extraction units and the messages built from them.

A unit is a run of consecutive SourceSpans of one current ResourceVersion,
small enough for one call (never a whole course). Each span gets a local ID
(S1, S2, ...) issued here; the model sees only those, and only ``resolve``
maps one back to a real span, of this unit. Course material enters the
prompt only in the user turn, between markers carrying a random nonce that
the material does not contain, so text inside it cannot close the block.
"""

from __future__ import annotations

import re
import secrets
import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.prompts import PromptFiles
from app.ai.provider import ChatMessage
from app.db.models import Resource, ResourceVersion, SourceSpan
from app.retrieval.chunking import estimate_tokens

MAX_UNIT_ESTIMATED_TOKENS = 1500


class UnitScopeError(LookupError):
    """The version is not a current version of the course."""


@dataclass(frozen=True, slots=True)
class UnitSpan:
    local_id: str
    span_id: uuid.UUID
    excerpt: str
    locator: str  # "page 2", "slide 8", "slide 8 notes" or the section path


@dataclass(frozen=True, slots=True)
class ExtractionUnit:
    course_id: uuid.UUID
    resource_version_id: uuid.UUID
    resource_title: str
    resource_type: str
    ordinal: int  # within the version
    spans: tuple[UnitSpan, ...]

    def resolve(self, local_id: str) -> UnitSpan | None:
        """The span a local ID was issued for, in this unit only."""
        for span in self.spans:
            if span.local_id == local_id:
                return span
        return None

    def resolve_by_id(self, span_id: uuid.UUID) -> UnitSpan:
        """A span of this unit by its real ID (from gated evidence)."""
        return next(span for span in self.spans if span.span_id == span_id)


def _locator(span: SourceSpan) -> str:
    if span.slide_number is not None:
        notes = " notes" if span.block_kind.value == "slide_notes" else ""
        return f"slide {span.slide_number}{notes}"
    if span.page_number is not None:
        return f"page {span.page_number}"
    return span.section_path or "section"


def pack_units(
    course_id: uuid.UUID,
    version_id: uuid.UUID,
    resource: Resource,
    spans: Sequence[SourceSpan],
    max_estimated_tokens: int = MAX_UNIT_ESTIMATED_TOKENS,
) -> list[ExtractionUnit]:
    """Consecutive spans, in source order, up to the budget; a span is never split."""
    groups: list[list[SourceSpan]] = [[]]
    used = 0
    for span in spans:
        cost = estimate_tokens(span.excerpt)
        if groups[-1] and used + cost > max_estimated_tokens:
            groups.append([])
            used = 0
        groups[-1].append(span)
        used += cost
    return [
        ExtractionUnit(
            course_id=course_id,
            resource_version_id=version_id,
            resource_title=resource.title,
            resource_type=resource.type.value,
            ordinal=ordinal,
            spans=tuple(
                UnitSpan(f"S{i}", s.id, s.excerpt, _locator(s)) for i, s in enumerate(group, 1)
            ),
        )
        for ordinal, group in enumerate(groups)
        if group
    ]


async def load_units(
    session: AsyncSession, course_id: uuid.UUID, version_id: uuid.UUID
) -> list[ExtractionUnit]:
    """The units of a version that is current in the course, else UnitScopeError."""
    resource = await session.scalar(
        select(Resource).where(
            Resource.course_id == course_id, Resource.current_version_id == version_id
        )
    )
    if resource is None:
        raise UnitScopeError(f"version {version_id} is not current in course {course_id}")
    spans = (
        await session.scalars(
            select(SourceSpan)
            .join(ResourceVersion, ResourceVersion.id == SourceSpan.resource_version_id)
            .where(SourceSpan.resource_version_id == version_id)
            .order_by(SourceSpan.ordinal)
        )
    ).all()
    return pack_units(course_id, version_id, resource, spans)


def render_material(unit: ExtractionUnit) -> str:
    return "\n\n".join(f"[{s.local_id}] {s.locator}\n{s.excerpt}" for s in unit.spans)


def new_nonce(unit: ExtractionUnit) -> str:
    while True:
        nonce = secrets.token_hex(8)
        texts = [
            unit.resource_title,
            *(s.excerpt for s in unit.spans),
            *(s.locator for s in unit.spans),
        ]
        if all(nonce not in text for text in texts):
            return nonce


_UNSAFE_TITLE = re.compile(r'[\x00-\x1f\x7f"<>]+')


def safe_title(title: str) -> str:
    """The resource title as it may appear outside the material markers: it
    comes from the LMS, so it is untrusted. One line, no quotes, no marker
    characters, at most 200 characters. Ordinary titles are unchanged."""
    return " ".join(_UNSAFE_TITLE.sub(" ", title).split())[:200]


def build_messages(
    prompt: PromptFiles, unit: ExtractionUnit, *, nonce: str | None = None
) -> list[ChatMessage]:
    """The fixed system rules, then the task with the delimited material."""
    nonce = nonce or new_nonce(unit)
    material = render_material(unit)
    if nonce in material or nonce in unit.resource_title:
        raise ValueError("the nonce occurs in the material")
    user = prompt.user.substitute(
        resource_type=unit.resource_type,
        resource_title=safe_title(unit.resource_title),
        nonce=nonce,
        material=material,
    )
    return [ChatMessage("system", prompt.system), ChatMessage("user", user)]
