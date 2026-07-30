"""Projects and project membership."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import ProjectRole, ProjectStatus
from app.database.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.database.types import GUID, JSONDict, UTCDateTime

if TYPE_CHECKING:
    from app.models.file import UploadedFile
    from app.models.task import Task
    from app.models.user import User


class Project(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """An engineering review engagement: a bundle of files plus everything the
    platform derived from them."""

    __tablename__ = "projects"

    name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    client_reference: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    status: Mapped[ProjectStatus] = mapped_column(
        default=ProjectStatus.DRAFT, nullable=False, index=True
    )

    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    #: Set when a workflow run is paused; the worker skips paused projects.
    is_paused: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    paused_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    #: Populated when status is ``failed`` so the UI can explain the failure.
    failure_reason: Mapped[str] = mapped_column(Text, default="", nullable=False)

    #: Monotonic per-project event sequence, used for WebSocket replay.
    event_seq: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    #: Running totals enforced against the configured project budgets.
    tokens_used: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cost_used_micros: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    #: Free-form, non-authoritative metadata (site, unit, discipline tags).
    attributes: Mapped[dict[str, Any]] = mapped_column(JSONDict, default=dict, nullable=False)

    members: Mapped[list[ProjectMember]] = relationship(
        back_populates="project", cascade="all, delete-orphan", lazy="selectin"
    )
    files: Mapped[list[UploadedFile]] = relationship(
        back_populates="project", cascade="all, delete-orphan", lazy="select"
    )
    tasks: Mapped[list[Task]] = relationship(
        back_populates="project", cascade="all, delete-orphan", lazy="select"
    )

    @property
    def is_archived(self) -> bool:
        return self.status is ProjectStatus.ARCHIVED

    @property
    def accepts_work(self) -> bool:
        """Whether the worker may pick up tasks for this project."""
        return not self.is_paused and self.status not in {
            ProjectStatus.ARCHIVED,
            ProjectStatus.COMPLETED,
            ProjectStatus.FAILED,
        }


class ProjectMember(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Grants a user a role on one project.

    Access control is membership-based: holding the global ``engineer`` role does
    not by itself grant access to someone else's project.
    """

    __tablename__ = "project_members"
    __table_args__ = (
        UniqueConstraint("project_id", "user_id", name="uq_project_members_project_id_user_id"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role: Mapped[ProjectRole] = mapped_column(nullable=False)

    project: Mapped[Project] = relationship(back_populates="members")
    user: Mapped[User] = relationship(back_populates="memberships")
