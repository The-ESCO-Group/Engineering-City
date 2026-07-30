"""Declarative base and shared column mixins."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, ClassVar

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.core.clock import new_id, utcnow
from app.core.enums import (
    AgentRunStatus,
    AgentStatus,
    AnchorType,
    ApprovalKind,
    ApprovalStatus,
    AuditAction,
    DocumentRole,
    EventType,
    EvidenceKind,
    FileKind,
    FileStatus,
    FindingCategory,
    FindingSeverity,
    FindingStatus,
    MessageRole,
    ProjectRole,
    ProjectStatus,
    RecommendationStatus,
    ReportStatus,
    RoleName,
    TaskKind,
    TaskStatus,
)
from app.database.types import GUID, JSONDict, StrEnumType, UTCDateTime

#: Explicit constraint naming so Alembic can autogenerate reversible migrations
#: (SQLite in particular cannot drop an unnamed constraint).
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Root of the ORM hierarchy."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    #: Maps Python annotations onto the portable column types, so models can be
    #: written with plain ``Mapped[uuid.UUID]`` / ``Mapped[datetime]`` /
    #: ``Mapped[ProjectStatus]``.
    #:
    #: Every enum column MUST be registered here. Passing ``String(n)`` directly
    #: on a ``Mapped[SomeStrEnum]`` column stores the value correctly but loads it
    #: back as a plain ``str``, which silently breaks identity comparisons and
    #: ``.value`` access on any row read from the database. Listing the enum here
    #: means a column whose enum was forgotten fails at mapper configuration
    #: instead. Lengths match the original columns, so adding this changed no
    #: DDL and required no migration.
    type_annotation_map: ClassVar[dict[Any, Any]] = {
        uuid.UUID: GUID,
        datetime: UTCDateTime,
        dict[str, Any]: JSONDict,
        RoleName: StrEnumType(RoleName, 32),
        ProjectRole: StrEnumType(ProjectRole, 32),
        ProjectStatus: StrEnumType(ProjectStatus, 32),
        FileKind: StrEnumType(FileKind, 32),
        FileStatus: StrEnumType(FileStatus, 32),
        DocumentRole: StrEnumType(DocumentRole, 40),
        AnchorType: StrEnumType(AnchorType, 40),
        EvidenceKind: StrEnumType(EvidenceKind, 32),
        TaskKind: StrEnumType(TaskKind, 40),
        TaskStatus: StrEnumType(TaskStatus, 32),
        AgentStatus: StrEnumType(AgentStatus, 32),
        AgentRunStatus: StrEnumType(AgentRunStatus, 32),
        MessageRole: StrEnumType(MessageRole, 16),
        FindingCategory: StrEnumType(FindingCategory, 40),
        FindingSeverity: StrEnumType(FindingSeverity, 20),
        FindingStatus: StrEnumType(FindingStatus, 20),
        RecommendationStatus: StrEnumType(RecommendationStatus, 20),
        ApprovalKind: StrEnumType(ApprovalKind, 48),
        ApprovalStatus: StrEnumType(ApprovalStatus, 20),
        ReportStatus: StrEnumType(ReportStatus, 20),
        EventType: StrEnumType(EventType, 48),
        AuditAction: StrEnumType(AuditAction, 48),
    }

    def __repr__(self) -> str:  # pragma: no cover - debugging affordance
        ident = getattr(self, "id", None)
        return f"<{type(self).__name__} id={ident}>"


class UUIDPrimaryKeyMixin:
    """Adds a random-UUID primary key generated in Python.

    Generating client-side (rather than via a database default) means the id is
    known before flush, which the event and audit writers rely on.
    """

    id: Mapped[uuid.UUID] = mapped_column(GUID, primary_key=True, default=new_id)


class TimestampMixin:
    """Adds UTC ``created_at`` / ``updated_at`` columns."""

    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utcnow, nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utcnow, onupdate=utcnow, nullable=False
    )
