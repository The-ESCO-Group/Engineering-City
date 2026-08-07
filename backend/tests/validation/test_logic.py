"""The PLC-logic validators.

Three negative cases here are the ones that keep the whole module credible:

* a ``JMP`` whose label the parser could not resolve is not an unresolved tag;
* an ``OTL`` and its ``OTU`` writing one bit is a design, not a duplicate;
* a ``CTU`` and its ``RES`` likewise.

Each of those is a case where a naive implementation produces a confident error
against correct logic, and the instruction catalogue exists precisely so the
distinction is data rather than a special case somebody has to remember.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.enums import (
    AlarmSeverity,
    ControllerFamily,
    InstructionCategory,
    ObjectType,
    ResetBehaviorKind,
    RoutineLanguage,
    SignalDirection,
    SignalType,
    ValidationSeverity,
)
from app.models.plc import LogicInstruction, LogicRung
from app.models.project import Project
from app.models.tags import Tag
from app.validation.logic import (
    DestructiveWriteValidator,
    InstructionSupportValidator,
    InterlockImplementationValidator,
    ModeLogicValidator,
    OperandValidator,
    OutputCoverageValidator,
    ResetBehaviourValidator,
    RoutineCallValidator,
    SequenceStateValidator,
)
from tests.validation import (
    codes,
    make_alarm,
    make_assignment,
    make_device,
    make_fault,
    make_instruction,
    make_interlock,
    make_io_point,
    make_ladder,
    make_mode,
    make_permissive,
    make_program,
    make_reset_behavior,
    make_routine,
    make_rung,
    make_signal,
    make_state,
    make_tag,
    make_target_platform,
    make_transition,
    only,
    read_tag,
    run_check,
    write_tag,
)

# ---------------------------------------------------------------------------
# logic.unresolved_operand
# ---------------------------------------------------------------------------
UNRESOLVED = "logic.unresolved_operand"


def test_an_unresolved_operand_is_an_error(db: Session, project: Project) -> None:
    rung = make_ladder(db, project)
    make_instruction(db, project, rung, "XIC", ["ALM_HOPPER_HIGH"], unresolved=[0])

    issue = only(run_check(OperandValidator(), db, project), UNRESOLVED)

    assert issue.severity is ValidationSeverity.ERROR
    assert issue.object_type is ObjectType.LOGIC_RUNG
    assert issue.object_label == "MainProgram / MainRoutine / Rung 0"
    assert "no tag by that name" in issue.message


def test_a_resolved_operand_does_not_fire(db: Session, project: Project) -> None:
    rung = make_ladder(db, project)
    read_tag(db, project, rung, "Motor_Run_Cmd")

    assert UNRESOLVED not in codes(run_check(OperandValidator(), db, project))


def test_a_jump_label_is_not_an_unresolved_tag(db: Session, project: Project) -> None:
    """No ``<Tags>`` entry declares a label, so the parser always reports one."""
    rung = make_ladder(db, project)
    make_instruction(
        db,
        project,
        rung,
        "JMP",
        ["Restart"],
        category=InstructionCategory.PROGRAM_CONTROL,
        unresolved=[0],
    )

    assert UNRESOLVED not in codes(run_check(OperandValidator(), db, project))


def test_an_external_routine_call_is_not_an_unresolved_tag(db: Session, project: Project) -> None:
    """JXR names a routine this project does not declare, exactly as JSR names one.

    Reported, the message told the engineer to "declare the tag the operand
    names" — the invent-a-tag outcome the non-tag table exists to prevent.
    """
    rung = make_ladder(db, project)
    make_instruction(
        db,
        project,
        rung,
        "JXR",
        ["Ext_Routine", "0", "Param"],
        category=InstructionCategory.PROGRAM_CONTROL,
        unresolved=[0],
    )

    assert UNRESOLVED not in codes(run_check(OperandValidator(), db, project))


def test_a_cip_object_path_is_not_an_unresolved_tag(db: Session, project: Project) -> None:
    """GSV's first three operands name a CIP class, instance and attribute.

    None of them is a tag, and the instance is routinely empty. Cataloguing GSV
    without saying so would trade one missing check for three false errors on
    every GSV in the project.
    """
    rung = make_ladder(db, project)
    make_instruction(
        db,
        project,
        rung,
        "GSV",
        ["WallClockTime", "", "LocalDateTime", "Now"],
        category=InstructionCategory.SPECIAL,
        unresolved=[0, 1, 2],
    )

    assert UNRESOLVED not in codes(run_check(OperandValidator(), db, project))


def test_an_unresolved_operand_with_no_reason_says_so(db: Session, project: Project) -> None:
    rung = make_ladder(db, project)
    make_instruction(db, project, rung, "XIC", ["Mystery"], unresolved=[0], unresolved_reason="")

    issue = only(run_check(OperandValidator(), db, project), UNRESOLVED)

    assert "No reason for the failure was recorded" in issue.message


# ---------------------------------------------------------------------------
# logic.duplicate_destructive_output
# ---------------------------------------------------------------------------
DUPLICATE = "logic.duplicate_destructive_output"
MASKED = "logic.masked_duplicate_destination"


def test_two_coils_on_one_tag_is_an_error(db: Session, project: Project) -> None:
    first = make_ladder(db, project, rung_ordinal=0)
    second = make_rung(db, project, first.routine, ordinal=1)
    write_tag(db, project, first, "Motor_Run_Cmd")
    write_tag(db, project, second, "Motor_Run_Cmd")

    issue = only(run_check(DestructiveWriteValidator(), db, project), DUPLICATE)

    assert issue.severity is ValidationSeverity.ERROR
    assert "Rung 0" in issue.message
    assert "Rung 1" in issue.message
    assert issue.detail["write_count"] == 2


def test_a_single_coil_does_not_fire(db: Session, project: Project) -> None:
    rung = make_ladder(db, project)
    write_tag(db, project, rung, "Motor_Run_Cmd")

    assert DUPLICATE not in codes(run_check(DestructiveWriteValidator(), db, project))


def test_a_latch_and_its_unlatch_are_not_a_duplicate(db: Session, project: Project) -> None:
    first = make_ladder(db, project, rung_ordinal=0)
    second = make_rung(db, project, first.routine, ordinal=1)
    write_tag(db, project, first, "Fault_Latched", mnemonic="OTL")
    write_tag(db, project, second, "Fault_Latched", mnemonic="OTU")

    assert DUPLICATE not in codes(run_check(DestructiveWriteValidator(), db, project))


def test_a_counter_and_its_reset_are_not_a_duplicate(db: Session, project: Project) -> None:
    first = make_ladder(db, project, rung_ordinal=0)
    second = make_rung(db, project, first.routine, ordinal=1)
    make_instruction(
        db,
        project,
        first,
        "CTU",
        ["Part_Count", "100", "0"],
        category=InstructionCategory.TIMER_COUNTER,
        literals=[1, 2],
    )
    make_instruction(
        db,
        project,
        second,
        "RES",
        ["Part_Count"],
        category=InstructionCategory.TIMER_COUNTER,
    )

    assert DUPLICATE not in codes(run_check(DestructiveWriteValidator(), db, project))


def test_two_latches_on_one_bit_are_still_a_duplicate(db: Session, project: Project) -> None:
    first = make_ladder(db, project, rung_ordinal=0)
    second = make_rung(db, project, first.routine, ordinal=1)
    write_tag(db, project, first, "Fault_Latched", mnemonic="OTL")
    write_tag(db, project, second, "Fault_Latched", mnemonic="OTL")

    assert DUPLICATE in codes(run_check(DestructiveWriteValidator(), db, project))


def test_two_masked_writes_are_a_warning_not_an_error(db: Session, project: Project) -> None:
    first = make_ladder(db, project, rung_ordinal=0)
    second = make_rung(db, project, first.routine, ordinal=1)
    for index, rung in enumerate((first, second)):
        make_instruction(
            db,
            project,
            rung,
            "MVM",
            [f"Source_{index}", "16#00FF", "Status_Word"],
            category=InstructionCategory.MOVE_LOGICAL,
            literals=[1],
        )

    issue = only(run_check(DestructiveWriteValidator(), db, project), MASKED)

    assert issue.severity is ValidationSeverity.WARNING
    assert "masks overlap" in issue.message


def test_a_fill_and_a_move_on_one_tag_is_an_error(db: Session, project: Project) -> None:
    """FLL was absent from the catalogue, so it contributed no write at all.

    Nothing else records one — ``LogicOperand.is_destination`` is never
    populated — so the group had one member and the project came back clean on
    exactly the defect the duplicate check exists to find.
    """
    first = make_ladder(db, project, rung_ordinal=0)
    second = make_rung(db, project, first.routine, ordinal=1)
    make_instruction(
        db,
        project,
        first,
        "FLL",
        ["0", "Buffer[0]", "10"],
        category=InstructionCategory.MOVE_LOGICAL,
        literals=[0, 2],
    )
    make_instruction(
        db,
        project,
        second,
        "MOV",
        ["Setpoint", "Buffer[0]"],
        category=InstructionCategory.MOVE_LOGICAL,
    )

    issue = only(run_check(DestructiveWriteValidator(), db, project), DUPLICATE)

    assert issue.severity is ValidationSeverity.ERROR
    assert issue.detail["mnemonics"] == ["FLL", "MOV"]


def test_a_get_system_value_destination_is_a_duplicate(db: Session, project: Project) -> None:
    """GSV writes operand 3, and a COP into the same element is a real collision."""
    first = make_ladder(db, project, rung_ordinal=0)
    second = make_rung(db, project, first.routine, ordinal=1)
    make_instruction(
        db,
        project,
        first,
        "GSV",
        ["WallClockTime", "", "LocalDateTime", "Now[0]"],
        category=InstructionCategory.SPECIAL,
        literals=[0, 1, 2],
    )
    make_instruction(
        db,
        project,
        second,
        "COP",
        ["Stamp", "Now[0]", "7"],
        category=InstructionCategory.MOVE_LOGICAL,
        literals=[2],
    )

    issue = only(run_check(DestructiveWriteValidator(), db, project), DUPLICATE)

    assert issue.detail["mnemonics"] == ["COP", "GSV"]


def test_a_queue_loaded_and_unloaded_is_not_a_duplicate(db: Session, project: Project) -> None:
    """FFL and FFU share the CONTROL and the array by design, as CTU shares with CTD."""
    first = make_ladder(db, project, rung_ordinal=0)
    second = make_rung(db, project, first.routine, ordinal=1)
    make_instruction(
        db,
        project,
        first,
        "FFL",
        ["New_Part", "Part_Queue[0]", "Queue_Ctl", "10", "0"],
        category=InstructionCategory.SPECIAL,
        literals=[3, 4],
    )
    make_instruction(
        db,
        project,
        second,
        "FFU",
        ["Part_Queue[0]", "Next_Part", "Queue_Ctl", "10", "0"],
        category=InstructionCategory.SPECIAL,
        literals=[3, 4],
    )

    assert DUPLICATE not in codes(run_check(DestructiveWriteValidator(), db, project))


def test_a_sequencer_input_and_output_share_one_control(db: Session, project: Project) -> None:
    first = make_ladder(db, project, rung_ordinal=0)
    second = make_rung(db, project, first.routine, ordinal=1)
    make_instruction(
        db,
        project,
        first,
        "SQI",
        ["Step_Table[0]", "16#FFFF", "Inputs", "Seq_Ctl", "8", "0"],
        category=InstructionCategory.SPECIAL,
        literals=[1, 4, 5],
    )
    make_instruction(
        db,
        project,
        second,
        "SQO",
        ["Step_Table[0]", "16#FFFF", "Outputs", "Seq_Ctl", "8", "0"],
        category=InstructionCategory.SPECIAL,
        literals=[1, 4, 5],
    )

    assert DUPLICATE not in codes(run_check(DestructiveWriteValidator(), db, project))


# ---------------------------------------------------------------------------
# logic.range_duplicate_destination
# ---------------------------------------------------------------------------
RANGE_DUPLICATE = "logic.range_duplicate_destination"


def _recipe_copy(
    db: Session,
    project: Project,
    rung: LogicRung,
    *,
    length: str = "20",
    destination: str = "Recipe_Active[0]",
    mnemonic: str = "COP",
) -> LogicInstruction:
    return make_instruction(
        db,
        project,
        rung,
        mnemonic,
        ["Recipe_Master[0]", destination, length],
        category=InstructionCategory.MOVE_LOGICAL,
        literals=[2] if length.isdigit() else [],
    )


def test_a_copy_range_covering_another_writer_is_an_error(db: Session, project: Project) -> None:
    """The copy writes Recipe_Active[0..19]; the MOV writes Recipe_Active[5].

    Grouping by operand text makes these two groups of one, so nothing was
    reported while the operator's manual entry was overwritten every scan.
    """
    first = make_ladder(db, project, rung_ordinal=0)
    second = make_rung(db, project, first.routine, ordinal=1)
    _recipe_copy(db, project, first)
    make_instruction(
        db,
        project,
        second,
        "MOV",
        ["Manual_Speed", "Recipe_Active[5]"],
        category=InstructionCategory.MOVE_LOGICAL,
    )

    issue = only(run_check(DestructiveWriteValidator(), db, project), RANGE_DUPLICATE)

    assert issue.severity is ValidationSeverity.ERROR
    assert issue.detail["overlap_proven"] is True
    assert issue.detail["array"] == "recipe_active"
    assert "lost every scan" in issue.message


def test_a_copy_range_that_stops_short_is_not_reported(db: Session, project: Project) -> None:
    """Three elements copied, and the other writer is element 5. No overlap."""
    first = make_ladder(db, project, rung_ordinal=0)
    second = make_rung(db, project, first.routine, ordinal=1)
    _recipe_copy(db, project, first, length="3")
    make_instruction(
        db,
        project,
        second,
        "MOV",
        ["Manual_Speed", "Recipe_Active[5]"],
        category=InstructionCategory.MOVE_LOGICAL,
    )

    assert RANGE_DUPLICATE not in codes(run_check(DestructiveWriteValidator(), db, project))


def test_a_copy_of_unknown_length_is_a_warning(db: Session, project: Project) -> None:
    """A tag length cannot be evaluated here, so the extent was not established."""
    first = make_ladder(db, project, rung_ordinal=0)
    second = make_rung(db, project, first.routine, ordinal=1)
    _recipe_copy(db, project, first, length="Recipe_Length")
    make_instruction(
        db,
        project,
        second,
        "MOV",
        ["Manual_Speed", "Recipe_Active[5]"],
        category=InstructionCategory.MOVE_LOGICAL,
    )

    issue = only(run_check(DestructiveWriteValidator(), db, project), RANGE_DUPLICATE)

    assert issue.severity is ValidationSeverity.WARNING
    assert issue.detail["overlap_proven"] is False
    assert "not established" in issue.message


def test_a_copy_nothing_else_writes_is_not_reported(db: Session, project: Project) -> None:
    rung = make_ladder(db, project)
    _recipe_copy(db, project, rung)

    assert RANGE_DUPLICATE not in codes(run_check(DestructiveWriteValidator(), db, project))


def test_one_copy_reaching_into_another_copy_is_reported(db: Session, project: Project) -> None:
    """Twenty elements from element 0, and three more from element 5.

    Neither is the other's exact destination, so the operand-text grouping sees
    two groups of one — while fifteen of the first copy's elements are written
    twice.
    """
    first = make_ladder(db, project, rung_ordinal=0)
    second = make_rung(db, project, first.routine, ordinal=1)
    _recipe_copy(db, project, first)
    _recipe_copy(db, project, second, destination="Recipe_Active[5]", length="3")

    issue = only(run_check(DestructiveWriteValidator(), db, project), RANGE_DUPLICATE)

    assert issue.severity is ValidationSeverity.ERROR
    assert issue.detail["overlap_proven"] is True


def test_range_overlap_uses_every_write_and_raw_array_element(
    db: Session, project: Project
) -> None:
    """A later unrelated destination cannot hide an earlier range collision.

    Canonical import resolves both array operands to the base declaration, so
    the raw text must retain the two distinct starts.  This also guards against
    reporting the two elements as an exact duplicate destination.
    """
    first = make_ladder(db, project, rung_ordinal=0)
    second = make_rung(db, project, first.routine, ordinal=1)
    third = make_rung(db, project, first.routine, ordinal=2)
    first_copy = _recipe_copy(db, project, first)
    second_copy = _recipe_copy(db, project, second, destination="Recipe_Active[5]", length="3")
    first_copy.operands[1].resolved_name = "Recipe_Active"
    second_copy.operands[1].resolved_name = "Recipe_Active"
    make_instruction(
        db,
        project,
        third,
        "MOV",
        ["Spare_Source", "ZZ_Unrelated"],
        category=InstructionCategory.MOVE_LOGICAL,
    )

    issues = run_check(DestructiveWriteValidator(), db, project)
    issue = only(issues, RANGE_DUPLICATE)

    assert DUPLICATE not in codes(issues)
    assert issue.detail["elements"] == ["Recipe_Active[5]"]
    assert issue.detail["overlaps"] == [
        {
            "range_start": "Recipe_Active[0]",
            "range_mnemonic": "COP",
            "range_location": "MainProgram / MainRoutine / Rung 0",
            "other_destination": "Recipe_Active[5]",
            "other_mnemonic": "COP",
            "other_location": "MainProgram / MainRoutine / Rung 1",
            "overlap_proven": True,
        }
    ]


def test_disjoint_copy_ranges_are_not_reported(db: Session, project: Project) -> None:
    first = make_ladder(db, project, rung_ordinal=0)
    second = make_rung(db, project, first.routine, ordinal=1)
    first_copy = _recipe_copy(db, project, first, length="3")
    second_copy = _recipe_copy(db, project, second, destination="Recipe_Active[5]", length="3")
    first_copy.operands[1].resolved_name = "Recipe_Active"
    second_copy.operands[1].resolved_name = "Recipe_Active"

    issues = run_check(DestructiveWriteValidator(), db, project)

    assert RANGE_DUPLICATE not in codes(issues)
    assert DUPLICATE not in codes(issues)


def test_zero_length_copy_inside_another_range_is_not_reported(
    db: Session, project: Project
) -> None:
    """A zero-length file instruction has a nominal start but writes no element."""
    first = make_ladder(db, project, rung_ordinal=0)
    second = make_rung(db, project, first.routine, ordinal=1)
    _recipe_copy(db, project, first, length="20")
    _recipe_copy(db, project, second, destination="Recipe_Active[5]", length="0")

    issues = run_check(DestructiveWriteValidator(), db, project)

    assert RANGE_DUPLICATE not in codes(issues)


def test_a_synchronous_copy_is_range_checked_too(db: Session, project: Project) -> None:
    first = make_ladder(db, project, rung_ordinal=0)
    second = make_rung(db, project, first.routine, ordinal=1)
    _recipe_copy(db, project, first, mnemonic="CPS")
    make_instruction(
        db,
        project,
        second,
        "MOV",
        ["Manual_Speed", "Recipe_Active[5]"],
        category=InstructionCategory.MOVE_LOGICAL,
    )

    issue = only(run_check(DestructiveWriteValidator(), db, project), RANGE_DUPLICATE)

    assert issue.severity is ValidationSeverity.ERROR


# ---------------------------------------------------------------------------
# JSR return parameters and SBR parameters
# ---------------------------------------------------------------------------


def test_a_jsr_return_parameter_is_a_write(db: Session, project: Project) -> None:
    """``JSR(Scale,1,Raw,Scaled)`` passes Scaled back; rung 12 also writes it.

    Both write the tag and whichever scans last wins. The catalogue records no
    fixed destructive position for JSR because it cannot, so without resolving
    the return parameters from the input count this is a group of one.
    """
    first = make_ladder(db, project, rung_ordinal=0)
    second = make_rung(db, project, first.routine, ordinal=1)
    make_instruction(
        db,
        project,
        first,
        "JSR",
        ["Scale_Routine", "1", "Raw_Count", "Scaled_Value"],
        category=InstructionCategory.PROGRAM_CONTROL,
        literals=[1],
    )
    make_instruction(
        db,
        project,
        second,
        "MOV",
        ["0", "Scaled_Value"],
        category=InstructionCategory.MOVE_LOGICAL,
        literals=[0],
    )

    issue = only(run_check(DestructiveWriteValidator(), db, project), DUPLICATE)

    assert issue.detail["mnemonics"] == ["JSR", "MOV"]
    assert issue.detail["tag_name"] == "Scaled_Value"


def test_a_jsr_input_parameter_is_not_a_write(db: Session, project: Project) -> None:
    """Naming an input as a destination would be a confident error against correct logic."""
    first = make_ladder(db, project, rung_ordinal=0)
    second = make_rung(db, project, first.routine, ordinal=1)
    make_instruction(
        db,
        project,
        first,
        "JSR",
        ["Scale_Routine", "1", "Raw_Count", "Scaled_Value"],
        category=InstructionCategory.PROGRAM_CONTROL,
        literals=[1],
    )
    make_instruction(
        db,
        project,
        second,
        "MOV",
        ["0", "Raw_Count"],
        category=InstructionCategory.MOVE_LOGICAL,
        literals=[0],
    )

    assert DUPLICATE not in codes(run_check(DestructiveWriteValidator(), db, project))


def test_a_jsr_with_a_non_literal_count_claims_nothing(db: Session, project: Project) -> None:
    """Where the input count is a tag, which operands are returns cannot be worked out."""
    first = make_ladder(db, project, rung_ordinal=0)
    second = make_rung(db, project, first.routine, ordinal=1)
    make_instruction(
        db,
        project,
        first,
        "JSR",
        ["Scale_Routine", "Param_Count", "Raw_Count", "Scaled_Value"],
        category=InstructionCategory.PROGRAM_CONTROL,
    )
    make_instruction(
        db,
        project,
        second,
        "MOV",
        ["0", "Scaled_Value"],
        category=InstructionCategory.MOVE_LOGICAL,
        literals=[0],
    )

    assert DUPLICATE not in codes(run_check(DestructiveWriteValidator(), db, project))


def test_an_sbr_parameter_is_a_write(db: Session, project: Project) -> None:
    """Every SBR operand is written when the routine is called."""
    first = make_ladder(db, project, rung_ordinal=0)
    second = make_rung(db, project, first.routine, ordinal=1)
    make_instruction(
        db,
        project,
        first,
        "SBR",
        ["Speed_SP", "Mode"],
        category=InstructionCategory.PROGRAM_CONTROL,
    )
    make_instruction(
        db,
        project,
        second,
        "MOV",
        ["0", "Speed_SP"],
        category=InstructionCategory.MOVE_LOGICAL,
        literals=[0],
    )

    issue = only(run_check(DestructiveWriteValidator(), db, project), DUPLICATE)

    assert issue.detail["mnemonics"] == ["MOV", "SBR"]


# ---------------------------------------------------------------------------
# logic.output_without_driving_rung
# ---------------------------------------------------------------------------
UNDRIVEN_OUTPUT = "logic.output_without_driving_rung"
NO_LADDER = "logic.no_ladder_logic_present"


def _output_tag(db: Session, project: Project, name: str = "Motor_Run_Cmd") -> None:
    device = make_device(db, project)
    signal = make_signal(
        db,
        project,
        device,
        "Run_Cmd",
        signal_type=SignalType.DISCRETE_OUTPUT,
        direction=SignalDirection.OUTPUT,
    )
    make_tag(db, project, name, signal_id=signal.id)


def test_a_field_output_no_rung_writes_is_an_error(db: Session, project: Project) -> None:
    rung = make_ladder(db, project)
    read_tag(db, project, rung, "Some_Permissive")
    _output_tag(db, project)

    issue = only(run_check(OutputCoverageValidator(), db, project), UNDRIVEN_OUTPUT)

    assert issue.severity is ValidationSeverity.ERROR
    assert "can never be energised" in issue.message


def test_a_driven_field_output_does_not_fire(db: Session, project: Project) -> None:
    rung = make_ladder(db, project)
    _output_tag(db, project)
    write_tag(db, project, rung, "Motor_Run_Cmd")

    assert UNDRIVEN_OUTPUT not in codes(run_check(OutputCoverageValidator(), db, project))


def test_an_output_driven_by_a_jsr_return_is_not_undriven(db: Session, project: Project) -> None:
    """The return parameter is what writes the output, and it is a write.

    Reading the catalogue's fixed tuples alone, JSR writes nothing, so the field
    output it drives was reported as one that can never energise — a confident
    ERROR against correct logic, on the tag the whole subroutine exists to set.
    """
    rung = make_ladder(db, project)
    _output_tag(db, project)
    make_instruction(
        db,
        project,
        rung,
        "JSR",
        ["Drive_Motor", "1", "Start_Req", "Motor_Run_Cmd"],
        category=InstructionCategory.PROGRAM_CONTROL,
        literals=[1],
    )

    assert UNDRIVEN_OUTPUT not in codes(run_check(OutputCoverageValidator(), db, project))


def test_no_ladder_means_output_coverage_was_not_examined(db: Session, project: Project) -> None:
    _output_tag(db, project)

    found = run_check(OutputCoverageValidator(), db, project)

    assert only(found, NO_LADDER).severity is ValidationSeverity.INFORMATION
    assert UNDRIVEN_OUTPUT not in codes(found)


# ---------------------------------------------------------------------------
# logic.output_name_mismatch
# ---------------------------------------------------------------------------
NAME_MISMATCH = "logic.output_name_mismatch"


def _wired_output(
    db: Session,
    project: Project,
    name: str = "MTR401_RUN_CMD",
    *,
    address: str = "Local:2:O.Data.0",
) -> Tag:
    """A field output with a terminal behind it, the way an I/O list gives one."""
    device = make_device(db, project)
    signal = make_signal(db, project, device, "Run_Cmd")
    point = make_io_point(db, project, address)
    make_assignment(db, project, signal, point)
    return make_tag(db, project, name, signal_id=signal.id, io_point_id=point.id)


def test_an_output_the_ladder_drives_under_another_name_is_not_a_release_blocker(
    db: Session, project: Project
) -> None:
    """The signal is wired and driven; only the two documents disagree.

    Reported as ``logic.output_without_driving_rung`` this said the output can
    never be energised — false, and at a severity that stopped the release and
    outranked every genuine protection gap in the same run. The terminal is what
    identifies the two names as one signal, because the names themselves do not
    normalise together and no comparison of them ever will.
    """
    rung = make_ladder(db, project)
    _wired_output(db, project)
    make_tag(
        db,
        project,
        "CV401_Run_Output",
        description="CV401 infeed drive run command. Wired to DO_401 channel 0 (Local:2:O.Data.0).",
    )
    write_tag(db, project, rung, "CV401_Run_Output")

    found = run_check(OutputCoverageValidator(), db, project)
    issue = only(found, NAME_MISMATCH)

    assert UNDRIVEN_OUTPUT not in codes(found)
    assert issue.severity is ValidationSeverity.WARNING
    # An engineer cannot settle a name disagreement without both names in front
    # of them, and cannot find either without the terminal.
    assert "MTR401_RUN_CMD" in issue.message
    assert "CV401_Run_Output" in issue.message
    assert "Local:2:O.Data.0" in issue.message
    assert issue.detail["driven_by"] == ["CV401_Run_Output"]
    assert issue.detail["established_from"] == "description"


def test_an_alias_to_the_same_terminal_is_the_stronger_evidence(
    db: Session, project: Project
) -> None:
    """An alias target is resolved by the controller, not written down by a person.

    Both routes have to work: aliasing the terminal is the ordinary way a Logix
    project ties a tag to a channel, and a base tag whose description names the
    address is the ordinary way an export explains itself. A check that read only
    one of them would keep raising a dead-output error against the other.
    """
    rung = make_ladder(db, project)
    _wired_output(db, project)
    make_tag(db, project, "CV401_Run_Output", is_alias=True, alias_for="Local:2:O.Data.0")
    write_tag(db, project, rung, "CV401_Run_Output")

    issue = only(run_check(OutputCoverageValidator(), db, project), NAME_MISMATCH)

    assert issue.detail["established_from"] == "alias"


def test_a_second_tag_on_the_same_io_point_is_a_name_disagreement(
    db: Session, project: Project
) -> None:
    """One terminal, two tag rows, one of them driven. Nothing is dead here."""
    rung = make_ladder(db, project)
    output = _wired_output(db, project)
    make_tag(db, project, "CV401_Run_Output", io_point_id=output.io_point_id)
    write_tag(db, project, rung, "CV401_Run_Output")

    issue = only(run_check(OutputCoverageValidator(), db, project), NAME_MISMATCH)

    assert issue.detail["established_from"] == "binding"


def test_a_description_ending_in_the_address_still_names_the_terminal(
    db: Session, project: Project
) -> None:
    """The full stop that ends the sentence is not part of the address.

    Rejected, the same signal would be reported as an output that can never
    energise on nothing more than where the author put a period.
    """
    rung = make_ladder(db, project)
    _wired_output(db, project)
    make_tag(db, project, "CV401_Run_Output", description="Run command, wired to Local:2:O.Data.0.")
    write_tag(db, project, rung, "CV401_Run_Output")

    assert NAME_MISMATCH in codes(run_check(OutputCoverageValidator(), db, project))


def test_a_neighbouring_channel_is_not_the_same_terminal(db: Session, project: Project) -> None:
    """``Local:2:O.Data.1`` occurs inside ``Local:2:O.Data.10``.

    The output on channel 1 really is driven by nothing, and excusing it against
    the tag that drives channel 10 would lose a dead terminal *and* name the
    wrong two tags while doing it.
    """
    rung = make_ladder(db, project)
    _wired_output(db, project, address="Local:2:O.Data.1")
    make_tag(db, project, "CV410_Run_Output", description="Wired to Local:2:O.Data.10.")
    write_tag(db, project, rung, "CV410_Run_Output")

    found = run_check(OutputCoverageValidator(), db, project)

    assert only(found, UNDRIVEN_OUTPUT).severity is ValidationSeverity.ERROR
    assert NAME_MISMATCH not in codes(found)


def test_an_output_with_no_terminal_recorded_stays_an_error(db: Session, project: Project) -> None:
    """With no address, nothing pairs the two names and the error stands.

    This is the case the downgrade must not reach: a field output whose signal
    nobody has assigned an address to has no terminal to compare, so "driven
    under another name" cannot be established and is not claimed.
    """
    rung = make_ladder(db, project)
    _output_tag(db, project, "MTR401_RUN_CMD")
    make_tag(db, project, "CV401_Run_Output", description="Wired to Local:2:O.Data.0.")
    write_tag(db, project, rung, "CV401_Run_Output")

    found = run_check(OutputCoverageValidator(), db, project)

    assert only(found, UNDRIVEN_OUTPUT).severity is ValidationSeverity.ERROR
    assert NAME_MISMATCH not in codes(found)


def test_a_tag_naming_the_terminal_that_no_rung_writes_does_not_excuse_it(
    db: Session, project: Project
) -> None:
    """The other name has to be one the ladder actually drives.

    Two undriven tags on one terminal are still an output nothing energises, and
    reading the second as a driver would turn a dead output into a note about
    spelling.
    """
    rung = make_ladder(db, project)
    _wired_output(db, project)
    make_tag(db, project, "CV401_Run_Output", description="Wired to Local:2:O.Data.0.")
    read_tag(db, project, rung, "CV401_Run_Output")

    found = run_check(OutputCoverageValidator(), db, project)

    assert only(found, UNDRIVEN_OUTPUT).severity is ValidationSeverity.ERROR
    assert NAME_MISMATCH not in codes(found)


# ---------------------------------------------------------------------------
# logic.alarm_trigger_never_written
# ---------------------------------------------------------------------------
UNDRIVEN_ALARM = "logic.alarm_trigger_never_written"


def test_an_alarm_trigger_no_rung_writes_is_an_error(db: Session, project: Project) -> None:
    rung = make_ladder(db, project)
    read_tag(db, project, rung, "Something_Else")
    make_alarm(db, project, "ALM-004", trigger_tag_name="ALM_HOPPER_HIGH")

    issue = only(run_check(OutputCoverageValidator(), db, project), UNDRIVEN_ALARM)

    assert issue.severity is ValidationSeverity.ERROR
    assert "can never be raised" in issue.message


def test_a_written_alarm_trigger_does_not_fire(db: Session, project: Project) -> None:
    rung = make_ladder(db, project)
    write_tag(db, project, rung, "ALM_HOPPER_HIGH")
    make_alarm(db, project, "ALM-004", trigger_tag_name="ALM_HOPPER_HIGH")

    assert UNDRIVEN_ALARM not in codes(run_check(OutputCoverageValidator(), db, project))


def test_an_alarm_on_a_hardwired_input_does_not_fire(db: Session, project: Project) -> None:
    """An input bit is driven by the module, not by a rung."""
    rung = make_ladder(db, project)
    read_tag(db, project, rung, "Something_Else")
    point = make_io_point(
        db,
        project,
        "Local:1:I.Data.3",
        signal_type=SignalType.DISCRETE_INPUT,
        direction=SignalDirection.INPUT,
    )
    make_tag(db, project, "Estop_Tripped", io_point_id=point.id)
    make_alarm(db, project, "ALM-005", trigger_tag_name="Estop_Tripped")

    assert UNDRIVEN_ALARM not in codes(run_check(OutputCoverageValidator(), db, project))


def test_a_fault_trigger_is_checked_too(db: Session, project: Project) -> None:
    rung = make_ladder(db, project)
    read_tag(db, project, rung, "Something_Else")
    make_fault(db, project, "Low suction", trigger_tag_name="FLT_LOW_SUCTION")

    issue = only(run_check(OutputCoverageValidator(), db, project), UNDRIVEN_ALARM)

    assert issue.object_type is ObjectType.FAULT_DEFINITION


# ---------------------------------------------------------------------------
# logic.latched_output_without_unlatch
# ---------------------------------------------------------------------------
NO_UNLATCH = "logic.latched_output_without_unlatch"


def test_a_latch_with_no_unlatch_anywhere_is_an_error(db: Session, project: Project) -> None:
    rung = make_ladder(db, project)
    write_tag(db, project, rung, "Fault_Latched", mnemonic="OTL")

    issue = only(run_check(OutputCoverageValidator(), db, project), NO_UNLATCH)

    assert issue.severity is ValidationSeverity.ERROR


def test_a_latch_with_its_unlatch_does_not_fire(db: Session, project: Project) -> None:
    first = make_ladder(db, project, rung_ordinal=0)
    second = make_rung(db, project, first.routine, ordinal=1)
    write_tag(db, project, first, "Fault_Latched", mnemonic="OTL")
    write_tag(db, project, second, "Fault_Latched", mnemonic="OTU")

    assert NO_UNLATCH not in codes(run_check(OutputCoverageValidator(), db, project))


# ---------------------------------------------------------------------------
# logic.jsr_target_missing / logic.uncalled_routine
# ---------------------------------------------------------------------------
JSR_MISSING = "logic.jsr_target_missing"
UNCALLED = "logic.uncalled_routine"


def test_a_jsr_to_a_routine_that_does_not_exist_is_an_error(db: Session, project: Project) -> None:
    rung = make_ladder(db, project)
    make_instruction(
        db,
        project,
        rung,
        "JSR",
        ["Conveyor_Interlocks", "0"],
        category=InstructionCategory.PROGRAM_CONTROL,
        literals=[1],
    )

    issue = only(run_check(RoutineCallValidator(), db, project), JSR_MISSING)

    assert issue.severity is ValidationSeverity.ERROR
    assert issue.detail["target_routine"] == "Conveyor_Interlocks"


def test_a_jsr_to_an_existing_routine_does_not_fire(db: Session, project: Project) -> None:
    rung = make_ladder(db, project)
    make_routine(db, project, rung.routine.program, "Conveyor_Interlocks", ordinal=1, is_main=False)
    make_instruction(
        db,
        project,
        rung,
        "JSR",
        ["Conveyor_Interlocks", "0"],
        category=InstructionCategory.PROGRAM_CONTROL,
        literals=[1],
    )

    found = codes(run_check(RoutineCallValidator(), db, project))
    assert JSR_MISSING not in found
    assert UNCALLED not in found


def test_a_routine_nothing_calls_is_a_warning(db: Session, project: Project) -> None:
    rung = make_ladder(db, project)
    read_tag(db, project, rung, "Motor_Run_Cmd")
    make_routine(db, project, rung.routine.program, "Orphan_Routine", ordinal=1, is_main=False)

    issue = only(run_check(RoutineCallValidator(), db, project), UNCALLED)

    assert issue.severity is ValidationSeverity.WARNING
    assert issue.object_label == "MainProgram / Orphan_Routine"


def test_a_program_main_routine_is_not_uncalled(db: Session, project: Project) -> None:
    """The controller calls the program's main routine without a JSR."""
    program = make_program(db, project, main_routine_name="Startup")
    routine = make_routine(db, project, program, "Startup", is_main=False)
    rung = make_rung(db, project, routine)
    read_tag(db, project, rung, "Motor_Run_Cmd")

    assert UNCALLED not in codes(run_check(RoutineCallValidator(), db, project))


