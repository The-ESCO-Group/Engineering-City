"""The tag validators.

The negative cases carry the weight. A tag validator that fires on correct names
is worse than no tag validator: the list is long, an engineer reads it once, and
after the first false positive they stop reading it — including the duplicate
that would have cost them a day at site.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.enums import BaseDataType, TagRole, TagScope, ValidationSeverity
from app.models.project import Project
from app.validation.tags import (
    MAX_TAG_NAME_CHARS,
    TagAliasValidator,
    TagDocumentationValidator,
    TagNameValidator,
    TagUsageValidator,
)
from tests.validation import (
    codes,
    make_aoi_reference,
    make_binding,
    make_ladder,
    make_program,
    make_screen,
    make_tag,
    make_tag_alias,
    only,
    read_tag,
    run_check,
)

# ---------------------------------------------------------------------------
# tags.invalid_identifier
# ---------------------------------------------------------------------------
INVALID = "tags.invalid_identifier"


def test_name_with_a_space_is_an_error(db: Session, project: Project) -> None:
    make_tag(db, project, "Motor Run Cmd")

    issue = only(run_check(TagNameValidator(), db, project), INVALID)

    assert issue.severity is ValidationSeverity.ERROR
    assert "Motor Run Cmd" in issue.message


def test_name_starting_with_a_digit_is_an_error(db: Session, project: Project) -> None:
    make_tag(db, project, "101_Motor_Run")

    assert INVALID in codes(run_check(TagNameValidator(), db, project))


def test_trailing_underscore_is_an_error(db: Session, project: Project) -> None:
    make_tag(db, project, "Motor_Run_")

    issue = only(run_check(TagNameValidator(), db, project), INVALID)

    assert "underscore" in issue.message


def test_double_underscore_is_an_error(db: Session, project: Project) -> None:
    make_tag(db, project, "Motor__Run")

    assert INVALID in codes(run_check(TagNameValidator(), db, project))


def test_a_legal_name_does_not_fire(db: Session, project: Project) -> None:
    make_tag(db, project, "_MTR_101_Run_Cmd_2")

    assert INVALID not in codes(run_check(TagNameValidator(), db, project))


# ---------------------------------------------------------------------------
# tags.reserved_word
# ---------------------------------------------------------------------------
RESERVED = "tags.reserved_word"


def test_reserved_word_is_an_error(db: Session, project: Project) -> None:
    make_tag(db, project, "Timer")

    issue = only(run_check(TagNameValidator(), db, project), RESERVED)

    assert issue.severity is ValidationSeverity.ERROR


def test_a_name_merely_containing_a_reserved_word_does_not_fire(
    db: Session, project: Project
) -> None:
    make_tag(db, project, "Dwell_Timer_Done")

    assert RESERVED not in codes(run_check(TagNameValidator(), db, project))


# ---------------------------------------------------------------------------
# tags.name_too_long
# ---------------------------------------------------------------------------
TOO_LONG = "tags.name_too_long"


def test_name_over_forty_characters_is_an_error(db: Session, project: Project) -> None:
    make_tag(db, project, "A" * (MAX_TAG_NAME_CHARS + 1))

    issue = only(run_check(TagNameValidator(), db, project), TOO_LONG)

    assert issue.severity is ValidationSeverity.ERROR
    assert issue.detail["length"] == MAX_TAG_NAME_CHARS + 1


def test_name_of_exactly_forty_characters_does_not_fire(db: Session, project: Project) -> None:
    make_tag(db, project, "A" * MAX_TAG_NAME_CHARS)

    assert TOO_LONG not in codes(run_check(TagNameValidator(), db, project))


# ---------------------------------------------------------------------------
# tags.duplicate_name
# ---------------------------------------------------------------------------
DUPLICATE = "tags.duplicate_name"


def test_two_controller_tags_of_one_name_are_an_error(db: Session, project: Project) -> None:
    make_tag(db, project, "Motor_Run_Cmd")
    make_tag(db, project, "motor_run_cmd")

    issues = [
        issue for issue in run_check(TagNameValidator(), db, project) if issue.code == DUPLICATE
    ]

    assert len(issues) == 2, "both declarations should be named"
    assert all(issue.severity is ValidationSeverity.ERROR for issue in issues)


def test_the_same_name_in_two_scopes_is_not_a_duplicate(db: Session, project: Project) -> None:
    """Logix identity is (scope, name); this is shadowing, reported separately."""
    program = make_program(db, project)
    make_tag(db, project, "Motor_Run_Cmd")
    make_tag(
        db,
        project,
        "Motor_Run_Cmd",
        scope=TagScope.PROGRAM,
        scope_program_id=program.id,
    )

    assert DUPLICATE not in codes(run_check(TagNameValidator(), db, project))


# ---------------------------------------------------------------------------
# tags.scope_collision
# ---------------------------------------------------------------------------
COLLISION = "tags.scope_collision"


def test_program_tag_shadowing_a_controller_tag_is_a_warning(db: Session, project: Project) -> None:
    program = make_program(db, project)
    make_tag(db, project, "Motor_Run_Cmd")
    make_tag(
        db,
        project,
        "Motor_Run_Cmd",
        scope=TagScope.PROGRAM,
        scope_program_id=program.id,
    )

    issue = only(run_check(TagNameValidator(), db, project), COLLISION)

    assert issue.severity is ValidationSeverity.WARNING
    assert issue.object_label.startswith("MainProgram / ")


def test_a_program_tag_with_a_unique_name_does_not_fire(db: Session, project: Project) -> None:
    program = make_program(db, project)
    make_tag(db, project, "Controller_Level_Tag")
    make_tag(
        db,
        project,
        "Program_Level_Tag",
        scope=TagScope.PROGRAM,
        scope_program_id=program.id,
    )

    assert COLLISION not in codes(run_check(TagNameValidator(), db, project))


# ---------------------------------------------------------------------------
# tags.missing_description / tags.missing_units
# ---------------------------------------------------------------------------
NO_DESCRIPTION = "tags.missing_description"
NO_UNITS = "tags.missing_units"


def test_undescribed_tag_is_a_warning_not_a_blocker(db: Session, project: Project) -> None:
    make_tag(db, project, "Motor_Run_Cmd", description="")

    issue = only(run_check(TagDocumentationValidator(), db, project), NO_DESCRIPTION)

    assert issue.severity is ValidationSeverity.WARNING


def test_described_tag_does_not_fire(db: Session, project: Project) -> None:
    make_tag(db, project, "Motor_Run_Cmd", description="Run command.")

    assert NO_DESCRIPTION not in codes(run_check(TagDocumentationValidator(), db, project))


def test_analog_value_without_units_is_a_warning(db: Session, project: Project) -> None:
    make_tag(
        db,
        project,
        "Tank_Level_PV",
        role=TagRole.ANALOG_PROCESS_VALUE,
        base_data_type=BaseDataType.REAL,
        engineering_units="",
    )

    issue = only(run_check(TagDocumentationValidator(), db, project), NO_UNITS)

    assert issue.severity is ValidationSeverity.WARNING
    assert "engineering units" in issue.message


def test_analog_value_with_units_does_not_fire(db: Session, project: Project) -> None:
    make_tag(
        db,
        project,
        "Tank_Level_PV",
        role=TagRole.ANALOG_PROCESS_VALUE,
        base_data_type=BaseDataType.REAL,
        engineering_units="%",
    )

    assert NO_UNITS not in codes(run_check(TagDocumentationValidator(), db, project))


def test_a_boolean_setpoint_is_not_expected_to_carry_units(db: Session, project: Project) -> None:
    """Reporting a missing unit on a BOOL would be inventing a requirement."""
    make_tag(
        db,
        project,
        "Recipe_Enable_SP",
        role=TagRole.SETPOINT,
        base_data_type=BaseDataType.BOOL,
        engineering_units="",
    )

    assert NO_UNITS not in codes(run_check(TagDocumentationValidator(), db, project))


# ---------------------------------------------------------------------------
# tags.stale_alias and friends
# ---------------------------------------------------------------------------
STALE = "tags.stale_alias"
NO_TARGET = "tags.alias_target_missing"
MODULE_ALIAS = "tags.unresolved_module_alias"
TYPE_MISMATCH = "tags.alias_type_mismatch"


def test_alias_to_a_missing_symbolic_tag_is_an_error(db: Session, project: Project) -> None:
    alias = make_tag(db, project, "Motor_Running", is_alias=True, alias_for="Deleted_Base_Tag")
    make_tag_alias(
        db,
        project,
        alias,
        target_expression="Deleted_Base_Tag",
        is_resolved=False,
        resolution_note="No tag of that name exists.",
    )

    issue = only(run_check(TagAliasValidator(), db, project), STALE)

    assert issue.severity is ValidationSeverity.ERROR
    assert "Deleted_Base_Tag" in issue.message


def test_a_resolved_alias_does_not_fire(db: Session, project: Project) -> None:
    target = make_tag(db, project, "Base_Tag", base_data_type=BaseDataType.BOOL)
    alias = make_tag(
        db,
        project,
        "Motor_Running",
        is_alias=True,
        alias_for="Base_Tag",
        base_data_type=BaseDataType.BOOL,
    )
    make_tag_alias(
        db,
        project,
        alias,
        target_expression="Base_Tag",
        target_tag_id=target.id,
        is_resolved=True,
    )

    found = codes(run_check(TagAliasValidator(), db, project))
    assert STALE not in found
    assert TYPE_MISMATCH not in found


def test_an_unresolved_module_address_is_reported_as_unchecked(
    db: Session, project: Project
) -> None:
    """The tag table cannot resolve an I/O address, so it must not claim to."""
    alias = make_tag(db, project, "Estop_OK", is_alias=True, alias_for="Local:2:I.Data.3")
    make_tag_alias(db, project, alias, target_expression="Local:2:I.Data.3", is_resolved=False)

    issue = only(run_check(TagAliasValidator(), db, project), MODULE_ALIAS)

    assert issue.severity is ValidationSeverity.UNSUPPORTED
    assert STALE not in codes(run_check(TagAliasValidator(), db, project))


def test_alias_naming_nothing_at_all_is_an_error(db: Session, project: Project) -> None:
    make_tag(db, project, "Motor_Running", is_alias=True, alias_for="")

    issue = only(run_check(TagAliasValidator(), db, project), NO_TARGET)

    assert issue.severity is ValidationSeverity.ERROR


def test_alias_type_disagreeing_with_its_target_is_an_error(db: Session, project: Project) -> None:
    target = make_tag(db, project, "Base_Word", base_data_type=BaseDataType.DINT)
    alias = make_tag(
        db,
        project,
        "Motor_Running",
        is_alias=True,
        alias_for="Base_Word",
        base_data_type=BaseDataType.BOOL,
    )
    make_tag_alias(
        db,
        project,
        alias,
        target_expression="Base_Word",
        target_tag_id=target.id,
        is_resolved=True,
    )

    issue = only(run_check(TagAliasValidator(), db, project), TYPE_MISMATCH)

    assert issue.severity is ValidationSeverity.ERROR
    assert "DINT" in issue.message


def test_an_alias_of_unknown_type_states_no_disagreement(db: Session, project: Project) -> None:
    """Nothing was said about the alias's type, so nothing can contradict it."""
    target = make_tag(db, project, "Base_Word", base_data_type=BaseDataType.DINT)
    alias = make_tag(
        db,
        project,
        "Motor_Running",
        is_alias=True,
        alias_for="Base_Word",
        base_data_type=BaseDataType.UNKNOWN,
    )
    make_tag_alias(
        db,
        project,
        alias,
        target_expression="Base_Word",
        target_tag_id=target.id,
        is_resolved=True,
    )

    assert TYPE_MISMATCH not in codes(run_check(TagAliasValidator(), db, project))


