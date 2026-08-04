"""Canonical requirement reads and named human decisions."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field

from app.api.schemas.common import ApiModel
from app.core.enums import (
    ObjectApprovalState,
    RequirementKind,
    RequirementPriority,
    RequirementStatus,
)


class RequirementSourceRead(ApiModel):
    id: uuid.UUID
    file_id: uuid.UUID | None = None
    anchor_id: uuid.UUID | None = None
    quoted_text: str
    location_label: str
    is_primary: bool


class AcceptanceCriterionRead(ApiModel):
    id: uuid.UUID
    reference: str
    statement: str
    ordinal: int
    is_testable: bool
    test_notes: str
    status: RequirementStatus
    approval_state: ObjectApprovalState


class RequirementRead(ApiModel):
    id: uuid.UUID
    project_id: uuid.UUID
    reference: str
    title: str
    statement: str
    source_text: str
    kind: RequirementKind
    priority: RequirementPriority
    status: RequirementStatus
    approval_state: ObjectApprovalState
    is_inferred: bool
    confidence: float
    rationale: str
    verification_method: str
    sources: list[RequirementSourceRead] = Field(default_factory=list)
    acceptance_criteria: list[AcceptanceCriterionRead] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class RequirementDecisionRequest(ApiModel):
    decision: Literal["accept", "reject"]
    rationale: Annotated[str, Field(min_length=1, max_length=10_000)]


class RequirementDecisionRead(ApiModel):
    requirement: RequirementRead
    decision_id: uuid.UUID
    decided_by_id: uuid.UUID
    decided_at: datetime
    unblocked_task_count: int = 0
