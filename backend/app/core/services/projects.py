"""Project domain logic: creation, listing, updates, membership, archival."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from app.core.clock import utcnow
from app.core.config import Settings
from app.core.enums import (
    AuditAction,
    EventType,
    FileStatus,
    ProjectRole,
    ProjectStatus,
    TaskStatus,
)
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.storage import ProjectStorage
from app.models.approval import Approval
from app.models.file import UploadedFile
from app.models.finding import Finding
from app.models.project import Project, ProjectMember
from app.models.report import Report
from app.models.task import Task
from app.models.user import User
from app.orchestration.events import record_event
from app.security import audit


def create_project(
    db: Session,
    *,
    name: str,
    description: str,
    client_reference: str,
    attributes: dict[str, Any],
    creator: User,
    settings: Settings,
    source_ip: str = "",
    user_agent: str = "",
) -> Project:
    """Create a project, make the creator its owner, and lay out its storage."""
    cleaned = name.strip()
    if not cleaned:
        raise ValidationError("Project name is required.")

    project = Project(
        name=cleaned,
        description=description.strip(),
        client_reference=client_reference.strip(),
        attributes=attributes or {},
        status=ProjectStatus.DRAFT,
        created_by_id=creator.id,
    )
    db.add(project)
    db.flush()

    db.add(ProjectMember(project_id=project.id, user_id=creator.id, role=ProjectRole.OWNER))

    # Create the directory tree up front so an upload never has to decide
    # whether it is also responsible for bootstrapping storage.
    ProjectStorage(project.id, settings).ensure_layout()

    audit.record(
        db,
        AuditAction.PROJECT_CREATED,
        actor_user_id=creator.id,
        project_id=project.id,
        target_type="project",
        target_id=project.id,
        summary=f"Created project '{project.name}'",
        details={"client_reference": project.client_reference},
        source_ip=source_ip,
        user_agent=user_agent,
    )
    record_event(
        db,
        project_id=project.id,
        event_type=EventType.PROJECT_CREATED,
        actor_user_id=creator.id,
        payload={
            "project_id": str(project.id),
            "name": project.name,
            "status": project.status.value,
        },
    )
    return project


def get_project(db: Session, project_id: uuid.UUID) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise NotFoundError("Project", project_id)
    return project


def visible_projects_query(user: User) -> Select[tuple[Project]]:
    """Projects the user may see.

    Administrators see everything; everyone else sees only projects they are a
    member of. Archived projects are included — they are read-only history, not
    deleted data.
    """
    statement = select(Project)
    if not user.is_administrator:
        statement = statement.join(ProjectMember).where(ProjectMember.user_id == user.id)
    return statement


def list_projects(
    db: Session,
    user: User,
    *,
    status: ProjectStatus | None = None,
    include_archived: bool = True,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[Project], int]:
    statement = visible_projects_query(user)
    if status is not None:
        statement = statement.where(Project.status == status)
    elif not include_archived:
        statement = statement.where(Project.status != ProjectStatus.ARCHIVED)

    total = db.execute(select(func.count()).select_from(statement.subquery())).scalar_one()
    rows = list(
        db.execute(
            statement.order_by(Project.created_at.desc()).limit(limit).offset(offset)
        ).scalars()
    )
    return rows, int(total)


def update_project(
    db: Session,
    project: Project,
    *,
    changes: dict[str, Any],
    actor: User,
) -> Project:
    """Apply a partial update.

    ``status`` is not updatable through this path: project status belongs to the
    workflow engine, and letting a client set it would make the state machine a
    suggestion.
    """
    if project.status is ProjectStatus.ARCHIVED:
        raise ConflictError("Archived projects are read-only.")

    applied: dict[str, Any] = {}
    for field in ("name", "description", "client_reference", "attributes"):
        if field not in changes or changes[field] is None:
            continue
        value = changes[field]
        if field == "name":
            value = str(value).strip()
            if not value:
                raise ValidationError("Project name cannot be empty.")
        before = getattr(project, field)
        if before != value:
            setattr(project, field, value)
            applied[field] = {"before": before, "after": value}

    if not applied:
        return project

    db.flush()
    audit.record(
        db,
        AuditAction.PROJECT_UPDATED,
        actor_user_id=actor.id,
        project_id=project.id,
        target_type="project",
        target_id=project.id,
        summary=f"Updated project '{project.name}'",
        details={"changed": sorted(applied)},
    )
    record_event(
        db,
        project_id=project.id,
        event_type=EventType.PROJECT_UPDATED,
        actor_user_id=actor.id,
        payload={
            "project_id": str(project.id),
            "name": project.name,
            "status": project.status.value,
        },
    )
    return project


def archive_project(db: Session, project: Project, *, actor: User, reason: str = "") -> Project:
    """Archive a project.

    Archiving is reversible in principle but has no un-archive endpoint yet, and
    nothing is deleted: files, findings and the audit trail stay intact.
    """
    if project.status is ProjectStatus.ARCHIVED:
        raise ConflictError("Project is already archived.")

    previous = project.status
    project.status = ProjectStatus.ARCHIVED
    project.archived_at = utcnow()
    db.flush()

    audit.record(
        db,
        AuditAction.PROJECT_ARCHIVED,
        actor_user_id=actor.id,
        project_id=project.id,
        target_type="project",
        target_id=project.id,
        summary=f"Archived project '{project.name}'",
        details={"previous_status": previous.value, "reason": reason},
    )
    record_event(
        db,
        project_id=project.id,
        event_type=EventType.PROJECT_UPDATED,
        actor_user_id=actor.id,
        payload={
            "project_id": str(project.id),
            "name": project.name,
            "status": project.status.value,
        },
    )
    return project


def add_member(
    db: Session,
    project: Project,
    *,
    user_id: uuid.UUID,
    role: ProjectRole,
    actor: User,
) -> ProjectMember:
    if db.get(User, user_id) is None:
        raise NotFoundError("User", user_id)

    existing = db.execute(
        select(ProjectMember).where(
            ProjectMember.project_id == project.id,
            ProjectMember.user_id == user_id,
        )
    ).scalar_one_or_none()

    if existing is not None:
        before = existing.role
        existing.role = role
        member = existing
        summary = f"Changed project role for user {user_id}"
        details = {"before": before, "after": role.value}
    else:
        member = ProjectMember(project_id=project.id, user_id=user_id, role=role)
        db.add(member)
        summary = f"Added user {user_id} to project"
        details = {"role": role.value}

    db.flush()
    audit.record(
        db,
        AuditAction.PERMISSION_CHANGED,
        actor_user_id=actor.id,
        project_id=project.id,
        target_type="project_member",
        target_id=member.id,
        summary=summary,
        details=details,
    )
    return member


def project_counts(db: Session, project_id: uuid.UUID) -> dict[str, int]:
    """Roll-ups for the project detail view and the city overview.

    One query per collection, all COUNTs — cheap enough to serve on every detail
    fetch and far cheaper than loading the collections themselves.
    """

    def count(statement: Select[Any]) -> int:
        return int(db.execute(statement).scalar() or 0)

    live_files = (UploadedFile.project_id == project_id, UploadedFile.deleted_at.is_(None))
    return {
        "files": count(select(func.count(UploadedFile.id)).where(*live_files)),
        "files_parsed": count(
            select(func.count(UploadedFile.id)).where(
                *live_files, UploadedFile.status == FileStatus.PARSED
            )
        ),
        "tasks": count(select(func.count(Task.id)).where(Task.project_id == project_id)),
        "tasks_completed": count(
            select(func.count(Task.id)).where(
                Task.project_id == project_id, Task.status == TaskStatus.COMPLETED
            )
        ),
        "findings": count(select(func.count(Finding.id)).where(Finding.project_id == project_id)),
        "findings_open": count(
            select(func.count(Finding.id)).where(
                Finding.project_id == project_id, Finding.status == "open"
            )
        ),
        "approvals_pending": count(
            select(func.count(Approval.id)).where(
                Approval.project_id == project_id, Approval.status == "pending"
            )
        ),
        "reports": count(select(func.count(Report.id)).where(Report.project_id == project_id)),
    }


def member_rows(db: Session, project_id: uuid.UUID) -> list[dict[str, Any]]:
    """Members with their display names, for the detail response."""
    rows = db.execute(
        select(ProjectMember, User)
        .join(User, User.id == ProjectMember.user_id)
        .where(ProjectMember.project_id == project_id)
        .order_by(User.display_name)
    ).all()
    return [
        {
            "id": member.id,
            "user_id": member.user_id,
            "role": member.role,
            "display_name": user.display_name,
            "email": user.email,
        }
        for member, user in rows
    ]
