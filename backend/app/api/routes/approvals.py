"""Human approval decisions.

The gate the whole product exists to enforce. Nothing safety-related is released
as engineering guidance without a row here carrying a real person's decision, and
an agent can never write one.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Path, Query
from sqlalchemy import select

from app.api.deps import (
    AppSettings,
    CurrentUser,
    DbSession,
    ProjectRead,
    Provenance,
    authorise_project,
)
from app.api.schemas.approval import ApprovalDecisionRequest, ApprovalRead
from app.core.clock import utcnow
from app.core.enums import (
    ApprovalStatus,
    AuditAction,
    EventType,
    FindingStatus,
    RecommendationStatus,
)
from app.core.errors import ConflictError, NotFoundError, PermissionDeniedError
from app.models.approval import Approval
from app.models.finding import Finding, Recommendation
from app.models.user import User
from app.orchestration import engine
from app.orchestration.events import record_event
from app.security import audit, permissions

router = APIRouter(tags=["approvals"])


def _read(db: DbSession, approval: Approval) -> ApprovalRead:
    payload = ApprovalRead.model_validate(approval)
    if approval.decided_by_id:
        decider = db.get(User, approval.decided_by_id)
        payload.decided_by_display_name = decider.display_name if decider else ""
    return payload


@router.get(
    "/projects/{project_id}/approvals",
    response_model=list[ApprovalRead],
    summary="Decisions waiting on a human, and those already made",
)
def list_approvals(
    access: ProjectRead,
    db: DbSession,
    approval_status: Annotated[ApprovalStatus | None, Query(alias="status")] = None,
) -> list[ApprovalRead]:
    statement = select(Approval).where(Approval.project_id == access.project.id)
    if approval_status is not None:
        statement = statement.where(Approval.status == approval_status)
    rows = db.execute(statement.order_by(Approval.created_at)).scalars()
    return [_read(db, row) for row in rows]


@router.get(
    "/approvals/{approval_id}",
    response_model=ApprovalRead,
    summary="One approval, with the evidence snapshot taken when it was raised",
)
def get_approval(
    approval_id: Annotated[uuid.UUID, Path()], db: DbSession, user: CurrentUser
) -> ApprovalRead:
    approval = db.get(Approval, approval_id)
    if approval is None:
        raise NotFoundError("Approval", approval_id)
    authorise_project(db, user, approval.project_id, permissions.READ_ROLES, "view")
    return _read(db, approval)


@router.post(
    "/approvals/{approval_id}/decision",
    response_model=ApprovalRead,
    summary="Record an engineering decision",
)
def decide(
    approval_id: Annotated[uuid.UUID, Path()],
    payload: ApprovalDecisionRequest,
    db: DbSession,
    user: CurrentUser,
    settings: AppSettings,
    provenance: Provenance,
) -> ApprovalRead:
    """Approve or reject.

    Three things this enforces, none of which can be left to convention:

    * only a human decides — the caller is an authenticated user, and no agent
      action can reach this endpoint;
    * the decider needs the reviewer role *and* a reviewing role on the project,
      so an engineer cannot sign off their own project's safety findings;
    * ``approved`` has no default. Approving is always an explicit act, never the
      result of an omitted field.

    Once every approval on a project is decided, the tasks waiting on the gate
    return to the queue and the run continues.
    """
    approval = db.get(Approval, approval_id)
    if approval is None:
        raise NotFoundError("Approval", approval_id)

    access = authorise_project(
        db, user, approval.project_id, permissions.REVIEW_ROLES, "decide approvals"
    )
    if not permissions.can_decide_approval(user, access.role):
        raise PermissionDeniedError(
            "Recording an engineering approval requires the reviewer role. This is "
            "a sign-off, not a bookkeeping action."
        )
    if approval.status is not ApprovalStatus.PENDING:
        raise ConflictError(
            f"This approval was already {approval.status.value}"
            + (f" on {approval.decided_at:%Y-%m-%d}." if approval.decided_at else ".")
        )

    approval.status = ApprovalStatus.APPROVED if payload.approved else ApprovalStatus.REJECTED
    approval.decided_by_id = user.id
    approval.decided_at = utcnow()
    approval.decision_notes = payload.notes
    db.flush()

    _apply(db, approval, approved=payload.approved, user=user)

    audit.record(
        db,
        AuditAction.APPROVAL_DECIDED,
        actor_user_id=user.id,
        project_id=approval.project_id,
        target_type="approval",
        target_id=approval.id,
        summary=(f"{'Approved' if payload.approved else 'Rejected'}: {approval.title}"),
        details={
            "kind": approval.kind.value,
            "finding_id": str(approval.finding_id) if approval.finding_id else None,
            "notes": payload.notes,
        },
        source_ip=provenance.source_ip,
        user_agent=provenance.user_agent,
    )
    record_event(
        db,
        project_id=approval.project_id,
        event_type=EventType.APPROVAL_DECIDED,
        actor_user_id=user.id,
        payload={
            "approval_id": str(approval.id),
            "kind": approval.kind.value,
            "status": approval.status.value,
            "title": approval.title,
            "finding_id": str(approval.finding_id) if approval.finding_id else None,
            "recommendation_id": (
                str(approval.recommendation_id) if approval.recommendation_id else None
            ),
            "decided_by_id": str(user.id),
        },
    )

    # Once nothing is pending, the gated tasks go back to the queue and the run
    # carries on without anyone having to restart it.
    engine.unblock_approval_tasks(db, access.project, settings)
    engine.refresh_project_status(db, access.project)
    db.commit()

    return _read(db, approval)


def _apply(db: DbSession, approval: Approval, *, approved: bool, user: User) -> None:
    """Carry the decision through to whatever it was about.

    A rejected finding is marked rejected, not deleted: the record of what the
    platform said, and that a human disagreed, is worth keeping.
    """
    if approval.finding_id:
        finding = db.get(Finding, approval.finding_id)
        if finding is not None:
            finding.status = FindingStatus.ACCEPTED if approved else FindingStatus.REJECTED
            finding.decided_by_id = user.id
            finding.decided_at = utcnow()
            finding.decision_notes = approval.decision_notes

    if approval.recommendation_id:
        recommendation = db.get(Recommendation, approval.recommendation_id)
        if recommendation is not None:
            recommendation.status = (
                RecommendationStatus.APPROVED if approved else RecommendationStatus.REJECTED
            )
    db.flush()
