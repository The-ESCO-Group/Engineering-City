"""Canonical requirement reads and signed reviewer decisions."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.enums import LogicOrigin, ObjectApprovalState, RequirementStatus
from app.models.profile import ProjectDecision
from app.models.project import Project
from app.models.requirements import AcceptanceCriterion, Requirement, RequirementSource
from tests.conftest import AuthedClient


def _requirement(db: Session, project: Project) -> Requirement:
    item = Requirement(
        project_id=project.id,
        reference="REQ-001",
        title="Conveyor stop behavior",
        statement="The conveyor shall stop within 3 seconds after a stop command.",
        source_text="Conveyor shall stop within 3 seconds.",
        status=RequirementStatus.PROPOSED,
        origin=LogicOrigin.AGENT_PROPOSED,
        confidence=0.98,
    )
    db.add(item)
    db.flush()
    item.sources.append(
        RequirementSource(
            project_id=project.id,
            quoted_text=item.source_text,
            location_label="Synthetic requirements.pdf / page 2",
            is_primary=True,
        )
    )
    item.acceptance_criteria.append(
        AcceptanceCriterion(
            project_id=project.id,
            reference="AC-001-1",
            statement="Measured stop time is no more than 3.0 seconds.",
            ordinal=1,
        )
    )
    db.commit()
    return item


def test_requirement_resource_includes_sources_and_acceptance_criteria(
    db: Session, project: Project, reviewer_client: AuthedClient
) -> None:
    requirement = _requirement(db, project)
    response = reviewer_client.get(f"/api/projects/{project.id}/requirements")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body[0]["id"] == str(requirement.id)
    assert body[0]["sources"][0]["location_label"].endswith("page 2")
    assert body[0]["acceptance_criteria"][0]["reference"] == "AC-001-1"


def test_named_reviewer_acceptance_protects_requirement_and_criteria(
    db: Session, project: Project, reviewer_client: AuthedClient
) -> None:
    requirement = _requirement(db, project)
    response = reviewer_client.post(
        f"/api/requirements/{requirement.id}/decision",
        json={
            "decision": "accept",
            "rationale": "Checked verbatim against the cited synthetic source.",
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["requirement"]["status"] == "accepted"
    db.expire_all()
    stored = db.get(Requirement, requirement.id)
    assert stored is not None
    assert stored.status is RequirementStatus.ACCEPTED
    assert stored.approval_state is ObjectApprovalState.APPROVED
    assert stored.approved_by_id == reviewer_client.user.id
    assert stored.acceptance_criteria[0].status is RequirementStatus.ACCEPTED
    decision = db.get(ProjectDecision, response.json()["decision_id"])
    assert decision is not None
    assert decision.decided_by_name == "Test Reviewer"


def test_engineer_cannot_sign_off_own_extracted_requirement(
    db: Session,
    project: Project,
    engineer_client: AuthedClient,
) -> None:
    requirement = _requirement(db, project)
    response = engineer_client.post(
        f"/api/requirements/{requirement.id}/decision",
        json={"decision": "accept", "rationale": "Self approval must be refused."},
    )
    assert response.status_code == 403
    db.expire_all()
    assert db.get(Requirement, requirement.id).status is RequirementStatus.PROPOSED


def test_requirement_decision_is_single_use(
    db: Session, project: Project, reviewer_client: AuthedClient
) -> None:
    requirement = _requirement(db, project)
    payload = {"decision": "reject", "rationale": "This sentence is background, not scope."}
    first = reviewer_client.post(f"/api/requirements/{requirement.id}/decision", json=payload)
    second = reviewer_client.post(f"/api/requirements/{requirement.id}/decision", json=payload)
    assert first.status_code == 200
    assert second.status_code == 409
