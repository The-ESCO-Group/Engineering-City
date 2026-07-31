"""The L5X parser, asserted against the designed ground truth of the fixture.

The values here are not arbitrary. ``fixtures/README.md`` plants a specific set
of discrepancies in ``Conveyor_Line3.L5X`` — a contradictory air-pressure
threshold, an abbreviated guard-door tag, an interlock that is documented but
never implemented, a duplicated rung, a missing permissive, an undeclared alarm
tag — and the whole product depends on the parser surfacing them as *structure*
without deciding what they mean. So the tests assert the exact operands, the
exact rung text and the exact absences, and they assert that what the parser
could not read is reported rather than dropped.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import pytest

from app.core.config import Settings
from app.core.enums import AnchorType, DocumentRole, EvidenceKind, FileKind
from app.core.errors import ParserError
from app.parsers.base import ParseContext, ParseResult, WarningCode, normalise_tag_name
from app.parsers.l5x_parser import L5XParser

FIXTURE_DIR = Path(__file__).resolve().parents[3] / "fixtures" / "demo_project"
DEMO_L5X = FIXTURE_DIR / "Conveyor_Line3.L5X"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _context(path: Path, settings: Settings, *, archive_member_path: str = "") -> ParseContext:
    return ParseContext(
        file_id=uuid.UUID(int=1),
        project_id=uuid.UUID(int=2),
        path=path,
        filename=path.name,
        file_kind=FileKind.L5X,
        document_role=DocumentRole.CONTROL_LOGIC,
        settings=settings,
        archive_member_path=archive_member_path,
    )


def _parse(path: Path, settings: Settings, *, archive_member_path: str = "") -> ParseResult:
    return L5XParser().parse(_context(path, settings, archive_member_path=archive_member_path))


def _l5x(body: str, *, controller: str = "Test_Controller") -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<RSLogix5000Content SchemaRevision="1.0" SoftwareRevision="32.00"'
        ' ExportDate="Mon Jan 06 09:14:22 2025">'
        f'<Controller Name="{controller}" ProcessorType="1756-L83E"'
        ' MajorRev="32" MinorRev="11">'
        f"{body}"
        "</Controller></RSLogix5000Content>"
    )


def _rung(number: int, text: str, comment: str = "") -> str:
    comment_xml = f"<Comment><![CDATA[{comment}]]></Comment>" if comment else ""
    return f'<Rung Number="{number}" Type="N">{comment_xml}<Text><![CDATA[{text}]]></Text></Rung>'


def _program(rungs: str, *, tags: str = "", routine: str = "R1") -> str:
    return (
        '<Programs><Program Name="P1" MainRoutineName="R1" Disabled="false">'
        f"<Tags>{tags}</Tags>"
        f'<Routines><Routine Name="{routine}" Type="RLL"><RLLContent>{rungs}'
        "</RLLContent></Routine></Routines></Program></Programs>"
    )


def _write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def _rung_of(result: ParseResult, routine: str, number: int) -> dict[str, Any]:
    matches = [
        rung
        for rung in result.content["rungs"]
        if rung["routine"] == routine and rung["number"] == number
    ]
    assert len(matches) == 1, f"expected exactly one {routine} rung {number}"
    return matches[0]


def _tag(result: ParseResult, name: str) -> dict[str, Any]:
    matches = [tag for tag in result.content["tags"] if tag["name"] == name]
    assert len(matches) == 1, f"expected exactly one tag named {name}"
    return matches[0]


def _codes(result: ParseResult) -> list[str]:
    return [warning.code for warning in result.warnings]


def _anchor_rows(result: ParseResult) -> list[tuple[Any, ...]]:
    return [
        (
            anchor.key,
            anchor.anchor_type,
            anchor.display_location,
            anchor.structured_location,
            anchor.excerpt,
            anchor.evidence_kind,
            anchor.source_label,
            anchor.expectation,
            anchor.content_hash,
        )
        for anchor in result.anchors
    ]


@pytest.fixture
def demo(settings: Settings) -> ParseResult:
    return _parse(DEMO_L5X, settings)


# ---------------------------------------------------------------------------
# Declared structure
# ---------------------------------------------------------------------------


def test_parser_claims_the_l5x_file_kind_and_labels_its_artifact(demo: ParseResult) -> None:
    parser = L5XParser()

    assert parser.name == "l5x"
    assert parser.handles == (FileKind.L5X,)
    assert demo.artifact_kind == FileKind.L5X.value
    assert demo.parser_name == "l5x"
    assert demo.parser_version == parser.version


def test_controller_header_is_read_from_the_export(demo: ParseResult) -> None:
    controller = demo.content["controller"]

    assert controller["name"] == "Conveyor_Line3"
    assert controller["processor_type"] == "1756-L83E"
    assert controller["major_rev"] == 32
    assert controller["minor_rev"] == 11
    assert controller["software_revision"] == "32.00"
    assert controller["export_date"] == "Mon Jan 06 09:14:22 2025"
    assert controller["schema_revision"] == "1.0"
    assert "SYNTHETIC FIXTURE" in controller["description"]


def test_summary_counts_the_shape_of_the_export(demo: ParseResult) -> None:
    """The summary is shown without loading content, so its counts must be right."""
    assert demo.summary["controller_name"] == "Conveyor_Line3"
    assert demo.summary["processor_type"] == "1756-L83E"
    assert demo.summary["datatype_count"] == 1
    assert demo.summary["addon_instruction_count"] == 1
    assert demo.summary["task_count"] == 1
    assert demo.summary["program_count"] == 1
    assert demo.summary["program_names"] == ["MainProgram"]
    assert demo.summary["routine_count"] == 5
    assert demo.summary["rung_count"] == 18
    assert demo.summary["instruction_count"] == 45
    assert demo.summary["tag_count"] == 19
    assert demo.summary["controller_tag_count"] == 13
    assert demo.summary["program_tag_count"] == 6
    assert demo.summary["indexed_tag_count"] == 20
    assert demo.summary["alarm_reference_count"] == 3
    assert demo.summary["unresolved_reference_count"] == 1
    assert demo.summary["unparsed_rung_count"] == 0
    assert demo.summary["mnemonics"] == [
        "EQU",
        "GRT",
        "JSR",
        "MotorStarter",
        "OTE",
        "OTL",
        "OTU",
        "XIC",
        "XIO",
    ]


def test_every_module_element_is_recorded_including_the_controllers_own(demo: ParseResult) -> None:
    """The export declares four <Module> elements: three I/O cards plus ``Local``.

    ``Local`` is the controller's own module, not a fourth card. The parser
    records what the file declares and leaves that distinction to a reader.
    """
    modules = demo.content["modules"]

    assert [module["name"] for module in modules] == ["Local", "DI_01", "DO_01", "AI_01"]
    assert [module["catalog_number"] for module in modules] == [
        "1756-L83E",
        "1756-IB16",
        "1756-OB16E",
        "1756-IF8",
    ]
    assert modules[1]["parent_module"] == "Local"
    assert modules[1]["inhibited"] is False
    assert modules[1]["major_fault"] is False
    assert modules[0]["major_fault"] is True
    assert "Digital input card" in modules[1]["description"]
    assert demo.summary["module_count"] == 4


def test_the_single_user_datatype_and_its_four_members_are_recorded(demo: ParseResult) -> None:
    datatypes = demo.content["datatypes"]

    assert len(datatypes) == 1
    assert datatypes[0]["name"] == "ConveyorStatus"
    assert datatypes[0]["family"] == "NoFamily"
    assert datatypes[0]["class"] == "User"

    members = datatypes[0]["members"]
    assert [member["name"] for member in members] == [
        "Running",
        "Faulted",
        "Interlocked",
        "SpeedFeedback",
    ]
    assert [member["data_type"] for member in members] == ["BOOL", "BOOL", "BOOL", "REAL"]
    assert members[3]["radix"] == "Float"
    assert members[0]["dimension"] == 0


def test_addon_instruction_definition_records_its_parameter_signature(demo: ParseResult) -> None:
    aois = demo.content["addon_instructions"]

    assert len(aois) == 1
    assert aois[0]["name"] == "MotorStarter"
    assert aois[0]["revision"] == "1.2"
    assert aois[0]["routine_names"] == ["Logic"]

    parameters = aois[0]["parameters"]
    assert [parameter["name"] for parameter in parameters] == [
        "EnableIn",
        "EnableOut",
        "StartCmd",
        "StopCmd",
        "Permissive",
        "RunOut",
    ]
    assert parameters[2] == {
        "name": "StartCmd",
        "data_type": "BOOL",
        "usage": "Input",
        "required": True,
        "visible": True,
    }
    assert parameters[0]["required"] is False


def test_addon_instruction_logic_is_declared_unsupported_rather_than_omitted(
    demo: ParseResult,
) -> None:
    """Not tokenising the AOI body is a boundary, so it has to be visible as one."""
    items = [item for item in demo.unsupported if item.kind == "addon_instruction_logic"]

    assert len(items) == 1
    assert items[0].location == "Add-On Instruction MotorStarter / Logic"
    assert "not tokenised" in items[0].detail


def test_tasks_record_their_scheduled_programs(demo: ParseResult) -> None:
    assert demo.content["tasks"] == [
        {
            "name": "MainTask",
            "type": "CONTINUOUS",
            "priority": 10,
            "watchdog": 500,
            "scheduled_programs": ["MainProgram"],
        }
    ]


# ---------------------------------------------------------------------------
# Tags
# ---------------------------------------------------------------------------


def test_tag_scope_distinguishes_controller_tags_from_program_tags(demo: ParseResult) -> None:
    assert _tag(demo, "ESTOP_OK")["scope"] == "controller"
    assert _tag(demo, "ESTOP_OK")["program"] == ""
    assert _tag(demo, "LINE_PERMISSIVE")["scope"] == "controller"

    for name in ("ModeSelect", "AutoMode", "ManualMode", "CV101_Start_PB", "CV101_Starter"):
        tag = _tag(demo, name)
        assert tag["scope"] == "program"
        assert tag["program"] == "MainProgram"

    assert len(demo.content["tags"]) == 19


def test_tag_records_carry_type_radix_description_and_exported_value(demo: ParseResult) -> None:
    air = _tag(demo, "AIR_PRESS_PSI")
    assert air["data_type"] == "REAL"
    assert air["radix"] == "Float"
    assert air["tag_type"] == "Base"
    assert air["value"] == "82.5"
    assert "psi" in air["description"]

    assert _tag(demo, "CV101_Stop_PB")["value"] == "1"
    assert _tag(demo, "CV101_Status")["data_type"] == "ConveyorStatus"
    assert _tag(demo, "CV101_Status")["value"] is None

    assert air["alias_for"] == ""
    assert air["dimensions"] == ""
    assert air["constant"] is False
    assert air["external_access"] == "Read/Write"
    assert not any(item.kind == "tag_alias" for item in demo.unsupported)


def test_tag_index_is_keyed_by_the_normalised_name(demo: ParseResult) -> None:
    index = demo.content["tag_index"]

    assert index[normalise_tag_name("ESTOP_OK")] == {
        "name": "ESTOP_OK",
        "scope": "controller",
        "data_type": "BOOL",
        "declared": True,
    }
    assert index[normalise_tag_name("ModeSelect")]["scope"] == "program"
    assert all(entry["declared"] for entry in index.values() if entry["scope"] != "unknown")


# ---------------------------------------------------------------------------
# Rungs and instructions: the planted discrepancies
# ---------------------------------------------------------------------------


def test_flat_rung_list_covers_every_routine_in_document_order(demo: ParseResult) -> None:
    rungs = demo.content["rungs"]

    assert len(rungs) == 18
    assert [(rung["routine"], rung["number"]) for rung in rungs] == [
        ("MainRoutine", 0),
        ("MainRoutine", 1),
        ("MainRoutine", 2),
        ("MainRoutine", 3),
        ("Mode_Logic", 0),
        ("Mode_Logic", 1),
        ("Conveyor_Interlocks", 0),
        ("Conveyor_Interlocks", 1),
        ("Conveyor_Interlocks", 2),
        ("Conveyor_Interlocks", 3),
        ("Conveyor_Interlocks", 4),
        ("Conveyor_Control", 0),
        ("Conveyor_Control", 1),
        ("Conveyor_Control", 2),
        ("Conveyor_Control", 3),
        ("Alarm_Logic", 0),
        ("Alarm_Logic", 1),
        ("Alarm_Logic", 2),
    ]
    assert all(rung["program"] == "MainProgram" for rung in rungs)
    assert [routine["name"] for routine in demo.content["programs"][0]["routines"]] == [
        "MainRoutine",
        "Mode_Logic",
        "Conveyor_Interlocks",
        "Conveyor_Control",
        "Alarm_Logic",
    ]
    assert demo.content["programs"][0]["routines"][2]["rungs"] == [0, 1, 2, 3, 4]


def test_air_pressure_comparison_is_recorded_verbatim_and_not_evaluated(demo: ParseResult) -> None:
    """ILK-005: the logic compares against 40.0 while the documents say 60 psi.

    The parser's job is to make that comparable, not to call it wrong, so the
    threshold is stored as the operand string it is written as.
    """
    rung = _rung_of(demo, "Conveyor_Interlocks", 0)
    comparisons = [
        instruction for instruction in rung["instructions"] if instruction["mnemonic"] == "GRT"
    ]

    assert len(comparisons) == 1
    assert comparisons[0]["operands"] == ["AIR_PRESS_PSI", "40.0"]
    assert comparisons[0]["text"] == "GRT(AIR_PRESS_PSI,40.0)"
    assert comparisons[0]["index"] == 4
    assert "40.0" in rung["text"]
    assert "60" not in rung["text"]


def test_interlock_rung_uses_the_abbreviated_guard_door_tag(demo: ParseResult) -> None:
    """ILK-004: the logic says GRD_DR_CLSD where the documents say GUARD_DOOR_CLOSED."""
    rung = _rung_of(demo, "Conveyor_Interlocks", 0)

    assert "GRD_DR_CLSD" in rung["referenced_tags"]
    assert "XIC(GRD_DR_CLSD)" in rung["text"]
    assert normalise_tag_name("GRD_DR_CLSD") != normalise_tag_name("GUARD_DOOR_CLOSED")
    assert normalise_tag_name("GUARD_DOOR_CLOSED") not in demo.content["tag_index"]
    assert _tag(demo, "GRD_DR_CLSD")["scope"] == "controller"


def test_first_interlock_rung_builds_the_permissive_from_five_conditions(
    demo: ParseResult,
) -> None:
    rung = _rung_of(demo, "Conveyor_Interlocks", 0)

    assert [
        (instruction["mnemonic"], instruction["operands"]) for instruction in rung["instructions"]
    ] == [
        ("XIC", ["ESTOP_OK"]),
        ("XIC", ["GRD_DR_CLSD"]),
        ("XIO", ["CV101_OL_TRIP"]),
        ("XIO", ["CV102_OL_TRIP"]),
        ("GRT", ["AIR_PRESS_PSI", "40.0"]),
        ("OTE", ["LINE_PERMISSIVE"]),
    ]
    assert rung["referenced_tags"] == [
        "ESTOP_OK",
        "GRD_DR_CLSD",
        "CV101_OL_TRIP",
        "CV102_OL_TRIP",
        "AIR_PRESS_PSI",
        "LINE_PERMISSIVE",
    ]


def test_every_interlock_rung_is_pinned_to_its_exact_instructions_and_operands(
    demo: ParseResult,
) -> None:
    """Rungs 1-4 carry planted ground truth, so each one is asserted whole.

    Without this, an instruction could be dropped anywhere in the routine and
    every count-based assertion would still hold.
    """
    interlocks = {
        number: [
            (instruction["mnemonic"], instruction["operands"])
            for instruction in _rung_of(demo, "Conveyor_Interlocks", number)["instructions"]
        ]
        for number in (1, 2, 3, 4)
    }

    assert interlocks[1] == [("XIC", ["CV101_OL_TRIP"]), ("OTL", ["CV101_Status.Interlocked"])]
    assert interlocks[2] == [("XIC", ["CV102_OL_TRIP"]), ("OTL", ["CV102_Status.Interlocked"])]
    assert interlocks[3] == [("XIC", ["CV103_VFD_FLT"]), ("OTL", ["CV103_Status.Interlocked"])]
    assert interlocks[4] == [
        ("XIC", ["ManualMode"]),
        ("XIO", ["CV101_OL_TRIP"]),
        ("XIO", ["CV102_OL_TRIP"]),
        ("OTU", ["CV101_Status.Interlocked"]),
    ]
    assert _rung_of(demo, "Conveyor_Interlocks", 4)["referenced_tags"] == [
        "ManualMode",
        "CV101_OL_TRIP",
        "CV102_OL_TRIP",
        "CV101_Status.Interlocked",
    ]


def test_the_undocumented_cv103_vfd_rung_is_asserted_through_its_logic(demo: ParseResult) -> None:
    """The ``undocumented_logic`` plant, pinned by its instructions not its comment."""
    rung = _rung_of(demo, "Conveyor_Interlocks", 3)

    assert rung["text"] == "XIC(CV103_VFD_FLT)OTL(CV103_Status.Interlocked);"
    assert rung["referenced_tags"] == ["CV103_VFD_FLT", "CV103_Status.Interlocked"]
    assert (
        demo.anchors_by_key["instruction:MainProgram/Conveyor_Interlocks/3/0"].excerpt
        == "XIC(CV103_VFD_FLT)"
    )


def test_the_hopper_high_alarm_rung_keeps_its_threshold_and_both_operands(
    demo: ParseResult,
) -> None:
    """ILK-007 has no schedule threshold; the logic's 90.0 is the only one there is."""
    rung = _rung_of(demo, "Alarm_Logic", 2)

    assert [
        (instruction["mnemonic"], instruction["operands"]) for instruction in rung["instructions"]
    ] == [("GRT", ["HOPPER_LEVEL_PCT", "90.0"]), ("OTE", ["ALM_HOPPER_HIGH"])]
    assert rung["referenced_tags"] == ["HOPPER_LEVEL_PCT", "ALM_HOPPER_HIGH"]
    assert rung["text"] == "GRT(HOPPER_LEVEL_PCT,90.0)OTE(ALM_HOPPER_HIGH);"

    assert [
        (instruction["mnemonic"], instruction["operands"])
        for instruction in _rung_of(demo, "Alarm_Logic", 0)["instructions"]
    ] == [("XIO", ["ESTOP_OK"]), ("OTE", ["ALM_ESTOP"])]
    assert [
        (instruction["mnemonic"], instruction["operands"])
        for instruction in _rung_of(demo, "Alarm_Logic", 1)["instructions"]
    ] == [("XIC", ["CV101_OL_TRIP"]), ("OTE", ["ALM_CV101_OL"])]


