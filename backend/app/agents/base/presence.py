"""Agent identity, assignment and city-presence integration.

Agent definitions describe *roles*.  The city renders per-project
``AgentInstance`` rows, so presenting the definition label (``PLC Logic``) as
the instance name makes every worker look generic and leaves no stable identity
for an engineer to follow through the activity feed.

This module owns the presentational bridge between the durable workflow and the
city event stream:

* every core role receives a stable, unique callsign;
* a workflow provisions its complete role roster up front;
* tasks keep the id of the instance that owns them; and
* assignment, status and semantic-location events are emitted with that id.

None of this affects engineering correctness or permissions.  Locations and
presence are deliberately presentational, while task and run records remain the
authoritative audit trail.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.base.registry import spec_for
from app.core.enums import (
    AgentKind,
    AgentStatus,
    EventType,
    SemanticLocation,
    TaskKind,
    TaskStatus,
)
from app.models.agent import AgentDefinition, AgentInstance
from app.models.project import Project
from app.models.task import Task
from app.orchestration.events import record_event

# Callsigns are intentionally non-human: they are memorable without pretending
# that an autonomous worker is a licensed person.  Role labels remain explicit
# beside them, and every value is unique within the built-in roster.
_CALLSIGNS: dict[str, str] = {
    AgentKind.PROJECT_MANAGER.value: "Atlas",
    AgentKind.PLC_REVIEW.value: "Relay",
    AgentKind.INTERLOCK_REVIEW.value: "Sentinel",
    AgentKind.SENIOR_REVIEW.value: "Aegis",
    AgentKind.DELIVERY.value: "Courier",
    AgentKind.REQUIREMENTS_PLANNING.value: "Scribe",
    AgentKind.TAG_ARCHITECTURE.value: "Forge",
    AgentKind.PLC_LOGIC.value: "Vector",
}

_FALLBACK_CALLSIGNS: tuple[str, ...] = (
    "Nova",
    "Pioneer",
    "Beacon",
    "Orbit",
    "Harbor",
    "Summit",
    "Mosaic",
    "Echo",
)

_ROLE_FALLBACKS: dict[str, tuple[str, str, SemanticLocation]] = {
    AgentKind.DELIVERY.value: (
        "Delivery and Release",
        "Delivery Yard",
        SemanticLocation.RELEASE_LOGISTICS,
    ),
}

_TASK_LOCATIONS: dict[TaskKind, SemanticLocation] = {
    TaskKind.INVENTORY_FILES: SemanticLocation.PROJECT_INTAKE,
    TaskKind.PLAN_PROJECT: SemanticLocation.PROJECT_CORE,
    TaskKind.REVIEW_PLC: SemanticLocation.LADDER_LOGIC_LAB,
    TaskKind.REVIEW_INTERLOCKS: SemanticLocation.ELECTRICAL_WORKS,
    TaskKind.SENIOR_REVIEW: SemanticLocation.QA_CAMPUS,
    TaskKind.REQUEST_APPROVALS: SemanticLocation.APPROVAL_PLAZA,
    TaskKind.DELIVERY: SemanticLocation.DOCUMENTATION_QUARTER,
    TaskKind.EXTRACT_REQUIREMENTS: SemanticLocation.REQUIREMENTS_HALL,
    TaskKind.DESIGN_TAGS: SemanticLocation.TAG_FOUNDRY,
    TaskKind.DESIGN_PLC_ARCHITECTURE: SemanticLocation.AUTOMATION_CORE,
    TaskKind.GENERATE_LOGIC: SemanticLocation.LADDER_LOGIC_LAB,
}


def _kind_value(kind: AgentKind | str) -> str:
    return kind.value if isinstance(kind, AgentKind) else str(kind).strip()


def _fallback_callsign(kind: str) -> str:
    digest = hashlib.sha256(kind.encode("utf-8")).digest()
    return _FALLBACK_CALLSIGNS[int.from_bytes(digest[:2], "big") % len(_FALLBACK_CALLSIGNS)]


def agent_display_name(kind: AgentKind | str, role_label: str) -> str:
    """Return the stable, human-friendly identity for one role instance."""

    value = _kind_value(kind)
    callsign = _CALLSIGNS.get(value, _fallback_callsign(value))
    label = role_label.strip() or value.replace("_", " ").title()
    return f"{callsign} · {label}"[:120]


def _definition_metadata(
    kind: AgentKind | str,
) -> tuple[str, str, SemanticLocation]:
    value = _kind_value(kind)
    spec = spec_for(value)
    if spec is not None:
        return spec.display_name, spec.building, spec.home_location
    if value in _ROLE_FALLBACKS:
        return _ROLE_FALLBACKS[value]
    label = value.replace("_", " ").title()
    return label, "Delivery Yard", SemanticLocation.RELEASE_LOGISTICS


def home_location_for(kind: AgentKind | str) -> SemanticLocation:
    """Semantic home used when an instance has not acquired a location yet."""

    return _definition_metadata(kind)[2]


def ensure_agent_instance(
    db: Session,
    project: Project,
    kind: AgentKind | str,
) -> AgentInstance:
    """Get or create this project's stable instance of an agent role.

    The database intentionally permits one instance per project/definition.  A
    workflow still exposes many workers because it provisions every distinct
    role in its graph, rather than lazily creating only the role currently on
    the CPU.
    """

    value = _kind_value(kind)
    role_label, building, home = _definition_metadata(value)
    definition = db.execute(
        select(AgentDefinition).where(AgentDefinition.kind == value)
    ).scalar_one_or_none()
    if definition is None:
        definition = AgentDefinition(
            kind=value,
            display_name=role_label,
            description="",
            building=building,
            handles_task_kinds=[],
        )
        db.add(definition)
        db.flush()

    identity = agent_display_name(value, definition.display_name or role_label)
    instance = db.execute(
        select(AgentInstance).where(
            AgentInstance.project_id == project.id,
            AgentInstance.definition_id == definition.id,
        )
    ).scalar_one_or_none()
    if instance is None:
        instance = AgentInstance(
            project_id=project.id,
            definition_id=definition.id,
            kind=value,
            display_name=identity,
            status=AgentStatus.IDLE,
            location=home.value,
        )
        db.add(instance)
        db.flush()
    else:
        # Upgrade projects created before callsigns were introduced.  The id is
        # retained, so run/task history keeps pointing at the same worker.
        instance.display_name = identity
        if not instance.location:
            instance.location = home.value
        db.flush()
    return instance


def instance_for_task(
    db: Session,
    project: Project,
    kind: AgentKind | str,
    task_id: uuid.UUID | None,
) -> AgentInstance:
    """Resolve the assigned instance, falling back to the project's role row."""

    value = _kind_value(kind)
    task = db.get(Task, task_id) if task_id is not None else None
    if task is not None and task.agent_instance_id is not None:
        assigned = db.get(AgentInstance, task.agent_instance_id)
        if assigned is not None and assigned.project_id == project.id and assigned.kind == value:
            expected = agent_display_name(value, assigned.definition.display_name)
            if assigned.display_name != expected:
                assigned.display_name = expected
                db.flush()
            return assigned

    instance = ensure_agent_instance(db, project, value)
    if task is not None and task.project_id == project.id:
        task.agent_instance_id = instance.id
        db.flush()
    return instance


