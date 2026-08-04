"""Canonical engineering profile and human clarification endpoints."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Path

from app.api.deps import (
    AppSettings,
    CurrentUser,
    DbSession,
    ProjectRead,
    ProjectWrite,
    authorise_project,
)
from app.api.schemas.engineering import (
    ClarificationInbox,
    ClarificationRead,
    ClarificationResolutionRead,
    EngineeringProfileInput,
    EngineeringProfileRead,
    ProjectQuestionRead,
    ResolveClarificationRequest,
    TargetPlatformRead,
)
from app.core.enums import ClarificationStatus
from app.core.errors import NotFoundError
from app.core.services import engineering_intake
from app.models.profile import ProjectQuestion
from app.models.requirements import Clarification
from app.security import permissions

router = APIRouter(tags=["engineering"])


def _profile_read(project_id: uuid.UUID, db: DbSession) -> EngineeringProfileRead:
    profile, target = engineering_intake.get_profile(db, project_id)
    return EngineeringProfileRead(
        id=profile.id,
        project_id=profile.project_id,
        project_name=profile.project_name,
        customer_label=profile.customer_label,
        plant_or_site=profile.plant_or_site,
        project_description=profile.project_description,
        project_type=profile.project_type,
        naming_standard_version=profile.naming_standard_version,
        alarm_philosophy_version=profile.alarm_philosophy_version,
        template_set_id=profile.template_set_id,
        engineering_standard_set_id=profile.engineering_standard_set_id,
        units=profile.units,
        locale=profile.locale,
        network_architecture=profile.network_architecture,
        release_mode=profile.release_mode,
        restricted_domains=list(profile.restricted_domains),
        restricted_domain_mode=profile.restricted_domain_mode,
        completeness=dict(profile.completeness),
        missing_required_fields=list(profile.missing_required_fields),
        is_complete_for_generation=profile.is_complete_for_generation,
        blocks_generation=profile.blocks_generation,
        target=TargetPlatformRead.model_validate(target),
        created_at=profile.created_at,
        updated_at=profile.updated_at,
    )


@router.get(
    "/projects/{project_id}/engineering-profile",
    response_model=EngineeringProfileRead,
    summary="Canonical engineering profile and exact target platform",
)
def get_engineering_profile(access: ProjectRead, db: DbSession) -> EngineeringProfileRead:
    return _profile_read(access.project.id, db)


@router.put(
    "/projects/{project_id}/engineering-profile",
    response_model=EngineeringProfileRead,
    summary="Record engineering intake before workflow work begins",
)
def put_engineering_profile(
    payload: EngineeringProfileInput,
    access: ProjectWrite,
    db: DbSession,
) -> EngineeringProfileRead:
    engineering_intake.configure_project(
        db,
        project=access.project,
        payload=payload,
        actor=access.user,
    )
    db.commit()
    return _profile_read(access.project.id, db)


def _question_read(row: ProjectQuestion) -> ProjectQuestionRead:
    return ProjectQuestionRead(
        id=row.id,
        project_id=row.project_id,
        reference=row.reference,
        category=row.category,
        question=row.question,
        why_it_matters=row.why_it_matters,
        status=row.status,
        is_mandatory=row.is_mandatory,
        is_blocking=row.is_blocking,
        blocks_task_kinds=list(row.blocks_task_kinds),
        answer=row.answer,
        answered_by_id=row.answered_by_id,
        answered_at=row.answered_at,
        asked_by_agent=row.asked_by_agent,
        raised_in_task_id=row.raised_in_task_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


@router.get(
    "/projects/{project_id}/clarifications",
    response_model=ClarificationInbox,
    summary="Questions and ambiguities that need a named human answer",
)
def list_clarifications(access: ProjectRead, db: DbSession) -> ClarificationInbox:
    questions, clarifications = engineering_intake.clarification_inbox(db, access.project.id)
    question_reads = [_question_read(row) for row in questions]
    clarification_reads = [ClarificationRead.model_validate(row) for row in clarifications]
    return ClarificationInbox(
        project_questions=question_reads,
        clarifications=clarification_reads,
        blocking_count=(
            sum(1 for row in questions if row.is_blocking)
            + sum(
                1
                for row in clarifications
                if row.blocks_generation
                and row.status in {ClarificationStatus.OPEN, ClarificationStatus.DEFERRED}
            )
        ),
    )


@router.post(
    "/project-questions/{question_id}/resolve",
    response_model=ClarificationResolutionRead,
    summary="Resolve a project-level question as a named human decision",
)
def resolve_project_question(
    question_id: Annotated[uuid.UUID, Path()],
    payload: ResolveClarificationRequest,
    db: DbSession,
    user: CurrentUser,
    settings: AppSettings,
) -> ClarificationResolutionRead:
    question = db.get(ProjectQuestion, question_id)
    if question is None:
        raise NotFoundError("Project question", question_id)
    access = authorise_project(
        db, user, question.project_id, permissions.WRITE_ROLES, "answer project questions"
    )
    decision, unblocked = engineering_intake.resolve_project_question(
        db,
        project=access.project,
        question=question,
        answer=payload.answer,
        resolution=payload.resolution,
        actor=user,
        settings=settings,
    )
    db.commit()
    return ClarificationResolutionRead(
        kind="project_question",
        id=question.id,
        project_id=question.project_id,
        status=question.status,
        decision_id=decision.id,
        answered_by_id=user.id,
        answered_at=question.answered_at,
        unblocked_task_count=unblocked,
    )


@router.post(
    "/clarifications/{clarification_id}/resolve",
    response_model=ClarificationResolutionRead,
    summary="Resolve a requirement or object ambiguity as a named human decision",
)
def resolve_clarification(
    clarification_id: Annotated[uuid.UUID, Path()],
    payload: ResolveClarificationRequest,
    db: DbSession,
    user: CurrentUser,
    settings: AppSettings,
) -> ClarificationResolutionRead:
    clarification = db.get(Clarification, clarification_id)
    if clarification is None:
        raise NotFoundError("Clarification", clarification_id)
    access = authorise_project(
        db, user, clarification.project_id, permissions.WRITE_ROLES, "answer clarifications"
    )
    decision, unblocked = engineering_intake.resolve_clarification(
        db,
        project=access.project,
        clarification=clarification,
        answer=payload.answer,
        resolution=payload.resolution,
        actor=user,
        settings=settings,
    )
    db.commit()
    return ClarificationResolutionRead(
        kind="clarification",
        id=clarification.id,
        project_id=clarification.project_id,
        status=clarification.status,
        decision_id=decision.id,
        answered_by_id=user.id,
        answered_at=clarification.resolved_at,
        unblocked_task_count=unblocked,
    )
