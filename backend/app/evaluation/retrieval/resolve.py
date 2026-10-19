# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Ingest a loaded dataset with the production pipeline and resolve every
quote to exact evidence.

Ingestion uses the production parser and ``ingest_file``: one course per
dataset course, each resource's versions in order, so the last version of
each resource is current and earlier ones are superseded. Resolution then
works on the stored text only:

1. find the quote in the exact version's extracted text: exactly one
   occurrence, or the one an explicit ``occurrence`` selects;
2. absolute offsets of that occurrence;
3. the one SourceSpan containing it, and the sub-range inside the span;
4. the production chunks (``span-pack/v1``, derived exactly as indexing
   derives them) that contain or overlap it. A positive target needs at
   least one chunk containing the whole quote.

No embedding, no provider, no ranking: nothing here can reveal retrieval
quality. ``validate_in_database`` runs everything in one transaction that is
always rolled back, with raw files in a temporary directory, so it leaves
nothing behind. Database IDs exist only inside that transaction; the
offsets, span ordinals and chunk ordinals are deterministic.
"""

from __future__ import annotations

import tempfile
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.db.models import Course, Institution, Resource, SourceSpan
from app.domain.enums import LmsType
from app.evaluation.retrieval.dataset import Dataset, DatasetError
from app.evaluation.retrieval.rules import contains
from app.evaluation.retrieval.schema import Forbidden, ForbiddenKind, RetrievalCase, TextRef
from app.ingestion.guards import IngestionLimits
from app.ingestion.schemas import RawSource
from app.ingestion.service import ingest_file
from app.ingestion.storage import LocalRawObjectStore, RawObjectStore
from app.retrieval import store
from app.retrieval.chunking import SPAN_PACK_V1, ChunkingParams
from app.retrieval.indexing import SourceContent, derive_chunks
from app.retrieval.provenance import ChunkDraft

VersionKey = tuple[str, str, int]  # (course key, resource key, version number)


def production_limits() -> IngestionLimits:
    """The production defaults, read from the settings model itself."""
    fields = Settings.model_fields
    return IngestionLimits(
        max_bytes=fields["ingest_max_bytes"].default,
        max_pdf_pages=fields["ingest_max_pdf_pages"].default,
        max_extracted_chars=fields["ingest_max_extracted_chars"].default,
        max_zip_members=fields["ingest_max_zip_members"].default,
        max_zip_uncompressed_bytes=fields["ingest_max_zip_uncompressed_bytes"].default,
        max_zip_compression_ratio=fields["ingest_max_zip_compression_ratio"].default,
    )


@dataclass(frozen=True, slots=True)
class IngestedVersion:
    key: VersionKey
    course_id: uuid.UUID
    resource_id: uuid.UUID
    version_id: uuid.UUID
    current: bool


@dataclass(frozen=True, slots=True)
class ResolvedText:
    """A quote resolved to evidence in one version."""

    ref: TextRef
    version_id: uuid.UUID
    start: int  # absolute offsets in the version's extracted text
    end: int
    span_id: uuid.UUID
    span_ordinal: int
    relative_start: int  # the same range inside the span's excerpt
    relative_end: int
    containing_chunks: tuple[int, ...]  # ordinals of chunks containing [start, end)
    overlapping_chunks: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class ResolvedCase:
    case_id: str
    target_groups: tuple[tuple[ResolvedText, ...], ...]
    distractors: tuple[ResolvedText | None, ...]  # None: the distractor names no quote
    forbidden_versions: tuple[VersionKey, ...]
    forbidden_version_ids: tuple[uuid.UUID, ...]


@dataclass(frozen=True, slots=True)
class ResolvedDataset:
    dataset: Dataset
    versions: dict[VersionKey, IngestedVersion]
    chunk_counts: dict[VersionKey, int]
    cases: tuple[ResolvedCase, ...]

    @property
    def chunks_current(self) -> int:
        return sum(n for key, n in self.chunk_counts.items() if self.versions[key].current)

    @property
    def chunks_all(self) -> int:
        return sum(self.chunk_counts.values())


# --- ingestion ------------------------------------------------------------------------------


def source_uri(dataset: Dataset, course: str, resource: str) -> str:
    manifest = dataset.manifest
    return f"eval://{manifest.dataset}/{manifest.dataset_version}/{course}/{resource}"


async def ingest_dataset(
    session: AsyncSession,
    raw_store: RawObjectStore,
    dataset: Dataset,
    *,
    limits: IngestionLimits | None = None,
) -> dict[VersionKey, IngestedVersion]:
    """Create the dataset's institution and courses and ingest every version
    in order, in the caller's transaction (never committed here)."""
    limits = limits or production_limits()
    manifest = dataset.manifest
    institution = Institution(name=manifest.institution, lms_type=LmsType.DEMO)
    session.add(institution)
    ingested: dict[VersionKey, IngestedVersion] = {}
    for entry in manifest.courses:
        course = Course(
            institution=institution,
            title=f"{entry.code} {entry.title}",
            external_id=f"{manifest.dataset}/{manifest.dataset_version}/{entry.key}",
        )
        session.add(course)
        await session.flush()
        for resource in entry.resources:
            for version in resource.versions:
                content = dataset.corpus_file(entry.key, resource.key, version.version).read_bytes()
                result = await ingest_file(
                    session,
                    raw_store,
                    course_id=course.id,
                    resource_type=resource.type,
                    source=RawSource(
                        content=content,
                        display_name=f"{resource.key}-v{version.version}.html",
                        source_uri=source_uri(dataset, entry.key, resource.key),
                    ),
                    limits=limits,
                    title=resource.title,
                )
                if not result.created or result.version_number != version.version:
                    problem = (
                        f"{entry.key}/{resource.key} v{version.version}: ingestion produced "
                        f"version {result.version_number} (created={result.created})"
                    )
                    raise DatasetError([problem])
                key = (entry.key, resource.key, version.version)
                ingested[key] = IngestedVersion(
                    key=key,
                    course_id=course.id,
                    resource_id=result.resource_id,
                    version_id=result.version_id,
                    current=version.version == resource.latest,
                )
    await _check_current_versions(session, ingested)
    return ingested


