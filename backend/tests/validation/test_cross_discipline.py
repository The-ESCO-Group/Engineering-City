"""The cross-discipline validators.

The severity split between an unresolved HMI *read* and an unresolved HMI *write*
is asserted here rather than left to a comment, because it is the one judgement in
this module that a later reader is most likely to flatten into "they are both
broken bindings".
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy.orm import Session

from app.core.clock import utcnow
from app.core.enums import (
    ObjectApprovalState,
    ObjectType,
    ReleaseCandidateStatus,
    RequirementStatus,
    SignalDirection,
    SignalType,
    TagRole,
    ValidationSeverity,
)
from app.models.project import Project
from app.validation.cross_discipline import (
    AlarmCoverageValidator,
    ApprovalIntegrityValidator,
    HMIBindingValidator,
    ReleaseFreshnessValidator,
    TestCoverageValidator,
)
from tests.validation import (
    codes,
    issues_coded,
    make_alarm,
    make_binding,
    make_fault,
    make_interlock,
    make_io_point,
    make_ladder,
    make_release_candidate,
    make_requirement,
    make_screen,
    make_tag,
    make_test_case,
    only,
    read_tag,
    run_check,
    write_tag,
)

# ---------------------------------------------------------------------------
# HMI bindings
# ---------------------------------------------------------------------------
READ_UNRESOLVED = "cross.hmi_read_binding_unresolved"
WRITE_UNRESOLVED = "cross.hmi_write_binding_unresolved"
WRITE_UNHANDLED = "cross.hmi_write_not_handled"
STATUS_UNDRIVEN = "cross.status_never_driven"
EXPRESSION = "cross.hmi_expression_unchecked"


def test_an_unresolved_read_binding_is_a_warning(db: Session, project: Project) -> None:
    """It shows itself on the display, in front of the operator."""
    screen = make_screen(db, project)
    make_binding(db, project, screen, tag_name="Missing_Tag", access="read")

    issue = only(run_check(HMIBindingValidator(), db, project), READ_UNRESOLVED)

    assert issue.severity is ValidationSeverity.WARNING


def test_an_unresolved_write_binding_is_an_error(db: Session, project: Project) -> None:
    """It looks entirely normal and reaches nothing."""
    screen = make_screen(db, project)
    make_binding(db, project, screen, tag_name="Missing_Tag", access="write")

    issue = only(run_check(HMIBindingValidator(), db, project), WRITE_UNRESOLVED)

    assert issue.severity is ValidationSeverity.ERROR
    assert issue.object_type is ObjectType.HMI_BINDING


def test_a_binding_to_a_declared_tag_does_not_fire(db: Session, project: Project) -> None:
    screen = make_screen(db, project)
    make_tag(db, project, "Motor_Run_Cmd")
    make_binding(db, project, screen, tag_name="Motor_Run_Cmd", access="read")

    found = codes(run_check(HMIBindingValidator(), db, project))
    assert READ_UNRESOLVED not in found
    assert WRITE_UNRESOLVED not in found


def test_a_write_binding_no_rung_reads_is_an_error(db: Session, project: Project) -> None:
    rung = make_ladder(db, project)
    read_tag(db, project, rung, "Something_Else")
    make_tag(db, project, "Motor_Start_PB")
    screen = make_screen(db, project)
    make_binding(db, project, screen, tag_name="Motor_Start_PB", access="write")

    issue = only(run_check(HMIBindingValidator(), db, project), WRITE_UNHANDLED)

    assert issue.severity is ValidationSeverity.ERROR
    assert "nothing acts on it" in issue.message


def test_a_write_binding_a_rung_reads_does_not_fire(db: Session, project: Project) -> None:
    rung = make_ladder(db, project)
    read_tag(db, project, rung, "Motor_Start_PB")
    make_tag(db, project, "Motor_Start_PB")
    screen = make_screen(db, project)
    make_binding(db, project, screen, tag_name="Motor_Start_PB", access="write")

    assert WRITE_UNHANDLED not in codes(run_check(HMIBindingValidator(), db, project))


def test_a_status_no_rung_writes_is_a_warning(db: Session, project: Project) -> None:
    rung = make_ladder(db, project)
    read_tag(db, project, rung, "Something_Else")
    make_tag(db, project, "Motor_Running")
    screen = make_screen(db, project)
    make_binding(db, project, screen, tag_name="Motor_Running", access="read")

    issue = only(run_check(HMIBindingValidator(), db, project), STATUS_UNDRIVEN)

    assert issue.severity is ValidationSeverity.WARNING


def test_a_driven_status_does_not_fire(db: Session, project: Project) -> None:
    rung = make_ladder(db, project)
    write_tag(db, project, rung, "Motor_Running")
    make_tag(db, project, "Motor_Running")
    screen = make_screen(db, project)
    make_binding(db, project, screen, tag_name="Motor_Running", access="read")

    assert STATUS_UNDRIVEN not in codes(run_check(HMIBindingValidator(), db, project))


def test_a_status_carried_by_an_input_terminal_does_not_fire(db: Session, project: Project) -> None:
    rung = make_ladder(db, project)
    read_tag(db, project, rung, "Something_Else")
    point = make_io_point(
        db,
        project,
        "Local:1:I.Data.0",
        signal_type=SignalType.DISCRETE_INPUT,
        direction=SignalDirection.INPUT,
    )
    make_tag(db, project, "Guard_Closed", io_point_id=point.id)
    screen = make_screen(db, project)
    make_binding(db, project, screen, tag_name="Guard_Closed", access="read")

    assert STATUS_UNDRIVEN not in codes(run_check(HMIBindingValidator(), db, project))


def test_an_expression_binding_is_reported_as_unchecked(db: Session, project: Project) -> None:
    screen = make_screen(db, project)
    make_binding(
        db,
        project,
        screen,
        tag_name="",
        expression="Motor_Running AND NOT Motor_Faulted",
        access="read",
    )

    issue = only(run_check(HMIBindingValidator(), db, project), EXPRESSION)

    assert issue.severity is ValidationSeverity.UNSUPPORTED


def test_a_plain_tag_binding_carries_no_expression_caveat(db: Session, project: Project) -> None:
    screen = make_screen(db, project)
    make_tag(db, project, "Motor_Running")
    make_binding(db, project, screen, tag_name="Motor_Running", access="read")

    assert EXPRESSION not in codes(run_check(HMIBindingValidator(), db, project))


# ---------------------------------------------------------------------------
# Alarm coverage
# ---------------------------------------------------------------------------
NO_TRIGGER = "cross.alarm_without_trigger"
NO_ALARM = "cross.trigger_without_alarm"


def test_an_alarm_with_no_trigger_is_an_error(db: Session, project: Project) -> None:
    make_alarm(db, project, "ALM-020", trigger_tag_name="")

    issue = only(run_check(AlarmCoverageValidator(), db, project), NO_TRIGGER)

    assert issue.severity is ValidationSeverity.ERROR


def test_an_alarm_with_a_trigger_does_not_fire(db: Session, project: Project) -> None:
    make_alarm(db, project, "ALM-021", trigger_tag_name="ALM_HOPPER_HIGH")

    assert NO_TRIGGER not in codes(run_check(AlarmCoverageValidator(), db, project))


def test_an_alarm_inheriting_its_trigger_from_a_fault_does_not_fire(
    db: Session, project: Project
) -> None:
    fault = make_fault(db, project, trigger_tag_name="FLT_LOW_SUCTION")
    make_alarm(db, project, "ALM-022", trigger_tag_name="", fault_definition_id=fault.id)

    assert NO_TRIGGER not in codes(run_check(AlarmCoverageValidator(), db, project))


def test_a_fault_no_alarm_annunciates_is_a_warning(db: Session, project: Project) -> None:
    make_fault(db, project, trigger_tag_name="FLT_LOW_SUCTION")

    issue = only(run_check(AlarmCoverageValidator(), db, project), NO_ALARM)

    assert issue.severity is ValidationSeverity.WARNING
    assert issue.object_type is ObjectType.FAULT_DEFINITION


def test_an_annunciated_fault_does_not_fire(db: Session, project: Project) -> None:
    fault = make_fault(db, project, trigger_tag_name="FLT_LOW_SUCTION")
    make_alarm(
        db, project, "ALM-023", trigger_tag_name="FLT_LOW_SUCTION", fault_definition_id=fault.id
    )

    assert NO_ALARM not in codes(run_check(AlarmCoverageValidator(), db, project))


def test_an_alarm_role_tag_with_no_definition_is_a_warning(db: Session, project: Project) -> None:
    make_tag(db, project, "ALM_HOPPER_HIGH", role=TagRole.ALARM)

    issue = only(run_check(AlarmCoverageValidator(), db, project), NO_ALARM)

    assert issue.object_type is ObjectType.TAG


def test_an_alarm_role_tag_used_as_a_trigger_does_not_fire(db: Session, project: Project) -> None:
    make_tag(db, project, "ALM_HOPPER_HIGH", role=TagRole.ALARM)
    make_alarm(db, project, "ALM-024", trigger_tag_name="ALM_HOPPER_HIGH")

    assert NO_ALARM not in codes(run_check(AlarmCoverageValidator(), db, project))


# ---------------------------------------------------------------------------
# Test coverage
# ---------------------------------------------------------------------------
TEST_UNTRACED = "cross.test_without_requirement"
REQUIREMENT_UNTESTED = "cross.requirement_without_test"
INTERLOCK_UNTESTED = "cross.interlock_without_test"


def test_a_test_tracing_to_nothing_is_a_warning(db: Session, project: Project) -> None:
    make_test_case(db, project, "TC-001")

    issue = only(run_check(TestCoverageValidator(), db, project), TEST_UNTRACED)

    assert issue.severity is ValidationSeverity.WARNING


def test_a_test_citing_a_requirement_does_not_fire(db: Session, project: Project) -> None:
    requirement = make_requirement(db, project, "REQ-001", status=RequirementStatus.ACCEPTED)
    make_test_case(db, project, "TC-001", requirement_id=requirement.id)

    found = codes(run_check(TestCoverageValidator(), db, project))
    assert TEST_UNTRACED not in found
    assert REQUIREMENT_UNTESTED not in found


def test_an_untested_requirement_is_a_warning(db: Session, project: Project) -> None:
    make_requirement(db, project, "REQ-002", status=RequirementStatus.ACCEPTED)

    issue = only(run_check(TestCoverageValidator(), db, project), REQUIREMENT_UNTESTED)

    assert issue.severity is ValidationSeverity.WARNING


def test_an_untested_safety_interlock_is_an_error(db: Session, project: Project) -> None:
    make_interlock(db, project, "ILK-001", is_safety_related=True)

    issue = only(run_check(TestCoverageValidator(), db, project), INTERLOCK_UNTESTED)

    assert issue.severity is ValidationSeverity.ERROR
    assert "safety-related" in issue.message


def test_an_untested_process_interlock_is_a_warning(db: Session, project: Project) -> None:
    make_interlock(db, project, "ILK-002", is_safety_related=False)

    issue = only(run_check(TestCoverageValidator(), db, project), INTERLOCK_UNTESTED)

    assert issue.severity is ValidationSeverity.WARNING


def test_a_tested_interlock_does_not_fire(db: Session, project: Project) -> None:
    interlock = make_interlock(db, project, "ILK-001", is_safety_related=True)
    make_test_case(db, project, "TC-010", interlock_id=interlock.id)

    assert INTERLOCK_UNTESTED not in codes(run_check(TestCoverageValidator(), db, project))


# ---------------------------------------------------------------------------
# Approval integrity
# ---------------------------------------------------------------------------
MODIFIED = "cross.approved_object_modified"


def test_an_approved_object_edited_afterwards_is_an_error(db: Session, project: Project) -> None:
    now = utcnow()
    make_tag(
        db,
        project,
        "Motor_Run_Cmd",
        approval_state=ObjectApprovalState.APPROVED,
        approved_at=now - timedelta(hours=2),
        updated_at=now,
    )

    issue = only(run_check(ApprovalIntegrityValidator(), db, project), MODIFIED)

    assert issue.severity is ValidationSeverity.ERROR
    assert issue.object_type is ObjectType.TAG


def test_an_untouched_approved_object_does_not_fire(db: Session, project: Project) -> None:
    now = utcnow()
    make_tag(
        db,
        project,
        "Motor_Run_Cmd",
        approval_state=ObjectApprovalState.APPROVED,
        approved_at=now,
        updated_at=now - timedelta(hours=2),
    )

    assert MODIFIED not in codes(run_check(ApprovalIntegrityValidator(), db, project))


def test_a_draft_object_is_not_subject_to_the_check(db: Session, project: Project) -> None:
    now = utcnow()
    make_tag(
        db,
        project,
        "Motor_Run_Cmd",
        approval_state=ObjectApprovalState.DRAFT,
        approved_at=now - timedelta(hours=2),
        updated_at=now,
    )

    assert MODIFIED not in codes(run_check(ApprovalIntegrityValidator(), db, project))


# ---------------------------------------------------------------------------
# Release freshness
# ---------------------------------------------------------------------------
STALE = "cross.stale_release_artifact"


def test_a_candidate_built_from_an_older_revision_is_an_error(
    db: Session, project: Project
) -> None:
    project.current_revision_number = 3
    db.flush()
    make_release_candidate(
        db, project, 1, revision_number=1, status=ReleaseCandidateStatus.GENERATED
    )

    issue = only(run_check(ReleaseFreshnessValidator(), db, project), STALE)

    assert issue.severity is ValidationSeverity.ERROR
    assert issue.detail["candidate_revision"] == 1
    assert issue.detail["project_revision"] == 3


def test_a_candidate_at_the_current_revision_does_not_fire(db: Session, project: Project) -> None:
    project.current_revision_number = 3
    db.flush()
    make_release_candidate(
        db, project, 1, revision_number=3, status=ReleaseCandidateStatus.GENERATED
    )

    assert STALE not in codes(run_check(ReleaseFreshnessValidator(), db, project))


def test_an_already_released_candidate_is_history_not_a_defect(
    db: Session, project: Project
) -> None:
    project.current_revision_number = 3
    db.flush()
    make_release_candidate(
        db, project, 1, revision_number=1, status=ReleaseCandidateStatus.RELEASED
    )

    assert STALE not in codes(run_check(ReleaseFreshnessValidator(), db, project))


def test_every_cross_discipline_issue_can_be_located(db: Session, project: Project) -> None:
    """A result nobody can find is not actionable, whatever it says."""
    screen = make_screen(db, project)
    make_binding(db, project, screen, tag_name="Missing_Tag", access="write")
    make_alarm(db, project, "ALM-030", trigger_tag_name="")
    make_interlock(db, project, "ILK-001", is_safety_related=True)

    issues = [
        *run_check(HMIBindingValidator(), db, project),
        *run_check(AlarmCoverageValidator(), db, project),
        *run_check(TestCoverageValidator(), db, project),
    ]

    assert issues
    for issue in issues:
        assert issue.object_label, f"{issue.code} has no object label"
        assert issue.suggested_action, f"{issue.code} suggests nothing"
    assert issues_coded(issues, WRITE_UNRESOLVED)
