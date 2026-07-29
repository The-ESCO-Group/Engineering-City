"""Agent, agent-run and message schemas."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import Field

from app.api.schemas.common import ApiModel
from app.core.enums import AgentRunStatus, AgentStatus, MessageRole


class AgentDefinitionRead(ApiModel):
    id: uuid.UUID
    kind: str
    display_name: str
    description: str = ""
    building: str = ""
    handles_task_kinds: list[Any] = Field(default_factory=list)
    is_plugin: bool = False
    is_available: bool = True
    unavailable_reason: str = Field(
        default="",
        description="Why the agent cannot run — e.g. its plugin is not installed.",
    )


class AgentInstanceRead(ApiModel):
    id: uuid.UUID
    project_id: uuid.UUID
    definition_id: uuid.UUID
    kind: str
    display_name: str
    status: AgentStatus
    location: str = ""
    current_activity: str = ""
    current_task_id: uuid.UUID | None = None
    updated_at: datetime


class AgentActionRead(ApiModel):
    """A model-proposed action, as recorded on the run.

    ``accepted`` is false for actions the backend refused; ``rejection_reason``
    says why. Refusals are kept rather than discarded — they are part of the
    record of what the model tried to do.
    """

    action_type: str
    summary: str
    target_type: str = ""
    target_id: str | None = None
    evidence_anchor_ids: list[uuid.UUID] = Field(default_factory=list)
    confidence: float = 0.0
    requires_approval: bool = False
    warnings: list[str] = Field(default_factory=list)
    accepted: bool = True
    rejection_reason: str = ""


class AgentRunRead(ApiModel):
    """Everything persisted about one agent execution.

    Contains no chain-of-thought by construction: ``rationale`` is a concise
    human-facing justification the agent is required to produce.
    """

    id: uuid.UUID
    project_id: uuid.UUID
    agent_instance_id: uuid.UUID | None = None
    task_id: uuid.UUID | None = None
    agent_kind: str
    status: AgentRunStatus
    provider: str = ""
    model: str = ""
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_ms: int = 0
    rationale: str = ""
    iterations: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    error_code: str = ""
    error_message: str = ""


class AgentRunDetail(AgentRunRead):
    actions: list[AgentActionRead] = Field(default_factory=list)
    rejected_actions: list[AgentActionRead] = Field(default_factory=list)
    warnings: list[Any] = Field(default_factory=list)
    tool_results: list[Any] = Field(default_factory=list)


class AgentMessageRead(ApiModel):
    id: uuid.UUID
    project_id: uuid.UUID
    agent_instance_id: uuid.UUID | None = None
    agent_run_id: uuid.UUID | None = None
    task_id: uuid.UUID | None = None
    author_user_id: uuid.UUID | None = None
    role: MessageRole
    agent_kind: str = ""
    body: str
    context: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class ModelConfigurationRead(ApiModel):
    id: uuid.UUID
    project_id: uuid.UUID | None = None
    agent_kind: str = ""
    provider: str
    model: str
    temperature: float = 0.0
    max_output_tokens: int = 4096


class UpdateModelConfigurationRequest(ApiModel):
    """Administrator-only. The provider must already be enabled in settings."""

    provider: str
    model: str
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    max_output_tokens: int = Field(default=4096, ge=1, le=200_000)
