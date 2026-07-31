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
from app.models.project import Project
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


def test_no_ladder_means_output_coverage_was_not_examined(db: Session, project: Project) -> None:
    _output_tag(db, project)

    found = run_check(OutputCoverageValidator(), db, project)

    assert only(found, NO_LADDER).severity is ValidationSeverity.INFORMATION
    assert UNDRIVEN_OUTPUT not in codes(found)


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
