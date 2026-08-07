"""The workflow engine: the run graph, the control verbs, and the status rule.

Three things are being protected here.

The **shape of the run** — every parse waits for intake, the evidence graph waits
softly for the parses, and everything after it is a hard chain. Soft is not a
detail: one unreadable drawing must cost only itself, not the review of the other
eight documents.

The **refusals** — a second concurrent run over one project would interleave
findings and neither would be reproducible, so ``start_run`` says no rather than
quietly restarting.

The **status rule** — and in particular that a project with an outstanding
approval is never reported as complete. Telling an engineer a safety review was
finished when nobody had read the safety question is the worst thing this product
could do, so it has a test of its own.

The worker is driven by hand (``worker.tick`` / ``worker.drain``); no background
thread runs in tests, so every assertion below is about a state the test itself
caused.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

# Importing the step module is what registers the deterministic handlers. The
# application only does it when the in-process worker is enabled, and the worker
# is off in tests, so the registry would otherwise be empty and every task would
# block with "No handler is installed".
import app.orchestration.steps  # noqa: F401
from app.core.clock import utcnow
from app.core.config import Settings
from app.core.enums import (
    ApprovalGateKey,
    ApprovalKind,
    ApprovalStatus,
    AuditAction,
    EventType,
    GateStatus,
    ProjectStatus,
    TaskKind,
    TaskStatus,
)
from app.core.errors import ConflictError
from app.core.services import parsing
from app.database.session import get_session_factory
from app.engineering import gates
from app.models.approval import Approval
from app.models.audit import AuditEntry
from app.models.event import Event
from app.models.project import Project
from app.models.revision import ApprovalGate
from app.models.task import Task, TaskDependency
from app.models.user import User
from app.orchestration import engine, worker

WORKER_ID = "test-worker"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _start_run(
    db: Session,
    project: Project,
    settings: Settings,
    actor: User,
    *,
    file_ids: list[uuid.UUID] | None = None,
    note: str = "",
) -> engine.RunSummary:
    summary = engine.start_run(db, project, settings, actor=actor, file_ids=file_ids, note=note)
    db.commit()
    return summary


def _tick(db: Session, settings: Settings) -> worker.TickResult:
    """Run exactly one task, with the arranging session out of the way."""
    db.commit()
    result = worker.tick(settings, WORKER_ID)
    db.expire_all()
    return result


def _drain(db: Session, settings: Settings) -> worker.TickResult:
    db.commit()
    result = worker.drain(settings)
    db.expire_all()
    return result


def _tasks(db: Session, project: Project) -> list[Task]:
    return list(db.execute(select(Task).where(Task.project_id == project.id)).scalars())


def _only(tasks: list[Task], kind: TaskKind) -> Task:
    matches = [task for task in tasks if task.kind is kind]
    assert len(matches) == 1, f"expected exactly one {kind.value} task, got {len(matches)}"
    return matches[0]


def _of_kind(tasks: list[Task], kind: TaskKind) -> list[Task]:
    return [task for task in tasks if task.kind is kind]


def _edges(db: Session, task: Task) -> list[TaskDependency]:
    return list(
        db.execute(select(TaskDependency).where(TaskDependency.task_id == task.id)).scalars()
    )


def _depends_on(db: Session, task: Task) -> set[uuid.UUID]:
    return {edge.depends_on_id for edge in _edges(db, task)}


def _events(db: Session, project: Project, event_type: EventType) -> list[Event]:
    return list(
        db.execute(
            select(Event)
            .where(Event.project_id == project.id, Event.event_type == event_type)
            .order_by(Event.seq)
        ).scalars()
    )


def _audit(db: Session, project: Project, action: AuditAction) -> list[AuditEntry]:
    return list(
        db.execute(
            select(AuditEntry).where(
                AuditEntry.project_id == project.id, AuditEntry.action == action
            )
        ).scalars()
    )


def _seed_task(
    db: Session,
    project: Project,
    *,
    status: TaskStatus,
    key: str,
    title: str = "Seeded task",
    kind: TaskKind = TaskKind.PARSE_FILE,
    error_message: str = "",
) -> Task:
    task = Task(
        project_id=project.id,
        kind=kind,
        title=title,
        status=status,
        idempotency_key=key,
        error_message=error_message,
    )
    db.add(task)
    db.flush()
    return task


def _pending_approval(
    db: Session, project: Project, *, title: str = "Confirm the trip"
) -> Approval:
    approval = Approval(
        project_id=project.id,
        kind=ApprovalKind.SAFETY_RECOMMENDATION,
        status=ApprovalStatus.PENDING,
        title=title,
        question="Does the guard-door interlock need to trip the drive directly?",
    )
    db.add(approval)
    db.flush()
    return approval


def _make_running(db: Session, project: Project) -> None:
    """Put the project into a state ``refresh_project_status`` will act on."""
    project.status = ProjectStatus.RUNNING
    db.flush()


# ---------------------------------------------------------------------------
# start_run — the graph it builds
# ---------------------------------------------------------------------------
def test_start_run_creates_one_parse_task_per_analysable_file(
    db: Session, ingested_project: Project, settings: Settings, seeded: dict[str, User]
) -> None:
    """A file that gets no parse task is a file the review never looked at."""
    files = parsing.parseable_files(db, ingested_project.id)
    assert len(files) > 1, "the fixture project should carry several analysable files"

    summary = _start_run(db, ingested_project, settings, seeded["engineer"])

    parses = _of_kind(summary.tasks, TaskKind.PARSE_FILE)
    assert len(parses) == len(files)
    assert {task.payload["file_id"] for task in parses} == {str(record.id) for record in files}
    for record in files:
        assert any(record.original_filename in task.title for task in parses), (
            f"{record.original_filename} has no parse task"
        )


def test_start_run_creates_the_full_prelude_and_tail_exactly_once(
    db: Session, ingested_project: Project, settings: Settings, seeded: dict[str, User]
) -> None:
    summary = _start_run(db, ingested_project, settings, seeded["engineer"])

    for kind in (
        TaskKind.INVENTORY_FILES,
        TaskKind.BUILD_EVIDENCE_GRAPH,
        TaskKind.PLAN_PROJECT,
        TaskKind.SENIOR_REVIEW,
        TaskKind.REQUEST_APPROVALS,
        TaskKind.DELIVERY,
        TaskKind.FINALIZE_PROJECT,
    ):
        _only(summary.tasks, kind)

    assert summary.task_count == len(summary.tasks)
    assert all(task.run_id == summary.run_id for task in summary.tasks)
    assert all(task.status is TaskStatus.QUEUED for task in summary.tasks)


def test_start_run_moves_the_project_to_running(
    db: Session, ingested_project: Project, settings: Settings, seeded: dict[str, User]
) -> None:
    ingested_project.failure_reason = "a previous run fell over"
    db.flush()

    _start_run(db, ingested_project, settings, seeded["engineer"])

    assert ingested_project.status is ProjectStatus.RUNNING
    assert ingested_project.is_paused is False
    assert ingested_project.paused_at is None
    assert ingested_project.completed_at is None
    assert ingested_project.failure_reason == ""


def test_start_run_records_the_decision_in_the_audit_trail(
    db: Session, ingested_project: Project, settings: Settings, seeded: dict[str, User]
) -> None:
    files = parsing.parseable_files(db, ingested_project.id)
    summary = _start_run(
        db, ingested_project, settings, seeded["engineer"], note="client requested a re-review"
    )

    entries = _audit(db, ingested_project, AuditAction.ANALYSIS_STARTED)
    assert len(entries) == 1
    entry = entries[0]
    assert entry.actor_user_id == seeded["engineer"].id
    assert entry.target_id == ingested_project.id
    assert entry.details["run_id"] == str(summary.run_id)
    assert entry.details["file_count"] == len(files)
    assert entry.details["task_count"] == summary.task_count
    assert entry.details["note"] == "client requested a re-review"


def test_start_run_announces_the_run_then_every_task_it_created(
    db: Session, ingested_project: Project, settings: Settings, seeded: dict[str, User]
) -> None:
    """A client replaying the log must see the run open before its tasks appear."""
    summary = _start_run(db, ingested_project, settings, seeded["engineer"])

    started = _events(db, ingested_project, EventType.WORKFLOW_STARTED)
    assert len(started) == 1
    assert started[0].payload["run_id"] == str(summary.run_id)
    assert started[0].payload["task_count"] == summary.task_count
    assert started[0].actor_user_id == seeded["engineer"].id

    created = _events(db, ingested_project, EventType.TASK_CREATED)
    assert {event.payload["task_id"] for event in created} == {
        str(task.id) for task in summary.tasks
    }
    assert {event.task_id for event in created} == {task.id for task in summary.tasks}
    assert all(event.payload["status"] == TaskStatus.QUEUED.value for event in created)
    assert min(event.seq for event in created) > started[0].seq


def test_every_parse_waits_for_the_inventory_and_the_tail_is_a_hard_chain(
    db: Session, ingested_project: Project, settings: Settings, seeded: dict[str, User]
) -> None:
    summary = _start_run(db, ingested_project, settings, seeded["engineer"])
    tasks = summary.tasks

    intake = _only(tasks, TaskKind.INVENTORY_FILES)
    parses = _of_kind(tasks, TaskKind.PARSE_FILE)
    graph = _only(tasks, TaskKind.BUILD_EVIDENCE_GRAPH)
    plan = _only(tasks, TaskKind.PLAN_PROJECT)
    senior = _only(tasks, TaskKind.SENIOR_REVIEW)
    approvals = _only(tasks, TaskKind.REQUEST_APPROVALS)
    delivery = _only(tasks, TaskKind.DELIVERY)
    finalize = _only(tasks, TaskKind.FINALIZE_PROJECT)

    assert _depends_on(db, intake) == set()
    for parse in parses:
        assert _depends_on(db, parse) == {intake.id}
        assert all(edge.hard is True for edge in _edges(db, parse))

    assert _depends_on(db, graph) == {parse.id for parse in parses}
    assert _depends_on(db, plan) == {graph.id}
    assert _depends_on(db, senior) == {plan.id}
    assert _depends_on(db, approvals) == {senior.id}
    assert _depends_on(db, delivery) == {approvals.id}
    assert _depends_on(db, finalize) == {delivery.id}

    for task, expected in (
        (plan, graph),
        (senior, plan),
        (approvals, senior),
        (delivery, approvals),
        (finalize, delivery),
    ):
        edges = _edges(db, task)
        assert [edge.depends_on_id for edge in edges] == [expected.id]
        assert all(edge.hard is True for edge in edges)


def test_the_evidence_graph_depends_softly_on_every_parse(
    db: Session, ingested_project: Project, settings: Settings, seeded: dict[str, User]
) -> None:
    """One unreadable document must not stop the review of the rest.

    A hard edge here would mean a scanned drawing with no text layer could take
    the whole cross-file comparison down with it.
    """
    summary = _start_run(db, ingested_project, settings, seeded["engineer"])
    graph = _only(summary.tasks, TaskKind.BUILD_EVIDENCE_GRAPH)

    edges = _edges(db, graph)
    assert len(edges) == len(_of_kind(summary.tasks, TaskKind.PARSE_FILE))
    assert edges, "the graph must still wait for the parses it can get"
    assert all(edge.hard is False for edge in edges)


# ---------------------------------------------------------------------------
# start_run — the refusals
# ---------------------------------------------------------------------------
def test_start_run_refuses_an_archived_project(
    db: Session, ingested_project: Project, settings: Settings, seeded: dict[str, User]
) -> None:
    ingested_project.status = ProjectStatus.ARCHIVED
    db.commit()

    with pytest.raises(ConflictError, match="read-only"):
        engine.start_run(db, ingested_project, settings, actor=seeded["engineer"])
    db.rollback()

    assert _tasks(db, ingested_project) == []


def test_start_run_refuses_a_second_concurrent_run(
    db: Session, ingested_project: Project, settings: Settings, seeded: dict[str, User]
) -> None:
    """Two runs over one project would interleave findings and neither would
    be reproducible."""
    first = _start_run(db, ingested_project, settings, seeded["engineer"])

    with pytest.raises(ConflictError, match="already in progress"):
        engine.start_run(db, ingested_project, settings, actor=seeded["engineer"])
    db.rollback()

    assert {task.id for task in _tasks(db, ingested_project)} == {task.id for task in first.tasks}
    assert len(_events(db, ingested_project, EventType.WORKFLOW_STARTED)) == 1


@pytest.mark.parametrize(
    "active_status",
    [
        TaskStatus.QUEUED,
        TaskStatus.RUNNING,
        TaskStatus.BLOCKED,
        TaskStatus.AWAITING_APPROVAL,
    ],
)
def test_awaiting_review_cannot_start_over_any_nonterminal_workflow_task(
    db: Session,
    ingested_project: Project,
    settings: Settings,
    seeded: dict[str, User],
    active_status: TaskStatus,
) -> None:
    first = _start_run(db, ingested_project, settings, seeded["engineer"])
    active = first.tasks[0]
    for task in first.tasks:
        task.status = TaskStatus.CANCELLED
        task.finished_at = utcnow()
    active.status = active_status
    active.finished_at = None
    ingested_project.status = ProjectStatus.AWAITING_REVIEW
    db.commit()

    with pytest.raises(ConflictError, match="still active"):
        engine.start_run(db, ingested_project, settings, actor=seeded["engineer"])
    db.rollback()

    assert {task.id for task in _tasks(db, ingested_project)} == {task.id for task in first.tasks}
    assert len(_events(db, ingested_project, EventType.WORKFLOW_STARTED)) == 1


def test_awaiting_review_cannot_start_over_a_pending_gate_decision(
    db: Session, ingested_project: Project, settings: Settings, seeded: dict[str, User]
) -> None:
    first = _start_run(db, ingested_project, settings, seeded["engineer"])
    for task in first.tasks:
        task.status = TaskStatus.CANCELLED
        task.finished_at = utcnow()
    request = Approval(
        project_id=ingested_project.id,
        kind=ApprovalKind.GATE_DECISION,
        status=ApprovalStatus.PENDING,
        gate_key=ApprovalGateKey.PROJECT_PROFILE,
        revision_number=ingested_project.current_revision_number,
        title="Approval gate: Project profile",
        question="Is this profile fit to proceed?",
    )
    db.add(request)
    ingested_project.status = ProjectStatus.AWAITING_REVIEW
    db.commit()

    with pytest.raises(ConflictError, match="still active"):
        engine.start_run(db, ingested_project, settings, actor=seeded["engineer"])
    db.rollback()

    assert {task.id for task in _tasks(db, ingested_project)} == {task.id for task in first.tasks}
    assert len(_events(db, ingested_project, EventType.WORKFLOW_STARTED)) == 1


def test_awaiting_review_can_restart_after_all_work_and_gate_requests_are_terminal(
    db: Session, ingested_project: Project, settings: Settings, seeded: dict[str, User]
) -> None:
    first = _start_run(db, ingested_project, settings, seeded["engineer"])
    for task in first.tasks:
        task.status = TaskStatus.CANCELLED
        task.finished_at = utcnow()
    ingested_project.status = ProjectStatus.AWAITING_REVIEW
    db.commit()

    second = _start_run(db, ingested_project, settings, seeded["engineer"])

    assert second.run_id != first.run_id
    assert {task.id for task in second.tasks}.isdisjoint({task.id for task in first.tasks})


def test_start_run_refreshes_a_stale_project_before_creating_a_second_graph(
    db: Session, ingested_project: Project, settings: Settings, seeded: dict[str, User]
) -> None:
    """A request loaded before the winning start must observe that winner."""
    # End the arranging read transaction while deliberately retaining this
    # Session's stale, expire_on_commit=False Project identity.
    db.commit()
    with get_session_factory()() as winner:
        current_project = winner.get(Project, ingested_project.id)
        current_actor = winner.get(User, seeded["engineer"].id)
        assert current_project is not None
        assert current_actor is not None
        first = engine.start_run(winner, current_project, settings, actor=current_actor)
        winner.commit()
        first_task_ids = {task.id for task in first.tasks}

    assert ingested_project.status is not ProjectStatus.RUNNING, "the caller must be stale"
    with pytest.raises(ConflictError, match="already in progress"):
        engine.start_run(db, ingested_project, settings, actor=seeded["engineer"])
    db.rollback()

    db.expire_all()
    assert {task.id for task in _tasks(db, ingested_project)} == first_task_ids
    assert len(_events(db, ingested_project, EventType.WORKFLOW_STARTED)) == 1


def test_start_run_refuses_a_project_with_nothing_to_analyse(
    db: Session, project: Project, settings: Settings, seeded: dict[str, User]
) -> None:
    """ "No findings" and "nothing to look at" are different answers."""
    with pytest.raises(ConflictError, match="Upload engineering documents"):
        engine.start_run(db, project, settings, actor=seeded["engineer"])
    db.rollback()

    assert project.status is ProjectStatus.DRAFT
    assert _tasks(db, project) == []


def test_start_run_refuses_when_the_selected_files_do_not_exist(
    db: Session, ingested_project: Project, settings: Settings, seeded: dict[str, User]
) -> None:
    with pytest.raises(ConflictError, match="no files that can be analysed"):
        engine.start_run(
            db, ingested_project, settings, actor=seeded["engineer"], file_ids=[uuid.uuid4()]
        )
    db.rollback()

    assert _tasks(db, ingested_project) == []


def test_start_run_refuses_a_project_that_is_still_ingesting(
    db: Session, ingested_project: Project, settings: Settings, seeded: dict[str, User]
) -> None:
    ingested_project.status = ProjectStatus.INGESTING
    db.commit()

    with pytest.raises(ConflictError, match="cannot be started while the project is 'ingesting'"):
        engine.start_run(db, ingested_project, settings, actor=seeded["engineer"])
    db.rollback()


def test_file_ids_restricts_the_run_to_the_selected_files(
    db: Session, ingested_project: Project, settings: Settings, seeded: dict[str, User]
) -> None:
    files = parsing.parseable_files(db, ingested_project.id)
    chosen = files[:2]

    summary = _start_run(
        db,
        ingested_project,
        settings,
        seeded["engineer"],
        file_ids=[record.id for record in chosen],
    )

    parses = _of_kind(summary.tasks, TaskKind.PARSE_FILE)
    assert {task.payload["file_id"] for task in parses} == {str(record.id) for record in chosen}

    excluded = {str(record.id) for record in files[2:]}
    assert excluded, "the fixture must have more files than were selected"
    assert excluded.isdisjoint({task.payload["file_id"] for task in parses})

    entry = _audit(db, ingested_project, AuditAction.ANALYSIS_STARTED)[0]
    assert entry.details["file_count"] == len(chosen)


# ---------------------------------------------------------------------------
# A second run over the same project
# ---------------------------------------------------------------------------
def test_a_run_started_after_a_cancellation_gets_a_fresh_graph(
    db: Session, ingested_project: Project, settings: Settings, seeded: dict[str, User]
) -> None:
    """The run id is part of every idempotency key, so the second run must not
    silently adopt the first run's rows."""
    first = _start_run(db, ingested_project, settings, seeded["engineer"])
    engine.cancel(db, ingested_project, settings, actor=seeded["engineer"], reason="restarting")
    db.commit()

    second = _start_run(db, ingested_project, settings, seeded["engineer"])

    assert second.run_id != first.run_id
    assert {task.id for task in second.tasks}.isdisjoint({task.id for task in first.tasks})
    assert {task.idempotency_key for task in second.tasks}.isdisjoint(
        {task.idempotency_key for task in first.tasks}
    )
    assert len(second.tasks) == len(first.tasks)
    assert all(task.status is TaskStatus.QUEUED for task in second.tasks)
    assert all(task.status is TaskStatus.CANCELLED for task in first.tasks)


