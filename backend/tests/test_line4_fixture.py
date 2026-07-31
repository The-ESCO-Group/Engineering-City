"""The Line 4 fixture, asserted against the ground truth its README states.

``fixtures/line4_packaging_conveyor/README.md`` is the contract the whole
acceptance scenario rests on: it names, for each of seven planted issues, the
file, the row or the rung a correct review must cite. A contract nothing checks
goes stale within a week, so every claim in it is asserted here — against the
committed bytes, not against the generator's own data tables where that would
make the check circular.

Three groups of tests:

* **Determinism** — two runs of the generator produce identical bytes, and the
  committed text artifacts are what the current generator produces.
* **The seven planted issues** — each is genuinely present, at the location the
  README cites, and the README cites it in those words.
* **Parser cleanliness** — every artifact parses with the real parser, and the
  L5X in particular resolves every operand it uses. Issues 4 and 5 are only
  findable by comparing files, and that is only true if nothing inside the
  export already gives them away.
"""

from __future__ import annotations

import csv
import importlib.util
import types
import uuid
from pathlib import Path
from typing import Any

import pytest

from app.core.config import Settings
from app.core.enums import DocumentRole, FileKind
from app.parsers.base import ParseContext, ParseResult, normalise_tag_name
from app.parsers.registry import parser_for

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "fixtures" / "line4_packaging_conveyor"
GENERATOR = REPO_ROOT / "scripts" / "backend" / "make_line4_fixture.py"
README = FIXTURES / "README.md"

#: Artifacts whose bytes depend on nothing but this repository's own code. The
#: OOXML pair is excluded on purpose: openpyxl and python-docx decide those
#: bytes, so pinning them here would turn a dependency bump into a fixture
#: failure. Their determinism is covered by ``test_regeneration_is_byte_identical``.
PURE_TEXT_ARTIFACTS = (
    "IO_List.csv",
    "Conveyor_Line4_Base.L5X",
    "HMI_Screens.xml",
    "Alarm_Register.csv",
    "Approved_Templates.txt",
)

#: What each artifact is parsed as, and what the parser must report. The counts
#: are quoted in the README's "Parser expectations" table.
PARSE_PLAN: tuple[tuple[str, FileKind, DocumentRole, int, int], ...] = (
    ("Project_Description.docx", FileKind.DOCX, DocumentRole.OTHER, 26, 0),
    (
        "Functional_Description.docx",
        FileKind.DOCX,
        DocumentRole.FUNCTIONAL_DESCRIPTION,
        60,
        0,
    ),
    ("IO_List.csv", FileKind.CSV, DocumentRole.IO_LIST, 265, 2),
    ("Interlock_Matrix.xlsx", FileKind.XLSX, DocumentRole.INTERLOCK_SCHEDULE, 165, 0),
    ("Conveyor_Line4_Base.L5X", FileKind.L5X, DocumentRole.CONTROL_LOGIC, 146, 0),
    ("HMI_Screens.xml", FileKind.XML, DocumentRole.HMI_EXPORT, 101, 0),
    ("Alarm_Register.csv", FileKind.CSV, DocumentRole.ALARM_LIST, 70, 2),
    ("Approved_Templates.txt", FileKind.TEXT, DocumentRole.OTHER, 45, 0),
)


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def generator() -> types.ModuleType:
    """The generator, loaded by path rather than imported.

    ``scripts/`` is not a package and is not on the path; loading by location
    keeps that true instead of making the test suite depend on it changing.
    """
    spec = importlib.util.spec_from_file_location("make_line4_fixture", GENERATOR)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def readme() -> str:
    return README.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def l5x_text() -> str:
    return (FIXTURES / "Conveyor_Line4_Base.L5X").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def hmi_text() -> str:
    return (FIXTURES / "HMI_Screens.xml").read_text(encoding="utf-8")


