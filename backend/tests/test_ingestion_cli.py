# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""The ingestion CLI, driven through main() against the test database."""

import asyncio
import json
import logging
import os
import shutil
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.db.models import Course, Institution
from app.domain.enums import LmsType
from app.ingestion import cli
from app.ingestion.hashing import sha256_hex
from app.ingestion.service import ConcurrentImportError
from tests.fixtures.ingestion.make_fixtures import FIXTURE_DIR

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def _restore_root_logging() -> Iterator[None]:
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)


@pytest.fixture
def raw_root(tmp_path: Path) -> Path:
    root = tmp_path / "raw"
    root.mkdir()
    return root


@pytest.fixture
def use_settings(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, raw_root: Path
) -> Iterator[Any]:
    """Point the CLI at the test database and a temporary raw store."""

    def apply(**overrides: Any) -> None:
        values = {"raw_storage_dir": raw_root} | overrides
        monkeypatch.setattr(cli, "get_settings", lambda: settings.model_copy(update=values))

    apply()
    yield apply


@pytest.fixture
async def course_id(committed_session: AsyncSession) -> uuid.UUID:
    course = Course(
        institution=Institution(name="Northbridge University", lms_type=LmsType.UPLOAD),
        title="Data Systems",
    )
    committed_session.add(course)
    await committed_session.commit()
    return course.id


async def _run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, dict[str, Any], str]:
    # main() owns its event loop (asyncio.run), like the real process; run it
    # in a worker thread because the test itself is async.
    code = await asyncio.to_thread(cli.main, list(argv))
    out, err = capsys.readouterr()
    lines = out.strip().splitlines()
    assert len(lines) == 1, f"stdout must hold exactly one JSON result, got: {out!r}"
    return code, json.loads(lines[0]), err


async def _import(
    capsys: pytest.CaptureFixture[str], course: uuid.UUID, path: Path, *extra: str
) -> Any:
    return await _run(
        capsys, "import", str(path), "--course-id", str(course), "--type", "brief", *extra
    )


async def test_import_prints_counts_and_hashes_and_logs_to_stderr(
    use_settings: Any, course_id: uuid.UUID, capsys: pytest.CaptureFixture[str]
) -> None:
    path = FIXTURE_DIR / "handbook.pdf"
    code, result, err = await _import(capsys, course_id, path)

    assert code == 0
    assert result["status"] == "created" and result["version_number"] == 1
    assert result["content_hash"] == sha256_hex(path.read_bytes())
    assert result["raw_object_ref"] == f"sha256:{result['content_hash']}"
    assert (result["span_count"], result["spans_by_kind"]) == (3, {"page": 3})
    assert result["media_type"] == "application/pdf"
    events = [json.loads(line)["event"] for line in err.strip().splitlines()]
    assert "ingestion.version_created" in events
    assert "ingestion.pdf_pages_without_text" in events


async def test_reimport_is_unchanged_and_verify_passes(
    use_settings: Any, course_id: uuid.UUID, capsys: pytest.CaptureFixture[str]
) -> None:
    path = FIXTURE_DIR / "brief.docx"
    _, first, _ = await _import(capsys, course_id, path)
    code, again, _ = await _import(capsys, course_id, path)
    assert code == 0 and again["status"] == "unchanged"
    assert again["version_id"] == first["version_id"]

    code, report, _ = await _run(capsys, "verify", first["version_id"])
    assert code == 0
    assert report["status"] == "verified" and report["problems"] == []
    assert report["span_count"] == first["span_count"]