def test_the_scheduler_and_mode_routines_are_pinned_instruction_by_instruction(
    demo: ParseResult,
) -> None:
    assert [
        [
            (instruction["mnemonic"], instruction["operands"])
            for instruction in _rung_of(demo, "MainRoutine", number)["instructions"]
        ]
        for number in (0, 1, 2, 3)
    ] == [
        [("JSR", ["Mode_Logic", "0"])],
        [("JSR", ["Conveyor_Interlocks", "0"])],
        [("JSR", ["Conveyor_Control", "0"])],
        [("JSR", ["Alarm_Logic", "0"])],
    ]
    assert [
        [
            (instruction["mnemonic"], instruction["operands"])
            for instruction in _rung_of(demo, "Mode_Logic", number)["instructions"]
        ]
        for number in (0, 1)
    ] == [
        [("EQU", ["ModeSelect", "1"]), ("OTE", ["ManualMode"])],
        [("EQU", ["ModeSelect", "2"]), ("OTE", ["AutoMode"])],
    ]


def test_no_rung_anywhere_references_the_documented_cv102_jam_interlock(
    demo: ParseResult,
) -> None:
    """ILK-003 is in the schedule, the description and the I/O list but not in logic.

    The absence is the finding, so it is asserted directly rather than inferred.
    """
    rungs = demo.content["rungs"]

    assert not any("CV102_JAM" in rung["referenced_tags"] for rung in rungs)
    assert not any("CV102_JAM" in rung["text"] for rung in rungs)
    assert normalise_tag_name("CV102_JAM") not in demo.content["tag_index"]
    assert not any(
        reference["name"] == "CV102_JAM" for reference in demo.content["unresolved_references"]
    )


