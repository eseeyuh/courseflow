# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""The LMS connector boundary: what any course source (a demo institution
now, Moodle or Canvas later) must hand to CourseFlow.

Connectors are read-only. They return normalised CourseFlow objects, never
vendor payloads, so ingestion and everything after it is the same for every
LMS. Content they return is untrusted course data: text inside a document
is never an instruction.
"""

from __future__ import annotations

import enum
from collections.abc import Sequence
from datetime import datetime
from typing import Annotated, Protocol

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints

from app.domain.enums import ResourceType
from app.ingestion.schemas import RawSource

Ref = Annotated[str, StringConstraints(pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$", max_length=96)]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)


class CourseStub(_Frozen):
    """A course the connector can import, before any of its content."""

    course_ref: Ref  # the connector's stable identity for the course
    code: Annotated[str, StringConstraints(min_length=1, max_length=32)]
    title: Annotated[str, StringConstraints(min_length=1, max_length=256)]


class SourceDocument(_Frozen):
    """One version of one course resource, normalised.

    ``source.source_uri`` is the resource's stable logical identity: every
    version of the resource carries the same one, which is how ingestion
    recognises a new version rather than a new resource.
    """

    source: RawSource
    resource_type: ResourceType
    title: Annotated[str, StringConstraints(min_length=1, max_length=512)]
    version: int = Field(ge=1)  # the connector's version number, 1..n per resource
    published_at: AwareDatetime


class ChangeKind(enum.StrEnum):
    ADDED = "added"  # a resource that did not exist at the previous sync
    UPDATED = "updated"  # a new version of an existing resource


class RawChange(_Frozen):
    """A change observed after a point in time."""

    kind: ChangeKind
    document: SourceDocument


class LMSConnector(Protocol):
    """Read-only access to an LMS's courses and their source documents."""

    async def list_courses(self, user_ref: str) -> Sequence[CourseStub]:
        """The courses ``user_ref`` can see."""
        ...

    async def fetch_course_sources(self, course_ref: str) -> Sequence[SourceDocument]:
        """The course's resources as of the initial sync: one document per
        resource, its version current at that time."""
        ...

    async def fetch_changes(self, course_ref: str, since: datetime) -> Sequence[RawChange]:
        """Versions published strictly after ``since``, oldest first."""
        ...


class ConnectorError(LookupError):
    """The connector cannot answer: an unknown course or user, or a bad request."""