# ---------------------------------------------------------------------------
# logic instruction support
# ---------------------------------------------------------------------------
RESTRICTED = "logic.restricted_instruction"
UNKNOWN = "logic.unknown_instruction"
UNSUPPORTED_FOR_FAMILY = "logic.unsupported_instruction"
HUMAN_REVIEW = "logic.instruction_requires_human_review"
FAMILY_UNKNOWN = "logic.controller_family_unknown"
NON_LADDER = "logic.non_ladder_routine_unchecked"


def test_a_restricted_instruction_needs_a_human_decision(db: Session, project: Project) -> None:
    make_target_platform(db, project, controller_family=ControllerFamily.CONTROLLOGIX)
    rung = make_ladder(db, project)
    make_instruction(db, project, rung, "MAM", ["Axis_1"], category=InstructionCategory.SPECIAL)

    issue = only(run_check(InstructionSupportValidator(), db, project), RESTRICTED)

    assert issue.severity is ValidationSeverity.UNSUPPORTED
    assert issue.requires_human_decision is True
    assert "Motion" in issue.message


def test_a_direct_drive_command_is_restricted_not_unknown(db: Session, project: Project) -> None:
    """MDO commands a servo drive with no profile at all.

    Absent from the nine-mnemonic motion list, it came back "not described by
    this platform's instruction catalogue" at UNSUPPORTED with no human decision
    required, and the suggested action told the engineer to supply an Add-On
    Instruction definition.
    """
    make_target_platform(db, project, controller_family=ControllerFamily.CONTROLLOGIX)
    rung = make_ladder(db, project)
    make_instruction(
        db,
        project,
        rung,
        "MDO",
        ["Axis_1", "Motion_Ctrl_1", "50.0", "0"],
        category=InstructionCategory.SPECIAL,
        literals=[2, 3],
    )

    found = run_check(InstructionSupportValidator(), db, project)
    issue = only(found, RESTRICTED)

    assert issue.requires_human_decision is True
    assert UNKNOWN not in codes(found)


