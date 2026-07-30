"""Delimited-text parsing.

The committed fixtures carry the ground truth (``fixtures/README.md``): the I/O
list opens with a ``#`` banner that must not become the header, it wires
``CV102_JAM`` — the interlock no rung implements — and the alarm list is
semicolon-delimited, so a parser that assumes a comma turns all five alarms into
one column of nonsense and still looks successful.

Degenerate and hostile inputs are built inline: a parser must say it could not
read something, never return quietly-empty content and never leak a bare
``_csv.Error``.
"""

from __future__ import annotations

import csv
import uuid
from collections.abc import Callable
from pathlib import Path

import pytest

from app.core.config import Settings
from app.core.enums import AnchorType, DocumentRole, EvidenceKind, FileKind
from app.core.errors import ParserError
from app.parsers import csv_parser
from app.parsers.base import AnchorDraft, ParseContext, Parser, ParseResult, WarningCode
from app.parsers.registry import parser_for

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "demo_project"

IO_LIST_COMMENT = "# SYNTHETIC FIXTURE - Conveyor Line 3 I/O list. Invented data."
IO_LIST_HEADERS = [
    "Tag",
    "Description",
    "Type",
    "Module",
    "Address",
    "Signal",
    "Engineering Units",
]

ParseBytes = Callable[[str, bytes], ParseResult]


def _context(path: Path, settings: Settings) -> ParseContext:
    return ParseContext(
        file_id=uuid.UUID(int=1),
        project_id=uuid.UUID(int=2),
        path=path,
        filename=path.name,
        file_kind=FileKind.CSV,
        document_role=DocumentRole.IO_LIST,
        settings=settings,
    )


def _parse_fixture(name: str, settings: Settings) -> ParseResult:
    return csv_parser.CsvParser().parse(_context(FIXTURES / name, settings))


@pytest.fixture
def parse_bytes(tmp_path: Path, settings: Settings) -> ParseBytes:
    """Parse bytes written to a throwaway file, for inputs no fixture should hold."""

    def _parse(name: str, data: bytes) -> ParseResult:
        path = tmp_path / name
        path.write_bytes(data)
        return csv_parser.CsvParser().parse(_context(path, settings))

    return _parse


@pytest.fixture
def io_list(settings: Settings) -> ParseResult:
    return _parse_fixture("IO_List.csv", settings)


@pytest.fixture
def alarm_list(settings: Settings) -> ParseResult:
    return _parse_fixture("Alarm_List.csv", settings)


def _codes(result: ParseResult) -> list[str]:
    return [warning.code for warning in result.warnings]


def _messages(result: ParseResult, code: str) -> list[str]:
    return [warning.message for warning in result.warnings if warning.code == code]


def _row(result: ParseResult, column: str, value: str) -> dict[str, object]:
    matches = [row for row in result.content["rows"] if row["record"].get(column) == value]
    assert len(matches) == 1, f"expected exactly one row with {column}={value!r}"
    return matches[0]


def _of_type(result: ParseResult, anchor_type: AnchorType) -> list[AnchorDraft]:
    return [anchor for anchor in result.anchors if anchor.anchor_type is anchor_type]


# ---------------------------------------------------------------------------
# IO_List.csv — comma-delimited, with a leading comment line
# ---------------------------------------------------------------------------


def test_the_leading_comment_line_is_skipped_and_does_not_become_the_header(
    io_list: ParseResult,
) -> None:
    assert io_list.content["skipped_lines"] == [
        {"row": 1, "reason": csv_parser.REASON_COMMENT, "text": IO_LIST_COMMENT}
    ]
    assert io_list.content["headers"] == IO_LIST_HEADERS
    assert io_list.content["header_row_number"] == 2


def test_the_comment_line_is_also_recorded_as_content_deliberately_not_interpreted(
    io_list: ParseResult,
) -> None:
    assert [item.kind for item in io_list.unsupported] == ["csv_comment_line"]
    assert io_list.unsupported[0].location == "IO_List.csv / row 1"
    assert "skipped_lines" in io_list.unsupported[0].detail