def _rows(name: str) -> list[tuple[int, dict[str, str]]]:
    """Data rows of a fixture CSV as ``(physical line number, record)``.

    Read with the stdlib rather than through the parser so that a change in the
    parser cannot quietly move the row numbers the README cites.
    """
    lines = (FIXTURES / name).read_text(encoding="utf-8").splitlines()
    records: list[tuple[int, dict[str, str]]] = []
    header: list[str] = []
    for number, line in enumerate(lines, start=1):
        if line.startswith("#"):
            continue
        values = next(csv.reader([line]))
        if not header:
            header = values
            continue
        records.append((number, dict(zip(header, values, strict=True))))
    return records


def _parse(name: str, kind: FileKind, role: DocumentRole, settings: Settings) -> ParseResult:
    parser = parser_for(kind)
    return parser.parse(
        ParseContext(
            file_id=uuid.UUID(int=1),
            project_id=uuid.UUID(int=2),
            path=FIXTURES / name,
            filename=name,
            file_kind=kind,
            document_role=role,
            settings=settings,
        )
    )


@pytest.fixture(scope="module")
def matrix_records() -> dict[str, dict[str, Any]]:
    """The interlock matrix, keyed by interlock ID, with its spreadsheet row."""
    from openpyxl import load_workbook

    sheet = load_workbook(FIXTURES / "Interlock_Matrix.xlsx")["Interlock Matrix"]
    headers = [cell.value for cell in sheet[1]]
    records: dict[str, dict[str, Any]] = {}
    for row in sheet.iter_rows(min_row=2):
        record = dict(zip(headers, [cell.value for cell in row], strict=True))
        record["_row"] = row[0].row
        records[str(record["Interlock ID"])] = record
    return records


def _rung_texts(l5x: str) -> list[str]:
    """Every ``<Text>`` body in the export, in document order."""
    parts: list[str] = []
    for chunk in l5x.split("<Text><![CDATA[")[1:]:
        parts.append(chunk.split("]]></Text>")[0])
    return parts


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_regeneration_is_byte_identical(generator: types.ModuleType, tmp_path: Path) -> None:
    """Two runs must agree byte for byte, OOXML packages included."""
    first = tmp_path / "first"
    second = tmp_path / "second"
    generator.generate(first)
    generator.generate(second)
    for name in generator.ARTIFACTS:
        assert (first / name).read_bytes() == (second / name).read_bytes(), (
            f"{name} is not deterministic"
        )


def test_committed_artifacts_are_current(generator: types.ModuleType, tmp_path: Path) -> None:
    """The checked-in text artifacts are what the generator produces today."""
    fresh = tmp_path / "fresh"
    generator.generate(fresh)
    for name in PURE_TEXT_ARTIFACTS:
        assert (fresh / name).read_bytes() == (FIXTURES / name).read_bytes(), (
            f"{name} is stale; re-run scripts/backend/make_line4_fixture.py"
        )


def test_readme_names_every_artifact(generator: types.ModuleType, readme: str) -> None:
    for name in generator.ARTIFACTS:
        assert name in readme, f"{name} is not described in the fixture README"
        assert (FIXTURES / name).exists()


# ---------------------------------------------------------------------------
# Issue 1 - ILK-404 is documented and wired, and implemented nowhere
# ---------------------------------------------------------------------------


def test_ilk404_is_documented_as_existing(matrix_records: dict[str, dict[str, Any]]) -> None:
    record = matrix_records["ILK-404"]
    assert record["_row"] == 5, "the README cites Interlock Matrix row 5"
    assert record["Trip Tag"] == "PE402_BLOCKED"
    assert record["Scope"] == "Existing", (
        "ILK-404 must be existing scope, or its absence from the base export is expected"
    )
    assert "Stop CV401" in str(record["Action"])


def test_ilk404_is_wired_in_the_io_list() -> None:
    matches = [
        (number, row)
        for number, row in _rows("IO_List.csv")
        if row["Device Tag"] == "PE402_BLOCKED"
    ]
    assert len(matches) == 1
    number, row = matches[0]
    assert number == 10, "the README cites IO_List.csv row 10"
    assert row["Address"] == "Local:1:I.Data.7"
    assert row["Status"] == "Existing"


def test_ilk404_is_absent_from_the_logic(l5x_text: str) -> None:
    """Not merely unimplemented: PE402 does not occur in the export at all."""
    assert "PE402" not in l5x_text