def test_a_motion_shaped_mnemonic_nobody_listed_is_still_restricted(
    db: Session, project: Project
) -> None:
    """The Logix motion set outgrows the list; the shape is the backstop."""
    make_target_platform(db, project, controller_family=ControllerFamily.CONTROLLOGIX)
    rung = make_ladder(db, project)
    make_instruction(db, project, rung, "MDCC", ["Axis_1"], category=InstructionCategory.SPECIAL)

    issue = only(run_check(InstructionSupportValidator(), db, project), RESTRICTED)

    assert issue.requires_human_decision is True
    assert "by its mnemonic rather than by name" in issue.message


def test_setting_a_system_value_is_restricted(db: Session, project: Project) -> None:
    """``SSV(Program,Prog_Purge,Disabled,One)`` disables a whole program at runtime.

    Catalogued as neither known nor restricted, it was reported as a name the
    platform had never heard of rather than as a rung that reconfigures the
    controller.
    """
    make_target_platform(db, project, controller_family=ControllerFamily.CONTROLLOGIX)
    rung = make_ladder(db, project)
    make_instruction(
        db,
        project,
        rung,
        "SSV",
        ["Program", "Prog_Purge", "Disabled", "One"],
        category=InstructionCategory.SPECIAL,
        literals=[0, 1, 2],
    )

    found = run_check(InstructionSupportValidator(), db, project)
    issue = only(found, RESTRICTED)

    assert issue.requires_human_decision is True
    assert UNKNOWN not in codes(found)