def test_a_run_started_after_completion_gets_a_fresh_graph(
    db: Session, ingested_project: Project, settings: Settings, seeded: dict[str, User]
) -> None:
    first = _start_run(db, ingested_project, settings, seeded["engineer"])
    for task in first.tasks:
        task.status = TaskStatus.COMPLETED
    db.flush()
    assert engine.refresh_project_status(db, ingested_project) is ProjectStatus.COMPLETED
    db.commit()

    second = _start_run(db, ingested_project, settings, seeded["engineer"])

    assert second.run_id != first.run_id
    assert {task.id for task in second.tasks}.isdisjoint({task.id for task in first.tasks})
    assert ingested_project.status is ProjectStatus.RUNNING
    assert ingested_project.completed_at is None
    assert all(task.status is TaskStatus.COMPLETED for task in first.tasks), (
        "a new run must not retract what the previous one found"
    )


# ---------------------------------------------------------------------------
# pause / resume
# ---------------------------------------------------------------------------
def test_pause_records_the_pause_and_says_why(
    db: Session, ingested_project: Project, settings: Settings, seeded: dict[str, User]
) -> None:
    _start_run(db, ingested_project, settings, seeded["engineer"])

    engine.pause(db, ingested_project, actor=seeded["engineer"], reason="waiting on the client")
    db.commit()

    assert ingested_project.is_paused is True
    assert ingested_project.paused_at is not None
    assert ingested_project.status is ProjectStatus.RUNNING, "pause is not a status change"

    entries = _audit(db, ingested_project, AuditAction.ANALYSIS_PAUSED)
    assert len(entries) == 1
    assert entries[0].details["reason"] == "waiting on the client"
    assert entries[0].actor_user_id == seeded["engineer"].id

    events = _events(db, ingested_project, EventType.WORKFLOW_PAUSED)
    assert len(events) == 1
    assert events[0].payload["reason"] == "waiting on the client"


