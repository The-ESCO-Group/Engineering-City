"""Portable write-serialization helpers for project-scoped state transitions."""

from __future__ import annotations

import uuid

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models.project import Project


def lock_project_root(db: Session, project_id: uuid.UUID) -> Project | None:
    """Acquire the project write root and return an authoritative ORM identity.

    PostgreSQL honours ``SELECT .. FOR UPDATE``; SQLite does not.  A no-op
    ``UPDATE`` first gives both dialects the same serialization point.  The
    explicit ``updated_at = updated_at`` prevents the lock acquisition itself
    from looking like a user-visible project edit, and ``populate_existing`` is
    required because API and worker sessions commonly pre-load this identity.
    """
    db.execute(
        update(Project)
        .where(Project.id == project_id)
        .values(
            current_revision_number=Project.current_revision_number,
            updated_at=Project.updated_at,
        )
        .execution_options(synchronize_session=False)
    )
    return db.execute(
        select(Project)
        .where(Project.id == project_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()


__all__ = ["lock_project_root"]
