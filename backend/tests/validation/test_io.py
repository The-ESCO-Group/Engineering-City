"""The I/O validators.

Two of the negative cases here are load-bearing product decisions rather than
tidiness. A device declared ``is_network_only`` must not be reported as unwired,
and a device whose only signals are ``VIRTUAL`` must not be either — both are
correctly modelled, and reporting them would turn the check that finds a genuinely
forgotten motor into a list nobody reads.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.enums import (
    AddressAssignmentStatus,
    FailState,
    ObjectType,
    SignalDirection,
    SignalType,
    ValidationSeverity,
)
from app.models.project import Project
from app.validation.io import (
    DeviceCoverageValidator,
    FailStateValidator,
    IOAssignmentValidator,
    IOHardwareValidator,
)
from tests.validation import (
    codes,
    issues_coded,
    make_assignment,
    make_device,
    make_io_point,
    make_module,
    make_signal,
    only,
    run_check,
)

# ---------------------------------------------------------------------------
# io.duplicate_point_assignment
# ---------------------------------------------------------------------------
DUPLICATE = "io.duplicate_point_assignment"


def test_two_signals_on_one_terminal_is_an_error(db: Session, project: Project) -> None:
    guard = make_device(db, project, "GD401")
    drive = make_device(db, project, "MTR403")
    point = make_io_point(db, project)
    first = make_signal(db, project, guard, "GD401_CLOSED", ordinal=0)
    second = make_signal(db, project, drive, "MTR403_FAULT", ordinal=0)
    make_assignment(db, project, first, point, status=AddressAssignmentStatus.ASSIGNED)
    make_assignment(db, project, second, point, status=AddressAssignmentStatus.ASSIGNED)

    found = issues_coded(run_check(IOAssignmentValidator(), db, project), DUPLICATE)

    assert len(found) == 2, "both claimants should be named"
    assert all(issue.severity is ValidationSeverity.ERROR for issue in found)
    assert "Local:2:O.Data.0" in found[0].object_label
    assert all(
        "GD401_CLOSED" in issue.message and "MTR403_FAULT" in issue.message for issue in found
    )
    assert all(issue.detail["signal_names"] == ["GD401_CLOSED", "MTR403_FAULT"] for issue in found)
    assert all(issue.detail["signal_ids"] == [str(first.id), str(second.id)] for issue in found), (
        "the deterministic names and IDs must continue to describe the same claimants"
    )


def test_one_signal_per_terminal_does_not_fire(db: Session, project: Project) -> None:
    device = make_device(db, project)
    first_point = make_io_point(db, project, "Local:2:O.Data.0")
    second_point = make_io_point(db, project, "Local:2:O.Data.1")
    first = make_signal(db, project, device, "Run_Cmd", ordinal=0)
    second = make_signal(db, project, device, "Fwd_Cmd", ordinal=1)
    make_assignment(db, project, first, first_point, status=AddressAssignmentStatus.ASSIGNED)
    make_assignment(db, project, second, second_point, status=AddressAssignmentStatus.ASSIGNED)

    assert DUPLICATE not in codes(run_check(IOAssignmentValidator(), db, project))


# ---------------------------------------------------------------------------
# io.assignment_without_point
# ---------------------------------------------------------------------------
NO_POINT = "io.assignment_without_point"


def test_an_assigned_binding_with_no_terminal_is_an_error(db: Session, project: Project) -> None:
    device = make_device(db, project)
    signal = make_signal(db, project, device)
    make_assignment(db, project, signal, None, status=AddressAssignmentStatus.ASSIGNED)

    issue = only(run_check(IOAssignmentValidator(), db, project), NO_POINT)

    assert issue.severity is ValidationSeverity.ERROR
    assert issue.object_type is ObjectType.ADDRESS_ASSIGNMENT


def test_a_proposed_binding_with_no_terminal_does_not_fire(db: Session, project: Project) -> None:
    """Intent recorded before the terminal is chosen is a normal state."""
    device = make_device(db, project)
    signal = make_signal(db, project, device)
    make_assignment(db, project, signal, None, status=AddressAssignmentStatus.PROPOSED)

    assert NO_POINT not in codes(run_check(IOAssignmentValidator(), db, project))


# ---------------------------------------------------------------------------
# io.signal_type_mismatch
# ---------------------------------------------------------------------------
MISMATCH = "io.signal_type_mismatch"


def test_a_discrete_signal_on_an_analog_terminal_is_an_error(db: Session, project: Project) -> None:
    device = make_device(db, project)
    signal = make_signal(
        db,
        project,
        device,
        "Running",
        signal_type=SignalType.DISCRETE_INPUT,
        direction=SignalDirection.INPUT,
    )
    point = make_io_point(
        db,
        project,
        "Local:4:I.Ch0Data",
        signal_type=SignalType.ANALOG_INPUT,
        direction=SignalDirection.INPUT,
    )
    make_assignment(db, project, signal, point, status=AddressAssignmentStatus.ASSIGNED)

    issue = only(run_check(IOAssignmentValidator(), db, project), MISMATCH)

    assert issue.severity is ValidationSeverity.ERROR
    assert issue.detail["signal_type"] == SignalType.DISCRETE_INPUT.value
    assert issue.detail["point_type"] == SignalType.ANALOG_INPUT.value


def test_matching_signal_and_terminal_types_do_not_fire(db: Session, project: Project) -> None:
    device = make_device(db, project)
    signal = make_signal(db, project, device)
    point = make_io_point(db, project)
    make_assignment(db, project, signal, point, status=AddressAssignmentStatus.ASSIGNED)

    assert MISMATCH not in codes(run_check(IOAssignmentValidator(), db, project))


# ---------------------------------------------------------------------------
# io.undeclared_module / io.point_without_module
# ---------------------------------------------------------------------------
UNDECLARED = "io.undeclared_module"
NO_MODULE = "io.point_without_module"


def test_a_referenced_only_module_is_an_error(db: Session, project: Project) -> None:
    module = make_module(db, project, "Local:9", is_referenced_only=True)
    make_io_point(db, project, "Local:9:I.Data.0", module=module)

    issue = only(run_check(IOHardwareValidator(), db, project), UNDECLARED)

    assert issue.severity is ValidationSeverity.ERROR
    assert issue.object_type is ObjectType.MODULE


def test_a_declared_module_does_not_fire(db: Session, project: Project) -> None:
    module = make_module(db, project, "Local:2", catalog_number="1756-OB16E", point_count=16)
    make_io_point(db, project, module=module)

    assert UNDECLARED not in codes(run_check(IOHardwareValidator(), db, project))


def test_a_point_on_no_module_is_a_warning(db: Session, project: Project) -> None:
    make_io_point(db, project, "Local:2:O.Data.0")

    issue = only(run_check(IOHardwareValidator(), db, project), NO_MODULE)

    assert issue.severity is ValidationSeverity.WARNING


def test_a_point_attached_to_its_module_does_not_fire(db: Session, project: Project) -> None:
    module = make_module(db, project, point_count=16)
    make_io_point(db, project, module=module)

    assert NO_MODULE not in codes(run_check(IOHardwareValidator(), db, project))


# ---------------------------------------------------------------------------
# io.module_oversubscribed / io.module_capacity_unstated
# ---------------------------------------------------------------------------
OVERSUBSCRIBED = "io.module_oversubscribed"
CAPACITY_UNSTATED = "io.module_capacity_unstated"


def test_more_points_than_the_card_has_is_an_error(db: Session, project: Project) -> None:
    module = make_module(db, project, point_count=1)
    make_io_point(db, project, "Local:2:O.Data.0", module=module)
    make_io_point(db, project, "Local:2:O.Data.1", module=module)

    issue = only(run_check(IOHardwareValidator(), db, project), OVERSUBSCRIBED)

    assert issue.severity is ValidationSeverity.ERROR
    assert issue.detail["addressed_point_count"] == 2


def test_points_within_the_declared_count_do_not_fire(db: Session, project: Project) -> None:
    module = make_module(db, project, point_count=16)
    make_io_point(db, project, "Local:2:O.Data.0", module=module)
    make_io_point(db, project, "Local:2:O.Data.1", module=module)

    found = codes(run_check(IOHardwareValidator(), db, project))
    assert OVERSUBSCRIBED not in found
    assert CAPACITY_UNSTATED not in found


def test_a_module_with_no_declared_capacity_is_reported_as_unchecked(
    db: Session, project: Project
) -> None:
    module = make_module(db, project, point_count=0)
    make_io_point(db, project, module=module)

    issue = only(run_check(IOHardwareValidator(), db, project), CAPACITY_UNSTATED)

    assert issue.severity is ValidationSeverity.UNSUPPORTED
    assert issue.detail["modules"] == ["Local:2"]


# ---------------------------------------------------------------------------
# io.device_without_io
# ---------------------------------------------------------------------------
NO_IO = "io.device_without_io"


def test_an_unwired_device_is_an_error(db: Session, project: Project) -> None:
    device = make_device(db, project)
    make_signal(db, project, device)

    issue = only(run_check(DeviceCoverageValidator(), db, project), NO_IO)

    assert issue.severity is ValidationSeverity.ERROR
    assert "MTR_101" in issue.object_label


def test_a_device_with_no_signals_at_all_is_an_error(db: Session, project: Project) -> None:
    make_device(db, project)

    assert NO_IO in codes(run_check(DeviceCoverageValidator(), db, project))


def test_a_network_only_device_does_not_fire(db: Session, project: Project) -> None:
    device = make_device(db, project, is_network_only=True)
    make_signal(db, project, device)

    assert NO_IO not in codes(run_check(DeviceCoverageValidator(), db, project))


def test_a_wired_device_does_not_fire(db: Session, project: Project) -> None:
    device = make_device(db, project)
    signal = make_signal(db, project, device)
    point = make_io_point(db, project)
    make_assignment(db, project, signal, point, status=AddressAssignmentStatus.ASSIGNED)

    assert NO_IO not in codes(run_check(DeviceCoverageValidator(), db, project))


def test_a_device_whose_signals_are_all_virtual_does_not_fire(
    db: Session, project: Project
) -> None:
    """A VIRTUAL signal exists only in logic and has no terminal by definition."""
    device = make_device(db, project)
    make_signal(
        db,
        project,
        device,
        "Interlocked",
        signal_type=SignalType.VIRTUAL,
        direction=SignalDirection.INPUT,
    )

    assert NO_IO not in codes(run_check(DeviceCoverageValidator(), db, project))


# ---------------------------------------------------------------------------
# io.safety_fail_state_undefined
# ---------------------------------------------------------------------------
FAIL_STATE = "io.safety_fail_state_undefined"


def test_a_safety_device_with_no_fail_state_is_an_error(db: Session, project: Project) -> None:
    make_device(db, project, is_safety_related=True, fail_state=FailState.UNDEFINED)

    issue = only(run_check(FailStateValidator(), db, project), FAIL_STATE)

    assert issue.severity is ValidationSeverity.ERROR
    assert "never stated" in issue.message
    assert "clarification" in issue.suggested_action


def test_a_safety_device_with_a_stated_fail_state_does_not_fire(
    db: Session, project: Project
) -> None:
    make_device(db, project, is_safety_related=True, fail_state=FailState.DE_ENERGIZE)

    assert FAIL_STATE not in codes(run_check(FailStateValidator(), db, project))


def test_a_non_safety_device_with_no_fail_state_does_not_fire(
    db: Session, project: Project
) -> None:
    make_device(db, project, is_safety_related=False, fail_state=FailState.UNDEFINED)

    assert FAIL_STATE not in codes(run_check(FailStateValidator(), db, project))


def test_a_safety_signal_and_its_terminal_are_both_reported(db: Session, project: Project) -> None:
    device = make_device(db, project, is_safety_related=True, fail_state=FailState.DE_ENERGIZE)
    signal = make_signal(db, project, device, fail_state=FailState.UNDEFINED)
    point = make_io_point(db, project, fail_state=FailState.UNDEFINED)
    make_assignment(db, project, signal, point, status=AddressAssignmentStatus.ASSIGNED)

    found = issues_coded(run_check(FailStateValidator(), db, project), FAIL_STATE)

    assert {issue.object_type for issue in found} == {ObjectType.SIGNAL, ObjectType.IO_POINT}
