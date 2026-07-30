"""Local artifact storage.

Layout, per project::

    data/projects/{project_id}/originals/   uploaded bytes, written read-only
    data/projects/{project_id}/derived/     parser output, archive expansions
    data/projects/{project_id}/reports/     generated reports

Originals are immutable. Once written they are chmod'ed read-only and never
reopened for writing, so an evidence anchor taken today still refers to the same
bytes tomorrow. Deleting a file marks the row deleted and removes the blob; it
never rewrites one in place.

Every path is built through :mod:`app.security.paths`, so a filename from a
client cannot escape its project directory.
"""

from __future__ import annotations

import hashlib
import shutil
import stat
import uuid
from collections.abc import Iterator
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO

from app.core.config import Settings
from app.core.errors import FileRejectedError, NotFoundError, PayloadTooLargeError
from app.core.logging import get_logger
from app.security.paths import resolve_within, safe_filename, unique_path

logger = get_logger(__name__)

ORIGINALS = "originals"
DERIVED = "derived"
REPORTS = "reports"

#: Streaming chunk size for hashing and copying.
CHUNK_BYTES = 1024 * 1024


@dataclass(slots=True)
class StoredBlob:
    """A file that has been written into the store."""

    #: Path relative to the project directory, e.g. ``originals/IO List.xlsx``.
    relative_path: PurePosixPath
    absolute_path: Path
    size_bytes: int
    sha256: str

    @property
    def filename(self) -> str:
        return self.relative_path.name


class ProjectStorage:
    """Filesystem operations scoped to one project."""

    def __init__(self, project_id: uuid.UUID, settings: Settings) -> None:
        self.project_id = project_id
        self.settings = settings
        self.root = settings.project_dir(project_id)

    # -- directories ------------------------------------------------------
    def ensure_layout(self) -> None:
        for name in (ORIGINALS, DERIVED, REPORTS):
            (self.root / name).mkdir(parents=True, exist_ok=True)

    @property
    def originals_dir(self) -> Path:
        return self.root / ORIGINALS

    @property
    def derived_dir(self) -> Path:
        return self.root / DERIVED

    @property
    def reports_dir(self) -> Path:
        return self.root / REPORTS

    # -- writing ----------------------------------------------------------
    def store_original(
        self,
        source: BinaryIO,
        filename: str,
        *,
        max_bytes: int | None = None,
    ) -> StoredBlob:
        """Stream an upload into ``originals/`` and seal it read-only.

        The size limit is enforced while copying rather than from a declared
        ``Content-Length``, which a client controls.
        """
        self.ensure_layout()
        limit = max_bytes if max_bytes is not None else self.settings.max_upload_bytes
        target = unique_path(self.originals_dir, safe_filename(filename))

        digest = hashlib.sha256()
        written = 0
        try:
            with target.open("wb") as sink:
                while chunk := source.read(CHUNK_BYTES):
                    written += len(chunk)
                    if written > limit:
                        raise PayloadTooLargeError(
                            f"Upload exceeds the {limit:,} byte per-file limit.",
                            details={"filename": filename, "limit": limit},
                        )
                    digest.update(chunk)
                    sink.write(chunk)
        except BaseException:
            target.unlink(missing_ok=True)
            raise

        if written == 0:
            target.unlink(missing_ok=True)
            raise FileRejectedError("File is empty.", details={"filename": filename})

        _seal_read_only(target)
        return StoredBlob(
            relative_path=PurePosixPath(ORIGINALS) / target.name,
            absolute_path=target,
            size_bytes=written,
            sha256=digest.hexdigest(),
        )

    def adopt_extracted(self, extracted_path: Path, member_name: str) -> StoredBlob:
        """Move a file expanded from an archive into ``originals/``.

        Members of a project package are first-class uploaded files: they get
        parsed, anchored and cited the same way, so they live alongside directly
        uploaded ones rather than in a second-class location.
        """
        self.ensure_layout()
        target = unique_path(self.originals_dir, safe_filename(member_name))
        shutil.move(str(extracted_path), str(target))

        size = target.stat().st_size
        digest = _hash_file(target)
        _seal_read_only(target)
        return StoredBlob(
            relative_path=PurePosixPath(ORIGINALS) / target.name,
            absolute_path=target,
            size_bytes=size,
            sha256=digest,
        )

    def write_derived(self, relative_name: str, data: bytes) -> StoredBlob:
        """Write parser output or another derived artifact.

        Derived files are rewritable — re-parsing with a newer parser version is
        expected — so they are not sealed.
        """
        self.ensure_layout()
        target = resolve_within(self.derived_dir, safe_filename(relative_name))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return StoredBlob(
            relative_path=PurePosixPath(DERIVED) / target.name,
            absolute_path=target,
            size_bytes=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
        )

    def write_report(self, filename: str, data: bytes) -> StoredBlob:
        self.ensure_layout()
        target = unique_path(self.reports_dir, safe_filename(filename))
        target.write_bytes(data)
        _seal_read_only(target)
        return StoredBlob(
            relative_path=PurePosixPath(REPORTS) / target.name,
            absolute_path=target,
            size_bytes=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
        )

    def extraction_dir(self, file_id: uuid.UUID) -> Path:
        """Scratch directory for expanding one archive."""
        path = resolve_within(self.derived_dir, f"extract-{file_id}")
        path.mkdir(parents=True, exist_ok=True)
        return path

    # -- reading ----------------------------------------------------------
    def absolute_path(self, relative_path: str | PurePosixPath) -> Path:
        """Resolve a stored relative path, refusing anything outside the project."""
        path = resolve_within(self.root, PurePosixPath(str(relative_path)))
        if not path.exists():
            raise NotFoundError("Stored file", str(relative_path))
        return path

    def open_original(self, relative_path: str | PurePosixPath) -> BinaryIO:
        return self.absolute_path(relative_path).open("rb")

    def read_head(self, relative_path: str | PurePosixPath, size: int = 8192) -> bytes:
        with self.absolute_path(relative_path).open("rb") as handle:
            return handle.read(size)

    def iter_chunks(
        self, relative_path: str | PurePosixPath, chunk_size: int = CHUNK_BYTES
    ) -> Iterator[bytes]:
        """Stream a stored file, for download responses."""
        with self.absolute_path(relative_path).open("rb") as handle:
            while chunk := handle.read(chunk_size):
                yield chunk

    def total_bytes(self) -> int:
        """Bytes currently occupied by this project, for the per-project quota."""
        if not self.root.exists():
            return 0
        return sum(f.stat().st_size for f in self.root.rglob("*") if f.is_file())

    # -- deletion ---------------------------------------------------------
    def delete(self, relative_path: str | PurePosixPath) -> bool:
        """Remove a stored blob. Returns ``False`` if it was already gone.

        The read-only bit is cleared first: on Windows, ``unlink`` on a
        read-only file raises ``PermissionError``.
        """
        try:
            path = self.absolute_path(relative_path)
        except NotFoundError:
            return False
        _unseal(path)
        path.unlink(missing_ok=True)
        return True

    def purge(self) -> None:
        """Delete the whole project tree. Used when a project is hard-deleted."""
        if not self.root.exists():
            return
        for path in self.root.rglob("*"):
            if path.is_file():
                _unseal(path)
        shutil.rmtree(self.root, ignore_errors=True)