def test_pausing_an_already_paused_project_is_refused(
    db: Session, ingested_project: Project, settings: Settings, seeded: dict[str, User]
) -> None:
    _start_run(db, ingested_project, settings, seeded["engineer"])
    engine.pause(db, ingested_project, actor=seeded["engineer"])
    db.commit()

    with pytest.raises(ConflictError, match="already paused"):
        engine.pause(db, ingested_project, actor=seeded["engineer"])
    db.rollback()

    assert len(_audit(db, ingested_project, AuditAction.ANALYSIS_PAUSED)) == 1


def test_pause_refreshes_a_stale_project_and_does_not_emit_a_duplicate_transition(
    db: Session, ingested_project: Project, settings: Settings, seeded: dict[str, User]
) -> None:
    _start_run(db, ingested_project, settings, seeded["engineer"])
    with get_session_factory()() as winner:
        current_project = winner.get(Project, ingested_project.id)
        current_actor = winner.get(User, seeded["engineer"].id)
        assert current_project is not None
        assert current_actor is not None
        engine.pause(winner, current_project, actor=current_actor, reason="first pause")
        winner.commit()

    assert ingested_project.is_paused is False, "the caller must be stale"
    with pytest.raises(ConflictError, match="already paused"):
        engine.pause(db, ingested_project, actor=seeded["engineer"], reason="duplicate")
    db.rollback()

    db.expire_all()
    assert ingested_project.is_paused is True
    assert len(_events(db, ingested_project, EventType.WORKFLOW_PAUSED)) == 1


