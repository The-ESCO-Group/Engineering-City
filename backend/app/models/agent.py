"""Agent definitions, per-project instances, runs and messages."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import AgentKind, AgentRunStatus, AgentStatus, MessageRole
from app.database.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.database.types import GUID, JSONDict, JSONList, UTCDateTime


class AgentDefinition(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A registered agent type.

    Rows are seeded from the code-side registry at startup so the API can list
    agents (including ones contributed by GPT-owned delivery plugins) without
    the frontend needing to know which are built in.
    """

    __tablename__ = "agent_definitions"
    __table_args__ = (UniqueConstraint("kind", name="uq_agent_definitions_kind"),)

    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    #: Where this agent lives in the city visualisation.
    building: Mapped[str] = mapped_column(String(80), default="", nullable=False)
    #: Task kinds this agent can execute.
    handles_task_kinds: Mapped[list[Any]] = mapped_column(JSONList, default=list, nullable=False)
    #: True for agents supplied by a plugin rather than the core product.
    is_plugin: Mapped[bool] = mapped_column(default=False, nullable=False)
    is_available: Mapped[bool] = mapped_column(default=True, nullable=False)
    #: Explains ``is_available=False`` (e.g. "delivery plugin not installed").
    unavailable_reason: Mapped[str] = mapped_column(Text, default="", nullable=False)

    instances: Mapped[list[AgentInstance]] = relationship(
        back_populates="definition", cascade="all, delete-orphan", lazy="select"
    )


class AgentInstance(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One agent working on one project.

    ``status`` and ``location`` drive the city view; neither is load-bearing for
    correctness.
    """

    __tablename__ = "agent_instances"
    __table_args__ = (
        UniqueConstraint("project_id", "definition_id", name="uq_agent_instances_project_def"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    definition_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("agent_definitions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)

    status: Mapped[AgentStatus] = mapped_column(
        String(32), default=AgentStatus.IDLE, nullable=False
    )
    #: Named place in the city ("intake_dock", "review_hall", ...).
    location: Mapped[str] = mapped_column(String(80), default="", nullable=False)
    #: Short, human-readable statement of what the agent is doing right now.
    current_activity: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    current_task_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True
    )

    definition: Mapped[AgentDefinition] = relationship(back_populates="instances")
    runs: Mapped[list[AgentRun]] = relationship(
        back_populates="agent_instance", cascade="all, delete-orphan", lazy="select"
    )


class AgentRun(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A single bounded execution of an agent against one task.

    Deliberately stores *no* chain-of-thought. ``rationale`` is a concise,
    reviewable statement the agent is asked to produce for humans; the raw model
    transcript is never persisted.
    """

    __tablename__ = "agent_runs"
    __table_args__ = (Index("ix_agent_runs_project_id_status", "project_id", "status"),)

    project_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    agent_instance_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("agent_instances.id", ondelete="SET NULL"), nullable=True, index=True
    )
    task_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True, index=True
    )
    agent_kind: Mapped[str] = mapped_column(String(40), nullable=False, index=True)

    status: Mapped[AgentRunStatus] = mapped_column(
        String(32), default=AgentRunStatus.RUNNING, nullable=False
    )
    provider: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    model: Mapped[str] = mapped_column(String(120), default="", nullable=False)

    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    #: Concise, human-readable justification. Not model reasoning traces.
    rationale: Mapped[str] = mapped_column(Text, default="", nullable=False)
    #: Validated actions the run proposed, with accept/reject outcome each.
    actions: Mapped[list[Any]] = mapped_column(JSONList, default=list, nullable=False)
    #: Actions the backend refused, with the reason. Kept for auditability.
    rejected_actions: Mapped[list[Any]] = mapped_column(JSONList, default=list, nullable=False)
    warnings: Mapped[list[Any]] = mapped_column(JSONList, default=list, nullable=False)
    #: Names/results of deterministic tools the runner executed for the agent.
    tool_results: Mapped[list[Any]] = mapped_column(JSONList, default=list, nullable=False)

    iterations: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cost_micros: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    error_code: Mapped[str] = mapped_column(String(80), default="", nullable=False)
    error_message: Mapped[str] = mapped_column(Text, default="", nullable=False)

    agent_instance: Mapped[AgentInstance | None] = relationship(back_populates="runs")

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class AgentMessage(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A short, human-readable statement shown in the project activity feed.

    This is a communication surface, not a log of model internals.
    """

    __tablename__ = "agent_messages"
    __table_args__ = (Index("ix_agent_messages_project_id_created_at", "project_id", "created_at"),)

    project_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    agent_instance_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("agent_instances.id", ondelete="SET NULL"), nullable=True
    )
    agent_run_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True
    )
    task_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True
    )
    author_user_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    role: Mapped[MessageRole] = mapped_column(String(16), nullable=False)
    agent_kind: Mapped[str] = mapped_column(String(40), default="", nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    #: Optional structured attachment (counts, ids) for richer UI rendering.
    context: Mapped[dict[str, Any]] = mapped_column(JSONDict, default=dict, nullable=False)


class ModelConfiguration(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Which provider/model an agent uses.

    A row with ``agent_kind = ''`` is the project (or global) default. Only
    providers present in ``settings.enabled_providers`` may be referenced;
    the service layer enforces this so a stale row cannot re-enable a provider
    the administrator turned off.
    """

    __tablename__ = "model_configurations"
    __table_args__ = (
        UniqueConstraint("project_id", "agent_kind", name="uq_model_configurations_scope"),
    )

    #: ``None`` means the global default.
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("projects.id", ondelete="CASCADE"), nullable=True, index=True
    )
    agent_kind: Mapped[str] = mapped_column(String(40), default="", nullable=False)

    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(120), nullable=False)
    temperature: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    max_output_tokens: Mapped[int] = mapped_column(Integer, default=4096, nullable=False)
    #: Cost per million tokens, in micro-dollars, for local usage accounting.
    prompt_cost_micros_per_mtok: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    completion_cost_micros_per_mtok: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    options: Mapped[dict[str, Any]] = mapped_column(JSONDict, default=dict, nullable=False)
    updated_by_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class UsageRecord(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Per-call token and cost accounting, used to enforce project budgets."""

    __tablename__ = "usage_records"
    __table_args__ = (Index("ix_usage_records_project_id_created_at", "project_id", "created_at"),)

    project_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("projects.id", ondelete="CASCADE"), nullable=True, index=True
    )
    agent_run_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=True, index=True
    )
    agent_kind: Mapped[str] = mapped_column(String(40), default="", nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(120), nullable=False)

    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cost_micros: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: False when the provider returned an error; the call still consumed budget.
    succeeded: Mapped[bool] = mapped_column(default=True, nullable=False)


#: Convenience for seeding: the four agents this backend owns.
CORE_AGENT_KINDS: tuple[AgentKind, ...] = (
    AgentKind.PROJECT_MANAGER,
    AgentKind.PLC_REVIEW,
    AgentKind.INTERLOCK_REVIEW,
    AgentKind.SENIOR_REVIEW,
)
