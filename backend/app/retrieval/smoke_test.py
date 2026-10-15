# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Live retrieval smoke test: ingest, index and search synthetic courses with the
configured embedding provider, and check the retrieval guarantees end to end.

    docker compose run --rm --no-deps api python -m app.retrieval.smoke_test

Runs where the raw store is mounted (the API container). All material is
invented. Records are namespaced (``courseflow-smoke:*`` external IDs,
``smoke://courseflow/*`` source URIs) and reused on every run, so reruns add
no courses, resources, versions, chunks, vectors or links; only each search
adds its own query ModelCall, because query vectors are never cached.

The report (one JSON object) identifies queries and targets by label only: it
never contains source or query text.

Exit codes: 0 every check passed; 1 a check failed; 2 provider or raw store
not configured.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai.embeddings import EmbeddingProvider
from app.ai.errors import AIConfigurationError
from app.ai.nebius_embeddings import NebiusEmbeddingProvider
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.db.models import (
    Chunk,
    ChunkEmbedding,
    Course,
    Embedding,
    Institution,
    ModelCall,
    Resource,
    ResourceVersion,
)
from app.db.session import create_db_engine, create_session_factory
from app.domain.enums import LmsType, ModelCallStatus, ResourceType
from app.ingestion.guards import IngestionLimits
from app.ingestion.schemas import RawSource
from app.ingestion.service import ingest_file
from app.ingestion.storage import LocalRawObjectStore, RawObjectStore, RawStoreError
from app.retrieval.chunking import SPAN_PACK_V1, ChunkingParams
from app.retrieval.indexing import IndexingService
from app.retrieval.search import SearchRequest, SearchService

EXPECTED_MODEL = "Qwen/Qwen3-Embedding-8B"
EXPECTED_DIMENSIONS = 4096
INSTITUTION = "CourseFlow Synthetic Smoke Institution (not real)"
COURSE_A = ("courseflow-smoke:course-a", "Synthetic Smoke Course A (invented)")
COURSE_B = ("courseflow-smoke:course-b", "Synthetic Smoke Distractor Course B (invented)")
TOP_K = 5


# --- the invented material ----------------------------------------------------------------------


def _page(title: str, sections: Sequence[tuple[str, Sequence[str]]]) -> bytes:
    body = "".join(
        f"<h2>{heading}</h2>" + "".join(f"<p>{p}</p>" for p in paragraphs)
        for heading, paragraphs in sections
    )
    return f"<!DOCTYPE html><html><body><h1>{title}</h1>{body}</body></html>".encode()


def _detail(topic: str, count: int) -> list[str]:
    return [
        f"Part {i} of the {topic} material walks through an invented example step by step, "
        f"explaining each decision and the trade-offs a student should notice."
        for i in range(1, count + 1)
    ]


_DEADLINE = "The coursework report deadline is 23:59 on 14 November 2026."
_SUBMIT_V1 = "Submit the coursework report by email to the module inbox."
_SUBMIT_V2 = "Submit the coursework report through the online coursework portal."
_SAFETY = "Eye protection must be worn at all times when soldering circuit boards in the lab."


def _brief(submission: str) -> bytes:
    return _page(
        "Coursework brief (synthetic)",
        [
            ("Overview", _detail("coursework overview", 6)),
            ("Deadline", [_DEADLINE]),
            ("Submission", [submission]),
            ("Marking", _detail("marking rubric", 8)),
        ],
    )


_SOURCES: dict[str, tuple[str, ResourceType, list[bytes]]] = {
    # key: (source_uri, type, contents in order; the last is current)
    "brief": (
        "smoke://courseflow/course-a/brief",
        ResourceType.BRIEF,
        [_brief(_SUBMIT_V1), _brief(_SUBMIT_V2)],
    ),
    "lecture": (
        "smoke://courseflow/course-a/lecture",
        ResourceType.LECTURE,
        [
            _page(
                "Lecture notes (synthetic)",
                [
                    ("Hash tables", _detail("hash table", 8)),
                    ("Binary search trees", _detail("binary search tree", 8)),
                    ("Graph traversal", _detail("graph traversal", 8)),
                ],
            )
        ],
    ),
    "lab": (
        "smoke://courseflow/course-a/lab",
        ResourceType.LAB,
        [
            _page(
                "Lab sheet (synthetic)",
                [("Setup", _detail("lab setup", 4)), ("Laboratory safety", [_SAFETY])],
            )
        ],
    ),
}
_DISTRACTOR = (
    "smoke://courseflow/course-b/notice",
    ResourceType.ANNOUNCEMENT,
    [_page("Notice (synthetic)", [("Notice", [_DEADLINE, _SUBMIT_V2, _SAFETY])])],
)

# label -> (question, target marker that the expected chunk contains)
QUERIES: dict[str, tuple[str, str]] = {
    "Q1": ("When is the coursework report deadline?", _DEADLINE),
    "Q2": ("Do I need eye protection when soldering in the lab?", _SAFETY),
    "Q3": ("How do I submit the coursework report?", _SUBMIT_V2),
}


# --- the run --------------------------------------------------------------------------------------


@dataclass
class _Checks:
    results: dict[str, bool]

    def record(self, name: str, passed: bool) -> None:
        self.results[name] = self.results.get(name, True) and passed

    @property
    def passed(self) -> bool:
        return all(self.results.values())


async def run_smoke(
    provider: EmbeddingProvider,
    session_factory: async_sessionmaker[AsyncSession],
    raw_store: RawObjectStore,
    limits: IngestionLimits,
    *,
    expected_model: str = EXPECTED_MODEL,
    expected_dimensions: int = EXPECTED_DIMENSIONS,
    params: ChunkingParams = SPAN_PACK_V1,
    batch_max_items: int = 64,
    batch_max_estimated_tokens: int = 32_000,
) -> dict[str, Any]:
    checks = _Checks({})
    space = provider.space
    checks.record("configured_model", space.model == expected_model)
    checks.record("configured_dimensions", space.dimensions == expected_dimensions)

    institution = await _institution(session_factory)
    course_a = await _course(session_factory, institution, *COURSE_A)
    course_b = await _course(session_factory, institution, *COURSE_B)
    versions: dict[str, list[uuid.UUID]] = {}
    for key, (uri, kind, contents) in _SOURCES.items():
        versions[key] = await _ingest(
            session_factory, raw_store, limits, course_a, uri, kind, contents
        )
    distractor_uri, distractor_kind, distractor_contents = _DISTRACTOR
    await _ingest(
        session_factory, raw_store, limits, course_b, distractor_uri, distractor_kind,
        distractor_contents,
    )  # fmt: skip
    current_a = {key: ids[-1] for key, ids in versions.items()}
    stale_a = {v for ids in versions.values() for v in ids[:-1]}
    checks.record("stale_version_exists", bool(stale_a))

    indexer = IndexingService(
        session_factory,
        provider,
        batch_max_items=batch_max_items,
        batch_max_estimated_tokens=batch_max_estimated_tokens,
        params=params,
    )
    index_runs = {}
    for name, course in (("course_a", course_a), ("course_b", course_b)):
        started = time.perf_counter()
        result = await indexer.index_course(course)
        checks.record("indexing_succeeded", result.status.value == "succeeded")
        summary = result.summary
        index_runs[name] = {
            "run_id": str(result.run_id),
            "status": result.status.value,
            "wall_ms": round((time.perf_counter() - started) * 1000),
            "versions_indexed": summary.versions_indexed,
            "versions_already_indexed": summary.versions_already_indexed,
            "chunks_created": summary.chunks_created,
            "chunks_reused": summary.chunks_reused,
            "embeddings_created": summary.embeddings_created,
            "embeddings_reused": summary.embeddings_reused,
            "provider_calls": summary.provider_calls,
            "prompt_tokens": summary.prompt_tokens,
        }
        index_runs[name]["document_model_calls"] = await _run_calls(session_factory, result.run_id)

    stored = await _stored_dimensions(session_factory, course_a)
    checks.record("stored_vector_dimensions", stored == {expected_dimensions})

    searcher = SearchService(session_factory, provider.space, provider, params=params)
    queries = {}
    for label, (question, marker) in QUERIES.items():
        started = time.perf_counter()
        result = await searcher.search_course(
            SearchRequest.build(course_id=course_a, query=question, k=TOP_K)
        )
        latency = round((time.perf_counter() - started) * 1000)
        hit_versions = {hit.version.id for hit in result.hits}
        target_rank = next((hit.rank for hit in result.hits if marker in hit.text), None)
        checks.record("target_in_top_5", target_rank is not None and target_rank <= TOP_K)
        checks.record("course_isolation", hit_versions <= set(current_a.values()))
        checks.record("stale_version_excluded", not hit_versions & stale_a)
        checks.record("provenance_present", all(hit.spans for hit in result.hits))
        checks.record(
            "active_space_only",
            await _hits_in_space(session_factory, [h.chunk_id for h in result.hits], expected_dimensions),
        )  # fmt: skip
        call = await _query_call(session_factory, result.query_model_call_id)
        checks.record("query_model_call_recorded", call is not None)
        queries[label] = {
            "target_rank": target_rank,
            "searchable_versions": result.searchable_versions,
            "current_versions": result.current_versions,
            "search_wall_ms": latency,
            "query_model_call": call,
            "hits": [
                {
                    "rank": hit.rank,
                    "score": round(hit.score, 4),
                    "chunk_id": str(hit.chunk_id),
                    "resource_type": hit.resource.type.value,
                    "version_id": str(hit.version.id),
                    "spans": len(hit.spans),
                }
                for hit in result.hits
            ],
        }
    checks.record(
        "document_model_calls_recorded",
        all(run["provider_calls"] == run["document_model_calls"] for run in index_runs.values()),
    )
    return {
        "status": "passed" if checks.passed else "failed",
        "checks": checks.results,
        "space": {
            "provider": space.provider,
            "model": space.model,
            "dimensions": space.dimensions,
            "space_version": space.space_version,
        },
        "chunker_version": params.version,
        "courses": {"course_a": str(course_a), "course_b": str(course_b)},
        "course_a_versions": {
            "current": {key: str(v) for key, v in current_a.items()},
            "stale": sorted(str(v) for v in stale_a),
        },
        "index_runs": index_runs,
        "queries": queries,
    }


# --- idempotent fixtures ----------------------------------------------------------------------------


async def _institution(sf: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    async with sf() as session, session.begin():
        found = await session.scalar(
            select(Institution.id).where(Institution.name == INSTITUTION).order_by(Institution.id)
        )
        if found is not None:
            return found
        institution = Institution(name=INSTITUTION, lms_type=LmsType.DEMO)
        session.add(institution)
        await session.flush()
        return institution.id


async def _course(
    sf: async_sessionmaker[AsyncSession], institution: uuid.UUID, external_id: str, title: str
) -> uuid.UUID:
    async with sf() as session, session.begin():
        found = await session.scalar(
            select(Course.id).where(
                Course.institution_id == institution, Course.external_id == external_id
            )
        )
        if found is not None:
            return found
        course = Course(institution_id=institution, title=title, external_id=external_id)
        session.add(course)
        await session.flush()
        return course.id


async def _ingest(
    sf: async_sessionmaker[AsyncSession],
    store: RawObjectStore,
    limits: IngestionLimits,
    course: uuid.UUID,
    source_uri: str,
    resource_type: ResourceType,
    contents: Sequence[bytes],
) -> list[uuid.UUID]:
    """Version ids of the resource in order, ingesting the history only once:
    re-importing an earlier content later would rightly create a new version."""
    async with sf() as session:
        existing = await session.scalar(
            select(Resource.id).where(
                Resource.course_id == course, Resource.source_uri == source_uri
            )
        )
    to_import = list(contents) if existing is None else [contents[-1]]
    for content in to_import:
        async with sf() as session:
            await ingest_file(
                session,
                store,
                course_id=course,
                resource_type=resource_type,
                source=RawSource(
                    content=content, display_name="synthetic.html", source_uri=source_uri
                ),
                limits=limits,
            )
            await session.commit()
    async with sf() as session:
        return list(
            await session.scalars(
                select(ResourceVersion.id)
                .join(Resource, Resource.id == ResourceVersion.resource_id)
                .where(Resource.course_id == course, Resource.source_uri == source_uri)
                .order_by(ResourceVersion.version_number)
            )
        )


# --- probes ---------------------------------------------------------------------------------------


async def _run_calls(sf: async_sessionmaker[AsyncSession], run_id: uuid.UUID) -> int:
    async with sf() as session:
        return int(
            await session.scalar(select(func.count()).where(ModelCall.workflow_run_id == run_id))
            or 0
        )


async def _stored_dimensions(sf: async_sessionmaker[AsyncSession], course: uuid.UUID) -> set[int]:
    async with sf() as session:
        rows = await session.scalars(
            select(func.vector_dims(Embedding.vector))
            .join(ChunkEmbedding, ChunkEmbedding.embedding_id == Embedding.id)
            .join(Chunk, Chunk.id == ChunkEmbedding.chunk_id)
            .join(ResourceVersion, ResourceVersion.id == Chunk.resource_version_id)
            .join(Resource, Resource.id == ResourceVersion.resource_id)
            .where(Resource.course_id == course)
            .distinct()
        )
        return set(rows)


async def _hits_in_space(
    sf: async_sessionmaker[AsyncSession], chunk_ids: Sequence[uuid.UUID], dimensions: int
) -> bool:
    if not chunk_ids:
        return True
    async with sf() as session:
        linked = await session.scalar(
            select(func.count(func.distinct(ChunkEmbedding.chunk_id)))
            .join(Embedding, Embedding.id == ChunkEmbedding.embedding_id)
            .where(ChunkEmbedding.chunk_id.in_(chunk_ids), Embedding.dimensions == dimensions)
        )
    return linked == len(set(chunk_ids))


async def _query_call(
    sf: async_sessionmaker[AsyncSession], call_id: uuid.UUID | None
) -> dict[str, Any] | None:
    if call_id is None:
        return None
    async with sf() as session:
        call = await session.get(ModelCall, call_id)
    if call is None or call.status is not ModelCallStatus.SUCCEEDED:
        return None
    return {
        "model_call_id": str(call.id),
        "workflow_run_id": call.workflow_run_id,
        "prompt_version": call.prompt_version,
        "output_schema": call.output_schema,
        "model": call.model,
        "attempts": call.attempts,
        "latency_ms": call.latency_ms,
        "prompt_tokens": call.prompt_tokens,
        "provider_request_id": call.provider_request_id,
    }


# --- command line ---------------------------------------------------------------------------------


async def _main() -> int:
    settings = get_settings()
    configure_logging(settings.log_level, stream=sys.stderr)
    try:
        provider = NebiusEmbeddingProvider.from_settings(settings)
        store = LocalRawObjectStore.from_settings(settings)
    except (AIConfigurationError, RawStoreError) as exc:
        print(json.dumps({"status": "error", "error": "not_configured", "detail": str(exc)}))
        return 2
    engine = create_db_engine(settings)
    try:
        report = await run_smoke(
            provider,
            create_session_factory(engine),
            store,
            IngestionLimits.from_settings(settings),
            batch_max_items=settings.embedding_batch_max_items,
            batch_max_estimated_tokens=settings.embedding_batch_max_estimated_tokens,
        )
    finally:
        await provider.aclose()
        await engine.dispose()
    print(json.dumps(report, default=str, sort_keys=True))
    return 0 if report["status"] == "passed" else 1


def main(argv: Sequence[str] | None = None) -> int:
    argparse.ArgumentParser(prog="python -m app.retrieval.smoke_test").parse_args(argv)
    return asyncio.run(_main())


if __name__ == "__main__":
    sys.exit(main())
