"""Generated reports.

Report *content* is produced by GPT-owned providers under ``app.reporting``;
this table is the core-owned record of what was generated, by whom and from
which findings.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import BigInteger, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.enums import ReportStatus
from app.database.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.database.types import GUID, JSONDict, JSONList


class Report(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A rendered artifact stored under ``data/projects/{id}/reports/``."""

    __tablename__ = "reports"
    __table_args__ = (Index("ix_reports_project_id_status", "project_id", "status"),)

    project_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    task_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True
    )
    generated_by_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    #: Provider key, e.g. ``discrepancy_matrix`` or ``test_plan``.
    report_type: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    #: Export format key, e.g. ``markdown``, ``html``, ``xlsx``.
    export_format: Mapped[str] = mapped_column(String(32), default="markdown", nullable=False)
    provider_name: Mapped[str] = mapped_column(String(80), default="", nullable=False)
    provider_version: Mapped[str] = mapped_column(String(40), default="", nullable=False)

    title: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[ReportStatus] = mapped_column(
        String(20), default=ReportStatus.DRAFT, nullable=False
    )
    #: Relative to the project directory; never an absolute path.
    relative_path: Mapped[str] = mapped_column(String(1024), default="", nullable=False)
    media_type: Mapped[str] = mapped_column(String(160), default="", nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), default="", nullable=False)

    #: Ids of the findings included, so a report is traceable to its inputs.
    finding_ids: Mapped[list[Any]] = mapped_column(JSONList, default=list, nullable=False)
    summary: Mapped[dict[str, Any]] = mapped_column(JSONDict, default=dict, nullable=False)
    #: Populated when ``status`` is ``failed``.
    error_message: Mapped[str] = mapped_column(Text, default="", nullable=False)
