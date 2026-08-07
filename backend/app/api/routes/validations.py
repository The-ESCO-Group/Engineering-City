"""Validation result reads and reviewer-only waiver decisions."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Path

from app.api.deps import (
    AppSettings,
    CurrentUser,
    DbSession,
    ProjectRead,
    Provenance,
    authorise_project,
)
from app.api.schemas.validation import (
    ValidationResultRead,
    ValidationWaiverRead,
    ValidationWaiverRequest,
)
from app.core.errors import NotFoundError, PermissionDeniedError
from app.core.services import validation_review
from app.models.release import ValidationResult
from app.security import permissions

router = APIRouter(tags=["validation"])


@router.get(
    "/projects/{project_id}/validation-results",
    response_model=list[ValidationResultRead],
    summary="Newest deterministic validation results, including recorded waivers",
)
def list_validation_results(access: ProjectRead, db: DbSession) -> list[ValidationResultRead]:
    return [
        ValidationResultRead.model_validate(row)
        for row in validation_review.latest_results(db, access.project)
    ]


@router.post(
    "/validation-results/{result_id}/waiver",
    response_model=ValidationWaiverRead,
    summary="Accept one current validation risk as a named reviewer",
)
def waive_validation_result(
    result_id: Annotated[uuid.UUID, Path()],
    payload: ValidationWaiverRequest,
    db: DbSession,
    user: CurrentUser,
    settings: AppSettings,
    provenance: Provenance,
) -> ValidationWaiverRead:
    # This first read exists only to route and authorise the request.  The
    # service takes the canonical project -> result locks before it revalidates
    # and writes, so an API caller cannot invert that order by locking the child
    # row here first.
    result = db.get(ValidationResult, result_id)
    if result is None:
        raise NotFoundError("Validation result", result_id)
    access = authorise_project(
        db,
        user,
        result.project_id,
        permissions.REVIEW_ROLES,
        "waive validation results",
    )
    if not permissions.can_decide_approval(user, access.role):
        raise PermissionDeniedError("Waiving a validation result requires the reviewer role.")
    outcome = validation_review.waive(
        db,
        project=access.project,
        result=result,
        expected_updated_at=payload.expected_updated_at,
        reason=payload.reason,
        actor=user,
        settings=settings,
        source_ip=provenance.source_ip,
        user_agent=provenance.user_agent,
    )
    db.commit()
    db.refresh(result)
    return ValidationWaiverRead(
        result=ValidationResultRead.model_validate(result),
        waived_by_id=user.id,
        waived_at=result.updated_at,
        decision_replayed=outcome.replayed,
        unblocked_task_count=outcome.unblocked_tasks,
    )
