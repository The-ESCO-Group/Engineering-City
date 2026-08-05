"""Human approval decisions.

The gate the whole product exists to enforce. Nothing safety-related is released
as engineering guidance without a row here carrying a real person's decision, and
an agent can never write one.
"""

from __future__ import annotations

import uuid
from typing import Annotated, cast

from fastapi import APIRouter, Path, Query
from sqlalchemy import select, update

from app.api.deps import (
    AppSettings,
    CurrentUser,
    DbSession,
    ProjectRead,
    Provenance,
    authorise_project,
)
from app.api.schemas.approval import (
    ApprovalDecisionRequest,
    ApprovalGateDecisionRequest,
    ApprovalRead,
)
from app.core.clock import utcnow
from app.core.enums import (
    ApprovalDecision,
    ApprovalKind,
    ApprovalStatus,
    AuditAction,
    EventType,
    FindingStatus,
    GateStatus,
    ProjectRole,
    ProjectStatus,
    RecommendationStatus,
    TaskStatus,
)
from app.core.errors import ConflictError, NotFoundError, PermissionDeniedError
from app.engineering import gates
from app.models.approval import Approval
from app.models.finding import Finding, Recommendation
from app.models.project import Project, ProjectMember
from app.models.revision import ApprovalGate
from app.models.task import Task
from app.models.user import User
from app.orchestration import engine
from app.orchestration.events import record_event
from app.security import audit, permissions

router = APIRouter(tags=["approvals"])


def _read(db: DbSession, approval: Approval) -> ApprovalRead:
    payload = ApprovalRead.model_validate(approval)
    if approval.gate_key is not None:
        gate = db.execute(
            select(ApprovalGate).where(
                ApprovalGate.project_id == approval.project_id,
                ApprovalGate.gate_key == approval.gate_key,
            )
        ).scalar_one_or_none()
        payload.gate_id = gate.id if gate is not None else None
    allowed, reason = _decision_eligibility(db, approval)
    payload.decision_allowed = allowed
    payload.decision_disabled_reason = reason
    if approval.decided_by_id:
        decider = db.get(User, approval.decided_by_id)
        payload.decided_by_display_name = decider.display_name if decider else ""
    return payload


def _decision_eligibility(
    db: DbSession, approval: Approval, *, lock_subject: bool = False
) -> tuple[bool, str]:
    """Whether this row may receive a human decision, with a UI-ready reason."""
    if approval.status is not ApprovalStatus.PENDING:
        return False, f"This approval was already {approval.status.value}."
    if approval.kind is ApprovalKind.GATE_DECISION or approval.gate_key is not None:
        # Eligibility is advisory. Never take a gate lock here without first
        # taking the project lock; the decision service owns that lock order.
        gate = _bound_gate(db, approval, required=False)
        if gate is None:
            return (
                False,
                "This approval is not bound to the workflow gate that requested it.",
            )
        if gate.status is not GateStatus.AWAITING_DECISION:
            return (
                False,
                f"The workflow gate is {gate.status.value}, not awaiting a decision.",
            )
        return True, ""
    if approval.finding_id is None:
        return True, ""

    finding = (
        db.execute(
            select(Finding).where(Finding.id == approval.finding_id).with_for_update()
        ).scalar_one_or_none()
        if lock_subject
        else db.get(Finding, approval.finding_id)
    )
    if finding is None:
        return False, "The finding attached to this approval no longer exists."
    if not finding.is_reviewed:
        return (
            False,
            "Independent senior review has not finished for this finding. A human "
            "decision is disabled until the senior-review verdict is recorded.",
        )
    return True, ""


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
    # Gate decisions deliberately do not lock the approval here. The gate
    # service owns the canonical project -> gate -> approval lock order; taking
    # the child lock first in this route would deadlock against a concurrent
    # merge or gate request. Ordinary approvals are locked below after routing.
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

    # Compatibility route: the original API exposed an approve/reject boolean.
    # Gate approvals must still pass through the production gate service, which
    # is the only place that atomically updates both the approval and its gate.
    if approval.kind is ApprovalKind.GATE_DECISION or approval.gate_key is not None:
        gate = cast(ApprovalGate, _bound_gate(db, approval))
        decision = ApprovalDecision.APPROVE if payload.approved else ApprovalDecision.REJECT
        return _record_gate_decision(
            db,
            approval=approval,
            gate=gate,
            decision=decision,
            comment=payload.notes,
            user=user,
            settings=settings,
        )

    return _record_ordinary_decision(
        db,
        approval_id=approval.id,
        project_id=approval.project_id,
        approved=payload.approved,
        notes=payload.notes,
        user=user,
        settings=settings,
        provenance=provenance,
    )