def test_thirteen_data_rows_are_numbered_by_their_real_line_in_the_file(
    io_list: ParseResult,
) -> None:
    rows = io_list.content["rows"]
    assert len(rows) == 13
    assert [row["row"] for row in rows] == list(range(3, 16))
    assert io_list.summary["row_count"] == 13
    assert io_list.summary["column_count"] == 7


def test_cv102_jam_is_found_at_its_real_row_number(io_list: ParseResult) -> None:
    """The planted missing interlock: wired in the I/O list, never in the L5X."""
    row = _row(io_list, "Tag", "CV102_JAM")

    assert row["row"] == 15
    assert row["record"] == {
        "Tag": "CV102_JAM",
        "Description": "CV102 jam photo-eye blocked",
        "Type": "DI",
        "Module": "DI_01",
        "Address": "DI_01:I.Data.7",
        "Signal": "24VDC",
        "Engineering Units": "",
    }
    assert row["ragged"] is False

    anchors = io_list.anchors_by_key
    assert anchors["row:15"].display_location == "IO_List.csv / row 15"
    assert anchors["row:15"].excerpt == (
        "CV102_JAM,CV102 jam photo-eye blocked,DI,DI_01,DI_01:I.Data.7,24VDC,"
    )
    assert anchors["cell:15:Tag"].display_location == "IO_List.csv / row 15, column 'Tag'"
    assert anchors["cell:15:Tag"].excerpt == "CV102_JAM"


def test_the_dialect_records_the_comma_the_crlf_endings_and_the_absent_bom(
    io_list: ParseResult,
) -> None:
    dialect = dict(io_list.content["dialect"])
    confidence = dialect.pop("encoding_confidence")

    assert dialect == {
        "delimiter": ",",
        "quotechar": '"',
        "encoding": "ascii",
        "line_terminator": "\r\n",
        "had_bom": False,
    }
    assert 0.9 <= confidence <= 1.0


def test_both_inferences_are_reported_because_a_csv_declares_neither(
    io_list: ParseResult,
) -> None:
    """A wrong delimiter or encoding is invisible downstream unless it is stated."""
    assert WarningCode.ENCODING_GUESSED in _codes(io_list)
    assert WarningCode.DELIMITER_GUESSED in _codes(io_list)

    assert "'ascii'" in _messages(io_list, WarningCode.ENCODING_GUESSED)[0]
    delimiter_message = _messages(io_list, WarningCode.DELIMITER_GUESSED)[0]
    assert "','" in delimiter_message
    assert "csv.Sniffer" in delimiter_message
    assert all(warning.location == "IO_List.csv" for warning in io_list.warnings)


def test_every_row_and_every_populated_cell_is_anchored(io_list: ParseResult) -> None:
    rows = _of_type(io_list, AnchorType.CSV_ROW)
    cells = _of_type(io_list, AnchorType.CSV_CELL)

    assert [anchor.key for anchor in rows] == [f"row:{number}" for number in range(2, 16)]
    assert len(cells) == 80
    assert io_list.summary["populated_cell_count"] == 80

    keys = io_list.anchors_by_key
    assert "cell:14:Engineering Units" in keys  # HOPPER_LEVEL_PCT is measured in %
    assert "cell:3:Engineering Units" not in keys  # ESTOP_OK has no units to cite

    assert keys["row:2"].structured_location == {"row": 2, "kind": "header"}
    assert keys["cell:14:Engineering Units"].structured_location == {
        "row": 14,
        "column": "Engineering Units",
        "column_index": 7,
    }
    assert {anchor.source_label for anchor in io_list.anchors} == {"IO_List.csv"}
    assert {anchor.evidence_kind for anchor in io_list.anchors} == {EvidenceKind.DIRECT}


