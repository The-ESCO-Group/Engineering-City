"""Append-only audit trail.

Separate from :class:`~app.models.event.Event`: events describe what the product
did for the UI's benefit, audit entries record *who* caused a security- or
engineering-relevant change and are never deleted or edited.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.enums import AuditAction
from app.database.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.database.types import GUID, JSONDict


class AuditEntry(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One immutable record of a consequential operation."""

    __tablename__ = "audit_entries"
    __table_args__ = (
        Index("ix_audit_entries_project_id_created_at", "project_id", "created_at"),
        Index("ix_audit_entries_action_created_at", "action", "created_at"),
    )

    action: Mapped[AuditAction] = mapped_column(nullable=False, index=True)

    #: The authenticated human, when there was one.
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    #: Set when the actor was an agent rather than a person.
    actor_agent_kind: Mapped[str] = mapped_column(String(40), default="", nullable=False)
    #: Free-form label for system actors ("worker", "startup").
    actor_label: Mapped[str] = mapped_column(String(80), default="", nullable=False)

    project_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("projects.id", ondelete="SET NULL"), nullable=True, index=True
    )
    target_type: Mapped[str] = mapped_column(String(48), default="", nullable=False)
    target_id: Mapped[uuid.UUID | None] = mapped_column(GUID, nullable=True, index=True)

    summary: Mapped[str] = mapped_column(Text, default="", nullable=False)
    #: Before/after values, request parameters — never secrets or file bytes.
    details: Mapped[dict[str, Any]] = mapped_column(JSONDict, default=dict, nullable=False)

    #: Request provenance. Useful when reconstructing an incident.
    request_id: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    source_ip: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    user_agent: Mapped[str] = mapped_column(String(255), default="", nullable=False)