def _record_ordinary_decision(
    db: DbSession,
    *,
    approval_id: uuid.UUID,
    project_id: uuid.UUID,
    approved: bool,
    notes: str,
    user: User,
    settings: AppSettings,
    provenance: Provenance,
) -> ApprovalRead:
    """Settle a non-gate approval once, against fresh authoritative rows.

    The preliminary child read in :func:`decide` is routing and authorization
    only.  This function starts the actual transition by taking a write lock on
    the project root.  A no-op ``UPDATE`` is intentional: PostgreSQL treats it
    as the canonical project row lock and SQLite, where ``FOR UPDATE`` is
    ignored, treats it as write intent that serializes the API and worker
    connections before either may rely on approval state.
    """
    root_lock = db.execute(
        update(Project)
        .where(Project.id == project_id)
        .values(id=Project.id, updated_at=Project.updated_at)
        .execution_options(synchronize_session=False)
    )
    if int(getattr(root_lock, "rowcount", 0) or 0) != 1:
        raise NotFoundError("Project", project_id)

    project = db.execute(
        select(Project)
        .where(Project.id == project_id)
        .execution_options(populate_existing=True)
    ).scalar_one()
    if project.status is ProjectStatus.ARCHIVED:
        raise ConflictError("Archived projects are read-only; this approval cannot be changed.")

    # Route-level authorization is an early response, not the invariant.  Roles
    # and account state can change after that read, so refresh the named human
    # under the project transition and check authority again here.
    actor = db.execute(
        select(User)
        .where(User.id == user.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if actor is None or not actor.is_active:
        raise PermissionDeniedError(
            "A deactivated or missing account cannot record an engineering approval."
        )
    db.expire(actor, ["roles"])
    membership = db.execute(
        select(ProjectMember)
        .where(ProjectMember.project_id == project.id, ProjectMember.user_id == actor.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    role = (
        ProjectRole.OWNER
        if actor.is_administrator
        else (ProjectRole(membership.role) if membership is not None else None)
    )
    if not permissions.can_decide_approval(actor, role):
        raise PermissionDeniedError(
            "Recording an engineering approval requires the reviewer role. This is "
            "a sign-off, not a bookkeeping action."
        )

    approval = db.execute(
        select(Approval)
        .where(Approval.id == approval_id, Approval.project_id == project.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if approval is None:
        raise ConflictError("This approval no longer belongs to the authorized project.")
    if approval.kind is ApprovalKind.GATE_DECISION or approval.gate_key is not None:
        raise ConflictError(
            "Workflow gate decisions must use the gate-bound decision transition."
        )
    if approval.status is not ApprovalStatus.PENDING:
        raise ConflictError(
            f"This approval was already {approval.status.value}"
            + (f" on {approval.decided_at:%Y-%m-%d}." if approval.decided_at else ".")
        )

    decision_allowed, disabled_reason = _decision_eligibility(db, approval, lock_subject=True)
    if not decision_allowed:
        raise ConflictError(disabled_reason)

    decided_at = utcnow()
    resulting_status = ApprovalStatus.APPROVED if approved else ApprovalStatus.REJECTED
    transition = db.execute(
        update(Approval)
        .where(
            Approval.id == approval.id,
            Approval.project_id == project.id,
            Approval.status == ApprovalStatus.PENDING,
        )
        .values(
            status=resulting_status,
            decision=(ApprovalDecision.APPROVE if approved else ApprovalDecision.REJECT),
            decided_by_id=actor.id,
            decided_at=decided_at,
            decision_notes=notes,
        )
        .execution_options(synchronize_session=False)
    )
    if int(getattr(transition, "rowcount", 0) or 0) != 1:
        db.refresh(approval)
        raise ConflictError(
            f"This approval was already {approval.status.value}"
            + (f" on {approval.decided_at:%Y-%m-%d}." if approval.decided_at else ".")
        )
    db.refresh(approval)

    _apply(db, approval, approved=approved, user=actor)

    cancelled_run = _approval_run_was_cancelled(db, approval)
    cancelled_run_is_inactive = cancelled_run and project.status is ProjectStatus.PLANNED

    audit.record(
        db,
        AuditAction.APPROVAL_DECIDED,
        actor_user_id=actor.id,
        project_id=approval.project_id,
        target_type="approval",
        target_id=approval.id,
        summary=(f"{'Approved' if approved else 'Rejected'}: {approval.title}"),
        details={
            "kind": approval.kind.value,
            "finding_id": str(approval.finding_id) if approval.finding_id else None,
            "notes": notes,
            "cancelled_run": cancelled_run,
        },
        source_ip=provenance.source_ip,
        user_agent=provenance.user_agent,
    )
    record_event(
        db,
        project_id=approval.project_id,
        event_type=EventType.APPROVAL_DECIDED,
        actor_user_id=actor.id,
        payload={
            "approval_id": str(approval.id),
            "kind": approval.kind.value,
            "status": approval.status.value,
            "title": approval.title,
            "finding_id": str(approval.finding_id) if approval.finding_id else None,
            "recommendation_id": (
                str(approval.recommendation_id) if approval.recommendation_id else None
            ),
            "decided_by_id": str(actor.id),
        },
    )

    # Findings from a cancelled run remain reviewable historical evidence, but
    # a late click may not resurrect a project that is still PLANNED or move it
    # to COMPLETED.  A fresh active run may legitimately reuse the same pending
    # finding review, in which case settling it may release that new run.
    if not cancelled_run_is_inactive:
        engine.unblock_approval_tasks(db, project, settings)
        engine.refresh_project_status(db, project)
    db.commit()

    return _read(db, approval)


def _approval_run_was_cancelled(db: DbSession, approval: Approval) -> bool:
    """Whether this approval belongs to a run an operator already cancelled."""
    if approval.task_id is None:
        return False
    task = db.execute(
        select(Task)
        .where(Task.id == approval.task_id, Task.project_id == approval.project_id)
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if task is None:
        return False
    if task.status is TaskStatus.CANCELLED:
        return True
    if task.run_id is None:
        return False
    return (
        db.execute(
            select(Task.id)
            .where(
                Task.project_id == approval.project_id,
                Task.run_id == task.run_id,
                Task.status == TaskStatus.CANCELLED,
            )
            .limit(1)
        ).first()
        is not None
    )


@router.post(
    "/approval-gates/{gate_id}/approvals/{approval_id}/decision",
    response_model=ApprovalRead,
    summary="Record an explicit workflow gate decision",
)
def decide_gate(
    gate_id: Annotated[uuid.UUID, Path()],
    approval_id: Annotated[uuid.UUID, Path()],
    payload: ApprovalGateDecisionRequest,
    db: DbSession,
    user: CurrentUser,
    settings: AppSettings,
) -> ApprovalRead:
    """Record any supported gate verdict against an exact gate/request pair.

    The gate and approval identifiers are both in the route so a stale client
    cannot accidentally apply a decision to a newer request for the same gate.
    ``record_decision`` remains the sole state transition authority.
    """
    # Do not invert the gate service's project -> gate -> approval lock order.
    # This first read exists only to route and authorize the request; the
    # service locks and revalidates the exact rows before changing anything.
    approval = db.get(Approval, approval_id)
    if approval is None:
        raise NotFoundError("Approval", approval_id)

    access = authorise_project(
        db, user, approval.project_id, permissions.REVIEW_ROLES, "decide approval gates"
    )
    if not permissions.can_decide_approval(user, access.role):
        raise PermissionDeniedError(
            "Recording an engineering approval requires the reviewer role. This is "
            "a sign-off, not a bookkeeping action."
        )

    gate = cast(ApprovalGate, _bound_gate(db, approval, gate_id=gate_id))
    return _record_gate_decision(
        db,
        approval=approval,
        gate=gate,
        decision=payload.decision,
        comment=payload.comment,
        user=user,
        settings=settings,
    )


def _bound_gate(
    db: DbSession,
    approval: Approval,
    *,
    gate_id: uuid.UUID | None = None,
    required: bool = True,
) -> ApprovalGate | None:
    """Load the exact gate/request binding, failing closed on malformed rows."""
    if approval.kind is not ApprovalKind.GATE_DECISION or approval.gate_key is None:
        if required:
            raise ConflictError("This approval is not a valid workflow gate decision request.")
        return None

    statement = select(ApprovalGate).where(
        ApprovalGate.project_id == approval.project_id,
        ApprovalGate.gate_key == approval.gate_key,
        ApprovalGate.approval_id == approval.id,
    )
    if gate_id is not None:
        statement = statement.where(ApprovalGate.id == gate_id)
    gate = db.execute(statement).scalar_one_or_none()
    if gate is None and required:
        raise ConflictError(
            "This approval is not bound to the workflow gate identified by the request."
        )
    return gate


def _record_gate_decision(
    db: DbSession,
    *,
    approval: Approval,
    gate: ApprovalGate,
    decision: ApprovalDecision,
    comment: str,
    user: User,
    settings: AppSettings,
) -> ApprovalRead:
    """Apply one gate decision and resume work in the same transaction."""
    try:
        gates.record_decision(
            db,
            gate=gate,
            approval=approval,
            decision=decision,
            user=user,
            comment=comment,
        )
    except ConflictError:
        # Re-evaluation may discover that the request is stale and cancel it.
        # Persist that convergence before returning 409; otherwise the request
        # transaction would roll the cancellation back and the orphaned
        # ``pending`` row would continue to hold every gated task indefinitely.
        # Other conflicts leave the approval pending and remain fully atomic.
        if approval.status is ApprovalStatus.CANCELLED:
            db.commit()
        raise

    # Reconcile the queue in the same transaction. The orchestration layer
    # resumes work only when the resulting gate state permits it; request-changes
    # and defer remain held under the gate service's decision-specific rules.
    project = db.get(Project, gate.project_id)
    if project is None:  # pragma: no cover - the FK prevents this in supported databases
        raise ConflictError("The project attached to this workflow gate no longer exists.")
    engine.unblock_approval_tasks(db, project, settings)
    engine.refresh_project_status(db, project)
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