async def _check_current_versions(
    session: AsyncSession, ingested: dict[VersionKey, IngestedVersion]
) -> None:
    rows = await session.execute(
        select(Resource.id, Resource.current_version_id).where(
            Resource.id.in_({v.resource_id for v in ingested.values()})
        )
    )
    current = {row.id: row.current_version_id for row in rows}
    wrong = [
        f"{'/'.join(map(str, v.key))}: current flag disagrees with the stored current version"
        for v in ingested.values()
        if (current[v.resource_id] == v.version_id) != v.current
    ]
    if wrong:
        raise DatasetError(wrong)


# --- resolution -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _VersionText:
    version_id: uuid.UUID
    text: str
    spans: tuple[tuple[uuid.UUID, int, int, int], ...]  # (id, ordinal, start, end)
    chunks: tuple[ChunkDraft, ...]


async def resolve_dataset(
    session: AsyncSession,
    dataset: Dataset,
    ingested: dict[VersionKey, IngestedVersion],
    *,
    params: ChunkingParams = SPAN_PACK_V1,
) -> ResolvedDataset:
    """Resolve every quote of every case; DatasetError lists every failure."""
    problems: list[str] = []
    if params.version != dataset.manifest.expected_chunker:
        problems.append(
            f"chunker {params.version} is not the dataset's {dataset.manifest.expected_chunker}"
        )
    texts: dict[VersionKey, _VersionText] = {}
    for key, version in ingested.items():
        loaded = await _load(session, version.version_id, params)
        if isinstance(loaded, str):
            problems.append(f"{'/'.join(map(str, key))}: {loaded}")
        else:
            texts[key] = loaded

    cases = [
        _resolve_case(case, dataset, ingested, texts, params, problems) for case in dataset.cases
    ]
    counts = {key: len(text.chunks) for key, text in texts.items()}
    resolved = ResolvedDataset(dataset, ingested, counts, tuple(cases))
    if not problems:
        expected = dataset.manifest.expected_chunks
        actual = (resolved.chunks_current, resolved.chunks_all)
        if actual != (expected.current_versions, expected.all_versions):
            problems.append(
                f"{params.version} produced {actual[0]} chunks in current versions and "
                f"{actual[1]} in all versions; the manifest expects "
                f"{expected.current_versions} and {expected.all_versions}"
            )
    if problems:
        raise DatasetError(problems)
    return resolved


def _resolve_case(
    case: RetrievalCase,
    dataset: Dataset,
    ingested: dict[VersionKey, IngestedVersion],
    texts: dict[VersionKey, _VersionText],
    params: ChunkingParams,
    problems: list[str],
) -> ResolvedCase:
    """One case's targets, distractors and forbidden versions; failures are
    appended to ``problems``."""

    def resolve(ref: TextRef, role: str, *, target: bool) -> ResolvedText | None:
        outcome = _resolve_text(ref, texts.get((ref.course, ref.resource, ref.version)))
        if isinstance(outcome, str):
            problems.append(f"{case.case_id} {role}: {outcome}")
            return None
        if target and not outcome.containing_chunks:
            problems.append(
                f"{case.case_id} {role}: no {params.version} chunk contains the whole quote"
            )
        return outcome

    groups = []
    for group in case.target_groups:
        role = f"target {group.group_id}"
        resolved = [resolve(ref, role, target=True) for ref in group.alternatives]
        groups.append(tuple(r for r in resolved if r is not None))
    distractors = []
    for distractor in case.distractors:
        ref = distractor.text_ref()
        role = f"distractor {distractor.kind.value}"
        distractors.append(None if ref is None else resolve(ref, role, target=False))
    forbidden = tuple(_forbidden_versions(dataset, case.forbidden))
    return ResolvedCase(
        case_id=case.case_id,
        target_groups=tuple(groups),
        distractors=tuple(distractors),
        forbidden_versions=forbidden,
        forbidden_version_ids=tuple(
            ingested[key].version_id for key in forbidden if key in ingested
        ),
    )


