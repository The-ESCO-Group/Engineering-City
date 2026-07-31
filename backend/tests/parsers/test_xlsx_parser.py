"""Tests for the Excel workbook parser.

The happy path runs against ``fixtures/demo_project/Interlock_Schedule.xlsx``,
whose ground truth is stated in ``fixtures/README.md``: six interlocks on a
visible sheet, a seventh withdrawn on a **hidden** one, a documented air-pressure
threshold of 60.0 that the logic contradicts, a guard-door tag the logic spells
differently, and an interlock with no trip tag and no threshold at all. Those
exact values are what the Interlock Review Agent reasons over, so they are
asserted literally — including that the blanks stay blank, because a parser that
fills them in would licence a fabricated finding.

Degenerate and hostile inputs are built inline in ``tmp_path``, never committed.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
import zipfile
from collections import Counter
from pathlib import Path
from typing import Any

import openpyxl
import pytest
from openpyxl import Workbook
from openpyxl.chart import BarChart, Reference
from openpyxl.comments import Comment
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.table import Table

from app.core.config import Settings
from app.core.enums import AnchorType, DocumentRole, EvidenceKind, FileKind
from app.core.errors import ParserError
from app.parsers.base import (
    MAX_DISPLAY_LOCATION_CHARS,
    MAX_EXCERPT_CHARS,
    MAX_SOURCE_LABEL_CHARS,
    AnchorDraft,
    ParseContext,
    ParseResult,
    WarningCode,
)
from app.parsers.xlsx_parser import (
    MAX_CELL_CHARS,
    MAX_COLUMNS_PER_ROW,
    MAX_ROWS_PER_SHEET,
    MAX_WARNINGS_PER_CODE,
    XlsxParser,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
SCHEDULE_PATH = REPO_ROOT / "fixtures" / "demo_project" / "Interlock_Schedule.xlsx"

FILE_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")
PROJECT_ID = uuid.UUID("44444444-4444-4444-4444-444444444444")

INTERLOCK_HEADERS = [
    "Interlock ID",
    "Description",
    "Trip Tag",
    "Trip Condition",
    "Action",
    "Affected Equipment",
    "Fail-Safe Behaviour",
    "Notes",
]


def make_context(path: Path, settings: Settings) -> ParseContext:
    return ParseContext(
        file_id=FILE_ID,
        project_id=PROJECT_ID,
        path=path,
        filename=path.name,
        file_kind=FileKind.XLSX,
        document_role=DocumentRole.INTERLOCK_SCHEDULE,
        settings=settings,
    )


def new_workbook(title: str = "Data") -> tuple[Workbook, Any]:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = title
    return workbook, sheet


def parse_workbook(
    tmp_path: Path, settings: Settings, workbook: Workbook, name: str = "book.xlsx"
) -> ParseResult:
    path = tmp_path / name
    workbook.save(path)
    return XlsxParser().parse(make_context(path, settings))


def parse_bytes(
    tmp_path: Path, settings: Settings, data: bytes, name: str = "book.xlsx"
) -> ParseResult:
    path = tmp_path / name
    path.write_bytes(data)
    return XlsxParser().parse(make_context(path, settings))


def codes(result: ParseResult) -> list[str]:
    return [warning.code for warning in result.warnings]


def warning_with(result: ParseResult, code: str) -> Any:
    return next(warning for warning in result.warnings if warning.code == code)


def table_on(result: ParseResult, sheet: str) -> dict[str, Any]:
    return next(table for table in result.content["candidate_tables"] if table["sheet"] == sheet)


def tables_on(result: ParseResult, sheet: str) -> list[dict[str, Any]]:
    return [table for table in result.content["candidate_tables"] if table["sheet"] == sheet]


def sheet_of(result: ParseResult, name: str) -> dict[str, Any]:
    return next(sheet for sheet in result.content["worksheets"] if sheet["name"] == name)


def record_at(table: dict[str, Any], row: int) -> dict[str, Any]:
    return next(record for record in table["records"] if record["row"] == row)


def warnings_with(result: ParseResult, code: str) -> list[Any]:
    return [warning for warning in result.warnings if warning.code == code]


def rebuilt_package(tmp_path: Path, name: str, **members: bytes) -> Path:
    """The committed fixture with named members replaced or added.

    Building on the real workbook keeps everything except the one property under
    test valid, so a rejection cannot be explained by some other malformation.
    """
    path = tmp_path / name
    with (
        zipfile.ZipFile(SCHEDULE_PATH) as source,
        zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as target,
    ):
        for info in source.infolist():
            target.writestr(info.filename, members.pop(info.filename, source.read(info.filename)))
        for filename, payload in members.items():
            target.writestr(filename, payload)
    return path


#: A sheet part whose entity definitions expand exponentially. Rejected only
#: because openpyxl's defused XML reader is active.
ENTITY_BOMB_SHEET = b"""<?xml version="1.0"?>
<!DOCTYPE worksheet [
 <!ENTITY a "AAAAAAAAAA">
 <!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">
 <!ENTITY c "&b;&b;&b;&b;&b;&b;&b;&b;&b;&b;">
]>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>&c;</t></is></c></row></sheetData>
</worksheet>"""


def anchor_signature(anchors: list[AnchorDraft]) -> list[tuple[str, str, str, str]]:
    return [
        (anchor.key, anchor.anchor_type.value, anchor.display_location, anchor.excerpt)
        for anchor in anchors
    ]


@pytest.fixture
def schedule(settings: Settings) -> ParseResult:
    return XlsxParser().parse(make_context(SCHEDULE_PATH, settings))


# ---------------------------------------------------------------------------
# Declared interface
# ---------------------------------------------------------------------------


def test_the_parser_declares_itself_for_xlsx_files() -> None:
    parser = XlsxParser()
    assert parser.name == "xlsx"
    assert parser.handles == (FileKind.XLSX,)
    assert parser.version


def test_the_artifact_kind_is_the_xlsx_file_kind(schedule: ParseResult) -> None:
    assert schedule.artifact_kind == FileKind.XLSX.value
    assert schedule.parser_name == "xlsx"


# ---------------------------------------------------------------------------
# The committed fixture: ground truth from fixtures/README.md
# ---------------------------------------------------------------------------


def test_every_sheet_is_listed_including_the_hidden_one(schedule: ParseResult) -> None:
    workbook = schedule.content["workbook"]

    assert workbook["sheet_names"] == ["Interlocks", "Revisions", "Superseded"]
    assert workbook["hidden_sheet_names"] == ["Superseded"]
    assert [sheet["state"] for sheet in schedule.content["worksheets"]] == [
        "visible",
        "visible",
        "hidden",
    ]
    assert workbook["properties"]["creator"] == "openpyxl"
    assert workbook["defined_names"] == []


def test_the_hidden_sheet_raises_a_warning_naming_it(schedule: ParseResult) -> None:
    hidden = warning_with(schedule, WarningCode.HIDDEN_CONTENT)

    assert "Superseded" in hidden.message
    assert hidden.location == "Sheet 'Superseded'"
    # The two title rows above the named table produce no records, and the
    # Revisions and Superseded header rows are this parser's guess: all three are
    # disclosed rather than left for a reader to notice.
    assert codes(schedule) == [
        WarningCode.MERGED_CELLS,
        WarningCode.STRUCTURE_UNRECOGNISED,
        WarningCode.STRUCTURE_UNRECOGNISED,
        WarningCode.HIDDEN_CONTENT,
        WarningCode.STRUCTURE_UNRECOGNISED,
    ]
    assert [warning.location for warning in schedule.warnings] == [
        "Sheet 'Interlocks'",
        "Sheet 'Interlocks'",
        "Sheet 'Revisions', row 1",
        "Sheet 'Superseded'",
        "Sheet 'Superseded', row 1",
    ]


def test_the_hidden_sheet_content_is_still_recorded(schedule: ParseResult) -> None:
    """Hiding a sheet is what a review has to surface, so its cells are read, not skipped."""
    superseded = sheet_of(schedule, "Superseded")

    assert superseded["populated_cell_count"] == 6
    assert schedule.anchors_by_key["cell:Superseded!A2"].excerpt == "ILK-006"
    assert record_at(table_on(schedule, "Superseded"), 2)["values"] == {
        "Interlock ID": "ILK-006",
        "Description": "Reserved - do not reuse",
        "Status": "withdrawn",
    }


def test_the_named_table_supplies_the_header_row(schedule: ParseResult) -> None:
    interlocks = table_on(schedule, "Interlocks")

    assert sheet_of(schedule, "Interlocks")["tables"] == [
        {"name": "InterlockSchedule", "ref": "A4:H10"}
    ]
    assert interlocks["table_name"] == "InterlockSchedule"
    assert interlocks["detection"] == "named_table"
    assert interlocks["header_row"] == 4
    assert interlocks["headers"] == INTERLOCK_HEADERS
    assert interlocks["headers"][0] == "Interlock ID"
    assert interlocks["column_count"] == 8
    assert interlocks["header_cells"] == ["A4", "B4", "C4", "D4", "E4", "F4", "G4", "H4"]
    assert (interlocks["first_data_row"], interlocks["last_data_row"]) == (5, 10)

    # Nothing about the table itself was guessed. The one structure warning this
    # sheet raises is about the two title rows above it, which are outside the
    # declared range and therefore yield no records.
    unrecognised = [
        warning
        for warning in schedule.warnings
        if warning.code == WarningCode.STRUCTURE_UNRECOGNISED
        and warning.location.startswith("Sheet 'Interlocks'")
    ]
    assert len(unrecognised) == 1
    assert unrecognised[0].location == "Sheet 'Interlocks'"
    assert "rows 1-2" in unrecognised[0].message
    assert "row 4" not in unrecognised[0].message


def test_the_schedule_yields_the_six_documented_interlocks(schedule: ParseResult) -> None:
    records = table_on(schedule, "Interlocks")["records"]

    assert [record["values"]["Interlock ID"] for record in records] == [
        "ILK-001",
        "ILK-002",
        "ILK-003",
        "ILK-004",
        "ILK-005",
        "ILK-007",
    ]
    assert [record["row"] for record in records] == [5, 6, 7, 8, 9, 10]
    assert records[2]["ref"] == "A7:H7"
    assert schedule.summary["record_count"] == 9


def test_the_withdrawn_interlock_is_only_on_the_hidden_sheet(schedule: ParseResult) -> None:
    """ILK-006 is withdrawn: reading it as a live schedule row would be wrong."""
    interlocks = json.dumps(table_on(schedule, "Interlocks"))

    assert "ILK-006" not in interlocks
    assert "ILK-006" in json.dumps(table_on(schedule, "Superseded"))


def test_the_documented_air_pressure_threshold_is_recorded_verbatim(
    schedule: ParseResult,
) -> None:
    """40.0 in the logic against 60.0 here is the planted ILK-005 contradiction."""
    values = record_at(table_on(schedule, "Interlocks"), 9)["values"]

    assert values["Trip Condition"] == "AIR_PRESS_PSI < 60.0"
    assert values["Trip Tag"] == "AIR_PRESS_PSI"
    assert schedule.anchors_by_key["cell:Interlocks!D9"].excerpt == "AIR_PRESS_PSI < 60.0"


def test_the_documented_guard_door_tag_is_recorded_verbatim(schedule: ParseResult) -> None:
    """The logic spells this GRD_DR_CLSD; the mismatch only exists if both spellings survive."""
    values = record_at(table_on(schedule, "Interlocks"), 8)["values"]

    assert values["Trip Tag"] == "GUARD_DOOR_CLOSED"
    assert values["Trip Condition"] == "GUARD_DOOR_CLOSED = 0"
    assert schedule.anchors_by_key["cell:Interlocks!C8"].excerpt == "GUARD_DOOR_CLOSED"


def test_the_incomplete_interlock_keeps_its_blank_fields_blank(schedule: ParseResult) -> None:
    """ILK-007 has no threshold. Anything other than empty here invents one."""
    record = record_at(table_on(schedule, "Interlocks"), 10)

    assert record["values"]["Interlock ID"] == "ILK-007"
    assert record["values"]["Trip Tag"] is None
    assert record["values"]["Trip Condition"] is None
    assert record["values"]["Fail-Safe Behaviour"] is None
    assert record["values"]["Notes"] == "Threshold to be confirmed by process engineering."
    assert record["formulas"] == {}
    assert record["errors"] == {}

    keys = schedule.anchors_by_key
    assert "cell:Interlocks!C10" not in keys
    assert "cell:Interlocks!D10" not in keys
    assert "cell:Interlocks!G10" not in keys


def test_a_cell_anchor_addresses_the_interlock_id_cell(schedule: ParseResult) -> None:
    anchor = schedule.anchors_by_key["cell:Interlocks!A7"]

    assert anchor.anchor_type is AnchorType.XLSX_CELL
    assert anchor.display_location == "Interlock_Schedule.xlsx / Interlocks / A7"
    assert anchor.excerpt == "ILK-003"
    assert anchor.structured_location == {
        "sheet": "Interlocks",
        "ref": "A7",
        "row": 7,
        "column": "A",
        "column_index": 1,
        "header": "Interlock ID",
        "table_ref": "A4:H10",
        "detection": "named_table",
    }


def test_a_range_anchor_covers_the_table_and_each_of_its_rows(schedule: ParseResult) -> None:
    keys = schedule.anchors_by_key

    assert keys["range:Interlocks!A4:H10"].anchor_type is AnchorType.XLSX_RANGE
    assert keys["range:Interlocks!A4:H4"].excerpt == " | ".join(INTERLOCK_HEADERS)
    for row in range(5, 11):
        assert f"range:Interlocks!A{row}:H{row}" in keys

    row_anchor = keys["range:Interlocks!A9:H9"]
    assert row_anchor.display_location == "Interlock_Schedule.xlsx / Interlocks / A9:H9"
    assert "Trip Condition" in row_anchor.excerpt
    assert "AIR_PRESS_PSI < 60.0" in row_anchor.excerpt


def test_a_declared_table_is_direct_evidence_and_an_inferred_one_is_not(
    schedule: ParseResult,
) -> None:
    """The Revisions header row is this parser's reading, not something the file declares."""
    keys = schedule.anchors_by_key

    assert table_on(schedule, "Revisions")["detection"] == "heuristic"
    assert keys["range:Interlocks!A4:H10"].evidence_kind is EvidenceKind.DIRECT
    assert keys["range:Revisions!A1:D3"].evidence_kind is EvidenceKind.INFERENCE
    assert keys["cell:Revisions!A2"].evidence_kind is EvidenceKind.DIRECT