def test_pausing_a_project_that_is_not_running_is_refused(
    db: Session, project: Project, seeded: dict[str, User]
) -> None:
    with pytest.raises(ConflictError, match="Only a running project can be paused"):
        engine.pause(db, project, actor=seeded["engineer"])
    db.rollback()

    assert project.is_paused is False


def test_a_paused_project_hands_the_worker_nothing(
    db: Session, ingested_project: Project, settings: Settings, seeded: dict[str, User]
) -> None:
    summary = _start_run(db, ingested_project, settings, seeded["engineer"])
    engine.pause(db, ingested_project, actor=seeded["engineer"])

    result = _tick(db, settings)

    assert result.claimed == 0
    assert all(task.status is TaskStatus.QUEUED for task in summary.tasks)
    assert all(task.locked_by is None for task in summary.tasks)


def test_resume_lets_the_worker_claim_again(
    db: Session, ingested_project: Project, settings: Settings, seeded: dict[str, User]
) -> None:
    summary = _start_run(db, ingested_project, settings, seeded["engineer"])
    engine.pause(db, ingested_project, actor=seeded["engineer"])
    assert _tick(db, settings).claimed == 0

    engine.resume(db, ingested_project, actor=seeded["engineer"], reason="client replied")
    db.commit()
    assert ingested_project.is_paused is False
    assert ingested_project.paused_at is None

    result = _tick(db, settings)
    assert result.claimed == 1
    intake = _only(summary.tasks, TaskKind.INVENTORY_FILES)
    assert intake.status is TaskStatus.COMPLETED

    entries = _audit(db, ingested_project, AuditAction.ANALYSIS_RESUMED)
    assert len(entries) == 1
    assert entries[0].details["reason"] == "client replied"
    assert len(_events(db, ingested_project, EventType.WORKFLOW_RESUMED)) == 1


