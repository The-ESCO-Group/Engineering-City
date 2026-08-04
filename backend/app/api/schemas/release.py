"""HTTP representations for engineering release candidates and archives."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import Field

from app.api.schemas.common import ApiModel
from app.core.enums import (
    ExportArtifactKind,
    ImportVerificationStatus,
    ObjectApprovalState,
    ReleaseCandidateStatus,
    ValidationLevel,
)


class ReleaseBlockRead(ApiModel):
    code: str
    summary: str
    detail: str


class ReleaseReadinessRead(ApiModel):
    revision_number: int
    is_ready: bool
    blocking: list[ReleaseBlockRead] = Field(default_factory=list)
    outstanding_gates: list[str] = Field(default_factory=list)
    validation_error_count: int = 0
    highest_attainable_validation_level: ValidationLevel
    import_verification_possible: bool
    import_verification_detail: str = ""


class CreateReleaseCandidateRequest(ApiModel):
    export_profile_id: uuid.UUID | None = None
    force: bool = Field(
        default=False,
        description="Record explicit permission to continue if this candidate later becomes stale.",
    )


class FinalizeReleaseRequest(ApiModel):
    confirm: Literal[True] = Field(
        description="Explicit acknowledgement that finalization creates immutable delivery bytes."
    )


class ReleaseActionRead(ApiModel):
    available: bool
    path: str
    reason: str = ""


class ReleaseActionsRead(ApiModel):
    generate: ReleaseActionRead
    finalize: ReleaseActionRead
    download: ReleaseActionRead


class ExportArtifactRead(ApiModel):
    id: uuid.UUID
    release_candidate_id: uuid.UUID
    kind: ExportArtifactKind
    relative_path: str
    filename: str
    media_type: str = ""
    size_bytes: int
    sha256: str
    validation_level: ValidationLevel
    generator_name: str = ""
    generator_version: str = ""
    import_order: int = 0
    is_produced_by_plugin: bool = False
    plugin_name: str = ""
    warnings: list[Any] = Field(default_factory=list)
    created_at: datetime


class ReleaseManifestRead(ApiModel):
    id: uuid.UUID
    release_candidate_id: uuid.UUID
    manifest_version: str
    content_sha256: str
    artifact_count: int
    checksum_file_sha256: str
    evidence_index_sha256: str
    frozen_at: datetime | None = None
    frozen_by_id: uuid.UUID | None = None
    approved_by_id: uuid.UUID | None = None
    approval_summary: dict[str, Any] = Field(default_factory=dict)


class ReleaseArchiveRead(ApiModel):
    id: uuid.UUID
    project_id: uuid.UUID
    release_candidate_id: uuid.UUID
    release_manifest_id: uuid.UUID
    approved_by_id: uuid.UUID | None = None
    approved_by_display_name: str
    finalized_by_id: uuid.UUID | None = None
    relative_path: str
    filename: str
    media_type: str
    size_bytes: int
    sha256: Annotated[str, Field(min_length=64, max_length=64)]
    file_count: int
    source_revision: int
    validation_level: ValidationLevel
    import_verification_status: ImportVerificationStatus
    import_verification_detail: str = ""
    rockwell_import_verified: bool
    manifest_sha256: Annotated[str, Field(min_length=64, max_length=64)]
    checksum_file_sha256: Annotated[str, Field(min_length=64, max_length=64)]
    frozen_at: datetime
    finalized_at: datetime
    download_path: str = ""


class ReleaseCandidateRead(ApiModel):
    id: uuid.UUID
    project_id: uuid.UUID
    revision_number: int
    candidate_number: int
    name: str
    status: ReleaseCandidateStatus
    approval_state: ObjectApprovalState
    export_profile_id: uuid.UUID | None = None
    validation_run_id: uuid.UUID | None = None
    validation_level: ValidationLevel
    import_verification_status: ImportVerificationStatus
    import_verification_detail: str = ""
    generator_version: str = ""
    package_path: str = ""
    package_sha256: str = ""
    package_bytes: int = 0
    limitations: list[Any] = Field(default_factory=list)
    unsupported_items: list[Any] = Field(default_factory=list)
    approved_by_id: uuid.UUID | None = None
    approved_at: datetime | None = None
    released_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    artifact_count: int = 0
    manifest: ReleaseManifestRead | None = None
    archive: ReleaseArchiveRead | None = None
    actions: ReleaseActionsRead


__all__ = [
    "CreateReleaseCandidateRequest",
    "ExportArtifactRead",
    "FinalizeReleaseRequest",
    "ReleaseActionRead",
    "ReleaseActionsRead",
    "ReleaseArchiveRead",
    "ReleaseBlockRead",
    "ReleaseCandidateRead",
    "ReleaseManifestRead",
    "ReleaseReadinessRead",
]