def test_a_catalogued_instruction_is_not_restricted(db: Session, project: Project) -> None:
    make_target_platform(db, project, controller_family=ControllerFamily.CONTROLLOGIX)
    rung = make_ladder(db, project)
    read_tag(db, project, rung, "Motor_Run_Cmd")

    found = codes(run_check(InstructionSupportValidator(), db, project))
    assert RESTRICTED not in found
    assert UNKNOWN not in found
    assert UNSUPPORTED_FOR_FAMILY not in found


def test_an_uncatalogued_mnemonic_is_reported_by_name(db: Session, project: Project) -> None:
    make_target_platform(db, project, controller_family=ControllerFamily.CONTROLLOGIX)
    rung = make_ladder(db, project)
    make_instruction(
        db,
        project,
        rung,
        "ESCO_Motor",
        ["MTR_101"],
        category=InstructionCategory.ADDON_INSTRUCTION,
        destinations=[0],
    )

    issue = only(run_check(InstructionSupportValidator(), db, project), UNKNOWN)

    assert issue.severity is ValidationSeverity.UNSUPPORTED
    assert "ESCO_MOTOR" in issue.message


def test_a_legacy_instruction_is_unsupported_for_a_logix_target(
    db: Session, project: Project
) -> None:
    make_target_platform(db, project, controller_family=ControllerFamily.CONTROLLOGIX)
    rung = make_ladder(db, project)
    make_instruction(
        db,
        project,
        rung,
        "SCL",
        ["Raw", "1.0", "0.0", "Scaled"],
        category=InstructionCategory.COMPUTE_MATH,
        literals=[1, 2],
    )

    issue = only(run_check(InstructionSupportValidator(), db, project), UNSUPPORTED_FOR_FAMILY)

    assert issue.severity is ValidationSeverity.UNSUPPORTED
    assert issue.detail["controller_family"] == ControllerFamily.CONTROLLOGIX.value