def test_resuming_a_project_that_is_not_paused_is_refused(
    db: Session, ingested_project: Project, settings: Settings, seeded: dict[str, User]
) -> None:
    _start_run(db, ingested_project, settings, seeded["engineer"])

    with pytest.raises(ConflictError, match="not paused"):
        engine.resume(db, ingested_project, actor=seeded["engineer"])
    db.rollback()

    assert _events(db, ingested_project, EventType.WORKFLOW_RESUMED) == []


def test_resume_refreshes_a_stale_project_and_does_not_emit_a_duplicate_transition(
    db: Session, ingested_project: Project, settings: Settings, seeded: dict[str, User]
) -> None:
    _start_run(db, ingested_project, settings, seeded["engineer"])
    engine.pause(db, ingested_project, actor=seeded["engineer"])
    db.commit()

    with get_session_factory()() as winner:
        current_project = winner.get(Project, ingested_project.id)
        current_actor = winner.get(User, seeded["engineer"].id)
        assert current_project is not None
        assert current_actor is not None
        engine.resume(winner, current_project, actor=current_actor, reason="first resume")
        winner.commit()

    assert ingested_project.is_paused is True, "the caller must be stale"
    with pytest.raises(ConflictError, match="not paused"):
        engine.resume(db, ingested_project, actor=seeded["engineer"], reason="duplicate")
    db.rollback()

    db.expire_all()
    assert ingested_project.is_paused is False
    assert len(_events(db, ingested_project, EventType.WORKFLOW_RESUMED)) == 1


# ---------------------------------------------------------------------------
# cancel
# ---------------------------------------------------------------------------
def test_cancel_stops_every_unfinished_task_and_says_why(
    db: Session, ingested_project: Project, settings: Settings, seeded: dict[str, User]
) -> None:
    summary = _start_run(db, ingested_project, settings, seeded["engineer"])

    stopped = engine.cancel(
        db, ingested_project, settings, actor=seeded["engineer"], reason="client withdrew"
    )
    db.commit()

    assert stopped == len(summary.tasks)
    for task in summary.tasks:
        assert task.status is TaskStatus.CANCELLED
        assert task.blocked_reason == "client withdrew"
        assert task.finished_at is not None
        assert task.locked_by is None

    assert ingested_project.status is ProjectStatus.PLANNED
    assert ingested_project.is_paused is False

    entries = _audit(db, ingested_project, AuditAction.ANALYSIS_CANCELLED)
    assert len(entries) == 1
    assert entries[0].details["cancelled_tasks"] == stopped
    events = _events(db, ingested_project, EventType.WORKFLOW_CANCELLED)
    assert len(events) == 1
    assert events[0].payload["task_count"] == stopped


