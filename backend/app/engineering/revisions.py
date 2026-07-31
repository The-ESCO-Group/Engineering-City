"""Revision creation and lookup.

A :class:`~app.models.revision.ProjectRevision` is a numbered label on a
project's timeline, not a copy of the model. Revision 0 exists from the moment a
project has an engineering model at all, so an agent always has a real base to
propose against rather than a null every caller has to special-case.

The number is allocated with an atomic ``UPDATE`` against
``Project.current_revision_number``, in the same shape the event sequence uses,
because two merges landing at once must not mint the same revision number: the
uniqueness of ``(project_id, number)`` is what makes "which revision introduced
this rung" a lookup instead of a guess.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.core.enums import EventType
from app.models.project import Project
from app.models.revision import ChangeSet, ProjectRevision
from app.orchestration.events import record_event

__all__ = [
    "allocate_revision_number",
    "create_revision",
    "current_revision_number",
    "ensure_initial_revision",
    "get_revision",
    "latest_revision",
    "mark_release_baseline",
    "revision_count",
]


def current_revision_number(db: Session, project_id: uuid.UUID) -> int:
    """The project's head revision, read from the row rather than from memory."""
    value = db.execute(
        select(Project.current_revision_number).where(Project.id == project_id)
    ).scalar_one_or_none()
    return int(value or 0)


def get_revision(db: Session, project_id: uuid.UUID, number: int) -> ProjectRevision | None:
    return db.execute(
        select(ProjectRevision).where(
            ProjectRevision.project_id == project_id, ProjectRevision.number == number
        )
    ).scalar_one_or_none()


def latest_revision(db: Session, project_id: uuid.UUID) -> ProjectRevision | None:
    return db.execute(
        select(ProjectRevision)
        .where(ProjectRevision.project_id == project_id)
        .order_by(ProjectRevision.number.desc())
        .limit(1)
    ).scalar_one_or_none()


def revision_count(db: Session, project_id: uuid.UUID) -> int:
    return int(
        db.execute(
            select(func.count(ProjectRevision.id)).where(ProjectRevision.project_id == project_id)
        ).scalar()
        or 0
    )


def ensure_initial_revision(db: Session, project: Project) -> ProjectRevision:
    """Create revision 0 if the project has none. Idempotent.

    Called on the merge path rather than at project creation so that a project
    created before the canonical model existed still acquires a base the first
    time anything is merged into it.
    """
    existing = get_revision(db, project.id, 0)
    if existing is not None:
        return existing

    revision = ProjectRevision(
        project_id=project.id,
        number=0,
        parent_revision_id=None,
        changeset_id=None,
        summary="Initial revision. Nothing has been merged into the engineering model yet.",
        created_by_id=project.created_by_id,
        object_counts={},
        operation_count=0,
    )
    db.add(revision)
    db.flush()
    return revision


def allocate_revision_number(db: Session, project: Project) -> int:
    """Reserve the next revision number, atomically.

    Split out from :func:`create_revision` because the merge needs the number
    *before* it writes anything — every object it touches records the revision it
    was created or updated in — while the revision row itself may only be written
    once the work has actually landed.
    """
    db.execute(
        update(Project)
        .where(Project.id == project.id)
        .values(current_revision_number=Project.current_revision_number + 1)
    )
    db.expire(project, ["current_revision_number"])
    return int(project.current_revision_number)


def create_revision(
    db: Session,
    project: Project,
    *,
    changeset: ChangeSet | None = None,
    summary: str = "",
    object_counts: dict[str, Any] | None = None,
    number: int | None = None,
    operation_count: int = 0,
    actor_user_id: uuid.UUID | None = None,
) -> ProjectRevision:
    """Record a merge point and announce it.

    ``number`` is allocated here when the caller has not already reserved one.
    The merge reserves it first (see :func:`allocate_revision_number`) so that
    the objects it writes and the revision that labels them cannot disagree.
    """
    if number is None:
        number = allocate_revision_number(db, project)

    parent = get_revision(db, project.id, number - 1) if number > 0 else None
    revision = ProjectRevision(
        project_id=project.id,
        number=number,
        parent_revision_id=parent.id if parent is not None else None,
        changeset_id=changeset.id if changeset is not None else None,
        summary=summary,
        created_by_id=actor_user_id or (changeset.author_user_id if changeset else None),
        created_by_agent=changeset.author_agent if changeset is not None else "",
        object_counts=object_counts or {},
        operation_count=operation_count,
    )
    db.add(revision)
    db.flush()

    record_event(
        db,
        project_id=project.id,
        event_type=EventType.REVISION_CREATED,
        actor_user_id=actor_user_id,
        payload={
            "revision_id": str(revision.id),
            "number": revision.number,
            "changeset_id": str(changeset.id) if changeset is not None else None,
            "summary": revision.summary,
            "object_counts": revision.object_counts,
            "operation_count": revision.operation_count,
        },
    )
    return revision


def mark_release_baseline(db: Session, revision: ProjectRevision) -> ProjectRevision:
    """Freeze a revision as the basis of a release candidate.

    A baseline revision must never become the base for an in-place edit: a
    package with a checksum has to keep meaning what it meant when the checksum
    was taken.
    """
    revision.is_release_baseline = True
    db.flush()
    return revision
