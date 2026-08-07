"""I/O-discipline checks: is every device wired, and is anything wired twice?

Codes in this module are namespaced ``io.*`` and are published API — see the
package docstring.

The plant schema is arranged so that the defects worth finding are *storable*:
two signals may both claim one terminal, a module may exist as a referenced-only
row, and ``FailState.UNDEFINED`` is a real value rather than a null. None of that
is an accident, and this module is the half of the bargain that reports what the
schema refused to hide.

Two rules run through every check here. Nothing is inferred from an absence — a
device with no I/O is reported as such rather than assumed to be a network
device, because ``is_network_only`` exists precisely so that declaration is
explicit. And nothing invents a fail state: a signal whose behaviour on loss of
source nobody wrote down is reported as unstated, never compared against a
plausible default.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable

from app.core.enums import (
    AddressAssignmentStatus,
    FailState,
    ObjectType,
    ValidationDiscipline,
    ValidationSeverity,
)
from app.models.plant import Device, IOPoint, Signal
from app.validation.base import Issue, ValidationContext, label

__all__ = [
    "DeviceCoverageValidator",
    "FailStateValidator",
    "IOAssignmentValidator",
    "IOHardwareValidator",
]

_DISCIPLINE = ValidationDiscipline.IO


def _device_label(device: Device) -> str:
    return label(f"{device.tag_prefix} / {device.name}")


def _signal_label(context: ValidationContext, signal: Signal) -> str:
    device = context.devices_by_id.get(signal.device_id)
    prefix = device.tag_prefix if device is not None else "unknown device"
    return label(f"{prefix} / {signal.name}")


def _point_label(context: ValidationContext, point: IOPoint) -> str:
    module = None
    if point.hardware_module_id is not None:
        module = next(
            (item for item in context.hardware_modules if item.id == point.hardware_module_id),
            None,
        )
    module_name = module.name if module is not None else "unassigned module"
    return label(f"{module_name} / {point.address}")


class IOAssignmentValidator:
    """Terminals claimed twice, assignments that name nothing, and type conflicts."""

    key = "io.assignments"
    discipline = _DISCIPLINE
    description = "Address assignments: duplicates, missing terminals and signal type conflicts."

    def check(self, context: ValidationContext) -> Iterable[Issue]:
        issues: list[Issue] = []

        by_point: dict[uuid.UUID, list[uuid.UUID]] = {}
        for assignment in context.address_assignments:
            if assignment.io_point_id is not None:
                by_point.setdefault(assignment.io_point_id, []).append(assignment.signal_id)

        for assignment in context.address_assignments:
            signal = context.signals_by_id.get(assignment.signal_id)
            location = _signal_label(context, signal) if signal is not None else "unknown signal"

            if assignment.io_point_id is None:
                if assignment.status is AddressAssignmentStatus.ASSIGNED:
                    issues.append(
                        Issue(
                            code="io.assignment_without_point",
                            discipline=_DISCIPLINE,
                            # ERROR: the assignment claims the signal is wired
                            # and names no terminal, so the export addresses
                            # nothing while the I/O schedule reads as complete.
                            # A PROPOSED or RESERVED assignment with no terminal
                            # is a legitimate intermediate state and is not
                            # reported.
                            severity=ValidationSeverity.ERROR,
                            message=(
                                "Address assignment is marked assigned but names no I/O point."
                            ),
                            object_type=ObjectType.ADDRESS_ASSIGNMENT,
                            object_id=assignment.id,
                            object_label=location,
                            suggested_action=(
                                "Bind the assignment to a terminal, or move it back to "
                                "proposed until one has been chosen."
                            ),
                            detail={"status": assignment.status.value},
                        )
                    )
                continue

            point = context.io_points_by_id.get(assignment.io_point_id)
            if point is None:
                continue

            claimants = sorted(
                by_point.get(assignment.io_point_id, []),
                key=lambda signal_id: (
                    context.signals_by_id[signal_id].name.casefold()
                    if signal_id in context.signals_by_id
                    else "\uffff",
                    context.signals_by_id[signal_id].name
                    if signal_id in context.signals_by_id
                    else "",
                    str(signal_id),
                ),
            )
            if len(claimants) > 1:
                claimant_names = [
                    (
                        context.signals_by_id[signal_id].name
                        if signal_id in context.signals_by_id
                        else f"unknown signal {signal_id}"
                    )
                    for signal_id in claimants
                ]
                issues.append(
                    Issue(
                        code="io.duplicate_point_assignment",
                        discipline=_DISCIPLINE,
                        # ERROR: one terminal, two signals. Whichever is written
                        # into the export, the other device is not wired to what
                        # the drawing says it is wired to — and the failure is
                        # found at commissioning, on the machine.
                        severity=ValidationSeverity.ERROR,
                        message=(
                            f"I/O point '{point.address}' is assigned to {len(claimants)} "
                            f"signals: {', '.join(repr(name) for name in claimant_names)}."
                        ),
                        object_type=ObjectType.ADDRESS_ASSIGNMENT,
                        object_id=assignment.id,
                        object_label=_point_label(context, point),
                        suggested_action=(
                            "Decide which signal owns this terminal and move the others to "
                            "free channels."
                        ),
                        detail={
                            "address": point.address,
                            "signal_ids": [str(item) for item in claimants],
                            "signal_names": claimant_names,
                        },
                    )
                )

            if signal is None:
                continue
            if signal.signal_type is not point.signal_type:
                issues.append(
                    Issue(
                        code="io.signal_type_mismatch",
                        discipline=_DISCIPLINE,
                        # ERROR: an analog signal on a discrete terminal, or an
                        # input on an output card, addresses data of the wrong
                        # shape. The generated alias reads whatever bits happen
                        # to be at that address, which is a plausible-looking
                        # number that means nothing.
                        severity=ValidationSeverity.ERROR,
                        message=(
                            f"Signal '{signal.name}' is a {signal.signal_type.value} but "
                            f"I/O point '{point.address}' is a {point.signal_type.value}."
                        ),
                        object_type=ObjectType.ADDRESS_ASSIGNMENT,
                        object_id=assignment.id,
                        object_label=_signal_label(context, signal),
                        suggested_action=(
                            "Move the signal to a terminal of the right type, or correct "
                            "whichever of the two declarations is wrong."
                        ),
                        detail={
                            "signal_type": signal.signal_type.value,
                            "point_type": point.signal_type.value,
                            "address": point.address,
                        },
                    )
                )

        return issues


class IOHardwareValidator:
    """Modules nothing declared, points on no module, and cards over capacity."""

    key = "io.hardware"
    discipline = _DISCIPLINE
    description = "Hardware modules referenced but undeclared, and module point budgets."

    def check(self, context: ValidationContext) -> Iterable[Issue]:
        issues: list[Issue] = []
        points_per_module: dict[uuid.UUID, int] = {}
        for point in context.io_points:
            if point.hardware_module_id is not None:
                points_per_module[point.hardware_module_id] = (
                    points_per_module.get(point.hardware_module_id, 0) + 1
                )

        uncheckable: list[str] = []
        for module in context.hardware_modules:
            if module.is_referenced_only:
                issues.append(
                    Issue(
                        code="io.undeclared_module",
                        discipline=_DISCIPLINE,
                        # ERROR: an address names a module the project never
                        # declared. Studio 5000 refuses the import and its error
                        # does not name the missing module, so the customer
                        # loses an hour finding out what this validator already
                        # knows.
                        severity=ValidationSeverity.ERROR,
                        message=(
                            f"Module '{module.name}' is referenced by an I/O address but was "
                            "never declared. Nothing in the model says what it is."
                        ),
                        object_type=ObjectType.MODULE,
                        object_id=module.id,
                        object_label=label(f"Module / {module.name}"),
                        suggested_action=(
                            "Declare the module with its catalogue number, slot and revision, "
                            "or correct the addresses that reference it."
                        ),
                        detail={
                            "name": module.name,
                            "catalog_number": module.catalog_number,
                            "point_count": points_per_module.get(module.id, 0),
                        },
                    )
                )

            declared = module.point_count
            used = points_per_module.get(module.id, 0)
            if declared <= 0:
                if used:
                    uncheckable.append(module.name)
                continue
            if used > declared:
                issues.append(
                    Issue(
                        code="io.module_oversubscribed",
                        discipline=_DISCIPLINE,
                        # ERROR: more terminals are in use than the card has.
                        # At least one address does not exist on the hardware,
                        # so at least one signal is not wired anywhere.
                        severity=ValidationSeverity.ERROR,
                        message=(
                            f"Module '{module.name}' declares {declared} points but "
                            f"{used} I/O points are addressed on it."
                        ),
                        object_type=ObjectType.MODULE,
                        object_id=module.id,
                        object_label=label(f"Module / {module.name}"),
                        suggested_action=(
                            "Check the addresses against the card's real channel count and "
                            "move the surplus to another module."
                        ),
                        detail={
                            "name": module.name,
                            "declared_point_count": declared,
                            "addressed_point_count": used,
                        },
                    )
                )

        for point in context.io_points:
            if point.hardware_module_id is not None:
                continue
            issues.append(
                Issue(
                    code="io.point_without_module",
                    discipline=_DISCIPLINE,
                    # WARNING: an address read out of an I/O list before anyone
                    # identified the card it lives on is a normal intermediate
                    # state, and it is recoverable at any point before export.
                    # It is reported because the address cannot be validated
                    # against anything until the module is known.
                    severity=ValidationSeverity.WARNING,
                    message=(
                        f"I/O point '{point.address}' is not attached to any hardware module."
                    ),
                    object_type=ObjectType.IO_POINT,
                    object_id=point.id,
                    object_label=label(f"I/O point / {point.address}"),
                    suggested_action=(
                        "Identify the module this address belongs to and attach the point to it."
                    ),
                    detail={"address": point.address, "signal_type": point.signal_type.value},
                )
            )

        if uncheckable:
            # One issue, not one per module: the statement being made is about
            # the coverage of this check, and repeating it per row would bury
            # the findings that can be acted on.
            issues.append(
                Issue(
                    code="io.module_capacity_unstated",
                    discipline=_DISCIPLINE,
                    severity=ValidationSeverity.UNSUPPORTED,
                    message=(
                        f"{len(uncheckable)} module(s) state no point count, so whether their "
                        "addressed channels exist on the real card was not checked: "
                        f"{', '.join(sorted(uncheckable)[:10])}."
                    ),
                    object_label=label(context.project.name),
                    suggested_action=(
                        "Record each module's channel count from its catalogue number so the "
                        "budget can be checked before the package ships."
                    ),
                    detail={"modules": sorted(uncheckable)},
                )
            )

        return issues


class DeviceCoverageValidator:
    """Devices that reach no terminal and were not declared network-only."""

    key = "io.device_coverage"
    discipline = _DISCIPLINE
    description = "Devices with no wired I/O that are not flagged is_network_only."

    def check(self, context: ValidationContext) -> Iterable[Issue]:
        issues: list[Issue] = []

        for device in context.devices:
            if not device.requires_io_points:
                continue

            signals = context.signals_by_device.get(device.id, [])
            expecting_terminals = [signal for signal in signals if signal.requires_terminal]
            if signals and not expecting_terminals:
                # Every signal this device exposes is VIRTUAL, which exists only
                # in logic and has no terminal by definition. Reporting it would
                # be a confident error against a correctly modelled device.
                continue

            wired = any(
                assignment.io_point_id is not None
                and assignment.status is not AddressAssignmentStatus.CONFLICTED
                for signal in expecting_terminals
                for assignment in context.assignments_by_signal.get(signal.id, ())
            )
            if wired:
                continue

            issues.append(
                Issue(
                    code="io.device_without_io",
                    discipline=_DISCIPLINE,
                    # ERROR: the model says this is a physical device and
                    # nothing connects it to the controller. Logic generated
                    # for it commands equipment that is not wired, which reads
                    # as complete in every document and does nothing on the
                    # floor. ``is_network_only`` is the explicit escape, and
                    # requiring it is the point: "network device" and "somebody
                    # forgot to wire it" are identical from the data.
                    severity=ValidationSeverity.ERROR,
                    message=(
                        f"Device '{device.tag_prefix}' reaches no I/O point and is not "
                        f"flagged network-only ({len(signals)} signal(s) declared)."
                    ),
                    object_type=ObjectType.DEVICE,
                    object_id=device.id,
                    object_label=_device_label(device),
                    suggested_action=(
                        "Assign the device's signals to terminals, or set is_network_only if "
                        "it genuinely exchanges everything over the network."
                    ),
                    detail={
                        "tag_prefix": device.tag_prefix,
                        "signal_count": len(signals),
                        "signals_expecting_terminals": len(expecting_terminals),
                        "category": device.category.value,
                    },
                )
            )

        return issues


class FailStateValidator:
    """Safety-related things whose behaviour on loss of source nobody stated."""

    key = "io.fail_states"
    discipline = _DISCIPLINE
    description = "FailState.UNDEFINED on a safety-related device, signal or terminal."

    def check(self, context: ValidationContext) -> Iterable[Issue]:
        issues: list[Issue] = []
        safety_devices = {device.id for device in context.devices if device.is_safety_related}

        for device in context.devices:
            if device.id not in safety_devices:
                continue
            if device.fail_state is not FailState.UNDEFINED:
                continue
            issues.append(
                self._issue(
                    ObjectType.DEVICE,
                    device.id,
                    _device_label(device),
                    f"Safety-related device '{device.tag_prefix}'",
                    {"tag_prefix": device.tag_prefix, "category": device.category.value},
                )
            )

        safety_signal_ids: set[uuid.UUID] = set()
        for signal in context.signals:
            if signal.device_id not in safety_devices:
                continue
            safety_signal_ids.add(signal.id)
            if signal.fail_state is not FailState.UNDEFINED:
                continue
            issues.append(
                self._issue(
                    ObjectType.SIGNAL,
                    signal.id,
                    _signal_label(context, signal),
                    f"Safety-related signal '{signal.name}'",
                    {"name": signal.name, "signal_type": signal.signal_type.value},
                )
            )

        for assignment in context.address_assignments:
            if assignment.signal_id not in safety_signal_ids:
                continue
            if assignment.io_point_id is None:
                continue
            point = context.io_points_by_id.get(assignment.io_point_id)
            if point is None or point.fail_state is not FailState.UNDEFINED:
                continue
            issues.append(
                self._issue(
                    ObjectType.IO_POINT,
                    point.id,
                    _point_label(context, point),
                    f"I/O point '{point.address}' carrying a safety-related signal",
                    {"address": point.address, "signal_type": point.signal_type.value},
                )
            )

        return issues

    def _issue(
        self,
        object_type: ObjectType,
        object_id: uuid.UUID,
        object_label: str,
        subject: str,
        detail: dict[str, object],
    ) -> Issue:
        return Issue(
            code="io.safety_fail_state_undefined",
            discipline=_DISCIPLINE,
            # ERROR: the whole content of this finding is that nobody stated
            # what happens when the source is lost. Releasing anyway means the
            # behaviour of a safety-related signal on failure is whatever the
            # hardware defaults to, decided by nobody and reviewed by nobody.
            # Nothing is filled in here — a de-energise default would read
            # afterwards as an engineering decision somebody made.
            severity=ValidationSeverity.ERROR,
            message=(
                f"{subject} has no fail state recorded. The source documents never stated "
                "what it does when its source is lost."
            ),
            object_type=object_type,
            object_id=object_id,
            object_label=object_label,
            suggested_action=(
                "Record the fail state the specification states. If no document states one, "
                "raise a clarification rather than choosing a default."
            ),
            detail=dict(detail) | {"fail_state": FailState.UNDEFINED.value},
        )