async def test_verify_detects_a_corrupted_raw_object(
    use_settings: Any, course_id: uuid.UUID, raw_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _, result, _ = await _import(capsys, course_id, FIXTURE_DIR / "brief.docx")
    digest = result["content_hash"]
    (raw_root / "sha256" / digest[:2] / digest[2:4] / digest).write_bytes(b"tampered")

    code, report, _ = await _run(capsys, "verify", result["version_id"])
    assert code == cli.EXIT_VERIFY_FAILED
    assert report["status"] == "failed"
    assert report["problems"] == ["raw object: RawObjectIntegrityError"]


async def test_source_uri_option_sets_the_resource_identity(
    use_settings: Any, course_id: uuid.UUID, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    renamed = tmp_path / "Brief FINAL.docx"
    shutil.copy(FIXTURE_DIR / "brief.docx", renamed)
    _, first, _ = await _import(
        capsys, course_id, FIXTURE_DIR / "brief.docx", "--source-uri", "upload:b"
    )
    _, again, _ = await _import(capsys, course_id, renamed, "--source-uri", "upload:b")

    assert again["status"] == "unchanged" and again["resource_id"] == first["resource_id"]


@pytest.mark.parametrize(
    ("name", "content", "category"),
    [("broken.pdf", b"not a pdf", "corrupt_file"), ("setup.exe", b"MZ", "unsupported_type")],
)
async def test_rejected_input_exits_4_with_its_category(
    use_settings: Any,
    course_id: uuid.UUID,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    name: str,
    content: bytes,
    category: str,
) -> None:
    path = tmp_path / name
    path.write_bytes(content)
    code, result, _ = await _import(capsys, course_id, path)
    assert code == cli.EXIT_REJECTED
    assert (result["status"], result["category"]) == ("rejected", category)


async def test_oversized_file_is_rejected_without_being_read(
    use_settings: Any,
    course_id: uuid.UUID,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    use_settings(ingest_max_bytes=10)
    path = tmp_path / "big.html"
    path.write_bytes(b"<p>" + b"x" * 100 + b"</p>")

    def no_open(self: Path, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("an oversized file must not be opened")

    monkeypatch.setattr(Path, "open", no_open)
    code, result, _ = await _import(capsys, course_id, path)
    assert code == cli.EXIT_REJECTED and result["category"] == "limit_exceeded"


async def test_missing_file_course_and_version_exit_3(
    use_settings: Any, course_id: uuid.UUID, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, result, _ = await _import(capsys, course_id, tmp_path / "absent.pdf")
    assert (code, result["error"]) == (cli.EXIT_NOT_FOUND, "file_not_found")

    code, result, _ = await _import(capsys, uuid.uuid4(), FIXTURE_DIR / "brief.docx")
    assert (code, result["error"]) == (cli.EXIT_NOT_FOUND, "course_not_found")

    code, result, _ = await _run(capsys, "verify", str(uuid.uuid4()))
    assert (code, result["error"]) == (cli.EXIT_NOT_FOUND, "version_not_found")


async def test_unconfigured_raw_storage_exits_2_before_any_work(
    use_settings: Any, course_id: uuid.UUID, capsys: pytest.CaptureFixture[str]
) -> None:
    use_settings(raw_storage_dir=None)
    code, result, _ = await _import(capsys, course_id, FIXTURE_DIR / "brief.docx")
    assert (code, result["error"]) == (cli.EXIT_STORAGE, "raw_storage")
    assert "RAW_STORAGE_DIR is not set" in result["detail"]


async def test_check_store_write_then_read(
    use_settings: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    code, written, _ = await _run(capsys, "check-store", "write")
    assert code == 0
    assert (written["created"], written["replaced"], written["temp_files"]) == (True, True, 0)

    code, read, _ = await _run(capsys, "check-store", "read")
    assert code == 0 and read["read"] is True
    assert read["probe_ref"] == written["probe_ref"]


# --- the error contract: one JSON result per outcome, no traceback, no paths ----


async def test_result_includes_stored_type_and_page_counts(
    use_settings: Any, course_id: uuid.UUID, capsys: pytest.CaptureFixture[str]
) -> None:
    _, result, _ = await _import(capsys, course_id, FIXTURE_DIR / "handbook.pdf")
    assert (result["resource_type"], result["total_page_count"], result["empty_page_count"]) == (
        "brief",
        4,
        1,
    )


async def test_directory_is_not_a_file(
    use_settings: Any, course_id: uuid.UUID, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, result, _ = await _import(capsys, course_id, tmp_path)
    assert (code, result["error"]) == (cli.EXIT_NOT_FOUND, "not_a_file")


async def test_unreadable_file_reports_errno_without_the_path(
    use_settings: Any,
    course_id: uuid.UUID,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "Jane Doe brief.pdf"
    path.write_bytes(b"%PDF-1.7")
    real_open = Path.open

    def denied(self: Path, *args: Any, **kwargs: Any) -> Any:
        if self == path:
            raise PermissionError(13, "Permission denied", str(self))
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", denied)
    code, result, err = await _import(capsys, course_id, path)

    assert code == cli.EXIT_NOT_FOUND
    assert result == {"status": "error", "error": "file_unreadable", "errno": "EACCES"}
    assert "Jane Doe" not in err


async def test_invalid_source_uri_is_rejected_with_the_field_name(
    use_settings: Any, course_id: uuid.UUID, capsys: pytest.CaptureFixture[str]
) -> None:
    code, result, _ = await _import(
        capsys, course_id, FIXTURE_DIR / "brief.docx", "--source-uri", "/home/someone/brief.docx"
    )
    assert code == cli.EXIT_REJECTED
    assert (result["error"], result["fields"]) == ("invalid_source", ["source_uri"])


@pytest.mark.parametrize(
    "argv",
    [
        ["import", "x.pdf", "--course-id", "not-a-uuid", "--type", "brief"],
        ["import", "x.pdf", "--type", "brief"],
        ["import", "x.pdf", "--course-id", str(uuid.uuid4()), "--type", "novel"],
        ["frobnicate"],
    ],
    ids=["bad-uuid", "missing-course", "bad-type", "unknown-command"],
)
async def test_usage_errors_exit_64_with_a_json_result(
    use_settings: Any, capsys: pytest.CaptureFixture[str], argv: list[str]
) -> None:
    code, result, _ = await _run(capsys, *argv)
    assert code == cli.EXIT_USAGE == 64
    assert result["error"] == "usage"


async def test_unexpected_error_is_one_json_result_without_its_message(
    use_settings: Any,
    course_id: uuid.UUID,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def explode(*args: Any, **kwargs: Any) -> Any:
        raise ValueError("Student 12345 at C:\\Users\\someone\\brief.docx")

    monkeypatch.setattr(cli, "ingest_file", explode)
    code, result, err = await _import(capsys, course_id, FIXTURE_DIR / "brief.docx")

    assert code == cli.EXIT_UNEXPECTED == 1
    assert result == {"status": "error", "error": "unexpected", "type": "ValueError"}
    assert "Traceback" not in err and "12345" not in err and "someone" not in err
    events = [json.loads(line)["event"] for line in err.strip().splitlines()]  # all JSON
    assert "cli.unexpected_error" in events


async def test_concurrent_import_conflict_is_reported_as_retryable(
    use_settings: Any,
    course_id: uuid.UUID,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def conflict(*args: Any, **kwargs: Any) -> Any:
        raise ConcurrentImportError("another import created this version number")

    monkeypatch.setattr(cli, "ingest_file", conflict)
    code, result, _ = await _import(capsys, course_id, FIXTURE_DIR / "brief.docx")
    assert code == cli.EXIT_UNEXPECTED
    assert (result["error"], result["retry"]) == ("concurrent_import", True)


async def test_invalid_raw_storage_setting_is_a_configuration_error(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    def invalid() -> Settings:
        return Settings(
            _env_file=None,  # type: ignore[call-arg]
            database_url=settings.database_url,
            raw_storage_dir="relative/raw",  # type: ignore[arg-type]
        )

    monkeypatch.setattr(cli, "get_settings", invalid)
    code, result, _ = await _run(capsys, "check-store", "read")
    assert code == cli.EXIT_STORAGE
    assert (result["error"], result["fields"]) == ("configuration", ["raw_storage_dir"])


async def test_other_invalid_configuration_exits_1(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    def invalid() -> Settings:
        return Settings(
            _env_file=None,  # type: ignore[call-arg]
            database_url=settings.database_url,
            log_level="LOUD",  # type: ignore[arg-type]
        )

    monkeypatch.setattr(cli, "get_settings", invalid)
    code, result, _ = await _run(capsys, "check-store", "read")
    assert code == cli.EXIT_UNEXPECTED == 1
    assert (result["error"], result["fields"]) == ("configuration", ["log_level"])


async def test_verify_detects_a_span_whose_text_no_longer_matches(
    use_settings: Any,
    course_id: uuid.UUID,
    committed_session: AsyncSession,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, result, _ = await _import(capsys, course_id, FIXTURE_DIR / "brief.docx")
    # Same length, different text: the database length check cannot see this.
    await committed_session.execute(
        text("UPDATE source_spans SET excerpt = reverse(excerpt) WHERE ordinal = 0")
    )
    await committed_session.commit()

    code, report, _ = await _run(capsys, "verify", result["version_id"])
    assert code == cli.EXIT_VERIFY_FAILED
    assert report["problems"] == ["1 span(s) do not slice back to their excerpt"]


async def test_verify_detects_a_missing_raw_object(
    use_settings: Any, course_id: uuid.UUID, raw_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _, result, _ = await _import(capsys, course_id, FIXTURE_DIR / "brief.docx")
    digest = result["content_hash"]
    (raw_root / "sha256" / digest[:2] / digest[2:4] / digest).unlink()

    code, report, _ = await _run(capsys, "verify", result["version_id"])
    assert code == cli.EXIT_VERIFY_FAILED
    assert report["problems"] == ["raw object: RawObjectNotFoundError"]


async def test_check_store_read_before_write_fails_clearly(
    use_settings: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    code, result, _ = await _run(capsys, "check-store", "read")
    assert (code, result["error"]) == (cli.EXIT_STORAGE, "raw_storage")


async def test_check_store_refuses_to_run_as_root(
    use_settings: Any, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os, "getuid", lambda: 0, raising=False)
    code, result, _ = await _run(capsys, "check-store", "write")
    assert (code, result["error"]) == (cli.EXIT_STORAGE, "running_as_root")