def test_commissioning_duplicate_rung_has_identical_text_to_the_rung_it_copies(
    demo: ParseResult,
) -> None:
    original = _rung_of(demo, "Conveyor_Control", 1)
    duplicate = _rung_of(demo, "Conveyor_Control", 3)

    assert duplicate["text"] == original["text"]
    assert duplicate["instructions"] == original["instructions"]
    assert duplicate["referenced_tags"] == original["referenced_tags"]
    assert duplicate["comment"] != original["comment"]
    assert "Duplicate of rung 1" in duplicate["comment"]


def test_cv103_run_rung_omits_the_permissive_that_cv101_and_cv102_use(
    demo: ParseResult,
) -> None:
    assert "LINE_PERMISSIVE" in _rung_of(demo, "Conveyor_Control", 0)["referenced_tags"]
    assert "LINE_PERMISSIVE" in _rung_of(demo, "Conveyor_Control", 1)["referenced_tags"]

    cv103 = _rung_of(demo, "Conveyor_Control", 2)
    assert "LINE_PERMISSIVE" not in cv103["referenced_tags"]
    assert "LINE_PERMISSIVE" not in cv103["text"]
    assert cv103["referenced_tags"] == [
        "AutoMode",
        "CV102_Status.Running",
        "CV103_Status.Interlocked",
        "CV103_Status.Running",
    ]


def test_addon_instruction_call_parses_as_a_five_operand_instruction(demo: ParseResult) -> None:
    rung = _rung_of(demo, "Conveyor_Control", 0)

    assert len(rung["instructions"]) == 1
    assert rung["instructions"][0]["mnemonic"] == "MotorStarter"
    assert rung["instructions"][0]["operands"] == [
        "CV101_Starter",
        "CV101_Start_PB",
        "CV101_Stop_PB",
        "LINE_PERMISSIVE",
        "CV101_Status.Running",
    ]
    assert rung["instructions"][0]["index"] == 0


def test_rung_comments_are_captured(demo: ParseResult) -> None:
    interlock = _rung_of(demo, "Conveyor_Interlocks", 0)
    assert "ILK-001" in interlock["comment"]
    assert "Fail-safe." in interlock["comment"]

    assert "no entry for this exists" in _rung_of(demo, "Conveyor_Interlocks", 3)["comment"].lower()
    assert "no LINE_PERMISSIVE contact" in _rung_of(demo, "Conveyor_Control", 2)["comment"]


def test_subroutine_calls_are_not_reported_as_tag_references(demo: ParseResult) -> None:
    """A JSR target is a routine the export declares, not an undeclared signal."""
    rung = _rung_of(demo, "MainRoutine", 1)

    assert rung["instructions"][0]["mnemonic"] == "JSR"
    assert rung["instructions"][0]["operands"] == ["Conveyor_Interlocks", "0"]
    assert rung["referenced_tags"] == []
    assert not any(
        reference["routine"] == "MainRoutine" for reference in demo.content["unresolved_references"]
    )


# ---------------------------------------------------------------------------
# Unresolved references and alarm observations
# ---------------------------------------------------------------------------


def test_undeclared_alarm_tag_is_reported_not_invented(demo: ParseResult) -> None:
    """ALM_HOPPER_HIGH is written by Alarm_Logic rung 2 and declared nowhere."""
    assert demo.content["unresolved_references"] == [
        {
            "name": "ALM_HOPPER_HIGH",
            "program": "MainProgram",
            "routine": "Alarm_Logic",
            "rung": 2,
            "kind": "tag",
        }
    ]
    assert demo.content["tag_index"][normalise_tag_name("ALM_HOPPER_HIGH")] == {
        "name": "ALM_HOPPER_HIGH",
        "scope": "unknown",
        "data_type": "",
        "declared": False,
    }

    warnings = [
        warning for warning in demo.warnings if warning.code == WarningCode.UNRESOLVED_REFERENCE
    ]
    assert len(warnings) == 1
    assert "ALM_HOPPER_HIGH" in warnings[0].message
    assert warnings[0].location == "MainProgram / Alarm_Logic / Rung 2"


