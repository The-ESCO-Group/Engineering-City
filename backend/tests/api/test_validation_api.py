"""Authenticated, immutable validation-result waiver decisions."""

from __future__ import annotations

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

import app.models.release as release_models
from app.core.config import Settings
from app.core.enums import (
    AuditAction,
    EventType,
    ProjectRole,
    TaskKind,
    TaskStatus,
    ValidationDiscipline,
    ValidationRunStatus,
    ValidationSeverity,
)
from app.core.errors import ConflictError, PermissionDeniedError
from app.core.services import validation_review
from app.database.session import get_session_factory
from app.models.audit import AuditEntry
from app.models.event import Event
from app.models.project import Project, ProjectMember
from app.models.release import ValidationResult, ValidationRun
from app.models.task import Task
from app.models.user import User


def _blocked_result(db: Session, project: Project) -> tuple[ValidationResult, Task]:
    task = Task(
        project_id=project.id,
        kind=TaskKind.VALIDATE_CROSS_DISCIPLINE,
        title="Validate current revision",
        status=TaskStatus.BLOCKED,
        blocked_reason="One validation error remains.",
        idempotency_key="test:validation-waiver",
    )
    db.add(task)
    db.flush()
    run = ValidationRun(
        project_id=project.id,
        revision_number=project.current_revision_number,
        status=ValidationRunStatus.COMPLETED,
        disciplines=[ValidationDiscipline.IO.value],
        triggered_by_task_id=task.id,
        checks_run=1,
        error_count=1,
    )
    db.add(run)
    db.flush()
    result = ValidationResult(
        project_id=project.id,
        validation_run_id=run.id,
        code="io.duplicate_point_assignment",
        discipline=ValidationDiscipline.IO,
        severity=ValidationSeverity.ERROR,
        message="Two signals claim one terminal.",
        object_label="Local:1:I.Data.10",
        suggested_action="Move one signal to a free channel.",
    )
    db.add(result)
    db.commit()
    db.refresh(result)
    return result, task


