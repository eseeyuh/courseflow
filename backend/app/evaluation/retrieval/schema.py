# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""The retrieval benchmark's file formats: ``retrieval-case/v1`` (one JSON
object per line of ``cases.jsonl``) and ``retrieval-manifest/v1``
(``manifest.json``).

Human-authored files refer to everything by stable logical keys (course,
resource, version number, exact quote), never by database IDs. These models
check each file's own shape; rules that relate the files to each other (a
reference names a declared resource, counts match) are checked by
``dataset.check_dataset``, and quotes are resolved against ingested text by
``resolve``.
"""

from __future__ import annotations

import enum
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from app.domain.enums import ResourceType

CASE_SCHEMA_VERSION = "retrieval-case/v1"
MANIFEST_SCHEMA_VERSION = "retrieval-manifest/v1"

# Lower-case words joined by single hyphens, e.g. "ds101" or "coursework-brief".
Key = Annotated[str, StringConstraints(pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$", max_length=64)]
CaseId = Annotated[str, StringConstraints(pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$", max_length=96)]
Text = Annotated[str, StringConstraints(min_length=1, max_length=4_000)]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class CaseCategory(enum.StrEnum):
    KEYWORD = "keyword"  # identifiers and exact terms
    PARAPHRASE = "paraphrase"  # the target's distinctive words mostly avoided
    MIXED = "mixed"  # an identifier plus paraphrased intent
    STALE_VERSION_INVARIANT = "stale_version_invariant"
    CROSS_COURSE_INVARIANT = "cross_course_invariant"


POSITIVE_CATEGORIES = frozenset({CaseCategory.KEYWORD, CaseCategory.PARAPHRASE, CaseCategory.MIXED})
INVARIANT_CATEGORIES = frozenset(
    {CaseCategory.STALE_VERSION_INVARIANT, CaseCategory.CROSS_COURSE_INVARIANT}
)


class DistractorKind(enum.StrEnum):
    """A labelled challenge, never a desired ranking outcome."""

    SAME_COURSE_SEMANTIC = "same_course_semantic"
    SAME_COURSE_IRRELEVANT = "same_course_irrelevant"
    STALE_VERSION = "stale_version"
    CROSS_COURSE_NEAR_DUPLICATE = "cross_course_near_duplicate"
    CROSS_COURSE_HOMONYM = "cross_course_homonym"


CROSS_COURSE_DISTRACTORS = frozenset(
    {DistractorKind.CROSS_COURSE_NEAR_DUPLICATE, DistractorKind.CROSS_COURSE_HOMONYM}
)


class ForbiddenKind(enum.StrEnum):
    """A hard invariant: any hit from the referenced version(s) fails the case."""

    STALE_VERSION = "stale_version"
    FOREIGN_COURSE = "foreign_course"


class TextRef(_Frozen):
    """An exact quote in one version of one resource. ``occurrence`` (1-based)
    selects among several occurrences; without it the quote must occur
    exactly once in that version's extracted text."""

    course: Key
    resource: Key
    version: int = Field(ge=1)
    quote: Text
    occurrence: int | None = Field(default=None, ge=1)


class TargetGroup(_Frozen):
    """One required piece of information; any alternative satisfies it."""

    group_id: Key
    alternatives: tuple[TextRef, ...] = Field(min_length=1)


class Distractor(_Frozen):
    kind: DistractorKind
    course: Key
    resource: Key
    version: int = Field(ge=1)
    quote: Text | None = None
    occurrence: int | None = Field(default=None, ge=1)
    note: Text

    @model_validator(mode="after")
    def _occurrence_needs_a_quote(self) -> Distractor:
        if self.occurrence is not None and self.quote is None:
            raise ValueError("occurrence needs a quote")
        return self

    def text_ref(self) -> TextRef | None:
        if self.quote is None:
            return None
        return TextRef(
            course=self.course,
            resource=self.resource,
            version=self.version,
            quote=self.quote,
            occurrence=self.occurrence,
        )


class Forbidden(_Frozen):
    """Versions no hit may come from. ``version`` None: every version of the
    resource."""

    kind: ForbiddenKind
    course: Key
    resource: Key
    version: int | None = Field(default=None, ge=1)