def test_the_merged_title_row_is_reported(schedule: ParseResult) -> None:
    merged = warning_with(schedule, WarningCode.MERGED_CELLS)

    assert sheet_of(schedule, "Interlocks")["merged_ranges"] == ["A1:H1"]
    assert "A1:H1" in merged.message
    assert merged.location == "Sheet 'Interlocks'"


def test_the_title_rows_above_the_table_are_recorded_but_not_made_into_records(
    schedule: ParseResult,
) -> None:
    rows = {row["row"]: row for row in sheet_of(schedule, "Interlocks")["rows"]}

    assert rows[1]["cells"][0]["value"].startswith("SYNTHETIC FIXTURE")
    assert [cell["ref"] for cell in rows[1]["cells"]] == ["A1"]
    assert 3 not in rows
    assert tables_on(schedule, "Interlocks")[0]["header_row"] == 4
    assert len(tables_on(schedule, "Interlocks")) == 1


def test_a_cell_records_its_reference_column_and_excel_type(schedule: ParseResult) -> None:
    rows = {row["row"]: row for row in sheet_of(schedule, "Interlocks")["rows"]}
    cell = next(cell for cell in rows[7]["cells"] if cell["ref"] == "C7")

    assert cell == {
        "ref": "C7",
        "column": "C",
        "column_index": 3,
        "value": "CV102_JAM",
        "formula": None,
        "data_type": "s",
    }


