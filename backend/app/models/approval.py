"""Human approval gates.

Nothing safety-related leaves the platform as an engineering conclusion without
a row here carrying a real person's decision.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.enums import ApprovalDecision, ApprovalGateKey, ApprovalKind, ApprovalStatus
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
    #: All three subject references are ``SET NULL``, never ``CASCADE``, for the
    #: same reason ``task_id`` above is: this row *is* the record that a named
    #: person accepted responsibility, and deleting the thing the decision was
    #: about must not delete the decision. Findings are hard-deleted in normal
    #: operation — :meth:`app.agents.base.executor.AgentExecutor._create_finding`
    #: discards a re-run finding whose evidence links no longer resolve — so a
    #: cascade here would take ``decision``, ``decision_notes``, ``decided_by_id``
    #: and ``decided_at`` with it. That is precisely the distinction
    #: :attr:`~app.core.enums.ApprovalStatus.RISK_ACCEPTED` exists to preserve:
    #: a release that proceeded on knowingly accepted risk must not become
    #: indistinguishable afterwards from one with no risk at all. An approval
    #: whose subject is gone is an orphan that still reads; ``title``,
    #: ``question`` and ``context`` say what it was about.
    finding_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("findings.id", ondelete="SET NULL"), nullable=True, index=True
    )
    recommendation_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("recommendations.id", ondelete="SET NULL"), nullable=True, index=True
    )
    report_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("reports.id", ondelete="SET NULL"), nullable=True
    )
    #: Set when the decision is about merging a proposed change to the canonical
    #: engineering model rather than about a finding.
    change_set_id: Mapped[uuid.UUID | None] = mapped_column(GUID, nullable=True, index=True)
    release_candidate_id: Mapped[uuid.UUID | None] = mapped_column(GUID, nullable=True, index=True)
    #: Set on a gate decision. Which of the six gates this settles.
    gate_key: Mapped[ApprovalGateKey | None] = mapped_column(nullable=True, index=True)
    #: The impact analysis a reviewer must see before approving a rename. An
    #: approval that names one is an approval given *with* the consequences in
    #: front of the reviewer, which is the only kind worth recording.
    impact_report_id: Mapped[uuid.UUID | None] = mapped_column(GUID, nullable=True)
    #: The revision the decision applies to. A later change does not inherit it.
    revision_number: Mapped[int | None] = mapped_column(Integer, nullable=True)

    kind: Mapped[ApprovalKind] = mapped_column(nullable=False, index=True)
    status: Mapped[ApprovalStatus] = mapped_column(
        default=ApprovalStatus.PENDING, nullable=False, index=True
    )
    #: What the reviewer chose, kept separately from ``status`` because the two
    #: are not the same thing: ``ACCEPT_RISK`` and ``APPROVE`` both leave an
    #: approval settled, and a release that proceeded on knowingly accepted risk
    #: must not be indistinguishable afterwards from one with no risk at all.
    decision: Mapped[ApprovalDecision | None] = mapped_column(nullable=True)

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