def test_the_summary_describes_the_shape_without_carrying_the_rows(
    io_list: ParseResult,
) -> None:
    """The summary is shown without loading content, so it must stay a shape hint."""
    assert set(io_list.summary) == {
        "row_count",
        "column_count",
        "columns",
        "header_row_number",
        "delimiter",
        "encoding",
        "skipped_line_count",
        "ragged_row_count",
        "populated_cell_count",
        "truncated",
    }
    assert io_list.summary["columns"] == IO_LIST_HEADERS
    assert io_list.summary["skipped_line_count"] == 1
    assert io_list.summary["ragged_row_count"] == 0
    assert io_list.summary["truncated"] is False


def test_the_result_identifies_the_parser_and_the_artifact_kind(io_list: ParseResult) -> None:
    assert io_list.artifact_kind == FileKind.CSV.value
    assert io_list.parser_name == "csv"
    assert io_list.parser_version == csv_parser.CsvParser.version


def test_the_parser_is_registered_for_csv_and_satisfies_the_protocol() -> None:
    parser = parser_for(FileKind.CSV)

    assert isinstance(parser, csv_parser.CsvParser)
    assert isinstance(parser, Parser)
    assert parser.handles == (FileKind.CSV,)


# ---------------------------------------------------------------------------
# Alarm_List.csv — semicolon-delimited
# ---------------------------------------------------------------------------


def test_the_semicolon_delimiter_is_detected(alarm_list: ParseResult) -> None:
    """Sniffed as ',' the whole file becomes one column that still parses cleanly."""
    assert alarm_list.content["dialect"]["delimiter"] == ";"
    assert alarm_list.summary["delimiter"] == ";"
    assert alarm_list.content["column_count"] > 1
    assert alarm_list.content["headers"] == [
        "Alarm Tag",
        "Message",
        "Severity",
        "Group",
        "Source Tag",
    ]
    assert "';'" in _messages(alarm_list, WarningCode.DELIMITER_GUESSED)[0]


def test_five_alarm_rows_are_read_including_alm_hopper_high(alarm_list: ParseResult) -> None:
    assert [row["record"]["Alarm Tag"] for row in alarm_list.content["rows"]] == [
        "ALM_ESTOP",
        "ALM_CV101_OL",
        "ALM_CV102_OL",
        "ALM_HOPPER_HIGH",
        "ALM_CV102_JAM",
    ]

    hopper = _row(alarm_list, "Alarm Tag", "ALM_HOPPER_HIGH")
    assert hopper["row"] == 5
    assert hopper["record"]["Source Tag"] == "HOPPER_LEVEL_PCT"
    assert hopper["values"] == [
        "ALM_HOPPER_HIGH",
        "Surge hopper level high",
        "Medium",
        "Process",
        "HOPPER_LEVEL_PCT",
    ]

    anchor = alarm_list.anchors_by_key["cell:5:Alarm Tag"]
    assert anchor.display_location == "Alarm_List.csv / row 5, column 'Alarm Tag'"
    assert anchor.excerpt == "ALM_HOPPER_HIGH"


def test_the_alarm_list_has_no_comment_or_blank_lines_to_skip(alarm_list: ParseResult) -> None:
    assert alarm_list.content["skipped_lines"] == []
    assert alarm_list.content["header_row_number"] == 1
    assert alarm_list.unsupported == []


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["IO_List.csv", "Alarm_List.csv"])
def test_re_parsing_the_same_bytes_produces_identical_output(name: str, settings: Settings) -> None:
    first = _parse_fixture(name, settings)
    second = _parse_fixture(name, settings)

    assert first.content == second.content
    assert first.summary == second.summary
    assert [warning.as_dict() for warning in first.warnings] == [
        warning.as_dict() for warning in second.warnings
    ]
    assert [item.as_dict() for item in first.unsupported] == [
        item.as_dict() for item in second.unsupported
    ]

    def fingerprint(result: ParseResult) -> list[tuple[str, str, str, str, str]]:
        return [
            (
                anchor.key,
                anchor.anchor_type.value,
                anchor.display_location,
                anchor.excerpt,
                anchor.content_hash,
            )
            for anchor in result.anchors
        ]

    assert fingerprint(first) == fingerprint(second)
    assert [anchor.structured_location for anchor in first.anchors] == [
        anchor.structured_location for anchor in second.anchors
    ]