def test_the_summary_reports_shape_without_carrying_the_cell_values(
    schedule: ParseResult,
) -> None:
    summary = schedule.summary

    assert summary["sheet_count"] == 3
    assert summary["hidden_sheet_count"] == 1
    assert summary["hidden_sheet_names"] == ["Superseded"]
    assert summary["populated_cell_count"] == 73
    assert summary["formula_cell_count"] == 0
    assert summary["merged_range_count"] == 1
    assert summary["named_table_count"] == 1
    assert summary["candidate_table_count"] == 3
    assert summary["error_cell_count"] == 0
    # Measured from the committed fixture, not read back off the same parse:
    # comparing the summary to ``len(schedule.anchors)`` holds even when both are
    # zero, so it survives the parser losing every anchor it should have made.
    assert summary["anchor_count"] == 90
    assert summary["warning_count"] == 5
    assert summary["unsupported_count"] == 0
    assert [sheet["record_count"] for sheet in summary["sheets"]] == [6, 2, 1]
    assert "ILK-003" not in json.dumps(summary)


def test_every_anchor_carries_the_source_label_and_a_bounded_excerpt(
    schedule: ParseResult,
) -> None:
    assert schedule.anchors
    for anchor in schedule.anchors:
        assert anchor.source_label == "Interlock_Schedule.xlsx"
        assert anchor.display_location.startswith("Interlock_Schedule.xlsx")
        assert len(anchor.excerpt) <= MAX_EXCERPT_CHARS

    keys = [anchor.key for anchor in schedule.anchors]
    # Counts measured from the committed fixture rather than a uniqueness check:
    # the anchor sink drops a repeated key on the floor, so a key collision can
    # only ever show up as a *missing* anchor, never as a duplicated one.
    assert len(keys) == 90
    assert Counter(anchor.anchor_type for anchor in schedule.anchors) == {
        AnchorType.XLSX_CELL: 71,
        AnchorType.XLSX_RANGE: 15,
        AnchorType.XLSX_WORKSHEET: 3,
        AnchorType.XLSX_WORKBOOK: 1,
    }
    assert keys[0] == "workbook"
    assert schedule.anchors[0].anchor_type is AnchorType.XLSX_WORKBOOK
    assert keys[1:4] == ["sheet:Interlocks", "sheet:Revisions", "sheet:Superseded"]
    assert {anchor.anchor_type for anchor in schedule.anchors[1:4]} == {AnchorType.XLSX_WORKSHEET}


def test_the_cells_no_candidate_table_covers_are_not_citable_and_that_is_disclosed(
    schedule: ParseResult,
) -> None:
    """Only cells inside a candidate table get a cell anchor. Which ones is pinned here.

    The fixture's two title rows sit above the declared table, so ``A1`` and
    ``A2`` are recorded as cells but cannot be cited individually. That is a real
    limitation of this parser, and the exact set is asserted so it cannot widen
    silently: a table that stopped being detected would take dozens of citable
    cells with it while every other assertion in this file still passed.
    """
    populated = {
        f"cell:{sheet['name']}!{cell['ref']}"
        for sheet in schedule.content["worksheets"]
        for row in sheet["rows"]
        for cell in row["cells"]
    }
    cited = {anchor.key for anchor in schedule.anchors if anchor.key.startswith("cell:")}

    assert len(populated) == 73
    assert len(cited) == 71
    assert cited <= populated
    assert populated - cited == {"cell:Interlocks!A1", "cell:Interlocks!A2"}

    # The rows those two cells sit on are named in a warning, so "no records and
    # no citations came from here" is in the record rather than left to be noticed.
    uncovered = warnings_with(schedule, WarningCode.STRUCTURE_UNRECOGNISED)
    assert any("rows 1-2" in warning.message for warning in uncovered)


def test_a_long_filename_still_produces_a_label_the_column_can_hold(
    settings: Settings,
) -> None:
    """Every anchor is stamped with the source label, and the column bounds it."""
    context = ParseContext(
        file_id=FILE_ID,
        project_id=PROJECT_ID,
        path=SCHEDULE_PATH,
        filename="I" * 300 + ".xlsx",
        file_kind=FileKind.XLSX,
        document_role=DocumentRole.INTERLOCK_SCHEDULE,
        settings=settings,
        archive_member_path="package/" + "deep/" * 40 + "Interlock_Schedule.xlsx",
    )

    result = XlsxParser().parse(context)

    assert result.anchors
    for anchor in result.anchors:
        assert len(anchor.source_label) <= MAX_SOURCE_LABEL_CHARS
        assert len(anchor.display_location) <= MAX_DISPLAY_LOCATION_CHARS


def test_the_workbook_anchor_names_the_hidden_sheet(schedule: ParseResult) -> None:
    anchor = schedule.anchors_by_key["workbook"]

    assert anchor.display_location == "Interlock_Schedule.xlsx"
    assert "Hidden sheets: Superseded" in anchor.excerpt
    assert anchor.structured_location["hidden_sheet_names"] == ["Superseded"]


def test_the_output_contains_no_absolute_path(schedule: ParseResult) -> None:
    serialised = json.dumps(
        {
            "content": schedule.content,
            "summary": schedule.summary,
            "anchors": anchor_signature(schedule.anchors),
            "structured": [anchor.structured_location for anchor in schedule.anchors],
            "warnings": [warning.as_dict() for warning in schedule.warnings],
        }
    )

    assert str(REPO_ROOT) not in serialised
    assert REPO_ROOT.as_posix() not in serialised


def test_reparsing_the_same_fixture_produces_identical_output(settings: Settings) -> None:
    first = XlsxParser().parse(make_context(SCHEDULE_PATH, settings))
    second = XlsxParser().parse(make_context(SCHEDULE_PATH, settings))

    assert first.content == second.content
    assert first.summary == second.summary
    assert anchor_signature(first.anchors) == anchor_signature(second.anchors)
    assert [anchor.structured_location for anchor in first.anchors] == [
        anchor.structured_location for anchor in second.anchors
    ]
    assert [anchor.content_hash for anchor in first.anchors] == [
        anchor.content_hash for anchor in second.anchors
    ]
    assert [warning.as_dict() for warning in first.warnings] == [
        warning.as_dict() for warning in second.warnings
    ]
    assert [item.as_dict() for item in first.unsupported] == [
        item.as_dict() for item in second.unsupported
    ]


# ---------------------------------------------------------------------------
# Table detection
# ---------------------------------------------------------------------------