def test_unresolved_reference_gets_a_missing_expected_anchor(demo: ParseResult) -> None:
    missing = [
        anchor for anchor in demo.anchors if anchor.evidence_kind is EvidenceKind.MISSING_EXPECTED
    ]

    assert len(missing) == 1
    anchor = missing[0]
    assert anchor.expectation == "declaration of tag ALM_HOPPER_HIGH"
    assert anchor.anchor_type is AnchorType.L5X_TAG
    assert anchor.display_location == (
        "MainProgram / Alarm_Logic / Rung 2 (no declaration of ALM_HOPPER_HIGH)"
    )
    assert anchor.structured_location == {
        "program": "MainProgram",
        "routine": "Alarm_Logic",
        "rung": 2,
        "tag": "ALM_HOPPER_HIGH",
    }
    assert "OTE(ALM_HOPPER_HIGH)" in anchor.excerpt


def test_alarm_named_outputs_are_recorded_as_observations_only(demo: ParseResult) -> None:
    """An ALM_ prefix is a naming hint, so the record says where, not what it means."""
    assert demo.content["alarm_references"] == [
        {
            "tag": "ALM_ESTOP",
            "rung": 0,
            "program": "MainProgram",
            "routine": "Alarm_Logic",
        },
        {
            "tag": "ALM_CV101_OL",
            "rung": 1,
            "program": "MainProgram",
            "routine": "Alarm_Logic",
        },
        {
            "tag": "ALM_HOPPER_HIGH",
            "rung": 2,
            "program": "MainProgram",
            "routine": "Alarm_Logic",
        },
    ]


def test_alarm_reference_rung_is_the_number_the_rung_is_addressed_by(
    tmp_path: Path, settings: Settings
) -> None:
    """``rung`` is the rung's address, which is not its ``Number`` attribute here.

    Both rungs are written ``Number="7"``, so the routine falls back to positional
    addressing and the addresses become 0 and 1. Nothing else pins that the alarm
    observation uses the same numbering as ``rungs[].number`` and the rung anchor
    keys, so a reader could not tell which of the two an ``alarm_references``
    entry meant.
    """
    body = _program(
        _rung(7, "XIC(A_Tag)OTE(ALM_ONE);") + _rung(7, "XIC(A_Tag)OTE(ALM_TWO);"),
        tags='<Tag Name="A_Tag" DataType="BOOL"/>',
    )
    path = _write(tmp_path, "alarm_positional.L5X", _l5x(body))

    result = _parse(path, settings)

    assert result.content["alarm_references"] == [
        {"tag": "ALM_ONE", "rung": 0, "program": "P1", "routine": "R1"},
        {"tag": "ALM_TWO", "rung": 1, "program": "P1", "routine": "R1"},
    ]
    assert [rung["number"] for rung in result.content["rungs"]] == [0, 1]
    assert [rung["anchor_key"] for rung in result.content["rungs"]] == [
        "rung:P1/R1/0",
        "rung:P1/R1/1",
    ]


def test_the_only_warning_on_the_demo_export_is_the_undeclared_tag(demo: ParseResult) -> None:
    """A correctly matched interlock must not generate parser noise either."""
    assert _codes(demo) == [WarningCode.UNRESOLVED_REFERENCE]


# ---------------------------------------------------------------------------
# Anchors
# ---------------------------------------------------------------------------


def test_anchor_keys_are_hierarchical_and_carry_the_matching_type(demo: ParseResult) -> None:
    by_key = demo.anchors_by_key

    assert len(by_key) == len(demo.anchors)
    expected = {
        "controller:Conveyor_Line3": AnchorType.L5X_CONTROLLER,
        "module:DI_01": AnchorType.L5X_MODULE,
        "aoi:MotorStarter": AnchorType.L5X_ADDON_INSTRUCTION,
        "tag:controller/AIR_PRESS_PSI": AnchorType.L5X_TAG,
        "tag:program/ModeSelect": AnchorType.L5X_TAG,
        "program:MainProgram": AnchorType.L5X_PROGRAM,
        "routine:MainProgram/Conveyor_Interlocks": AnchorType.L5X_ROUTINE,
        "rung:MainProgram/Conveyor_Interlocks/0": AnchorType.L5X_RUNG,
        "instruction:MainProgram/Conveyor_Interlocks/0/4": AnchorType.L5X_INSTRUCTION,
    }
    for key, anchor_type in expected.items():
        assert by_key[key].anchor_type is anchor_type


def test_every_anchor_is_labelled_with_its_source_and_a_followable_location(
    demo: ParseResult,
) -> None:
    assert demo.anchors
    for anchor in demo.anchors:
        assert anchor.source_label == "Conveyor_Line3.L5X"
        assert anchor.display_location.strip()
        assert anchor.structured_location

    rung = demo.anchors_by_key["rung:MainProgram/Conveyor_Interlocks/0"]
    assert rung.display_location == "MainProgram / Conveyor_Interlocks / Rung 0"
    assert rung.structured_location == {
        "program": "MainProgram",
        "routine": "Conveyor_Interlocks",
        "rung": 0,
    }


def test_rung_anchor_excerpt_shows_the_comment_and_the_logic(demo: ParseResult) -> None:
    """The excerpt is what a human reads to check a finding, so it needs both."""
    excerpt = demo.anchors_by_key["rung:MainProgram/Conveyor_Interlocks/0"].excerpt

    assert "ILK-001" in excerpt
    assert "GRT(AIR_PRESS_PSI,40.0)" in excerpt
    assert excerpt.startswith("Comment:")

    instruction = demo.anchors_by_key["instruction:MainProgram/Conveyor_Interlocks/0/4"]
    assert instruction.excerpt == "GRT(AIR_PRESS_PSI,40.0)"


def test_anchor_count_matches_the_elements_worth_citing(demo: ParseResult) -> None:
    counts: dict[AnchorType, int] = {}
    for anchor in demo.anchors:
        counts[anchor.anchor_type] = counts.get(anchor.anchor_type, 0) + 1

    assert counts[AnchorType.L5X_CONTROLLER] == 1
    assert counts[AnchorType.L5X_MODULE] == 4
    assert counts[AnchorType.L5X_ADDON_INSTRUCTION] == 1
    assert counts[AnchorType.L5X_PROGRAM] == 1
    assert counts[AnchorType.L5X_ROUTINE] == 5
    assert counts[AnchorType.L5X_RUNG] == 18
    # The literal, not summary['instruction_count'] — comparing the parser's
    # anchors to the parser's own count would hold however many it lost.
    assert counts[AnchorType.L5X_INSTRUCTION] == 45
    assert counts[AnchorType.L5X_TAG] == 19 + 1
    assert sum(counts.values()) == 95


def test_archive_provenance_reaches_every_anchor(settings: Settings) -> None:
    result = _parse(DEMO_L5X, settings, archive_member_path="logic/Conveyor_Line3.L5X")

    assert result.anchors
    for anchor in result.anchors:
        assert anchor.source_label == "Conveyor_Line3.L5X (logic/Conveyor_Line3.L5X)"


def test_a_long_filename_and_member_path_still_produce_a_bounded_label(
    tmp_path: Path, settings: Settings
) -> None:
    """Anchors are stored in String(255)/String(512)/String(400) columns.

    SQLite stores the overflow silently, so an unbounded label works in the demo
    and raises on PostgreSQL. The bound belongs to ``AnchorDraft``; this asserts
    an L5X anchor gets it.
    """
    filename = "Conveyor_" + "L" * 100 + ".L5X"
    member = "package/" + "nested/" * 60 + filename
    path = _write(tmp_path, filename, _l5x('<Modules><Module Name="Local"/></Modules>'))

    result = _parse(path, settings, archive_member_path=member)

    assert result.anchors
    assert len(f"{filename} ({member})") > 255
    for anchor in result.anchors:
        assert len(anchor.source_label) <= 255
        assert len(anchor.display_location) <= 512
        assert len(anchor.key) <= 400
    assert result.anchors[0].source_label.startswith(f"Conveyor_{'L' * 20}")


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_reparsing_the_same_bytes_produces_identical_output(settings: Settings) -> None:
    first = _parse(DEMO_L5X, settings)
    second = _parse(DEMO_L5X, settings)

    assert first.content == second.content
    assert json.dumps(first.content) == json.dumps(second.content)
    assert first.summary == second.summary
    assert json.dumps(first.summary) == json.dumps(second.summary)
    assert _anchor_rows(first) == _anchor_rows(second)
    assert [anchor.key for anchor in first.anchors] == [anchor.key for anchor in second.anchors]
    assert [warning.as_dict() for warning in first.warnings] == [
        warning.as_dict() for warning in second.warnings
    ]
    assert [item.as_dict() for item in first.unsupported] == [
        item.as_dict() for item in second.unsupported
    ]