def test_cancel_closes_every_gate_request_and_recovery_cannot_resurrect_it(
    db: Session, ingested_project: Project, settings: Settings, seeded: dict[str, User]
) -> None:
    """A cancelled run stays cancelled even when restart reconciliation runs."""
    summary = _start_run(db, ingested_project, settings, seeded["engineer"])
    gate = next(
        item
        for item in gates.ensure_gates(db, project=ingested_project)
        if item.gate_key is ApprovalGateKey.PROJECT_PROFILE
    )
    gate.status = GateStatus.AWAITING_DECISION
    gate_task = Task(
        project_id=ingested_project.id,
        run_id=summary.run_id,
        kind=TaskKind.EVALUATE_APPROVAL_GATE,
        title="Evaluate project profile gate",
        status=TaskStatus.AWAITING_APPROVAL,
        attempts=1,
        payload={"gate_key": ApprovalGateKey.PROJECT_PROFILE.value},
        idempotency_key=f"{summary.run_id}:test-gate",
    )
    db.add(gate_task)
    requests = [
        Approval(
            project_id=ingested_project.id,
            gate_key=ApprovalGateKey.PROJECT_PROFILE,
            kind=ApprovalKind.GATE_DECISION,
            status=ApprovalStatus.PENDING,
            revision_number=ingested_project.current_revision_number,
            title="Approval gate: Project profile",
            question="Is this profile fit to proceed?",
        )
        for _ in range(2)
    ]
    db.add_all(requests)
    db.flush()
    gate.approval_id = requests[0].id
    db.commit()

    stopped = engine.cancel(
        db,
        ingested_project,
        settings,
        actor=seeded["engineer"],
        reason="The client withdrew this run.",
    )
    db.commit()

    assert stopped == len(summary.tasks) + 1
    db.expire_all()
    stored_gate_task = db.get(Task, gate_task.id)
    stored_gate = db.get(ApprovalGate, gate.id)
    assert stored_gate_task is not None and stored_gate_task.status is TaskStatus.CANCELLED
    assert stored_gate is not None and stored_gate.approval_id is None
    for request in requests:
        stored = db.get(Approval, request.id)
        assert stored is not None
        assert stored.status is ApprovalStatus.CANCELLED
        assert stored.decision is None
        assert stored.decided_by_id is None
        assert stored.decided_at is not None
        assert "client withdrew" in stored.decision_notes.lower()

    entry = _audit(db, ingested_project, AuditAction.ANALYSIS_CANCELLED)[-1]
    assert entry.details["cancelled_gate_approvals"] == 2
    assert set(entry.details["cancelled_gate_approval_ids"]) == {
        str(request.id) for request in requests
    }
    assert _events(db, ingested_project, EventType.APPROVAL_DECIDED) == []
    assert _audit(db, ingested_project, AuditAction.APPROVAL_DECIDED) == []

    # Both the direct repair hook and startup recovery used to turn the gate
    # task live again from the durable gate projection.
    assert engine.reconcile_gate_tasks(db, ingested_project) == 0
    db.commit()
    assert worker.recover(settings) == 0
    assert worker.drain(settings).claimed == 0

    db.expire_all()
    assert db.get(Task, gate_task.id).status is TaskStatus.CANCELLED  # type: ignore[union-attr]
    assert (
        db.scalar(
            select(Approval.id).where(
                Approval.project_id == ingested_project.id,
                Approval.kind == ApprovalKind.GATE_DECISION,
                Approval.status == ApprovalStatus.PENDING,
            )
        )
        is None
    )


def test_restart_requeues_a_failed_gate_task_when_the_gate_is_authoritatively_skipped(
    db: Session, project: Project, settings: Settings
) -> None:
    gate = next(
        item
        for item in gates.ensure_gates(db, project=project)
        if item.gate_key is ApprovalGateKey.DEVICES_IO_TAGS
    )
    gate.is_enabled = False
    gate.disabled_reason = "This discipline is explicitly outside the engagement."
    task = Task(
        project_id=project.id,
        run_id=uuid.uuid4(),
        kind=TaskKind.EVALUATE_APPROVAL_GATE,
        title="Evaluate devices, I/O and tags gate",
        status=TaskStatus.FAILED,
        attempts=1,
        error_code="worker_crashed",
        error_message="The previous process stopped during finalization.",
        payload={"gate_key": ApprovalGateKey.DEVICES_IO_TAGS.value},
        idempotency_key=f"restart-gate:{uuid.uuid4()}",
    )
    db.add(task)
    db.commit()

    assert worker.recover(settings) == 0
    db.expire_all()
    recovered = db.get(Task, task.id)
    assert recovered is not None
    assert recovered.status is TaskStatus.QUEUED
    assert recovered.error_code == ""

    tick = worker.tick(settings, WORKER_ID)
    assert tick.completed == 1
    db.expire_all()
    assert db.get(Task, task.id).status is TaskStatus.COMPLETED  # type: ignore[union-attr]


