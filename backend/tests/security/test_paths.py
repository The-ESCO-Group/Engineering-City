"""Filename sanitisation and filesystem containment.

These helpers are the only thing standing between a client-supplied name — an
upload filename, a ZIP member — and a real path on disk, so they are asserted
directly rather than through a route.
"""

from __future__ import annotations

import unicodedata
from pathlib import Path, PurePosixPath

import pytest

from app.core.errors import FileRejectedError
from app.security.paths import (
    MAX_FILENAME_LENGTH,
    MAX_PATH_COMPONENTS,
    TRAVERSAL_CODE,
    resolve_within,
    safe_filename,
    safe_relative_path,
    unique_path,
)

# ---------------------------------------------------------------------------
# safe_filename
# ---------------------------------------------------------------------------


def test_safe_filename_strips_posix_directory_components() -> None:
    assert safe_filename("../../etc/passwd") == "passwd"
    assert safe_filename("/var/log/syslog") == "syslog"
    assert safe_filename("uploads/2026/IO List.xlsx") == "IO List.xlsx"


def test_safe_filename_strips_windows_directory_components() -> None:
    """Browsers on Windows send the full local path, backslashes and drive included."""
    assert safe_filename(r"C:\Users\engineer\Desktop\report.pdf") == "report.pdf"
    assert safe_filename(r"..\..\Windows\System32\cmd.exe") == "cmd.exe"


def test_safe_filename_neutralises_parent_directory_sequences() -> None:
    """A surviving '..' would still traverse once the name is joined to a root."""
    assert safe_filename("....//....//io.xlsx") == "io.xlsx"
    assert safe_filename("..") == "unnamed"
    assert safe_filename("..hidden..name.txt") == "hidden.name.txt"


def test_safe_filename_removes_control_characters_and_nul() -> None:
    """A NUL truncates the name inside the C library, so an extension check could be bypassed."""
    result = safe_filename("re\x00port\x07\n.txt")

    assert result == "re_port__.txt"
    assert "\x00" not in result
    assert all(ord(char) >= 32 for char in result)


def test_safe_filename_falls_back_when_the_name_is_only_dots_or_spaces() -> None:
    assert safe_filename("...") == "unnamed"
    assert safe_filename("   ") == "unnamed"
    assert safe_filename(" . . ") == "unnamed"
    assert safe_filename("...", fallback="io-list") == "io-list"


def test_safe_filename_defuses_windows_reserved_device_names() -> None:
    """'CON' and friends are not creatable on Windows and can open a device instead."""
    assert safe_filename("CON") == "_CON"
    assert safe_filename("NUL") == "_NUL"
    assert safe_filename("COM1") == "_COM1"
    assert safe_filename("LPT9") == "_LPT9"
    assert safe_filename("CON.txt") == "_CON.txt"
    assert safe_filename("nul.TXT") == "_nul.TXT"


def test_safe_filename_preserves_a_legitimate_extension() -> None:
    assert safe_filename("IO List.xlsx") == "IO List.xlsx"
    assert safe_filename("Control Narrative.docx") == "Control Narrative.docx"
    assert safe_filename("IO   List.xlsx") == "IO List.xlsx"


def test_safe_filename_bounds_an_over_long_name_and_keeps_the_extension() -> None:
    """The bound has to leave the extension intact: type detection and the UI both read it."""
    long_name = safe_filename("a" * 300 + ".pdf")
    assert len(long_name) == MAX_FILENAME_LENGTH
    assert long_name.endswith(".pdf")
    assert long_name.startswith("aaaa")

    assert len(safe_filename("b" * 300)) == MAX_FILENAME_LENGTH


def test_safe_filename_normalises_unicode_compatibility_forms() -> None:
    """Compatibility forms fold, so two names that render identically cannot diverge on disk."""
    assert safe_filename("ﬁle.txt") == "file.txt"

    fullwidth_io = chr(0xFF29) + chr(0xFF2F) + ".txt"
    assert safe_filename(fullwidth_io) == "IO.txt"

    composed = "café.txt"
    decomposed = "café.txt"
    assert safe_filename(composed) == safe_filename(decomposed)
    assert safe_filename(composed) == unicodedata.normalize("NFKD", composed)


def test_safe_filename_returns_the_fallback_for_empty_input() -> None:
    assert safe_filename("") == "unnamed"
    assert safe_filename("", fallback="attachment.bin") == "attachment.bin"


# ---------------------------------------------------------------------------
# safe_relative_path
# ---------------------------------------------------------------------------


def test_safe_relative_path_keeps_a_legitimate_nested_path() -> None:
    result = safe_relative_path("docs/specs/IO List.xlsx")
    assert result == PurePosixPath("docs/specs/IO List.xlsx")
    assert result.parts == ("docs", "specs", "IO List.xlsx")


def test_safe_relative_path_rejects_a_member_that_traverses_upwards() -> None:
    """A traversing ZIP member is a signal about the archive, not a formatting quirk."""
    with pytest.raises(FileRejectedError) as excinfo:
        safe_relative_path("docs/../../etc/passwd")

    assert excinfo.value.code == TRAVERSAL_CODE
    assert excinfo.value.status_code == 400
    assert excinfo.value.details["member"] == "docs/../../etc/passwd"

    with pytest.raises(FileRejectedError):
        safe_relative_path("..")