def announce_agent_presence(db: Session, instance: AgentInstance) -> None:
    """Publish an idle/active roster row and its semantic city location."""

    record_event(
        db,
        project_id=instance.project_id,
        event_type=EventType.AGENT_STATUS_CHANGED,
        agent_id=instance.id,
        task_id=instance.current_task_id,
        payload={
            "agent_id": str(instance.id),
            "agent_kind": instance.kind,
            "display_name": instance.display_name,
            "status": instance.status.value,
            "current_activity": instance.current_activity,
            "task_id": str(instance.current_task_id) if instance.current_task_id else None,
        },
    )
    if instance.location:
        record_event(
            db,
            project_id=instance.project_id,
            event_type=EventType.AGENT_LOCATION_CHANGED,
            agent_id=instance.id,
            task_id=instance.current_task_id,
            payload={
                "agent_id": str(instance.id),
                "agent_kind": instance.kind,
                "location": instance.location,
                "previous_location": "",
            },
        )


def provision_workflow_agents(
    db: Session,
    project: Project,
    tasks: Iterable[Task],
    *,
    announce: bool = True,
) -> list[AgentInstance]:
    """Assign all agent-owned tasks and expose the complete workflow roster."""

    materialized = list(tasks)
    instances: dict[str, AgentInstance] = {}
    for task in materialized:
        if not task.agent_kind:
            continue
        instance = instances.get(task.agent_kind)
        if instance is None:
            instance = ensure_agent_instance(db, project, task.agent_kind)
            instances[task.agent_kind] = instance
        task.agent_instance_id = instance.id
    db.flush()

    ordered = [instances[key] for key in sorted(instances)]
    if announce:
        for instance in ordered:
            announce_agent_presence(db, instance)
    return ordered


