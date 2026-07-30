"""Persisted domain events.

Events are written to this table *before* they are broadcast, so a client that
reconnects can replay everything it missed by sequence number, and a crash
between "state changed" and "client notified" loses nothing.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, Index, Integer, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.enums import EventType
from app.database.base import Base, UUIDPrimaryKeyMixin
from app.database.types import GUID, JSONDict, UTCDateTime


class Event(UUIDPrimaryKeyMixin, Base):
    """One entry in a project's ordered event log.

    ``seq`` is a per-project counter allocated under the same transaction that
    writes the row, giving clients a gap-free ordering to resume from.
    """

    __tablename__ = "events"
    __table_args__ = (
        UniqueConstraint("project_id", "seq", name="uq_events_project_id_seq"),
        Index("ix_events_project_id_occurred_at", "project_id", "occurred_at"),
        Index("ix_events_project_id_event_type", "project_id", "event_type"),
    )

    #: Envelope version. Bumped only on a breaking change to the payload shape.
    schema_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)

    project_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    event_type: Mapped[EventType] = mapped_column(nullable=False)

    agent_id: Mapped[uuid.UUID | None] = mapped_column(GUID, nullable=True)
    task_id: Mapped[uuid.UUID | None] = mapped_column(GUID, nullable=True)
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(GUID, nullable=True)

    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONDict, default=dict, nullable=False)
