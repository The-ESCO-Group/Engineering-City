"""Uploaded files and the artifacts parsers derive from them."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import BigInteger, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import DocumentRole, FileKind, FileStatus
from app.database.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.database.types import GUID, JSONDict, JSONList, UTCDateTime

if TYPE_CHECKING:
    from app.models.evidence import EvidenceAnchor
    from app.models.project import Project


class UploadedFile(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One file an engineer uploaded, or one member extracted from an archive.

    The bytes on disk are treated as immutable: intake writes them once,
    read-only, and every later stage reads from that copy.
    """

    __tablename__ = "uploaded_files"
    __table_args__ = (
        Index("ix_uploaded_files_project_id_status", "project_id", "status"),
        Index("ix_uploaded_files_project_id_sha256", "project_id", "sha256"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    uploaded_by_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    #: Name as supplied by the client, retained verbatim for display only.
    original_filename: Mapped[str] = mapped_column(String(512), nullable=False)
    #: Sanitised, collision-free name actually used on disk.
    stored_filename: Mapped[str] = mapped_column(String(512), nullable=False)
    #: Path relative to the project directory, never an absolute path.
    relative_path: Mapped[str] = mapped_column(String(1024), nullable=False)

    media_type: Mapped[str] = mapped_column(String(160), default="", nullable=False)
    extension: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    file_kind: Mapped[FileKind] = mapped_column(
        default=FileKind.UNKNOWN, nullable=False, index=True
    )
    document_role: Mapped[DocumentRole] = mapped_column(
        default=DocumentRole.UNCLASSIFIED, nullable=False
    )
    #: True when the role was declared by the uploader rather than inferred.
    role_declared_by_user: Mapped[bool] = mapped_column(default=False, nullable=False)

    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), default="", nullable=False, index=True)

    status: Mapped[FileStatus] = mapped_column(
        default=FileStatus.UPLOADED, nullable=False, index=True
    )
    #: Human-readable explanation when status is ``rejected`` or ``parse_failed``.
    status_detail: Mapped[str] = mapped_column(Text, default="", nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    #: Set when this file came out of an uploaded ZIP.
    parent_file_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("uploaded_files.id", ondelete="CASCADE"), nullable=True, index=True
    )
    #: Path inside the containing archive, for provenance in evidence labels.
    archive_member_path: Mapped[str] = mapped_column(String(1024), default="", nullable=False)

    project: Mapped[Project] = relationship(back_populates="files")
    artifacts: Mapped[list[ParsedArtifact]] = relationship(
        back_populates="file", cascade="all, delete-orphan", lazy="select"
    )
    anchors: Mapped[list[EvidenceAnchor]] = relationship(
        back_populates="file", cascade="all, delete-orphan", lazy="select"
    )

    @property
    def is_available(self) -> bool:
        return self.status not in {FileStatus.DELETED, FileStatus.REJECTED}


class ParsedArtifact(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """The normalised structure a parser produced for one file.

    ``content`` holds the parser's normalised document (never the raw bytes).
    Its shape is documented per artifact type in ``docs/backend/parsers.md`` and
    is versioned by ``parser_version`` so a re-parse after a parser change is
    detectable.
    """

    __tablename__ = "parsed_artifacts"
    __table_args__ = (Index("ix_parsed_artifacts_project_id_kind", "project_id", "artifact_kind"),)

    project_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    file_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("uploaded_files.id", ondelete="CASCADE"), nullable=False, index=True
    )

    #: Matches the ``FileKind`` the parser handles (``l5x``, ``xlsx``, ...).
    artifact_kind: Mapped[str] = mapped_column(String(40), nullable=False)
    parser_name: Mapped[str] = mapped_column(String(80), nullable=False)
    parser_version: Mapped[str] = mapped_column(String(40), nullable=False)

    content: Mapped[dict[str, Any]] = mapped_column(JSONDict, default=dict, nullable=False)
    #: Counts and shape hints for quick display without loading ``content``.
    summary: Mapped[dict[str, Any]] = mapped_column(JSONDict, default=dict, nullable=False)
    #: Non-fatal problems: hidden sheets, unreadable regions, low text yield.
    warnings: Mapped[list[Any]] = mapped_column(JSONList, default=list, nullable=False)
    #: Content the parser deliberately did not interpret.
    unsupported: Mapped[list[Any]] = mapped_column(JSONList, default=list, nullable=False)

    duration_ms: Mapped[int] = mapped_column(default=0, nullable=False)

    file: Mapped[UploadedFile] = relationship(back_populates="artifacts")
