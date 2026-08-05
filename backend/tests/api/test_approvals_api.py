"""Approval decisions are unavailable until their finding was independently reviewed."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from app.api.deps import RequestContext
from app.api.routes import approvals as approval_routes
from app.core.clock import utcnow
from app.core.config import Settings
from app.core.enums import (
    ApprovalDecision,
    ApprovalGateKey,
    ApprovalKind,
    ApprovalStatus,
    AuditAction,
    EventType,
    FindingSeverity,
    FindingStatus,
    GateStatus,
    ProjectStatus,
    TaskKind,
    TaskStatus,
)
from app.core.errors import ConflictError, PermissionDeniedError
from app.engineering import gates
from app.models.approval import Approval
from app.models.audit import AuditEntry
from app.models.event import Event
from app.models.finding import Finding
from app.models.project import Project
from app.models.revision import ApprovalGate
from app.models.task import Task
from app.models.user import User
from tests.conftest import AuthedClient


def _finding_approval(db: Session, project: Project) -> tuple[Finding, Approval]:
    finding = Finding(
        project_id=project.id,
        title="Guard interlock has no implementing rung",
        severity=FindingSeverity.HIGH,
        status=FindingStatus.OPEN,
        requires_approval=True,
        is_reviewed=False,
        dedupe_key="approval-senior-review-order",
    )
    db.add(finding)
    db.flush()
    approval = Approval(
        project_id=project.id,
        finding_id=finding.id,
        kind=ApprovalKind.FINDING_RELEASE,
        status=ApprovalStatus.PENDING,
        title="Release guard-interlock finding",
        question="May this finding be included in the engineering release?",
        requested_at=utcnow(),
    )
    db.add(approval)
    db.commit()
    return finding, approval


def _gate_approval(db: Session, project: Project) -> tuple[ApprovalGate, Approval]:
    """A bound request at gate 1, isolated from the gate criteria under test elsewhere."""
    gate = gates.ensure_gates(db, project=project)[0]
    approval = Approval(
        project_id=project.id,
        gate_key=gate.gate_key,
        kind=ApprovalKind.GATE_DECISION,
        status=ApprovalStatus.PENDING,
        revision_number=project.current_revision_number,
        title=f"Approval gate: {gate.title}",
        question="May this engineering workflow gate proceed?",
        requested_by_agent_kind="project_manager",
        requested_at=utcnow(),
    )
    db.add(approval)
    db.flush()
    gate.approval_id = approval.id
    gate.status = GateStatus.AWAITING_DECISION
    project.status = ProjectStatus.AWAITING_REVIEW
    db.commit()
    return gate, approval


def _waiting_task(db: Session, project: Project) -> Task:
    task = Task(
        project_id=project.id,
        kind=TaskKind.REVIEW_PLC,
        title="Continue after gate decision",
        status=TaskStatus.AWAITING_APPROVAL,
        idempotency_key="api-gate-wait",
        blocked_reason="Waiting on a workflow gate decision.",
    )
    db.add(task)
    db.commit()
    return task


def _count(
    db: Session,
    model: type[AuditEntry] | type[Event],
    *criteria: ColumnElement[bool],
) -> int:
    return int(db.execute(select(func.count()).select_from(model).where(*criteria)).scalar_one())


@pytest.fixture
def current_gate_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the synthetic request current; gate currency itself is covered in gate tests."""
    monkeypatch.setattr(
        gates,
        "evaluate_gate",
        lambda db, *, project, gate: gate,
    )


def test_unreviewed_finding_approval_is_visible_but_cannot_be_decided(
    db: Session, project: Project, reviewer_client: AuthedClient
) -> None:
    finding, approval = _finding_approval(db, project)

    listed = reviewer_client.get(f"/api/projects/{project.id}/approvals")

    assert listed.status_code == 200
    row = next(item for item in listed.json() if item["id"] == str(approval.id))
    assert row["decision_allowed"] is False
    assert "senior review" in row["decision_disabled_reason"].lower()

    response = reviewer_client.post(
        f"/api/approvals/{approval.id}/decision",
        json={"approved": True, "notes": "Reviewed."},
    )

    assert response.status_code == 409
    assert "senior review" in response.text.lower()
    db.expire_all()
    assert db.get(Approval, approval.id).status is ApprovalStatus.PENDING
    assert db.get(Finding, finding.id).status is FindingStatus.OPEN


