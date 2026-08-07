"""Human approval schemas."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from pydantic import Field

from app.api.schemas.common import ApiModel
from app.core.enums import ApprovalDecision, ApprovalGateKey, ApprovalKind, ApprovalStatus


class ApprovalRead(ApiModel):
    id: uuid.UUID
    project_id: uuid.UUID
    gate_id: uuid.UUID | None = None
    gate_key: ApprovalGateKey | None = None
    revision_number: int | None = None
    release_candidate_id: uuid.UUID | None = None
    decision: ApprovalDecision | None = None
    task_id: uuid.UUID | None = None
    finding_id: uuid.UUID | None = None
    recommendation_id: uuid.UUID | None = None
    report_id: uuid.UUID | None = None
    kind: ApprovalKind
    status: ApprovalStatus
    title: str
    question: str = Field(
        description="Plain-language statement of what the human is accepting responsibility for."
    )
    context: dict[str, Any] = Field(
        default_factory=dict,
        description="Snapshot of evidence and severity at request time; not re-read later.",
    )
    requested_by_agent_kind: str = ""
    requested_at: datetime | None = None
    decided_by_id: uuid.UUID | None = None
    decided_by_display_name: str = ""
    decided_at: datetime | None = None
    decision_notes: str = ""
    decision_allowed: bool = True
    decision_disabled_reason: str = ""
    created_at: datetime


class ApprovalDecisionRequest(ApiModel):
    """A human decision.

    ``approved`` is required and has no default: approving must always be an
    explicit act, never the result of an omitted field.
    """

    approved: bool
    notes: Annotated[str, Field(max_length=4000)] = ""


class ApprovalGateDecisionRequest(ApiModel):
    """An explicit decision on one of the six production workflow gates."""

    decision: ApprovalDecision
    comment: Annotated[str, Field(max_length=4000)] = ""
