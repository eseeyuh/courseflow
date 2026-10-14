# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Invariants of the retrieval schema (chunks, embedding spaces, embeddings,
chunk links), tested against PostgreSQL itself, and the application-level
chunk provenance check that must reject the same bad chunks first."""

import hashlib
import random
import struct
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import delete, func, insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.embeddings import DOCUMENT_INPUT_FORMAT, QUERY_INPUT_FORMAT
from app.db.models import (
    Chunk,
    ChunkEmbedding,
    Course,
    Embedding,
    EmbeddingSpaceRecord,
    Institution,
    ModelCall,
    Resource,
    ResourceVersion,
    SourceSpan,
)
from app.domain.enums import (
    BlockKind,
    LmsType,
    MediaType,
    ModelCallKind,
    ModelCallStatus,
    ModelRole,
    ResourceType,
)
from app.ingestion.hashing import text_hash
from app.retrieval.provenance import (
    ChunkDraft,
    ChunkProvenanceError,
    SpanBounds,
    check_chunk_provenance,
    span_bounds,
)

pytestmark = pytest.mark.anyio

CHUNKER = "span-pack/v1"
EXCERPTS = (
    "Week 3: recursion and base cases.",
    "Lab 2 covers linked lists.",
    "Coursework 1 is due on 31 October.",
)


def _layout(excerpts: tuple[str, ...]) -> tuple[str, dict[int, SpanBounds]]:
    """Extracted text as ingestion builds it (excerpts joined by a blank line)."""
    text, bounds, offset = "", {}, 0
    for ordinal, excerpt in enumerate(excerpts):
        if ordinal:
            text += "\n\n"
            offset += 2
        bounds[ordinal] = SpanBounds(ordinal, offset, offset + len(excerpt))
        text += excerpt
        offset += len(excerpt)
    return text, bounds


TEXT, BOUNDS = _layout(EXCERPTS)
B0, B1, B2 = BOUNDS[0], BOUNDS[1], BOUNDS[2]


def _draft(
    first: int,
    last: int,
    start: int | None = None,
    end: int | None = None,
    *,
    text: str | None = None,
    ordinal: int = 0,
) -> ChunkDraft:
    start = BOUNDS[first].start_offset if start is None else start
    end = BOUNDS[last].end_offset if end is None else end
    return ChunkDraft(
        ordinal=ordinal,
        start_offset=start,
        end_offset=end,
        first_span_ordinal=first,
        last_span_ordinal=last,
        text=TEXT[start:end] if text is None else text,
        estimated_tokens=max(1, (end - start) // 4),
    )


def _altered(draft: ChunkDraft) -> ChunkDraft:
    """Same range, same length, one character different (hash recomputed)."""
    text = "X" + draft.text[1:]
    return ChunkDraft(**{**_fields(draft), "text": text})


def _fields(draft: ChunkDraft) -> dict[str, Any]:
    return {name: getattr(draft, name) for name in ChunkDraft.__slots__}


GOOD_CHUNKS = {
    "one-span": _draft(0, 0),
    "two-spans-across-the-gap": _draft(0, 1),
    "all-spans": _draft(0, 2),
    "split-oversized-span": _draft(1, 1, B1.start_offset + 4, B1.end_offset - 3),
}
BAD_CHUNKS = {
    "first-after-last": _draft(1, 0, B0.start_offset, B1.end_offset),
    # Ordinal 3 exists only in the other version the database test creates.
    "span-of-another-version": _draft(0, 3, B0.start_offset, B2.end_offset),
    "start-before-first-span": _draft(1, 1, B0.start_offset, B1.end_offset),
    "start-in-the-gap": _draft(1, 1, B0.end_offset, B1.end_offset),
    "end-after-last-span": _draft(0, 0, B0.start_offset, B1.end_offset),
    "end-at-last-span-start": _draft(0, 1, B0.start_offset, B1.start_offset),
    "text-not-the-exact-slice": _altered(_draft(0, 1)),
}


# --- builders --------------------------------------------------------------------------


async def _course(session: AsyncSession, title: str = "Data Systems") -> Course:
    course = Course(institution=Institution(name="Northbridge", lms_type=LmsType.DEMO), title=title)
    session.add(course)
    await session.flush()
    return course


async def _version(
    session: AsyncSession,
    course: Course,
    excerpts: tuple[str, ...] = EXCERPTS,
    *,
    source_uri: str = "upload:notes.pdf",
    resource: Resource | None = None,
    current: bool = True,
) -> ResourceVersion:
    text, bounds = _layout(excerpts)
    content_hash = hashlib.sha256(f"{course.title}|{source_uri}|{text}".encode()).hexdigest()
    if resource is None:
        resource = Resource(
            course=course, type=ResourceType.LECTURE, title="Notes", source_uri=source_uri
        )
    version = ResourceVersion(
        resource=resource,
        version_number=1 if current or resource.id is None else 2,
        content_hash=content_hash,
        raw_object_ref=f"sha256:{content_hash}",
        media_type=MediaType.PDF,
        byte_size=len(text),
        display_name="notes.pdf",
        parser_name="pdf",
        parser_version="1",
        extracted_text=text,
        text_hash=text_hash(text),
    )
    spans = [
        SourceSpan(
            resource_version=version,
            ordinal=ordinal,
            block_kind=BlockKind.PAGE,
            page_number=ordinal + 1,
            start_offset=bound.start_offset,
            end_offset=bound.end_offset,
            excerpt=excerpts[ordinal],
        )
        for ordinal, bound in bounds.items()
    ]
    session.add_all([version, *spans])
    await session.flush()
    if current:
        resource.current_version = version
        await session.flush()
    return version


async def _insert_chunk(
    session: AsyncSession, version: ResourceVersion, draft: ChunkDraft, **overrides: Any
) -> uuid.UUID:
    """A Core INSERT, bypassing the application check: the database must refuse alone."""
    values = {
        **_fields(draft),
        "resource_version_id": version.id,
        "chunker_version": CHUNKER,
        "text_hash": draft.text_hash,
        **overrides,
    }
    return (await session.execute(insert(Chunk).values(values).returning(Chunk.id))).scalar_one()


async def _space(
    session: AsyncSession, dimensions: int = 4, space_version: str = "v1"
) -> EmbeddingSpaceRecord:
    space = EmbeddingSpaceRecord(
        provider="fake", model="fake/model", dimensions=dimensions, space_version=space_version
    )
    session.add(space)
    await session.flush()
    return space


def _basis(dimensions: int, position: int = 0) -> list[float]:
    vector = [0.0] * dimensions
    vector[position] = 1.0
    return vector


async def _embedding(
    session: AsyncSession,
    space: EmbeddingSpaceRecord,
    provider_input: str,
    *,
    input_format: str = DOCUMENT_INPUT_FORMAT,
    vector: list[float] | None = None,
    **overrides: Any,
) -> uuid.UUID:
    values = {
        "space_id": space.id,
        "dimensions": space.dimensions,
        "input_format": input_format,
        "input_hash": text_hash(provider_input),
        "vector": _basis(space.dimensions) if vector is None else vector,
        **overrides,
    }
    statement = insert(Embedding).values(values).returning(Embedding.id)
    return (await session.execute(statement)).scalar_one()


async def _link(
    session: AsyncSession, chunk_id: uuid.UUID, embedding_id: uuid.UUID, **overrides: Any
) -> None:
    embedding = await session.get_one(Embedding, embedding_id)
    values = {
        "chunk_id": chunk_id,
        "embedding_id": embedding_id,
        "space_id": embedding.space_id,
        "input_format": embedding.input_format,
        **overrides,
    }
    await session.execute(insert(ChunkEmbedding).values(values))


async def _chunk_in_course(
    session: AsyncSession, title: str, source_uri: str = "upload:notes.pdf"
) -> tuple[Course, uuid.UUID]:
    course = await _course(session, title)
    version = await _version(session, course, source_uri=source_uri)
    return course, await _insert_chunk(session, version, _draft(0, 1))


# --- chunk provenance: the application check and the database agree -------------------


@pytest.mark.parametrize("draft", GOOD_CHUNKS.values(), ids=GOOD_CHUNKS.keys())
def test_application_check_accepts_exact_chunks(draft: ChunkDraft) -> None:
    check_chunk_provenance(draft, TEXT, BOUNDS)


@pytest.mark.parametrize("draft", BAD_CHUNKS.values(), ids=BAD_CHUNKS.keys())
def test_application_check_rejects_bad_provenance_before_persistence(draft: ChunkDraft) -> None:
    with pytest.raises(ChunkProvenanceError) as caught:
        check_chunk_provenance(draft, TEXT, BOUNDS)

    assert draft.text not in str(caught.value)  # offsets and ordinals only


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"ordinal": -1}, "ordinal is negative"),
        ({"estimated_tokens": 0}, "estimated_tokens"),
        ({"start_offset": 5, "end_offset": 5, "text": ""}, "not ordered"),
    ],
)
def test_application_check_mirrors_the_row_checks(fields: dict[str, Any], message: str) -> None:
    draft = ChunkDraft(**{**_fields(_draft(0, 0)), **fields})

    with pytest.raises(ChunkProvenanceError, match=message):
        check_chunk_provenance(draft, TEXT, BOUNDS)


def test_spans_without_offsets_cannot_anchor_chunks() -> None:
    span = SourceSpan(ordinal=4, start_offset=None, end_offset=None)

    with pytest.raises(ChunkProvenanceError, match="span 4 has no offsets"):
        span_bounds([span])


async def test_database_accepts_exact_chunks(db_session: AsyncSession) -> None:
    version = await _version(db_session, await _course(db_session))

    for ordinal, draft in enumerate(GOOD_CHUNKS.values()):
        await _insert_chunk(db_session, version, draft, ordinal=ordinal)

    count = select(func.count()).select_from(Chunk)
    assert await db_session.scalar(count) == len(GOOD_CHUNKS)


@pytest.mark.parametrize("draft", BAD_CHUNKS.values(), ids=BAD_CHUNKS.keys())
async def test_trigger_rejects_the_same_bad_provenance(
    db_session: AsyncSession, draft: ChunkDraft
) -> None:
    course = await _course(db_session)
    version = await _version(db_session, course)
    # A sibling version with a fourth span: its ordinal 3 is not this version's.
    await _version(db_session, course, (*EXCERPTS, "Extra page."), source_uri="upload:other.pdf")

    with pytest.raises(IntegrityError, match="chunks_provenance") as caught:
        await _insert_chunk(db_session, version, draft)

    assert draft.text not in str(caught.value.orig)


async def test_trigger_rejects_a_missing_version(db_session: AsyncSession) -> None:
    version = ResourceVersion(id=uuid.uuid4())  # never inserted

    with pytest.raises(IntegrityError, match="chunks_provenance: resource version .* not exist"):
        await _insert_chunk(db_session, version, _draft(0, 0))


async def test_trigger_also_checks_updates(db_session: AsyncSession) -> None:
    version = await _version(db_session, await _course(db_session))
    chunk_id = await _insert_chunk(db_session, version, _draft(0, 0))

    with pytest.raises(IntegrityError, match="chunks_provenance: end"):
        await db_session.execute(
            Chunk.__table__.update()
            .where(Chunk.id == chunk_id)
            .values(end_offset=B1.end_offset, text=TEXT[: B1.end_offset])
            .values(text_hash=text_hash(TEXT[: B1.end_offset]))
        )


@pytest.mark.parametrize(
    ("overrides", "constraint"),
    [
        ({"text_hash": "0" * 64}, "ck_chunks_text_hash_matches_text"),
        ({"estimated_tokens": 0}, "ck_chunks_estimated_tokens_positive"),
        ({"chunker_version": "  "}, "ck_chunks_chunker_version_nonblank"),
        ({"ordinal": -1}, "ck_chunks_ordinal_nonnegative"),
    ],
    ids=["text-hash", "tokens", "chunker-version", "ordinal"],
)
async def test_row_checks_reject_invalid_chunks(
    db_session: AsyncSession, overrides: dict[str, Any], constraint: str
) -> None:
    version = await _version(db_session, await _course(db_session))

    with pytest.raises(IntegrityError, match=constraint):
        await _insert_chunk(db_session, version, _draft(0, 0), **overrides)


async def test_chunk_ordinal_is_unique_per_version_and_chunker(db_session: AsyncSession) -> None:
    version = await _version(db_session, await _course(db_session))
    await _insert_chunk(db_session, version, _draft(0, 0))
    await _insert_chunk(db_session, version, _draft(0, 0), chunker_version="span-pack/v2")

    with pytest.raises(IntegrityError, match="uq_chunks_version_chunker_ordinal"):
        await _insert_chunk(db_session, version, _draft(1, 1))


# --- embedding spaces and embeddings -----------------------------------------------------


@pytest.mark.parametrize(
    ("fields", "constraint"),
    [
        ({"provider": ""}, "ck_embedding_spaces_provider_valid"),
        ({"provider": " fake"}, "ck_embedding_spaces_provider_valid"),
        ({"model": "m" * 257}, "ck_embedding_spaces_model_valid"),
        ({"model": "fake/model "}, "ck_embedding_spaces_model_valid"),
        ({"space_version": "V1"}, "ck_embedding_spaces_space_version_valid"),
        ({"space_version": ""}, "ck_embedding_spaces_space_version_valid"),
        ({"dimensions": 0}, "ck_embedding_spaces_dimensions_in_range"),
        ({"dimensions": 16_001}, "ck_embedding_spaces_dimensions_in_range"),
    ],
)
async def test_invalid_spaces_are_rejected(
    db_session: AsyncSession, fields: dict[str, Any], constraint: str
) -> None:
    values = {"provider": "fake", "model": "fake/model", "dimensions": 4, "space_version": "v1"}
    with pytest.raises(IntegrityError, match=constraint):
        await db_session.execute(insert(EmbeddingSpaceRecord).values({**values, **fields}))


async def test_space_identity_is_unique(db_session: AsyncSession) -> None:
    await _space(db_session)
    await _space(db_session, space_version="v2")  # a new revision is a new space

    with pytest.raises(IntegrityError, match="uq_embedding_spaces_identity"):
        await _space(db_session)


async def test_vectors_of_different_dimensions_coexist_and_search_stays_in_one_space(
    db_session: AsyncSession,
) -> None:
    plane, solid = await _space(db_session, 2), await _space(db_session, 3, "v-3d")
    await _embedding(db_session, plane, "a", vector=[0.6, 0.8])
    near = await _embedding(db_session, solid, "near", vector=[1.0, 0.1, 0.0])
    far = await _embedding(db_session, solid, "far", vector=[0.0, 0.0, 1.0])
    query = [1.0, 0.0, 0.0]

    distance = Embedding.vector.cosine_distance(query)
    rows = (
        await db_session.execute(
            select(Embedding.id, (1 - distance).label("score"))
            .where(Embedding.space_id == solid.id)
            .order_by(distance)
        )
    ).all()

    assert [row.id for row in rows] == [near, far]
    assert rows[0].score == pytest.approx(0.995, abs=1e-3)
    assert rows[1].score == pytest.approx(0.0, abs=1e-6)


def _float32(value: float) -> float:
    return struct.unpack("f", struct.pack("f", value))[0]


async def test_4096_dimension_vector_round_trips_through_asyncpg(db_session: AsyncSession) -> None:
    space = await _space(db_session, 4096)
    rng = random.Random(4096)
    vector = [rng.uniform(-1.0, 1.0) for _ in range(4096)]
    embedding_id = await _embedding(db_session, space, "long", vector=vector)

    stored = await db_session.scalar(select(Embedding.vector).where(Embedding.id == embedding_id))
    self_distance = await db_session.scalar(
        select(Embedding.vector.cosine_distance(vector)).where(Embedding.id == embedding_id)
    )

    assert isinstance(stored, list) and len(stored) == 4096
    assert all(isinstance(value, float) for value in stored)
    # pgvector stores float32: equal after rounding both sides to float32.
    assert [_float32(v) for v in stored] == [_float32(v) for v in vector]
    assert self_distance == pytest.approx(0.0, abs=1e-6)


@pytest.mark.parametrize(
    ("overrides", "constraint"),
    [
        ({"vector": [1.0, 0.0, 0.0]}, "ck_embeddings_vector_dims_match"),
        ({"vector": [0.0, 0.0, 0.0, 0.0]}, "ck_embeddings_vector_nonzero"),
        ({"input_hash": "ABC"}, "ck_embeddings_input_hash_sha256_hex"),
        ({"input_hash": "A" * 64}, "ck_embeddings_input_hash_sha256_hex"),
        ({"input_format": "document"}, "ck_embeddings_input_format_versioned"),
        ({"input_format": "summary/v1"}, "ck_embeddings_input_format_versioned"),
        ({"input_format": "document/v0"}, "ck_embeddings_input_format_versioned"),
        ({"dimensions": 5, "vector": [1.0] * 5}, "fk_embeddings_space"),
    ],
    ids=[
        "wrong-dimensions",
        "zero-vector",
        "short-hash",
        "uppercase-hash",
        "unversioned-format",
        "unknown-format",
        "format-v0",
        "dimensions-not-the-space",
    ],
)
async def test_invalid_embeddings_are_rejected(
    db_session: AsyncSession, overrides: dict[str, Any], constraint: str
) -> None:
    space = await _space(db_session)

    with pytest.raises(IntegrityError, match=constraint):
        await _embedding(db_session, space, "x", **overrides)


async def test_cache_key_is_unique_per_space_format_and_input(db_session: AsyncSession) -> None:
    space, other_space = await _space(db_session), await _space(db_session, space_version="v2")
    await _embedding(db_session, space, "same input")
    await _embedding(db_session, other_space, "same input")  # another space
    await _embedding(db_session, space, "same input", input_format=QUERY_INPUT_FORMAT)

    with pytest.raises(IntegrityError, match="uq_embeddings_cache_key"):
        await _embedding(db_session, space, "same input")


# --- chunk <-> embedding links ------------------------------------------------------------


async def test_one_embedding_is_reused_by_chunks_of_different_courses(
    db_session: AsyncSession,
) -> None:
    _, chunk_a = await _chunk_in_course(db_session, "Course A")
    _, chunk_b = await _chunk_in_course(db_session, "Course B")
    space = await _space(db_session)
    shared = await _embedding(db_session, space, TEXT[: B1.end_offset])

    await _link(db_session, chunk_a, shared)
    await _link(db_session, chunk_b, shared)

    links = select(func.count()).where(ChunkEmbedding.embedding_id == shared)
    assert await db_session.scalar(links) == 2
    assert await db_session.scalar(select(func.count()).select_from(Embedding)) == 1


async def test_one_chunk_has_vectors_in_several_spaces(db_session: AsyncSession) -> None:
    _, chunk = await _chunk_in_course(db_session, "Course A")
    small, large = await _space(db_session, 4), await _space(db_session, 8, "v-large")

    await _link(db_session, chunk, await _embedding(db_session, small, "t"))
    await _link(db_session, chunk, await _embedding(db_session, large, "t"))

    spaces = select(func.count(ChunkEmbedding.space_id.distinct())).where(
        ChunkEmbedding.chunk_id == chunk
    )
    assert await db_session.scalar(spaces) == 2


async def test_duplicate_link_is_rejected(db_session: AsyncSession) -> None:
    _, chunk = await _chunk_in_course(db_session, "Course A")
    embedding = await _embedding(db_session, await _space(db_session), "t")
    await _link(db_session, chunk, embedding)

    with pytest.raises(IntegrityError, match="pk_chunk_embeddings"):
        await _link(db_session, chunk, embedding)


async def test_second_vector_for_the_same_chunk_space_and_format_is_rejected(
    db_session: AsyncSession,
) -> None:
    _, chunk = await _chunk_in_course(db_session, "Course A")
    space = await _space(db_session)
    await _link(db_session, chunk, await _embedding(db_session, space, "t"))

    with pytest.raises(IntegrityError, match="uq_chunk_embeddings_chunk_space_format"):
        await _link(db_session, chunk, await _embedding(db_session, space, "other text"))


async def test_link_must_copy_the_embeddings_own_space(db_session: AsyncSession) -> None:
    _, chunk = await _chunk_in_course(db_session, "Course A")
    space, other = await _space(db_session), await _space(db_session, space_version="v2")
    embedding = await _embedding(db_session, space, "t")

    with pytest.raises(IntegrityError, match="fk_chunk_embeddings_embedding"):
        await _link(db_session, chunk, embedding, space_id=other.id)


async def test_query_embedding_cannot_be_linked_to_a_chunk(db_session: AsyncSession) -> None:
    _, chunk = await _chunk_in_course(db_session, "Course A")
    query = await _embedding(
        db_session, await _space(db_session), "q", input_format=QUERY_INPUT_FORMAT
    )

    with pytest.raises(IntegrityError, match="ck_chunk_embeddings_document_format_only"):
        await _link(db_session, chunk, query)


async def test_query_embedding_cannot_be_linked_by_claiming_a_document_format(
    db_session: AsyncSession,
) -> None:
    _, chunk = await _chunk_in_course(db_session, "Course A")
    query = await _embedding(
        db_session, await _space(db_session), "q", input_format=QUERY_INPUT_FORMAT
    )

    with pytest.raises(IntegrityError, match="fk_chunk_embeddings_embedding"):
        await _link(db_session, chunk, query, input_format=DOCUMENT_INPUT_FORMAT)


async def test_a_future_document_format_can_be_linked(db_session: AsyncSession) -> None:
    _, chunk = await _chunk_in_course(db_session, "Course A")
    space = await _space(db_session)
    await _link(db_session, chunk, await _embedding(db_session, space, "t"))

    v2 = await _embedding(db_session, space, "t2", input_format="document/v2")
    await _link(db_session, chunk, v2)


# --- delete policy --------------------------------------------------------------------------


async def test_deleting_a_chunk_deletes_its_links_but_keeps_the_vector(
    db_session: AsyncSession,
) -> None:
    _, chunk = await _chunk_in_course(db_session, "Course A")
    embedding = await _embedding(db_session, await _space(db_session), "t")
    await _link(db_session, chunk, embedding)

    await db_session.execute(delete(Chunk).where(Chunk.id == chunk))

    assert await db_session.scalar(select(func.count()).select_from(ChunkEmbedding)) == 0
    assert await db_session.get(Embedding, embedding) is not None


async def test_an_embedding_in_use_cannot_be_deleted(db_session: AsyncSession) -> None:
    _, chunk = await _chunk_in_course(db_session, "Course A")
    embedding = await _embedding(db_session, await _space(db_session), "t")
    await _link(db_session, chunk, embedding)

    with pytest.raises(IntegrityError, match="fk_chunk_embeddings_embedding"):
        await db_session.execute(delete(Embedding).where(Embedding.id == embedding))


def _model_call(**fields: Any) -> ModelCall:
    now = datetime.now(UTC)
    values: dict[str, Any] = {
        "call_kind": ModelCallKind.EMBEDDING,
        "provider": "fake",
        "model": "fake/model",
        "prompt_version": DOCUMENT_INPUT_FORMAT,
        "output_schema": "vector[4]",
        "status": ModelCallStatus.SUCCEEDED,
        "attempts": 1,
        "latency_ms": 3,
        "output_summary": '{"vectors": 1}',
        "started_at": now,
        "finished_at": now,
    }
    return ModelCall(**(values | fields))


async def test_pruning_the_model_call_keeps_the_vector(db_session: AsyncSession) -> None:
    call = _model_call()
    db_session.add(call)
    await db_session.flush()
    embedding = await _embedding(db_session, await _space(db_session), "t", model_call_id=call.id)

    await db_session.execute(delete(ModelCall).where(ModelCall.id == call.id))

    kept = await db_session.scalar(select(Embedding.model_call_id).where(Embedding.id == embedding))
    assert kept is None


# --- ModelCall.call_kind ----------------------------------------------------------------------


async def test_embedding_calls_have_no_role_and_no_repairs(db_session: AsyncSession) -> None:
    db_session.add(_model_call())
    await db_session.flush()


@pytest.mark.parametrize(
    "fields",
    [{"model_role": ModelRole.FAST}, {"attempts": 2, "repair_rounds": 1}],
    ids=["role", "repairs"],
)
async def test_embedding_call_with_role_or_repairs_is_rejected(
    db_session: AsyncSession, fields: dict[str, Any]
) -> None:
    db_session.add(_model_call(**fields))

    with pytest.raises(IntegrityError, match="ck_model_calls_embedding_has_no_role"):
        await db_session.flush()


# --- course scoping through Chunk -> ResourceVersion -> Resource -> Course ---------------


async def test_course_scoped_search_never_returns_another_courses_chunk(
    db_session: AsyncSession,
) -> None:
    """The nearest vector belongs to course B, and a stale version of course A
    holds the second nearest: a course-A search returns only A's current chunk."""
    course_a, chunk_a = await _chunk_in_course(db_session, "Course A")
    _, chunk_b = await _chunk_in_course(db_session, "Course B")
    resource_a = await db_session.scalar(select(Resource).where(Resource.course_id == course_a.id))
    stale = await _version(
        db_session, course_a, ("Old notes page.",), resource=resource_a, current=False
    )
    chunk_stale = await _insert_chunk(
        db_session, stale, ChunkDraft(0, 0, 15, 0, 0, "Old notes page.", 4)
    )
    space = await _space(db_session, 3)
    query = [1.0, 0.0, 0.0]
    for chunk, vector in (
        (chunk_b, [1.0, 0.0, 0.0]),  # identical to the query
        (chunk_stale, [1.0, 0.2, 0.0]),
        (chunk_a, [0.0, 1.0, 1.0]),  # far
    ):
        await _link(
            db_session, chunk, await _embedding(db_session, space, str(chunk), vector=vector)
        )

    distance = Embedding.vector.cosine_distance(query)
    statement = (
        select(Chunk.id)
        .join(ChunkEmbedding, ChunkEmbedding.chunk_id == Chunk.id)
        .join(Embedding, Embedding.id == ChunkEmbedding.embedding_id)
        .join(ResourceVersion, ResourceVersion.id == Chunk.resource_version_id)
        .join(Resource, Resource.current_version_id == ResourceVersion.id)
        .where(
            Resource.course_id == course_a.id,
            ChunkEmbedding.space_id == space.id,
            ChunkEmbedding.input_format == DOCUMENT_INPUT_FORMAT,
        )
        .order_by(distance)
        .limit(5)
    )

    assert list(await db_session.scalars(statement)) == [chunk_a]
