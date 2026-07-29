"""Finding, evidence-link, comment and recommendation schemas."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from pydantic import Field

from app.api.schemas.common import ApiModel
from app.api.schemas.file import EvidenceAnchorRead
from app.core.enums import (
    EvidenceKind,
    FindingCategory,
    FindingSeverity,
    FindingStatus,
    RecommendationStatus,
)


class FindingEvidenceRead(ApiModel):
    """One citation attached to a finding, with the role it plays."""

    id: uuid.UUID
    anchor_id: uuid.UUID
    evidence_kind: EvidenceKind
    role: str = Field(
        default="context",
        description="requirement | logic | context — which side of a comparison this supports.",
    )
    note: str = ""
    anchor: EvidenceAnchorRead | None = None


class FindingRead(ApiModel):
    id: uuid.UUID
    project_id: uuid.UUID
    title: str
    category: FindingCategory
    severity: FindingSeverity
    status: FindingStatus
    fact: str = Field(description="What the evidence literally shows.")
    inference: str = Field(description="What the agent concluded. Always a conclusion, not a fact.")
    suggested_review_action: str = ""
    limitations: str = ""
    confidence: float
    requires_approval: bool
    is_reviewed: bool
    created_by_agent_kind: str = ""
    agent_run_id: uuid.UUID | None = None
    task_id: uuid.UUID | None = None
    review_verdict: str = ""
    review_notes: str = ""
    reviewed_at: datetime | None = None
    original_confidence: float | None = None
    original_severity: str = ""
    superseded_by_id: uuid.UUID | None = None
    decided_by_id: uuid.UUID | None = None
    decided_at: datetime | None = None
    decision_notes: str = ""
    evidence_count: int = 0
    created_at: datetime
    updated_at: datetime


class FindingDetail(FindingRead):
    evidence: list[FindingEvidenceRead] = Field(default_factory=list)
    details: dict[str, Any] = Field(default_factory=dict)
    warnings: list[Any] = Field(default_factory=list)
    recommendations: list[RecommendationRead] = Field(default_factory=list)


class UpdateFindingRequest(ApiModel):
    """Human disposition of a finding.

    Only ``status`` and reviewer notes are editable. The agent-produced content
    (fact, inference, evidence) is immutable so the record of what the platform
    actually said cannot be rewritten after the fact.
    """

    status: FindingStatus
    decision_notes: Annotated[str, Field(max_length=4000)] = ""


class CreateFindingCommentRequest(ApiModel):
    body: Annotated[str, Field(min_length=1, max_length=8000)]


class FindingCommentRead(ApiModel):
    id: uuid.UUID
    finding_id: uuid.UUID
    author_id: uuid.UUID | None = None
    author_display_name: str = ""
    body: str
    created_at: datetime


class RecommendationRead(ApiModel):
    id: uuid.UUID
    project_id: uuid.UUID
    finding_id: uuid.UUID | None = None
    title: str
    body: str = ""
    rationale: str = ""
    status: RecommendationStatus
    safety_related: bool = False
    equipment_damage_related: bool = False
    requires_approval: bool = True
    confidence: float = 0.0
    effort_estimate: str = ""
    created_by_agent_kind: str = ""
    details: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class DiscrepancyMatrixRow(ApiModel):
    """One row of the Interlock Review Agent's structured comparison.

    Published as part of the contract because the frontend renders it as a
    table and the reporting plugins consume it.
    """

    interlock_id: str
    description: str = ""
    requirement_evidence_ids: list[uuid.UUID] = Field(default_factory=list)
    logic_evidence_ids: list[uuid.UUID] = Field(default_factory=list)
    comparison_result: str = Field(
        description=(
            "matched | missing_in_logic | missing_in_documentation | "
            "contradictory | naming_mismatch | ambiguous"
        )
    )
    severity: FindingSeverity
    confidence: float
    suggested_review_action: str = ""
    requires_approval: bool = False
    notes: str = ""


FindingDetail.model_rebuild()
