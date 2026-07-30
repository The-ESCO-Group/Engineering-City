"""Workflow control and task inspection."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Path, Query, status
from sqlalchemy import select

from app.api.deps import (
    AppSettings,
    CurrentUser,
    DbSession,
    ProjectRead,
    ProjectWrite,
    authorise_project,
)
from app.api.schemas.common import OkResponse
from app.api.schemas.task import (
    RunRead,
    StartRunRequest,
    TaskDetail,
    TaskRead,
    WorkflowControlRequest,
    WorkflowStateResponse,
)
from app.core.enums import TaskStatus
from app.core.errors import NotFoundError
from app.core.services import parsing as parsing_service
from app.models.task import Task, TaskDependency
from app.orchestration import engine
from app.orchestration.queue import DatabaseTaskQueue
from app.security import permissions

router = APIRouter(tags=["workflow"])


@router.post(
    "/projects/{project_id}/runs",
    response_model=RunRead,
    status_code=status.HTTP_201_CREATED,
    summary="Start a review run",
)
def start_run(
    payload: StartRunRequest,
    access: ProjectWrite,
    db: DbSession,
    settings: AppSettings,
) -> RunRead:
    """Create the task graph for a new run.

    Returns `409` if a run is already in progress: two concurrent runs over one
    project would interleave findings and neither would be reproducible.
    """
    summary = engine.start_run(
        db,
        access.project,
        settings,
        actor=access.user,
        force_reparse=payload.force_reparse,
        file_ids=payload.file_ids or None,
        note=payload.note,
    )
    db.commit()

    return RunRead(
        run_id=summary.run_id,
        project_id=summary.project_id,
        status=access.project.status.value,
        started_at=access.project.updated_at,
        task_count=summary.task_count,
        tasks=[TaskRead.model_validate(task) for task in summary.tasks],
    )


@router.post(
    "/projects/{project_id}/pause",
    response_model=WorkflowStateResponse,
    summary="Pause the run",
)
def pause_run(
    payload: WorkflowControlRequest,
    access: ProjectWrite,
    db: DbSession,
    settings: AppSettings,
) -> WorkflowStateResponse:
    """Stop the worker taking further tasks.

    A task already running is allowed to finish; interrupting it mid-write would
    leave partial state for the recovery path to untangle.
    """
    engine.pause(db, access.project, actor=access.user, reason=payload.reason)
    db.commit()
    return _state(db, access, settings, "Paused. Tasks already running will finish.")


@router.post(
    "/projects/{project_id}/resume",
    response_model=WorkflowStateResponse,
    summary="Resume a paused run",
)
def resume_run(
    payload: WorkflowControlRequest,
    access: ProjectWrite,
    db: DbSession,
    settings: AppSettings,
) -> WorkflowStateResponse:
    engine.resume(db, access.project, actor=access.user, reason=payload.reason)
    db.commit()
    return _state(db, access, settings, "Resumed.")


@router.post(
    "/projects/{project_id}/cancel",
    response_model=WorkflowStateResponse,
    summary="Cancel the run",
)
def cancel_run(
    payload: WorkflowControlRequest,
    access: ProjectWrite,
    db: DbSession,
    settings: AppSettings,
) -> WorkflowStateResponse:
    """Cancel every unfinished task.

    Completed work is kept: cancelling a run is not the same as retracting what
    it already found.
    """
    cancelled = engine.cancel(
        db, access.project, settings, actor=access.user, reason=payload.reason
    )
    db.commit()
    return _state(db, access, settings, f"Cancelled {cancelled} unfinished task(s).")


@router.get(
    "/projects/{project_id}/workflow",
    response_model=WorkflowStateResponse,
    summary="Current workflow state and task counts",
)
def workflow_state(
    access: ProjectRead, db: DbSession, settings: AppSettings
) -> WorkflowStateResponse:
    return _state(db, access, settings)


def _state(
    db: DbSession,
    access: ProjectRead | ProjectWrite,
    settings: AppSettings,
    message: str = "",
) -> WorkflowStateResponse:
    counts = DatabaseTaskQueue(db, settings).status_counts(access.project.id)
    return WorkflowStateResponse(
        project_id=access.project.id,
        project_status=access.project.status.value,
        is_paused=access.project.is_paused,
        active_run_id=engine.active_run_id(db, access.project.id),
        queued=counts.get(TaskStatus.QUEUED.value, 0),
        running=counts.get(TaskStatus.RUNNING.value, 0),
        blocked=counts.get(TaskStatus.BLOCKED.value, 0),
        awaiting_approval=counts.get(TaskStatus.AWAITING_APPROVAL.value, 0),
        completed=counts.get(TaskStatus.COMPLETED.value, 0),
        failed=counts.get(TaskStatus.FAILED.value, 0),
        cancelled=counts.get(TaskStatus.CANCELLED.value, 0),
        message=message,
    )


@router.get(
    "/projects/{project_id}/tasks",
    response_model=list[TaskRead],
    summary="List a project's tasks",
)
def list_tasks(
    access: ProjectRead,
    db: DbSession,
    task_status: Annotated[TaskStatus | None, Query(alias="status")] = None,
    run_id: Annotated[uuid.UUID | None, Query()] = None,
) -> list[TaskRead]:
    statement = select(Task).where(Task.project_id == access.project.id)
    if task_status is not None:
        statement = statement.where(Task.status == task_status)
    if run_id is not None:
        statement = statement.where(Task.run_id == run_id)

    rows = db.execute(statement.order_by(Task.priority, Task.created_at)).scalars()
    return [TaskRead.model_validate(row) for row in rows]


@router.get(
    "/tasks/{task_id}",
    response_model=TaskDetail,
    summary="Task detail with its dependency edges",
)
def get_task(
    task_id: Annotated[uuid.UUID, Path()],
    db: DbSession,
    user: CurrentUser,
) -> TaskDetail:
    task = db.get(Task, task_id)
    if task is None:
        raise NotFoundError("Task", task_id)
    authorise_project(db, user, task.project_id, permissions.READ_ROLES, "view")

    detail = TaskDetail.model_validate(task)
    detail.depends_on = [dependency.depends_on_id for dependency in task.dependencies]
    detail.blocks = [
        row.task_id
        for row in db.execute(
            select(TaskDependency).where(TaskDependency.depends_on_id == task.id)
        ).scalars()
    ]
    return detail


@router.post(
    "/projects/{project_id}/recover",
    response_model=OkResponse,
    summary="Requeue tasks abandoned by a stopped worker",
)
def recover_tasks(access: ProjectWrite, db: DbSession, settings: AppSettings) -> OkResponse:
    """Manually trigger the lease sweep.

    The worker does this automatically at startup and periodically; this exists
    for an operator who has just restarted after a crash and does not want to
    wait out the sweep interval.
    """
    requeued = DatabaseTaskQueue(db, settings).requeue_expired()
    db.commit()
    return OkResponse(
        message=(
            f"Requeued {requeued} task(s) whose worker stopped responding."
            if requeued
            else "No abandoned tasks were found."
        )
    )


@router.get(
    "/projects/{project_id}/parsers",
    response_model=list[dict],
    summary="Which file kinds this installation can parse",
)
def parser_capabilities(access: ProjectRead) -> list[dict]:
    """Per-kind parser availability.

    A kind reported unavailable here explains why a file of that kind will not be
    analysed, rather than leaving it looking merely unexamined.
    """
    return parsing_service.capability_report()