class RetrievalCase(_Frozen):
    case_id: CaseId
    schema_version: Literal["retrieval-case/v1"]
    course: Key
    query: Annotated[str, StringConstraints(min_length=1, max_length=2_000)]
    category: CaseCategory
    # Optional resource-type filter passed with the query.
    resource_types: tuple[ResourceType, ...] | None = Field(default=None, min_length=1)
    target_groups: tuple[TargetGroup, ...] = ()
    distractors: tuple[Distractor, ...] = ()
    forbidden: tuple[Forbidden, ...] = ()
    critical: bool
    notes: Text

    @field_validator("query")
    @classmethod
    def _clean_query(cls, value: str) -> str:
        if value != value.strip() or chr(0) in value:
            raise ValueError("query must be stripped and contain no NUL")
        return value

    @field_validator("resource_types")
    @classmethod
    def _distinct_types(cls, value: tuple[ResourceType, ...] | None) -> object:
        if value is not None and len(set(value)) != len(value):
            raise ValueError("resource_types must not repeat a type")
        return value

    @model_validator(mode="after")
    def _shape_matches_category(self) -> RetrievalCase:
        if self.category in POSITIVE_CATEGORIES:
            if not self.target_groups:
                raise ValueError("a positive case needs at least one target group")
            if self.forbidden:
                raise ValueError("a positive case has no forbidden references in v1")
        else:
            if self.target_groups:
                raise ValueError("an invariant case has no positive targets in v1")
            if not self.forbidden:
                raise ValueError("an invariant case needs at least one forbidden reference")
            if self.critical:
                raise ValueError("the critical case must be a positive case")
        groups = [g.group_id for g in self.target_groups]
        if len(set(groups)) != len(groups):
            raise ValueError("group_id values must be distinct within a case")
        return self

    @property
    def is_positive(self) -> bool:
        return self.category in POSITIVE_CATEGORIES


# --- manifest -------------------------------------------------------------------------------


class VersionEntry(_Frozen):
    version: int = Field(ge=1)
    # Relative POSIX path: corpus/<course>/<resource>/v<version>.html
    file: Annotated[str, StringConstraints(pattern=r"^corpus/[a-z0-9/-]+/v[0-9]+\.html$")]


class ResourceEntry(_Frozen):
    key: Key
    type: ResourceType
    title: Text
    versions: tuple[VersionEntry, ...] = Field(min_length=1)

    @property
    def latest(self) -> int:
        return self.versions[-1].version


class CourseEntry(_Frozen):
    key: Key
    code: Annotated[str, StringConstraints(pattern=r"^[A-Z]{2,4}[0-9]{3}$")]
    title: Text
    resources: tuple[ResourceEntry, ...] = Field(min_length=1)


class CriticalCase(_Frozen):
    case_id: CaseId
    reason: Text
    rule: Text


class ExpectedChunks(_Frozen):
    """Production chunk counts the frozen corpus must produce."""

    current_versions: int = Field(ge=1)
    all_versions: int = Field(ge=1)


class Manifest(_Frozen):
    schema_version: Literal["retrieval-manifest/v1"]
    dataset: Key
    dataset_version: Annotated[str, StringConstraints(pattern=r"^v[0-9]+$")]
    case_schema_version: Literal["retrieval-case/v1"]
    description: Text
    synthetic: Literal[True]
    institution: Text
    licence: Text
    frozen_on: Annotated[str, StringConstraints(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")]
    expected_chunker: Text
    expected_chunks: ExpectedChunks
    # Versioned semantics (see rules.py); ``definitions`` repeats the canonical
    # text of each, so the dataset is self-describing.
    relevance_rule: Literal["full-target-containment/v1"]
    target_span_rule: Literal["single-source-span/v1"]
    dataset_hash_scheme: Literal["sha256-path-and-bytes/v1"]
    definitions: dict[str, Text]
    invariant_rule: Text
    courses: tuple[CourseEntry, ...] = Field(min_length=1)
    cases_file: Literal["cases.jsonl"]
    case_count: int = Field(ge=1)
    category_counts: dict[CaseCategory, int]
    positive_case_count: int = Field(ge=0)
    invariant_case_count: int = Field(ge=0)
    critical_case: CriticalCase
