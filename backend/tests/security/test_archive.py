"""Safe expansion of untrusted ZIP archives.

An uploaded project package is the one place where a client chooses the paths the
platform writes to, so every archive here is hostile on purpose. All of them are
built in-test with :mod:`zipfile` — a malicious fixture is never committed, and
nothing is ever written outside ``tmp_path``.
"""

from __future__ import annotations

import zipfile
from collections.abc import Iterable, Sequence
from pathlib import Path, PurePosixPath

import pytest

from app.core.config import Settings
from app.core.errors import FileRejectedError
from app.security.archive import ExtractionResult, _copy_member, extract_archive, inspect_archive

#: Fixed so an archive built twice is byte-identical.
FIXED_DATE = (2026, 1, 1, 0, 0, 0)

IO_LIST = b"TAG,DESCRIPTION\nSYNTH_MTR_101,Synthetic conveyor motor\n"
RUNG = b"XIC SYNTH_MTR_101_RUN OTE SYNTH_ALARM_101\n"


def _build_archive(
    path: Path,
    members: Sequence[tuple[str, bytes]],
    *,
    compression: int = zipfile.ZIP_DEFLATED,
) -> Path:
    with zipfile.ZipFile(path, "w", compression) as archive:
        for name, payload in members:
            info = zipfile.ZipInfo(name, date_time=FIXED_DATE)
            info.compress_type = compression
            archive.writestr(info, payload)
    return path


def _build_archive_with_modes(
    path: Path,
    members: Iterable[tuple[str, bytes, int]],
) -> Path:
    """Build an archive whose members carry explicit Unix mode bits.

    ``external_attr`` is what marks a member a symlink or a device node, and it is
    attacker-controlled, so tests must be able to set it directly.
    """
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload, mode in members:
            info = zipfile.ZipInfo(name, date_time=FIXED_DATE)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = mode << 16
            archive.writestr(info, payload)
    return path


def _build_archive_with_raw_name(path: Path, raw_name: str, payload: bytes) -> Path:
    """Build a one-member archive, bypassing :class:`zipfile.ZipInfo` name cleanup.

    ``ZipInfo.__init__`` rewrites the platform separator, which would hide the
    backslash form of a traversal attempt on Windows.
    """
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        info = zipfile.ZipInfo("placeholder", date_time=FIXED_DATE)
        info.filename = raw_name
        info.compress_type = zipfile.ZIP_DEFLATED
        archive.writestr(info, payload)
    return path


def _by_path(result: ExtractionResult) -> dict[str, bytes]:
    return {
        str(member.relative_path): member.absolute_path.read_bytes() for member in result.members
    }


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_nested_members_are_extracted_with_sanitised_relative_paths(
    tmp_path: Path, settings: Settings
) -> None:
    """A member name is client-supplied text: it reaches the filesystem sanitised."""
    archive_path = _build_archive(
        tmp_path / "package.zip",
        [("Project Files/IO List.txt", IO_LIST), ("Project Files/logic/rung:12.txt", RUNG)],
    )
    destination = tmp_path / "extract"

    result = extract_archive(archive_path, destination, settings)

    assert [str(member.relative_path) for member in result.members] == [
        "Project Files/IO List.txt",
        "Project Files/logic/rung_12.txt",
    ]
    assert result.skipped == []
    assert (destination / "Project Files" / "logic" / "rung_12.txt").is_file()


def test_original_member_name_is_preserved_for_provenance(
    tmp_path: Path, settings: Settings
) -> None:
    """The engineer has to be able to match an extracted file back to the archive."""
    archive_path = _build_archive(
        tmp_path / "package.zip", [("Project Files/logic/rung:12.txt", RUNG)]
    )

    result = extract_archive(archive_path, tmp_path / "extract", settings)

    member = result.members[0]
    assert member.original_name == "Project Files/logic/rung:12.txt"
    assert member.relative_path == PurePosixPath("Project Files/logic/rung_12.txt")


def test_extracted_bytes_and_reported_total_match_the_input(
    tmp_path: Path, settings: Settings
) -> None:
    archive_path = _build_archive(
        tmp_path / "package.zip",
        [("Project Files/IO List.txt", IO_LIST), ("Project Files/logic/rung:12.txt", RUNG)],
    )
    destination = tmp_path / "extract"

    result = extract_archive(archive_path, destination, settings)

    assert _by_path(result) == {
        "Project Files/IO List.txt": IO_LIST,
        "Project Files/logic/rung_12.txt": RUNG,
    }
    assert [member.size_bytes for member in result.members] == [len(IO_LIST), len(RUNG)]
    assert result.total_uncompressed_bytes == len(IO_LIST) + len(RUNG)


