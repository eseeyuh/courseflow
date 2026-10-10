# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

import logging
import os
from pathlib import Path

import pytest

from app.core.config import Settings
from app.ingestion.hashing import sha256_hex
from app.ingestion.storage import (
    InvalidObjectRefError,
    LocalRawObjectStore,
    RawObjectIntegrityError,
    RawObjectNotFoundError,
    RawStoreNotConfiguredError,
    RawStoreUnavailableError,
    RawStoreWriteError,
    object_ref,
    parse_object_ref,
)

CONTENT = b"%PDF-1.7 original bytes"
DIGEST = sha256_hex(CONTENT)
REF = f"sha256:{DIGEST}"


def _object_path(root: Path, digest: str = DIGEST) -> Path:
    return root / "sha256" / digest[:2] / digest[2:4] / digest


def _leftover_temp_files(root: Path) -> list[Path]:
    return [p for p in root.rglob(".tmp-*")]


# --- references ----------------------------------------------------------------------


def test_object_ref_round_trips() -> None:
    assert object_ref(DIGEST) == REF
    assert parse_object_ref(REF) == DIGEST


@pytest.mark.parametrize(
    "ref",
    [
        DIGEST,  # no scheme
        f"SHA256:{DIGEST}",
        f"sha256:{DIGEST.upper()}",
        f"sha256:{DIGEST[:-1]}",
        f"sha256:{DIGEST}0",
        f"sha256:{DIGEST}\n",
        "sha256:../../../../etc/passwd",
        "sha256:" + "../" * 21 + "x",
        f"sha1:{DIGEST[:40]}",
        "",
    ],
)
def test_invalid_references_are_rejected_without_echoing_them(ref: str) -> None:
    with pytest.raises(InvalidObjectRefError) as excinfo:
        parse_object_ref(ref)
    assert "etc" not in str(excinfo.value)


# --- configuration ---------------------------------------------------------------------


def test_unconfigured_storage_fails_clearly() -> None:
    settings = Settings(_env_file=None, database_url="postgresql+asyncpg://u:p@h/d")  # type: ignore[call-arg, arg-type]
    with pytest.raises(RawStoreNotConfiguredError, match="RAW_STORAGE_DIR is not set"):
        LocalRawObjectStore.from_settings(settings)


def test_missing_root_is_never_created(tmp_path: Path) -> None:
    missing = tmp_path / "typo" / "raw"
    with pytest.raises(RawStoreUnavailableError):
        LocalRawObjectStore(missing)
    assert not missing.exists()


def test_root_that_is_a_file_is_rejected(tmp_path: Path) -> None:
    not_a_dir = tmp_path / "raw"
    not_a_dir.write_bytes(b"")
    with pytest.raises(RawStoreUnavailableError):
        LocalRawObjectStore(not_a_dir)


def test_relative_root_is_rejected() -> None:
    with pytest.raises(RawStoreNotConfiguredError):
        LocalRawObjectStore(Path("raw"))


def test_store_from_settings_uses_raw_storage_dir(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        database_url="postgresql+asyncpg://u:p@h/d",  # type: ignore[arg-type]
        raw_storage_dir=tmp_path,
    )
    store = LocalRawObjectStore.from_settings(settings)
    store.put(CONTENT)
    assert _object_path(tmp_path).is_file()


# --- put / open -----------------------------------------------------------------------


def test_put_stores_content_addressed_and_open_returns_it(tmp_path: Path) -> None:
    store = LocalRawObjectStore(tmp_path)

    assert store.put(CONTENT) == REF
    assert _object_path(tmp_path).read_bytes() == CONTENT
    assert store.open(REF) == CONTENT
    assert store.exists(REF)
    assert _leftover_temp_files(tmp_path) == []


def test_identical_put_is_a_no_op(tmp_path: Path) -> None:
    store = LocalRawObjectStore(tmp_path)
    store.put(CONTENT)
    path = _object_path(tmp_path)
    before = path.stat()

    assert store.put(CONTENT) == REF
    after = path.stat()
    assert (after.st_mtime_ns, after.st_ino) == (before.st_mtime_ns, before.st_ino)


def test_put_repairs_a_corrupted_object_and_logs_it(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    store = LocalRawObjectStore(tmp_path)
    store.put(CONTENT)
    _object_path(tmp_path).write_bytes(b"tampered")

    with caplog.at_level(logging.WARNING):
        assert store.put(CONTENT) == REF
    assert store.open(REF) == CONTENT
    assert [r.getMessage() for r in caplog.records] == ["raw_store.object_repaired"]
    assert caplog.records[0].content_hash == DIGEST  # type: ignore[attr-defined]


def _make_unreadable(monkeypatch: pytest.MonkeyPatch, target: Path) -> None:
    """Reading ``target`` fails with PermissionError (portable stand-in for chmod 000)."""
    real_open = Path.open

    def guarded_open(self: Path, *args: object, **kwargs: object) -> object:
        if self == target:
            raise PermissionError(13, "Permission denied")
        return real_open(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "open", guarded_open)


def test_put_never_overwrites_an_object_it_cannot_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # Unreadable is an infrastructure problem, not evidence of corruption.
    store = LocalRawObjectStore(tmp_path)
    store.put(CONTENT)
    path = _object_path(tmp_path)
    before = path.stat()
    _make_unreadable(monkeypatch, path)

    with caplog.at_level(logging.WARNING), pytest.raises(RawStoreUnavailableError):
        store.put(CONTENT)

    monkeypatch.undo()
    after = path.stat()
    assert (after.st_mtime_ns, after.st_ino) == (before.st_mtime_ns, before.st_ino)
    assert path.read_bytes() == CONTENT
    assert "raw_store.object_repaired" not in [r.getMessage() for r in caplog.records]
    assert _leftover_temp_files(tmp_path) == []


def test_open_of_an_unreadable_object_is_unavailable_not_corrupt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = LocalRawObjectStore(tmp_path)
    store.put(CONTENT)
    _make_unreadable(monkeypatch, _object_path(tmp_path))

    with pytest.raises(RawStoreUnavailableError):
        store.open(REF)


def test_open_rejects_a_corrupted_object(tmp_path: Path) -> None:
    store = LocalRawObjectStore(tmp_path)
    store.put(CONTENT)
    _object_path(tmp_path).write_bytes(b"tampered")

    with pytest.raises(RawObjectIntegrityError):
        store.open(REF)


def test_open_of_a_missing_object_fails(tmp_path: Path) -> None:
    store = LocalRawObjectStore(tmp_path)
    with pytest.raises(RawObjectNotFoundError):
        store.open(REF)
    assert not store.exists(REF)


def test_failed_write_leaves_no_object_and_no_temp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = LocalRawObjectStore(tmp_path)

    def fail_replace(src: str, dst: str) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(RawStoreWriteError):
        store.put(CONTENT)

    assert not _object_path(tmp_path).exists()
    assert _leftover_temp_files(tmp_path) == []


def test_empty_content_is_storable(tmp_path: Path) -> None:
    store = LocalRawObjectStore(tmp_path)
    ref = store.put(b"")
    assert store.open(ref) == b""