# ---------------------------------------------------------------------------
# Degenerate and hostile input
# ---------------------------------------------------------------------------


def test_an_empty_file_is_reported_as_empty_rather_than_parsed(parse_bytes: ParseBytes) -> None:
    result = parse_bytes("empty.csv", b"")

    assert WarningCode.EMPTY_DOCUMENT in _codes(result)
    assert result.content["headers"] == []
    assert result.content["rows"] == []
    assert result.content["column_count"] == 0
    assert result.content["header_row_number"] == 0
    assert result.anchors == []
    assert result.summary["row_count"] == 0


def test_a_file_of_only_comments_and_blank_lines_reports_no_header_row(
    parse_bytes: ParseBytes,
) -> None:
    result = parse_bytes("notes.csv", b"# a note\n\n# another note\n")

    assert WarningCode.EMPTY_DOCUMENT in _codes(result)
    assert result.content["skipped_lines"] == [
        {"row": 1, "reason": csv_parser.REASON_COMMENT, "text": "# a note"},
        {"row": 2, "reason": csv_parser.REASON_BLANK, "text": ""},
        {"row": 3, "reason": csv_parser.REASON_COMMENT, "text": "# another note"},
    ]
    assert result.content["rows"] == []


def test_a_header_with_no_data_rows_says_so(parse_bytes: ParseBytes) -> None:
    result = parse_bytes("header_only.csv", b"Tag,Description,Type\n")

    assert result.content["headers"] == ["Tag", "Description", "Type"]
    assert result.content["rows"] == []
    assert WarningCode.EMPTY_DOCUMENT in _codes(result)
    assert "no data rows" in _messages(result, WarningCode.EMPTY_DOCUMENT)[0]
    assert [anchor.key for anchor in result.anchors] == ["row:1"]


def test_a_ragged_row_is_recorded_as_found_and_its_row_number_named(
    parse_bytes: ParseBytes,
) -> None:
    result = parse_bytes(
        "ragged.csv",
        b"Tag,Description,Type\r\n"
        b"CV101_OL_TRIP,CV101 overload,DI\r\n"
        b"CV102_JAM,no type recorded\r\n"
        b"CV103_VFD_FLT,CV103 fault,DI,extra field\r\n",
    )

    short, long = result.content["rows"][1], result.content["rows"][2]
    assert short["row"] == 3
    assert short["values"] == ["CV102_JAM", "no type recorded"]
    assert short["missing_columns"] == ["Type"]
    assert short["record"] == {"Tag": "CV102_JAM", "Description": "no type recorded"}
    assert long["row"] == 4
    assert long["extra_values"] == ["extra field"]
    assert long["values"] == ["CV103_VFD_FLT", "CV103 fault", "DI", "extra field"]
    assert [row["ragged"] for row in result.content["rows"]] == [False, True, True]

    assert WarningCode.RAGGED_ROWS in _codes(result)
    assert "rows 3, 4" in _messages(result, WarningCode.RAGGED_ROWS)[0]
    assert result.summary["ragged_row_count"] == 2


def test_a_value_beyond_the_header_gets_no_cell_anchor_but_stays_visible(
    parse_bytes: ParseBytes,
) -> None:
    """There is no column to address it by, so it is quoted through the row anchor."""
    result = parse_bytes("wide.csv", b"Tag,Type\r\nCV102_JAM,DI,orphan\r\n")

    assert result.content["rows"][0]["extra_values"] == ["orphan"]
    assert [anchor.key for anchor in result.anchors] == [
        "row:1",
        "row:2",
        "cell:2:Tag",
        "cell:2:Type",
    ]
    assert "orphan" in result.anchors_by_key["row:2"].excerpt


def test_a_utf8_bom_is_recorded_and_kept_out_of_the_first_heading(
    parse_bytes: ParseBytes,
) -> None:
    result = parse_bytes("bom.csv", b"\xef\xbb\xbfTag,Description\nCV102_JAM,jam eye\n")

    assert result.content["dialect"]["had_bom"] is True
    assert result.content["headers"] == ["Tag", "Description"]
    assert "cell:2:Tag" in result.anchors_by_key


