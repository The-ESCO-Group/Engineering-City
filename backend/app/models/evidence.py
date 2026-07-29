"""Evidence anchors: the addressable citations every finding must point at."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from sqlalchemy import ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import AnchorType, EvidenceKind
from app.database.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.database.types import GUID, JSONDict

if TYPE_CHECKING:
    from app.models.file import UploadedFile

#: Length cap on a stored excerpt. Long enough to be meaningful in a report,
#: short enough that a finding never smuggles a whole document into the record.
MAX_EXCERPT_CHARS = 2_000


class EvidenceAnchor(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A precise, re-locatable pointer into a source file.

    Two halves matter:

    ``display_location``
        What a human reads: ``"MainProgram / Conveyor_Interlocks / Rung 12"``.

    ``structured_location``
        What software matches on: ``{"program": "MainProgram", "routine":
        "Conveyor_Interlocks", "rung": 12}``. Shapes are per ``anchor_type`` and
        documented in ``docs/backend/evidence.md``.

    ``content_hash`` fixes the excerpt at the time the anchor was created, so a
    re-upload that changes the underlying file is detectable rather than
    silently re-pointing an old finding at new text.
    """

    __tablename__ = "evidence_anchors"
    __table_args__ = (
        Index("ix_evidence_anchors_project_id_anchor_type", "project_id", "anchor_type"),
        Index("ix_evidence_anchors_file_id_anchor_type", "file_id", "anchor_type"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    file_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("uploaded_files.id", ondelete="CASCADE"), nullable=False, index=True
    )
    artifact_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("parsed_artifacts.id", ondelete="SET NULL"), nullable=True
    )

    anchor_type: Mapped[AnchorType] = mapped_column(String(40), nullable=False)
    evidence_kind: Mapped[EvidenceKind] = mapped_column(
        String(32), default=EvidenceKind.DIRECT, nullable=False, index=True
    )

    #: Short label naming the source, e.g. the filename or sheet name.
    source_label: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    display_location: Mapped[str] = mapped_column(String(512), default="", nullable=False)
    structured_location: Mapped[dict[str, Any]] = mapped_column(
        JSONDict, default=dict, nullable=False
    )

    excerpt: Mapped[str] = mapped_column(Text, default="", nullable=False)
    #: SHA-256 of the excerpt as extracted.
    content_hash: Mapped[str] = mapped_column(String(64), default="", nullable=False, index=True)
    parser_version: Mapped[str] = mapped_column(String(40), default="", nullable=False)

    #: Set on ``missing_expected`` anchors: what was looked for and not found.
    expectation: Mapped[str] = mapped_column(Text, default="", nullable=False)

    file: Mapped[UploadedFile] = relationship(back_populates="anchors")

    @property
    def is_direct(self) -> bool:
        return self.evidence_kind is EvidenceKind.DIRECT