def test_restart_reconciliation_visits_project_roots_in_deterministic_order(
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_ids = [
        uuid.UUID("eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"),
        uuid.UUID("11111111-1111-1111-1111-111111111111"),
        uuid.UUID("99999999-9999-9999-9999-999999999999"),
    ]
    # Insert in deliberately non-sorted order; SQL result order is not a lock
    # ordering guarantee on either supported database.
    for index, project_id in enumerate(project_ids):
        db.add(
            Project(
                id=project_id,
                name=f"Recovery order project {index}",
                status=ProjectStatus.DRAFT,
            )
        )
        db.add(
            Task(
                project_id=project_id,
                run_id=uuid.uuid4(),
                kind=TaskKind.EVALUATE_APPROVAL_GATE,
                title="Evaluate project profile gate",
                status=TaskStatus.QUEUED,
                payload={"gate_key": ApprovalGateKey.PROJECT_PROFILE.value},
                idempotency_key=f"recovery-order:{index}",
            )
        )
    db.commit()

    visited: list[uuid.UUID] = []

    def record_visit(_: Session, current_project: Project, **__: object) -> int:
        visited.append(current_project.id)
        return 0

    monkeypatch.setattr(engine, "reconcile_gate_tasks", record_visit)

    assert engine.reconcile_all_gate_tasks(db) == 0
    assert visited == sorted(project_ids, key=str)


def test_cancel_leaves_completed_work_alone(
    db: Session, ingested_project: Project, settings: Settings, seeded: dict[str, User]
) -> None:
    """Cancelling a run is not the same as retracting what it found."""
    summary = _start_run(db, ingested_project, settings, seeded["engineer"])
    assert _tick(db, settings).completed == 1

    intake = _only(summary.tasks, TaskKind.INVENTORY_FILES)
    assert intake.status is TaskStatus.COMPLETED
    finished_at = intake.finished_at
    result = dict(intake.result)

    stopped = engine.cancel(db, ingested_project, settings, actor=seeded["engineer"])
    db.commit()

    assert stopped == len(summary.tasks) - 1
    assert intake.status is TaskStatus.COMPLETED
    assert intake.finished_at == finished_at
    assert intake.result == result
    assert intake.blocked_reason == ""

    others = [task for task in summary.tasks if task.id != intake.id]
    assert all(task.status is TaskStatus.CANCELLED for task in others)
    assert all(task.blocked_reason == "The run was cancelled." for task in others)


def test_cancelling_a_run_with_nothing_left_to_stop_changes_no_status(
    db: Session, ingested_project: Project, settings: Settings, seeded: dict[str, User]
) -> None:
    summary = _start_run(db, ingested_project, settings, seeded["engineer"])
    for task in summary.tasks:
        task.status = TaskStatus.COMPLETED
    db.flush()
    engine.refresh_project_status(db, ingested_project)
    db.commit()
    assert ingested_project.status is ProjectStatus.COMPLETED

    stopped = engine.cancel(db, ingested_project, settings, actor=seeded["engineer"])
    db.commit()

    assert stopped == 0
    assert ingested_project.status is ProjectStatus.COMPLETED


# ---------------------------------------------------------------------------
# refresh_project_status — the truth table
# ---------------------------------------------------------------------------
def test_every_task_complete_and_nothing_pending_completes_the_project(
    db: Session, project: Project
) -> None:
    _make_running(db, project)
    _seed_task(db, project, status=TaskStatus.COMPLETED, key="a")
    _seed_task(db, project, status=TaskStatus.COMPLETED, key="b")

    assert engine.refresh_project_status(db, project) is ProjectStatus.COMPLETED
    assert project.completed_at is not None
    assert len(_events(db, project, EventType.PROJECT_COMPLETED)) == 1


@pytest.mark.parametrize("status", [TaskStatus.QUEUED, TaskStatus.RUNNING, TaskStatus.BLOCKED])
def test_any_task_still_in_flight_keeps_the_project_running(
    db: Session, project: Project, status: TaskStatus
) -> None:
    project.status = ProjectStatus.PLANNED
    db.flush()
    _seed_task(db, project, status=TaskStatus.COMPLETED, key="done")
    _seed_task(db, project, status=status, key="live")

    assert engine.refresh_project_status(db, project) is ProjectStatus.RUNNING
    assert project.completed_at is None


def test_a_task_waiting_on_a_human_puts_the_project_in_review(
    db: Session, project: Project
) -> None:
    _make_running(db, project)
    _seed_task(db, project, status=TaskStatus.COMPLETED, key="done")
    _seed_task(db, project, status=TaskStatus.AWAITING_APPROVAL, key="gate")

    assert engine.refresh_project_status(db, project) is ProjectStatus.AWAITING_REVIEW
    assert project.completed_at is None


def test_a_pending_approval_stops_the_project_being_called_complete(
    db: Session, project: Project
) -> None:
    """The single most important assertion about this function.

    Every task is finished. Nothing is running. The only thing outstanding is a
    human decision on a safety recommendation — and reporting the review complete
    while nobody has read that question would be the worst thing this product
    could do.
    """
    _make_running(db, project)
    _seed_task(db, project, status=TaskStatus.COMPLETED, key="a")
    _seed_task(db, project, status=TaskStatus.COMPLETED, key="b")
    _pending_approval(db, project)

    status = engine.refresh_project_status(db, project)

    assert status is ProjectStatus.AWAITING_REVIEW
    assert project.status is not ProjectStatus.COMPLETED
    assert project.completed_at is None
    assert _events(db, project, EventType.PROJECT_COMPLETED) == []


def test_deciding_the_last_approval_lets_the_project_complete(
    db: Session, project: Project, seeded: dict[str, User]
) -> None:
    _make_running(db, project)
    _seed_task(db, project, status=TaskStatus.COMPLETED, key="a")
    approval = _pending_approval(db, project)
    assert engine.refresh_project_status(db, project) is ProjectStatus.AWAITING_REVIEW

    approval.status = ApprovalStatus.APPROVED
    approval.decided_by_id = seeded["reviewer"].id
    db.flush()

    assert engine.refresh_project_status(db, project) is ProjectStatus.COMPLETED
    assert project.completed_at is not None


def test_a_failed_task_fails_the_project_and_names_it(db: Session, project: Project) -> None:
    _make_running(db, project)
    _seed_task(db, project, status=TaskStatus.COMPLETED, key="a")
    _seed_task(
        db,
        project,
        status=TaskStatus.FAILED,
        key="b",
        title="Parse Conveyor_Line3.L5X",
        error_message="the export was truncated",
    )

    assert engine.refresh_project_status(db, project) is ProjectStatus.FAILED
    assert "Parse Conveyor_Line3.L5X" in project.failure_reason
    assert "the export was truncated" in project.failure_reason
    assert len(_events(db, project, EventType.PROJECT_FAILED)) == 1


def test_a_failed_task_does_not_fail_the_project_while_work_remains(
    db: Session, project: Project
) -> None:
    _make_running(db, project)
    _seed_task(db, project, status=TaskStatus.FAILED, key="a", error_message="unreadable")
    _seed_task(db, project, status=TaskStatus.QUEUED, key="b")

    assert engine.refresh_project_status(db, project) is ProjectStatus.RUNNING


def test_a_wholly_cancelled_run_returns_the_project_to_planned(
    db: Session, project: Project
) -> None:
    _make_running(db, project)
    _seed_task(db, project, status=TaskStatus.CANCELLED, key="a")
    _seed_task(db, project, status=TaskStatus.CANCELLED, key="b")

    assert engine.refresh_project_status(db, project) is ProjectStatus.PLANNED
    assert project.completed_at is None


@pytest.mark.parametrize("status", [ProjectStatus.DRAFT, ProjectStatus.ARCHIVED])
def test_a_draft_or_archived_project_is_never_restatused_by_its_tasks(
    db: Session, project: Project, status: ProjectStatus
) -> None:
    project.status = status
    db.flush()
    _seed_task(db, project, status=TaskStatus.COMPLETED, key="a")

    assert engine.refresh_project_status(db, project) is status
    assert project.completed_at is None


def test_a_project_with_no_tasks_keeps_its_status(db: Session, project: Project) -> None:
    _make_running(db, project)

    assert engine.refresh_project_status(db, project) is ProjectStatus.RUNNING
    assert project.completed_at is None


# ---------------------------------------------------------------------------
# unblock_approval_tasks
# ---------------------------------------------------------------------------
def test_one_pending_approval_holds_every_gated_task(
    db: Session, project: Project, settings: Settings, seeded: dict[str, User]
) -> None:
    """A partially-approved set must not slip through."""
    _make_running(db, project)
    gated = _seed_task(db, project, status=TaskStatus.AWAITING_APPROVAL, key="gate")
    gated.blocked_reason = "1 approval(s) are still awaiting an engineering decision."
    decided = _pending_approval(db, project, title="Decided already")
    decided.status = ApprovalStatus.APPROVED
    decided.decided_by_id = seeded["reviewer"].id
    _pending_approval(db, project, title="Still open")
    db.flush()

    assert engine.unblock_approval_tasks(db, project, settings) == 0
    assert gated.status is TaskStatus.AWAITING_APPROVAL
    assert gated.blocked_reason


def test_deciding_every_approval_returns_gated_tasks_to_the_queue(
    db: Session, project: Project, settings: Settings, seeded: dict[str, User]
) -> None:
    _make_running(db, project)
    gated = _seed_task(db, project, status=TaskStatus.AWAITING_APPROVAL, key="gate")
    gated.blocked_reason = "waiting on a human"
    other = _seed_task(db, project, status=TaskStatus.AWAITING_APPROVAL, key="gate-2")
    approval = _pending_approval(db, project)
    db.flush()

    approval.status = ApprovalStatus.REJECTED
    approval.decided_by_id = seeded["reviewer"].id
    db.flush()

    assert engine.unblock_approval_tasks(db, project, settings) == 2
    for task in (gated, other):
        assert task.status is TaskStatus.QUEUED
        assert task.blocked_reason == ""
        assert task.available_at is None
        assert task.locked_by is None


def test_unblocking_does_not_disturb_tasks_that_are_blocked_for_other_reasons(
    db: Session, project: Project, settings: Settings
) -> None:
    """A task blocked for a missing input is not waiting on an approval."""
    _make_running(db, project)
    blocked = _seed_task(db, project, status=TaskStatus.BLOCKED, key="blocked")
    blocked.blocked_reason = "No interlock schedule was uploaded."
    completed = _seed_task(db, project, status=TaskStatus.COMPLETED, key="done")
    db.flush()

    assert engine.unblock_approval_tasks(db, project, settings) == 0
    assert blocked.status is TaskStatus.BLOCKED
    assert blocked.blocked_reason == "No interlock schedule was uploaded."
    assert completed.status is TaskStatus.COMPLETED


# ---------------------------------------------------------------------------
# The run as it stands today
# ---------------------------------------------------------------------------
def test_a_drained_run_finishes_the_deterministic_prelude(
    db: Session, ingested_project: Project, settings: Settings, seeded: dict[str, User]
) -> None:
    summary = _start_run(db, ingested_project, settings, seeded["engineer"])

    _drain(db, settings)

    assert _only(summary.tasks, TaskKind.INVENTORY_FILES).status is TaskStatus.COMPLETED
    parses = _of_kind(summary.tasks, TaskKind.PARSE_FILE)
    assert parses
    assert all(task.status is TaskStatus.COMPLETED for task in parses), (
        "a parse failure is recorded on the file, not by stalling the run"
    )
    assert _only(summary.tasks, TaskKind.BUILD_EVIDENCE_GRAPH).status is TaskStatus.COMPLETED


def test_a_drained_run_blocks_at_the_first_kind_with_no_handler_installed(
    db: Session, ingested_project: Project, settings: Settings, seeded: dict[str, User]
) -> None:
    """A run parks at the approval gate, neither finished nor failed.

    The approval step is the one place a run is *supposed* to stop and wait for a
    person, so it must be distinguishable from a stall: the task is
    ``awaiting_approval`` and the project is ``awaiting_review``, not ``running``
    with nothing happening.
    """
    summary = _start_run(db, ingested_project, settings, seeded["engineer"])

    _drain(db, settings)

    plan = _only(summary.tasks, TaskKind.PLAN_PROJECT)
    assert plan.status is TaskStatus.COMPLETED
    assert plan.error_code == ""

    approvals = _only(summary.tasks, TaskKind.REQUEST_APPROVALS)
    assert approvals.status is TaskStatus.AWAITING_APPROVAL
    assert approvals.blocked_reason, "the gate must say what it is waiting for"

    db.refresh(ingested_project)
    assert ingested_project.status is ProjectStatus.AWAITING_REVIEW
    assert ingested_project.completed_at is None
    assert _only(summary.tasks, TaskKind.FINALIZE_PROJECT).status is TaskStatus.QUEUED


# ---------------------------------------------------------------------------
# active_run_id
# ---------------------------------------------------------------------------
def test_active_run_id_is_none_before_anything_has_been_run(db: Session, project: Project) -> None:
    assert engine.active_run_id(db, project.id) is None


def test_active_run_id_reports_the_most_recently_created_run(
    db: Session, ingested_project: Project, settings: Settings, seeded: dict[str, User]
) -> None:
    first = _start_run(db, ingested_project, settings, seeded["engineer"])
    assert engine.active_run_id(db, ingested_project.id) == first.run_id

    engine.cancel(db, ingested_project, settings, actor=seeded["engineer"])
    db.commit()
    second = _start_run(db, ingested_project, settings, seeded["engineer"])

    # Ordering is by creation time, so make the second run unambiguously later
    # rather than relying on how finely the clock happened to tick.
    later = utcnow() + timedelta(minutes=1)
    for task in second.tasks:
        task.created_at = later
    db.commit()

    assert engine.active_run_id(db, ingested_project.id) == second.run_id
    assert second.run_id != first.run_id