def test_every_extracted_path_stays_under_the_destination(
    tmp_path: Path, settings: Settings
) -> None:
    archive_path = _build_archive(
        tmp_path / "package.zip",
        [("Project Files/IO List.txt", IO_LIST), ("Project Files/logic/rung:12.txt", RUNG)],
    )
    destination = tmp_path / "extract"

    result = extract_archive(archive_path, destination, settings)

    root = destination.resolve()
    for member in result.members:
        assert root in member.absolute_path.parents


def test_inspecting_a_benign_archive_writes_nothing(tmp_path: Path, settings: Settings) -> None:
    """Inspection is the pre-flight check, so it must be free of side effects."""
    archive_path = _build_archive(
        tmp_path / "package.zip", [("a.txt", IO_LIST), ("nested/b.txt", RUNG)]
    )
    destination = tmp_path / "extract"

    infos = inspect_archive(archive_path, settings)

    assert [info.filename for info in infos] == ["a.txt", "nested/b.txt"]
    assert not destination.exists()


# ---------------------------------------------------------------------------
# Zip Slip
# ---------------------------------------------------------------------------


def test_traversal_member_is_rejected_and_nothing_escapes(
    tmp_path: Path, settings: Settings
) -> None:
    """Zip Slip: a '..' member would let an upload overwrite files it does not own."""
    archive_path = _build_archive(
        tmp_path / "slip.zip",
        [("../../escape.txt", b"owned"), ("Project Files/IO List.txt", IO_LIST)],
    )
    destination = tmp_path / "extract" / "inner"

    with pytest.raises(FileRejectedError) as excinfo:
        extract_archive(archive_path, destination, settings)

    assert "outside the project directory" in excinfo.value.message
    assert excinfo.value.details["member"] == "../../escape.txt"
    assert not (tmp_path / "escape.txt").exists()
    assert not (tmp_path / "extract" / "escape.txt").exists()
    # Refused from the central directory, so the destination is never even
    # created: no partial tree can be left behind for a caller to clean up.
    assert not destination.exists()


def test_windows_style_traversal_member_is_rejected(tmp_path: Path, settings: Settings) -> None:
    """A backslash separator must not become a way past the forward-slash check."""
    archive_path = _build_archive_with_raw_name(
        tmp_path / "slip_windows.zip", r"..\..\escape.txt", b"owned"
    )
    destination = tmp_path / "extract" / "inner"

    with pytest.raises(FileRejectedError, match="outside the project directory"):
        extract_archive(archive_path, destination, settings)

    assert not (tmp_path / "escape.txt").exists()
    assert not (tmp_path / "extract" / "escape.txt").exists()
    # Refused from the central directory, so the destination is never even
    # created: no partial tree can be left behind for a caller to clean up.
    assert not destination.exists()


def test_member_nested_past_the_component_limit_is_rejected(
    tmp_path: Path, settings: Settings
) -> None:
    """An unbounded path depth is a way to exhaust the filesystem's own path limits."""
    deep_name = "/".join(f"level{index}" for index in range(17)) + "/deep.txt"
    archive_path = _build_archive(tmp_path / "deep.zip", [(deep_name, b"synthetic")])
    destination = tmp_path / "extract"

    with pytest.raises(FileRejectedError) as excinfo:
        extract_archive(archive_path, destination, settings)

    assert excinfo.value.details["member"] == deep_name
    # Over-nesting is a shape problem, not an attack: the message must not
    # accuse a benign archive of attempting traversal.
    assert "nested too deeply" in excinfo.value.message
    assert "outside the project directory" not in excinfo.value.message
    assert not destination.exists()


def test_absolute_member_path_is_contained_not_honoured(tmp_path: Path, settings: Settings) -> None:
    """'/etc/passwd' is stripped to a relative path rather than written at the root."""
    archive_path = _build_archive(tmp_path / "absolute.zip", [("/etc/passwd", b"root:x:0:0:\n")])
    destination = tmp_path / "extract"

    result = extract_archive(archive_path, destination, settings)

    member = result.members[0]
    assert member.original_name == "/etc/passwd"
    assert member.relative_path == PurePosixPath("etc/passwd")
    assert member.absolute_path == (destination / "etc" / "passwd").resolve()
    assert destination.resolve() in member.absolute_path.parents


def test_drive_letter_member_path_is_contained_not_honoured(
    tmp_path: Path, settings: Settings
) -> None:
    """A Windows absolute member must not be able to target the system directory."""
    archive_path = _build_archive_with_raw_name(
        tmp_path / "drive.zip", r"C:\Windows\System32\evil.dll", b"synthetic"
    )
    destination = tmp_path / "extract"

    result = extract_archive(archive_path, destination, settings)

    member = result.members[0]
    assert member.relative_path == PurePosixPath("Windows/System32/evil.dll")
    assert destination.resolve() in member.absolute_path.parents


