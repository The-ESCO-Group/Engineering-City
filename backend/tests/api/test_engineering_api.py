"""Explicit engineering intake and clarification HTTP behavior."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.enums import TaskKind, TaskStatus
from app.models.profile import (
    EngineeringStandardSet,
    ProjectDecision,
    ProjectProfile,
    ProjectQuestion,
    TargetPlatform,
    TemplateSet,
)
from app.models.project import Project
from app.models.release import ExportProfile
from app.models.task import Task


def _profile(*, restricted_domains: list[str] | None = None) -> dict[str, object]:
    return {
        "project_name": "Line 4 Packaging Conveyor Upgrade",
        "customer_label": "Synthetic internal demo",
        "plant_or_site": "Demo Plant",
        "project_description": (
            "Upgrade the synthetic Line 4 controls and produce an import package."
        ),
        "project_type": "upgrade",
        "units": "metric",
        "locale": "en-US",
        "release_mode": "import_package_only",
        "restricted_domains": restricted_domains or [],
        "target": {
            "controller_family": "controllogix",
            "controller_catalog_number": "1756-L83E",
            "firmware_revision": "35.011",
            "studio_5000_version": "35.00",
            "hmi_product": "none",
        },
        "standards": {
            "naming_standard_version": "ESCO naming 3.2",
            "alarm_philosophy_version": "ESCO alarms 1.1",
            "controller_template": "esco.motor.standard@1.0.0",
            "aoi_udt_library": "esco.logix.library@2.0.0",
            "hmi_library": "",
            "test_template_set": "esco.fat.standard@1.0.0",
        },
    }


def test_project_creation_atomically_records_a_production_profile(
    engineer_client: object, db: Session
) -> None:
    response = engineer_client.post(  # type: ignore[attr-defined]
        "/api/projects",
        json={
            "name": "Line 4 Packaging Conveyor Upgrade",
            "description": "Synthetic acceptance project.",
            "client_reference": "SYN-L4",
            "engineering_profile": _profile(),
        },
    )
    assert response.status_code == 201, response.text
    project_id = response.json()["id"]

    profile_response = engineer_client.get(  # type: ignore[attr-defined]
        f"/api/projects/{project_id}/engineering-profile"
    )
    assert profile_response.status_code == 200, profile_response.text
    body = profile_response.json()
    assert body["is_complete_for_generation"] is True
    assert body["blocks_generation"] is False
    assert body["target"]["controller_family"] == "controllogix"
    assert body["target"]["studio_5000_version"] == "35.00"
    assert set(body["completeness"].values()) == {"present"}

    assert (
        db.execute(
            select(func.count(ProjectProfile.id)).where(ProjectProfile.project_id == project_id)
        ).scalar_one()
        == 1
    )
    assert (
        db.execute(
            select(func.count(TargetPlatform.id)).where(TargetPlatform.project_id == project_id)
        ).scalar_one()
        == 1
    )
    assert (
        db.execute(
            select(func.count(TemplateSet.id)).where(TemplateSet.project_id == project_id)
        ).scalar_one()
        == 1
    )
    assert (
        db.execute(
            select(func.count(EngineeringStandardSet.id)).where(
                EngineeringStandardSet.project_id == project_id
            )
        ).scalar_one()
        == 1
    )
    assert (
        db.execute(
            select(func.count(ExportProfile.id)).where(ExportProfile.project_id == project_id)
        ).scalar_one()
        == 1
    )


def test_restricted_intake_is_complete_but_does_not_claim_generation_authority(
    engineer_client: object,
) -> None:
    response = engineer_client.post(  # type: ignore[attr-defined]
        "/api/projects",
        json={
            "name": "Restricted synthetic project",
            "engineering_profile": _profile(restricted_domains=["safety_logic"]),
        },
    )
    assert response.status_code == 201, response.text
    project_id = response.json()["id"]
    profile = engineer_client.get(  # type: ignore[attr-defined]
        f"/api/projects/{project_id}/engineering-profile"
    ).json()
    assert profile["is_complete_for_generation"] is True
    assert profile["blocks_generation"] is True
    assert profile["restricted_domain_mode"] == "manual_review_only"


def test_invalid_target_rolls_back_the_project_and_storage(
    engineer_client: object, db: Session, settings: Settings
) -> None:
    payload = _profile()
    target = dict(payload["target"])  # type: ignore[arg-type]
    target.update(
        {
            "hmi_product": "factorytalk_view_me",
            "hmi_version": "14",
            "hmi_resolution": "wide",
        }
    )
    payload["target"] = target

    before = db.execute(select(func.count(Project.id))).scalar_one()
    response = engineer_client.post(  # type: ignore[attr-defined]
        "/api/projects",
        json={"name": "Invalid target", "engineering_profile": payload},
    )
    assert response.status_code == 422, response.text
    db.expire_all()
    assert db.execute(select(func.count(Project.id))).scalar_one() == before
    project_dirs = settings.projects_dir
    if project_dirs.exists():
        assert not any(Path(project_dirs).iterdir())


def test_named_answer_persists_a_citable_decision(
    engineer_client: object,
    db: Session,
) -> None:
    created = engineer_client.post(  # type: ignore[attr-defined]
        "/api/projects",
        json={"name": "Clarification project", "engineering_profile": _profile()},
    )
    project_id = created.json()["id"]
    blocked_task = Task(
        project_id=project_id,
        kind=TaskKind.GENERATE_LOGIC,
        title="Generate logic after clarification",
        status=TaskStatus.BLOCKED,
        blocked_reason="Waiting for Q-001.",
        idempotency_key="test:clarification:generate-logic",
    )
    db.add(blocked_task)
    db.flush()
    question = ProjectQuestion(
        project_id=project_id,
        reference="Q-001",
        question="Which controlled stop time applies?",
        why_it_matters="The stop sequence cannot be generated without the accepted time.",
        blocks_task_kinds=["generate_logic"],
        raised_in_task_id=blocked_task.id,
    )
    db.add(question)
    db.commit()

    response = engineer_client.post(  # type: ignore[attr-defined]
        f"/api/project-questions/{question.id}/resolve",
        json={"answer": "Use a 3.0 second controlled stop.", "resolution": "answered"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "answered"
    assert response.json()["unblocked_task_count"] == 1

    db.expire_all()
    stored = db.get(ProjectQuestion, question.id)
    assert stored is not None
    assert stored.answer == "Use a 3.0 second controlled stop."
    assert stored.answered_by_id is not None
    decision = db.get(ProjectDecision, response.json()["decision_id"])
    assert decision is not None
    assert decision.statement == stored.answer
    assert decision.decided_by_name == "Test Engineer"
    db.refresh(blocked_task)
    assert blocked_task.status is TaskStatus.QUEUED
    assert blocked_task.blocked_reason == ""


def test_answer_requeues_stale_validation_and_gate_evaluations(
    engineer_client: object,
    db: Session,
) -> None:
    """An answered design question must not leave its old validation parked."""
    created = engineer_client.post(  # type: ignore[attr-defined]
        "/api/projects",
        json={"name": "Validation convergence", "engineering_profile": _profile()},
    )
    project_id = created.json()["id"]
    design = Task(
        project_id=project_id,
        kind=TaskKind.DESIGN_TAGS,
        title="Partial tag design",
        status=TaskStatus.COMPLETED,
        idempotency_key="test:clarification:completed-design",
    )
    validation = Task(
        project_id=project_id,
        kind=TaskKind.VALIDATE_CROSS_DISCIPLINE,
        title="Revalidate answered questions",
        status=TaskStatus.BLOCKED,
        blocked_reason="requirements.unanswered_question",
        idempotency_key="test:clarification:validation",
    )
    gate = Task(
        project_id=project_id,
        kind=TaskKind.EVALUATE_APPROVAL_GATE,
        title="Re-evaluate gate readiness",
        status=TaskStatus.BLOCKED,
        blocked_reason="A blocking question is unanswered.",
        idempotency_key="test:clarification:gate",
    )
    db.add_all([design, validation, gate])
    db.flush()
    question = ProjectQuestion(
        project_id=project_id,
        reference="Q-DESIGN",
        question="Which accepted requirement is this tag based on?",
        why_it_matters="Validation cannot establish traceability without the answer.",
        raised_in_task_id=design.id,
    )
    db.add(question)
    db.commit()

    response = engineer_client.post(  # type: ignore[attr-defined]
        f"/api/project-questions/{question.id}/resolve",
        json={"answer": "Use accepted requirement REQ-001.", "resolution": "answered"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["unblocked_task_count"] == 2
    db.expire_all()
    assert db.get(Task, design.id).status is TaskStatus.COMPLETED  # type: ignore[union-attr]
    assert db.get(Task, validation.id).status is TaskStatus.QUEUED  # type: ignore[union-attr]
    assert db.get(Task, gate.id).status is TaskStatus.QUEUED  # type: ignore[union-attr]