def set_agent_status(
    db: Session,
    instance: AgentInstance,
    status: AgentStatus,
    *,
    activity: str = "",
    task_id: uuid.UUID | None = None,
) -> None:
    """Update presence and emit the status event rendered by the city."""

    activity = activity[:255]
    if (
        instance.status is status
        and instance.current_activity == activity
        and instance.current_task_id == task_id
    ):
        return

    instance.status = status
    instance.current_activity = activity
    instance.current_task_id = task_id
    db.flush()

    record_event(
        db,
        project_id=instance.project_id,
        event_type=EventType.AGENT_STATUS_CHANGED,
        agent_id=instance.id,
        task_id=task_id,
        payload={
            "agent_id": str(instance.id),
            "agent_kind": instance.kind,
            "display_name": instance.display_name,
            "status": status.value,
            "current_activity": instance.current_activity,
            "task_id": str(task_id) if task_id else None,
        },
    )


def move_agent(
    db: Session,
    instance: AgentInstance,
    location: SemanticLocation | str,
    *,
    task_id: uuid.UUID | None = None,
) -> None:
    """Move an agent between semantic districts and emit the route endpoint."""

    value = location.value if isinstance(location, SemanticLocation) else str(location)
    if instance.location == value:
        return
    previous = instance.location
    instance.location = value[:80]
    db.flush()
    record_event(
        db,
        project_id=instance.project_id,
        event_type=EventType.AGENT_LOCATION_CHANGED,
        agent_id=instance.id,
        task_id=task_id,
        payload={
            "agent_id": str(instance.id),
            "agent_kind": instance.kind,
            "location": instance.location,
            "previous_location": previous,
        },
    )


def task_location(task: Task, instance: AgentInstance) -> SemanticLocation:
    """Where a task is performed, with the worker's home as a safe fallback."""

    explicit = _TASK_LOCATIONS.get(task.kind)
    if explicit is not None:
        return explicit
    try:
        return SemanticLocation(instance.location)
    except ValueError:
        return home_location_for(instance.kind)


def begin_task_presence(db: Session, task: Task, project: Project) -> AgentInstance | None:
    """Commit-ready assignment state recorded before a potentially long handler."""

    if not task.agent_kind:
        return None
    instance = instance_for_task(db, project, task.agent_kind, task.id)
    destination = task_location(task, instance)

    try:
        agent_kind = AgentKind(task.agent_kind)
    except ValueError:
        agent_kind = None
    if agent_kind is not None:
        record_event(
            db,
            project_id=project.id,
            event_type=EventType.TASK_ASSIGNED,
            agent_id=instance.id,
            task_id=task.id,
            payload={
                "task_id": str(task.id),
                "kind": task.kind.value,
                "title": task.title,
                "agent_kind": agent_kind.value,
                "location": destination.value,
                "agent_id": str(instance.id),
                "attempt": task.attempts,
            },
        )

    set_agent_status(
        db,
        instance,
        AgentStatus.WORKING,
        activity=task.title,
        task_id=task.id,
    )
    move_agent(db, instance, destination, task_id=task.id)
    return instance


def finish_task_presence(db: Session, task: Task) -> None:
    """Leave the worker in an honest resting state after its task outcome."""

    if task.agent_instance_id is None:
        return
    instance = db.get(AgentInstance, task.agent_instance_id)
    if instance is None:
        return

    if task.status is TaskStatus.AWAITING_APPROVAL:
        status = AgentStatus.WAITING
        activity = task.blocked_reason or "Awaiting an engineering decision"
        task_id: uuid.UUID | None = task.id
    elif task.status in {TaskStatus.BLOCKED, TaskStatus.FAILED}:
        status = AgentStatus.BLOCKED
        activity = task.blocked_reason or task.error_message or "Task needs attention"
        task_id = task.id
    else:
        status = AgentStatus.IDLE
        activity = ""
        task_id = None
    set_agent_status(db, instance, status, activity=activity, task_id=task_id)
