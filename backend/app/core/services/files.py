"""File intake.

The pipeline for one upload:

1. sniff the head and validate it (type allowlist, size, executable content);
2. stream it into ``originals/`` and seal it read-only;
3. record an :class:`~app.models.file.UploadedFile` row with its SHA-256;
4. if it is a ZIP, expand it safely and record each member as a file in its own
   right, with provenance back to the archive;
5. audit the upload and emit ``file.uploaded``.

A rejected file does not fail the whole request. Uploading nine valid documents
and one executable must keep the nine, and tell the user precisely why the tenth
was refused.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.clock import utcnow
from app.core.config import Settings
from app.core.enums import AuditAction, DocumentRole, EventType, FileKind, FileStatus
from app.core.errors import (
    AppError,
    ConflictError,
    FileRejectedError,
    NotFoundError,
    PayloadTooLargeError,
)
from app.core.logging import get_logger
from app.core.storage import ProjectStorage
from app.models.file import UploadedFile
from app.models.project import Project
from app.models.user import User
from app.orchestration.events import record_event
from app.security import audit
from app.security.archive import extract_archive
from app.security.file_validation import SNIFF_BYTES, validate_upload
from app.security.xml_safety import guard_xml_upload

logger = get_logger(__name__)


@dataclass(slots=True)
class IngestOutcome:
    """Result of ingesting one candidate file."""

    original_filename: str
    accepted: bool
    file: UploadedFile | None = None
    #: Members recorded from an accepted archive.
    extracted: list[UploadedFile] = field(default_factory=list)
    rejection_code: str = ""
    rejection_reason: str = ""
    warnings: list[str] = field(default_factory=list)


def _check_project_quota(
    db: Session, project: Project, incoming_bytes: int, settings: Settings
) -> None:
    """Refuse an upload that would push the project past its byte quota."""
    used = int(
        db.execute(
            select(func.coalesce(func.sum(UploadedFile.size_bytes), 0)).where(
                UploadedFile.project_id == project.id,
                UploadedFile.deleted_at.is_(None),
            )
        ).scalar()
        or 0
    )
    if used + incoming_bytes > settings.max_project_bytes:
        raise PayloadTooLargeError(
            f"Project storage would exceed the {settings.max_project_bytes:,} byte "
            f"limit ({used:,} bytes already used).",
            details={"used_bytes": used, "limit": settings.max_project_bytes},
        )


def _assert_writable(project: Project) -> None:
    if project.is_archived:
        raise ConflictError("Archived projects are read-only; files cannot be added.")


def ingest_upload(
    db: Session,
    *,
    project: Project,
    filename: str,
    stream: BinaryIO,
    size_bytes: int,
    declared_media_type: str = "",
    declared_role: DocumentRole | None = None,
    uploader: User,
    settings: Settings,
    source_ip: str = "",
    user_agent: str = "",
) -> IngestOutcome:
    """Validate, store and record one uploaded file.

    ``stream`` must be seekable; the head is read for content sniffing and then
    rewound before the body is copied.
    """
    _assert_writable(project)

    try:
        head = stream.read(SNIFF_BYTES)
        stream.seek(0)

        validated = validate_upload(
            filename=filename,
            head=head,
            size_bytes=size_bytes,
            max_bytes=settings.max_upload_bytes,
            declared_media_type=declared_media_type,
        )

        # Reject a DTD-bearing XML at intake so the user is told why now, rather
        # than seeing an opaque parse failure later in the workflow.
        if validated.file_kind in {FileKind.XML, FileKind.L5X}:
            guard_xml_upload(head)

        _check_project_quota(db, project, size_bytes, settings)

        storage = ProjectStorage(project.id, settings)
        blob = storage.store_original(stream, filename, max_bytes=settings.max_upload_bytes)

    except AppError as exc:
        audit.record(
            db,
            AuditAction.FILE_REJECTED,
            actor_user_id=uploader.id,
            project_id=project.id,
            target_type="uploaded_file",
            summary=f"Rejected upload '{filename}': {exc.message}",
            details={"filename": filename, "code": exc.code},
            source_ip=source_ip,
            user_agent=user_agent,
        )
        logger.info(
            "upload rejected",
            extra={
                "project_id": str(project.id),
                "filename": filename,
                "code": exc.code,
            },
        )
        return IngestOutcome(
            original_filename=filename,
            accepted=False,
            rejection_code=exc.code,
            rejection_reason=exc.message,
        )

    record = UploadedFile(
        project_id=project.id,
        uploaded_by_id=uploader.id,
        original_filename=filename,
        stored_filename=blob.filename,
        relative_path=str(blob.relative_path),
        media_type=validated.media_type,
        extension=validated.extension,
        file_kind=validated.file_kind,
        document_role=declared_role or validated.suggested_role,
        role_declared_by_user=declared_role is not None,
        size_bytes=blob.size_bytes,
        sha256=blob.sha256,
        status=FileStatus.UPLOADED,
        status_detail="; ".join(validated.warnings),
    )
    db.add(record)
    db.flush()

    outcome = IngestOutcome(
        original_filename=filename,
        accepted=True,
        file=record,
        warnings=list(validated.warnings),
    )

    audit.record(
        db,
        AuditAction.FILE_UPLOADED,
        actor_user_id=uploader.id,
        project_id=project.id,
        target_type="uploaded_file",
        target_id=record.id,
        summary=f"Uploaded '{filename}' ({blob.size_bytes:,} bytes)",
        details={
            "file_kind": record.file_kind.value,
            "document_role": record.document_role.value,
            "sha256": record.sha256,
            "warnings": validated.warnings,
        },
        source_ip=source_ip,
        user_agent=user_agent,
    )
    _emit_uploaded(db, project, record, uploader)

    if record.file_kind is FileKind.ZIP:
        outcome.extracted = _ingest_archive_members(
            db,
            project=project,
            archive=record,
            uploader=uploader,
            settings=settings,
            outcome=outcome,
            source_ip=source_ip,
            user_agent=user_agent,
        )

    return outcome


def _emit_uploaded(db: Session, project: Project, record: UploadedFile, uploader: User) -> None:
    record_event(
        db,
        project_id=project.id,
        event_type=EventType.FILE_UPLOADED,
        actor_user_id=uploader.id,
        payload={
            "file_id": str(record.id),
            "original_filename": record.original_filename,
            "file_kind": record.file_kind.value,
            "document_role": record.document_role.value,
            "status": record.status.value,
            "size_bytes": record.size_bytes,
            "sha256": record.sha256,
            "detail": record.status_detail,
        },
    )


def _ingest_archive_members(
    db: Session,
    *,
    project: Project,
    archive: UploadedFile,
    uploader: User,
    settings: Settings,
    outcome: IngestOutcome,
    source_ip: str,
    user_agent: str,
    depth: int = 1,
) -> list[UploadedFile]:
    """Expand an accepted archive and record each usable member.

    Nested archives are *not* recursed into beyond ``max_archive_depth``; a
    deeper one is recorded as a stored file with a warning rather than silently
    ignored, so the engineer can see it was not analysed.
    """
    storage = ProjectStorage(project.id, settings)
    archive_path = storage.absolute_path(archive.relative_path)
    scratch = storage.extraction_dir(archive.id)
    members: list[UploadedFile] = []

    try:
        try:
            extraction = extract_archive(archive_path, scratch, settings)
        except FileRejectedError as exc:
            archive.status = FileStatus.REJECTED
            archive.status_detail = exc.message
            db.flush()
            outcome.warnings.append(f"Archive was not expanded: {exc.message}")
            audit.record(
                db,
                AuditAction.FILE_REJECTED,
                actor_user_id=uploader.id,
                project_id=project.id,
                target_type="uploaded_file",
                target_id=archive.id,
                summary=f"Archive '{archive.original_filename}' rejected: {exc.message}",
                details={"code": exc.code},
            )
            return []

        for skipped in extraction.skipped:
            outcome.warnings.append(
                f"Archive member '{skipped['member']}' was not extracted: {skipped['reason']}"
            )

        for member in extraction.members:
            child = _ingest_archive_member(
                db,
                project=project,
                archive=archive,
                member_path=member.absolute_path,
                member_name=str(member.relative_path),
                original_member_name=member.original_name,
                uploader=uploader,
                settings=settings,
                outcome=outcome,
                depth=depth,
                source_ip=source_ip,
                user_agent=user_agent,
            )
            if child is not None:
                members.append(child)
    finally:
        # Unconditional: a rejected or half-ingested archive must not leave
        # unreferenced bytes sitting in the project's derived/ tree, where
        # nothing would ever clean them up or account for them against the quota.
        # Everything worth keeping has already been moved into originals/.
        _remove_tree(scratch)

    archive.status = FileStatus.PARSED
    archive.status_detail = f"Expanded {len(members)} member(s)" + (
        f", skipped {len(extraction.skipped)}" if extraction.skipped else ""
    )
    db.flush()
    return members


def _ingest_archive_member(
    db: Session,
    *,
    project: Project,
    archive: UploadedFile,
    member_path: Path,
    member_name: str,
    original_member_name: str,
    uploader: User,
    settings: Settings,
    outcome: IngestOutcome,
    depth: int,
    source_ip: str,
    user_agent: str,
) -> UploadedFile | None:
    """Validate and record one archive member, or explain why it was skipped."""
    with member_path.open("rb") as handle:
        head = handle.read(SNIFF_BYTES)
    size = member_path.stat().st_size

    try:
        validated = validate_upload(
            filename=Path(member_name).name,
            head=head,
            size_bytes=size,
            max_bytes=settings.max_upload_bytes,
        )
        if validated.file_kind in {FileKind.XML, FileKind.L5X}:
            guard_xml_upload(head)
    except AppError as exc:
        outcome.warnings.append(
            f"Archive member '{original_member_name}' was not analysed: {exc.message}"
        )
        member_path.unlink(missing_ok=True)
        return None

    storage = ProjectStorage(project.id, settings)
    blob = storage.adopt_extracted(member_path, Path(member_name).name)

    detail = "; ".join(validated.warnings)
    if validated.file_kind is FileKind.ZIP and depth >= settings.max_archive_depth:
        detail = (
            f"Nested archive at depth {depth + 1} was stored but not expanded "
            f"(limit is {settings.max_archive_depth})."
        )
        outcome.warnings.append(
            f"Nested archive '{original_member_name}' was stored but not expanded."
        )

    record = UploadedFile(
        project_id=project.id,
        uploaded_by_id=uploader.id,
        original_filename=Path(original_member_name).name,
        stored_filename=blob.filename,
        relative_path=str(blob.relative_path),
        media_type=validated.media_type,
        extension=validated.extension,
        file_kind=validated.file_kind,
        document_role=validated.suggested_role,
        role_declared_by_user=False,
        size_bytes=blob.size_bytes,
        sha256=blob.sha256,
        status=FileStatus.UPLOADED,
        status_detail=detail,
        parent_file_id=archive.id,
        archive_member_path=original_member_name,
    )
    db.add(record)
    db.flush()

    audit.record(
        db,
        AuditAction.FILE_UPLOADED,
        actor_user_id=uploader.id,
        project_id=project.id,
        target_type="uploaded_file",
        target_id=record.id,
        summary=(
            f"Extracted '{original_member_name}' from "
            f"'{archive.original_filename}' ({blob.size_bytes:,} bytes)"
        ),
        details={
            "file_kind": record.file_kind.value,
            "sha256": record.sha256,
            "archive_file_id": str(archive.id),
        },
        source_ip=source_ip,
        user_agent=user_agent,
    )
    _emit_uploaded(db, project, record, uploader)
    return record


def _remove_tree(path: Path) -> None:
    import shutil

    shutil.rmtree(path, ignore_errors=True)


def list_files(
    db: Session,
    project_id: uuid.UUID,
    *,
    include_deleted: bool = False,
    file_kind: FileKind | None = None,
) -> list[UploadedFile]:
    statement = select(UploadedFile).where(UploadedFile.project_id == project_id)
    if not include_deleted:
        statement = statement.where(UploadedFile.deleted_at.is_(None))
    if file_kind is not None:
        statement = statement.where(UploadedFile.file_kind == file_kind)
    return list(db.execute(statement.order_by(UploadedFile.created_at)).scalars())


def get_file(db: Session, file_id: uuid.UUID) -> UploadedFile:
    record = db.get(UploadedFile, file_id)
    if record is None:
        raise NotFoundError("File", file_id)
    return record


def delete_file(
    db: Session,
    record: UploadedFile,
    *,
    actor: User,
    settings: Settings,
    source_ip: str = "",
    user_agent: str = "",
) -> UploadedFile:
    """Soft-delete a file and remove its bytes.

    The row is kept so existing evidence anchors still resolve to *something*
    and can report that their source was deleted. Silently orphaning a citation
    would be worse than showing a tombstone.
    """
    if record.deleted_at is not None:
        return record

    project = db.get(Project, record.project_id)
    if project is not None and project.is_archived:
        raise ConflictError("Archived projects are read-only; files cannot be deleted.")

    storage = ProjectStorage(record.project_id, settings)
    removed = storage.delete(record.relative_path)

    record.status = FileStatus.DELETED
    record.deleted_at = utcnow()
    record.status_detail = "Deleted by user; evidence anchors referencing it are retained."

    # Members of a deleted archive go with it: they only existed because it did.
    for child in db.execute(
        select(UploadedFile).where(
            UploadedFile.parent_file_id == record.id,
            UploadedFile.deleted_at.is_(None),
        )
    ).scalars():
        storage.delete(child.relative_path)
        child.status = FileStatus.DELETED
        child.deleted_at = utcnow()
        child.status_detail = f"Deleted with its containing archive '{record.original_filename}'."

    db.flush()

    audit.record(
        db,
        AuditAction.FILE_DELETED,
        actor_user_id=actor.id,
        project_id=record.project_id,
        target_type="uploaded_file",
        target_id=record.id,
        summary=f"Deleted '{record.original_filename}'",
        details={"blob_removed": removed, "sha256": record.sha256},
        source_ip=source_ip,
        user_agent=user_agent,
    )
    record_event(
        db,
        project_id=record.project_id,
        event_type=EventType.FILE_DELETED,
        actor_user_id=actor.id,
        payload={
            "file_id": str(record.id),
            "original_filename": record.original_filename,
            "file_kind": record.file_kind.value,
            "document_role": record.document_role.value,
            "status": record.status.value,
        },
    )
    return record