def test_a_declared_table_wins_over_the_row_a_heuristic_would_pick(
    tmp_path: Path, settings: Settings
) -> None:
    workbook, sheet = new_workbook("Schedule")
    sheet["A1"] = "Conveyor Line 3 interlock schedule, rev B"
    sheet["A2"] = "ID"
    sheet["B2"] = "Trip Tag"
    sheet["A3"] = "ILK-001"
    sheet["B3"] = "ESTOP_OK"
    sheet.add_table(Table(displayName="Declared", ref="A2:B3"))

    result = parse_workbook(tmp_path, settings, workbook)
    tables = result.content["candidate_tables"]

    assert len(tables) == 1
    assert tables[0]["detection"] == "named_table"
    assert tables[0]["table_name"] == "Declared"
    assert tables[0]["header_row"] == 2
    assert tables[0]["records"][0]["values"] == {"ID": "ILK-001", "Trip Tag": "ESTOP_OK"}


def test_a_blank_row_separates_two_inferred_tables(tmp_path: Path, settings: Settings) -> None:
    workbook, sheet = new_workbook("Two")
    sheet["A1"] = "Tag"
    sheet["B1"] = "Description"
    sheet["A2"] = "CV101_RUN"
    sheet["B2"] = "feed conveyor run"
    sheet["A4"] = "Alarm"
    sheet["B4"] = "Text"
    sheet["A5"] = "ALM_HOPPER_HIGH"
    sheet["B5"] = "hopper level high"

    result = parse_workbook(tmp_path, settings, workbook)
    tables = result.content["candidate_tables"]

    assert [table["ref"] for table in tables] == ["A1:B2", "A4:B5"]
    assert [table["header_row"] for table in tables] == [1, 4]
    assert [table["headers"] for table in tables] == [["Tag", "Description"], ["Alarm", "Text"]]
    assert all(table["detection"] == "heuristic" for table in tables)


def test_a_sheet_with_data_but_no_header_row_is_reported_as_unrecognised(
    tmp_path: Path, settings: Settings
) -> None:
    """Naming columns after numbers would be an invention; the cells still have to survive."""
    workbook, sheet = new_workbook("Readings")
    sheet["A1"] = 39.5
    sheet["B1"] = 60
    sheet["A2"] = 41.0
    sheet["B2"] = 60

    result = parse_workbook(tmp_path, settings, workbook)
    unrecognised = warning_with(result, WarningCode.STRUCTURE_UNRECOGNISED)

    assert result.content["candidate_tables"] == []
    assert unrecognised.location == "Sheet 'Readings'"
    assert "Readings" in unrecognised.message
    assert [cell["value"] for cell in sheet_of(result, "Readings")["rows"][0]["cells"]] == [
        39.5,
        60,
    ]
    assert "sheet:Readings" in result.anchors_by_key


def test_a_header_only_table_yields_no_records_and_no_invented_row(
    tmp_path: Path, settings: Settings
) -> None:
    workbook, sheet = new_workbook("Empty Table")
    sheet["A1"] = "Interlock ID"
    sheet["B1"] = "Trip Tag"
    sheet.add_table(Table(displayName="Headers", ref="A1:B1"))

    result = parse_workbook(tmp_path, settings, workbook)
    table = table_on(result, "Empty Table")

    assert table["records"] == []
    assert table["first_data_row"] is None
    assert table["last_data_row"] is None
    assert table["ref"] == "A1:B1"


def test_a_blank_header_cell_is_named_by_its_column_and_reported(
    tmp_path: Path, settings: Settings
) -> None:
    workbook, sheet = new_workbook("Gappy")
    sheet["A1"] = "Tag"
    sheet["C1"] = "Notes"
    sheet["A2"] = "CV102_JAM"
    sheet["B2"] = "unlabelled value"
    sheet["C2"] = "photo-eye"

    result = parse_workbook(tmp_path, settings, workbook)
    table = table_on(result, "Gappy")
    warning = warning_with(result, WarningCode.STRUCTURE_UNRECOGNISED)

    assert table["headers"] == ["Tag", "Column B", "Notes"]
    assert table["records"][0]["values"]["Column B"] == "unlabelled value"
    assert warning.location == "Sheet 'Gappy', cell B1"
    assert "Column B" in warning.message


def test_a_repeated_header_name_keeps_every_column_addressable(
    tmp_path: Path, settings: Settings
) -> None:
    workbook, sheet = new_workbook("Repeats")
    sheet["A1"] = "Notes"
    sheet["B1"] = "Notes"
    sheet["A2"] = "first note"
    sheet["B2"] = "second note"

    result = parse_workbook(tmp_path, settings, workbook)
    table = table_on(result, "Repeats")

    assert table["headers"] == ["Notes", "Notes"]
    assert table["record_keys"] == ["Notes", "Notes (2)"]
    assert table["records"][0]["values"] == {"Notes": "first note", "Notes (2)": "second note"}
    assert "Notes (2)" in warning_with(result, WarningCode.STRUCTURE_UNRECOGNISED).message


def test_a_value_outside_the_table_columns_is_reported_not_dropped(
    tmp_path: Path, settings: Settings
) -> None:
    workbook, sheet = new_workbook("Ragged")
    sheet["A1"] = "Tag"
    sheet["B1"] = "Description"
    sheet["A2"] = "CV101_RUN"
    sheet["B2"] = "feed conveyor run"
    sheet["D2"] = "stray comment"

    result = parse_workbook(tmp_path, settings, workbook)
    ragged = warning_with(result, WarningCode.RAGGED_ROWS)
    table = table_on(result, "Ragged")

    assert table["column_count"] == 2
    assert "stray comment" not in json.dumps(table)
    assert ragged.location == "Sheet 'Ragged', row 2"
    assert "D2" in ragged.message
    stray = next(
        cell
        for row in sheet_of(result, "Ragged")["rows"]
        for cell in row["cells"]
        if cell["ref"] == "D2"
    )
    assert stray["value"] == "stray comment"


def test_a_date_cell_is_recorded_as_an_iso_string(tmp_path: Path, settings: Settings) -> None:
    workbook, sheet = new_workbook("Dated")
    sheet["A1"] = "Rev"
    sheet["B1"] = "Date"
    sheet["A2"] = "B"
    sheet["B2"] = dt.datetime(2025, 1, 3, 8, 30)

    result = parse_workbook(tmp_path, settings, workbook)
    record = table_on(result, "Dated")["records"][0]

    assert record["values"]["Date"] == "2025-01-03T08:30:00"
    assert json.dumps(result.content)
    assert result.anchors_by_key["cell:Dated!B2"].excerpt == "2025-01-03T08:30:00"


def test_a_very_hidden_sheet_is_reported_like_a_hidden_one(
    tmp_path: Path, settings: Settings
) -> None:
    workbook, sheet = new_workbook("Visible")
    sheet["A1"] = "Tag"
    sheet["B1"] = "Description"
    sheet["A2"] = "CV101_RUN"
    sheet["B2"] = "feed conveyor run"
    archive = workbook.create_sheet("Archive")
    archive.append(["Interlock ID", "Status"])
    archive.append(["ILK-006", "withdrawn"])
    archive.sheet_state = "veryHidden"

    result = parse_workbook(tmp_path, settings, workbook)
    hidden = warning_with(result, WarningCode.HIDDEN_CONTENT)

    assert result.content["workbook"]["hidden_sheet_names"] == ["Archive"]
    assert "veryHidden" in hidden.message
    assert hidden.location == "Sheet 'Archive'"
    assert result.anchors_by_key["cell:Archive!A2"].excerpt == "ILK-006"


# ---------------------------------------------------------------------------
# Formulas
# ---------------------------------------------------------------------------


