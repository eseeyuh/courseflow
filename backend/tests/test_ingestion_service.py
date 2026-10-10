# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Persistence of ingested sources against the real test database.

Uses committed sessions (tables truncated around each test) because
savepoints, row locks and concurrent imports only behave realistically
with real transactions.
"""

import asyncio
import logging
import threading
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import URL, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.config import Settings
from app.db.models import Course, Institution, Resource, ResourceVersion, SourceSpan
from app.domain.enums import BlockKind, IngestionErrorCategory, LmsType, MediaType, ResourceType
from app.ingestion import service
from app.ingestion.errors import IngestionError
from app.ingestion.guards import IngestionLimits
from app.ingestion.hashing import sha256_hex
from app.ingestion.schemas import ParsedDocument, RawSource
from app.ingestion.service import (
    CourseNotFoundError,
    IngestFailure,
    IngestItem,
    IngestResult,
    SpanInvariantError,
    ingest_file,
    ingest_files,
)
from app.ingestion.storage import (
    LocalRawObjectStore,
    RawStoreError,
    RawStoreUnavailableError,
    RawStoreWriteError,
)
from tests.fixtures.ingestion.make_fixtures import FIXTURE_DIR

pytestmark = pytest.mark.anyio

LIMITS = IngestionLimits.from_settings(
    Settings(_env_file=None, database_url="postgresql+asyncpg://u:p@127.0.0.1/db")  # type: ignore[call-arg, arg-type]
)
BRIEF = (FIXTURE_DIR / "brief.docx").read_bytes()
LECTURE = (FIXTURE_DIR / "lecture.pptx").read_bytes()
HANDBOOK = (FIXTURE_DIR / "handbook.pdf").read_bytes()
PAGE = (FIXTURE_DIR / "module-page.html").read_bytes()
URI = "upload:brief"


def _source(content: bytes, name: str = "brief.docx", uri: str = URI) -> RawSource:
    return RawSource(content=content, display_name=name, source_uri=uri)


def _object_path(root: Path, content: bytes) -> Path:
    digest = sha256_hex(content)
    return root / "sha256" / digest[:2] / digest[2:4] / digest


@pytest.fixture
def raw_root(tmp_path: Path) -> Path:
    root = tmp_path / "raw"
    root.mkdir()
    return root


@pytest.fixture
def store(raw_root: Path) -> LocalRawObjectStore:
    return LocalRawObjectStore(raw_root)


@pytest.fixture
async def course_id(committed_session: AsyncSession) -> uuid.UUID:
    course = Course(
        institution=Institution(name="Northbridge University", lms_type=LmsType.UPLOAD),
        title="Data Systems",
        external_id="DS101",
    )
    committed_session.add(course)
    await committed_session.commit()
    return course.id


async def _import(
    session: AsyncSession,
    store: Any,
    course_id: uuid.UUID,
    source: RawSource,
    resource_type: ResourceType = ResourceType.BRIEF,
) -> IngestResult:
    result = await ingest_file(
        session,
        store,
        course_id=course_id,
        resource_type=resource_type,
        source=source,
        limits=LIMITS,
    )
    await session.commit()
    return result


async def _count(session: AsyncSession, model: type) -> int:
    return int(await session.scalar(select(func.count()).select_from(model)) or 0)


async def _versions(session: AsyncSession) -> list[ResourceVersion]:
    session.expire_all()
    return list(
        await session.scalars(select(ResourceVersion).order_by(ResourceVersion.version_number))
    )


# --- first import ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("content", "name", "media_type"),
    [
        (BRIEF, "brief.docx", MediaType.DOCX),
        (LECTURE, "lecture.pptx", MediaType.PPTX),
        (HANDBOOK, "handbook.pdf", MediaType.PDF),
        (PAGE, "module-page.html", MediaType.HTML),
    ],
    ids=["docx", "pptx", "pdf", "html"],
)
async def test_first_import_persists_version_spans_and_raw_object(
    committed_session: AsyncSession,
    store: LocalRawObjectStore,
    course_id: uuid.UUID,
    content: bytes,
    name: str,
    media_type: MediaType,
) -> None:
    result = await _import(committed_session, store, course_id, _source(content, name))

    assert result.created and result.resource_created and result.version_number == 1
    assert result.content_hash == sha256_hex(content)
    assert result.raw_object_ref == f"sha256:{result.content_hash}"
    assert store.open(result.raw_object_ref) == content  # original bytes, verified

    committed_session.expire_all()
    resource = await committed_session.scalar(select(Resource))
    version = await committed_session.scalar(select(ResourceVersion))
    spans = list(await committed_session.scalars(select(SourceSpan).order_by(SourceSpan.ordinal)))
    assert resource is not None and version is not None
    assert resource.current_version_id == version.id == result.version_id
    assert resource.source_uri == URI and resource.title == name
    assert (version.media_type, version.byte_size, version.display_name) == (
        media_type,
        len(content),
        name,
    )
    assert version.text_hash == sha256_hex(version.extracted_text.encode())
    assert [s.ordinal for s in spans] == list(range(result.span_count))
    # Exact location survives persistence: offsets index the stored text.
    for span in spans:
        assert span.start_offset is not None and span.end_offset is not None
        assert version.extracted_text[span.start_offset : span.end_offset] == span.excerpt


async def test_persisted_locators_match_the_parser(
    committed_session: AsyncSession, store: LocalRawObjectStore, course_id: uuid.UUID
) -> None:
    await _import(committed_session, store, course_id, _source(LECTURE, "lecture.pptx"))
    committed_session.expire_all()
    spans = list(await committed_session.scalars(select(SourceSpan).order_by(SourceSpan.ordinal)))

    notes = [s for s in spans if s.block_kind is BlockKind.SLIDE_NOTES]
    assert [(s.slide_number, s.excerpt) for s in notes] == [
        (2, "Mention the lab on Thursday. PPTX-S2-NOTES")
    ]
    assert sorted({s.slide_number for s in spans}) == [1, 2, 3, 4, 5, 7]


# --- unchanged bytes ------------------------------------------------------------------


@pytest.fixture
def no_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*args: object, **kwargs: object) -> None:
        raise AssertionError("an unchanged file must not be parsed")

    monkeypatch.setattr(service, "parse_source", fail)


async def _assert_one_version(session: AsyncSession, span_count: int) -> None:
    assert len(await _versions(session)) == 1
    assert await _count(session, SourceSpan) == span_count


async def test_unchanged_bytes_create_nothing_even_under_a_new_file_name(
    committed_session: AsyncSession,
    store: LocalRawObjectStore,
    course_id: uuid.UUID,
    request: pytest.FixtureRequest,
) -> None:
    first = await _import(committed_session, store, course_id, _source(BRIEF, "brief.docx"))
    request.getfixturevalue("no_parsing")

    again = await _import(committed_session, store, course_id, _source(BRIEF, "Brief FINAL.docx"))

    assert not again.created and not again.resource_created
    assert (again.version_id, again.version_number) == (first.version_id, 1)
    assert (again.span_count, again.spans_by_kind) == (first.span_count, first.spans_by_kind)
    await _assert_one_version(committed_session, first.span_count)
    versions = await _versions(committed_session)
    assert versions[0].display_name == "brief.docx"  # the new name created nothing


@pytest.mark.parametrize("damage", ["deleted", "corrupted"])
async def test_unchanged_import_restores_a_damaged_raw_object(
    committed_session: AsyncSession,
    store: LocalRawObjectStore,
    raw_root: Path,
    course_id: uuid.UUID,
    request: pytest.FixtureRequest,
    damage: str,
) -> None:
    first = await _import(committed_session, store, course_id, _source(BRIEF))
    path = _object_path(raw_root, BRIEF)
    if damage == "deleted":
        path.unlink()
    else:
        path.write_bytes(b"corrupted")
    request.getfixturevalue("no_parsing")

    again = await _import(committed_session, store, course_id, _source(BRIEF))

    assert not again.created and again.version_id == first.version_id
    assert store.open(first.raw_object_ref) == BRIEF  # restored / repaired
    await _assert_one_version(committed_session, first.span_count)


class _FailingStore:
    """A raw store whose writes fail, e.g. the volume is read-only or full."""

    def __init__(self, inner: LocalRawObjectStore, fail_on_call: int = 1) -> None:
        self._inner, self._fail_on_call, self.calls = inner, fail_on_call, 0

    def put(self, content: bytes) -> str:
        self.calls += 1
        if self.calls >= self._fail_on_call:
            raise RawStoreWriteError("raw object could not be written")
        return self._inner.put(content)

    def open(self, ref: str) -> bytes:
        return self._inner.open(ref)

    def exists(self, ref: str) -> bool:
        return self._inner.exists(ref)


async def test_unchanged_import_fails_when_the_raw_object_cannot_be_verified(
    committed_session: AsyncSession,
    store: LocalRawObjectStore,
    raw_root: Path,
    course_id: uuid.UUID,
) -> None:
    first = await _import(committed_session, store, course_id, _source(BRIEF))
    _object_path(raw_root, BRIEF).unlink()

    with pytest.raises(RawStoreError):
        await ingest_file(
            committed_session,
            _FailingStore(store),
            course_id=course_id,
            resource_type=ResourceType.BRIEF,
            source=_source(BRIEF),
            limits=LIMITS,
        )
    await committed_session.rollback()
    await _assert_one_version(committed_session, first.span_count)


async def test_unchanged_import_fails_when_the_raw_object_is_unreadable(
    committed_session: AsyncSession,
    store: LocalRawObjectStore,
    raw_root: Path,
    course_id: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    first = await _import(committed_session, store, course_id, _source(BRIEF))
    target = _object_path(raw_root, BRIEF)
    real_open = Path.open

    def guarded_open(self: Path, *args: Any, **kwargs: Any) -> Any:
        if self == target:
            raise PermissionError(13, "Permission denied")
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_open)

    with (
        caplog.at_level(logging.INFO, logger="app.ingestion"),
        pytest.raises(RawStoreUnavailableError),
    ):
        await ingest_file(
            committed_session,
            store,
            course_id=course_id,
            resource_type=ResourceType.BRIEF,
            source=_source(BRIEF),
            limits=LIMITS,
        )
    monkeypatch.undo()

    assert "ingestion.unchanged" not in [r.getMessage() for r in caplog.records]
    assert target.read_bytes() == BRIEF  # left untouched, not "repaired"
    await committed_session.rollback()
    await _assert_one_version(committed_session, first.span_count)


# --- changed bytes and reverts --------------------------------------------------------


async def test_changed_bytes_create_the_next_version_and_move_current(
    committed_session: AsyncSession, store: LocalRawObjectStore, course_id: uuid.UUID
) -> None:
    v1 = await _import(committed_session, store, course_id, _source(BRIEF))
    v2 = await _import(committed_session, store, course_id, _source(PAGE, "brief.html"))

    assert v2.created and not v2.resource_created and v2.version_number == 2
    assert v2.resource_id == v1.resource_id
    committed_session.expire_all()
    resource = await committed_session.scalar(select(Resource))
    assert resource is not None and resource.current_version_id == v2.version_id
    # v1 and its spans are kept: versions are immutable history.
    assert (
        await committed_session.scalar(
            select(func.count()).where(SourceSpan.resource_version_id == v1.version_id)
        )
        == v1.span_count
    )


async def test_revert_to_earlier_bytes_is_a_new_version(
    committed_session: AsyncSession, store: LocalRawObjectStore, course_id: uuid.UUID
) -> None:
    a1 = await _import(committed_session, store, course_id, _source(BRIEF))
    await _import(committed_session, store, course_id, _source(PAGE, "brief.html"))
    a3 = await _import(committed_session, store, course_id, _source(BRIEF))

    assert a3.created and a3.version_number == 3
    assert a3.content_hash == a1.content_hash and a3.version_id != a1.version_id
    assert [v.version_number for v in await _versions(committed_session)] == [1, 2, 3]


async def test_reimport_never_reclassifies_the_resource(
    committed_session: AsyncSession,
    store: LocalRawObjectStore,
    course_id: uuid.UUID,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await _import(committed_session, store, course_id, _source(BRIEF), ResourceType.BRIEF)
    with caplog.at_level(logging.WARNING, logger="app.ingestion.service"):
        await _import(
            committed_session, store, course_id, _source(PAGE, "b.html"), ResourceType.LECTURE
        )

    committed_session.expire_all()
    resource = await committed_session.scalar(select(Resource))
    assert resource is not None and resource.type is ResourceType.BRIEF
    assert "ingestion.resource_type_mismatch" in [r.getMessage() for r in caplog.records]


# --- failures leave no rows -------------------------------------------------------------


@pytest.mark.parametrize(
    ("content", "name", "category"),
    [
        (b"not a pdf", "x.pdf", IngestionErrorCategory.CORRUPT_FILE),
        ((FIXTURE_DIR / "scanned.pdf").read_bytes(), "x.pdf", IngestionErrorCategory.EMPTY_TEXT),
        (b"MZ", "setup.exe", IngestionErrorCategory.UNSUPPORTED_TYPE),
    ],
    ids=["corrupt", "empty", "unsupported"],
)
async def test_rejected_input_writes_no_rows_and_no_raw_object(
    committed_session: AsyncSession,
    store: LocalRawObjectStore,
    raw_root: Path,
    course_id: uuid.UUID,
    content: bytes,
    name: str,
    category: IngestionErrorCategory,
) -> None:
    with pytest.raises(IngestionError) as excinfo:
        await _import(committed_session, store, course_id, _source(content, name))
    assert excinfo.value.category is category

    await committed_session.commit()
    assert await _count(committed_session, Resource) == 0
    assert list(raw_root.rglob("*")) == []


async def test_database_failure_after_raw_write_leaves_no_rows_but_keeps_the_object(
    committed_session: AsyncSession,
    store: LocalRawObjectStore,
    raw_root: Path,
    course_id: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = service._create_version

    async def create_then_fail(*args: Any, **kwargs: Any) -> ResourceVersion:
        await original(*args, **kwargs)  # rows flushed inside the savepoint
        raise RuntimeError("database failure after the rows were written")

    monkeypatch.setattr(service, "_create_version", create_then_fail)

    with pytest.raises(RuntimeError):
        await ingest_file(
            committed_session,
            store,
            course_id=course_id,
            resource_type=ResourceType.BRIEF,
            source=_source(BRIEF),
            limits=LIMITS,
        )
    await committed_session.commit()  # the outer transaction is still usable

    assert await _count(committed_session, Resource) == 0
    assert await _count(committed_session, ResourceVersion) == 0
    assert await _count(committed_session, SourceSpan) == 0
    assert _object_path(raw_root, BRIEF).read_bytes() == BRIEF  # harmless orphan


async def test_offsets_that_do_not_reproduce_the_excerpt_are_refused_before_writing(
    committed_session: AsyncSession,
    store: LocalRawObjectStore,
    raw_root: Path,
    course_id: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = ParsedDocument.block_offsets

    def shifted(self: ParsedDocument) -> tuple[tuple[int, int], ...]:
        return tuple((start + 1, end + 1) for start, end in real(self))

    monkeypatch.setattr(ParsedDocument, "block_offsets", shifted)

    with pytest.raises(SpanInvariantError):
        await _import(committed_session, store, course_id, _source(BRIEF))
    await committed_session.rollback()
    assert await _count(committed_session, Resource) == 0
    assert list(raw_root.rglob("*")) == []


async def test_missing_course_fails_before_any_write(
    committed_session: AsyncSession, store: LocalRawObjectStore, raw_root: Path
) -> None:
    with pytest.raises(CourseNotFoundError):
        await _import(committed_session, store, uuid.uuid4(), _source(BRIEF))
    assert list(raw_root.rglob("*")) == []


# --- batches ----------------------------------------------------------------------------


def _items(*sources: RawSource) -> list[IngestItem]:
    return [IngestItem(source=s, resource_type=ResourceType.READING) for s in sources]


async def test_batch_continues_past_a_bad_file(
    committed_session: AsyncSession, store: LocalRawObjectStore, course_id: uuid.UUID
) -> None:
    outcomes = await ingest_files(
        committed_session,
        store,
        course_id=course_id,
        items=_items(
            _source(BRIEF, "brief.docx", "upload:a"),
            _source(b"not a pdf", "broken.pdf", "upload:b"),
            _source(LECTURE, "lecture.pptx", "upload:c"),
        ),
        limits=LIMITS,
    )
    await committed_session.commit()

    assert [type(o) for o in outcomes] == [IngestResult, IngestFailure, IngestResult]
    failure = outcomes[1]
    assert isinstance(failure, IngestFailure)
    assert (failure.source_uri, failure.category) == ("upload:b", "corrupt_file")
    uris = await committed_session.scalars(select(Resource.source_uri).order_by("source_uri"))
    assert list(uris) == ["upload:a", "upload:c"]


async def test_infrastructure_failure_stops_the_batch_and_nothing_is_committed(
    committed_session: AsyncSession, store: LocalRawObjectStore, course_id: uuid.UUID
) -> None:
    failing = _FailingStore(store, fail_on_call=2)
    with pytest.raises(RawStoreWriteError):
        await ingest_files(
            committed_session,
            failing,
            course_id=course_id,
            items=_items(
                _source(BRIEF, "brief.docx", "upload:a"),
                _source(LECTURE, "lecture.pptx", "upload:b"),
                _source(PAGE, "page.html", "upload:c"),
            ),
            limits=LIMITS,
        )
    assert failing.calls == 2  # stopped at the failing file; the third never ran
    await committed_session.rollback()  # the caller's decision
    assert await _count(committed_session, Resource) == 0


# --- concurrency ------------------------------------------------------------------------


async def _wait_for_lock_waiter(url: URL, timeout: float = 10.0) -> None:
    """Return once some backend is waiting on a lock (the second importer)."""
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            deadline = asyncio.get_running_loop().time() + timeout
            while asyncio.get_running_loop().time() < deadline:
                waiting = await conn.scalar(
                    text(
                        "SELECT count(*) FROM pg_stat_activity "
                        "WHERE datname = current_database() AND wait_event_type = 'Lock'"
                    )
                )
                if waiting:
                    return
                await asyncio.sleep(0.05)
    finally:
        await engine.dispose()
    raise AssertionError("the second import never waited for the first one's lock")


@pytest.fixture
async def second_session(test_database_url: URL) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(test_database_url)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            yield session
    finally:
        await engine.dispose()


async def test_concurrent_identical_imports_create_exactly_one_version(
    committed_session: AsyncSession,
    second_session: AsyncSession,
    store: LocalRawObjectStore,
    course_id: uuid.UUID,
    test_database_url: URL,
) -> None:
    def ingest(session: AsyncSession) -> Any:
        return ingest_file(
            session,
            store,
            course_id=course_id,
            resource_type=ResourceType.BRIEF,
            source=_source(BRIEF),
            limits=LIMITS,
        )

    first = await ingest(committed_session)  # written, NOT committed yet
    second_task = asyncio.create_task(ingest(second_session))
    await _wait_for_lock_waiter(test_database_url)
    assert not second_task.done()  # blocked on the uncommitted resource row

    await committed_session.commit()
    second = await second_task
    await second_session.commit()

    assert first.created and not second.created
    assert second.version_id == first.version_id
    assert len(await _versions(committed_session)) == 1


async def test_resource_lock_blocks_a_second_importer_until_the_first_finishes(
    committed_session: AsyncSession,
    second_session: AsyncSession,
    store: LocalRawObjectStore,
    course_id: uuid.UUID,
    test_database_url: URL,
) -> None:
    """The row lock alone serialises importers of one resource, even before the
    first importer has changed anything (no version written, no row updated)."""
    await _import(committed_session, store, course_id, _source(BRIEF))  # resource exists

    async def lock(session: AsyncSession) -> Resource:
        resource, created = await service._lock_or_create_resource(
            session, course_id, ResourceType.BRIEF, _source(BRIEF), None
        )
        assert not created
        return resource

    await lock(committed_session)  # holds the row lock; nothing updated
    second_task = asyncio.create_task(lock(second_session))
    await _wait_for_lock_waiter(test_database_url)
    assert not second_task.done()

    await committed_session.rollback()  # releases the lock without any change
    await second_task
    await second_session.rollback()


async def test_concurrent_different_imports_become_consecutive_versions(
    committed_session: AsyncSession,
    second_session: AsyncSession,
    store: LocalRawObjectStore,
    course_id: uuid.UUID,
    test_database_url: URL,
) -> None:
    await _import(committed_session, store, course_id, _source(BRIEF))  # v1, committed

    first = await ingest_file(
        committed_session,
        store,
        course_id=course_id,
        resource_type=ResourceType.BRIEF,
        source=_source(PAGE, "b.html"),
        limits=LIMITS,
    )  # holds the row lock
    second_task = asyncio.create_task(
        ingest_file(
            second_session,
            store,
            course_id=course_id,
            resource_type=ResourceType.BRIEF,
            source=_source(LECTURE, "b.pptx"),
            limits=LIMITS,
        )
    )
    await _wait_for_lock_waiter(test_database_url)
    await committed_session.commit()
    second = await second_task
    await second_session.commit()

    assert (first.version_number, second.version_number) == (2, 3)
    assert [v.version_number for v in await _versions(committed_session)] == [1, 2, 3]


class _GatedStore:
    """Pauses inside put() until released, to interleave two imports exactly."""

    def __init__(self, inner: LocalRawObjectStore) -> None:
        self._inner = inner
        self.entered = threading.Event()
        self.release = threading.Event()

    def put(self, content: bytes) -> str:
        self.entered.set()
        assert self.release.wait(timeout=10), "test never released the gated put"
        return self._inner.put(content)

    def open(self, ref: str) -> bytes:
        return self._inner.open(ref)

    def exists(self, ref: str) -> bool:
        return self._inner.exists(ref)


async def test_unchanged_reports_the_version_it_observed_even_if_a_newer_one_commits(
    committed_session: AsyncSession,
    second_session: AsyncSession,
    store: LocalRawObjectStore,
    course_id: uuid.UUID,
) -> None:
    """Intended semantics of created=False under concurrent writes.

    Import A observes v1 as current and starts verifying the raw object. Before
    it finishes, import B commits v2. A still returns created=False for v1:
    the two calls overlap, so A is linearised before B. created=False means
    "no new version was needed relative to the version this import observed";
    it does not promise that version is still current when the call returns.
    """
    v1 = await _import(committed_session, store, course_id, _source(BRIEF))
    gated = _GatedStore(store)

    import_a = asyncio.create_task(
        ingest_file(
            committed_session,
            gated,
            course_id=course_id,
            resource_type=ResourceType.BRIEF,
            source=_source(BRIEF),
            limits=LIMITS,
        )
    )
    while not gated.entered.is_set():  # A has read v1 and is inside store.put
        await asyncio.sleep(0.01)

    v2 = await _import(second_session, store, course_id, _source(PAGE, "brief.html"))
    gated.release.set()
    a = await import_a
    await committed_session.commit()

    assert v2.created and v2.version_number == 2
    assert not a.created and (a.version_id, a.version_number) == (v1.version_id, 1)
    committed_session.expire_all()
    resource = await committed_session.scalar(select(Resource))
    assert resource is not None and resource.current_version_id == v2.version_id
    assert len(await _versions(committed_session)) == 2  # A created nothing


# --- logging ----------------------------------------------------------------------------


def _record_fields(record: logging.LogRecord) -> dict[str, Any]:
    standard = vars(logging.LogRecord("", 0, "", 0, "", None, None))
    return {k: v for k, v in vars(record).items() if k not in standard}


async def test_mixed_pdf_logs_page_counts_and_no_document_content(
    committed_session: AsyncSession,
    store: LocalRawObjectStore,
    course_id: uuid.UUID,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO, logger="app.ingestion"):
        await _import(
            committed_session,
            store,
            course_id,
            _source(HANDBOOK, "Jane Doe handbook.pdf", "upload:private/handbook"),
        )

    events = {r.getMessage(): _record_fields(r) for r in caplog.records}
    created = events["ingestion.version_created"]
    assert (created["total_page_count"], created["empty_page_count"]) == (4, 1)
    warning = events["ingestion.pdf_pages_without_text"]
    assert (warning["total_page_count"], warning["empty_page_count"]) == (4, 1)

    logged = " ".join(f"{r.getMessage()} {_record_fields(r)}" for r in caplog.records)
    for secret in ("Jane Doe", "handbook.pdf", "upload:private", "PDF-P1", "Welcome to"):
        assert secret not in logged


async def test_text_only_documents_log_no_page_counts(
    committed_session: AsyncSession,
    store: LocalRawObjectStore,
    course_id: uuid.UUID,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO, logger="app.ingestion"):
        await _import(committed_session, store, course_id, _source(BRIEF))

    messages = [r.getMessage() for r in caplog.records]
    assert "ingestion.pdf_pages_without_text" not in messages
    created = next(r for r in caplog.records if r.getMessage() == "ingestion.version_created")
    assert "total_page_count" not in _record_fields(created)


async def test_rejected_import_is_logged_with_its_category(
    committed_session: AsyncSession,
    store: LocalRawObjectStore,
    course_id: uuid.UUID,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="app.ingestion"), pytest.raises(IngestionError):
        await _import(committed_session, store, course_id, _source(b"not a pdf", "x.pdf"))

    rejected = next(r for r in caplog.records if r.getMessage() == "ingestion.rejected")
    assert _record_fields(rejected)["category"] == "corrupt_file"
