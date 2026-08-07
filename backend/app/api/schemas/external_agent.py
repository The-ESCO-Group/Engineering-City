"""Schemas for externally hosted OpenAI and Claude SDK workers."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import Field, model_validator

from app.api.schemas.agent import AgentInstanceRead, AgentRunDetail
from app.api.schemas.common import ApiModel
from app.core.enums import AgentStatus, ProviderKind, SemanticLocation


class ExternalAgentDeployRequest(ApiModel):
    provider: Literal["openai", "anthropic"]
    display_name: Annotated[str, Field(min_length=1, max_length=80)]
    specialty: Annotated[str, Field(min_length=1, max_length=120)]
    mission: Annotated[str, Field(min_length=1, max_length=4000)]
    model: Annotated[str, Field(min_length=1, max_length=120)]
    initial_location: SemanticLocation = SemanticLocation.PROJECT_INTAKE
    ttl_seconds: int = Field(default=3600, ge=300, le=86400)
    acknowledge_external_processing: Literal[True] = Field(
        description=(
            "Explicit acknowledgement that the SDK worker may send the returned "
            "project context to the selected external provider."
        )
    )


class ExternalAgentEndpoints(ApiModel):
    context: str
    progress: str
    submit: str


class ExternalAgentDeployResponse(ApiModel):
    instance: AgentInstanceRead
    agent_token: str = Field(
        description="Shown once. Send as `Authorization: Bearer <token>` to mission endpoints."
    )
    token_expires_at: datetime
    provider: ProviderKind
    model: str
    mission: str
    allowed_actions: list[str]
    endpoints: ExternalAgentEndpoints


class ExternalAgentProgressRequest(ApiModel):
    status: AgentStatus
    location: SemanticLocation
    activity: Annotated[str, Field(min_length=1, max_length=255)]
    message: Annotated[str, Field(max_length=4000)] = ""

    @model_validator(mode="after")
    def _only_active_presence_states(self) -> ExternalAgentProgressRequest:
        if self.status not in {
            AgentStatus.WALKING,
            AgentStatus.WORKING,
            AgentStatus.WAITING,
            AgentStatus.BLOCKED,
        }:
            raise ValueError(
                "external agents may report walking, working, waiting or blocked; "
                "the gateway owns idle/offline terminal state"
            )
        return self


class ExternalAgentContextRead(ApiModel):
    instance: AgentInstanceRead
    provider: ProviderKind
    model: str
    mission: str
    project: dict[str, Any]
    files: list[dict[str, Any]]
    requirements: list[dict[str, Any]]
    evidence_anchors: list[dict[str, Any]]
    evidence_total: int
    evidence_offset: int
    evidence_limit: int
    allowed_actions: list[str]
    response_schema: dict[str, Any]
    rules: str


class ExternalAgentSubmissionRequest(ApiModel):
    submission_id: uuid.UUID
    rationale: Annotated[str, Field(min_length=1, max_length=4000)]
    actions: Annotated[list[dict[str, Any]], Field(max_length=100)]
    warnings: Annotated[list[Annotated[str, Field(max_length=4000)]], Field(max_length=100)] = (
        Field(default_factory=list)
    )
    prompt_tokens: int = Field(default=0, ge=0, le=200_000)
    completion_tokens: int = Field(default=0, ge=0, le=200_000)
    latency_ms: int = Field(default=0, ge=0, le=86_400_000)


class ExternalAgentSubmissionResponse(ApiModel):
    run: AgentRunDetail
    accepted_actions: int
    rejected_actions: int
    execution: dict[str, Any]
    replayed: bool = False


class ExternalAgentRevokeResponse(ApiModel):
    instance_id: uuid.UUID
    status: AgentStatus
    message: str