def test_an_instruction_the_catalogue_flags_goes_to_a_human(db: Session, project: Project) -> None:
    make_target_platform(db, project, controller_family=ControllerFamily.CONTROLLOGIX)
    rung = make_ladder(db, project)
    write_tag(db, project, rung, "Fault_Latched", mnemonic="OTL")

    issue = only(run_check(InstructionSupportValidator(), db, project), HUMAN_REVIEW)

    assert issue.severity is ValidationSeverity.HUMAN_REVIEW_REQUIRED
    assert issue.requires_human_decision is True


def test_no_target_platform_means_support_was_not_checked(db: Session, project: Project) -> None:
    rung = make_ladder(db, project)
    read_tag(db, project, rung, "Motor_Run_Cmd")

    found = run_check(InstructionSupportValidator(), db, project)

    assert only(found, FAMILY_UNKNOWN).severity is ValidationSeverity.UNSUPPORTED


def test_a_known_target_platform_removes_the_caveat(db: Session, project: Project) -> None:
    make_target_platform(db, project, controller_family=ControllerFamily.CONTROLLOGIX)
    rung = make_ladder(db, project)
    read_tag(db, project, rung, "Motor_Run_Cmd")

    assert FAMILY_UNKNOWN not in codes(run_check(InstructionSupportValidator(), db, project))


