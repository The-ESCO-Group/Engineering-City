"""Human review of requirements extracted from supplied source material."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.clock import utcnow
from app.core.config import Settings
from app.core.enums import (
    AuditAction,
    DecisionKind,
    ObjectApprovalState,
    RequirementStatus,
    TaskKind,
    TaskStatus,
)
from app.core.errors import ConflictError, NotFoundError
from app.models.profile import ProjectDecision
from app.models.project import Project
from app.models.requirements import Requirement
from app.models.task import Task
from app.models.user import User
from app.orchestration import engine
from app.orchestration.queue import DatabaseTaskQueue
from app.security import audit


def list_requirements(db: Session, project_id: uuid.UUID) -> list[Requirement]:
    return list(
        db.execute(
            select(Requirement)
            .where(Requirement.project_id == project_id, Requirement.deleted_at.is_(None))
            .options(
                selectinload(Requirement.sources),
                selectinload(Requirement.acceptance_criteria),
            )
            .order_by(Requirement.ordinal, Requirement.reference)
        ).scalars()
    )


def _unblock_profile_work(db: Session, project: Project, settings: Settings) -> int:
    """Re-evaluate work whose truth can change when requirements are decided."""
    queue = DatabaseTaskQueue(db, settings)
    rows = list(
        db.execute(
            select(Task).where(
                Task.project_id == project.id,
                Task.status == TaskStatus.BLOCKED,
                Task.kind.in_(
                    (
                        TaskKind.RESOLVE_CLARIFICATIONS,
                        TaskKind.EVALUATE_APPROVAL_GATE,
                        TaskKind.DESIGN_TAGS,
                        TaskKind.DESIGN_PLC_ARCHITECTURE,
                        TaskKind.GENERATE_LOGIC,
                    )
                ),
            )
        ).scalars()
    )
    for row in rows:
        queue.unblock(row)
    if rows:
        engine.refresh_project_status(db, project)
    return len(rows)


def decide_requirement(
    db: Session,
    *,
    project: Project,
    requirement: Requirement,
    decision: str,
    rationale: str,
    actor: User,
    settings: Settings,
    source_ip: str = "",
    user_agent: str = "",
) -> tuple[ProjectDecision, int]:
    if requirement.project_id != project.id or requirement.deleted_at is not None:
        raise NotFoundError("Requirement", requirement.id)
    if requirement.status is not RequirementStatus.PROPOSED:
        raise ConflictError(
            f"Requirement {requirement.reference} is already {requirement.status.value}."
        )
    if decision not in {"accept", "reject"}:
        raise ConflictError("Requirement decision must be accept or reject.")

    now = utcnow()
    accepted = decision == "accept"
    requirement.status = (
        RequirementStatus.ACCEPTED if accepted else RequirementStatus.REJECTED
    )
    requirement.approval_state = (
        ObjectApprovalState.APPROVED if accepted else ObjectApprovalState.REJECTED
    )
    requirement.approved_at = now if accepted else None
    requirement.approved_by_id = actor.id if accepted else None
    requirement.object_revision += 1
    requirement.updated_in_revision = project.current_revision_number
    for criterion in requirement.acceptance_criteria:
        criterion.status = requirement.status
        criterion.approval_state = requirement.approval_state
        criterion.approved_at = requirement.approved_at
        criterion.approved_by_id = requirement.approved_by_id
        criterion.object_revision += 1
        criterion.updated_in_revision = project.current_revision_number

    project_decision = ProjectDecision(
        project_id=project.id,
        reference=f"DEC-{uuid.uuid4().hex[:8].upper()}",
        kind=DecisionKind.DESIGN_CHOICE,
        title=f"{decision.title()} requirement {requirement.reference}",
        statement=f"{requirement.reference} was " + (
            "accepted as an engineering requirement."
            if accepted
            else "rejected from project scope."
        ),
        rationale=rationale.strip(),
        decided_by_id=actor.id,
        decided_by_name=actor.display_name,
        decided_at=now,
        scope={
            "requirement_id": str(requirement.id),
            "reference": requirement.reference,
            "decision": decision,
        },
        created_by_id=actor.id,
        created_in_revision=project.current_revision_number,
        updated_in_revision=project.current_revision_number,
    )
    db.add(project_decision)
    db.flush()

    audit.record(
        db,
        AuditAction.DECISION_RECORDED,
        actor_user_id=actor.id,
        project_id=project.id,
        target_type="requirement",
        target_id=requirement.id,
        summary=f"{decision.title()}ed requirement {requirement.reference}",
        details={
            "requirement_id": str(requirement.id),
            "reference": requirement.reference,
            "status": requirement.status.value,
            "decision_id": str(project_decision.id),
            "rationale": rationale.strip(),
        },
        source_ip=source_ip,
        user_agent=user_agent,
    )
    return project_decision, _unblock_profile_work(db, project, settings)