def test_readme_cites_issue_one(readme: str) -> None:
    for citation in ("ILK-404", "PE402_BLOCKED", "Local:1:I.Data.7", "**row 10**", "**row 5**"):
        assert citation in readme


# ---------------------------------------------------------------------------
# Issue 2 - one physical address carries two devices
# ---------------------------------------------------------------------------


def test_exactly_one_io_address_is_duplicated() -> None:
    rows = _rows("IO_List.csv")
    addresses = [row["Address"] for _, row in rows]
    duplicated = sorted({item for item in addresses if addresses.count(item) > 1})
    assert duplicated == ["Local:1:I.Data.10"]

    sharing = [(number, row) for number, row in rows if row["Address"] == "Local:1:I.Data.10"]
    assert [number for number, _ in sharing] == [13, 18], "the README cites rows 13 and 18"
    assert [row["Device Tag"] for _, row in sharing] == ["GD401_CLOSED", "MTR403_FAULT"]
    # Channel and address agree on both rows, so this is one defect and not two.
    assert {row["Channel"] for _, row in sharing} == {"10"}
    assert {row["Slot"] for _, row in sharing} == {"1"}


def test_both_duplicated_signals_are_used_in_logic(l5x_text: str) -> None:
    """The defect is in the I/O assignment; the logic reads both signals."""
    permissive = [text for text in _rung_texts(l5x_text) if "OTE(LINE_PERMISSIVE)" in text]
    assert len(permissive) == 1
    assert "XIC(GD401_CLOSED)" in permissive[0]
    assert "XIO(MTR403_FAULT)" in permissive[0]


def test_readme_cites_issue_two(readme: str) -> None:
    for citation in ("Local:1:I.Data.10", "**row 13**", "**row 18**", "GD401_CLOSED"):
        assert citation in readme


# ---------------------------------------------------------------------------
# Issue 3 - the same signal under two names
# ---------------------------------------------------------------------------


def test_cv401_run_command_is_named_two_ways(l5x_text: str) -> None:
    matches = [
        (number, row)
        for number, row in _rows("IO_List.csv")
        if row["Address"] == "Local:2:O.Data.0"
    ]
    assert len(matches) == 1
    number, row = matches[0]
    assert number == 20, "the README cites IO_List.csv row 20"
    assert row["Device Tag"] == "MTR401_RUN_CMD"

    assert 'Name="CV401_Run_Output"' in l5x_text
    assert "MTR401_RUN_CMD" not in l5x_text
    # The L5X tag names the same physical address, which is what identifies the
    # two as one signal rather than two.
    assert "Local:2:O.Data.0" in l5x_text


def test_the_two_names_do_not_normalise_together() -> None:
    assert normalise_tag_name("MTR401_RUN_CMD") != normalise_tag_name("CV401_Run_Output")


def test_the_sibling_run_commands_do_match(l5x_text: str) -> None:
    """What makes issue 3 a defect rather than a house style."""
    io_tags = {row["Device Tag"] for _, row in _rows("IO_List.csv")}
    for sibling in ("MTR402_RUN_CMD", "MTR403_RUN_CMD"):
        assert sibling in io_tags
        assert f'Name="{sibling}"' in l5x_text


def test_readme_cites_issue_three(readme: str) -> None:
    for citation in ("MTR401_RUN_CMD", "CV401_Run_Output", "**row 20**", "Local:2:O.Data.0"):
        assert citation in readme


# ---------------------------------------------------------------------------
# Issue 4 - an alarm nothing drives
# ---------------------------------------------------------------------------


def test_stop_gate_alarm_is_defined_and_declared(l5x_text: str) -> None:
    matches = [
        (number, row)
        for number, row in _rows("Alarm_Register.csv")
        if row["Trigger Tag"] == "ALM_CYL402_FAIL"
    ]
    assert len(matches) == 1
    number, row = matches[0]
    assert number == 9, "the README cites Alarm_Register.csv row 9"
    assert row["Alarm ID"] == "ALM-401-07"
    assert 'Name="ALM_CYL402_FAIL"' in l5x_text