def _seal_read_only(path: Path) -> None:
    """Drop write permission so an original cannot be modified in place."""
    try:
        mode = path.stat().st_mode
        path.chmod(mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
    except OSError as exc:  # pragma: no cover - filesystem dependent
        # Some network and container filesystems ignore chmod. Immutability is
        # then a convention rather than an enforced property, which is worth
        # recording but not worth failing an upload over.
        logger.warning(
            "could not seal file read-only",
            extra={"path": str(path), "error": str(exc)},
        )


def _unseal(path: Path) -> None:
    try:
        path.chmod(path.stat().st_mode | stat.S_IWUSR)
    except OSError:  # pragma: no cover - filesystem dependent
        pass


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def ensure_data_root(settings: Settings) -> Path:
    """Create the data root on startup and confirm it is writable."""
    root = settings.data_dir
    root.mkdir(parents=True, exist_ok=True)
    probe = root / ".write-probe"
    try:
        probe.write_bytes(b"ok")
    except OSError as exc:
        raise RuntimeError(f"Data directory {root} is not writable: {exc}") from exc
    finally:
        with suppress(OSError):
            probe.unlink(missing_ok=True)
    return root


def relative_to_data_dir(path: Path, settings: Settings) -> str:
    """Render an absolute stored path relative to the data root, for logging.

    Absolute paths are never returned to a client; they leak the server layout.
    """
    try:
        return str(path.resolve().relative_to(settings.data_dir))
    except ValueError:
        return path.name