def test_a_formula_cell_records_the_formula_and_reports_it_unevaluated(
    tmp_path: Path, settings: Settings
) -> None:
    """The cached result is not loaded, so the value must read as unknown, not as zero."""
    workbook, sheet = new_workbook("Calc")
    sheet["A1"] = "Tag"
    sheet["B1"] = "Trip Setpoint"
    sheet["A2"] = "AIR_PRESS_PSI"
    sheet["B2"] = "=40+20"

    result = parse_workbook(tmp_path, settings, workbook)
    record = table_on(result, "Calc")["records"][0]
    warning = warning_with(result, WarningCode.FORMULA_NOT_EVALUATED)

    assert record["values"]["Trip Setpoint"] is None
    assert record["formulas"] == {"Trip Setpoint": "=40+20"}
    assert warning.location == "Sheet 'Calc', cell B2"
    assert "cached" in warning.message
    assert result.summary["formula_cell_count"] == 1

    cell = next(
        cell
        for row in sheet_of(result, "Calc")["rows"]
        for cell in row["cells"]
        if cell["ref"] == "B2"
    )
    assert cell == {
        "ref": "B2",
        "column": "B",
        "column_index": 2,
        "value": None,
        "formula": "=40+20",
        "data_type": "f",
    }
    assert result.anchors_by_key["cell:Calc!B2"].excerpt == "=40+20"


# ---------------------------------------------------------------------------
# Degenerate and hostile input
# ---------------------------------------------------------------------------


def test_a_workbook_with_a_single_empty_sheet_reports_an_empty_document(
    tmp_path: Path, settings: Settings
) -> None:
    workbook, _ = new_workbook("Blank")

    result = parse_workbook(tmp_path, settings, workbook)

    assert codes(result) == [WarningCode.EMPTY_DOCUMENT]
    assert result.content["candidate_tables"] == []
    assert sheet_of(result, "Blank")["rows"] == []
    assert sheet_of(result, "Blank")["populated_cell_count"] == 0
    assert [anchor.key for anchor in result.anchors] == ["workbook", "sheet:Blank"]
    assert result.summary["populated_cell_count"] == 0


def test_a_sheet_holding_only_a_single_cell_is_not_turned_into_a_table(
    tmp_path: Path, settings: Settings
) -> None:
    workbook, sheet = new_workbook("Note")
    sheet["A1"] = "Superseded - see revision C"

    result = parse_workbook(tmp_path, settings, workbook)

    assert result.content["candidate_tables"] == []
    assert codes(result) == [WarningCode.STRUCTURE_UNRECOGNISED]
    assert sheet_of(result, "Note")["rows"][0]["cells"][0]["value"] == (
        "Superseded - see revision C"
    )


def test_an_over_long_cell_is_shortened_and_the_shortening_is_reported(
    tmp_path: Path, settings: Settings
) -> None:
    workbook, sheet = new_workbook("Long")
    sheet["A1"] = "Tag"
    sheet["B1"] = "Notes"
    sheet["A2"] = "CV101_RUN"
    sheet["B2"] = "y" * (MAX_CELL_CHARS * 2)

    result = parse_workbook(tmp_path, settings, workbook)
    value = table_on(result, "Long")["records"][0]["values"]["Notes"]
    truncated = warning_with(result, WarningCode.TRUNCATED)

    assert len(value) == MAX_CELL_CHARS
    assert value.endswith("...")
    assert truncated.location == "Sheet 'Long', cell B2"
    assert len(result.anchors_by_key["cell:Long!B2"].excerpt) <= MAX_EXCERPT_CHARS


def test_columns_beyond_the_bound_are_reported_rather_than_dropped_quietly(
    tmp_path: Path, settings: Settings
) -> None:
    workbook, sheet = new_workbook("Wide")
    sheet["A1"] = "Tag"
    sheet["B1"] = "Description"
    sheet["A2"] = "CV101_RUN"
    sheet["B2"] = "feed conveyor run"
    sheet.cell(row=2, column=MAX_COLUMNS_PER_ROW + 4, value="beyond the bound")

    result = parse_workbook(tmp_path, settings, workbook)
    truncated = warning_with(result, WarningCode.TRUNCATED)

    assert "beyond the bound" not in json.dumps(result.content)
    assert truncated.location == "Sheet 'Wide'"
    assert get_column_letter(MAX_COLUMNS_PER_ROW) in truncated.message
    assert table_on(result, "Wide")["column_count"] == 2


def test_rows_beyond_the_bound_are_reported_rather_than_dropped_quietly(
    tmp_path: Path, settings: Settings
) -> None:
    workbook, sheet = new_workbook("Tall")
    sheet["A1"] = "Tag"
    sheet["B1"] = "Description"
    over_cap = MAX_ROWS_PER_SHEET + 1
    for row in range(2, over_cap + 1):
        sheet.cell(row=row, column=1, value=f"TAG_{row:05d}")
        sheet.cell(row=row, column=2, value="synthetic point")

    result = parse_workbook(tmp_path, settings, workbook)
    truncated = warning_with(result, WarningCode.TRUNCATED)

    assert len(sheet_of(result, "Tall")["rows"]) == MAX_ROWS_PER_SHEET
    assert sheet_of(result, "Tall")["max_row"] == over_cap
    assert str(over_cap) in truncated.message
    assert truncated.location == "Sheet 'Tall'"
    assert f"TAG_{over_cap:05d}" not in json.dumps(result.content)


def test_repeated_warnings_are_capped_and_the_suppression_is_disclosed(
    tmp_path: Path, settings: Settings
) -> None:
    """A cap keeps the record readable; dropping the remainder without saying so would not."""
    workbook, sheet = new_workbook("Many")
    sheet["A1"] = "Tag"
    sheet["B1"] = "Trip Setpoint"
    over_cap = MAX_WARNINGS_PER_CODE + 10
    for offset in range(over_cap):
        sheet.cell(row=2 + offset, column=1, value=f"TAG_{offset:03d}")
        sheet.cell(row=2 + offset, column=2, value="=40+20")

    result = parse_workbook(tmp_path, settings, workbook)
    formula_warnings = [
        warning for warning in result.warnings if warning.code == WarningCode.FORMULA_NOT_EVALUATED
    ]

    assert len(formula_warnings) == MAX_WARNINGS_PER_CODE + 1
    assert formula_warnings[-1].location == ""
    assert f"A further {over_cap - MAX_WARNINGS_PER_CODE}" in formula_warnings[-1].message
    assert result.summary["formula_cell_count"] == over_cap
    assert len(table_on(result, "Many")["records"]) == over_cap


def test_an_empty_file_raises_a_parser_error(tmp_path: Path, settings: Settings) -> None:
    with pytest.raises(ParserError) as excinfo:
        parse_bytes(tmp_path, settings, b"", name="nothing.xlsx")

    assert excinfo.value.details["filename"] == "nothing.xlsx"
    assert "empty" in excinfo.value.message
    assert str(tmp_path) not in excinfo.value.message


def test_a_non_xlsx_payload_raises_a_parser_error_not_a_zip_error(
    tmp_path: Path, settings: Settings
) -> None:
    """openpyxl would raise BadZipFile here, which says nothing an engineer can act on."""
    with pytest.raises(ParserError) as excinfo:
        parse_bytes(
            tmp_path,
            settings,
            b"Interlock ID,Trip Tag\nILK-001,ESTOP_OK\n",
            name="actually_a_csv.xlsx",
        )

    assert excinfo.value.details["filename"] == "actually_a_csv.xlsx"
    assert "ZIP" in excinfo.value.message


def test_a_zip_that_is_not_a_workbook_raises_a_parser_error(
    tmp_path: Path, settings: Settings
) -> None:
    path = tmp_path / "package.xlsx"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("readme.txt", "not a workbook")

    with pytest.raises(ParserError) as excinfo:
        XlsxParser().parse(make_context(path, settings))

    assert excinfo.value.details["filename"] == "package.xlsx"
    assert "could not be opened" in excinfo.value.message