def test_output_carries_no_filesystem_paths(demo: ParseResult) -> None:
    """An anchor has to be portable: it names a location in a file, not on a disk."""
    serialised = json.dumps(
        {
            "content": demo.content,
            "summary": demo.summary,
            "anchors": [
                [anchor.key, anchor.display_location, anchor.excerpt, anchor.source_label]
                for anchor in demo.anchors
            ],
        }
    )

    assert str(DEMO_L5X) not in serialised
    assert str(DEMO_L5X.resolve()) not in serialised
    assert str(DEMO_L5X.parent) not in serialised
    assert str(FIXTURE_DIR.parent) not in serialised


# ---------------------------------------------------------------------------
# Degenerate and hostile input
# ---------------------------------------------------------------------------


def test_an_empty_file_fails_with_a_parser_error(tmp_path: Path, settings: Settings) -> None:
    path = tmp_path / "empty.L5X"
    path.write_bytes(b"")

    with pytest.raises(ParserError) as excinfo:
        _parse(path, settings)

    assert excinfo.value.message


def test_malformed_xml_fails_with_a_parser_error(tmp_path: Path, settings: Settings) -> None:
    path = _write(tmp_path, "truncated.L5X", '<RSLogix5000Content><Controller Name="X">')

    with pytest.raises(ParserError) as excinfo:
        _parse(path, settings)

    assert "Malformed XML" in excinfo.value.message


def test_xml_without_a_controller_element_fails_with_a_parser_error(
    tmp_path: Path, settings: Settings
) -> None:
    """A file that is not an L5X must say so, not parse to an empty review."""
    path = _write(
        tmp_path,
        "not_an_export.L5X",
        '<?xml version="1.0"?><RSLogix5000Content SchemaRevision="1.0"><Other/>'
        "</RSLogix5000Content>",
    )

    with pytest.raises(ParserError) as excinfo:
        _parse(path, settings)

    assert "<Controller>" in excinfo.value.message
    assert excinfo.value.details["root_element"] == "RSLogix5000Content"


def test_an_unexpected_root_element_warns_and_still_reads_the_controller(
    tmp_path: Path, settings: Settings
) -> None:
    path = _write(
        tmp_path,
        "odd_root.L5X",
        '<?xml version="1.0"?><Wrapper><Controller Name="Odd_Root"/></Wrapper>',
    )

    result = _parse(path, settings)

    assert result.content["controller"]["name"] == "Odd_Root"
    assert WarningCode.STRUCTURE_UNRECOGNISED in _codes(result)


def test_a_controller_with_no_content_warns_instead_of_reporting_nothing(
    tmp_path: Path, settings: Settings
) -> None:
    path = _write(tmp_path, "empty_controller.L5X", _l5x("", controller="Empty_Controller"))

    result = _parse(path, settings)

    assert WarningCode.EMPTY_DOCUMENT in _codes(result)
    assert result.content["controller"]["name"] == "Empty_Controller"
    assert result.content["rungs"] == []
    assert result.summary["rung_count"] == 0
    assert result.anchors_by_key["controller:Empty_Controller"]


def test_untokenisable_rung_text_warns_and_keeps_the_raw_text(
    tmp_path: Path, settings: Settings
) -> None:
    """Unreadable logic must remain readable to a human, and must be flagged."""
    body = _program(
        _rung(0, "%%!!(&&") + _rung(1, "XIC(A_Tag") + _rung(2, "XIC(A_Tag)OTE(B_Tag);"),
        tags='<Tag Name="A_Tag" DataType="BOOL"/><Tag Name="B_Tag" DataType="BOOL"/>',
    )
    path = _write(tmp_path, "unparsed.L5X", _l5x(body))

    result = _parse(path, settings)

    unparsed = [
        warning for warning in result.warnings if warning.code == WarningCode.UNPARSED_INSTRUCTION
    ]
    assert [warning.location for warning in unparsed] == ["P1 / R1 / Rung 0", "P1 / R1 / Rung 1"]
    assert "%%!!(&&" in unparsed[0].message
    assert result.summary["unparsed_rung_count"] == 2

    assert _rung_of(result, "R1", 0)["text"] == "%%!!(&&"
    assert _rung_of(result, "R1", 0)["instructions"] == []
    assert _rung_of(result, "R1", 1)["text"] == "XIC(A_Tag"
    assert _rung_of(result, "R1", 2)["referenced_tags"] == ["A_Tag", "B_Tag"]


def test_branch_delimiters_are_flattened_into_the_instruction_sequence(
    tmp_path: Path, settings: Settings
) -> None:
    """Branch structure is flattened; the brackets survive in the rung text."""
    body = _program(
        _rung(0, "XIC(Stop_PB)[XIC(Start_PB),XIC(Run_Out)]OTE(Run_Out);"),
        tags=(
            '<Tag Name="Stop_PB" DataType="BOOL"/><Tag Name="Start_PB" DataType="BOOL"/>'
            '<Tag Name="Run_Out" DataType="BOOL"/>'
        ),
    )
    path = _write(tmp_path, "branch.L5X", _l5x(body))

    result = _parse(path, settings)
    rung = _rung_of(result, "R1", 0)

    assert [
        (instruction["mnemonic"], instruction["operands"]) for instruction in rung["instructions"]
    ] == [
        ("XIC", ["Stop_PB"]),
        ("XIC", ["Start_PB"]),
        ("XIC", ["Run_Out"]),
        ("OTE", ["Run_Out"]),
    ]
    assert "[XIC(Start_PB),XIC(Run_Out)]" in rung["text"]
    assert rung["referenced_tags"] == ["Stop_PB", "Start_PB", "Run_Out"]
    assert result.content["unresolved_references"] == []
    assert WarningCode.UNPARSED_INSTRUCTION not in _codes(result)


def test_literal_operands_are_not_mistaken_for_tag_references(
    tmp_path: Path, settings: Settings
) -> None:
    body = _program(
        _rung(0, "MOV(16#FF,Dest)GRT(Dest,40.0)LIM(-1.5e3,Dest,2#0101)OTE(Out);"),
        tags='<Tag Name="Dest" DataType="DINT"/><Tag Name="Out" DataType="BOOL"/>',
    )
    path = _write(tmp_path, "literals.L5X", _l5x(body))

    result = _parse(path, settings)

    assert _rung_of(result, "R1", 0)["referenced_tags"] == ["Dest", "Out"]
    assert result.content["unresolved_references"] == []


def test_module_scoped_operands_resolve_against_declared_modules(
    tmp_path: Path, settings: Settings
) -> None:
    """An alias onto an undeclared module is an unresolved reference, not a tag."""
    body = (
        '<Modules><Module Name="Local" CatalogNumber="1756-L83E"/></Modules>'
        '<Tags><Tag Name="Out" DataType="BOOL"/></Tags>'
        + _program(_rung(0, "XIC(Local:1:I.Data[0])XIC(Rack2:3:I.Data[0])OTE(Out);"))
    )
    path = _write(tmp_path, "module_io.L5X", _l5x(body))

    result = _parse(path, settings)

    assert result.content["unresolved_references"] == [
        {"name": "Rack2", "program": "P1", "routine": "R1", "rung": 0, "kind": "module_io"}
    ]
    assert _rung_of(result, "R1", 0)["referenced_tags"] == [
        "Local:1:I.Data[0]",
        "Rack2:3:I.Data[0]",
        "Out",
    ]
    missing = [
        anchor for anchor in result.anchors if anchor.evidence_kind is EvidenceKind.MISSING_EXPECTED
    ]
    assert [anchor.expectation for anchor in missing] == ["declaration of module Rack2"]


