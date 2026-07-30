"""Workflow tasks, their dependencies, and the durable queue lease columns."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import TaskKind, TaskStatus
from app.database.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.database.types import GUID, JSONDict, UTCDateTime

if TYPE_CHECKING:
    from app.models.project import Project


class Task(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A unit of work the persisted queue hands to a worker.

    The queue is the database. ``locked_by`` / ``lease_expires_at`` implement an
    at-least-once lease: a worker that dies mid-task leaves an expired lease,
    which the recovery sweep requeues. Handlers must therefore be idempotent,
    which ``idempotency_key`` supports.
    """

    __tablename__ = "tasks"
    __table_args__ = (
        Index("ix_tasks_status_available_at", "status", "available_at"),
        Index("ix_tasks_project_id_status", "project_id", "status"),
        UniqueConstraint("project_id", "idempotency_key", name="uq_tasks_project_id_idem"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    run_id: Mapped[uuid.UUID | None] = mapped_column(GUID, nullable=True, index=True)

    kind: Mapped[TaskKind] = mapped_column(nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)

    status: Mapped[TaskStatus] = mapped_column(
        default=TaskStatus.QUEUED, nullable=False, index=True
    )
    #: Lower runs first among tasks that are ready at the same moment.
    priority: Mapped[int] = mapped_column(Integer, default=100, nullable=False)

    #: Agent definition responsible for this task, when one applies.
    agent_kind: Mapped[str | None] = mapped_column(String(40), nullable=True)
    agent_instance_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("agent_instances.id", ondelete="SET NULL"), nullable=True
    )

    #: Handler input. Must be small: ids and flags, never file contents.
    payload: Mapped[dict[str, Any]] = mapped_column(JSONDict, default=dict, nullable=False)
    #: Handler output, retained for restart recovery and for the UI.
    result: Mapped[dict[str, Any]] = mapped_column(JSONDict, default=dict, nullable=False)
    #: Intermediate state written by long handlers so a resumed run can skip
    #: work it already finished.
    checkpoint: Mapped[dict[str, Any]] = mapped_column(JSONDict, default=dict, nullable=False)

    #: Stable per-project key. A second attempt to enqueue the same logical work
    #: is a no-op rather than a duplicate task.
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)

    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    #: Not runnable before this instant (retry backoff).
    available_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True, index=True)

    locked_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    locked_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime, nullable=True, index=True
    )

    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    error_code: Mapped[str] = mapped_column(String(80), default="", nullable=False)
    error_message: Mapped[str] = mapped_column(Text, default="", nullable=False)
    #: Why the task is ``blocked`` — shown to the engineer, not swallowed.
    blocked_reason: Mapped[str] = mapped_column(Text, default="", nullable=False)

    project: Mapped[Project] = relationship(back_populates="tasks")
    dependencies: Mapped[list[TaskDependency]] = relationship(
        back_populates="task",
        foreign_keys="TaskDependency.task_id",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    @property
    def is_terminal(self) -> bool:
        return self.status in {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}

    @property
    def can_retry(self) -> bool:
        return self.attempts < self.max_attempts


class TaskDependency(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """``task`` may not start until ``depends_on`` has completed."""

    __tablename__ = "task_dependencies"
    __table_args__ = (
        UniqueConstraint("task_id", "depends_on_id", name="uq_task_dependencies_task_id_dep"),
    )

    task_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    depends_on_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: When true, a failed dependency fails this task instead of blocking it.
    hard: Mapped[bool] = mapped_column(default=True, nullable=False)

    task: Mapped[Task] = relationship(back_populates="dependencies", foreign_keys=[task_id])
