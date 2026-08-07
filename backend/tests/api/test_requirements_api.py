"""Canonical requirement reads and signed reviewer decisions."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.enums import LogicOrigin, ObjectApprovalState, RequirementStatus
from app.models.profile import ProjectDecision
from app.models.project import Project
from app.models.requirements import AcceptanceCriterion, Requirement, RequirementSource
from app.validation.cross_discipline import ApprovalIntegrityValidator
from tests.conftest import AuthedClient
from tests.validation import codes, run_check

#: The two verdicts the approval-integrity check can reach about an approved
#: object, named here because two tests below turn on which one it reaches.
MODIFIED = "cross.approved_object_modified"
UNRECORDED = "cross.approval_revision_unrecorded"


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


def test_accepting_a_requirement_does_not_invalidate_its_own_approval(
    db: Session, project: Project, reviewer_client: AuthedClient
) -> None:
    """Signing a requirement must not read as tampering with it.

    The decision writes the approval and bumps the revision in one flush, and
    ``updated_at`` is a second, independent read of the clock inside that flush.
    While staleness was inferred from those two timestamps, whether they landed in
    the same tick decided whether every accepted requirement became a
    release-blocking ``cross.approved_object_modified`` milliseconds after a
    reviewer signed it — so this asserts against the real endpoint rather than a
    constructed row, because the race lived in the endpoint's own flush.
    """
    requirement = _requirement(db, project)
    response = reviewer_client.post(
        f"/api/requirements/{requirement.id}/decision",
        json={"decision": "accept", "rationale": "Checked against the cited synthetic source."},
    )
    assert response.status_code == 200, response.text
    db.expire_all()

    stored = db.get(Requirement, requirement.id)
    assert stored is not None
    assert stored.approved_object_revision == stored.object_revision, (
        "the approval did not record the revision it covers"
    )
    found = codes(run_check(ApprovalIntegrityValidator(), db, project))
    assert MODIFIED not in found, "accepting a requirement reported the reviewer who accepted it"
    assert UNRECORDED not in found, "the approval left nothing to check itself against"


def test_editing_an_accepted_requirement_is_still_reported(
    db: Session, project: Project, reviewer_client: AuthedClient
) -> None:
    """The teeth. Removing the race must not remove the check.

    An approved requirement edited afterwards carries a named engineer's
    signature against a sentence that person never read, which is the failure the
    whole approval mechanism exists to prevent. The edit here is the shape a
    writer that bypassed the merge engine leaves — the merge engine itself refuses
    to touch a protected object — so this is exactly the case the validator is a
    backstop for.
    """
    requirement = _requirement(db, project)
    accepted = reviewer_client.post(
        f"/api/requirements/{requirement.id}/decision",
        json={"decision": "accept", "rationale": "Checked against the cited synthetic source."},
    )
    assert accepted.status_code == 200, accepted.text
    db.expire_all()

    stored = db.get(Requirement, requirement.id)
    assert stored is not None
    signed_revision = stored.object_revision
    stored.statement = "The conveyor shall stop within 8 seconds after a stop command."
    stored.object_revision += 1
    db.flush()

    issues = [
        issue
        for issue in run_check(ApprovalIntegrityValidator(), db, project)
        if issue.code == MODIFIED
    ]

    assert len(issues) == 1, codes(run_check(ApprovalIntegrityValidator(), db, project))
    assert issues[0].detail["approved_object_revision"] == signed_revision
    assert issues[0].detail["object_revision"] == signed_revision + 1
    assert "REQ-001" in issues[0].object_label


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