def test_a_non_ladder_routine_is_reported_as_unsupported_content(
    tmp_path: Path, settings: Settings
) -> None:
    body = (
        '<Programs><Program Name="P1" MainRoutineName="R1"><Routines>'
        '<Routine Name="R1" Type="ST"><STContent><Line Number="0">'
        "<![CDATA[Out := In1 AND In2;]]></Line></STContent></Routine>"
        "</Routines></Program></Programs>"
    )
    path = _write(tmp_path, "structured_text.L5X", _l5x(body))

    result = _parse(path, settings)

    items = [item for item in result.unsupported if item.kind == "routine_body"]
    assert len(items) == 1
    assert items[0].location == "P1 / R1"
    assert "<STContent>" in items[0].detail
    assert "Type 'ST'" in items[0].detail
    assert result.content["programs"][0]["routines"][0]["rungs"] == []
    assert result.content["rungs"] == []


def test_a_routine_declaring_ladder_with_no_body_is_a_warning_not_a_capability_boundary(
    tmp_path: Path, settings: Settings
) -> None:
    """A missing body is an oddity in the export, not a limit of this parser.

    Reporting it as ``routine_body`` said "routine type 'RLL' is not ladder
    logic" about a routine the export declares as ladder — a message that states
    the opposite of the file. What is true is only that no body element is there.
    """
    body = (
        '<Programs><Program Name="P1" MainRoutineName="R1"><Routines>'
        '<Routine Name="R1" Type="RLL"><Description><![CDATA[Empty.]]></Description>'
        "</Routine></Routines></Program></Programs>"
    )
    path = _write(tmp_path, "bodyless_ladder.L5X", _l5x(body))

    result = _parse(path, settings)

    assert [item for item in result.unsupported if item.kind == "routine_body"] == []
    structural = [warning for warning in result.warnings if warning.location == "P1 / R1"]
    assert len(structural) == 1
    assert structural[0].code == WarningCode.STRUCTURE_UNRECOGNISED
    assert "declares Type 'RLL'" in structural[0].message
    assert "no body element" in structural[0].message
    assert "not ladder logic" not in structural[0].message
    assert result.content["programs"][0]["routines"][0]["rungs"] == []
    assert result.content["rungs"] == []


def test_a_routine_whose_declared_type_disagrees_with_its_body_reports_both(
    tmp_path: Path, settings: Settings
) -> None:
    """The record states the declared Type and the body seen, and concludes neither."""
    body = (
        '<Programs><Program Name="P1" MainRoutineName="R1"><Routines>'
        '<Routine Name="R1" Type="RLL"><STContent><Line Number="0">'
        "<![CDATA[Out := In1 AND In2;]]></Line></STContent></Routine>"
        "</Routines></Program></Programs>"
    )
    path = _write(tmp_path, "ladder_type_st_body.L5X", _l5x(body))

    result = _parse(path, settings)

    items = [item for item in result.unsupported if item.kind == "routine_body"]
    assert len(items) == 1
    assert items[0].location == "P1 / R1"
    assert "Type 'RLL'" in items[0].detail
    assert "<STContent>" in items[0].detail
    assert "is not ladder logic" not in items[0].detail
    assert result.content["rungs"] == []


def test_repeated_rung_numbers_fall_back_to_positional_addressing(
    tmp_path: Path, settings: Settings
) -> None:
    """Two rungs sharing a number would collapse two citations into one."""
    body = _program(
        _rung(0, "XIC(A_Tag)OTE(B_Tag);") + _rung(0, "XIO(A_Tag)OTE(B_Tag);"),
        tags='<Tag Name="A_Tag" DataType="BOOL"/><Tag Name="B_Tag" DataType="BOOL"/>',
    )
    path = _write(tmp_path, "repeated.L5X", _l5x(body))

    result = _parse(path, settings)

    assert WarningCode.STRUCTURE_UNRECOGNISED in _codes(result)
    assert [rung["number"] for rung in result.content["rungs"]] == [0, 1]
    keys = [anchor.key for anchor in result.anchors if anchor.anchor_type is AnchorType.L5X_RUNG]
    assert keys == ["rung:P1/R1/0", "rung:P1/R1/1"]
    assert len(set(keys)) == len(keys)


def test_same_named_program_tags_in_two_programs_stay_separately_citable(
    tmp_path: Path, settings: Settings
) -> None:
    """The plain ``tag:program/<name>`` key cannot address both, so one is qualified."""
    program = (
        '<Program Name="{name}" MainRoutineName="R1">'
        '<Tags><Tag Name="Shared" DataType="BOOL"/></Tags>'
        '<Routines><Routine Name="R1" Type="RLL"><RLLContent>'
        '<Rung Number="0" Type="N"><Text><![CDATA[OTE(Shared);]]></Text></Rung>'
        "</RLLContent></Routine></Routines></Program>"
    )
    body = f"<Programs>{program.format(name='P1')}{program.format(name='P2')}</Programs>"
    path = _write(tmp_path, "two_programs.L5X", _l5x(body))

    result = _parse(path, settings)
    keys = [anchor.key for anchor in result.anchors if anchor.anchor_type is AnchorType.L5X_TAG]

    assert keys == ["tag:program/Shared", "tag:program/P2/Shared"]
    assert len({anchor.key for anchor in result.anchors}) == len(result.anchors)
    assert WarningCode.STRUCTURE_UNRECOGNISED in _codes(result)
    assert result.content["unresolved_references"] == []


def test_a_rung_with_no_logic_text_is_reported(tmp_path: Path, settings: Settings) -> None:
    body = _program('<Rung Number="0" Type="N"><Comment><![CDATA[Placeholder.]]></Comment></Rung>')
    path = _write(tmp_path, "textless.L5X", _l5x(body))

    result = _parse(path, settings)

    assert WarningCode.STRUCTURE_UNRECOGNISED in _codes(result)
    assert _rung_of(result, "R1", 0)["comment"] == "Placeholder."
    assert _rung_of(result, "R1", 0)["text"] == ""


def test_unread_controller_sections_are_recorded_as_a_boundary(
    tmp_path: Path, settings: Settings
) -> None:
    """A section the parser never opened must not read as a section that was empty."""
    body = (
        '<Trends><Trend Name="T1"/></Trends><QuickWatchLists/><Trends><Trend Name="T2"/></Trends>'
    )
    path = _write(tmp_path, "extra_sections.L5X", _l5x(body))

    result = _parse(path, settings)

    sections = [item for item in result.unsupported if item.kind == "controller_section"]
    assert [item.location for item in sections] == [
        "Controller / Trends",
        "Controller / QuickWatchLists",
    ]


# ---------------------------------------------------------------------------
# Alias tags: the binding to a physical point
# ---------------------------------------------------------------------------


def test_an_alias_tag_keeps_its_target_and_says_the_alias_was_not_followed(
    tmp_path: Path, settings: Settings
) -> None:
    """The AliasFor target is the binding between a symbol and a wired point.

    Dropping it would make "we did not follow this alias" indistinguishable from
    "this tag has no alias", which is the silent failure rule 1 forbids.
    """
    body = (
        '<Modules><Module Name="Local" CatalogNumber="1756-L83E"/></Modules>'
        '<Tags><Tag Name="ESTOP_OK" TagType="Alias" AliasFor="Local:1:I.Data[3]">'
        "<Description><![CDATA[E-stop healthy.]]></Description></Tag>"
        '<Tag Name="TRIP_HISTORY" TagType="Base" DataType="DINT" Dimensions="16"/></Tags>'
    )
    path = _write(tmp_path, "alias.L5X", _l5x(body))

    result = _parse(path, settings)
    alias = _tag(result, "ESTOP_OK")

    assert alias["alias_for"] == "Local:1:I.Data[3]"
    assert alias["tag_type"] == "Alias"
    assert _tag(result, "TRIP_HISTORY")["dimensions"] == "16"
    assert "Local:1:I.Data[3]" in json.dumps(result.content)

    items = [item for item in result.unsupported if item.kind == "tag_alias"]
    assert len(items) == 1
    assert items[0].location == "Controller tag ESTOP_OK"
    assert "Local:1:I.Data[3]" in items[0].detail
    assert "not followed" in items[0].detail
    assert "Local:1:I.Data[3]" in result.anchors_by_key["tag:controller/ESTOP_OK"].excerpt