# ---------------------------------------------------------------------------
# tags.orphaned_tag and the coverage statements around it
# ---------------------------------------------------------------------------
ORPHAN = "tags.orphaned_tag"
NO_XREF = "tags.cross_reference_unavailable"
AOI_UNCHECKED = "tags.aoi_interface_unchecked"


def test_unreferenced_tag_is_reported_as_information(db: Session, project: Project) -> None:
    rung = make_ladder(db, project)
    read_tag(db, project, rung, "Some_Other_Tag")
    make_tag(db, project, "Never_Used")

    issue = only(run_check(TagUsageValidator(), db, project), ORPHAN)

    assert issue.severity is ValidationSeverity.INFORMATION
    assert "Never_Used" in issue.message


def test_a_tag_a_rung_references_is_not_an_orphan(db: Session, project: Project) -> None:
    rung = make_ladder(db, project)
    read_tag(db, project, rung, "Motor_Run_Cmd")
    make_tag(db, project, "Motor_Run_Cmd")

    assert ORPHAN not in codes(run_check(TagUsageValidator(), db, project))


def test_a_udt_member_reference_keeps_the_base_tag_off_the_orphan_list(
    db: Session, project: Project
) -> None:
    rung = make_ladder(db, project)
    read_tag(db, project, rung, "CV101_Status.Running")
    make_tag(db, project, "CV101_Status", base_data_type=BaseDataType.UDT)

    assert ORPHAN not in codes(run_check(TagUsageValidator(), db, project))


