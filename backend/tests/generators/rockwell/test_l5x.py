"""L5X emission: rendering from the model, round-tripping, and refusing.

The round-trip tests are the only honest basis for claiming
``ROUND_TRIP_PARSED``. They generate from the structured rows, hand the bytes to
:mod:`app.parsers.l5x_parser`, and assert the rungs, instructions and operands
come back matching the model — not that the file "looks right".

The refusal tests assert the offending *name* is in the message. A refusal that
says "an instruction is not supported" tells an engineer nothing they can act
on, and a caller cannot turn it into a validation result that points anywhere.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest

from app.core.config import Settings
from app.core.enums import (
    ControllerFamily,
    DocumentRole,
    FileKind,
    InstructionCategory,
    LogicElementKind,
    OperandKind,
)
from app.generators.rockwell.l5x import (
    AOI_REFERENCE_NOTICE,
    ControllerContext,
    GenerationRefused,
    LogicScope,
    RefusalReason,
    emit_aoi_reference,
    emit_controller_project,
    emit_program,
    emit_routine,
    emit_udt,
    render_rung_text,
)
from app.parsers.base import ParseContext
from app.parsers.l5x_parser import L5XParser
from tests.generators.rockwell.conftest import (
    make_aoi,
    make_branch,
    make_controller,
    make_instruction,
    make_module,
    make_operand,
    make_program,
    make_routine,
    make_rung,
    make_tag,
    make_task,
    make_udt,
    make_udt_member,
)

CONTROLLER = ControllerContext(
    name="Line3", processor_type="1756-L83E", major_revision=32, minor_revision=11
)


def scope(
    *, tags: tuple[str, ...] = (), routines: tuple[str, ...] = (), aois: tuple[str, ...] = ()
) -> LogicScope:
    return LogicScope.build(
        family=ControllerFamily.CONTROLLOGIX, tags=tags, routines=routines, aois=aois
    )


def parse(data: bytes, tmp_path: Path, settings: Settings, name: str = "generated.L5X") -> Any:
    path = tmp_path / name
    path.write_bytes(data)
    return L5XParser().parse(
        ParseContext(
            file_id=uuid.UUID(int=9),
            project_id=uuid.UUID(int=9),
            path=path,
            filename=name,
            file_kind=FileKind.L5X,
            document_role=DocumentRole.CONTROL_LOGIC,
            settings=settings,
        )
    )


# ---------------------------------------------------------------------------
# Rung rendering
# ---------------------------------------------------------------------------


class TestRungRendering:
    def test_series_conditions_then_the_output_then_a_semicolon(self) -> None:
        rung = make_rung(
            0,
            [
                make_instruction(0, "XIC", ["A"]),
                make_instruction(1, "XIO", ["B"]),
                make_instruction(
                    2, "OTE", ["C"], element_kind=LogicElementKind.OUTPUT, is_destructive=True
                ),
            ],
        )
        assert render_rung_text(rung, scope(tags=("A", "B", "C"))) == "XIC(A)XIO(B)OTE(C);"

    def test_a_branch_renders_as_bracketed_legs(self) -> None:
        rung = make_rung(
            0,
            [
                make_instruction(0, "XIC", ["Start"], branch_level=1, branch_index=0),
                make_instruction(1, "XIC", ["Run"], branch_level=1, branch_index=1),
                make_instruction(2, "OTE", ["Run"], element_kind=LogicElementKind.OUTPUT),
            ],
            branches=[make_branch(level=1, leg_index=0), make_branch(level=1, leg_index=1)],
        )
        assert render_rung_text(rung, scope(tags=("Start", "Run"))) == (
            "[XIC(Start),XIC(Run)]OTE(Run);"
        )

    def test_a_nested_branch_renders_nested(self) -> None:
        rung = make_rung(
            0,
            [
                make_instruction(0, "XIC", ["A"], branch_level=1, branch_index=0),
                make_instruction(1, "XIC", ["B"], branch_level=2, branch_index=0),
                make_instruction(2, "XIC", ["C"], branch_level=2, branch_index=1),
                make_instruction(3, "XIC", ["D"], branch_level=1, branch_index=1),
                make_instruction(4, "OTE", ["E"], element_kind=LogicElementKind.OUTPUT),
            ],
            branches=[
                make_branch(level=1, leg_index=0),
                make_branch(level=1, leg_index=1),
                make_branch(level=2, leg_index=0),
                make_branch(level=2, leg_index=1),
            ],
        )
        assert render_rung_text(rung, scope(tags=("A", "B", "C", "D", "E"))) == (
            "[XIC(A)[XIC(B),XIC(C)],XIC(D)]OTE(E);"
        )

    def test_a_zero_operand_instruction_renders_with_empty_parentheses(self) -> None:
        rung = make_rung(
            0,
            [
                make_instruction(0, "AFI", [], category=InstructionCategory.PROGRAM_CONTROL),
                make_instruction(1, "OTE", ["C"], element_kind=LogicElementKind.OUTPUT),
            ],
        )
        assert render_rung_text(rung, scope(tags=("C",))) == "AFI()OTE(C);"

    def test_operand_text_is_used_verbatim_including_members(self) -> None:
        rung = make_rung(
            0,
            [
                make_instruction(
                    0,
                    "XIC",
                    [make_operand(0, "CV101_Status.Running", resolved_name="CV101_Status")],
                ),
                make_instruction(
                    1,
                    "GRT",
                    [
                        make_operand(0, "AIR_PSI"),
                        make_operand(1, "40.0", kind=OperandKind.LITERAL),
                    ],
                    category=InstructionCategory.COMPARE,
                ),
                make_instruction(2, "OTE", ["OK"], element_kind=LogicElementKind.OUTPUT),
            ],
        )
        assert render_rung_text(rung, scope(tags=("CV101_Status", "AIR_PSI", "OK"))) == (
            "XIC(CV101_Status.Running)GRT(AIR_PSI,40.0)OTE(OK);"
        )

    def test_a_branch_tree_that_disagrees_with_the_coordinates_is_refused(self) -> None:
        # The coordinates say two legs; the rows say three. Neither view can then
        # be trusted to be the rung somebody reviewed.
        rung = make_rung(
            0,
            [
                make_instruction(0, "XIC", ["A"], branch_level=1, branch_index=0),
                make_instruction(1, "XIC", ["B"], branch_level=1, branch_index=1),
                make_instruction(2, "OTE", ["C"], element_kind=LogicElementKind.OUTPUT),
            ],
            branches=[
                make_branch(level=1, leg_index=0),
                make_branch(level=1, leg_index=1),
                make_branch(level=1, leg_index=2),
            ],
        )
        with pytest.raises(GenerationRefused) as caught:
            render_rung_text(rung, scope(tags=("A", "B", "C")), location="P / R / Rung 0")
        assert caught.value.reason is RefusalReason.MALFORMED_BRANCH
        assert "P / R / Rung 0" in str(caught.value)

    def test_a_skipped_branch_leg_is_refused(self) -> None:
        rung = make_rung(
            0,
            [
                make_instruction(0, "XIC", ["A"], branch_level=1, branch_index=0),
                make_instruction(1, "XIC", ["B"], branch_level=1, branch_index=2),
            ],
        )
        with pytest.raises(GenerationRefused, match="Branch leg goes from 0 to 2"):
            render_rung_text(rung, scope(tags=("A", "B")))


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


class TestRefusals:
    def test_an_unresolved_operand_is_refused_by_name(self) -> None:
        rung = make_rung(
            0,
            [
                make_instruction(
                    0,
                    "XIC",
                    [
                        make_operand(
                            0,
                            "GRD_DR_CLSD",
                            kind=OperandKind.UNRESOLVED,
                            unresolved_reason="no tag by that name in controller scope",
                        )
                    ],
                )
            ],
        )
        with pytest.raises(GenerationRefused) as caught:
            render_rung_text(rung, scope(), location="MainProgram / R1 / Rung 0")
        assert caught.value.reason is RefusalReason.UNRESOLVED_OPERAND
        assert "GRD_DR_CLSD" in str(caught.value)
        assert "no tag by that name in controller scope" in str(caught.value)

    def test_a_restricted_instruction_is_refused_with_its_reason(self) -> None:
        rung = make_rung(
            0,
            [
                make_instruction(
                    0,
                    "MAM",
                    [make_operand(0, "Axis1")],
                    category=InstructionCategory.SPECIAL,
                    element_kind=LogicElementKind.OUTPUT,
                )
            ],
        )
        with pytest.raises(GenerationRefused) as caught:
            render_rung_text(rung, scope(tags=("Axis1",)))
        assert caught.value.reason is RefusalReason.RESTRICTED_INSTRUCTION
        assert "MAM" in str(caught.value)
        assert "Motion Axis Move" in str(caught.value)

    def test_an_instruction_unsupported_on_the_target_family_is_refused(self) -> None:
        # SCL is a PLC-5 / SLC instruction. It is catalogued so a migration
        # source can be read, and it is never emitted for a Logix target.
        rung = make_rung(
            0,
            [
                make_instruction(
                    0,
                    "SCL",
                    ["Raw", "Rate", "Offset", "Scaled"],
                    category=InstructionCategory.COMPUTE_MATH,
                    element_kind=LogicElementKind.OUTPUT,
                )
            ],
        )
        with pytest.raises(GenerationRefused) as caught:
            render_rung_text(rung, scope(tags=("Raw", "Rate", "Offset", "Scaled")))
        assert caught.value.reason is RefusalReason.UNSUPPORTED_INSTRUCTION
        assert "SCL" in str(caught.value)
        assert "controllogix" in str(caught.value)

    def test_an_undeclared_tag_is_refused_by_name(self) -> None:
        rung = make_rung(0, [make_instruction(0, "XIC", ["NOT_DECLARED"])])
        with pytest.raises(GenerationRefused) as caught:
            render_rung_text(rung, scope(tags=("SOMETHING_ELSE",)), location="P / R / Rung 0")
        assert caught.value.reason is RefusalReason.UNDECLARED_TAG
        assert "NOT_DECLARED" in str(caught.value)
        assert caught.value.subject == "NOT_DECLARED"

    def test_an_unknown_mnemonic_that_is_not_a_declared_aoi_is_refused(self) -> None:
        rung = make_rung(
            0,
            [
                make_instruction(
                    0,
                    "ESCO_Widget",
                    ["A"],
                    category=InstructionCategory.ADDON_INSTRUCTION,
                    element_kind=LogicElementKind.OUTPUT,
                )
            ],
        )
        with pytest.raises(GenerationRefused, match="ESCO_WIDGET"):
            render_rung_text(rung, scope(tags=("A",), aois=("MotorStarter",)))

    def test_a_declared_add_on_instruction_call_is_allowed(self) -> None:
        rung = make_rung(
            0,
            [
                make_instruction(
                    0,
                    "MotorStarter",
                    ["CV101_Starter", "Run"],
                    category=InstructionCategory.ADDON_INSTRUCTION,
                    element_kind=LogicElementKind.OUTPUT,
                )
            ],
        )
        rendered = render_rung_text(
            rung, scope(tags=("CV101_Starter", "Run"), aois=("MotorStarter",))
        )
        # The declared case is preserved: Logix matches an Add-On Instruction
        # name case-sensitively, so ``MOTORSTARTER`` would call nothing.
        assert rendered == "MotorStarter(CV101_Starter,Run);"

    def test_a_jsr_to_a_routine_that_does_not_exist_is_refused(self) -> None:
        rung = make_rung(
            0,
            [
                make_instruction(
                    0,
                    "JSR",
                    [
                        make_operand(0, "Missing_Routine", kind=OperandKind.ROUTINE_REFERENCE),
                        make_operand(1, "0", kind=OperandKind.LITERAL),
                    ],
                    category=InstructionCategory.PROGRAM_CONTROL,
                    element_kind=LogicElementKind.OUTPUT,
                )
            ],
        )
        with pytest.raises(GenerationRefused, match="Missing_Routine"):
            render_rung_text(rung, scope(routines=("Other_Routine",)))

    def test_a_wrong_operand_count_is_refused(self) -> None:
        rung = make_rung(0, [make_instruction(0, "XIC", ["A", "B"])])
        with pytest.raises(GenerationRefused) as caught:
            render_rung_text(rung, scope(tags=("A", "B")))
        assert caught.value.reason is RefusalReason.OPERAND_COUNT
        assert "XIC" in str(caught.value)

    def test_an_instruction_the_model_marked_unsupported_is_refused(self) -> None:
        rung = make_rung(
            0,
            [
                make_instruction(
                    0,
                    "XIC",
                    ["A"],
                    is_supported=False,
                    unsupported_reason="read from a source this platform will not regenerate",
                )
            ],
        )
        with pytest.raises(GenerationRefused, match="will not regenerate"):
            render_rung_text(rung, scope(tags=("A",)))


# ---------------------------------------------------------------------------
# Round trip
# ---------------------------------------------------------------------------


class TestRoutineRoundTrip:
    def _routine_bytes(self) -> tuple[bytes, str]:
        rung = make_rung(
            0,
            [
                make_instruction(0, "XIC", ["ESTOP_OK"]),
                make_instruction(1, "XIC", ["Start_PB"], branch_level=1, branch_index=0),
                make_instruction(2, "XIC", ["Run_Cmd"], branch_level=1, branch_index=1),
                make_instruction(3, "OTE", ["Run_Cmd"], element_kind=LogicElementKind.OUTPUT),
            ],
            comment="Seal-in, with the ]]> terminator pasted into the comment.",
            branches=[make_branch(level=1, leg_index=0), make_branch(level=1, leg_index=1)],
        )
        second = make_rung(
            1,
            [
                make_instruction(
                    0,
                    "TON",
                    [
                        make_operand(0, "T_Dwell", is_destination=True),
                        make_operand(1, "5000", kind=OperandKind.LITERAL),
                        make_operand(2, "0", kind=OperandKind.LITERAL),
                    ],
                    category=InstructionCategory.TIMER_COUNTER,
                    element_kind=LogicElementKind.OUTPUT,
                )
            ],
        )
        logic_scope = scope(tags=("ESTOP_OK", "Start_PB", "Run_Cmd", "T_Dwell"))
        routine = make_routine("Conveyor_Control", description="CV101 run logic.")
        data = emit_routine(
            controller=CONTROLLER,
            program_name="MainProgram",
            routine=routine,
            rungs=[rung, second],
            scope=logic_scope,
        )
        return data, render_rung_text(rung, logic_scope)

    def test_the_rungs_come_back_as_written(self, tmp_path: Path, settings: Settings) -> None:
        data, expected_first = self._routine_bytes()
        result = parse(data, tmp_path, settings)
        rungs = result.content["rungs"]
        assert [rung["text"] for rung in rungs] == [
            expected_first,
            "TON(T_Dwell,5000,0);",
        ]
        assert expected_first == "XIC(ESTOP_OK)[XIC(Start_PB),XIC(Run_Cmd)]OTE(Run_Cmd);"

    def test_the_instructions_and_operands_come_back_matching_the_model(
        self, tmp_path: Path, settings: Settings
    ) -> None:
        data, _ = self._routine_bytes()
        result = parse(data, tmp_path, settings)
        first = result.content["rungs"][0]
        assert [
            (instruction["mnemonic"], tuple(instruction["operands"]))
            for instruction in first["instructions"]
        ] == [
            ("XIC", ("ESTOP_OK",)),
            ("XIC", ("Start_PB",)),
            ("XIC", ("Run_Cmd",)),
            ("OTE", ("Run_Cmd",)),
        ]
        second = result.content["rungs"][1]
        assert second["instructions"][0]["operands"] == ["T_Dwell", "5000", "0"]

    def test_a_comment_containing_the_cdata_terminator_survives(
        self, tmp_path: Path, settings: Settings
    ) -> None:
        data, _ = self._routine_bytes()
        result = parse(data, tmp_path, settings)
        assert result.content["rungs"][0]["comment"] == (
            "Seal-in, with the ]]> terminator pasted into the comment."
        )

    def test_the_target_and_its_context_are_declared(
        self, tmp_path: Path, settings: Settings
    ) -> None:
        data, _ = self._routine_bytes()
        text = data.decode("utf-8")
        assert 'TargetType="Routine"' in text
        assert 'TargetName="Conveyor_Control"' in text
        assert 'ContainsContext="true"' in text
        assert 'Use="Context"' in text
        result = parse(data, tmp_path, settings)
        assert result.content["programs"][0]["name"] == "MainProgram"
        assert result.content["programs"][0]["routines"][0]["name"] == "Conveyor_Control"

    def test_no_wall_clock_timestamp_is_written(self) -> None:
        data, _ = self._routine_bytes()
        assert b"ExportDate" not in data

    def test_an_export_date_is_written_when_the_profile_supplies_one(self) -> None:
        controller = ControllerContext(name="Line3", export_date="Mon Jan 06 09:14:22 2025")
        data = emit_routine(
            controller=controller,
            program_name="P1",
            routine=make_routine("R1"),
            rungs=[],
            scope=scope(),
        )
        assert b'ExportDate="Mon Jan 06 09:14:22 2025"' in data


class TestProgramRoundTrip:
    def test_program_tags_and_descriptions_survive_escaping(
        self, tmp_path: Path, settings: Settings
    ) -> None:
        awkward = 'Trips when P < 40 & the "wet" guard is open'
        program = make_program("MainProgram", description="Line 3 control.")
        tags = [
            make_tag("Run_Cmd", description=awkward, scope_program_id=program.id),
            make_tag(
                "ESTOP_Alias",
                is_alias=True,
                alias_for='DI_01:I.Data[0] "<&>"',
                scope_program_id=program.id,
            ),
        ]
        routine = make_routine("R1")
        rung = make_rung(
            0,
            [
                make_instruction(0, "XIC", ["Run_Cmd"]),
                make_instruction(1, "OTE", ["Run_Cmd"], element_kind=LogicElementKind.OUTPUT),
            ],
        )
        data = emit_program(
            controller=CONTROLLER,
            program=program,
            tags=tags,
            routines=[(routine, [rung])],
            scope=scope(tags=("Run_Cmd", "ESTOP_Alias"), routines=("R1",)),
        )
        result = parse(data, tmp_path, settings)
        by_name = {tag["name"]: tag for tag in result.content["tags"]}
        assert by_name["Run_Cmd"]["description"] == awkward
        assert by_name["ESTOP_Alias"]["alias_for"] == 'DI_01:I.Data[0] "<&>"'
        assert by_name["ESTOP_Alias"]["tag_type"] == "Alias"
        assert result.content["rungs"][0]["text"] == "XIC(Run_Cmd)OTE(Run_Cmd);"


class TestComponentDocuments:
    def test_a_udt_keeps_member_order(self, tmp_path: Path, settings: Settings) -> None:
        data = emit_udt(
            controller=CONTROLLER,
            udt=make_udt(),
            members=[
                make_udt_member("Running", 0, description="Drive is running."),
                make_udt_member("Faulted", 1),
                make_udt_member("Speed", 2, dimensions="[4]"),
            ],
        )
        result = parse(data, tmp_path, settings)
        datatype = result.content["datatypes"][0]
        assert datatype["name"] == "ConveyorStatus"
        assert [member["name"] for member in datatype["members"]] == [
            "Running",
            "Faulted",
            "Speed",
        ]
        assert datatype["members"][2]["dimension"] == 4
        assert b'TargetType="DataType"' in data

    def test_a_multi_dimensional_udt_member_is_refused(self) -> None:
        with pytest.raises(GenerationRefused, match="multi-dimensional"):
            emit_udt(
                controller=CONTROLLER,
                udt=make_udt(),
                members=[make_udt_member("Grid", 0, dimensions="[4,8]")],
            )

    def test_an_aoi_file_declares_the_interface_and_says_it_has_no_logic(
        self, tmp_path: Path, settings: Settings
    ) -> None:
        data = emit_aoi_reference(controller=CONTROLLER, aoi=make_aoi())
        result = parse(data, tmp_path, settings)
        aoi = result.content["addon_instructions"][0]
        assert aoi["name"] == "MotorStarter"
        assert [parameter["name"] for parameter in aoi["parameters"]] == ["StartCmd", "RunOut"]
        assert aoi["routine_names"] == []
        assert AOI_REFERENCE_NOTICE.split(" - ")[0] in aoi["description"]

    def test_the_controller_skeleton_carries_modules_tasks_and_program_shells(
        self, tmp_path: Path, settings: Settings
    ) -> None:
        data = emit_controller_project(
            controller=ControllerContext.from_model(make_controller(description="Line 3.")),
            modules=[make_module()],
            tasks=[(make_task(), ("MainProgram",))],
            programs=[make_program("MainProgram")],
        )
        text = data.decode("utf-8")
        assert 'TargetType="Controller"' in text
        assert 'ContainsContext="false"' in text
        result = parse(data, tmp_path, settings)
        assert result.content["controller"]["name"] == "Line3"
        assert [module["name"] for module in result.content["modules"]] == ["DI_01"]
        assert result.content["tasks"][0]["scheduled_programs"] == ["MainProgram"]
        # A shell: the program is declared, its routines come from elsewhere.
        assert result.content["programs"][0]["routines"] == []
