# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Content-addressed store for original source bytes.

A reference is ``sha256:<64 lowercase hex>``: it names the bytes, not where
they are kept, so another backend (e.g. object storage) can serve the same
references later. ``LocalRawObjectStore`` keeps each object at
``<root>/sha256/<hex[0:2]>/<hex[2:4]>/<hex>``. Paths are derived only from a
validated hash, never from a file name or other input.

Writes go to a temporary file in the destination directory and are then
renamed into place (``os.replace``), so readers see either no object or the
complete object, never a partial one. The data (and, on POSIX, the directory
entry) is fsynced, which improves durability after a crash as far as the
platform and file system honour fsync; it is not an absolute guarantee.

Store writes are not part of any database transaction: an object written
for an import whose rows are later rolled back stays behind, unreferenced.
That is harmless (same content, same name) and is left for a later cleanup.
"""

import contextlib
import errno
import hashlib
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Protocol, Self

from app.core.config import Settings
from app.ingestion.hashing import sha256_hex

logger = logging.getLogger(__name__)

_REF = re.compile(r"sha256:([0-9a-f]{64})")
_REF_PREFIX = "sha256:"
_READ_CHUNK = 1024 * 1024
# Fixed content for LocalRawObjectStore.self_check; never real source material.
PROBE_CONTENT = b"CourseFlow raw store probe v1\n"


class RawStoreError(Exception):
    """Raw storage is unusable or inconsistent. An infrastructure error, not a
    problem with one input file."""


class RawStoreNotConfiguredError(RawStoreError):
    pass


class RawStoreUnavailableError(RawStoreError):
    pass


class RawStoreWriteError(RawStoreError):
    pass


class InvalidObjectRefError(RawStoreError, ValueError):
    pass


class RawObjectNotFoundError(RawStoreError):
    pass


class RawObjectIntegrityError(RawStoreError):
    pass


def object_ref(content_hash: str) -> str:
    """The reference for bytes with this SHA-256 (validated)."""
    ref = f"{_REF_PREFIX}{content_hash}"
    parse_object_ref(ref)
    return ref


def parse_object_ref(ref: str) -> str:
    """Return the hex digest of a ``sha256:<hex>`` reference."""
    # fullmatch: with match(), `$` would also accept a trailing newline.
    match = _REF.fullmatch(ref) if isinstance(ref, str) else None
    if match is None:
        # The rejected value is not echoed: it may be arbitrary input.
        raise InvalidObjectRefError("raw object reference must be 'sha256:' + 64 lowercase hex")
    return match.group(1)


class RawObjectStore(Protocol):
    def put(self, content: bytes) -> str:
        """Store ``content`` (idempotent); return its reference."""
        ...

    def open(self, ref: str) -> bytes:
        """Return the verified bytes for ``ref``."""
        ...

    def exists(self, ref: str) -> bool: ...


class LocalRawObjectStore:
    """``RawObjectStore`` on a local directory (or a mounted volume)."""

    def __init__(self, root: Path) -> None:
        # Never created here: a mistyped path must fail, not become a second store.
        if not root.is_absolute():
            raise RawStoreNotConfiguredError("raw storage root must be an absolute path")
        try:
            is_dir = root.is_dir()
        except OSError as exc:
            raise RawStoreUnavailableError(
                f"raw storage root could not be inspected ({errno_name(exc)})"
            ) from None
        if not is_dir:
            raise RawStoreUnavailableError("raw storage root does not exist or is not a directory")
        self._root = root

    @classmethod
    def from_settings(cls, settings: Settings) -> Self:
        if settings.raw_storage_dir is None:
            raise RawStoreNotConfiguredError(
                "RAW_STORAGE_DIR is not set; raw storage is required for ingestion"
            )
        return cls(settings.raw_storage_dir)

    def put(self, content: bytes) -> str:
        """Store ``content`` and return its reference.

        - No object yet: written atomically.
        - Object present and its bytes hash to its name: nothing to do.
        - Object present and readable but its bytes do not match (corrupted or
          tampered): replaced atomically with ``content``, which is known to be
          correct because its hash is the object's name.
        - Object present but unreadable (permissions, I/O error): an
          infrastructure problem, not evidence of corruption. Raises
          ``RawStoreUnavailableError`` and leaves the object untouched.
        """
        digest = sha256_hex(content)
        path = self._path(digest)
        existing = _existing_digest(path)
        if existing == digest:
            return object_ref(digest)
        self._write_atomically(path, content)
        if existing is not None:  # logged only once the repair has succeeded
            logger.warning("raw_store.object_repaired", extra={"content_hash": digest})
        return object_ref(digest)

    def open(self, ref: str) -> bytes:
        digest = parse_object_ref(ref)
        path = self._path(digest)
        try:
            content = path.read_bytes()
        except FileNotFoundError:
            raise RawObjectNotFoundError(f"raw object {ref} is missing") from None
        except OSError as exc:
            raise RawStoreUnavailableError(
                f"raw object {ref} could not be read ({errno_name(exc)})"
            ) from None
        if sha256_hex(content) != digest:
            raise RawObjectIntegrityError(f"raw object {ref} does not match its hash")
        return content

    def exists(self, ref: str) -> bool:
        try:
            return self._path(parse_object_ref(ref)).is_file()
        except OSError as exc:
            raise RawStoreUnavailableError(
                f"raw object {ref} could not be inspected ({errno_name(exc)})"
            ) from None

    def self_check(self) -> dict[str, object]:
        """Prove this process can use the store the way ingestion does.

        Uses one fixed probe object (never real content): removes it, creates
        it (temp file + atomic rename), reads it back, overwrites it with junk
        and lets ``put`` repair it (atomic replace of an existing object), then
        reads it again. Raises ``RawStoreError`` on any failure.
        """
        ref = object_ref(sha256_hex(PROBE_CONTENT))
        path = self._path(parse_object_ref(ref))
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            raise RawStoreUnavailableError(
                f"self-check: probe object could not be removed ({errno_name(exc)})"
            ) from None

        self.put(PROBE_CONTENT)
        if self.open(ref) != PROBE_CONTENT:
            raise RawStoreError("self-check: created probe object reads back differently")
        try:
            path.write_bytes(b"not the probe")
        except OSError as exc:
            raise RawStoreWriteError(
                f"self-check: probe object could not be overwritten ({errno_name(exc)})"
            ) from None
        self.put(PROBE_CONTENT)  # repairs via os.replace over the existing file
        if self.open(ref) != PROBE_CONTENT:
            raise RawStoreError("self-check: replaced probe object reads back differently")
        leftovers = len(list(path.parent.glob(".tmp-*")))
        if leftovers:
            raise RawStoreError(f"self-check: {leftovers} temporary file(s) left behind")
        return {"probe_ref": ref, "created": True, "replaced": True, "temp_files": 0}

    def _path(self, digest: str) -> Path:
        return self._root / "sha256" / digest[:2] / digest[2:4] / digest

    def _write_atomically(self, path: Path, content: bytes) -> None:
        temp_name: str | None = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            # Same directory as the destination, so the rename stays on one file system.
            fd, temp_name = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
            with os.fdopen(fd, "wb") as temp:
                temp.write(content)
                temp.flush()
                os.fsync(temp.fileno())
            os.replace(temp_name, path)
            temp_name = None
        except OSError as exc:
            raise RawStoreWriteError(
                f"raw object could not be written ({errno_name(exc)})"
            ) from None
        finally:
            if temp_name is not None:
                with contextlib.suppress(OSError):
                    os.unlink(temp_name)
        try:
            _fsync_directory(path.parent)
        except OSError as exc:
            # The object is complete and visible; only its durability is uncertain.
            raise RawStoreWriteError(
                f"raw object was written but its directory could not be synced ({errno_name(exc)})"
            ) from None


def _existing_digest(path: Path) -> str | None:
    """SHA-256 of the stored object, or None if there is no object.

    Any other failure to read it raises ``RawStoreUnavailableError``.
    """
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stored:
            while chunk := stored.read(_READ_CHUNK):
                digest.update(chunk)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise RawStoreUnavailableError(
            f"existing raw object could not be read ({errno_name(exc)})"
        ) from None
    return digest.hexdigest()


def errno_name(exc: OSError) -> str:
    """e.g. "ENOSPC": tells disk-full from permission problems without a path."""
    return errno.errorcode.get(exc.errno or 0, "unknown error")


def _fsync_directory(directory: Path) -> None:
    """Persist the rename's directory entry where the platform supports it."""
    if os.name != "posix":
        return  # Windows cannot open a directory for fsync
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