def test_an_alias_tag_with_no_target_attribute_is_still_declared_unfollowed(
    tmp_path: Path, settings: Settings
) -> None:
    body = '<Tags><Tag Name="ESTOP_OK" TagType="Alias" DataType="BOOL"/></Tags>'
    path = _write(tmp_path, "alias_no_target.L5X", _l5x(body))

    result = _parse(path, settings)

    items = [item for item in result.unsupported if item.kind == "tag_alias"]
    assert len(items) == 1
    assert "does not name" in items[0].detail


# ---------------------------------------------------------------------------
# Repeated containers
# ---------------------------------------------------------------------------


def test_a_second_tags_block_is_read_instead_of_being_dropped(
    tmp_path: Path, settings: Settings
) -> None:
    """Reading only the first <Tags> made the parser call a declared tag undeclared."""
    body = (
        '<Tags><Tag Name="FIRST_BLOCK_TAG" DataType="BOOL"/></Tags>'
        '<Tags><Tag Name="SECOND_BLOCK_TAG" DataType="BOOL"/></Tags>'
        + _program(_rung(0, "XIC(FIRST_BLOCK_TAG)OTE(SECOND_BLOCK_TAG);"))
    )
    path = _write(tmp_path, "two_tag_blocks.L5X", _l5x(body))

    result = _parse(path, settings)

    assert [tag["name"] for tag in result.content["tags"]] == [
        "FIRST_BLOCK_TAG",
        "SECOND_BLOCK_TAG",
    ]
    assert result.summary["tag_count"] == 2
    assert result.content["unresolved_references"] == []
    assert WarningCode.UNRESOLVED_REFERENCE not in _codes(result)

    repeated = [
        warning
        for warning in result.warnings
        if warning.code == WarningCode.STRUCTURE_UNRECOGNISED
        and warning.location == "Controller / Tags"
    ]
    assert len(repeated) == 1
    assert "2 <Tags>" in repeated[0].message


def test_a_second_programs_block_is_read_instead_of_being_dropped(
    tmp_path: Path, settings: Settings
) -> None:
    body = _program(_rung(0, "XIC(A_Tag)OTE(B_Tag);")) + (
        '<Programs><Program Name="P2" MainRoutineName="R2"><Routines>'
        '<Routine Name="R2" Type="RLL"><RLLContent>'
        + _rung(0, "XIC(C_Tag)OTE(D_Tag);")
        + "</RLLContent></Routine></Routines></Program></Programs>"
    )
    path = _write(tmp_path, "two_program_blocks.L5X", _l5x(body))

    result = _parse(path, settings)

    assert result.summary["program_names"] == ["P1", "P2"]
    assert result.summary["program_count"] == 2
    assert result.summary["routine_count"] == 2
    assert [(rung["program"], rung["text"]) for rung in result.content["rungs"]] == [
        ("P1", "XIC(A_Tag)OTE(B_Tag);"),
        ("P2", "XIC(C_Tag)OTE(D_Tag);"),
    ]
    assert any(
        warning.location == "Controller / Programs"
        and warning.code == WarningCode.STRUCTURE_UNRECOGNISED
        for warning in result.warnings
    )


def test_a_second_routines_block_in_one_program_is_read_instead_of_being_dropped(
    tmp_path: Path, settings: Settings
) -> None:
    routines = (
        '<Routines><Routine Name="R_KEPT" Type="RLL"><RLLContent>'
        + _rung(0, "XIC(A_Tag)OTE(B_Tag);")
        + "</RLLContent></Routine></Routines>"
        '<Routines><Routine Name="R_LOST" Type="RLL"><RLLContent>'
        + _rung(0, "XIC(C_Tag)OTE(D_Tag);")
        + "</RLLContent></Routine></Routines>"
    )
    body = f'<Programs><Program Name="P1" MainRoutineName="R_KEPT">{routines}</Program></Programs>'
    path = _write(tmp_path, "two_routine_blocks.L5X", _l5x(body))

    result = _parse(path, settings)

    assert [routine["name"] for routine in result.content["programs"][0]["routines"]] == [
        "R_KEPT",
        "R_LOST",
    ]
    assert result.summary["routine_count"] == 2
    assert result.summary["rung_count"] == 2
    assert _rung_of(result, "R_LOST", 0)["text"] == "XIC(C_Tag)OTE(D_Tag);"
    assert any(
        warning.location == "P1 / Routines" and warning.code == WarningCode.STRUCTURE_UNRECOGNISED
        for warning in result.warnings
    )


def test_a_second_rll_content_block_in_one_routine_is_read_instead_of_being_dropped(
    tmp_path: Path, settings: Settings
) -> None:
    body = (
        '<Programs><Program Name="P1" MainRoutineName="R1"><Routines>'
        '<Routine Name="R1" Type="RLL">'
        f"<RLLContent>{_rung(0, 'XIC(A_Tag)OTE(B_Tag);')}</RLLContent>"
        f"<RLLContent>{_rung(1, 'XIC(C_Tag)OTE(D_Tag);')}</RLLContent>"
        "</Routine></Routines></Program></Programs>"
    )
    path = _write(tmp_path, "two_rll_blocks.L5X", _l5x(body))

    result = _parse(path, settings)

    assert [rung["number"] for rung in result.content["rungs"]] == [0, 1]
    assert _rung_of(result, "R1", 1)["text"] == "XIC(C_Tag)OTE(D_Tag);"
    assert result.content["programs"][0]["routines"][0]["rungs"] == [0, 1]
    assert any(
        warning.location == "P1 / R1" and warning.code == WarningCode.STRUCTURE_UNRECOGNISED
        for warning in result.warnings
    )


def test_unread_addon_instruction_sections_are_recorded_as_a_boundary(
    tmp_path: Path, settings: Settings
) -> None:
    """<LocalTags> declares tags this parser never reads, so it has to say so."""
    body = (
        '<AddOnInstructionDefinitions><AddOnInstructionDefinition Name="MotorStarter"'
        ' Revision="1.2">'
        '<LocalTags><LocalTag Name="Seal" DataType="BOOL"/></LocalTags>'
        '<Routines><Routine Name="Logic" Type="RLL"><RLLContent/></Routine></Routines>'
        "</AddOnInstructionDefinition></AddOnInstructionDefinitions>"
    )
    path = _write(tmp_path, "aoi_local_tags.L5X", _l5x(body))

    result = _parse(path, settings)

    items = [item for item in result.unsupported if item.kind == "addon_instruction_section"]
    assert [item.location for item in items] == ["Add-On Instruction MotorStarter / LocalTags"]
    assert "local tag" in items[0].detail
    assert normalise_tag_name("Seal") not in result.content["tag_index"]


# ---------------------------------------------------------------------------
# Anchor keys stay unique
# ---------------------------------------------------------------------------


def test_two_programs_of_the_same_name_never_share_an_anchor_key(
    tmp_path: Path, settings: Settings
) -> None:
    """A shadowed anchor would cite the wrong rung's text for the second program."""
    program = (
        '<Program Name="P1" MainRoutineName="R1"><Routines>'
        '<Routine Name="R1" Type="RLL"><RLLContent>'
        "{rung}"
        "</RLLContent></Routine></Routines></Program>"
    )
    body = (
        "<Programs>"
        + program.format(rung=_rung(0, "XIC(A_First)OTE(B_Tag);"))
        + program.format(rung=_rung(0, "XIC(A_Second)OTE(B_Tag);"))
        + "</Programs>"
    )
    path = _write(tmp_path, "same_named_programs.L5X", _l5x(body))

    result = _parse(path, settings)
    keys = [anchor.key for anchor in result.anchors]

    assert len(set(keys)) == len(keys)
    assert "rung:P1/R1/0" in keys
    assert "rung:P1/R1/0#2" in keys
    assert result.anchors_by_key["rung:P1/R1/0"].excerpt == "XIC(A_First)OTE(B_Tag);"
    assert result.anchors_by_key["rung:P1/R1/0#2"].excerpt == "XIC(A_Second)OTE(B_Tag);"
    assert [rung["anchor_key"] for rung in result.content["rungs"]] == [
        "rung:P1/R1/0",
        "rung:P1/R1/0#2",
    ]
    collisions = [
        warning.message
        for warning in result.warnings
        if warning.code == WarningCode.STRUCTURE_UNRECOGNISED and "anchor key" in warning.message
    ]
    assert any("rung:P1/R1/0#2" in message for message in collisions)
    # The warning must not claim the shadowed element is unaddressable when it
    # has just been given an address.
    assert not any("only the first is addressable" in message for message in collisions)