def test_a_latin1_byte_that_is_not_valid_utf8_still_parses(parse_bytes: ParseBytes) -> None:
    result = parse_bytes(
        "latin1.csv",
        b"Tag,Description\nAIR_PRESS_PSI,caf\xe9 side header\nCV101_RUN_CMD,run command\n",
    )

    description = _row(result, "Tag", "AIR_PRESS_PSI")["record"]["Description"]
    assert description == "café side header"
    assert not [
        message
        for message in _messages(result, WarningCode.ENCODING_GUESSED)
        if "U+FFFD" in message
    ], "the byte was decoded, not replaced"
    assert WarningCode.ENCODING_GUESSED in _codes(result)
    assert result.content["dialect"]["encoding"] not in {"ascii", "utf_8"}
    assert len(result.content["rows"]) == 2


def test_a_tab_separated_file_is_detected(parse_bytes: ParseBytes) -> None:
    result = parse_bytes(
        "tabs.csv",
        b"Tag\tDescription\tType\nESTOP_OK\tE-stop healthy\tDI\nCV102_JAM\tjam eye\tDI\n",
    )

    assert result.content["dialect"]["delimiter"] == "\t"
    assert result.content["column_count"] == 3
    assert _row(result, "Tag", "CV102_JAM")["record"]["Type"] == "DI"


def test_bytes_that_are_not_text_degrade_to_warnings_rather_than_an_exception(
    parse_bytes: ParseBytes,
) -> None:
    result = parse_bytes("binary.csv", b"\x00\x01\x02\xff\xfe\x81\x8d\x8f\x90\x9d" * 20)

    assert WarningCode.EMPTY_DOCUMENT in _codes(result)
    replaced = [
        message
        for message in _messages(result, WarningCode.ENCODING_GUESSED)
        if "U+FFFD" in message
    ]
    assert replaced, "undecodable bytes must be reported, not silently replaced"
    assert result.content["rows"] == []


def test_a_field_over_the_csv_limit_becomes_a_warning_not_a_library_error(
    parse_bytes: ParseBytes,
) -> None:
    """``csv`` raises ``_csv.Error`` here; leaking it would lose the whole file."""
    oversized = b"h1,h2\r\nCV101_OL_TRIP," + b"x" * (csv.field_size_limit() + 1)

    result = parse_bytes("oversized.csv", oversized + b"\r\nCV102_JAM,readable\r\n")

    assert WarningCode.STRUCTURE_UNRECOGNISED in _codes(result)
    message = _messages(result, WarningCode.STRUCTURE_UNRECOGNISED)[0]
    assert "field larger than field limit" in message
    assert "1 line(s) were not read" in message
    assert result.content["skipped_lines"] == [
        {"row": 3, "reason": csv_parser.REASON_PARSE_STOPPED, "text": "CV102_JAM,readable"}
    ]
    assert result.summary["truncated"] is True


def test_duplicate_and_blank_headings_are_disambiguated_and_reported(
    parse_bytes: ParseBytes,
) -> None:
    """A repeated key would drop a whole column out of every record silently."""
    result = parse_bytes("headings.csv", b"Tag,Tag,,Tag\r\nCV102_JAM,DI,DI_01,24VDC\r\n")

    assert result.content["headers"] == ["Tag", "Tag", "", "Tag"]
    assert result.content["column_names"] == ["Tag", "Tag_2", "column_3", "Tag_3"]
    assert result.content["rows"][0]["record"] == {
        "Tag": "CV102_JAM",
        "Tag_2": "DI",
        "column_3": "DI_01",
        "Tag_3": "24VDC",
    }
    assert WarningCode.STRUCTURE_UNRECOGNISED in _codes(result)
    message = _messages(result, WarningCode.STRUCTURE_UNRECOGNISED)[0]
    assert "blank heading" in message
    assert "repeats the heading 'Tag'" in message