# ---------------------------------------------------------------------------
# Zip bombs and limits
# ---------------------------------------------------------------------------


def test_zip_bomb_is_rejected_on_its_compression_ratio(tmp_path: Path, settings: Settings) -> None:
    """A few megabytes of zeros expand past 200:1; the guard must fire before any write."""
    archive_path = _build_archive(tmp_path / "bomb.zip", [("zeros.bin", bytes(4 * 1024 * 1024))])
    destination = tmp_path / "extract"

    with pytest.raises(FileRejectedError) as excinfo:
        extract_archive(archive_path, destination, settings)

    message = excinfo.value.message
    assert "compression ratio" in message
    assert "200:1 limit" in message
    assert "zip bomb" in message
    assert excinfo.value.details["ratio"] > settings.max_archive_compression_ratio
    assert not destination.exists()


def test_entry_count_above_the_limit_is_rejected(tmp_path: Path, settings: Settings) -> None:
    """Hundreds of thousands of tiny members are a denial-of-service, not a package."""
    archive_path = _build_archive(
        tmp_path / "flood.zip", [(f"member_{index}.txt", b"x") for index in range(3)]
    )
    destination = tmp_path / "extract"
    tight = settings.model_copy(update={"max_archive_entries": 2})

    with pytest.raises(FileRejectedError) as excinfo:
        extract_archive(archive_path, destination, tight)

    assert "3 entries" in excinfo.value.message
    assert "2 limit" in excinfo.value.message
    assert excinfo.value.details["entries"] == 3
    assert not destination.exists()


def test_total_uncompressed_size_above_the_limit_is_rejected(
    tmp_path: Path, settings: Settings
) -> None:
    archive_path = _build_archive(
        tmp_path / "large.zip",
        [("a.txt", b"a" * 40), ("b.txt", b"b" * 40)],
        compression=zipfile.ZIP_STORED,
    )
    destination = tmp_path / "extract"
    tight = settings.model_copy(update={"max_archive_uncompressed_bytes": 50})

    with pytest.raises(FileRejectedError) as excinfo:
        extract_archive(archive_path, destination, tight)

    assert "expands to 80 bytes" in excinfo.value.message
    assert "50 limit" in excinfo.value.message
    assert excinfo.value.details["uncompressed_bytes"] == 80
    assert not destination.exists()


def test_member_over_the_per_member_limit_is_skipped_not_fatal(
    tmp_path: Path, settings: Settings
) -> None:
    """One oversized file must not cost the engineer the rest of the package."""
    archive_path = _build_archive(
        tmp_path / "mixed.zip",
        [("small.txt", RUNG), ("huge.txt", b"x" * 4096)],
    )
    destination = tmp_path / "extract"

    result = extract_archive(archive_path, destination, settings, max_bytes_per_member=1024)

    assert _by_path(result) == {"small.txt": RUNG}
    assert result.total_uncompressed_bytes == len(RUNG)
    assert len(result.skipped) == 1
    assert result.skipped[0]["member"] == "huge.txt"
    assert "per-file limit" in result.skipped[0]["reason"]
    assert not (destination / "huge.txt").exists()


# ---------------------------------------------------------------------------
# Non-regular members
# ---------------------------------------------------------------------------


def test_symlink_member_is_skipped_with_a_reason(tmp_path: Path, settings: Settings) -> None:
    """Restoring a symlink would let a later write follow it out of the project tree."""
    archive_path = _build_archive_with_modes(
        tmp_path / "links.zip",
        [
            ("escape_link", b"../../../../etc/passwd", 0o120777),
            ("Project Files/IO List.txt", IO_LIST, 0o100644),
        ],
    )
    destination = tmp_path / "extract"

    result = extract_archive(archive_path, destination, settings)

    assert _by_path(result) == {"Project Files/IO List.txt": IO_LIST}
    assert result.skipped == [
        {"member": "escape_link", "reason": "symlink members are not extracted"}
    ]
    assert not (destination / "escape_link").exists()


def test_non_regular_member_is_skipped_with_a_reason(tmp_path: Path, settings: Settings) -> None:
    """A FIFO or device entry has no place in a project package."""
    archive_path = _build_archive_with_modes(
        tmp_path / "fifo.zip",
        [("pipe", b"", 0o010600), ("notes.txt", RUNG, 0o100644)],
    )
    destination = tmp_path / "extract"

    result = extract_archive(archive_path, destination, settings)

    assert _by_path(result) == {"notes.txt": RUNG}
    assert result.skipped == [{"member": "pipe", "reason": "member is not a regular file"}]
    assert not (destination / "pipe").exists()