def test_stop_gate_alarm_is_never_written(l5x_text: str) -> None:
    assert not any("ALM_CYL402_FAIL" in text for text in _rung_texts(l5x_text))


def test_every_other_alarm_trigger_is_written(l5x_text: str) -> None:
    """The control for issue 4: only one trigger tag in the register is dead."""
    texts = _rung_texts(l5x_text)
    undriven = {
        row["Trigger Tag"]
        for _, row in _rows("Alarm_Register.csv")
        if not any(
            f"{mnemonic}({row['Trigger Tag']})" in text
            for text in texts
            for mnemonic in ("OTE", "OTL", "OTU")
        )
    }
    assert undriven == {"ALM_CYL402_FAIL"}


def test_the_input_that_would_drive_it_is_wired_and_used(l5x_text: str) -> None:
    """So the finding is one omission, not also an orphan input."""
    io_tags = {row["Device Tag"] for _, row in _rows("IO_List.csv")}
    assert "CYL402_OPEN_PROX" in io_tags
    assert any("XIC(CYL402_OPEN_PROX)" in text for text in _rung_texts(l5x_text))


def test_readme_cites_issue_four(readme: str) -> None:
    for citation in ("ALM_CYL402_FAIL", "ALM-401-07", "**row 9**", "CYL402_OPEN_PROX"):
        assert citation in readme


# ---------------------------------------------------------------------------
# Issue 5 - an HMI object bound to a tag that does not exist
# ---------------------------------------------------------------------------


def _bindings(hmi: str) -> list[str]:
    """Every ``{::[Line4]...}`` expression in the display export."""
    found: list[str] = []
    for chunk in hmi.split('Expression="{::[Line4]')[1:]:
        found.append(chunk.split("}")[0])
    return found


def test_exactly_one_hmi_binding_is_unresolvable(
    generator: types.ModuleType, hmi_text: str, l5x_text: str
) -> None:
    expressions = _bindings(hmi_text)
    assert len(expressions) == 29, "the README states 29 bound graphic elements"
    unresolved = {
        expression
        for expression in expressions
        if f'Name="{generator.base_tag(expression)}"' not in l5x_text
    }
    assert unresolved == {"Accum_Case_Count"}


def test_the_missing_hmi_tag_is_missing_everywhere() -> None:
    for name in ("IO_List.csv", "Alarm_Register.csv", "Approved_Templates.txt"):
        assert "Accum_Case_Count" not in (FIXTURES / name).read_text(encoding="utf-8")


def test_readme_cites_issue_five(readme: str) -> None:
    for citation in ("Accum_Case_Count", "num_CasesAccumulated", "Line4_Overview"):
        assert citation in readme


# ---------------------------------------------------------------------------
# Issue 6 - one sentence, two readings
# ---------------------------------------------------------------------------


def test_the_ambiguous_sentence_is_in_the_project_description(
    generator: types.ModuleType,
) -> None:
    from docx import Document

    document = Document(str(FIXTURES / "Project_Description.docx"))
    matches = [
        paragraph
        for paragraph in document.paragraphs
        if generator.AMBIGUOUS_SENTENCE in paragraph.text
    ]
    assert len(matches) == 1


def test_the_readme_writes_out_both_readings(generator: types.ModuleType, readme: str) -> None:
    # Quoted as a block quote, so the line breaks differ from the document's.
    for fragment in ("release the accumulated", "stop gate shall close again"):
        assert fragment in readme
    assert "Reading A" in readme
    assert "Reading B" in readme
    assert generator.AMBIGUOUS_SENTENCE.split(",")[0] in readme


def test_nothing_else_settles_the_ambiguity() -> None:
    """The functional description records it as open rather than resolving it."""
    from docx import Document

    document = Document(str(FIXTURES / "Functional_Description.docx"))
    text = "\n".join(paragraph.text for paragraph in document.paragraphs)
    assert "To be confirmed" in text
    assert "no logic has been written for it" in text


# ---------------------------------------------------------------------------
# Issue 7 - the false-positive control
# ---------------------------------------------------------------------------


