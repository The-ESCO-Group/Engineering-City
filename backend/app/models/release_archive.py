"""Persisted outer ZIP containers for frozen engineering releases.

The files described by :class:`~app.models.release.ExportArtifact` are the
contents of a release package.  This row describes the separate, deterministic
ZIP container handed to a customer.  Keeping those identities separate is
load-bearing: ``ReleaseCandidate.package_sha256`` is the hash of the package's
checksum index, not the hash of the outer ZIP.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.enums import ImportVerificationStatus, ValidationLevel
from app.database.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.database.types import GUID, UTCDateTime


class ReleaseArchive(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One immutable ZIP built from one frozen release manifest.

    ZIP bytes live under the canonical project store and are addressed by the
    project-relative ``relative_path``.  The database row holds every trust fact
    needed to verify those bytes after a process restart without rebuilding or
    reinterpreting the release.
    """

    __tablename__ = "release_archives"
    __table_args__ = (
        UniqueConstraint(
            "release_candidate_id",
            name="uq_release_archives_release_candidate_id",
        ),
        UniqueConstraint(
            "release_manifest_id",
            name="uq_release_archives_release_manifest_id",
        ),
        UniqueConstraint(
            "project_id",
            "relative_path",
            name="uq_release_archives_project_id_relative_path",
        ),
        Index("ix_release_archives_project_id_finalized_at", "project_id", "finalized_at"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    release_candidate_id: Mapped[uuid.UUID] = mapped_column(
        GUID,
        ForeignKey(
            "release_candidates.id",
            ondelete="CASCADE",
            name="fk_release_archives_release_candidate_id",
        ),
        nullable=False,
        index=True,
    )
    release_manifest_id: Mapped[uuid.UUID] = mapped_column(
        GUID,
        ForeignKey(
            "release_manifests.id",
            ondelete="CASCADE",
            name="fk_release_archives_release_manifest_id",
        ),
        nullable=False,
        index=True,
    )

    #: The person whose signature froze the inner manifest.  The display name
    #: is snapshotted because the user row may later be deactivated or removed.
    approved_by_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    approved_by_display_name: Mapped[str] = mapped_column(String(200), nullable=False)

    #: The authenticated person or workflow identity that performed the
    #: mechanical finalization.  Approval still comes only from the manifest and
    #: final release gate; this field records who caused the ZIP to be persisted.
    finalized_by_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )

    relative_path: Mapped[str] = mapped_column(String(500), nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    media_type: Mapped[str] = mapped_column(
        String(120), default="application/zip", nullable=False
    )
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    file_count: Mapped[int] = mapped_column(Integer, nullable=False)

    source_revision: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    validation_level: Mapped[ValidationLevel] = mapped_column(nullable=False)
    import_verification_status: Mapped[ImportVerificationStatus] = mapped_column(
        nullable=False
    )
    import_verification_detail: Mapped[str] = mapped_column(Text, default="", nullable=False)
    rockwell_import_verified: Mapped[bool] = mapped_column(default=False, nullable=False)

    manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    checksum_file_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    frozen_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    finalized_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, index=True)


__all__ = ["ReleaseArchive"]