def test_a_legacy_or_encrypted_workbook_is_named_as_such(
    tmp_path: Path, settings: Settings
) -> None:
    """An OLE2 header is the one case where 'not a zip' has a specific cause worth stating."""
    payload = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64

    with pytest.raises(ParserError) as excinfo:
        parse_bytes(tmp_path, settings, payload, name="legacy.xlsx")

    assert excinfo.value.details["signature"] == "ole2"
    assert "password-protected" in excinfo.value.message


def test_a_missing_file_raises_a_parser_error_without_leaking_the_path(
    tmp_path: Path, settings: Settings
) -> None:
    missing = tmp_path / "not_uploaded.xlsx"

    with pytest.raises(ParserError) as excinfo:
        XlsxParser().parse(make_context(missing, settings))

    assert excinfo.value.details["filename"] == "not_uploaded.xlsx"
    assert str(tmp_path) not in excinfo.value.message
    assert "not_uploaded.xlsx" in excinfo.value.message


# ---------------------------------------------------------------------------
# A heuristic header is a guess, and says so
# ---------------------------------------------------------------------------


def test_an_inferred_header_row_names_the_row_it_borrowed_the_column_names_from(
    tmp_path: Path, settings: Settings
) -> None:
    """The row chosen as a header contributes no record, so losing it must be visible.

    Here the second block's first row is a real interlock. Nothing in the file
    distinguishes it from a row of column names, so the parser cannot avoid the
    guess — but a reader enumerating records would otherwise never learn that
    ILK-005 and its 60 psi threshold are absent from them.
    """
    workbook, sheet = new_workbook("Interlocks")
    sheet["A1"] = "Interlock ID"
    sheet["B1"] = "Trip Condition"
    sheet["A2"] = "ILK-001"
    sheet["B2"] = "ESTOP_OK = 0"
    sheet["A4"] = "ILK-005"
    sheet["B4"] = "AIR_PRESS_PSI < 60.0"
    sheet["A5"] = "ILK-007"
    sheet["B5"] = "TBD"

    result = parse_workbook(tmp_path, settings, workbook)
    borrowed = table_on(result, "Interlocks")
    inferred = [
        warning
        for warning in warnings_with(result, WarningCode.STRUCTURE_UNRECOGNISED)
        if warning.location == "Sheet 'Interlocks', row 4"
    ]

    # The guess itself is unchanged and still wrong; what must not happen is
    # that it is silent.
    assert borrowed["headers"] == ["Interlock ID", "Trip Condition"]
    recorded = [
        value
        for table in tables_on(result, "Interlocks")
        for record in table["records"]
        for value in record["values"].values()
    ]
    assert "ILK-005" not in recorded
    assert "AIR_PRESS_PSI < 60.0" not in recorded
    assert len(inferred) == 1
    assert inferred[0].severity == "info"
    assert "ILK-005" in inferred[0].message
    assert "AIR_PRESS_PSI < 60.0" in inferred[0].message
    assert "heuristic" in inferred[0].message


def test_a_title_row_taken_as_a_header_is_reported_rather_than_presented_as_fact(
    tmp_path: Path, settings: Settings
) -> None:
    """A title line above a real header row is the same guess, seen from the other side."""
    workbook, sheet = new_workbook("Schedule")
    sheet["A1"] = "Conveyor Line 3"
    sheet["B1"] = "Revision B"
    sheet["A2"] = "Interlock ID"
    sheet["B2"] = "Trip Condition"
    sheet["A3"] = "ILK-005"
    sheet["B3"] = "AIR_PRESS_PSI < 60.0"

    result = parse_workbook(tmp_path, settings, workbook)
    table = table_on(result, "Schedule")
    inferred = warnings_with(result, WarningCode.STRUCTURE_UNRECOGNISED)

    assert table["headers"] == ["Conveyor Line 3", "Revision B"]
    assert [warning.location for warning in inferred] == ["Sheet 'Schedule', row 1"]
    assert "Conveyor Line 3" in inferred[0].message

    # Every citation that depends on the guess carries the reading, so a claim
    # about "the column named Conveyor Line 3" cannot pass as a fact about the file.
    keys = result.anchors_by_key
    assert keys["range:Schedule!A1:B1"].evidence_kind is EvidenceKind.INFERENCE
    assert keys["range:Schedule!A1:B3"].evidence_kind is EvidenceKind.INFERENCE
    assert keys["cell:Schedule!A3"].structured_location["detection"] == "heuristic"


def test_a_header_row_anchor_of_a_declared_table_stays_direct_evidence(
    schedule: ParseResult,
) -> None:
    """The contrast matters: a declared header row is a fact the workbook states."""
    keys = schedule.anchors_by_key

    assert keys["range:Interlocks!A4:H4"].evidence_kind is EvidenceKind.DIRECT
    assert keys["range:Interlocks!A4:H4"].structured_location["detection"] == "named_table"
    assert keys["range:Revisions!A1:D1"].evidence_kind is EvidenceKind.INFERENCE
    assert keys["range:Revisions!A1:D1"].structured_location["detection"] == "heuristic"
    assert keys["cell:Revisions!A2"].structured_location["detection"] == "heuristic"


def test_a_data_block_a_table_does_not_cover_is_reported_even_when_the_sheet_has_one(
    tmp_path: Path, settings: Settings
) -> None:
    """One structured table on a sheet must not silence the blocks around it."""
    workbook, sheet = new_workbook("Mixed")
    sheet["A1"] = "Tag"
    sheet["B1"] = "Description"
    sheet["A2"] = "CV101_RUN"
    sheet["B2"] = "feed conveyor run"
    sheet["A5"] = 39.5
    sheet["B5"] = 60
    sheet["A6"] = 41.0
    sheet["B6"] = 60

    result = parse_workbook(tmp_path, settings, workbook)
    uncovered = [
        warning
        for warning in warnings_with(result, WarningCode.STRUCTURE_UNRECOGNISED)
        if "rows 5-6" in warning.message
    ]

    assert [table["ref"] for table in result.content["candidate_tables"]] == ["A1:B2"]
    assert len(uncovered) == 1
    assert uncovered[0].location == "Sheet 'Mixed'"
    assert "Mixed" in uncovered[0].message
    assert "2 populated row(s)" in uncovered[0].message


def test_rows_above_a_declared_table_are_reported_as_yielding_no_records(
    schedule: ParseResult,
) -> None:
    """The fixture's own title rows are the same case, on the committed file."""
    uncovered = [
        warning
        for warning in warnings_with(schedule, WarningCode.STRUCTURE_UNRECOGNISED)
        if warning.location == "Sheet 'Interlocks'"
    ]

    assert len(uncovered) == 1
    assert "rows 1-2" in uncovered[0].message
    assert "SYNTHETIC FIXTURE" in json.dumps(sheet_of(schedule, "Interlocks")["rows"])


# ---------------------------------------------------------------------------
# What is and is not a formula
# ---------------------------------------------------------------------------


