"""The versioned event envelope broadcast over the project WebSocket.

The envelope is deliberately uniform: clients switch on ``event_type`` and read
``payload``. Payload shapes are additive-only — a new optional key is a
non-breaking change, removing or retyping a key is not.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import Field

from app.api.schemas.common import ApiModel
from app.core.enums import EventType


class EventEnvelope(ApiModel):
    """One entry in a project's ordered event stream."""

    id: uuid.UUID
    schema_version: int = Field(
        description="Envelope version. Bumped only on a breaking payload change."
    )
    seq: int = Field(
        description=(
            "Gap-free per-project sequence number. Reconnecting clients pass the "
            "last value they saw as ?since_seq= to replay what they missed."
        )
    )
    event_type: EventType
    project_id: uuid.UUID
    agent_id: uuid.UUID | None = Field(
        default=None, description="Agent instance that caused the event, if any."
    )
    task_id: uuid.UUID | None = None
    actor_user_id: uuid.UUID | None = Field(
        default=None, description="Authenticated human that caused the event, if any."
    )
    occurred_at: datetime
    payload: dict[str, Any] = Field(default_factory=dict)


class WebSocketHello(ApiModel):
    """First frame the server sends after a WebSocket connection is accepted."""

    type: str = "hello"
    project_id: uuid.UUID
    schema_version: int
    #: Highest sequence number currently persisted for the project.
    current_seq: int
    #: True when the requested ``since_seq`` was older than the replay window,
    #: meaning the client must refetch state via REST rather than rely on replay.
    replay_truncated: bool = False
    replayed: int = 0


class WebSocketError(ApiModel):
    """Terminal frame sent before the server closes a WebSocket."""

    type: str = "error"
    code: str
    message: str


# ---------------------------------------------------------------------------
# Payload shapes, documented per event type.
# These are published in contracts/events.schema.json so the frontend can type
# the payload after switching on event_type.
# ---------------------------------------------------------------------------
class ProjectEventPayload(ApiModel):
    project_id: uuid.UUID
    name: str
    status: str
    #: Present on ``project.failed``.
    failure_reason: str = ""


class FileEventPayload(ApiModel):
    file_id: uuid.UUID
    original_filename: str
    file_kind: str
    document_role: str
    status: str
    size_bytes: int = 0
    sha256: str = ""
    #: Present on ``file.parsed`` / ``file.parse_failed``.
    warning_count: int = 0
    detail: str = ""


class WorkflowEventPayload(ApiModel):
    run_id: uuid.UUID | None = None
    task_count: int = 0
    reason: str = ""


class TaskEventPayload(ApiModel):
    task_id: uuid.UUID
    kind: str
    title: str
    status: str
    agent_kind: str = ""
    attempts: int = 0
    #: Present on ``task.failed`` / ``task.blocked``.
    detail: str = ""


class AgentStatusEventPayload(ApiModel):
    agent_id: uuid.UUID
    agent_kind: str
    display_name: str
    status: str
    current_activity: str = ""
    task_id: uuid.UUID | None = None


class AgentLocationEventPayload(ApiModel):
    agent_id: uuid.UUID
    agent_kind: str
    location: str
    previous_location: str = ""


class AgentMessageEventPayload(ApiModel):
    message_id: uuid.UUID
    agent_id: uuid.UUID | None = None
    agent_kind: str = ""
    role: str
    body: str
    task_id: uuid.UUID | None = None


class FindingEventPayload(ApiModel):
    finding_id: uuid.UUID
    title: str
    category: str
    severity: str
    status: str
    confidence: float
    requires_approval: bool
    evidence_count: int = 0
    created_by_agent_kind: str = ""
    #: Present on ``finding.reviewed``.
    review_verdict: str = ""


class ApprovalEventPayload(ApiModel):
    approval_id: uuid.UUID
    kind: str
    status: str
    title: str
    finding_id: uuid.UUID | None = None
    recommendation_id: uuid.UUID | None = None
    decided_by_id: uuid.UUID | None = None


class ReportEventPayload(ApiModel):
    report_id: uuid.UUID
    report_type: str
    export_format: str
    title: str
    status: str
    provider_name: str = ""


#: Maps each event type to the model documenting its payload. Used by the
#: contract generator and by the payload-shape test.
EVENT_PAYLOAD_MODELS: dict[EventType, type[ApiModel]] = {
    EventType.PROJECT_CREATED: ProjectEventPayload,
    EventType.PROJECT_UPDATED: ProjectEventPayload,
    EventType.PROJECT_COMPLETED: ProjectEventPayload,
    EventType.PROJECT_FAILED: ProjectEventPayload,
    EventType.FILE_UPLOADED: FileEventPayload,
    EventType.FILE_PARSING_STARTED: FileEventPayload,
    EventType.FILE_PARSED: FileEventPayload,
    EventType.FILE_PARSE_FAILED: FileEventPayload,
    EventType.FILE_DELETED: FileEventPayload,
    EventType.WORKFLOW_STARTED: WorkflowEventPayload,
    EventType.WORKFLOW_PAUSED: WorkflowEventPayload,
    EventType.WORKFLOW_RESUMED: WorkflowEventPayload,
    EventType.WORKFLOW_CANCELLED: WorkflowEventPayload,
    EventType.TASK_CREATED: TaskEventPayload,
    EventType.TASK_STARTED: TaskEventPayload,
    EventType.TASK_COMPLETED: TaskEventPayload,
    EventType.TASK_FAILED: TaskEventPayload,
    EventType.TASK_BLOCKED: TaskEventPayload,
    EventType.AGENT_STATUS_CHANGED: AgentStatusEventPayload,
    EventType.AGENT_LOCATION_CHANGED: AgentLocationEventPayload,
    EventType.AGENT_MESSAGE_CREATED: AgentMessageEventPayload,
    EventType.FINDING_CREATED: FindingEventPayload,
    EventType.FINDING_REVIEWED: FindingEventPayload,
    EventType.FINDING_UPDATED: FindingEventPayload,
    EventType.APPROVAL_REQUESTED: ApprovalEventPayload,
    EventType.APPROVAL_DECIDED: ApprovalEventPayload,
    EventType.REPORT_CREATED: ReportEventPayload,
}
