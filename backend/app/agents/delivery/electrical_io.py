"""Deterministic, read-only Electrical and I/O delivery audit."""

from __future__ import annotations

import uuid
from collections import defaultdict

from sqlalchemy import select

from app.agents.delivery.audit import DeliveryAuditResult, ReadOnlyDeliveryAgent, issue
from app.core.enums import FailState, ObjectType
from app.models.plant import AddressAssignment, Device, IOPoint, Signal
from app.orchestration.handlers import TaskContext


class ElectricalIOAgent(ReadOnlyDeliveryAgent):
    kind = "electrical_io_delivery"
    display_name = "Electrical and I/O Agent"
    location = "electrical_works"

    def inspect(self, context: TaskContext) -> DeliveryAuditResult:
        project_id = context.project.id
        devices = list(
            context.db.execute(select(Device).where(Device.project_id == project_id)).scalars()
        )
        signals = list(
            context.db.execute(select(Signal).where(Signal.project_id == project_id)).scalars()
        )
        points = list(
            context.db.execute(select(IOPoint).where(IOPoint.project_id == project_id)).scalars()
        )
        assignments = list(
            context.db.execute(
                select(AddressAssignment).where(AddressAssignment.project_id == project_id)
            ).scalars()
        )
        signals_by_id = {item.id: item for item in signals}
        points_by_id = {item.id: item for item in points}
        assignments_by_point: dict[uuid.UUID, list[AddressAssignment]] = defaultdict(list)
        assignments_by_signal = {item.signal_id: item for item in assignments}
        for assignment in assignments:
            if assignment.io_point_id is not None:
                assignments_by_point[assignment.io_point_id].append(assignment)

        issues = []
        for point_id, rows in assignments_by_point.items():
            if len(rows) < 2:
                continue
            point = points_by_id.get(point_id)
            if point is None:
                continue
            names = sorted(
                signals_by_id[row.signal_id].name for row in rows if row.signal_id in signals_by_id
            )
            issues.append(
                issue(
                    code="electrical.address_conflict",
                    severity="error",
                    title=f"I/O point {point.address} has duplicate assignments",
                    detail=f"Canonical assignments claim this point for: {', '.join(names)}.",
                    object_type=ObjectType.IO_POINT,
                    object_id=point.id,
                    reference=point.address,
                    requires_human_review=True,
                )
            )

        for assignment in assignments:
            signal = signals_by_id.get(assignment.signal_id)
            point = (
                points_by_id.get(assignment.io_point_id)
                if assignment.io_point_id is not None
                else None
            )
            if signal is None or point is None:
                continue
            mismatch = (
                signal.signal_type != point.signal_type or signal.direction != point.direction
            )
            if mismatch:
                issues.append(
                    issue(
                        code="electrical.incompatible_signal",
                        severity="error",
                        title=f"Signal {signal.name} is incompatible with {point.address}",
                        detail=(
                            f"Signal is {signal.signal_type.value}/{signal.direction.value}; "
                            f"point is {point.signal_type.value}/{point.direction.value}."
                        ),
                        object_type=ObjectType.ADDRESS_ASSIGNMENT,
                        object_id=assignment.id,
                        reference=f"{signal.name} -> {point.address}",
                        requires_human_review=True,
                    )
                )

        for signal in signals:
            signal_assignment = assignments_by_signal.get(signal.id)
            if (
                signal.is_required
                and signal.requires_terminal
                and (signal_assignment is None or not signal_assignment.is_resolved)
            ):
                issues.append(
                    issue(
                        code="electrical.required_signal_unassigned",
                        severity="warning",
                        title=f"Required signal {signal.name} has no resolved I/O point",
                        detail="No physical address is recorded; the agent does not invent one.",
                        object_type=ObjectType.SIGNAL,
                        object_id=signal.id,
                        reference=signal.name,
                    )
                )
            if not signal.description.strip():
                issues.append(
                    issue(
                        code="electrical.missing_description",
                        severity="warning",
                        title=f"Signal {signal.name} has no description",
                        detail="The canonical signal description is empty.",
                        object_type=ObjectType.SIGNAL,
                        object_id=signal.id,
                        reference=signal.name,
                    )
                )
            if signal.fail_state is FailState.UNDEFINED:
                issues.append(
                    issue(
                        code="electrical.missing_fail_state",
                        severity="warning",
                        title=f"Signal {signal.name} has no stated fail state",
                        detail=(
                            "The canonical fail state is undefined; no safe default was assumed."
                        ),
                        object_type=ObjectType.SIGNAL,
                        object_id=signal.id,
                        reference=signal.name,
                    )
                )

        for device in devices:
            if device.fail_state is FailState.UNDEFINED:
                issues.append(
                    issue(
                        code="electrical.device_fail_state_missing",
                        severity="warning",
                        title=f"Device {device.tag_prefix} has no stated fail state",
                        detail="The canonical device fail state is undefined.",
                        object_type=ObjectType.DEVICE,
                        object_id=device.id,
                        reference=device.tag_prefix,
                        requires_human_review=device.is_safety_related,
                    )
                )

        return DeliveryAuditResult(
            agent_kind=self.kind,
            location=self.location,
            scope_examined="Devices, signals, I/O points, and address assignments",
            issues=tuple(issues),
            counts={
                "devices": len(devices),
                "signals": len(signals),
                "io_points": len(points),
                "assignments": len(assignments),
            },
        )