def test_cyl401_io_is_complete(l5x_text: str) -> None:
    expected = {
        "CYL401_EXT_PROX": ("Local:1:I.Data.8", 11),
        "CYL401_RET_PROX": ("Local:1:I.Data.9", 12),
        "CYL401_SOL": ("Local:2:O.Data.3", 23),
    }
    rows = {row["Device Tag"]: (number, row) for number, row in _rows("IO_List.csv")}
    for tag, (address, line) in expected.items():
        number, row = rows[tag]
        assert (number, row["Address"]) == (line, address)
        assert row["Status"] == "Existing"
        # The same name, unchanged, in the logic: normalisation must join them.
        assert f'Name="{tag}"' in l5x_text


def test_cyl401_logic_is_complete(l5x_text: str) -> None:
    texts = _rung_texts(l5x_text)
    for instruction in (
        "OTE(CYL401_SOL)",
        "TON(CYL401_Extend_TMR,1500,0)",
        "OTL(ALM_CYL401_FAIL)",
        "OTU(ALM_CYL401_FAIL)",
        "XIO(ALM_CYL401_FAIL)",
    ):
        assert any(instruction in text for text in texts), f"missing {instruction}"

    reset = [text for text in texts if "OTU(ALM_CYL401_FAIL)" in text]
    assert len(reset) == 1
    for condition in ("XIC(RESET_PB)", "XIC(ManualMode)", "XIC(CYL401_RET_PROX)"):
        assert condition in reset[0], "the reset conditions must match the description"


def test_cyl401_timing_agrees_everywhere(
    matrix_records: dict[str, dict[str, Any]], l5x_text: str
) -> None:
    from docx import Document

    record = matrix_records["ILK-403"]
    assert record["_row"] == 4, "the README cites Interlock Matrix row 4"
    assert record["Scope"] == "Existing"
    assert "1.5 s" in str(record["Trip Condition"])
    assert "1500 ms" in str(record["Notes"])

    assert "TON(CYL401_Extend_TMR,1500,0)" in l5x_text

    alarms = {row["Trigger Tag"]: (number, row) for number, row in _rows("Alarm_Register.csv")}
    number, alarm = alarms["ALM_CYL401_FAIL"]
    assert number == 8, "the README cites Alarm_Register.csv row 8"
    assert "1.5 s" in alarm["Message"]
    assert alarm["Interlock ID"] == "ILK-403"

    for name in ("Project_Description.docx", "Functional_Description.docx"):
        text = "\n".join(paragraph.text for paragraph in Document(str(FIXTURES / name)).paragraphs)
        assert "1.5 second" in text, f"{name} does not state the 1.5 s confirm time"


def test_cyl401_is_on_the_hmi_and_in_the_alarm_register(hmi_text: str) -> None:
    bound = set(_bindings(hmi_text))
    assert {
        "CYL401_SOL",
        "CYL401_EXT_PROX",
        "CYL401_RET_PROX",
        "ALM_CYL401_FAIL",
    } <= bound
    assert 'Display Name="Line4_Diverter"' in hmi_text


def test_readme_marks_cyl401_as_producing_no_finding(readme: str) -> None:
    assert "false-positive control" in readme
    assert "no findings about CYL401" in readme
    assert "Any finding raised against CYL401 is a false positive." in readme


# ---------------------------------------------------------------------------
# What must not be reported
# ---------------------------------------------------------------------------


def test_new_scope_interlocks_are_marked_as_such(
    matrix_records: dict[str, dict[str, Any]],
) -> None:
    """Their absence from the base export is expected, not a defect."""
    for interlock in ("ILK-405", "ILK-406"):
        assert matrix_records[interlock]["Scope"] == "New (this upgrade)"
    assert {
        identifier
        for identifier, record in matrix_records.items()
        if record["Scope"] == "New (this upgrade)"
    } == {"ILK-405", "ILK-406"}


