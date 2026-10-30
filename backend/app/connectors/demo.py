# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""``DemoConnector``: an ``LMSConnector`` over a synthetic institution on disk.

The institution is described by ``manifest.yaml`` (``demo-manifest/v1``):
courses, their resources, every version's file and publication time, the
intended active version, and which demo users see which courses. It behaves
like a real LMS seen through time:

- ``fetch_course_sources`` returns the course as of ``initial_sync_at``:
  for each resource that existed then, its latest version published by then;
- ``fetch_changes(since)`` returns versions published strictly after
  ``since``, oldest first: ``added`` for a resource's first version,
  ``updated`` for a later one;
- every version of a resource carries the same logical ``source_uri``
  (``demo://<institution>/<course>/<resource>``), so ingestion records a new
  ResourceVersion of the same Resource.

The manifest is parsed with a YAML safe loader that also refuses duplicate
keys, then validated strictly. Files are relative POSIX paths inside the
manifest's directory; nothing outside it is ever read.

An optional ``supplement.yaml`` (``demo-supplement/v1``) beside it adds
further courses of the same institution, and the users who see them. It is
read only on request (``supplement=True``): ``manifest.yaml`` alone is the
frozen evaluation corpus, and the supplement never changes it.
"""

from __future__ import annotations

import itertools
import re
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, Literal

import yaml
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)

from app.connectors.base import (
    ChangeKind,
    ConnectorError,
    CourseStub,
    RawChange,
    Ref,
    SourceDocument,
)
from app.domain.enums import ResourceType
from app.ingestion.schemas import RawSource

MANIFEST_FILE = "manifest.yaml"
SUPPLEMENT_FILE = "supplement.yaml"
MANIFEST_SCHEMA_VERSION = "demo-manifest/v1"
MAX_MANIFEST_BYTES = 1_000_000

_FILE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(/[A-Za-z0-9][A-Za-z0-9._-]*)*$")


class ManifestError(ValueError):
    """The manifest is invalid. ``problems`` lists every rule it breaks."""

    def __init__(self, problems: Sequence[str]) -> None:
        super().__init__(f"{len(problems)} manifest problem(s): " + "; ".join(problems))
        self.problems = tuple(problems)


class _StrictLoader(yaml.SafeLoader):
    """Safe loading (basic YAML types only) that also refuses duplicate keys,
    which PyYAML would otherwise silently resolve to the last one."""

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
        seen = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=deep)
            if key in seen:
                raise yaml.constructor.ConstructorError(
                    None, None, f"duplicate key {key!r}", key_node.start_mark
                )
            seen.add(key)
        return super().construct_mapping(node, deep=deep)


# --- the manifest -------------------------------------------------------------------------


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


Text = Annotated[str, StringConstraints(min_length=1, max_length=512)]


class ManifestVersion(_Model):
    version: int = Field(ge=1)
    file: Annotated[str, StringConstraints(max_length=512)]
    published_at: AwareDatetime

    @field_validator("file")
    @classmethod
    def _relative_posix_path(cls, value: str) -> str:
        if not _FILE.match(value) or ".." in PurePosixPath(value).parts:
            raise ValueError("file must be a relative POSIX path inside the dataset")
        return value


class ManifestResource(_Model):
    key: Ref
    type: ResourceType
    title: Text
    active_version: int = Field(ge=1)
    versions: tuple[ManifestVersion, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _versions_in_order(self) -> ManifestResource:
        numbers = [v.version for v in self.versions]
        if numbers != list(range(1, len(numbers) + 1)):
            raise ValueError(f"{self.key}: versions must be 1..n in order")
        times = [v.published_at for v in self.versions]
        if any(b <= a for a, b in itertools.pairwise(times)):
            raise ValueError(f"{self.key}: published_at must strictly increase")
        if self.active_version != numbers[-1]:
            raise ValueError(f"{self.key}: active_version must be the latest version")
        return self


class ManifestCourse(_Model):
    ref: Ref
    code: Annotated[str, StringConstraints(pattern=r"^[A-Z]{2,5}[0-9]{3,4}$")]
    title: Text
    resources: tuple[ManifestResource, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _distinct_resources(self) -> ManifestCourse:
        keys = [r.key for r in self.resources]
        if len(set(keys)) != len(keys):
            raise ValueError(f"{self.ref}: resource keys must be distinct")
        return self


class ManifestInstitution(_Model):
    key: Ref
    name: Text
    fictional: Literal[True]


class ManifestUser(_Model):
    user_ref: Ref
    courses: tuple[Ref, ...] = Field(min_length=1)


class DemoManifest(_Model):
    schema_version: Literal["demo-manifest/v1"]
    institution: ManifestInstitution
    initial_sync_at: AwareDatetime
    users: tuple[ManifestUser, ...] = Field(min_length=1)
    courses: tuple[ManifestCourse, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _references(self) -> DemoManifest:
        refs = [c.ref for c in self.courses]
        if len(set(refs)) != len(refs):
            raise ValueError("course refs must be distinct")
        users = [u.user_ref for u in self.users]
        if len(set(users)) != len(users):
            raise ValueError("user refs must be distinct")
        unknown = sorted({c for u in self.users for c in u.courses} - set(refs))
        if unknown:
            raise ValueError(f"users see unknown courses {unknown}")
        for course in self.courses:
            if not any(
                r.versions[0].published_at <= self.initial_sync_at for r in course.resources
            ):
                raise ValueError(f"{course.ref}: no resource exists at initial_sync_at")
        return self

    def course(self, ref: str) -> ManifestCourse:
        for course in self.courses:
            if course.ref == ref:
                return course
        raise ConnectorError(f"unknown course {ref!r}")


class DemoSupplement(_Model):
    """Further courses of the manifest's institution (``supplement.yaml``)."""

    schema_version: Literal["demo-supplement/v1"]
    users: tuple[ManifestUser, ...] = Field(min_length=1)
    courses: tuple[ManifestCourse, ...] = Field(min_length=1)


def _read_yaml(path: Path) -> Any:
    try:
        data = path.read_bytes()
    except OSError:
        raise ManifestError([f"{path.name}: missing or unreadable"]) from None
    if len(data) > MAX_MANIFEST_BYTES:
        raise ManifestError([f"{path.name}: larger than {MAX_MANIFEST_BYTES} bytes"])
    try:
        # _StrictLoader is a SafeLoader: only basic YAML types, never Python objects.
        return yaml.load(data.decode("utf-8"), Loader=_StrictLoader)
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise ManifestError([f"{path.name}: not valid UTF-8 YAML ({exc})"]) from None


def _validate[M: BaseModel](model: type[M], raw: Any) -> M:
    try:
        return model.model_validate(raw)
    except ValidationError as exc:
        raise ManifestError(
            [f"{'.'.join(map(str, e['loc'])) or '(root)'}: {e['msg']}" for e in exc.errors()]
        ) from None


def load_supplement(root: Path) -> DemoSupplement:
    """Parse and validate ``root/supplement.yaml`` on its own."""
    return _validate(DemoSupplement, _read_yaml(root / SUPPLEMENT_FILE))


def load_manifest(root: Path, *, supplement: bool = False) -> DemoManifest:
    """Parse and validate ``root/manifest.yaml`` (and, with ``supplement``,
    merge ``root/supplement.yaml`` into it), checking that every listed file
    exists inside ``root``; ManifestError lists every problem."""
    manifest = _validate(DemoManifest, _read_yaml(root / MANIFEST_FILE))
    if supplement:
        extra = load_supplement(root)
        users = {u.user_ref: list(u.courses) for u in manifest.users}
        for user in extra.users:
            users.setdefault(user.user_ref, []).extend(user.courses)
        manifest = _validate(
            DemoManifest,
            manifest.model_dump(mode="json")
            | {
                "users": [{"user_ref": u, "courses": c} for u, c in users.items()],
                "courses": [c.model_dump(mode="json") for c in manifest.courses + extra.courses],
            },
        )
    problems = []
    resolved_root = root.resolve()
    for course in manifest.courses:
        for resource in course.resources:
            for version in resource.versions:
                file = (root / version.file).resolve()
                if not file.is_relative_to(resolved_root) or not file.is_file():
                    problems.append(f"{version.file}: missing, or outside the dataset")
    if problems:
        raise ManifestError(problems)
    return manifest


# --- the connector ------------------------------------------------------------------------


class DemoConnector:
    """A synthetic institution, read from ``root`` (which holds manifest.yaml,
    and with ``supplement`` also supplement.yaml)."""

    def __init__(self, root: Path, *, supplement: bool = False) -> None:
        self._root = root
        self.manifest = load_manifest(root, supplement=supplement)

    def source_uri(self, course: ManifestCourse, resource: ManifestResource) -> str:
        return f"demo://{self.manifest.institution.key}/{course.ref}/{resource.key}"

    def _document(
        self, course: ManifestCourse, resource: ManifestResource, version: ManifestVersion
    ) -> SourceDocument:
        return SourceDocument(
            source=RawSource(
                content=(self._root / version.file).read_bytes(),
                display_name=PurePosixPath(version.file).name,
                source_uri=self.source_uri(course, resource),
            ),
            resource_type=resource.type,
            title=resource.title,
            version=version.version,
            published_at=version.published_at,
        )

    async def list_courses(self, user_ref: str) -> Sequence[CourseStub]:
        users = {u.user_ref: u for u in self.manifest.users}
        if user_ref not in users:
            raise ConnectorError(f"unknown user {user_ref!r}")
        visible = set(users[user_ref].courses)
        return [
            CourseStub(course_ref=c.ref, code=c.code, title=c.title)
            for c in self.manifest.courses
            if c.ref in visible
        ]

    async def fetch_course_sources(self, course_ref: str) -> Sequence[SourceDocument]:
        course = self.manifest.course(course_ref)
        documents = []
        for resource in course.resources:
            known = [
                v for v in resource.versions if v.published_at <= self.manifest.initial_sync_at
            ]
            if known:
                documents.append(self._document(course, resource, known[-1]))
        return documents

    async def fetch_changes(self, course_ref: str, since: datetime) -> Sequence[RawChange]:
        if since.tzinfo is None:
            raise ConnectorError("since must be timezone-aware")
        course = self.manifest.course(course_ref)
        changes = [
            (version.published_at, resource.key, version.version, resource, version)
            for resource in course.resources
            for version in resource.versions
            if version.published_at > since
        ]
        changes.sort(key=lambda c: (c[0], c[1], c[2]))
        return [
            RawChange(
                kind=ChangeKind.ADDED if version.version == 1 else ChangeKind.UPDATED,
                document=self._document(course, resource, version),
            )
            for _, _, _, resource, version in changes
        ]
