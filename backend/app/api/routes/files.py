"""File upload, listing, download and deletion."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, File, Form, Path, Query, UploadFile, status
from fastapi.responses import StreamingResponse

from app.api.deps import (
    AppSettings,
    CurrentUser,
    DbSession,
    ProjectRead,
    ProjectWrite,
    Provenance,
    authorise_project,
)
from app.api.schemas.common import OkResponse
from app.api.schemas.file import (
    FileDetail,
    ParsedArtifactRead,
    UploadedFileRead,
    UploadResponse,
    UploadResultItem,
)
from app.core.enums import DocumentRole, FileKind
from app.core.errors import NotFoundError, ValidationError
from app.core.services import files as file_service
from app.core.storage import ProjectStorage
from app.models.evidence import EvidenceAnchor
from app.security import permissions

router = APIRouter(tags=["files"])

#: Guard against a single request that would take the process down before any
#: per-file limit could apply.
MAX_FILES_PER_REQUEST = 50


@router.post(
    "/projects/{project_id}/files",
    response_model=UploadResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Upload one or more files",
)
async def upload_files(
    access: ProjectWrite,
    db: DbSession,
    settings: AppSettings,
    provenance: Provenance,
    files: Annotated[list[UploadFile], File(description="One or more engineering documents.")],
    document_role: Annotated[
        DocumentRole | None,
        Form(description="Declared engineering purpose, applied to every file in this request."),
    ] = None,
) -> UploadResponse:
    """Validate and store uploaded files.

    Each file is accepted or rejected independently: one refused file does not
    discard the rest, and every rejection comes back with a reason the uploader
    can act on. A ZIP is expanded safely and each usable member is recorded as a
    file in its own right.
    """
    if not files:
        raise ValidationError("No files were supplied.")
    if len(files) > MAX_FILES_PER_REQUEST:
        raise ValidationError(
            f"At most {MAX_FILES_PER_REQUEST} files may be uploaded per request; "
            f"{len(files)} were supplied."
        )

    results: list[UploadResultItem] = []
    accepted = 0

    for upload in files:
        filename = upload.filename or "unnamed"
        # ``UploadFile.size`` is populated by Starlette from what it actually
        # buffered, so it is a real byte count rather than a client claim.
        size = upload.size if upload.size is not None else 0

        outcome = file_service.ingest_upload(
            db,
            project=access.project,
            filename=filename,
            stream=upload.file,
            size_bytes=size,
            declared_media_type=upload.content_type or "",
            declared_role=document_role,
            uploader=access.user,
            settings=settings,
            source_ip=provenance.source_ip,
            user_agent=provenance.user_agent,
        )
        await upload.close()

        if outcome.accepted:
            accepted += 1

        results.append(
            UploadResultItem(
                accepted=outcome.accepted,
                original_filename=outcome.original_filename,
                file=(
                    UploadedFileRead.model_validate(outcome.file)
                    if outcome.file is not None
                    else None
                ),
                extracted=[UploadedFileRead.model_validate(child) for child in outcome.extracted],
                rejection_code=outcome.rejection_code,
                rejection_reason=(
                    outcome.rejection_reason
                    if not outcome.accepted
                    else "; ".join(outcome.warnings)
                ),
            )
        )

    db.commit()
    return UploadResponse(
        project_id=access.project.id,
        accepted_count=accepted,
        rejected_count=len(results) - accepted,
        results=results,
    )


@router.get(
    "/projects/{project_id}/files",
    response_model=list[UploadedFileRead],
    summary="List a project's files",
)
def list_files(
    access: ProjectRead,
    db: DbSession,
    include_deleted: Annotated[bool, Query()] = False,
    file_kind: Annotated[FileKind | None, Query()] = None,
) -> list[UploadedFileRead]:
    rows = file_service.list_files(
        db,
        access.project.id,
        include_deleted=include_deleted,
        file_kind=file_kind,
    )
    return [UploadedFileRead.model_validate(row) for row in rows]


@router.get(
    "/files/{file_id}",
    response_model=FileDetail,
    summary="File metadata, parse artifacts and anchor count",
)
def get_file(
    file_id: Annotated[uuid.UUID, Path()],
    db: DbSession,
    user: CurrentUser,
) -> FileDetail:
    record = file_service.get_file(db, file_id)
    authorise_project(db, user, record.project_id, permissions.READ_ROLES, "view")

    from sqlalchemy import func, select

    anchor_count = int(
        db.execute(
            select(func.count(EvidenceAnchor.id)).where(EvidenceAnchor.file_id == record.id)
        ).scalar()
        or 0
    )

    detail = FileDetail.model_validate(record)
    detail.artifacts = [
        ParsedArtifactRead.model_validate(artifact) for artifact in record.artifacts
    ]
    detail.anchor_count = anchor_count
    return detail


@router.get(
    "/files/{file_id}/content",
    summary="Download the stored original bytes",
    response_class=StreamingResponse,
)
def download_file(
    file_id: Annotated[uuid.UUID, Path()],
    db: DbSession,
    user: CurrentUser,
    settings: AppSettings,
) -> StreamingResponse:
    """Stream a stored original.

    ``Content-Disposition: attachment`` plus the ``nosniff`` header applied to
    every response means an uploaded document is never rendered as active
    content by a browser.
    """
    record = file_service.get_file(db, file_id)
    authorise_project(db, user, record.project_id, permissions.READ_ROLES, "view")

    if record.deleted_at is not None:
        raise NotFoundError("File content", file_id, details={"reason": "file was deleted"})

    storage = ProjectStorage(record.project_id, settings)
    # Resolve first so a missing blob is a 404 rather than a mid-stream failure.
    storage.absolute_path(record.relative_path)

    safe_name = record.original_filename.replace('"', "").replace("\r", "").replace("\n", "")
    return StreamingResponse(
        storage.iter_chunks(record.relative_path),
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{safe_name}"',
            "Content-Length": str(record.size_bytes),
            "X-Content-Sha256": record.sha256,
        },
    )


@router.delete(
    "/files/{file_id}",
    response_model=OkResponse,
    summary="Delete a file and remove its stored bytes",
)
def delete_file(
    file_id: Annotated[uuid.UUID, Path()],
    db: DbSession,
    user: CurrentUser,
    settings: AppSettings,
    provenance: Provenance,
) -> OkResponse:
    """Delete a file.

    The bytes are removed but the row is retained, so evidence anchors that
    cited it resolve to a tombstone rather than silently disappearing.
    """
    record = file_service.get_file(db, file_id)
    authorise_project(db, user, record.project_id, permissions.WRITE_ROLES, "delete files")

    file_service.delete_file(
        db,
        record,
        actor=user,
        settings=settings,
        source_ip=provenance.source_ip,
        user_agent=provenance.user_agent,
    )
    db.commit()
    return OkResponse(
        message=(
            "File deleted. Evidence anchors that cite it are retained and will "
            "report that their source was removed."
        )
    )
