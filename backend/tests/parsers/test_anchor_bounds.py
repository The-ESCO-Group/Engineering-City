"""Anchor fields must fit the columns they are stored in.

``AnchorDraft`` bounds ``source_label``, ``display_location`` and ``key`` in
``__post_init__``, centrally, so no parser has to remember the limits.

The bug this prevents is a quiet one. A long filename — or a file pulled out of a
deeply nested archive, where the label carries the member path too — composes
values past ``String(255)`` and ``String(512)``. SQLite does not enforce column
lengths, so the demo stores the excess and works; PostgreSQL raises on the same
upload. Truncating where the anchor is built keeps both backends behaving the
same, and keeps the failure out of the middle of an ingest.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from app.core.config import Settings
from app.core.enums import AnchorType, DocumentRole, FileKind
from app.models.evidence import MAX_EXCERPT_CHARS
from app.parsers.base import (
    MAX_ANCHOR_KEY_CHARS,
    MAX_DISPLAY_LOCATION_CHARS,
    MAX_SOURCE_LABEL_CHARS,
    AnchorDraft,
    ParseContext,
)
from app.parsers.registry import parser_for

REPO_ROOT = Path(__file__).resolve().parents[3]
FIXTURES = REPO_ROOT / "fixtures" / "demo_project"

#: Every parser that runs against a committed fixture.
FIXTURE_CASES: tuple[tuple[str, FileKind], ...] = (
    ("Conveyor_Line3.L5X", FileKind.L5X),
    ("Interlock_Schedule.xlsx", FileKind.XLSX),
    ("IO_List.csv", FileKind.CSV),
    ("Functional_Description.docx", FileKind.DOCX),
    ("Testing_Notes.pdf", FileKind.PDF),
    ("HMI_Alarm_Export.xml", FileKind.XML),
    ("Site_Notes_Ambiguous.txt", FileKind.TEXT),
)


def test_the_bounds_match_the_database_columns() -> None:
    """The two must not drift apart; the column is the reason the bound exists."""
    from app.models.evidence import EvidenceAnchor

    columns = EvidenceAnchor.__table__.columns
    assert columns["source_label"].type.length == MAX_SOURCE_LABEL_CHARS
    assert columns["display_location"].type.length == MAX_DISPLAY_LOCATION_CHARS
    assert columns["anchor_key"].type.length == MAX_ANCHOR_KEY_CHARS


def test_an_over_long_label_and_location_are_truncated() -> None:
    anchor = AnchorDraft(
        key="k" * (MAX_ANCHOR_KEY_CHARS * 2),
        anchor_type=AnchorType.FILE_LINE,
        display_location="d" * (MAX_DISPLAY_LOCATION_CHARS * 2),
        source_label="s" * (MAX_SOURCE_LABEL_CHARS * 2),
        excerpt="e" * (MAX_EXCERPT_CHARS * 2),
    )
    assert len(anchor.source_label) == MAX_SOURCE_LABEL_CHARS
    assert len(anchor.display_location) == MAX_DISPLAY_LOCATION_CHARS
    assert len(anchor.key) == MAX_ANCHOR_KEY_CHARS
    assert len(anchor.excerpt) == MAX_EXCERPT_CHARS


def test_truncation_is_visible_rather_than_a_silent_cut() -> None:
    """A cut value ends in an ellipsis, so a reader can tell it was shortened."""
    anchor = AnchorDraft(
        key="k",
        anchor_type=AnchorType.FILE_LINE,
        display_location="d" * (MAX_DISPLAY_LOCATION_CHARS + 50),
        source_label="s" * (MAX_SOURCE_LABEL_CHARS + 50),
    )
    assert anchor.display_location.endswith("...")
    assert anchor.source_label.endswith("...")


def test_values_within_the_bounds_are_left_alone() -> None:
    anchor = AnchorDraft(
        key="rung:MainProgram/Conveyor_Interlocks/0",
        anchor_type=AnchorType.L5X_RUNG,
        display_location="MainProgram / Conveyor_Interlocks / Rung 0",
        source_label="Conveyor_Line3.L5X",
        excerpt="XIC(ESTOP_OK)OTE(LINE_PERMISSIVE);",
    )
    assert anchor.display_location == "MainProgram / Conveyor_Interlocks / Rung 0"
    assert anchor.source_label == "Conveyor_Line3.L5X"
    assert not anchor.key.endswith("...")


def test_a_long_filename_from_a_deep_archive_stays_within_bounds(
    tmp_path: Path, settings: Settings
) -> None:
    """The real path to the overflow: a long name plus an archive member path.

    ``ParseContext.source_label`` composes both, and nothing upstream bounds a
    filename, so this is reachable from an ordinary upload.
    """
    name = ("A" * 200) + ".txt"
    path = tmp_path / name
    path.write_text("HEADING\n=======\n\nBody text.\n" * 5, encoding="utf-8")

    context = ParseContext(
        file_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        path=path,
        filename=name,
        file_kind=FileKind.TEXT,
        document_role=DocumentRole.OTHER,
        settings=settings,
        archive_member_path="/".join("nested" * 10 for _ in range(8)),
    )
    assert len(context.source_label) > MAX_SOURCE_LABEL_CHARS, (
        "the fixture must actually exceed the bound, or this proves nothing"
    )

    result = parser_for(FileKind.TEXT).parse(context)
    assert result.anchors
    for anchor in result.anchors:
        assert len(anchor.source_label) <= MAX_SOURCE_LABEL_CHARS
        assert len(anchor.display_location) <= MAX_DISPLAY_LOCATION_CHARS
        assert len(anchor.key) <= MAX_ANCHOR_KEY_CHARS


@pytest.mark.parametrize(("filename", "kind"), FIXTURE_CASES)
def test_every_parser_emits_storable_anchors(
    filename: str, kind: FileKind, settings: Settings
) -> None:
    """Across every parser, on the committed fixtures."""
    context = ParseContext(
        file_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        path=FIXTURES / filename,
        filename=filename,
        file_kind=kind,
        document_role=DocumentRole.OTHER,
        settings=settings,
    )
    result = parser_for(kind).parse(context)
    assert result.anchors, f"{filename} produced no anchors"

    for anchor in result.anchors:
        assert len(anchor.source_label) <= MAX_SOURCE_LABEL_CHARS, anchor.key
        assert len(anchor.display_location) <= MAX_DISPLAY_LOCATION_CHARS, anchor.key
        assert len(anchor.key) <= MAX_ANCHOR_KEY_CHARS, anchor.key
        assert len(anchor.excerpt) <= MAX_EXCERPT_CHARS, anchor.key
