"""Project schemas."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from pydantic import Field

from app.api.schemas.common import ApiModel
from app.core.enums import ProjectRole, ProjectStatus


class ProjectMemberRead(ApiModel):
    id: uuid.UUID
    user_id: uuid.UUID
    role: ProjectRole
    display_name: str = ""
    email: str = ""


class CreateProjectRequest(ApiModel):
    name: Annotated[str, Field(min_length=1, max_length=200)]
    description: str = ""
    client_reference: Annotated[str, Field(max_length=120)] = ""
    attributes: dict[str, Any] = Field(default_factory=dict)


class UpdateProjectRequest(ApiModel):
    """Partial update. Omitted fields are left unchanged.

    ``status`` is intentionally absent: project status is owned by the workflow
    engine and cannot be set directly.
    """

    name: Annotated[str, Field(min_length=1, max_length=200)] | None = None
    description: str | None = None
    client_reference: Annotated[str, Field(max_length=120)] | None = None
    attributes: dict[str, Any] | None = None


class ProjectCounts(ApiModel):
    """Cheap roll-ups so the city view does not have to fetch every collection."""

    files: int = 0
    files_parsed: int = 0
    tasks: int = 0
    tasks_completed: int = 0
    findings: int = 0
    findings_open: int = 0
    approvals_pending: int = 0
    reports: int = 0


class ProjectRead(ApiModel):
    id: uuid.UUID
    name: str
    description: str
    client_reference: str
    status: ProjectStatus
    is_paused: bool
    created_by_id: uuid.UUID | None = None
    created_at: datetime
    updated_at: datetime
    paused_at: datetime | None = None
    archived_at: datetime | None = None
    completed_at: datetime | None = None
    failure_reason: str = ""
    event_seq: int = 0
    tokens_used: int = 0
    attributes: dict[str, Any] = Field(default_factory=dict)


class ProjectDetail(ProjectRead):
    members: list[ProjectMemberRead] = Field(default_factory=list)
    counts: ProjectCounts = Field(default_factory=ProjectCounts)


class AddProjectMemberRequest(ApiModel):
    user_id: uuid.UUID
    role: ProjectRole = ProjectRole.ENGINEER
