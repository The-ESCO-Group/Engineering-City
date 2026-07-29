"""Workflow and task schemas."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import Field

from app.api.schemas.common import ApiModel
from app.core.enums import TaskKind, TaskStatus


class TaskRead(ApiModel):
    id: uuid.UUID
    project_id: uuid.UUID
    run_id: uuid.UUID | None = None
    kind: TaskKind
    title: str
    description: str = ""
    status: TaskStatus
    priority: int = 100
    agent_kind: str | None = None
    agent_instance_id: uuid.UUID | None = None
    attempts: int = 0
    max_attempts: int = 3
    available_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error_code: str = ""
    error_message: str = ""
    blocked_reason: str = ""
    created_at: datetime
    updated_at: datetime


class TaskDetail(TaskRead):
    payload: dict[str, Any] = Field(default_factory=dict)
    result: dict[str, Any] = Field(default_factory=dict)
    depends_on: list[uuid.UUID] = Field(default_factory=list)
    #: Tasks that cannot start until this one completes.
    blocks: list[uuid.UUID] = Field(default_factory=list)


class StartRunRequest(ApiModel):
    """Starts a workflow run over the project's current files."""

    #: Re-parse files that already have an artifact from the current parser
    #: version. Off by default because parsing is the expensive step.
    force_reparse: bool = False
    #: Restrict the run to these files. Empty means every available file.
    file_ids: list[uuid.UUID] = Field(default_factory=list)
    #: Human-readable reason, recorded in the audit trail.
    note: str = ""


class RunRead(ApiModel):
    """A workflow run: the task graph created by one ``POST /runs`` call."""

    run_id: uuid.UUID
    project_id: uuid.UUID
    status: str
    started_at: datetime
    task_count: int
    tasks: list[TaskRead] = Field(default_factory=list)


class WorkflowControlRequest(ApiModel):
    reason: str = ""


class WorkflowStateResponse(ApiModel):
    project_id: uuid.UUID
    project_status: str
    is_paused: bool
    active_run_id: uuid.UUID | None = None
    queued: int = 0
    running: int = 0
    blocked: int = 0
    awaiting_approval: int = 0
    completed: int = 0
    failed: int = 0
    cancelled: int = 0
    message: str = ""