def test_an_hmi_only_tag_is_not_an_orphan(db: Session, project: Project) -> None:
    rung = make_ladder(db, project)
    read_tag(db, project, rung, "Some_Other_Tag")
    make_tag(db, project, "Line_Speed_Display")
    screen = make_screen(db, project)
    make_binding(db, project, screen, tag_name="Line_Speed_Display")

    assert ORPHAN not in codes(run_check(TagUsageValidator(), db, project))


def test_no_ladder_means_the_orphan_question_was_not_asked(db: Session, project: Project) -> None:
    make_tag(db, project, "Never_Used")

    found = run_check(TagUsageValidator(), db, project)

    assert only(found, NO_XREF).severity is ValidationSeverity.UNSUPPORTED
    assert ORPHAN not in codes(found)


def test_ladder_present_means_no_coverage_caveat(db: Session, project: Project) -> None:
    rung = make_ladder(db, project)
    read_tag(db, project, rung, "Motor_Run_Cmd")
    make_tag(db, project, "Motor_Run_Cmd")

    assert NO_XREF not in codes(run_check(TagUsageValidator(), db, project))


def test_a_tag_of_an_unsupplied_aoi_type_is_reported_as_unchecked(
    db: Session, project: Project
) -> None:
    make_tag(
        db,
        project,
        "MTR_101",
        base_data_type=BaseDataType.ADDON_INSTRUCTION,
        data_type_name="ESCO_Motor",
    )
    make_aoi_reference(
        db,
        project,
        "ESCO_Motor",
        is_available=False,
        availability_note="Referenced by MainRoutine, not present in the L5X.",
    )

    issue = only(run_check(TagUsageValidator(), db, project), AOI_UNCHECKED)

    assert issue.severity is ValidationSeverity.UNSUPPORTED
    assert "ESCO_Motor" in issue.message


def test_a_tag_of_a_supplied_aoi_type_does_not_fire(db: Session, project: Project) -> None:
    make_tag(
        db,
        project,
        "MTR_101",
        base_data_type=BaseDataType.ADDON_INSTRUCTION,
        data_type_name="ESCO_Motor",
    )
    make_aoi_reference(db, project, "ESCO_Motor", is_available=True)

    assert AOI_UNCHECKED not in codes(run_check(TagUsageValidator(), db, project))
