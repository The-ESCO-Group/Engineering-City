"""Local artifact storage: layout, immutability, limits and containment.

The store holds the bytes every finding is cited against. If an original could be
overwritten, truncated or written outside its project directory, an evidence
anchor recorded today would stop meaning the same thing tomorrow — so the
read-only sealing, the never-overwrite rule and the path containment checks are
product guarantees, not incidental behaviour.
"""

from __future__ import annotations

import hashlib
import io
import os
import stat
import uuid
from pathlib import Path, PurePosixPath

import pytest

from app.core.config import Settings
from app.core.errors import FileRejectedError, NotFoundError, PayloadTooLargeError
from app.core.storage import (
    DERIVED,
    ORIGINALS,
    REPORTS,
    ProjectStorage,
    ensure_data_root,
    hash_bytes,
    relative_to_data_dir,
)

#: Fixed rather than random so the on-disk directory name is the same every run.
PROJECT_ID = uuid.UUID("00000000-0000-4000-8000-0000000000aa")
FILE_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")


@pytest.fixture
def storage(settings: Settings) -> ProjectStorage:
    return ProjectStorage(PROJECT_ID, settings)


def upload(data: bytes) -> io.BytesIO:
    """An in-memory stand-in for an uploaded file stream."""
    return io.BytesIO(data)


def is_writable(path: Path) -> bool:
    """Owner-write bit only: it is the single permission bit Windows honours."""
    return bool(path.stat().st_mode & stat.S_IWUSR)


# -- layout ------------------------------------------------------------------
def test_ensure_layout_creates_the_three_storage_areas(storage: ProjectStorage) -> None:
    storage.ensure_layout()

    assert sorted(entry.name for entry in storage.root.iterdir()) == [DERIVED, ORIGINALS, REPORTS]
    for directory in (storage.originals_dir, storage.derived_dir, storage.reports_dir):
        assert directory.is_dir()


def test_ensure_layout_leaves_existing_content_alone(storage: ProjectStorage) -> None:
    """It runs on every write, so it must never behave like a reset."""
    blob = storage.store_original(upload(b"already here"), "kept.bin")

    storage.ensure_layout()

    assert blob.absolute_path.read_bytes() == b"already here"


# -- store_original ----------------------------------------------------------
def test_store_original_writes_bytes_with_matching_size_and_sha256(
    storage: ProjectStorage,
) -> None:
    payload = b"TAG,DESCRIPTION\nMTR_101,Conveyor motor\n"

    blob = storage.store_original(upload(payload), "IO List.csv")

    assert blob.absolute_path.read_bytes() == payload
    assert blob.size_bytes == len(payload)
    assert blob.sha256 == hashlib.sha256(payload).hexdigest()
    assert blob.filename == "IO List.csv"
    assert str(blob.relative_path).startswith(f"{ORIGINALS}/")
    assert storage.absolute_path(blob.relative_path) == blob.absolute_path.resolve()


def test_stored_original_is_sealed_read_only(storage: ProjectStorage) -> None:
    """A reopenable original would let the bytes behind a citation change."""
    blob = storage.store_original(upload(b"sealed evidence"), "sealed.bin")

    assert is_writable(blob.absolute_path) is False
    assert os.access(blob.absolute_path, os.W_OK) is False
    with pytest.raises(PermissionError):
        blob.absolute_path.open("wb")
    assert blob.absolute_path.read_bytes() == b"sealed evidence"


def test_second_upload_of_the_same_filename_does_not_overwrite_the_first(
    storage: ProjectStorage,
) -> None:
    """Two revisions of "IO List.csv" must both remain citable."""
    first = storage.store_original(upload(b"revision A"), "IO List.csv")
    second = storage.store_original(upload(b"revision B"), "IO List.csv")

    assert first.absolute_path != second.absolute_path
    assert first.absolute_path.read_bytes() == b"revision A"
    assert second.absolute_path.read_bytes() == b"revision B"
    assert second.filename == "IO List (2).csv"
    assert first.sha256 != second.sha256


def test_open_original_yields_a_readable_handle(storage: ProjectStorage) -> None:
    blob = storage.store_original(upload(b"payload"), "opened.bin")

    with storage.open_original(blob.relative_path) as handle:
        assert handle.read() == b"payload"


# -- limits ------------------------------------------------------------------
def test_oversize_upload_is_refused_and_leaves_no_partial_file(storage: ProjectStorage) -> None:
    """A half-written blob would be indistinguishable from a genuine short file."""
    with pytest.raises(PayloadTooLargeError):
        storage.store_original(upload(b"x" * 64), "big.bin", max_bytes=16)

    assert list(storage.originals_dir.iterdir()) == []
    assert storage.total_bytes() == 0


def test_upload_limit_defaults_to_the_configured_maximum(settings: Settings) -> None:
    """Callers that omit max_bytes must still be held to the per-file cap."""
    capped = ProjectStorage(PROJECT_ID, settings.model_copy(update={"max_upload_bytes": 16}))

    with pytest.raises(PayloadTooLargeError):
        capped.store_original(upload(b"x" * 64), "big.bin")

    assert list(capped.originals_dir.iterdir()) == []


