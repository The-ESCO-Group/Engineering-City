"""Named agent identity and the worker-to-city presence lifecycle."""

from __future__ import annotations

import uuid
from collections.abc import Callable

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.base.presence import agent_display_name, ensure_agent_instance
from app.agents.base.registry import CORE_AGENTS
from app.core.config import Settings
from app.core.enums import AgentKind, AgentStatus, EventType, TaskKind, TaskStatus
from app.database.session import get_session_factory
from app.models.agent import AgentInstance
from app.models.event import Event
from app.models.project import Project
from app.models.task import Task
from app.models.user import User
from app.orchestration import engine, handlers, worker
from app.orchestration.handlers import HandlerResult, TaskContext
from app.orchestration.queue import DatabaseTaskQueue, EnqueueRequest


def test_core_agent_callsigns_are_stable_unique_and_keep_the_role_label() -> None:
    first = [agent_display_name(spec.kind, spec.display_name) for spec in CORE_AGENTS]
    second = [agent_display_name(spec.kind, spec.display_name) for spec in CORE_AGENTS]
    every_kind = [agent_display_name(kind, kind.value) for kind in AgentKind]

    assert first == second
    assert len(first) == len(set(first))
    assert len(every_kind) == len(set(every_kind))
    assert all(" · " in name for name in first)
    assert all(spec.display_name in name for spec, name in zip(CORE_AGENTS, first, strict=True))


def test_start_run_provisions_named_roster_assignments_and_presence_events(
    db: Session,
    ingested_project: Project,
    settings: Settings,
    seeded: dict[str, User],
) -> None:
    summary = engine.start_run(
        db,
        ingested_project,
        settings,
        actor=seeded["engineer"],
    )
    db.commit()

    agent_tasks = [task for task in summary.tasks if task.agent_kind]
    expected_kinds = {task.agent_kind for task in agent_tasks}
    instances = list(
        db.execute(
            select(AgentInstance).where(AgentInstance.project_id == ingested_project.id)
        ).scalars()
    )

    assert {instance.kind for instance in instances} == expected_kinds
    assert len({instance.display_name for instance in instances}) == len(instances)
    assert all(instance.status is AgentStatus.IDLE for instance in instances)

    instance_by_kind = {instance.kind: instance for instance in instances}
    assert all(
        task.agent_instance_id == instance_by_kind[task.agent_kind].id for task in agent_tasks
    )

    # Re-provisioning a role retains its durable identity rather than spawning a
    # second generic worker on the next task or run.
    existing = instance_by_kind[AgentKind.PROJECT_MANAGER.value]
    assert ensure_agent_instance(db, ingested_project, AgentKind.PROJECT_MANAGER).id == existing.id

    events = list(
        db.execute(
            select(Event).where(Event.project_id == ingested_project.id).order_by(Event.seq)
        ).scalars()
    )
    announced_status_ids = {
        event.agent_id for event in events if event.event_type is EventType.AGENT_STATUS_CHANGED
    }
    announced_location_ids = {
        event.agent_id for event in events if event.event_type is EventType.AGENT_LOCATION_CHANGED
    }
    roster_ids = {instance.id for instance in instances}
    assert announced_status_ids == roster_ids
    assert announced_location_ids == roster_ids

    created_by_task = {
        event.task_id: event for event in events if event.event_type is EventType.TASK_CREATED
    }
    assert all(created_by_task[task.id].agent_id == task.agent_instance_id for task in agent_tasks)


@pytest.mark.parametrize(
    ("outcome", "expected_task_status", "expected_agent_status"),
    [
        (lambda: HandlerResult.completed(produced=1), TaskStatus.COMPLETED, AgentStatus.IDLE),
        (
            lambda: HandlerResult.awaiting_approval("Engineer decision required"),
            TaskStatus.AWAITING_APPROVAL,
            AgentStatus.WAITING,
        ),
        (
            lambda: HandlerResult.failed("synthetic_failure", "Synthetic handler failure"),
            TaskStatus.FAILED,
            AgentStatus.BLOCKED,
        ),
    ],
    ids=["completed-idle", "approval-waiting", "failed-blocked"],
)
def test_worker_commits_working_presence_before_handler_and_finishes_honestly(
    db: Session,
    project: Project,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    outcome: Callable[[], HandlerResult],
    expected_task_status: TaskStatus,
    expected_agent_status: AgentStatus,
) -> None:
    queue = DatabaseTaskQueue(db, settings)
    task = queue.enqueue(
        project.id,
        EnqueueRequest(
            kind=TaskKind.PLAN_PROJECT,
            title="Inspect the synthetic intake",
            idempotency_key=f"presence:{uuid.uuid4()}",
            agent_kind=AgentKind.PROJECT_MANAGER.value,
            max_attempts=1,
        ),
    )
    ensure_agent_instance(db, project, AgentKind.PROJECT_MANAGER)
    db.commit()

    claimed = queue.claim("presence-test-worker", limit=1)
    assert [item.id for item in claimed] == [task.id]
    db.commit()

    observed: dict[str, object] = {}

    def inspect_committed_presence(context: TaskContext) -> HandlerResult:
        # A second connection can see only committed state. This is the
        # regression assertion that keeps long provider calls visible live.
        with get_session_factory()() as observer:
            stored_task = observer.get(Task, context.task.id)
            assert stored_task is not None
            assert stored_task.agent_instance_id is not None
            instance = observer.get(AgentInstance, stored_task.agent_instance_id)
            assert instance is not None
            observed.update(
                status=instance.status,
                activity=instance.current_activity,
                current_task_id=instance.current_task_id,
                assigned_instance_id=stored_task.agent_instance_id,
                event_types=set(
                    observer.execute(
                        select(Event.event_type).where(Event.task_id == context.task.id)
                    ).scalars()
                ),
            )
        return outcome()

    monkeypatch.setitem(handlers._HANDLERS, TaskKind.PLAN_PROJECT, inspect_committed_presence)

    assert worker.run_one(db, claimed[0], settings) == expected_task_status.value
    db.commit()
    db.expire_all()

    stored_task = db.get(Task, task.id)
    assert stored_task is not None
    assert stored_task.status is expected_task_status
    assert observed == {
        "status": AgentStatus.WORKING,
        "activity": task.title,
        "current_task_id": task.id,
        "assigned_instance_id": stored_task.agent_instance_id,
        "event_types": {
            EventType.AGENT_LOCATION_CHANGED,
            EventType.AGENT_STATUS_CHANGED,
            EventType.TASK_ASSIGNED,
            EventType.TASK_STARTED,
        },
    }

    instance = db.get(AgentInstance, stored_task.agent_instance_id)
    assert instance is not None
    assert instance.status is expected_agent_status
    if expected_agent_status is AgentStatus.IDLE:
        assert instance.current_task_id is None
        assert instance.current_activity == ""
    else:
        assert instance.current_task_id == task.id
        assert instance.current_activity
