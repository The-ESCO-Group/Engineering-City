"""Reviewer-owned validation waivers and workflow convergence."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core.clock import ensure_utc, utcnow
from app.core.config import Settings
from app.core.enums import (
    AuditAction,
    EventType,
    TaskKind,
    TaskStatus,
    ValidationRunStatus,
    ValidationSeverity,
)
from app.core.errors import ConflictError, ValidationError
from app.database.locking import lock_project_root
from app.models.project import Project
from app.models.release import ValidationResult, ValidationRun
from app.models.task import Task
from app.models.user import User
from app.orchestration import engine
from app.orchestration.events import record_event
from app.security import audit, permissions


@dataclass(frozen=True, slots=True)
class WaiverOutcome:
    replayed: bool
    unblocked_tasks: int


def latest_results(db: Session, project: Project) -> list[ValidationResult]:
    current_revision = db.execute(
        select(Project.current_revision_number).where(Project.id == project.id)
    ).scalar_one()
    recent_runs = db.execute(
        select(ValidationRun.id, ValidationRun.created_at)
        .where(
            ValidationRun.project_id == project.id,
            ValidationRun.revision_number == current_revision,
            ValidationRun.status == ValidationRunStatus.COMPLETED,
            ValidationRun.deleted_at.is_(None),
        )
        .order_by(ValidationRun.created_at.desc(), ValidationRun.id.desc())
        .limit(2)
    ).all()
    if not recent_runs:
        return []
    if len(recent_runs) > 1 and ensure_utc(recent_runs[0].created_at) == ensure_utc(
        recent_runs[1].created_at
    ):
        return []
    run_id = recent_runs[0].id
    return list(
        db.execute(
            select(ValidationResult)
            .where(ValidationResult.validation_run_id == run_id)
            .order_by(
                ValidationResult.severity,
                ValidationResult.discipline,
                ValidationResult.code,
                ValidationResult.created_at,
            )
        ).scalars()
    )


def waive(
    db: Session,
    *,
    project: Project,
    result: ValidationResult,
    expected_updated_at: object,
    reason: str,
    actor: User,
    settings: Settings,
    source_ip: str = "",
    user_agent: str = "",
) -> WaiverOutcome:
    """Waive one current result without erasing it or silently replacing a waiver."""
    from datetime import datetime

    # Serialise validation, revision changes and waiver decisions through the
    # project row, then lock the exact result.  Without this order a new
    # validation run could become current between the latest-run check and this
    # transaction's commit, leaving a waiver recorded against a superseded
    # snapshot.
    locked_project = lock_project_root(db, project.id)
    if locked_project is None:  # pragma: no cover - validation results cannot outlive their project
        raise ConflictError("The project attached to this validation result no longer exists.")
    project = locked_project
    actor = permissions.require_current_reviewer(
        db,
        user_id=actor.id,
        project_id=project.id,
        operation="record a validation waiver",
    )
    result = db.execute(
        select(ValidationResult)
        .where(ValidationResult.id == result.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one()

    clean_reason = reason.strip()
    if not clean_reason:
        raise ValidationError("A validation waiver requires a nonblank engineering reason.")
    if len(clean_reason) > 2_000:
        raise ValidationError("A validation waiver reason may not exceed 2,000 characters.")
    if not isinstance(expected_updated_at, datetime):
        raise ValidationError("expected_updated_at must be a timestamp returned by the API.")

    run = db.get(ValidationRun, result.validation_run_id)
    if run is None or run.project_id != project.id or result.project_id != project.id:
        raise ConflictError("The validation result no longer belongs to this project.")
    recent_runs = db.execute(
        select(ValidationRun.id, ValidationRun.created_at)
        .where(
            ValidationRun.project_id == project.id,
            ValidationRun.revision_number == project.current_revision_number,
            ValidationRun.status == ValidationRunStatus.COMPLETED,
            ValidationRun.deleted_at.is_(None),
        )
        .order_by(
            ValidationRun.revision_number.desc(),
            ValidationRun.created_at.desc(),
            ValidationRun.id.desc(),
        )
        .limit(2)
    ).all()
    newest = recent_runs[0] if recent_runs else None
    ambiguous_head = len(recent_runs) > 1 and ensure_utc(recent_runs[0].created_at) == ensure_utc(
        recent_runs[1].created_at
    )
    if (
        newest is None
        or ambiguous_head
        or run.id != newest.id
        or run.revision_number != project.current_revision_number
    ):
        raise ConflictError(
            "Only a result from the newest completed validation of the current revision "
            "can be waived. If two runs have the same recorded creation time, neither is "
            "treated as authoritative. Run validation again before deciding it."
        )
    if not (
        result.severity in {ValidationSeverity.ERROR, ValidationSeverity.HUMAN_REVIEW_REQUIRED}
        or result.requires_human_decision
    ):
        raise ConflictError(
            "This result does not block a gate or require a human decision, so it cannot be waived."
        )

    # A transport retry of the exact decision converges even though its old
    # timestamp is now stale. A different reason is a new decision and may not
    # overwrite the immutable first one; there is deliberately no unwaive API.
    if result.is_waived:
        if result.waived_by_id == actor.id and result.waiver_reason == clean_reason:
            return WaiverOutcome(replayed=True, unblocked_tasks=0)
        raise ConflictError(
            "This validation result is already waived. Existing waivers are immutable; "
            "run validation again if the engineering decision has changed."
        )
    if ensure_utc(result.updated_at) != ensure_utc(expected_updated_at):
        raise ConflictError(
            "The validation result changed after it was opened. Refresh it before waiving.",
            code="stale_validation_result",
            details={"current_updated_at": ensure_utc(result.updated_at).isoformat()},
        )

    result.is_waived = True
    result.waived_by_id = actor.id
    result.waiver_reason = clean_reason
    db.flush()

    audit.record(
        db,
        AuditAction.VALIDATION_RUN,
        actor_user_id=actor.id,
        actor_label=actor.display_name,
        project_id=project.id,
        target_type="validation_result",
        target_id=result.id,
        summary=f"{actor.display_name} waived validation result {result.code}.",
        details={
            "operation": "validation_result_waived",
            "validation_run_id": str(run.id),
            "revision_number": run.revision_number,
            "code": result.code,
            "severity": result.severity.value,
            "reason": clean_reason,
        },
        source_ip=source_ip,
        user_agent=user_agent,
    )

    moved = _settle_validation_task_if_clear(db, run)
    moved += engine.reconcile_gate_tasks(db, project)
    engine.refresh_project_status(db, project)
    db.flush()
    return WaiverOutcome(replayed=False, unblocked_tasks=moved)


def _settle_validation_task_if_clear(db: Session, run: ValidationRun) -> int:
    if run.triggered_by_task_id is None:
        return 0
    still_blocking = db.execute(
        select(ValidationResult.id).where(
            ValidationResult.validation_run_id == run.id,
            ValidationResult.severity == ValidationSeverity.ERROR,
            ValidationResult.is_waived.is_(False),
        )
    ).first()
    if still_blocking is not None:
        return 0
    task = db.execute(
        select(Task)
        .where(Task.id == run.triggered_by_task_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if (
        task is None
        or task.project_id != run.project_id
        or task.kind is not TaskKind.VALIDATE_CROSS_DISCIPLINE
        or task.status is not TaskStatus.BLOCKED
    ):
        return 0
    completed_at = utcnow()
    completed_result = {
        **task.result,
        "validation_run_id": str(run.id),
        "waivers_applied": True,
        "unwaived_error_count": 0,
    }
    transition = db.execute(
        update(Task)
        .where(
            Task.id == task.id,
            Task.project_id == run.project_id,
            Task.kind == TaskKind.VALIDATE_CROSS_DISCIPLINE,
            Task.status == TaskStatus.BLOCKED,
        )
        .values(
            status=TaskStatus.COMPLETED,
            blocked_reason="",
            finished_at=completed_at,
            result=completed_result,
            locked_by=None,
            locked_at=None,
            lease_expires_at=None,
        )
        .execution_options(synchronize_session=False)
    )
    if int(getattr(transition, "rowcount", 0) or 0) != 1:
        db.refresh(task)
        return 0
    db.refresh(task)
    record_event(
        db,
        project_id=run.project_id,
        event_type=EventType.TASK_COMPLETED,
        agent_id=task.agent_instance_id,
        task_id=task.id,
        payload={
            "task_id": str(task.id),
            "kind": task.kind.value,
            "title": task.title,
            "status": TaskStatus.COMPLETED.value,
            "agent_kind": task.agent_kind or "",
            "attempts": task.attempts,
            "detail": "All validation errors are resolved or carry named reviewer waivers.",
        },
    )
    db.flush()
    return 1


__all__ = ["WaiverOutcome", "latest_results", "waive"]
