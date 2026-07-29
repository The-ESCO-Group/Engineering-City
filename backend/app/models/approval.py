"""Human approval gates.

Nothing safety-related leaves the platform as an engineering conclusion without
a row here carrying a real person's decision.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.enums import ApprovalKind, ApprovalStatus
from app.database.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.database.types import GUID, JSONDict, UTCDateTime


class Approval(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A pending or decided human decision.

    ``decided_by_id`` is required to move out of ``pending``: an approval can
    never be granted by an agent, and the service layer rejects any attempt to
    record a decision without an authenticated reviewer.
    """

    __tablename__ = "approvals"
    __table_args__ = (Index("ix_approvals_project_id_status", "project_id", "status"),)

    project_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    task_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True, index=True
    )
    finding_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("findings.id", ondelete="CASCADE"), nullable=True, index=True
    )
    recommendation_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("recommendations.id", ondelete="CASCADE"), nullable=True, index=True
    )
    report_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("reports.id", ondelete="CASCADE"), nullable=True
    )

    kind: Mapped[ApprovalKind] = mapped_column(String(48), nullable=False, index=True)
    status: Mapped[ApprovalStatus] = mapped_column(
        String(20), default=ApprovalStatus.PENDING, nullable=False, index=True
    )

    title: Mapped[str] = mapped_column(String(255), nullable=False)
    #: Plain-language statement of exactly what the human is being asked to
    #: accept responsibility for.
    question: Mapped[str] = mapped_column(Text, nullable=False)
    #: Snapshot of the evidence and severity at request time, so a later edit
    #: cannot change what was actually approved.
    context: Mapped[dict[str, Any]] = mapped_column(JSONDict, default=dict, nullable=False)

    requested_by_agent_kind: Mapped[str] = mapped_column(String(40), default="", nullable=False)
    requested_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    decided_by_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    decided_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    decision_notes: Mapped[str] = mapped_column(Text, default="", nullable=False)

    @property
    def is_pending(self) -> bool:
        return self.status is ApprovalStatus.PENDING