def test_empty_upload_is_rejected_and_leaves_no_file(storage: ProjectStorage) -> None:
    """An empty file carries no evidence but would still occupy a citable row."""
    with pytest.raises(FileRejectedError):
        storage.store_original(upload(b""), "empty.txt")

    assert list(storage.originals_dir.iterdir()) == []


# -- containment -------------------------------------------------------------
def test_traversing_filename_is_written_inside_originals(
    storage: ProjectStorage, settings: Settings
) -> None:
    blob = storage.store_original(upload(b"evil"), "../../evil.txt")

    assert blob.relative_path == PurePosixPath(ORIGINALS) / "evil.txt"
    assert blob.absolute_path.parent == storage.originals_dir
    assert (storage.originals_dir / "evil.txt").read_bytes() == b"evil"
    assert not (settings.projects_dir / "evil.txt").exists()
    assert not (settings.data_dir / "evil.txt").exists()
    assert not (settings.data_dir.parent / "evil.txt").exists()


def test_absolute_path_refuses_a_relative_path_that_escapes_the_project(
    storage: ProjectStorage,
) -> None:
    storage.ensure_layout()

    with pytest.raises(FileRejectedError):
        storage.absolute_path("../../../etc/passwd")


def test_absolute_path_of_a_missing_file_raises_not_found(storage: ProjectStorage) -> None:
    """Distinguishable from a rejection, so a stale row is not reported as an attack."""
    storage.ensure_layout()

    with pytest.raises(NotFoundError):
        storage.absolute_path(f"{ORIGINALS}/never-uploaded.csv")


# -- derived and reports -----------------------------------------------------
def test_write_derived_round_trips_and_stays_rewritable(storage: ProjectStorage) -> None:
    """Re-parsing with a newer parser version must be able to replace its output."""
    first = storage.write_derived("parsed.json", b'{"version": 1}')

    assert first.relative_path == PurePosixPath(DERIVED) / "parsed.json"
    assert first.absolute_path.read_bytes() == b'{"version": 1}'
    assert first.sha256 == hashlib.sha256(b'{"version": 1}').hexdigest()

    second = storage.write_derived("parsed.json", b'{"version": 2}')

    assert second.absolute_path == first.absolute_path
    assert second.absolute_path.read_bytes() == b'{"version": 2}'
    assert second.size_bytes == len(b'{"version": 2}')


def test_write_report_round_trips_and_is_sealed_read_only(storage: ProjectStorage) -> None:
    """A delivered report is a record of what was said, so it cannot be edited."""
    data = b"%PDF-1.4 synthetic review report"

    blob = storage.write_report("Review.pdf", data)

    assert blob.relative_path == PurePosixPath(REPORTS) / "Review.pdf"
    assert blob.absolute_path.read_bytes() == data
    assert blob.size_bytes == len(data)
    assert blob.sha256 == hashlib.sha256(data).hexdigest()
    assert is_writable(blob.absolute_path) is False
    with pytest.raises(PermissionError):
        blob.absolute_path.open("wb")


def test_regenerated_report_is_written_alongside_the_earlier_one(storage: ProjectStorage) -> None:
    first = storage.write_report("Review.pdf", b"first issue")
    second = storage.write_report("Review.pdf", b"second issue")

    assert first.absolute_path.read_bytes() == b"first issue"
    assert second.filename == "Review (2).pdf"
    assert second.absolute_path.read_bytes() == b"second issue"


def test_extraction_dir_is_a_scratch_directory_under_derived(storage: ProjectStorage) -> None:
    path = storage.extraction_dir(FILE_ID)

    assert path.is_dir()
    assert path.parent == storage.derived_dir.resolve()
    assert path.name == f"extract-{FILE_ID}"


def test_adopt_extracted_moves_an_archive_member_into_originals(
    storage: ProjectStorage, tmp_path: Path
) -> None:
    """Archive members get parsed and cited like uploads, so they live with them."""
    member = tmp_path / "scratch-member.csv"
    member.write_bytes(b"TAG\nMTR_101\n")

    blob = storage.adopt_extracted(member, "docs/nested/member.csv")

    assert member.exists() is False
    assert blob.relative_path == PurePosixPath(ORIGINALS) / "member.csv"
    assert blob.absolute_path.read_bytes() == b"TAG\nMTR_101\n"
    assert blob.size_bytes == len(b"TAG\nMTR_101\n")
    assert blob.sha256 == hashlib.sha256(b"TAG\nMTR_101\n").hexdigest()
    assert is_writable(blob.absolute_path) is False


# -- reading -----------------------------------------------------------------
def test_iter_chunks_streams_back_the_exact_bytes_across_chunk_boundaries(
    storage: ProjectStorage,
) -> None:
    """Downloads are streamed; a boundary bug would corrupt large files silently."""
    payload = bytes(range(256)) * 3
    blob = storage.store_original(upload(payload), "stream.bin")

    chunks = list(storage.iter_chunks(blob.relative_path, chunk_size=100))

    assert len(chunks) > 1
    assert b"".join(chunks) == payload