def test_a_structured_text_routine_is_reported_as_unexamined(db: Session, project: Project) -> None:
    program = make_program(db, project)
    make_routine(db, project, program, "Scaling", language=RoutineLanguage.STRUCTURED_TEXT)

    issue = only(run_check(InstructionSupportValidator(), db, project), NON_LADDER)

    assert issue.severity is ValidationSeverity.UNSUPPORTED
    assert "structured text" in issue.message


def test_a_ladder_routine_carries_no_such_caveat(db: Session, project: Project) -> None:
    program = make_program(db, project)
    make_routine(db, project, program, "MainRoutine")

    assert NON_LADDER not in codes(run_check(InstructionSupportValidator(), db, project))


# ---------------------------------------------------------------------------
# logic mode conflicts
# ---------------------------------------------------------------------------
TWO_DEFAULTS = "logic.multiple_default_modes"
MODE_CONFLICT = "logic.conflicting_mode_selection"


def test_two_default_modes_is_an_error(db: Session, project: Project) -> None:
    make_mode(db, project, "AUTO", ordinal=0, is_default=True)
    make_mode(db, project, "MANUAL", ordinal=1, is_default=True)

    issue = only(run_check(ModeLogicValidator(), db, project), TWO_DEFAULTS)

    assert issue.severity is ValidationSeverity.ERROR
    assert issue.detail["modes"] == ["AUTO", "MANUAL"]