def test_members_without_recorded_unix_modes_are_still_extracted(
    tmp_path: Path, settings: Settings
) -> None:
    """Most Windows zippers record no mode bits; that must not read as 'special'."""
    archive_path = _build_archive_with_modes(tmp_path / "windows.zip", [("notes.txt", RUNG, 0o0)])

    result = extract_archive(archive_path, tmp_path / "extract", settings)

    assert _by_path(result) == {"notes.txt": RUNG}
    assert result.skipped == []


def test_directory_entry_is_not_reported_as_a_member(tmp_path: Path, settings: Settings) -> None:
    """A directory carries no bytes, so counting it would misreport the package."""
    archive_path = _build_archive(
        tmp_path / "with_dir.zip", [("Project Files/", b""), ("Project Files/IO List.txt", IO_LIST)]
    )
    destination = tmp_path / "extract"

    result = extract_archive(archive_path, destination, settings)

    assert [member.original_name for member in result.members] == ["Project Files/IO List.txt"]
    assert result.skipped == []
    assert result.total_uncompressed_bytes == len(IO_LIST)


# ---------------------------------------------------------------------------
# Understated sizes and duplicates
# ---------------------------------------------------------------------------


def test_member_expanding_past_the_limit_is_stopped_and_the_partial_file_removed(
    tmp_path: Path,
) -> None:
    """The declared file_size is attacker-controlled, so the copy caps the real bytes.

    Called directly because the central-directory pre-check would skip an honestly
    declared member first; this is the second line of defence behind it.
    """
    archive_path = _build_archive(tmp_path / "understated.zip", [("big.bin", b"Z" * 4096)])
    target = tmp_path / "extract" / "big.bin"
    target.parent.mkdir(parents=True)

    with zipfile.ZipFile(archive_path) as archive:
        info = archive.getinfo("big.bin")
        with pytest.raises(FileRejectedError) as excinfo:
            _copy_member(archive, info, target, 100)

    assert "declared size was understated" in excinfo.value.message
    assert excinfo.value.details["member"] == "big.bin"
    assert not target.exists()


def test_second_member_sanitising_to_the_same_path_is_skipped_as_a_duplicate(
    tmp_path: Path, settings: Settings
) -> None:
    """Two names can collapse to one path; the first written file must not be replaced."""
    archive_path = _build_archive(
        tmp_path / "dupes.zip",
        [
            ("notes/plan.txt", b"first"),
            ("notes/PLAN.TXT", b"second"),
            ("notes/plan .txt", b"third"),
        ],
    )
    destination = tmp_path / "extract"

    result = extract_archive(archive_path, destination, settings)

    assert _by_path(result) == {"notes/plan.txt": b"first"}
    assert [row["member"] for row in result.skipped] == ["notes/PLAN.TXT", "notes/plan .txt"]
    assert {row["reason"] for row in result.skipped} == {"duplicate path after sanitisation"}
    assert result.total_uncompressed_bytes == len(b"first")


# ---------------------------------------------------------------------------
# Unreadable archives
# ---------------------------------------------------------------------------


def test_non_zip_file_is_rejected_with_a_clear_message(tmp_path: Path, settings: Settings) -> None:
    """A .zip extension proves nothing, and the engineer needs to be told which file failed."""
    archive_path = tmp_path / "not_really.zip"
    archive_path.write_bytes(b"This is a synthetic text file, not a ZIP container.\n")
    destination = tmp_path / "extract"

    with pytest.raises(FileRejectedError) as excinfo:
        extract_archive(archive_path, destination, settings)

    assert "not a readable ZIP archive" in excinfo.value.message
    assert not destination.exists()


def test_truncated_archive_is_rejected(tmp_path: Path, settings: Settings) -> None:
    """An interrupted upload must fail intake rather than half-extract."""
    archive_path = _build_archive(tmp_path / "truncated.zip", [("notes.txt", RUNG)])
    whole = archive_path.read_bytes()
    archive_path.write_bytes(whole[: len(whole) // 2])
    destination = tmp_path / "extract"

    with pytest.raises(FileRejectedError, match="not a readable ZIP archive"):
        extract_archive(archive_path, destination, settings)

    assert not destination.exists()


def test_archive_with_a_failing_checksum_is_rejected_as_corrupt(
    tmp_path: Path, settings: Settings
) -> None:
    """A member whose CRC does not match its bytes is not evidence anyone can rely on."""
    payload = b"synthetic-conveyor-payload"
    archive_path = _build_archive(
        tmp_path / "corrupt.zip", [("data.bin", payload)], compression=zipfile.ZIP_STORED
    )
    raw = bytearray(archive_path.read_bytes())
    offset = raw.find(payload)
    assert offset != -1
    raw[offset] = raw[offset] ^ 0xFF
    archive_path.write_bytes(bytes(raw))
    destination = tmp_path / "extract"

    with pytest.raises(FileRejectedError, match="corrupt entry"):
        extract_archive(archive_path, destination, settings)

    assert not destination.exists()