def test_safe_relative_path_drops_absolute_and_drive_prefixes() -> None:
    assert safe_relative_path("/etc/passwd") == PurePosixPath("etc/passwd")
    assert safe_relative_path("C:/Users/engineer/f.txt") == PurePosixPath("Users/engineer/f.txt")
    assert safe_relative_path(r"C:\Users\engineer\f.txt") == PurePosixPath("Users/engineer/f.txt")
    assert not safe_relative_path("/etc/passwd").is_absolute()


def test_safe_relative_path_drops_current_directory_components() -> None:
    assert safe_relative_path("./docs/./io.xlsx") == PurePosixPath("docs/io.xlsx")
    assert safe_relative_path("docs/./specs/f.txt") == PurePosixPath("docs/specs/f.txt")


def test_safe_relative_path_rejects_nesting_beyond_the_component_limit() -> None:
    """Depth is another shape a hostile archive takes, so it is bounded like size is."""
    accepted = "/".join(["dir"] * (MAX_PATH_COMPONENTS - 1)) + "/f.txt"
    assert len(safe_relative_path(accepted).parts) == MAX_PATH_COMPONENTS

    rejected = "/".join(["dir"] * MAX_PATH_COMPONENTS) + "/f.txt"
    with pytest.raises(FileRejectedError) as excinfo:
        safe_relative_path(rejected)
    assert excinfo.value.details["member"] == rejected


def test_safe_relative_path_falls_back_when_no_component_survives() -> None:
    assert safe_relative_path("") == PurePosixPath("unnamed")
    assert safe_relative_path("/") == PurePosixPath("unnamed")
    assert safe_relative_path("/", fallback="member") == PurePosixPath("member")


# ---------------------------------------------------------------------------
# resolve_within
# ---------------------------------------------------------------------------


def test_resolve_within_allows_a_path_inside_the_root(tmp_path: Path) -> None:
    resolved = resolve_within(tmp_path, "uploads", "IO List.xlsx")
    assert resolved == tmp_path.resolve() / "uploads" / "IO List.xlsx"

    nested = resolve_within(tmp_path, PurePosixPath("uploads/2026"), "io.xlsx")
    assert nested == tmp_path.resolve() / "uploads" / "2026" / "io.xlsx"


def test_resolve_within_rejects_a_traversal_escape(tmp_path: Path) -> None:
    with pytest.raises(FileRejectedError) as excinfo:
        resolve_within(tmp_path, "..", "escape.txt")

    assert excinfo.value.code == "file_rejected"
    assert excinfo.value.details["root"] == str(tmp_path.resolve())
    assert excinfo.value.details["resolved"].endswith("escape.txt")

    with pytest.raises(FileRejectedError):
        resolve_within(tmp_path, "uploads/../../escape.txt")


def test_resolve_within_allows_the_root_itself(tmp_path: Path) -> None:
    """Callers resolve the project directory itself before listing or creating it."""
    assert resolve_within(tmp_path) == tmp_path.resolve()
    assert resolve_within(tmp_path, ".") == tmp_path.resolve()


def test_resolve_within_rejects_an_absolute_part_pointing_outside_the_root(tmp_path: Path) -> None:
    """Joining an absolute path discards the root entirely, so containment must be re-proved."""
    outside = tmp_path.parent / "not-my-project"
    with pytest.raises(FileRejectedError):
        resolve_within(tmp_path, str(outside))


# ---------------------------------------------------------------------------
# unique_path
# ---------------------------------------------------------------------------


def test_unique_path_returns_the_plain_name_when_free(tmp_path: Path) -> None:
    directory = tmp_path / "projects" / "p1" / "uploads"
    chosen = unique_path(directory, "IO List.xlsx")

    assert chosen == directory / "IO List.xlsx"
    assert not chosen.exists()
    assert directory.is_dir()


def test_unique_path_numbers_successive_collisions(tmp_path: Path) -> None:
    """Two uploads of the same filename must both survive; neither may be overwritten."""
    first = unique_path(tmp_path, "IO List.xlsx")
    first.write_text("first", encoding="utf-8")

    second = unique_path(tmp_path, "IO List.xlsx")
    assert second.name == "IO List (2).xlsx"
    second.write_text("second", encoding="utf-8")

    third = unique_path(tmp_path, "IO List.xlsx")
    assert third.name == "IO List (3).xlsx"

    assert first.read_text(encoding="utf-8") == "first"


def test_unique_path_never_returns_an_existing_file(tmp_path: Path) -> None:
    chosen: list[Path] = []
    for index in range(4):
        candidate = unique_path(tmp_path, "Control Narrative.docx")
        assert not candidate.exists()
        candidate.write_text(str(index), encoding="utf-8")
        chosen.append(candidate)

    assert len({path.name for path in chosen}) == 4


def test_unique_path_handles_a_name_without_an_extension(tmp_path: Path) -> None:
    first = unique_path(tmp_path, "notes")
    assert first.name == "notes"
    first.write_text("x", encoding="utf-8")

    assert unique_path(tmp_path, "notes").name == "notes (2)"