def test_one_default_mode_does_not_fire(db: Session, project: Project) -> None:
    make_mode(db, project, "AUTO", ordinal=0, is_default=True)
    make_mode(db, project, "MANUAL", ordinal=1, is_default=False)

    assert TWO_DEFAULTS not in codes(run_check(ModeLogicValidator(), db, project))


def test_two_modes_on_one_selector_value_is_an_error(db: Session, project: Project) -> None:
    make_mode(db, project, "AUTO", ordinal=0, selection_tag_name="Mode_Sel", selection_value="1")
    make_mode(db, project, "CLEAN", ordinal=1, selection_tag_name="Mode_Sel", selection_value="1")

    issue = only(run_check(ModeLogicValidator(), db, project), MODE_CONFLICT)

    assert issue.severity is ValidationSeverity.ERROR
    assert issue.detail["modes"] == ["AUTO", "CLEAN"]


def test_distinct_selector_values_do_not_fire(db: Session, project: Project) -> None:
    make_mode(db, project, "AUTO", ordinal=0, selection_tag_name="Mode_Sel", selection_value="1")
    make_mode(db, project, "CLEAN", ordinal=1, selection_tag_name="Mode_Sel", selection_value="2")

    assert MODE_CONFLICT not in codes(run_check(ModeLogicValidator(), db, project))


