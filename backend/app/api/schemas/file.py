"""Uploaded-file, parsed-artifact and evidence schemas."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import Field

from app.api.schemas.common import ApiModel
from app.core.enums import AnchorType, DocumentRole, EvidenceKind, FileKind, FileStatus


class UploadedFileRead(ApiModel):
    id: uuid.UUID
    project_id: uuid.UUID
    original_filename: str
    file_kind: FileKind
    document_role: DocumentRole
    role_declared_by_user: bool = False
    media_type: str = ""
    extension: str = ""
    size_bytes: int = 0
    sha256: str = ""
    status: FileStatus
    status_detail: str = ""
    uploaded_by_id: uuid.UUID | None = None
    parent_file_id: uuid.UUID | None = Field(
        default=None, description="Set when this file was extracted from an uploaded archive."
    )
    archive_member_path: str = ""
    created_at: datetime
    updated_at: datetime


class UploadResultItem(ApiModel):
    """Per-file outcome of a multi-file upload.

    A rejected file is reported here with the reason rather than failing the
    whole request, so uploading nine good files and one executable does not lose
    the nine.
    """

    accepted: bool
    original_filename: str
    file: UploadedFileRead | None = None
    #: Files extracted from an accepted archive.
    extracted: list[UploadedFileRead] = Field(default_factory=list)
    rejection_code: str = ""
    rejection_reason: str = ""


class UploadResponse(ApiModel):
    project_id: uuid.UUID
    accepted_count: int
    rejected_count: int
    results: list[UploadResultItem]


class ParserWarningRead(ApiModel):
    code: str
    message: str
    location: str = ""
    severity: str = "warning"


class ParsedArtifactRead(ApiModel):
    id: uuid.UUID
    file_id: uuid.UUID
    artifact_kind: str
    parser_name: str
    parser_version: str
    summary: dict[str, Any] = Field(default_factory=dict)
    warnings: list[Any] = Field(default_factory=list)
    unsupported: list[Any] = Field(default_factory=list)
    duration_ms: int = 0
    created_at: datetime


class ParsedArtifactDetail(ParsedArtifactRead):
    """Includes the normalised document. Can be large; fetched on demand only."""

    content: dict[str, Any] = Field(default_factory=dict)


class EvidenceAnchorRead(ApiModel):
    """A citation the UI can render and, where possible, deep-link to."""

    id: uuid.UUID
    file_id: uuid.UUID
    artifact_id: uuid.UUID | None = None
    anchor_type: AnchorType
    evidence_kind: EvidenceKind
    source_label: str
    display_location: str
    structured_location: dict[str, Any] = Field(default_factory=dict)
    excerpt: str
    content_hash: str
    parser_version: str
    expectation: str = Field(
        default="",
        description="On a missing_expected anchor: what was looked for and not found.",
    )


class FileDetail(UploadedFileRead):
    artifacts: list[ParsedArtifactRead] = Field(default_factory=list)
    anchor_count: int = 0