def test_the_new_analog_point_is_marked_new() -> None:
    rows = {row["Device Tag"]: (number, row) for number, row in _rows("IO_List.csv")}
    number, row = rows["AIR_PRESS_PSI"]
    assert number == 25
    assert row["Status"] == "New (this upgrade)"
    assert {tag for tag, (_, item) in rows.items() if item["Status"] != "Existing"} == {
        "AIR_PRESS_PSI"
    }


def test_every_existing_interlock_except_ilk404_is_in_the_logic(
    matrix_records: dict[str, dict[str, Any]], l5x_text: str
) -> None:
    implemented = {
        identifier
        for identifier in matrix_records
        if matrix_records[identifier]["Scope"] == "Existing" and identifier in l5x_text
    }
    existing = {
        identifier for identifier, record in matrix_records.items() if record["Scope"] == "Existing"
    }
    assert existing - implemented == {"ILK-404"}


def test_readme_lists_the_false_positives(readme: str) -> None:
    section = readme.split("What a correct review must")[1]
    for expectation in ("ILK-405", "ILK-406", "AIR_PRESS_PSI", "AutoMode", "ILK-401", "ILK-407"):
        assert expectation in section


# ---------------------------------------------------------------------------
# Parser cleanliness
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("name", "kind", "role", "anchors", "warnings"), PARSE_PLAN)
def test_every_artifact_parses_as_the_readme_states(
    name: str,
    kind: FileKind,
    role: DocumentRole,
    anchors: int,
    warnings: int,
    settings: Settings,
) -> None:
    result = _parse(name, kind, role, settings)
    assert len(result.anchors) == anchors, f"{name}: anchor count moved"
    assert len(result.warnings) == warnings, [warning.as_dict() for warning in result.warnings]
    assert not any(warning.severity == "error" for warning in result.warnings)


def test_the_l5x_resolves_every_operand_it_uses(settings: Settings) -> None:
    """Issues 4 and 5 must only be findable by comparing files."""
    result = _parse("Conveyor_Line4_Base.L5X", FileKind.L5X, DocumentRole.CONTROL_LOGIC, settings)
    assert result.summary["unresolved_reference_count"] == 0
    assert result.summary["unparsed_rung_count"] == 0
    assert result.summary["rung_count"] == 24
    assert result.summary["tag_count"] == 38
    assert result.summary["processor_type"] == "5069-L330ER"
    # ALM_CYL402_FAIL is declared but never written, so it is a tag and not an
    # alarm reference. That difference is the whole of issue 4.
    written = {reference["tag"] for reference in result.content["alarm_references"]}
    assert "ALM_CYL402_FAIL" not in written
    assert "ALM_CYL401_FAIL" in written
    assert normalise_tag_name("ALM_CYL402_FAIL") in result.content["tag_index"]


def test_the_interlock_matrix_reads_as_declared_tables(settings: Settings) -> None:
    result = _parse(
        "Interlock_Matrix.xlsx", FileKind.XLSX, DocumentRole.INTERLOCK_SCHEDULE, settings
    )
    tables = result.content["candidate_tables"]
    assert [table["table_name"] for table in tables] == ["InterlockMatrix", "CauseAndEffect"]
    assert {table["detection"] for table in tables} == {"named_table"}
    assert all(len(table["records"]) == 7 for table in tables)
    assert result.content["workbook"]["hidden_sheet_names"] == []


def test_the_csv_artifacts_are_not_ragged(settings: Settings) -> None:
    for name, role, rows in (
        ("IO_List.csv", DocumentRole.IO_LIST, 26),
        ("Alarm_Register.csv", DocumentRole.ALARM_LIST, 7),
    ):
        result = _parse(name, FileKind.CSV, role, settings)
        assert result.summary["ragged_row_count"] == 0, name
        assert result.summary["row_count"] == rows, name
        assert result.summary["delimiter"] == ",", name


def test_the_template_list_headings_are_marked_up_not_inferred(settings: Settings) -> None:
    """An inferred heading would group lines the author never grouped."""
    result = _parse("Approved_Templates.txt", FileKind.TEXT, DocumentRole.OTHER, settings)
    sections = result.content["sections"]
    assert len(sections) == 4
    assert {section["title_source"] for section in sections} == {"underlined"}