# ---------------------------------------------------------------------------
# logic interlock and permissive implementation
# ---------------------------------------------------------------------------
NOT_IMPLEMENTED = "logic.interlock_not_implemented"
UNVERIFIABLE = "logic.interlock_implementation_unverifiable"
PERMISSIVE_MISSING = "logic.permissive_not_implemented"


def test_a_documented_interlock_with_no_logic_is_an_error(db: Session, project: Project) -> None:
    make_interlock(
        db,
        project,
        "ILK-003",
        source_reference="Interlock_Schedule.xlsx / Interlocks / row 5",
        is_implemented=False,
    )

    issue = only(run_check(InterlockImplementationValidator(), db, project), NOT_IMPLEMENTED)

    assert issue.severity is ValidationSeverity.ERROR
    assert "ILK-003" in issue.object_label


def test_an_implemented_interlock_does_not_fire(db: Session, project: Project) -> None:
    rung = make_ladder(db, project)
    make_interlock(
        db,
        project,
        "ILK-001",
        source_reference="Interlock_Schedule.xlsx / Interlocks / row 3",
        is_implemented=True,
        implementing_rung_id=rung.id,
    )

    found = codes(run_check(InterlockImplementationValidator(), db, project))
    assert NOT_IMPLEMENTED not in found
    assert UNVERIFIABLE not in found


def test_an_undocumented_interlock_is_not_compared_against_nothing(
    db: Session, project: Project
) -> None:
    """With no source and no requirement, there is no documented claim to check."""
    make_interlock(db, project, "ILK-009", is_implemented=False)

    assert NOT_IMPLEMENTED not in codes(run_check(InterlockImplementationValidator(), db, project))


def test_an_implementation_claim_with_no_rung_is_an_error(db: Session, project: Project) -> None:
    make_interlock(
        db,
        project,
        "ILK-004",
        source_reference="Interlock_Schedule.xlsx / row 6",
        is_implemented=True,
        implementing_rung_id=None,
    )

    issue = only(run_check(InterlockImplementationValidator(), db, project), UNVERIFIABLE)

    assert issue.severity is ValidationSeverity.ERROR


def test_an_unimplemented_permissive_is_an_error(db: Session, project: Project) -> None:
    make_permissive(db, project, "Guard door closed")

    issue = only(run_check(InterlockImplementationValidator(), db, project), PERMISSIVE_MISSING)

    assert issue.severity is ValidationSeverity.ERROR


def test_an_implemented_permissive_does_not_fire(db: Session, project: Project) -> None:
    rung = make_ladder(db, project)
    make_permissive(
        db,
        project,
        "Guard door closed",
        is_implemented=True,
        implementing_rung_id=rung.id,
    )

    assert PERMISSIVE_MISSING not in codes(
        run_check(InterlockImplementationValidator(), db, project)
    )


# ---------------------------------------------------------------------------
# logic.latched_fault_without_reset
# ---------------------------------------------------------------------------
NO_RESET = "logic.latched_fault_without_reset"


def test_a_latched_alarm_with_no_reset_behaviour_is_an_error(db: Session, project: Project) -> None:
    make_alarm(db, project, "ALM-010", latched=True, severity=AlarmSeverity.URGENT)

    issue = only(run_check(ResetBehaviourValidator(), db, project), NO_RESET)

    assert issue.severity is ValidationSeverity.ERROR
    assert "Nothing states how it is cleared" in issue.message


def test_a_latched_alarm_whose_reset_is_undefined_is_an_error(
    db: Session, project: Project
) -> None:
    behaviour = make_reset_behavior(db, project, kind=ResetBehaviorKind.UNDEFINED)
    fault = make_fault(db, project, reset_behavior_id=behaviour.id)
    make_alarm(db, project, "ALM-011", latched=True, fault_definition_id=fault.id)

    assert NO_RESET in codes(run_check(ResetBehaviourValidator(), db, project))


def test_a_latched_alarm_with_a_stated_reset_does_not_fire(db: Session, project: Project) -> None:
    behaviour = make_reset_behavior(db, project, kind=ResetBehaviorKind.MANUAL_LOCAL)
    fault = make_fault(db, project, reset_behavior_id=behaviour.id)
    make_alarm(db, project, "ALM-012", latched=True, fault_definition_id=fault.id)

    assert NO_RESET not in codes(run_check(ResetBehaviourValidator(), db, project))


def test_an_unlatched_alarm_needs_no_reset_behaviour(db: Session, project: Project) -> None:
    make_alarm(db, project, "ALM-013", latched=False)

    assert NO_RESET not in codes(run_check(ResetBehaviourValidator(), db, project))


# ---------------------------------------------------------------------------
# logic.unreachable_state
# ---------------------------------------------------------------------------
UNREACHABLE = "logic.unreachable_state"
NO_TRANSITIONS = "logic.state_transitions_absent"


def test_a_state_nothing_enters_is_an_error(db: Session, project: Project) -> None:
    idle = make_state(db, project, "IDLE", ordinal=0, is_initial=True)
    running = make_state(db, project, "RUNNING", ordinal=1)
    make_state(db, project, "CLEANING", ordinal=2)
    make_transition(db, project, idle, running)

    issue = only(run_check(SequenceStateValidator(), db, project), UNREACHABLE)

    assert issue.severity is ValidationSeverity.ERROR
    assert "CLEANING" in issue.message


def test_a_state_with_an_incoming_transition_does_not_fire(db: Session, project: Project) -> None:
    idle = make_state(db, project, "IDLE", ordinal=0, is_initial=True)
    running = make_state(db, project, "RUNNING", ordinal=1)
    make_transition(db, project, idle, running)

    assert UNREACHABLE not in codes(run_check(SequenceStateValidator(), db, project))


def test_the_initial_state_needs_no_incoming_transition(db: Session, project: Project) -> None:
    idle = make_state(db, project, "IDLE", ordinal=0, is_initial=True)
    running = make_state(db, project, "RUNNING", ordinal=1)
    make_transition(db, project, idle, running)

    issues = run_check(SequenceStateValidator(), db, project)
    assert all(issue.detail.get("state") != "IDLE" for issue in issues)


def test_states_with_no_transitions_at_all_are_not_judged(db: Session, project: Project) -> None:
    make_state(db, project, "IDLE", ordinal=0, is_initial=True)
    make_state(db, project, "RUNNING", ordinal=1)

    found = run_check(SequenceStateValidator(), db, project)

    assert only(found, NO_TRANSITIONS).severity is ValidationSeverity.UNSUPPORTED
    assert UNREACHABLE not in codes(found)