def test_read_head_returns_only_the_leading_bytes(storage: ProjectStorage) -> None:
    """Type sniffing must not have to load a whole upload into memory."""
    payload = b"PK\x03\x04" + b"z" * 4096
    blob = storage.store_original(upload(payload), "package.zip")

    assert storage.read_head(blob.relative_path, 4) == b"PK\x03\x04"
    assert storage.read_head(blob.relative_path, len(payload) + 100) == payload


def test_read_head_of_a_missing_file_raises_not_found(storage: ProjectStorage) -> None:
    storage.ensure_layout()

    with pytest.raises(NotFoundError):
        storage.read_head(f"{ORIGINALS}/absent.bin")


# -- accounting --------------------------------------------------------------
def test_total_bytes_is_zero_before_anything_is_written(storage: ProjectStorage) -> None:
    assert storage.total_bytes() == 0

    storage.ensure_layout()

    assert storage.total_bytes() == 0


def test_total_bytes_counts_originals_derived_and_reports(storage: ProjectStorage) -> None:
    """The per-project quota has to include parser output, not just uploads."""
    storage.store_original(upload(b"x" * 40), "original.bin")
    storage.write_derived("parsed.json", b"y" * 12)
    storage.write_report("Review.pdf", b"z" * 5)

    assert storage.total_bytes() == 57


# -- deletion ----------------------------------------------------------------
def test_delete_removes_a_sealed_original(storage: ProjectStorage) -> None:
    """On Windows, unlinking a read-only file fails unless the bit is cleared first."""
    blob = storage.store_original(upload(b"to be deleted"), "gone.bin")

    assert storage.delete(blob.relative_path) is True
    assert blob.absolute_path.exists() is False


def test_delete_reports_false_for_an_already_missing_path(storage: ProjectStorage) -> None:
    """Deleting a row whose blob is gone must succeed, not raise."""
    storage.ensure_layout()

    assert storage.delete(f"{ORIGINALS}/never-there.bin") is False


def test_delete_is_idempotent(storage: ProjectStorage) -> None:
    blob = storage.store_original(upload(b"once"), "once.bin")

    assert storage.delete(blob.relative_path) is True
    assert storage.delete(blob.relative_path) is False


def test_delete_leaves_the_other_blobs_in_place(storage: ProjectStorage) -> None:
    doomed = storage.store_original(upload(b"doomed"), "doomed.bin")
    kept = storage.store_original(upload(b"kept"), "kept.bin")

    storage.delete(doomed.relative_path)

    assert kept.absolute_path.read_bytes() == b"kept"
    assert storage.originals_dir.is_dir()


def test_purge_removes_the_whole_tree_including_sealed_files(storage: ProjectStorage) -> None:
    """Hard-deleting a project must not leave read-only originals behind."""
    storage.store_original(upload(b"original bytes"), "original.bin")
    storage.write_report("Review.pdf", b"report bytes")
    storage.write_derived("parsed.json", b"{}")

    storage.purge()

    assert storage.root.exists() is False
    assert storage.total_bytes() == 0


def test_purge_of_a_project_that_was_never_written_is_a_no_op(storage: ProjectStorage) -> None:
    storage.purge()

    assert storage.root.exists() is False


# -- module helpers ----------------------------------------------------------
def test_hash_bytes_agrees_with_the_streamed_upload_digest(storage: ProjectStorage) -> None:
    """The same bytes must hash identically whether streamed or hashed in memory."""
    payload = b"identical bytes"
    blob = storage.store_original(upload(payload), "hashed.bin")

    assert hash_bytes(payload) == blob.sha256


def test_ensure_data_root_creates_the_root_and_removes_its_write_probe(
    settings: Settings, tmp_path: Path
) -> None:
    fresh = settings.model_copy(update={"data_dir": tmp_path / "fresh-root"})

    root = ensure_data_root(fresh)

    assert root == fresh.data_dir
    assert root.is_dir()
    assert list(root.iterdir()) == []


def test_relative_to_data_dir_hides_the_server_layout(
    storage: ProjectStorage, settings: Settings
) -> None:
    """Absolute server paths must never reach a client."""
    blob = storage.store_original(upload(b"shown to a client"), "shown.bin")

    rendered = relative_to_data_dir(blob.absolute_path, settings)

    assert rendered.replace("\\", "/") == f"projects/{PROJECT_ID}/{ORIGINALS}/shown.bin"
    assert str(settings.data_dir) not in rendered


def test_relative_to_data_dir_falls_back_to_the_bare_name_outside_the_root(
    settings: Settings, tmp_path: Path
) -> None:
    outside = tmp_path / "elsewhere.txt"
    outside.write_bytes(b"not in the store")

    assert relative_to_data_dir(outside, settings) == "elsewhere.txt"
