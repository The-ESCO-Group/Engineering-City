"""Findings, their evidence links, and the recommendations derived from them."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import (
    EvidenceKind,
    FindingCategory,
    FindingSeverity,
    FindingStatus,
    RecommendationStatus,
)
from app.database.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.database.types import GUID, JSONDict, JSONList, UTCDateTime

if TYPE_CHECKING:
    from app.models.evidence import EvidenceAnchor


class Finding(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A discrepancy or observation an agent raised.

    Invariants enforced by the service layer, not just by convention:

    * every finding carries at least one :class:`FindingEvidence` row;
    * ``fact`` and ``inference`` are separate fields so a report never presents
      a deduction as an observation;
    * a finding is only visible as "reviewed" after the Senior Review Agent has
      recorded a verdict, and only "released" after a human approval.
    """

    __tablename__ = "findings"
    __table_args__ = (
        Index("ix_findings_project_id_status", "project_id", "status"),
        Index("ix_findings_project_id_severity", "project_id", "severity"),
        UniqueConstraint("project_id", "dedupe_key", name="uq_findings_project_id_dedupe_key"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    task_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True
    )
    agent_run_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_by_agent_kind: Mapped[str] = mapped_column(String(40), default="", nullable=False)

    title: Mapped[str] = mapped_column(String(255), nullable=False)
    category: Mapped[FindingCategory] = mapped_column(
        default=FindingCategory.OTHER, nullable=False, index=True
    )
    severity: Mapped[FindingSeverity] = mapped_column(
        default=FindingSeverity.INFORMATIONAL, nullable=False
    )
    status: Mapped[FindingStatus] = mapped_column(
        default=FindingStatus.OPEN, nullable=False, index=True
    )

    #: What the evidence literally shows.
    fact: Mapped[str] = mapped_column(Text, default="", nullable=False)
    #: What the agent concluded from it. Always presented as a conclusion.
    inference: Mapped[str] = mapped_column(Text, default="", nullable=False)
    #: What a human engineer should do to confirm or dismiss this.
    suggested_review_action: Mapped[str] = mapped_column(Text, default="", nullable=False)
    #: Anything that weakens the finding: partial parse, ambiguous naming, ...
    limitations: Mapped[str] = mapped_column(Text, default="", nullable=False)

    #: 0.0-1.0. Lowered by the Senior Review Agent when evidence is thin.
    confidence: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    #: True for safety- or equipment-damage-related content; forces an approval.
    requires_approval: Mapped[bool] = mapped_column(default=False, nullable=False)
    #: True once the Senior Review Agent has recorded a verdict.
    is_reviewed: Mapped[bool] = mapped_column(default=False, nullable=False)

    #: Stable hash of the finding's identity, used to make agent runs idempotent
    #: and to let the Senior Review Agent detect duplicates.
    dedupe_key: Mapped[str] = mapped_column(String(120), nullable=False)

    #: Structured detail: the discrepancy matrix row, comparison result, etc.
    details: Mapped[dict[str, Any]] = mapped_column(JSONDict, default=dict, nullable=False)
    warnings: Mapped[list[Any]] = mapped_column(JSONList, default=list, nullable=False)

    # -- Senior review outcome -------------------------------------------
    review_verdict: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    review_notes: Mapped[str] = mapped_column(Text, default="", nullable=False)
    reviewed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    #: Confidence as first proposed, retained when senior review adjusts it.
    original_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    original_severity: Mapped[str] = mapped_column(String(20), default="", nullable=False)
    #: Points at the surviving finding when this one was merged as a duplicate.
    superseded_by_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("findings.id", ondelete="SET NULL"), nullable=True
    )

    # -- Human disposition ------------------------------------------------
    decided_by_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    decided_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    decision_notes: Mapped[str] = mapped_column(Text, default="", nullable=False)

    evidence_links: Mapped[list[FindingEvidence]] = relationship(
        back_populates="finding", cascade="all, delete-orphan", lazy="selectin"
    )
    comments: Mapped[list[FindingComment]] = relationship(
        back_populates="finding", cascade="all, delete-orphan", lazy="select"
    )
    recommendations: Mapped[list[Recommendation]] = relationship(
        back_populates="finding", cascade="all, delete-orphan", lazy="select"
    )

    @property
    def has_direct_evidence(self) -> bool:
        return any(link.evidence_kind is EvidenceKind.DIRECT for link in self.evidence_links)


class FindingEvidence(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Links a finding to one evidence anchor, recording *how* it is used.

    The same anchor can support one finding as direct evidence and another as
    the basis for an inference, so the kind lives on the link.
    """

    __tablename__ = "finding_evidence"
    __table_args__ = (
        UniqueConstraint("finding_id", "anchor_id", "role", name="uq_finding_evidence_link"),
    )

    finding_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("findings.id", ondelete="CASCADE"), nullable=False, index=True
    )
    anchor_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("evidence_anchors.id", ondelete="CASCADE"), nullable=False, index=True
    )
    evidence_kind: Mapped[EvidenceKind] = mapped_column(default=EvidenceKind.DIRECT, nullable=False)
    #: ``requirement`` | ``logic`` | ``context`` — which side of a comparison
    #: this evidence represents.
    role: Mapped[str] = mapped_column(String(32), default="context", nullable=False)
    note: Mapped[str] = mapped_column(Text, default="", nullable=False)

    finding: Mapped[Finding] = relationship(back_populates="evidence_links")
    anchor: Mapped[EvidenceAnchor] = relationship(lazy="selectin")


class FindingComment(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A human note attached to a finding during review."""

    __tablename__ = "finding_comments"

    finding_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("findings.id", ondelete="CASCADE"), nullable=False, index=True
    )
    author_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    body: Mapped[str] = mapped_column(Text, nullable=False)

    finding: Mapped[Finding] = relationship(back_populates="comments")


class Recommendation(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A proposed engineering change.

    Recommendations are *never* applied by the platform. They exist to be read,
    approved or rejected by a human engineer; nothing downstream treats an
    approved recommendation as an executed change.
    """

    __tablename__ = "recommendations"

    project_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    finding_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("findings.id", ondelete="CASCADE"), nullable=True, index=True
    )
    agent_run_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True
    )
    created_by_agent_kind: Mapped[str] = mapped_column(String(40), default="", nullable=False)

    title: Mapped[str] = mapped_column(String(255), nullable=False)
    body: Mapped[str] = mapped_column(Text, default="", nullable=False)
    rationale: Mapped[str] = mapped_column(Text, default="", nullable=False)
    status: Mapped[RecommendationStatus] = mapped_column(
        default=RecommendationStatus.PROPOSED, nullable=False, index=True
    )

    #: Safety- or damage-related recommendations always require approval; the
    #: service layer refuses to persist one with this set to False.
    safety_related: Mapped[bool] = mapped_column(default=False, nullable=False)
    equipment_damage_related: Mapped[bool] = mapped_column(default=False, nullable=False)
    requires_approval: Mapped[bool] = mapped_column(default=True, nullable=False)

    confidence: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    effort_estimate: Mapped[str] = mapped_column(String(40), default="", nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSONDict, default=dict, nullable=False)
    #: Ordering hint for report generation.
    display_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    finding: Mapped[Finding | None] = relationship(back_populates="recommendations")
