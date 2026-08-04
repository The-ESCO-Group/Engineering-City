"""Approval decisions are unavailable until their finding was independently reviewed."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.clock import utcnow
from app.core.enums import (
    ApprovalKind,
    ApprovalStatus,
    FindingSeverity,
    FindingStatus,
)
from app.models.approval import Approval
from app.models.finding import Finding
from app.models.project import Project
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
    assert response.json()["decision_allowed"] is False
    db.expire_all()
    assert db.get(Finding, finding.id).status is FindingStatus.ACCEPTED