def test_a_string_that_begins_with_an_equals_sign_is_a_string_not_a_formula(
    tmp_path: Path, settings: Settings
) -> None:
    """Excel's type decides. Sniffing the text would blank a documented threshold.

    ``values[x] is None`` is how this parser says "the schedule states nothing
    here" (ILK-007). A populated cell must never read that way, and a
    FORMULA_NOT_EVALUATED warning must never assert a formula the file does not
    contain.
    """
    workbook, sheet = new_workbook("Sniff")
    sheet["A1"] = "Interlock ID"
    sheet["B1"] = "Trip Condition"
    sheet["A2"] = "ILK-005"
    sheet["B2"] = "=60 psi (per P&ID)"
    # Excel stores this as a string; only openpyxl's convenience setter guessed
    # otherwise from the leading '='.
    sheet["B2"].data_type = "s"

    result = parse_workbook(tmp_path, settings, workbook)
    record = table_on(result, "Sniff")["records"][0]
    cell = next(
        cell
        for row in sheet_of(result, "Sniff")["rows"]
        for cell in row["cells"]
        if cell["ref"] == "B2"
    )

    assert record["values"]["Trip Condition"] == "=60 psi (per P&ID)"
    assert record["formulas"] == {}
    assert cell["value"] == "=60 psi (per P&ID)"
    assert cell["formula"] is None
    assert cell["data_type"] == "s"
    assert WarningCode.FORMULA_NOT_EVALUATED not in codes(result)
    assert result.summary["formula_cell_count"] == 0


# ---------------------------------------------------------------------------
# What is and is not an error value
# ---------------------------------------------------------------------------


def test_an_error_cell_is_reported_and_marked_in_the_record(
    tmp_path: Path, settings: Settings
) -> None:
    """``#REF!`` in a setpoint column is a broken cell, not a documented setpoint.

    The marker text survives in ``values`` — deleting it would make the cell read
    like ILK-007's genuinely empty field — but a consumer reading records has to
    be able to tell it apart from the same characters typed in as text, which is
    what ``errors`` and the warning are for.
    """
    workbook, sheet = new_workbook("Setpoints")
    sheet["A1"] = "Tag"
    sheet["B1"] = "Trip Setpoint"
    sheet["A2"] = "AIR_PRESS_PSI"
    sheet["B2"] = "#REF!"
    sheet["A3"] = "HOPPER_LEVEL_PCT"
    sheet["B3"] = "#DIV/0!"

    result = parse_workbook(tmp_path, settings, workbook)
    table = table_on(result, "Setpoints")
    reported = [
        warning
        for warning in warnings_with(result, WarningCode.STRUCTURE_UNRECOGNISED)
        if warning.location == "Sheet 'Setpoints', cell B2"
    ]

    assert record_at(table, 2)["values"]["Trip Setpoint"] == "#REF!"
    assert record_at(table, 2)["errors"] == {"Trip Setpoint": "#REF!"}
    assert record_at(table, 3)["errors"] == {"Trip Setpoint": "#DIV/0!"}
    assert record_at(table, 2)["formulas"] == {}
    assert result.summary["error_cell_count"] == 2
    assert len(reported) == 1
    assert "#REF!" in reported[0].message
    assert "error cell" in reported[0].message
    # The parser says what Excel typed, not what the cell was meant to hold.
    assert "was meant to hold is not established" in reported[0].message


def test_a_text_cell_that_reads_like_an_error_is_not_reported_as_one(
    tmp_path: Path, settings: Settings
) -> None:
    """Excel's type decides here too: sniffing the text would invent a broken cell."""
    workbook, sheet = new_workbook("Typed")
    sheet["A1"] = "Tag"
    sheet["B1"] = "Notes"
    sheet["A2"] = "AIR_PRESS_PSI"
    sheet["B2"] = "#REF! appeared here before rev B; value confirmed as 60.0"
    # openpyxl types a bare "#REF!" as an error; this text is a string, and the
    # committed workbook stores it as one.
    assert sheet["B2"].data_type == "s"

    result = parse_workbook(tmp_path, settings, workbook)
    record = record_at(table_on(result, "Typed"), 2)

    assert record["values"]["Notes"].startswith("#REF!")
    assert record["errors"] == {}
    assert result.summary["error_cell_count"] == 0
    assert [warning.location for warning in result.warnings] == ["Sheet 'Typed', row 1"]


def test_error_cells_do_not_bury_the_other_warnings(tmp_path: Path, settings: Settings) -> None:
    """The per-code cap applies here as it does to formulas, and says how many it held back."""
    workbook, sheet = new_workbook("Broken")
    sheet["A1"] = "Tag"
    sheet["B1"] = "Trip Setpoint"
    over_cap = MAX_WARNINGS_PER_CODE + 5
    for offset in range(over_cap):
        sheet.cell(row=2 + offset, column=1, value=f"TAG_{offset:03d}")
        sheet.cell(row=2 + offset, column=2, value="#REF!")

    result = parse_workbook(tmp_path, settings, workbook)
    structure = warnings_with(result, WarningCode.STRUCTURE_UNRECOGNISED)

    # The sheet's heuristic-header disclosure carries the same code, so it is the
    # last thing held back: 55 error cells plus that one row-1 warning, of which
    # 50 are emitted and 6 are counted in the suppression notice.
    assert result.summary["error_cell_count"] == over_cap
    assert len(structure) == MAX_WARNINGS_PER_CODE + 1
    assert f"A further {over_cap - MAX_WARNINGS_PER_CODE + 1} " in structure[-1].message
    assert all(record["errors"] for record in table_on(result, "Broken")["records"])


# ---------------------------------------------------------------------------
# Content that is in the file but not on the screen
# ---------------------------------------------------------------------------


def test_a_hidden_row_and_hidden_column_are_reported_not_read_as_live_data(
    tmp_path: Path, settings: Settings
) -> None:
    """Hiding a row in place conceals it exactly as moving it to a hidden sheet does."""
    workbook, sheet = new_workbook("Hidden")
    sheet["A1"] = "Interlock ID"
    sheet["B1"] = "Trip Condition"
    sheet["C1"] = "Notes"
    sheet["A2"] = "ILK-001"
    sheet["B2"] = "ESTOP_OK = 0"
    sheet["C2"] = "current"
    sheet["A3"] = "ILK-009"
    sheet["B3"] = "SECRET_TAG = 1"
    sheet["C3"] = "superseded note"
    sheet.row_dimensions[3].hidden = True
    sheet.column_dimensions["C"].hidden = True

    result = parse_workbook(tmp_path, settings, workbook)
    hidden = warning_with(result, WarningCode.HIDDEN_CONTENT)
    worksheet = sheet_of(result, "Hidden")

    assert worksheet["hidden_rows"] == [3]
    assert worksheet["hidden_columns"] == ["C"]
    assert hidden.location == "Sheet 'Hidden'"
    assert "row(s) 3" in hidden.message
    assert "column(s) C" in hidden.message
    assert result.summary["hidden_row_count"] == 1
    assert result.summary["hidden_column_count"] == 1
    # Still recorded in full, exactly like a hidden sheet.
    assert record_at(table_on(result, "Hidden"), 3)["values"]["Trip Condition"] == "SECRET_TAG = 1"


def test_a_hidden_row_holding_nothing_is_not_reported_as_concealed_content(
    tmp_path: Path, settings: Settings
) -> None:
    """Layout is not content; warning about an empty hidden row would be noise."""
    workbook, sheet = new_workbook("Spaced")
    sheet["A1"] = "Tag"
    sheet["B1"] = "Description"
    sheet["A2"] = "CV101_RUN"
    sheet["B2"] = "feed conveyor run"
    sheet.row_dimensions[9].hidden = True

    result = parse_workbook(tmp_path, settings, workbook)

    assert sheet_of(result, "Spaced")["hidden_rows"] == []
    assert WarningCode.HIDDEN_CONTENT not in codes(result)