def test_reviewed_finding_approval_becomes_eligible_and_can_be_decided(
    db: Session, project: Project, reviewer_client: AuthedClient
) -> None:
    finding, approval = _finding_approval(db, project)
    finding.is_reviewed = True
    finding.review_verdict = "supported"
    finding.reviewed_at = utcnow()
    db.commit()

    detail = reviewer_client.get(f"/api/approvals/{approval.id}")

    assert detail.status_code == 200
    assert detail.json()["decision_allowed"] is True
    assert detail.json()["decision_disabled_reason"] == ""

    response = reviewer_client.post(
        f"/api/approvals/{approval.id}/decision",
        json={"approved": True, "notes": "Evidence and senior verdict checked."},
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == ApprovalStatus.APPROVED.value
    assert response.json()["decision"] == ApprovalDecision.APPROVE.value
    assert response.json()["decision_allowed"] is False
    db.expire_all()
    assert db.get(Finding, finding.id).status is FindingStatus.ACCEPTED


def test_stale_ordinary_decision_cannot_overwrite_the_first_reviewer(
    db: Session,
    project: Project,
    reviewer_client: AuthedClient,
    settings: Settings,
) -> None:
    """A lock SELECT must refresh a row the route already placed in the identity map."""
    finding, approval = _finding_approval(db, project)
    finding.is_reviewed = True
    finding.review_verdict = "supported"
    finding.reviewed_at = utcnow()
    db.commit()

    stale = Session(bind=db.get_bind(), autoflush=False, expire_on_commit=False)
    try:
        assert stale.get(Approval, approval.id).status is ApprovalStatus.PENDING
        assert stale.get(Project, project.id) is not None
        actor = stale.get(User, reviewer_client.user.id)
        assert actor is not None
        # Retain the stale identity-map snapshot without retaining a SQLite read
        # transaction; the winner below is an independent request/session.
        stale.commit()

        winner = reviewer_client.post(
            f"/api/approvals/{approval.id}/decision",
            json={"approved": False, "notes": "First reviewer rejected the release."},
        )
        assert winner.status_code == 200, winner.text
        assert winner.json()["status"] == ApprovalStatus.REJECTED.value

        with pytest.raises(ConflictError, match="already rejected"):
            approval_routes._record_ordinary_decision(
                stale,
                approval_id=approval.id,
                project_id=project.id,
                approved=True,
                notes="A stale tab tried to reverse the first reviewer.",
                user=actor,
                settings=settings,
                provenance=RequestContext(),
            )
        stale.rollback()
    finally:
        stale.close()

    db.expire_all()
    decided = db.get(Approval, approval.id)
    decided_finding = db.get(Finding, finding.id)
    assert decided is not None
    assert decided.status is ApprovalStatus.REJECTED
    assert decided.decision is ApprovalDecision.REJECT
    assert decided.decision_notes == "First reviewer rejected the release."
    assert decided_finding is not None
    assert decided_finding.status is FindingStatus.REJECTED
    assert (
        _count(
            db,
            AuditEntry,
            AuditEntry.project_id == project.id,
            AuditEntry.action == AuditAction.APPROVAL_DECIDED,
            AuditEntry.target_id == approval.id,
        )
        == 1
    )


def test_ordinary_decision_service_rechecks_reviewer_authority(
    db: Session,
    project: Project,
    engineer_client: AuthedClient,
    settings: Settings,
) -> None:
    _, approval = _finding_approval(db, project)
    isolated = Session(bind=db.get_bind(), autoflush=False, expire_on_commit=False)
    try:
        actor = isolated.get(User, engineer_client.user.id)
        assert actor is not None
        with pytest.raises(PermissionDeniedError, match="requires the reviewer role"):
            approval_routes._record_ordinary_decision(
                isolated,
                approval_id=approval.id,
                project_id=project.id,
                approved=True,
                notes="Self-approval attempted through the service.",
                user=actor,
                settings=settings,
                provenance=RequestContext(),
            )
        isolated.rollback()
    finally:
        isolated.close()

    db.expire_all()
    assert db.get(Approval, approval.id).status is ApprovalStatus.PENDING


def test_historical_approval_after_cancel_does_not_restart_the_cancelled_run(
    db: Session,
    project: Project,
    reviewer_client: AuthedClient,
) -> None:
    finding, approval = _finding_approval(db, project)
    finding.is_reviewed = True
    finding.review_verdict = "supported"
    finding.reviewed_at = utcnow()
    run_id = uuid.uuid4()
    source = Task(
        project_id=project.id,
        run_id=run_id,
        kind=TaskKind.REQUEST_APPROVALS,
        title="Raise findings for review",
        status=TaskStatus.COMPLETED,
        idempotency_key=f"cancelled-approval-source:{run_id}",
    )
    stopped = Task(
        project_id=project.id,
        run_id=run_id,
        kind=TaskKind.FINALIZE_PROJECT,
        title="Finalize cancelled run",
        status=TaskStatus.CANCELLED,
        idempotency_key=f"cancelled-approval-tail:{run_id}",
        blocked_reason="The run was cancelled.",
        finished_at=utcnow(),
    )
    db.add_all([source, stopped])
    db.flush()
    approval.task_id = source.id
    project.status = ProjectStatus.PLANNED
    db.commit()

    response = reviewer_client.post(
        f"/api/approvals/{approval.id}/decision",
        json={"approved": True, "notes": "Historical finding reviewed after cancellation."},
    )

    assert response.status_code == 200, response.text
    db.expire_all()
    assert db.get(Approval, approval.id).status is ApprovalStatus.APPROVED
    assert db.get(Finding, finding.id).status is FindingStatus.ACCEPTED
    assert db.get(Project, project.id).status is ProjectStatus.PLANNED
    assert db.get(Task, source.id).status is TaskStatus.COMPLETED
    assert db.get(Task, stopped.id).status is TaskStatus.CANCELLED


def test_legacy_boolean_route_uses_gate_service_and_resumes_workflow(
    db: Session,
    project: Project,
    reviewer_client: AuthedClient,
    current_gate_request: None,
) -> None:
    gate, approval = _gate_approval(db, project)
    task = _waiting_task(db, project)

    response = reviewer_client.post(
        f"/api/approvals/{approval.id}/decision",
        json={"approved": True, "notes": "Profile and target evidence checked."},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == ApprovalStatus.APPROVED.value
    assert {
        key: body[key]
        for key in (
            "gate_id",
            "gate_key",
            "revision_number",
            "release_candidate_id",
            "decision",
        )
    } == {
        "gate_id": str(gate.id),
        "gate_key": gate.gate_key.value,
        "revision_number": approval.revision_number,
        "release_candidate_id": None,
        "decision": ApprovalDecision.APPROVE.value,
    }
    db.expire_all()
    decided = db.get(Approval, approval.id)
    decided_gate = db.get(ApprovalGate, gate.id)
    resumed = db.get(Task, task.id)
    assert decided is not None
    assert decided.decision is ApprovalDecision.APPROVE
    assert decided.decision_notes == "Profile and target evidence checked."
    assert decided.decided_by_id == reviewer_client.user.id
    assert decided_gate is not None
    assert decided_gate.status is GateStatus.PASSED
    assert decided_gate.decided_by_id == reviewer_client.user.id
    assert decided_gate.decided_revision_number == approval.revision_number
    assert resumed is not None
    assert resumed.status is TaskStatus.QUEUED
    assert resumed.blocked_reason == ""
    assert db.get(Project, project.id).status is ProjectStatus.RUNNING

    assert (
        _count(
            db,
            AuditEntry,
            AuditEntry.project_id == project.id,
            AuditEntry.action == AuditAction.GATE_DECIDED,
            AuditEntry.target_id == gate.id,
        )
        == 1
    )
    assert (
        _count(
            db,
            AuditEntry,
            AuditEntry.project_id == project.id,
            AuditEntry.action == AuditAction.APPROVAL_DECIDED,
        )
        == 0
    )
    assert (
        _count(
            db,
            Event,
            Event.project_id == project.id,
            Event.event_type == EventType.APPROVAL_DECIDED,
        )
        == 1
    )
    assert (
        _count(
            db,
            Event,
            Event.project_id == project.id,
            Event.event_type == EventType.GATE_STATUS_CHANGED,
        )
        >= 1
    )


@pytest.mark.parametrize(
    ("decision", "approval_status", "settled_gate_status"),
    [
        (ApprovalDecision.APPROVE, ApprovalStatus.APPROVED, GateStatus.PASSED),
        (ApprovalDecision.ACCEPT_RISK, ApprovalStatus.RISK_ACCEPTED, GateStatus.PASSED),
        (ApprovalDecision.REJECT, ApprovalStatus.REJECTED, GateStatus.FAILED),
        (ApprovalDecision.REQUEST_CHANGES, ApprovalStatus.CHANGES_REQUESTED, None),
        (ApprovalDecision.DEFER, ApprovalStatus.DEFERRED, None),
    ],
)
def test_gate_route_exposes_every_engineering_decision(
    db: Session,
    project: Project,
    reviewer_client: AuthedClient,
    decision: ApprovalDecision,
    approval_status: ApprovalStatus,
    settled_gate_status: GateStatus | None,
    current_gate_request: None,
) -> None:
    gate, approval = _gate_approval(db, project)

    response = reviewer_client.post(
        f"/api/approval-gates/{gate.id}/approvals/{approval.id}/decision",
        json={"decision": decision.value, "comment": "Reviewer decision rationale."},
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == approval_status.value
    assert response.json()["gate_id"] == str(gate.id)
    assert response.json()["gate_key"] == gate.gate_key.value
    assert response.json()["revision_number"] == approval.revision_number
    assert response.json()["release_candidate_id"] is None
    assert response.json()["decision"] == decision.value
    db.expire_all()
    decided = db.get(Approval, approval.id)
    decided_gate = db.get(ApprovalGate, gate.id)
    assert decided is not None
    assert decided.decision is decision
    assert decided.status is approval_status
    assert decided.decision_notes == "Reviewer decision rationale."
    assert decided_gate is not None
    if settled_gate_status is not None:
        assert decided_gate.status is settled_gate_status
        assert decided_gate.decided_revision_number == approval.revision_number
    else:
        assert decided_gate.status not in {GateStatus.PASSED, GateStatus.FAILED}
        assert decided_gate.decided_revision_number is None


def test_gate_route_requires_reviewer_authority(
    db: Session,
    project: Project,
    engineer_client: AuthedClient,
) -> None:
    gate, approval = _gate_approval(db, project)

    response = engineer_client.post(
        f"/api/approval-gates/{gate.id}/approvals/{approval.id}/decision",
        json={"decision": "approve", "comment": "Self approval attempted."},
    )

    assert response.status_code == 403
    db.expire_all()
    assert db.get(Approval, approval.id).status is ApprovalStatus.PENDING
    assert db.get(ApprovalGate, gate.id).status is GateStatus.AWAITING_DECISION


def test_gate_route_rejects_a_mismatched_gate_and_approval(
    db: Session,
    project: Project,
    reviewer_client: AuthedClient,
) -> None:
    gate, approval = _gate_approval(db, project)
    other_gate = next(
        row
        for row in gates.ensure_gates(db, project=project)
        if row.gate_key is ApprovalGateKey.DEVICES_IO_TAGS
    )
    db.commit()

    response = reviewer_client.post(
        f"/api/approval-gates/{other_gate.id}/approvals/{approval.id}/decision",
        json={"decision": "approve", "comment": "Wrong gate."},
    )

    assert response.status_code == 409
    assert "not bound" in response.text.lower()
    db.expire_all()
    assert db.get(Approval, approval.id).status is ApprovalStatus.PENDING
    assert db.get(ApprovalGate, gate.id).status is GateStatus.AWAITING_DECISION


def test_gate_route_enforces_required_comments_atomically(
    db: Session,
    project: Project,
    reviewer_client: AuthedClient,
    current_gate_request: None,
) -> None:
    gate, approval = _gate_approval(db, project)

    response = reviewer_client.post(
        f"/api/approval-gates/{gate.id}/approvals/{approval.id}/decision",
        json={"decision": "reject", "comment": ""},
    )

    assert response.status_code == 422
    assert "requires a comment" in response.text.lower()
    db.expire_all()
    assert db.get(Approval, approval.id).status is ApprovalStatus.PENDING
    assert db.get(ApprovalGate, gate.id).status is GateStatus.AWAITING_DECISION
    assert (
        _count(
            db,
            AuditEntry,
            AuditEntry.project_id == project.id,
            AuditEntry.action == AuditAction.GATE_DECIDED,
        )
        == 0
    )


def test_gate_decision_retry_conflicts_without_duplicate_audit_or_events(
    db: Session,
    project: Project,
    reviewer_client: AuthedClient,
    current_gate_request: None,
) -> None:
    gate, approval = _gate_approval(db, project)
    url = f"/api/approval-gates/{gate.id}/approvals/{approval.id}/decision"
    payload = {"decision": "approve", "comment": "Evidence checked."}

    first = reviewer_client.post(url, json=payload)
    assert first.status_code == 200, first.text
    audit_count = _count(
        db,
        AuditEntry,
        AuditEntry.project_id == project.id,
        AuditEntry.action == AuditAction.GATE_DECIDED,
    )
    event_count = _count(
        db,
        Event,
        Event.project_id == project.id,
        Event.event_type == EventType.APPROVAL_DECIDED,
    )

    retry = reviewer_client.post(url, json=payload)

    assert retry.status_code == 409
    assert "already approved" in retry.text.lower()
    assert (
        _count(
            db,
            AuditEntry,
            AuditEntry.project_id == project.id,
            AuditEntry.action == AuditAction.GATE_DECIDED,
        )
        == audit_count
    )
    assert (
        _count(
            db,
            Event,
            Event.project_id == project.id,
            Event.event_type == EventType.APPROVAL_DECIDED,
        )
        == event_count
    )


def test_gate_route_persists_request_invalidation_before_returning_conflict(
    db: Session,
    project: Project,
    reviewer_client: AuthedClient,
) -> None:
    """A stale request must not survive the 409 transaction rollback.

    The synthetic request is attached to a project with no complete profile, so
    the service's locked re-evaluation correctly invalidates it before trying
    to apply the reviewer decision.
    """
    gate, approval = _gate_approval(db, project)

    response = reviewer_client.post(
        f"/api/approval-gates/{gate.id}/approvals/{approval.id}/decision",
        json={"decision": "approve", "comment": "Opened from a stale review tab."},
    )

    assert response.status_code == 409
    assert "no longer current" in response.text.lower()
    db.expire_all()
    invalidated = db.get(Approval, approval.id)
    reevaluated_gate = db.get(ApprovalGate, gate.id)
    assert invalidated is not None
    assert invalidated.status is ApprovalStatus.CANCELLED
    assert "criteria no longer hold" in invalidated.decision_notes
    assert reevaluated_gate is not None
    assert reevaluated_gate.approval_id is None
    assert reevaluated_gate.status is GateStatus.NOT_READY

    pending = reviewer_client.get(
        f"/api/projects/{project.id}/approvals",
        params={"status": ApprovalStatus.PENDING.value},
    )
    assert pending.status_code == 200
    assert all(row["id"] != str(approval.id) for row in pending.json())
