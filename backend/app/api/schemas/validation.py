"""Deterministic validation results and named human waivers."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import Field

from app.api.schemas.common import ApiModel
from app.core.enums import ObjectType, ValidationDiscipline, ValidationSeverity


class ValidationResultRead(ApiModel):
    id: uuid.UUID
    project_id: uuid.UUID
    validation_run_id: uuid.UUID
    code: str
    discipline: ValidationDiscipline
    severity: ValidationSeverity
    message: str
    detail: dict[str, Any] = Field(default_factory=dict)
    object_type: ObjectType | None = None
    object_id: uuid.UUID | None = None
    object_label: str = ""
    suggested_action: str = ""
    requires_human_decision: bool
    is_waived: bool
    waived_by_id: uuid.UUID | None = None
    waiver_reason: str = ""
    created_at: datetime
    updated_at: datetime


class ValidationWaiverRequest(ApiModel):
    confirm: Literal[True]
    reason: Annotated[str, Field(min_length=1, max_length=2_000)]
    expected_updated_at: datetime


class ValidationWaiverRead(ApiModel):
    result: ValidationResultRead
    waived_by_id: uuid.UUID
    waived_at: datetime
    decision_replayed: bool = False
    unblocked_task_count: int = 0


__all__ = ["ValidationResultRead", "ValidationWaiverRead", "ValidationWaiverRequest"]