def test_a_cell_comment_is_recorded_and_reported_not_dropped(
    tmp_path: Path, settings: Settings
) -> None:
    """A comment is a normal home for the engineering note that contradicts the cell."""
    workbook, sheet = new_workbook("Notes")
    sheet["A1"] = "Interlock ID"
    sheet["B1"] = "Trip Condition"
    sheet["A2"] = "ILK-005"
    sheet["B2"] = "AIR_PRESS_PSI < 60.0"
    sheet["B2"].comment = Comment("Process eng says this should be 40 psi", "R. Patel")

    result = parse_workbook(tmp_path, settings, workbook)
    hidden = warning_with(result, WarningCode.HIDDEN_CONTENT)

    assert sheet_of(result, "Notes")["comments"] == [
        {
            "ref": "B2",
            "author": "R. Patel",
            "text": "Process eng says this should be 40 psi",
        }
    ]
    assert hidden.location == "Sheet 'Notes'"
    assert "B2" in hidden.message
    assert result.summary["comment_count"] == 1
    # The comment is not silently folded into the cell's value either.
    assert record_at(table_on(result, "Notes"), 2)["values"]["Trip Condition"] == (
        "AIR_PRESS_PSI < 60.0"
    )


def test_a_comment_on_an_empty_cell_still_reaches_the_record(
    tmp_path: Path, settings: Settings
) -> None:
    workbook, sheet = new_workbook("Empty Note")
    sheet["A1"] = "Interlock ID"
    sheet["B1"] = "Trip Condition"
    sheet["A2"] = "ILK-007"
    sheet["B2"].comment = Comment("No threshold agreed yet.", "R. Patel")

    result = parse_workbook(tmp_path, settings, workbook)

    assert [entry["ref"] for entry in sheet_of(result, "Empty Note")["comments"]] == ["B2"]
    assert "B2" in warning_with(result, WarningCode.HIDDEN_CONTENT).message


def test_a_tab_that_is_not_a_worksheet_is_named_instead_of_vanishing(
    tmp_path: Path, settings: Settings
) -> None:
    """A chart sheet is a tab a human sees; leaving it out understates the workbook."""
    workbook, sheet = new_workbook("Data")
    sheet["A1"] = "Tag"
    sheet["B1"] = "Value"
    sheet["A2"] = "AIR_PRESS_PSI"
    sheet["B2"] = 39
    chartsheet = workbook.create_chartsheet("Trend")
    chart = BarChart()
    chart.add_data(Reference(sheet, min_col=2, min_row=1, max_row=2), titles_from_data=True)
    chartsheet.add_chart(chart)

    result = parse_workbook(tmp_path, settings, workbook)
    unsupported = [item for item in result.unsupported if item.kind == "non_worksheet_tab"]

    assert result.content["workbook"]["sheet_names"] == ["Data"]
    assert result.content["workbook"]["unread_sheet_names"] == ["Trend"]
    assert result.summary["unread_sheet_count"] == 1
    assert len(unsupported) == 1
    assert "Trend" in unsupported[0].detail
    assert "Chartsheet" in unsupported[0].detail
    assert unsupported[0].location == "Sheet 'Trend'"
    assert "Trend" in result.anchors_by_key["workbook"].excerpt


def test_a_workbook_whose_only_content_is_an_unread_tab_is_not_called_empty(
    tmp_path: Path, settings: Settings
) -> None:
    """ "Nothing to review" would be a conclusion about content never looked at."""
    workbook, sheet = new_workbook("Data")
    chartsheet = workbook.create_chartsheet("Trend")
    chart = BarChart()
    chart.add_data(Reference(sheet, min_col=1, min_row=1, max_row=1))
    chartsheet.add_chart(chart)

    result = parse_workbook(tmp_path, settings, workbook)
    empty = warning_with(result, WarningCode.EMPTY_DOCUMENT)

    assert "Trend" in empty.message
    assert "nothing in it to review" not in empty.message
    assert result.content["workbook"]["unread_sheet_names"] == ["Trend"]


# ---------------------------------------------------------------------------
# The package itself
# ---------------------------------------------------------------------------


def test_an_openpyxl_failure_does_not_leak_the_path_on_disk(
    tmp_path: Path, settings: Settings
) -> None:
    """openpyxl's own message names the absolute path, and that message reaches the client."""
    path = rebuilt_package(
        tmp_path, "entities.xlsx", **{"xl/worksheets/sheet1.xml": ENTITY_BOMB_SHEET}
    )

    with pytest.raises(ParserError) as excinfo:
        XlsxParser().parse(make_context(path, settings))

    assert excinfo.value.details["filename"] == "entities.xlsx"
    assert str(tmp_path) not in excinfo.value.message
    assert tmp_path.as_posix() not in excinfo.value.message
    assert "entities.xlsx" in excinfo.value.message
    assert "could not be opened" in excinfo.value.message


def test_a_zip_that_is_not_a_workbook_does_not_leak_the_path_either(
    tmp_path: Path, settings: Settings
) -> None:
    path = tmp_path / "package.xlsx"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("readme.txt", "not a workbook")

    with pytest.raises(ParserError) as excinfo:
        XlsxParser().parse(make_context(path, settings))

    assert str(tmp_path) not in excinfo.value.message


def test_a_package_that_expands_far_beyond_its_size_is_refused_before_it_is_read(
    tmp_path: Path, settings: Settings
) -> None:
    """openpyxl materialises the whole workbook, so the bound has to bite first."""
    path = rebuilt_package(tmp_path, "padded.xlsx", **{"xl/pad.xml": b" " * (8 * 1024 * 1024)})

    with pytest.raises(ParserError) as excinfo:
        XlsxParser().parse(make_context(path, settings))

    assert path.stat().st_size < 1024 * 1024
    assert excinfo.value.details["filename"] == "padded.xlsx"
    assert excinfo.value.details["ratio"] > settings.max_archive_compression_ratio
    assert "zip bomb" in excinfo.value.message
    assert str(tmp_path) not in excinfo.value.message


def test_the_committed_fixture_is_well_inside_the_package_bounds(settings: Settings) -> None:
    """The guard above must not be able to reject a real workbook."""
    with zipfile.ZipFile(SCHEDULE_PATH) as package:
        infos = package.infolist()
    uncompressed = sum(info.file_size for info in infos)
    compressed = sum(info.compress_size for info in infos)

    assert uncompressed / compressed < settings.max_archive_compression_ratio / 10
    assert max(info.file_size for info in infos) < settings.max_xml_bytes
    assert XlsxParser().parse(make_context(SCHEDULE_PATH, settings)).content["candidate_tables"]


def test_an_entity_bomb_workbook_is_rejected(tmp_path: Path, settings: Settings) -> None:
    path = rebuilt_package(tmp_path, "bomb.xlsx", **{"xl/worksheets/sheet1.xml": ENTITY_BOMB_SHEET})

    with pytest.raises(ParserError):
        XlsxParser().parse(make_context(path, settings))


def test_the_parser_refuses_to_run_when_the_xml_entity_defences_are_off(
    tmp_path: Path, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An .xlsx is XML, and this is the only parser that does not use xml_safety.

    openpyxl turns its own defences off when ``defusedxml`` is missing or when
    ``OPENPYXL_DEFUSEDXML`` is set. Depending on that silently means an entity
    bomb expands inside the worker with nothing in this repository to blame.
    """
    workbook, sheet = new_workbook("Data")
    sheet["A1"] = "Tag"
    sheet["B1"] = "Description"
    sheet["A2"] = "CV101_RUN"
    sheet["B2"] = "feed conveyor run"
    path = tmp_path / "plain.xlsx"
    workbook.save(path)
    monkeypatch.setattr(openpyxl, "DEFUSEDXML", False)

    with pytest.raises(ParserError) as excinfo:
        XlsxParser().parse(make_context(path, settings))

    assert excinfo.value.details["error"] == "xml_defences_disabled"
    assert "defusedxml" in excinfo.value.message
    assert str(tmp_path) not in excinfo.value.message
