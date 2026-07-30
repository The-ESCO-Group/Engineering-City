"""The workflow engine.

Owns the project state machine and the shape of a review run.

A run has a fixed prelude and a planned remainder:

1. **intake** — inventory what was uploaded, name what is missing
2. **parse** — one task per file, so one unreadable document does not stop the rest
3. **evidence graph** — join the parsed artifacts across files
4. **plan** — the Project Manager Agent decides which specialists to run
5. *(planned)* PLC review, interlock review — enqueued by the plan
6. **senior review** — independently check every finding
7. **approvals** — route what needs a human to a human
8. *(optional)* delivery — GPT-owned plugins, if installed
9. **finalize** — only after the required approvals are decided

Steps 1-4 are fixed because they are mechanical. Step 5 is planned because which
specialists are worth running depends on what was actually uploaded — and the
Project Manager Agent is not permitted to draw engineering conclusions while
deciding it.

Project status is owned here and nowhere else. There is no API to set it
directly: a status a client could assign would make the state machine a
suggestion.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.clock import utcnow
from app.core.config import Settings
from app.core.enums import (
    AgentKind,
    AuditAction,
    EventType,
    ProjectStatus,
    TaskKind,
    TaskStatus,
)
from app.core.errors import ConflictError
from app.core.logging import get_logger
from app.core.services import parsing
from app.models.approval import Approval
from app.models.project import Project
from app.models.task import Task
from app.models.user import User
from app.orchestration.events import record_event
from app.orchestration.queue import DatabaseTaskQueue, EnqueueRequest
from app.security import audit

logger = get_logger(__name__)

#: Project statuses from which a new run may be started.
STARTABLE: frozenset[ProjectStatus] = frozenset(
    {
        ProjectStatus.DRAFT,
        ProjectStatus.PLANNED,
        ProjectStatus.COMPLETED,
        ProjectStatus.FAILED,
        ProjectStatus.AWAITING_REVIEW,
    }
)


@dataclass(slots=True)
class RunSummary:
    run_id: uuid.UUID
    project_id: uuid.UUID
    task_count: int
    tasks: list[Task]


def start_run(
    db: Session,
    project: Project,
    settings: Settings,
    *,
    actor: User,
    force_reparse: bool = False,
    file_ids: list[uuid.UUID] | None = None,
    note: str = "",
) -> RunSummary:
    """Create the task graph for a new review run.

    Refuses rather than silently restarting when a run is already in flight —
    two concurrent runs over one project would interleave findings and neither
    would be reproducible.
    """
    if project.is_archived:
        raise ConflictError("Archived projects are read-only.")
    if project.status is ProjectStatus.RUNNING:
        raise ConflictError(
            "A run is already in progress for this project. Cancel it before starting another."
        )
    if project.status not in STARTABLE:
        raise ConflictError(
            f"A run cannot be started while the project is '{project.status.value}'."
        )

    files = parsing.parseable_files(db, project.id)
    if file_ids:
        wanted = set(file_ids)
        files = [record for record in files if record.id in wanted]
    if not files:
        raise ConflictError(
            "This project has no files that can be analysed. Upload engineering "
            "documents before starting a review."
        )

    run_id = uuid.uuid4()
    queue = DatabaseTaskQueue(db, settings)
    created: list[Task] = []

    def enqueue(request: EnqueueRequest) -> Task:
        task = queue.enqueue(project.id, request)
        created.append(task)
        return task

    # The run id is part of every idempotency key so a second run over the same
    # files creates a fresh graph instead of colliding with the first.
    prefix = f"{run_id.hex[:8]}"

    intake = enqueue(
        EnqueueRequest(
            kind=TaskKind.INVENTORY_FILES,
            title="Inventory uploaded files",
            description="Record what was supplied and name any expected input that was not.",
            idempotency_key=f"{prefix}:intake",
            agent_kind=AgentKind.PROJECT_MANAGER.value,
            run_id=run_id,
            priority=10,
        )
    )

    parse_tasks: list[Task] = []
    for record in files:
        parse_tasks.append(
            enqueue(
                EnqueueRequest(
                    kind=TaskKind.PARSE_FILE,
                    title=f"Parse {record.original_filename}",
                    description=f"Extract normalised content and evidence anchors from "
                    f"{record.original_filename}.",
                    idempotency_key=f"{prefix}:parse:{record.id}",
                    payload={"file_id": str(record.id), "force": force_reparse},
                    run_id=run_id,
                    priority=20,
                    depends_on=[intake.id],
                )
            )
        )

    graph = enqueue(
        EnqueueRequest(
            kind=TaskKind.BUILD_EVIDENCE_GRAPH,
            title="Build the project evidence graph",
            description="Join every parsed artifact into one cross-file structure.",
            idempotency_key=f"{prefix}:graph",
            run_id=run_id,
            priority=30,
            # Soft: one unreadable document must not stop the review of the rest.
            # The graph records that the file was unreadable instead.
            depends_on=[],
        )
    )
    for task in parse_tasks:
        queue.add_dependency(graph, task.id, hard=False)

    plan = enqueue(
        EnqueueRequest(
            kind=TaskKind.PLAN_PROJECT,
            title="Plan the review",
            description="Decide which specialist reviews the supplied evidence supports.",
            idempotency_key=f"{prefix}:plan",
            agent_kind=AgentKind.PROJECT_MANAGER.value,
            run_id=run_id,
            priority=40,
            depends_on=[graph.id],
        )
    )

    # The specialist tasks are enqueued by the plan handler, which is why senior
    # review depends on the plan rather than on tasks that do not exist yet. The
    # plan adds the specialists as dependencies of this task as it creates them.
    senior = enqueue(
        EnqueueRequest(
            kind=TaskKind.SENIOR_REVIEW,
            title="Senior review of all findings",
            description="Independently check every finding before it reaches a human.",
            idempotency_key=f"{prefix}:senior",
            agent_kind=AgentKind.SENIOR_REVIEW.value,
            run_id=run_id,
            priority=70,
            depends_on=[plan.id],
        )
    )

    approvals = enqueue(
        EnqueueRequest(
            kind=TaskKind.REQUEST_APPROVALS,
            title="Route findings for human approval",
            description="Raise an approval for everything that needs an engineer's decision.",
            idempotency_key=f"{prefix}:approvals",
            agent_kind=AgentKind.SENIOR_REVIEW.value,
            run_id=run_id,
            priority=80,
            depends_on=[senior.id],
        )
    )

    delivery = enqueue(
        EnqueueRequest(
            kind=TaskKind.DELIVERY,
            title="Produce reports and testing documentation",
            description="Run installed delivery agents over the approved findings.",
            idempotency_key=f"{prefix}:delivery",
            agent_kind=AgentKind.DELIVERY.value,
            run_id=run_id,
            priority=90,
            depends_on=[approvals.id],
        )
    )

    enqueue(
        EnqueueRequest(
            kind=TaskKind.FINALIZE_PROJECT,
            title="Finalise the review",
            description="Complete the project once the required approvals are decided.",
            idempotency_key=f"{prefix}:finalize",
            run_id=run_id,
            priority=100,
            depends_on=[delivery.id],
        )
    )

    project.status = ProjectStatus.RUNNING
    project.is_paused = False
    project.paused_at = None
    project.failure_reason = ""
    project.completed_at = None
    db.flush()

    audit.record(
        db,
        AuditAction.ANALYSIS_STARTED,
        actor_user_id=actor.id,
        project_id=project.id,
        target_type="project",
        target_id=project.id,
        summary=f"Started a review run over {len(files)} file(s)",
        details={
            "run_id": str(run_id),
            "file_count": len(files),
            "task_count": len(created),
            "force_reparse": force_reparse,
            "note": note,
        },
    )
    record_event(
        db,
        project_id=project.id,
        event_type=EventType.WORKFLOW_STARTED,
        actor_user_id=actor.id,
        payload={"run_id": str(run_id), "task_count": len(created), "reason": note},
    )
    for task in created:
        record_event(
            db,
            project_id=project.id,
            event_type=EventType.TASK_CREATED,
            task_id=task.id,
            payload={
                "task_id": str(task.id),
                "kind": task.kind.value,
                "title": task.title,
                "status": task.status.value,
                "agent_kind": task.agent_kind or "",
            },
        )

    logger.info(
        "workflow run started",
        extra={
            "project_id": str(project.id),
            "run_id": str(run_id),
            "tasks": len(created),
            "files": len(files),
        },
    )
    return RunSummary(run_id=run_id, project_id=project.id, task_count=len(created), tasks=created)


# ---------------------------------------------------------------------------
# Control
# ---------------------------------------------------------------------------
def pause(db: Session, project: Project, *, actor: User, reason: str = "") -> Project:
    """Stop the worker picking up further tasks for this project.

    A task already running is allowed to finish; killing it mid-write would leave
    partial state that the restart-recovery path then has to reason about.
    """
    if project.is_paused:
        raise ConflictError("Project is already paused.")
    if project.status is not ProjectStatus.RUNNING:
        raise ConflictError(
            f"Only a running project can be paused; this one is '{project.status.value}'."
        )

    project.is_paused = True
    project.paused_at = utcnow()
    db.flush()

    audit.record(
        db,
        AuditAction.ANALYSIS_PAUSED,
        actor_user_id=actor.id,
        project_id=project.id,
        target_type="project",
        target_id=project.id,
        summary="Paused the review",
        details={"reason": reason},
    )
    record_event(
        db,
        project_id=project.id,
        event_type=EventType.WORKFLOW_PAUSED,
        actor_user_id=actor.id,
        payload={"reason": reason},
    )
    return project


def resume(db: Session, project: Project, *, actor: User, reason: str = "") -> Project:
    if not project.is_paused:
        raise ConflictError("Project is not paused.")

    project.is_paused = False
    project.paused_at = None
    db.flush()

    audit.record(
        db,
        AuditAction.ANALYSIS_RESUMED,
        actor_user_id=actor.id,
        project_id=project.id,
        target_type="project",
        target_id=project.id,
        summary="Resumed the review",
        details={"reason": reason},
    )
    record_event(
        db,
        project_id=project.id,
        event_type=EventType.WORKFLOW_RESUMED,
        actor_user_id=actor.id,
        payload={"reason": reason},
    )
    return project


def cancel(
    db: Session, project: Project, settings: Settings, *, actor: User, reason: str = ""
) -> int:
    """Cancel every unfinished task and stop the run.

    Completed work is kept: findings already produced and approved remain valid,
    because cancelling a run is not the same as retracting what it found.
    """
    queue = DatabaseTaskQueue(db, settings)
    unfinished = list(
        db.execute(
            select(Task).where(
                Task.project_id == project.id,
                Task.status.notin_([TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED]),
            )
        ).scalars()
    )
    for task in unfinished:
        queue.cancel(task, reason or "The run was cancelled.")

    project.status = ProjectStatus.PLANNED if unfinished else project.status
    project.is_paused = False
    project.paused_at = None
    db.flush()

    audit.record(
        db,
        AuditAction.ANALYSIS_CANCELLED,
        actor_user_id=actor.id,
        project_id=project.id,
        target_type="project",
        target_id=project.id,
        summary=f"Cancelled the review, stopping {len(unfinished)} task(s)",
        details={"reason": reason, "cancelled_tasks": len(unfinished)},
    )
    record_event(
        db,
        project_id=project.id,
        event_type=EventType.WORKFLOW_CANCELLED,
        actor_user_id=actor.id,
        payload={"reason": reason, "task_count": len(unfinished)},
    )
    return len(unfinished)


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------
def refresh_project_status(db: Session, project: Project) -> ProjectStatus:
    """Recompute project status from its tasks.

    Called after every task transition. Deliberately conservative about
    ``completed``: a project is only complete when every task is terminal AND no
    approval is still pending. Marking it complete with an outstanding safety
    approval would be the single worst thing this product could do.
    """
    if project.status in {ProjectStatus.ARCHIVED, ProjectStatus.DRAFT}:
        return project.status

    tasks = list(db.execute(select(Task).where(Task.project_id == project.id)).scalars())
    if not tasks:
        return project.status

    statuses = [task.status for task in tasks]
    pending_approvals = int(
        db.execute(
            select(Approval.id).where(
                Approval.project_id == project.id,
                Approval.status == "pending",
            )
        )
        .scalars()
        .first()
        is not None
    )

    active = any(
        status in {TaskStatus.QUEUED, TaskStatus.RUNNING, TaskStatus.BLOCKED} for status in statuses
    )
    awaiting = any(status is TaskStatus.AWAITING_APPROVAL for status in statuses)

    previous = project.status
    if awaiting or pending_approvals:
        project.status = ProjectStatus.AWAITING_REVIEW
    elif active:
        project.status = ProjectStatus.RUNNING
    elif all(status is TaskStatus.CANCELLED for status in statuses):
        project.status = ProjectStatus.PLANNED
    elif any(status is TaskStatus.FAILED for status in statuses):
        # A run that could not finish is failed, and the reason names the tasks
        # rather than leaving an engineer to guess.
        failed = [task for task in tasks if task.status is TaskStatus.FAILED]
        project.status = ProjectStatus.FAILED
        project.failure_reason = "; ".join(
            f"{task.title}: {task.error_message or task.error_code}" for task in failed[:5]
        )
    else:
        project.status = ProjectStatus.COMPLETED
        project.completed_at = utcnow()

    if project.status is not previous:
        db.flush()
        _emit_status_change(db, project, previous)
    return project.status


def _emit_status_change(db: Session, project: Project, previous: ProjectStatus) -> None:
    payload = {
        "project_id": str(project.id),
        "name": project.name,
        "status": project.status.value,
        "failure_reason": project.failure_reason,
    }
    if project.status is ProjectStatus.COMPLETED:
        event_type = EventType.PROJECT_COMPLETED
    elif project.status is ProjectStatus.FAILED:
        event_type = EventType.PROJECT_FAILED
    else:
        event_type = EventType.PROJECT_UPDATED

    record_event(db, project_id=project.id, event_type=event_type, payload=payload)
    logger.info(
        "project status changed",
        extra={
            "project_id": str(project.id),
            "from": previous.value,
            "to": project.status.value,
        },
    )


def unblock_approval_tasks(db: Session, project: Project, settings: Settings) -> int:
    """Return approval-gated tasks to the queue once nothing is pending.

    Called after an approval decision. A task waiting on approval resumes only
    when every approval for the project has been decided, so a partially-approved
    set cannot slip through.
    """
    still_pending = db.execute(
        select(Approval.id).where(Approval.project_id == project.id, Approval.status == "pending")
    ).first()
    if still_pending is not None:
        return 0

    queue = DatabaseTaskQueue(db, settings)
    waiting = list(
        db.execute(
            select(Task).where(
                Task.project_id == project.id,
                Task.status == TaskStatus.AWAITING_APPROVAL,
            )
        ).scalars()
    )
    for task in waiting:
        queue.unblock(task)
    if waiting:
        logger.info(
            "approval gate cleared",
            extra={"project_id": str(project.id), "resumed_tasks": len(waiting)},
        )
    return len(waiting)


def active_run_id(db: Session, project_id: uuid.UUID) -> uuid.UUID | None:
    """The run id of the most recently created task, if any."""
    task = db.execute(
        select(Task)
        .where(Task.project_id == project_id, Task.run_id.isnot(None))
        .order_by(Task.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    return task.run_id if task else None