async def _load(
    session: AsyncSession, version_id: uuid.UUID, params: ChunkingParams
) -> _VersionText | str:
    source = await store.load_version(session, version_id)
    content, chunks = derive_chunks(source, params)
    if source is None or content is not SourceContent.CHUNKABLE:
        return f"not chunkable ({content.value})"
    spans = await session.execute(
        select(SourceSpan.id, SourceSpan.ordinal, SourceSpan.start_offset, SourceSpan.end_offset)
        .where(SourceSpan.resource_version_id == version_id)
        .order_by(SourceSpan.ordinal)
    )
    return _VersionText(
        version_id=version_id,
        text=source.extracted_text,
        spans=tuple((r.id, r.ordinal, r.start_offset, r.end_offset) for r in spans),
        chunks=tuple(chunks),
    )


def occurrences(text: str, quote: str) -> list[int]:
    """Start offsets of every occurrence of ``quote``, overlapping ones included."""
    starts = []
    position = text.find(quote)
    while position != -1:
        starts.append(position)
        position = text.find(quote, position + 1)
    return starts


def _resolve_text(ref: TextRef, version: _VersionText | None) -> ResolvedText | str:
    where = f"{ref.course}/{ref.resource} v{ref.version}"
    if version is None:
        return f"{where} was not ingested"
    starts = occurrences(version.text, ref.quote)
    if ref.occurrence is None:
        if not starts:
            return f"quote not found in {where}"
        if len(starts) > 1:
            return f"quote occurs {len(starts)} times in {where}; add an occurrence selector"
        start = starts[0]
    else:
        if ref.occurrence > len(starts):
            return f"quote occurrence {ref.occurrence} requested, {len(starts)} found in {where}"
        start = starts[ref.occurrence - 1]
    end = start + len(ref.quote)
    spans = [s for s in version.spans if contains(s[2], s[3], start, end)]
    if len(spans) != 1:  # single-source-span/v1
        return f"quote in {where} is not inside exactly one SourceSpan"
    span_id, ordinal, span_start, _ = spans[0]
    return ResolvedText(
        ref=ref,
        version_id=version.version_id,
        start=start,
        end=end,
        span_id=span_id,
        span_ordinal=ordinal,
        relative_start=start - span_start,
        relative_end=end - span_start,
        containing_chunks=tuple(  # full-target-containment/v1
            c.ordinal for c in version.chunks if contains(c.start_offset, c.end_offset, start, end)
        ),
        overlapping_chunks=tuple(
            c.ordinal for c in version.chunks if c.start_offset < end and start < c.end_offset
        ),
    )


def _forbidden_versions(dataset: Dataset, forbidden: Sequence[Forbidden]) -> list[VersionKey]:
    """Every version a hit may not come from. A stale_version reference never
    covers the current version, even when it names the whole resource."""
    keys: list[VersionKey] = []
    for item in forbidden:
        resource = dataset.resource(item.course, item.resource)
        numbers = [v.version for v in resource.versions] if item.version is None else [item.version]
        if item.kind is ForbiddenKind.STALE_VERSION:
            numbers = [n for n in numbers if n != resource.latest]
        keys.extend((item.course, item.resource, n) for n in numbers)
    return keys


# --- the whole check in a disposable transaction ---------------------------------------------


async def validate_in_database(
    session_factory: async_sessionmaker[AsyncSession],
    dataset: Dataset,
    *,
    params: ChunkingParams = SPAN_PACK_V1,
) -> ResolvedDataset:
    """Ingest and resolve inside one transaction that is always rolled back,
    with raw files in a temporary directory: nothing is left behind."""
    with tempfile.TemporaryDirectory(prefix="courseflow-eval-") as directory:
        raw_store = LocalRawObjectStore(Path(directory).resolve())
        async with session_factory() as session:
            try:
                ingested = await ingest_dataset(session, raw_store, dataset)
                return await resolve_dataset(session, dataset, ingested, params=params)
            finally:
                await session.rollback()
