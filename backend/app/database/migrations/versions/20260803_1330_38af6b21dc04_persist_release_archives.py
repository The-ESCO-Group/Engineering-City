"""persist deterministic release ZIP archives

Revision ID: 38af6b21dc04
Revises: b067028843ca
Created: 2026-08-03 13:30:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import app.database.types

revision: str = "38af6b21dc04"
down_revision: str | None = "b067028843ca"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "release_archives",
        sa.Column("project_id", app.database.types.GUID(), nullable=False),
        sa.Column("release_candidate_id", app.database.types.GUID(), nullable=False),
        sa.Column("release_manifest_id", app.database.types.GUID(), nullable=False),
        sa.Column("approved_by_id", app.database.types.GUID(), nullable=True),
        sa.Column("approved_by_display_name", sa.String(length=200), nullable=False),
        sa.Column("finalized_by_id", app.database.types.GUID(), nullable=True),
        sa.Column("relative_path", sa.String(length=500), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("media_type", sa.String(length=120), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("file_count", sa.Integer(), nullable=False),
        sa.Column("source_revision", sa.Integer(), nullable=False),
        sa.Column("validation_level", sa.String(length=40), nullable=False),
        sa.Column("import_verification_status", sa.String(length=20), nullable=False),
        sa.Column("import_verification_detail", sa.Text(), nullable=False),
        sa.Column("rockwell_import_verified", sa.Boolean(), nullable=False),
        sa.Column("manifest_sha256", sa.String(length=64), nullable=False),
        sa.Column("checksum_file_sha256", sa.String(length=64), nullable=False),
        sa.Column("frozen_at", app.database.types.UTCDateTime(), nullable=False),
        sa.Column("finalized_at", app.database.types.UTCDateTime(), nullable=False),
        sa.Column("id", app.database.types.GUID(), nullable=False),
        sa.Column("created_at", app.database.types.UTCDateTime(), nullable=False),
        sa.Column("updated_at", app.database.types.UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["approved_by_id"],
            ["users.id"],
            name=op.f("fk_release_archives_approved_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["finalized_by_id"],
            ["users.id"],
            name=op.f("fk_release_archives_finalized_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_release_archives_project_id_projects"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["release_candidate_id"],
            ["release_candidates.id"],
            name="fk_release_archives_release_candidate_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["release_manifest_id"],
            ["release_manifests.id"],
            name="fk_release_archives_release_manifest_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_release_archives")),
        sa.UniqueConstraint(
            "project_id",
            "relative_path",
            name="uq_release_archives_project_id_relative_path",
        ),
        sa.UniqueConstraint(
            "release_candidate_id",
            name="uq_release_archives_release_candidate_id",
        ),
        sa.UniqueConstraint(
            "release_manifest_id",
            name="uq_release_archives_release_manifest_id",
        ),
    )
    with op.batch_alter_table("release_archives", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_release_archives_approved_by_id"),
            ["approved_by_id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_release_archives_created_at"), ["created_at"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_release_archives_finalized_at"), ["finalized_at"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_release_archives_finalized_by_id"),
            ["finalized_by_id"],
            unique=False,
        )
        batch_op.create_index(
            "ix_release_archives_project_id_finalized_at",
            ["project_id", "finalized_at"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_release_archives_project_id"), ["project_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_release_archives_release_candidate_id"),
            ["release_candidate_id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_release_archives_release_manifest_id"),
            ["release_manifest_id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_release_archives_source_revision"),
            ["source_revision"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("release_archives", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_release_archives_source_revision"))
        batch_op.drop_index(batch_op.f("ix_release_archives_release_manifest_id"))
        batch_op.drop_index(batch_op.f("ix_release_archives_release_candidate_id"))
        batch_op.drop_index(batch_op.f("ix_release_archives_project_id"))
        batch_op.drop_index("ix_release_archives_project_id_finalized_at")
        batch_op.drop_index(batch_op.f("ix_release_archives_finalized_by_id"))
        batch_op.drop_index(batch_op.f("ix_release_archives_finalized_at"))
        batch_op.drop_index(batch_op.f("ix_release_archives_created_at"))
        batch_op.drop_index(batch_op.f("ix_release_archives_approved_by_id"))
    op.drop_table("release_archives")