def test_every_tag_and_rung_carries_the_anchor_key_it_was_filed_under(demo: ParseResult) -> None:
    """A citation must copy this key, not rebuild it: rebuilding cannot see a clash."""
    by_key = demo.anchors_by_key

    for tag in demo.content["tags"]:
        anchor = by_key[tag["anchor_key"]]
        assert anchor.anchor_type is AnchorType.L5X_TAG
        assert anchor.structured_location["tag"] == tag["name"]

    for rung in demo.content["rungs"]:
        anchor = by_key[rung["anchor_key"]]
        assert anchor.structured_location == {
            "program": rung["program"],
            "routine": rung["routine"],
            "rung": rung["number"],
        }

    assert _tag(demo, "ESTOP_OK")["anchor_key"] == "tag:controller/ESTOP_OK"
    assert _rung_of(demo, "Alarm_Logic", 2)["anchor_key"] == "rung:MainProgram/Alarm_Logic/2"


def test_the_qualified_tag_anchor_key_is_recorded_on_the_tag_it_belongs_to(
    tmp_path: Path, settings: Settings
) -> None:
    """P2's tag is anchored under a qualified key, so P2's record has to carry it."""
    program = (
        '<Program Name="{name}" MainRoutineName="R1">'
        '<Tags><Tag Name="Shared" DataType="BOOL"/></Tags>'
        '<Routines><Routine Name="R1" Type="RLL"><RLLContent>'
        '<Rung Number="0" Type="N"><Text><![CDATA[OTE(Shared);]]></Text></Rung>'
        "</RLLContent></Routine></Routines></Program>"
    )
    body = f"<Programs>{program.format(name='P1')}{program.format(name='P2')}</Programs>"
    path = _write(tmp_path, "shared_tag_keys.L5X", _l5x(body))

    result = _parse(path, settings)

    assert [tag["anchor_key"] for tag in result.content["tags"]] == [
        "tag:program/Shared",
        "tag:program/P2/Shared",
    ]
    assert result.anchors_by_key["tag:program/P2/Shared"].structured_location["program"] == "P2"


# ---------------------------------------------------------------------------
# The tag index and reference resolution
# ---------------------------------------------------------------------------


def test_a_name_declared_at_two_scopes_is_reported_rather_than_silently_shadowed(
    tmp_path: Path, settings: Settings
) -> None:
    """The index keeps one entry; which one is invisible unless it is reported."""
    body = '<Tags><Tag Name="Interlock_OK" DataType="BOOL"/></Tags>' + _program(
        _rung(0, "XIC(Interlock_OK)OTE(Interlock_OK);"),
        tags='<Tag Name="Interlock_OK" DataType="DINT"/>',
    )
    path = _write(tmp_path, "two_scopes.L5X", _l5x(body))

    result = _parse(path, settings)

    assert [(tag["scope"], tag["data_type"]) for tag in result.content["tags"]] == [
        ("controller", "BOOL"),
        ("program", "DINT"),
    ]
    assert result.summary["tag_count"] == 2
    assert result.summary["indexed_tag_count"] == 1

    shadowed = [
        warning
        for warning in result.warnings
        if warning.code == WarningCode.STRUCTURE_UNRECOGNISED
        and warning.location == "P1 / Tag Interlock_OK"
    ]
    assert len(shadowed) == 1
    assert "program scope (P1)" in shadowed[0].message
    assert "controller scope" in shadowed[0].message


def test_an_operand_named_like_a_routine_stays_a_tag_reference(
    tmp_path: Path, settings: Settings
) -> None:
    """Only JSR's first operand is a routine target; XIC's operand is a signal."""
    body = _program(
        _rung(0, "XIC(Interlocks)OTE(Out);") + _rung(1, "JSR(Interlocks,0);"),
        routine="Interlocks",
        tags='<Tag Name="Out" DataType="BOOL"/>',
    )
    path = _write(tmp_path, "routine_named_operand.L5X", _l5x(body))

    result = _parse(path, settings)

    assert _rung_of(result, "Interlocks", 0)["referenced_tags"] == ["Interlocks", "Out"]
    assert result.content["unresolved_references"] == [
        {
            "name": "Interlocks",
            "program": "P1",
            "routine": "Interlocks",
            "rung": 0,
            "kind": "tag",
        }
    ]
    assert WarningCode.UNRESOLVED_REFERENCE in _codes(result)

    assert _rung_of(result, "Interlocks", 1)["referenced_tags"] == []


# ---------------------------------------------------------------------------
# Excerpts claim only what the export states
# ---------------------------------------------------------------------------


def test_the_controller_excerpt_quotes_revision_attributes_without_claiming_firmware(
    demo: ParseResult,
) -> None:
    """fixtures/README.md forbids claiming firmware behaviour from an export."""
    excerpt = demo.anchors_by_key["controller:Conveyor_Line3"].excerpt

    assert "MajorRev 32 / MinorRev 11 (as exported)" in excerpt
    assert "Firmware" not in excerpt


def test_absent_attributes_are_omitted_from_excerpts_rather_than_rendered_as_none(
    tmp_path: Path, settings: Settings
) -> None:
    path = _write(
        tmp_path,
        "sparse.L5X",
        '<?xml version="1.0"?><RSLogix5000Content SchemaRevision="1.0">'
        '<Controller Name="Sparse" MajorRev="32">'
        '<Modules><Module Name="M1"/></Modules>'
        "</Controller></RSLogix5000Content>",
    )

    result = _parse(path, settings)
    controller = result.anchors_by_key["controller:Sparse"].excerpt
    module = result.anchors_by_key["module:M1"].excerpt

    assert "MajorRev 32 (as exported)" in controller
    assert "MinorRev" not in controller
    assert "None" not in controller
    assert "Inhibited" not in module
    assert "MajorFault" not in module
    assert "None" not in module


# ---------------------------------------------------------------------------
# The tokeniser invents nothing
# ---------------------------------------------------------------------------


def test_unreadable_rung_text_does_not_become_an_invented_instruction(
    tmp_path: Path, settings: Settings
) -> None:
    """A pasted ST expression is unread text, not an instruction called ``Out``."""
    body = _program(_rung(0, "Out := In1 AND In2;"))
    path = _write(tmp_path, "expression_in_ladder.L5X", _l5x(body))

    result = _parse(path, settings)
    rung = _rung_of(result, "R1", 0)

    assert rung["instructions"] == []
    assert rung["text"] == "Out := In1 AND In2;"
    assert result.summary["mnemonics"] == []
    assert result.summary["instruction_count"] == 0
    assert not any(anchor.anchor_type is AnchorType.L5X_INSTRUCTION for anchor in result.anchors)

    unparsed = [
        warning for warning in result.warnings if warning.code == WarningCode.UNPARSED_INSTRUCTION
    ]
    assert len(unparsed) == 1
    assert "Out := In1 AND In2;" in unparsed[0].message


def test_an_operandless_instruction_survives_when_the_rung_tokenises_cleanly(
    tmp_path: Path, settings: Settings
) -> None:
    """AFI takes no operands and is real; only unread text is discarded."""
    body = _program(
        _rung(0, "AFI XIC(A_Tag)OTE(B_Tag);"),
        tags='<Tag Name="A_Tag" DataType="BOOL"/><Tag Name="B_Tag" DataType="BOOL"/>',
    )
    path = _write(tmp_path, "afi.L5X", _l5x(body))

    result = _parse(path, settings)

    assert [
        (instruction["mnemonic"], instruction["operands"])
        for instruction in _rung_of(result, "R1", 0)["instructions"]
    ] == [("AFI", []), ("XIC", ["A_Tag"]), ("OTE", ["B_Tag"])]
    assert WarningCode.UNPARSED_INSTRUCTION not in _codes(result)
    assert result.summary["mnemonics"] == ["AFI", "OTE", "XIC"]