def test_reviewer_waiver_is_visible_audited_immutable_and_convergent(
    reviewer_client: object,
    db: Session,
    project: Project,
) -> None:
    result, task = _blocked_result(db, project)
    payload = {
        "confirm": True,
        "reason": "Accepted for this synthetic offline release after checking the wiring schedule.",
        "expected_updated_at": result.updated_at.isoformat(),
    }

    response = reviewer_client.post(  # type: ignore[attr-defined]
        f"/api/validation-results/{result.id}/waiver", json=payload
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["result"]["is_waived"] is True
    assert body["result"]["waiver_reason"] == payload["reason"]
    assert body["decision_replayed"] is False
    assert body["unblocked_task_count"] >= 1
    db.expire_all()
    assert db.get(Task, task.id).status is TaskStatus.COMPLETED  # type: ignore[union-attr]
    audit = db.execute(
        select(AuditEntry).where(
            AuditEntry.action == AuditAction.VALIDATION_RUN,
            AuditEntry.target_id == result.id,
        )
    ).scalar_one()
    assert audit.actor_user_id is not None
    assert audit.details["operation"] == "validation_result_waived"
    assert audit.details["reason"] == payload["reason"]
    completion = db.execute(
        select(Event).where(
            Event.project_id == project.id,
            Event.task_id == task.id,
            Event.event_type == EventType.TASK_COMPLETED,
        )
    ).scalar_one()
    assert completion.payload["status"] == TaskStatus.COMPLETED.value
    assert "named reviewer waivers" in completion.payload["detail"]

    retry = reviewer_client.post(  # type: ignore[attr-defined]
        f"/api/validation-results/{result.id}/waiver", json=payload
    )
    assert retry.status_code == 200, retry.text
    assert retry.json()["decision_replayed"] is True

    replacement = reviewer_client.post(  # type: ignore[attr-defined]
        f"/api/validation-results/{result.id}/waiver",
        json={**payload, "reason": "A different decision must not replace the first waiver."},
    )
    assert replacement.status_code == 409
    db.expire_all()
    assert (
        db.scalar(
            select(func.count())
            .select_from(Event)
            .where(
                Event.project_id == project.id,
                Event.task_id == task.id,
                Event.event_type == EventType.TASK_COMPLETED,
            )
        )
        == 1
    )


def test_a_preloaded_snapshot_cannot_overwrite_a_concurrent_waiver(
    db: Session,
    project: Project,
    seeded: dict[str, User],
    settings: Settings,
) -> None:
    """The locking read must refresh the identity-map row before immutability checks."""
    result, _ = _blocked_result(db, project)
    factory = get_session_factory()
    stale = factory()
    winner = factory()
    try:
        stale_project = stale.get(Project, project.id)
        stale_result = stale.get(ValidationResult, result.id)
        stale_actor = stale.get(User, seeded["reviewer"].id)
        winner_project = winner.get(Project, project.id)
        winner_result = winner.get(ValidationResult, result.id)
        winner_actor = winner.get(User, seeded["reviewer"].id)
        assert stale_project is not None and stale_result is not None and stale_actor is not None
        assert winner_project is not None and winner_result is not None and winner_actor is not None
        opened_at = stale_result.updated_at

        validation_review.waive(
            winner,
            project=winner_project,
            result=winner_result,
            expected_updated_at=opened_at,
            reason="The first reviewer decision is the immutable record.",
            actor=winner_actor,
            settings=settings,
        )
        winner.commit()

        with pytest.raises(ConflictError, match="already waived"):
            validation_review.waive(
                stale,
                project=stale_project,
                result=stale_result,
                expected_updated_at=opened_at,
                reason="A stale session must not replace the committed reason.",
                actor=stale_actor,
                settings=settings,
            )
        stale.rollback()
    finally:
        stale.close()
        winner.close()

    db.expire_all()
    stored = db.get(ValidationResult, result.id)
    assert stored is not None
    assert stored.waiver_reason == "The first reviewer decision is the immutable record."
    assert stored.waived_by_id == seeded["reviewer"].id


def test_project_lock_refresh_rejects_a_revision_that_advanced_after_authorisation(
    db: Session,
    project: Project,
    seeded: dict[str, User],
    settings: Settings,
) -> None:
    """A pre-authorisation Project instance cannot pin the waiver to a stale head."""
    result, _ = _blocked_result(db, project)
    factory = get_session_factory()
    stale = factory()
    revision_writer = factory()
    try:
        stale_project = stale.get(Project, project.id)
        stale_result = stale.get(ValidationResult, result.id)
        stale_actor = stale.get(User, seeded["reviewer"].id)
        assert stale_project is not None and stale_result is not None and stale_actor is not None
        opened_at = stale_result.updated_at

        revision_writer.execute(
            update(Project)
            .where(Project.id == project.id)
            .values(current_revision_number=Project.current_revision_number + 1)
        )
        revision_writer.commit()

        with pytest.raises(ConflictError, match="newest completed validation"):
            validation_review.waive(
                stale,
                project=stale_project,
                result=stale_result,
                expected_updated_at=opened_at,
                reason="The decision must be refused after the project head advances.",
                actor=stale_actor,
                settings=settings,
            )
        stale.rollback()
    finally:
        stale.close()
        revision_writer.close()

    db.expire_all()
    stored = db.get(ValidationResult, result.id)
    assert stored is not None
    assert stored.is_waived is False


def test_direct_waiver_service_refuses_an_engineer_and_an_inactive_reviewer(
    db: Session,
    project: Project,
    seeded: dict[str, User],
    settings: Settings,
) -> None:
    result, _ = _blocked_result(db, project)
    opened_at = result.updated_at

    with pytest.raises(PermissionDeniedError, match="reviewer role"):
        validation_review.waive(
            db,
            project=project,
            result=result,
            expected_updated_at=opened_at,
            reason="An engineer cannot own this waiver.",
            actor=seeded["engineer"],
            settings=settings,
        )
    db.rollback()

    seeded["reviewer"].is_active = False
    db.commit()
    with pytest.raises(PermissionDeniedError, match="deactivated"):
        validation_review.waive(
            db,
            project=project,
            result=result,
            expected_updated_at=opened_at,
            reason="An inactive reviewer cannot own this waiver.",
            actor=seeded["reviewer"],
            settings=settings,
        )
    db.rollback()

    db.expire_all()
    assert db.get(ValidationResult, result.id).is_waived is False  # type: ignore[union-attr]


def test_stale_reviewer_identity_cannot_bypass_a_membership_downgrade(
    db: Session,
    project: Project,
    seeded: dict[str, User],
    settings: Settings,
) -> None:
    result, _ = _blocked_result(db, project)
    factory = get_session_factory()
    stale = factory()
    authority_writer = factory()
    try:
        stale_project = stale.get(Project, project.id)
        stale_result = stale.get(ValidationResult, result.id)
        stale_actor = stale.get(User, seeded["reviewer"].id)
        assert stale_project is not None and stale_result is not None and stale_actor is not None
        opened_at = stale_result.updated_at

        authority_writer.execute(
            update(ProjectMember)
            .where(
                ProjectMember.project_id == project.id,
                ProjectMember.user_id == seeded["reviewer"].id,
            )
            .values(role=ProjectRole.ENGINEER)
        )
        authority_writer.commit()

        with pytest.raises(PermissionDeniedError, match="reviewer role"):
            validation_review.waive(
                stale,
                project=stale_project,
                result=stale_result,
                expected_updated_at=opened_at,
                reason="A stale review tab cannot retain removed authority.",
                actor=stale_actor,
                settings=settings,
            )
        stale.rollback()
    finally:
        stale.close()
        authority_writer.close()

    db.expire_all()
    assert db.get(ValidationResult, result.id).is_waived is False  # type: ignore[union-attr]


def test_waiver_never_completes_a_cross_project_or_wrong_kind_trigger_task(
    db: Session,
    project: Project,
    seeded: dict[str, User],
    settings: Settings,
) -> None:
    result, task = _blocked_result(db, project)
    foreign = Project(
        name="Unrelated project",
        description="A malformed trigger must not cross this boundary.",
        client_reference="OTHER",
        created_by_id=seeded["engineer"].id,
    )
    db.add(foreign)
    db.flush()
    task.project_id = foreign.id
    db.commit()
    db.refresh(result)

    outcome = validation_review.waive(
        db,
        project=project,
        result=result,
        expected_updated_at=result.updated_at,
        reason="The result may be waived, but its malformed trigger must remain untouched.",
        actor=seeded["reviewer"],
        settings=settings,
    )
    db.commit()

    assert outcome.unblocked_tasks == 0
    db.expire_all()
    stored_task = db.get(Task, task.id)
    assert stored_task is not None and stored_task.status is TaskStatus.BLOCKED
    assert (
        db.scalar(
            select(func.count(Event.id)).where(
                Event.task_id == task.id,
                Event.event_type == EventType.TASK_COMPLETED,
            )
        )
        == 0
    )


def test_engineer_cannot_waive_and_reason_must_be_nonblank(
    engineer_client: object,
    reviewer_client: object,
    db: Session,
    project: Project,
) -> None:
    result, _ = _blocked_result(db, project)
    payload = {
        "confirm": True,
        "reason": "A qualified reviewer must own this decision.",
        "expected_updated_at": result.updated_at.isoformat(),
    }
    forbidden = engineer_client.post(  # type: ignore[attr-defined]
        f"/api/validation-results/{result.id}/waiver", json=payload
    )
    assert forbidden.status_code == 403
    blank = reviewer_client.post(  # type: ignore[attr-defined]
        f"/api/validation-results/{result.id}/waiver", json={**payload, "reason": "   "}
    )
    assert blank.status_code == 422


def test_latest_validation_results_expose_the_concurrency_token(
    reviewer_client: object,
    db: Session,
    project: Project,
) -> None:
    result, _ = _blocked_result(db, project)
    response = reviewer_client.get(  # type: ignore[attr-defined]
        f"/api/projects/{project.id}/validation-results"
    )
    assert response.status_code == 200, response.text
    assert response.json()[0]["id"] == str(result.id)
    assert response.json()[0]["updated_at"]


def test_equal_time_current_runs_expose_no_arbitrarily_selected_results(
    reviewer_client: object,
    db: Session,
    project: Project,
) -> None:
    first, _ = _blocked_result(db, project)
    first_run = db.get(ValidationRun, first.validation_run_id)
    assert first_run is not None
    second_run = ValidationRun(
        project_id=project.id,
        revision_number=project.current_revision_number,
        status=ValidationRunStatus.COMPLETED,
        disciplines=[ValidationDiscipline.IO.value],
        checks_run=1,
    )
    db.add(second_run)
    db.flush()
    db.add(
        ValidationResult(
            project_id=project.id,
            validation_run_id=second_run.id,
            code="io.second_snapshot",
            discipline=ValidationDiscipline.IO,
            severity=ValidationSeverity.ERROR,
            message="This result must not win merely because its run UUID sorts later.",
        )
    )
    second_run.created_at = first_run.created_at
    db.commit()

    response = reviewer_client.get(  # type: ignore[attr-defined]
        f"/api/projects/{project.id}/validation-results"
    )

    assert response.status_code == 200, response.text
    assert response.json() == []

    db.refresh(first)
    refusal = reviewer_client.post(  # type: ignore[attr-defined]
        f"/api/validation-results/{first.id}/waiver",
        json={
            "confirm": True,
            "reason": "An equal-time snapshot has no authority to receive this decision.",
            "expected_updated_at": first.updated_at.isoformat(),
        },
    )
    assert refusal.status_code == 409, refusal.text


def test_waiver_rejects_a_stale_result_snapshot(
    reviewer_client: object,
    db: Session,
    project: Project,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, _ = _blocked_result(db, project)
    opened_at = result.updated_at

    # Force the exact clock collision that previously made this test flaky on
    # fast Windows runs. SQLAlchemy wraps zero-argument column defaults as a
    # context callable, while the model hook reads the module-level clock.
    updated_at_column = ValidationResult.__table__.c.updated_at
    assert updated_at_column.onupdate is not None
    monkeypatch.setattr(updated_at_column.onupdate, "arg", lambda _context: opened_at)
    monkeypatch.setattr(release_models, "utcnow", lambda: opened_at)

    result.message = "The validation evidence changed after the reviewer opened it."
    db.commit()
    db.refresh(result)
    assert result.updated_at > opened_at

    response = reviewer_client.post(  # type: ignore[attr-defined]
        f"/api/validation-results/{result.id}/waiver",
        json={
            "confirm": True,
            "reason": "This decision was based on the earlier evidence and must be refused.",
            "expected_updated_at": opened_at.isoformat(),
        },
    )

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "stale_validation_result"
    db.expire_all()
    assert db.get(ValidationResult, result.id).is_waived is False  # type: ignore[union-attr]


def test_waiver_rejects_a_superseded_run_and_a_nonblocking_result(
    reviewer_client: object,
    db: Session,
    project: Project,
) -> None:
    stale, _ = _blocked_result(db, project)
    newer = ValidationRun(
        project_id=project.id,
        revision_number=project.current_revision_number,
        status=ValidationRunStatus.COMPLETED,
        disciplines=[ValidationDiscipline.IO.value],
        checks_run=1,
    )
    db.add(newer)
    db.flush()
    warning = ValidationResult(
        project_id=project.id,
        validation_run_id=newer.id,
        code="io.description_missing",
        discipline=ValidationDiscipline.IO,
        severity=ValidationSeverity.WARNING,
        message="A description would improve the package but does not make it wrong.",
    )
    db.add(warning)
    db.commit()
    db.refresh(warning)

    stale_response = reviewer_client.post(  # type: ignore[attr-defined]
        f"/api/validation-results/{stale.id}/waiver",
        json={
            "confirm": True,
            "reason": "A superseded validation snapshot cannot receive a release decision.",
            "expected_updated_at": stale.updated_at.isoformat(),
        },
    )
    assert stale_response.status_code == 409, stale_response.text

    warning_response = reviewer_client.post(  # type: ignore[attr-defined]
        f"/api/validation-results/{warning.id}/waiver",
        json={
            "confirm": True,
            "reason": "Warnings are advisory and must not be converted into risk waivers.",
            "expected_updated_at": warning.updated_at.isoformat(),
        },
    )
    assert warning_response.status_code == 409, warning_response.text


def test_validation_task_stays_blocked_until_every_error_is_waived(
    reviewer_client: object,
    db: Session,
    project: Project,
) -> None:
    first, task = _blocked_result(db, project)
    second = ValidationResult(
        project_id=project.id,
        validation_run_id=first.validation_run_id,
        code="logic.unresolved_operand",
        discipline=ValidationDiscipline.PLC_LOGIC,
        severity=ValidationSeverity.ERROR,
        message="A second independent error still blocks release.",
    )
    db.add(second)
    db.commit()
    db.refresh(second)

    first_response = reviewer_client.post(  # type: ignore[attr-defined]
        f"/api/validation-results/{first.id}/waiver",
        json={
            "confirm": True,
            "reason": "The duplicate assignment is knowingly accepted for this test revision.",
            "expected_updated_at": first.updated_at.isoformat(),
        },
    )
    assert first_response.status_code == 200, first_response.text
    db.expire_all()
    assert db.get(Task, task.id).status is TaskStatus.BLOCKED  # type: ignore[union-attr]

    second_response = reviewer_client.post(  # type: ignore[attr-defined]
        f"/api/validation-results/{second.id}/waiver",
        json={
            "confirm": True,
            "reason": "The unresolved reference is also knowingly accepted for this test.",
            "expected_updated_at": second.updated_at.isoformat(),
        },
    )
    assert second_response.status_code == 200, second_response.text
    db.expire_all()
    assert db.get(Task, task.id).status is TaskStatus.COMPLETED  # type: ignore[union-attr]


def test_waiver_requires_explicit_confirmation(
    reviewer_client: object,
    db: Session,
    project: Project,
) -> None:
    result, _ = _blocked_result(db, project)
    response = reviewer_client.post(  # type: ignore[attr-defined]
        f"/api/validation-results/{result.id}/waiver",
        json={
            "confirm": False,
            "reason": "This must never be treated as an affirmative engineering decision.",
            "expected_updated_at": result.updated_at.isoformat(),
        },
    )
    assert response.status_code == 422, response.text