def test_a_line_break_inside_a_quoted_field_does_not_shift_the_row_numbers(
    parse_bytes: ParseBytes,
) -> None:
    """A '#' inside a quoted field is data; only a line starting one is a comment."""
    result = parse_bytes(
        "quoted.csv",
        b'Tag,Note\r\nCV102_JAM,"jam eye\r\n# not a comment\r\n\r\nsecond paragraph"\r\n'
        b"CV103_VFD_FLT,plain\r\n",
    )

    rows = result.content["rows"]
    assert [row["row"] for row in rows] == [2, 6]
    assert rows[0]["record"]["Note"] == "jam eye\r\n# not a comment\r\n\r\nsecond paragraph"
    assert result.content["skipped_lines"] == []
    assert WarningCode.STRUCTURE_UNRECOGNISED in _codes(result)
    assert "row 2 spans lines 2-5" in _messages(result, WarningCode.STRUCTURE_UNRECOGNISED)[0]


def test_a_blank_line_between_rows_is_skipped_without_renumbering_what_follows(
    parse_bytes: ParseBytes,
) -> None:
    result = parse_bytes("gap.csv", b"Tag,Type\r\nESTOP_OK,DI\r\n\r\nCV102_JAM,DI\r\n")

    assert [row["row"] for row in result.content["rows"]] == [2, 4]
    assert result.content["skipped_lines"] == [
        {"row": 3, "reason": csv_parser.REASON_BLANK, "text": ""}
    ]
    assert result.anchors_by_key["row:4"].excerpt == "CV102_JAM,DI"


def test_reaching_the_row_limit_is_reported_instead_of_dropping_rows(
    parse_bytes: ParseBytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(csv_parser, "MAX_ROWS", 2)

    result = parse_bytes("many.csv", b"Tag,Type\r\nA,DI\r\nB,DI\r\nC,DI\r\nD,DI\r\n")

    assert len(result.content["rows"]) == 2
    assert result.summary["truncated"] is True
    assert WarningCode.TRUNCATED in _codes(result)
    assert "2 further line(s)" in _messages(result, WarningCode.TRUNCATED)[0]
    assert result.content["skipped_lines"] == [
        {"row": 4, "reason": csv_parser.REASON_ROW_LIMIT, "text": "C,DI"}
    ]


def test_a_file_of_exactly_the_row_limit_is_not_reported_as_truncated(
    parse_bytes: ParseBytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing was dropped, so claiming truncation would send a reviewer hunting."""
    monkeypatch.setattr(csv_parser, "MAX_ROWS", 2)

    result = parse_bytes("exact.csv", b"Tag,Type\r\nA,DI\r\nB,DI\r\n")

    assert len(result.content["rows"]) == 2
    assert result.summary["truncated"] is False
    assert WarningCode.TRUNCATED not in _codes(result)
    assert result.content["skipped_lines"] == []


def test_reaching_the_cell_anchor_limit_leaves_every_row_still_anchored(
    parse_bytes: ParseBytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(csv_parser, "MAX_CELL_ANCHORS", 3)

    result = parse_bytes("cells.csv", b"Tag,Type\r\nA,DI\r\nB,DI\r\nC,DI\r\n")

    assert [anchor.key for anchor in result.anchors] == [
        "row:1",
        "row:2",
        "cell:2:Tag",
        "cell:2:Type",
        "row:3",
        "cell:3:Tag",
        "row:4",
    ]
    assert WarningCode.TRUNCATED in _codes(result)
    assert "out of 6" in _messages(result, WarningCode.TRUNCATED)[0]
    assert result.summary["populated_cell_count"] == 6
    assert result.summary["truncated"] is True


def test_an_unreadable_file_raises_a_parser_error_without_leaking_the_path(
    tmp_path: Path, settings: Settings
) -> None:
    unreadable = tmp_path / "IO_List.csv"
    unreadable.mkdir()

    with pytest.raises(ParserError) as excinfo:
        csv_parser.CsvParser().parse(_context(unreadable, settings))

    assert excinfo.value.code == "parser_error"
    assert str(tmp_path) not in excinfo.value.message
    assert excinfo.value.details == {"source": "IO_List.csv"}
