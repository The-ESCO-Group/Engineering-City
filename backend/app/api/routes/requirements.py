"""Canonical requirement review endpoints."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Path
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.api.deps import (
    AppSettings,
    CurrentUser,
    DbSession,
    ProjectRead,
    Provenance,
    authorise_project,
)
from app.api.schemas.requirements import (
    RequirementDecisionRead,
    RequirementDecisionRequest,
    RequirementRead,
)
from app.core.errors import NotFoundError, PermissionDeniedError
from app.core.services import requirements_review
from app.models.requirements import Requirement
from app.security import permissions

router = APIRouter(tags=["requirements"])


@router.get(
    "/projects/{project_id}/requirements",
    response_model=list[RequirementRead],
    summary="Canonical requirements and their exact source citations",
)
def list_requirements(access: ProjectRead, db: DbSession) -> list[RequirementRead]:
    return [
        RequirementRead.model_validate(item)
        for item in requirements_review.list_requirements(db, access.project.id)
    ]


@router.post(
    "/requirements/{requirement_id}/decision",
    response_model=RequirementDecisionRead,
    summary="Accept or reject an extracted requirement as a named reviewer",
)
def decide_requirement(
    requirement_id: Annotated[uuid.UUID, Path()],
    payload: RequirementDecisionRequest,
    db: DbSession,
    user: CurrentUser,
    settings: AppSettings,
    provenance: Provenance,
) -> RequirementDecisionRead:
    requirement = db.execute(
        select(Requirement)
        .where(Requirement.id == requirement_id)
        .options(
            selectinload(Requirement.sources),
            selectinload(Requirement.acceptance_criteria),
        )
        .with_for_update()
    ).scalar_one_or_none()
    if requirement is None:
        raise NotFoundError("Requirement", requirement_id)
    access = authorise_project(
        db, user, requirement.project_id, permissions.REVIEW_ROLES, "decide requirements"
    )
    if not permissions.can_decide_approval(user, access.role):
        raise PermissionDeniedError(
            "Accepting or rejecting extracted requirements requires the reviewer role."
        )
    decision, unblocked = requirements_review.decide_requirement(
        db,
        project=access.project,
        requirement=requirement,
        decision=payload.decision,
        rationale=payload.rationale,
        actor=user,
        settings=settings,
        source_ip=provenance.source_ip,
        user_agent=provenance.user_agent,
    )
    db.commit()
    return RequirementDecisionRead(
        requirement=RequirementRead.model_validate(requirement),
        decision_id=decision.id,
        decided_by_id=user.id,
        decided_at=decision.decided_at,
        unblocked_task_count=unblocked,
    )
