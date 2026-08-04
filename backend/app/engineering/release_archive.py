"""Finalize approved engineering packages into immutable delivery ZIPs.

The release generator owns the directory and its inner checksum index.  This
module owns the separate outer ZIP that is actually downloaded.  It deliberately
does not reuse ``ReleaseCandidate.package_sha256``: that column authenticates
``00_Manifest/checksums.sha256``, while :class:`ReleaseArchive` authenticates
the complete deterministic ZIP container.

Finalization is a convergence operation.  Bytes are verified and atomically
published before the database row is written.  A process that dies in between
therefore leaves an orphan ZIP which a retry may adopt only when it is byte-for-
byte the deterministic result.  Existing paths are never overwritten.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import tempfile
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import BinaryIO

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.agents.delivery.release_container import (
    ReleaseContainerError,
    ReleaseFileAnchor,
    ReleaseTrustAnchor,
    ReleaseZipArtifact,
    build_release_zip,
)
from app.core.clock import ensure_utc, utcnow
from app.core.config import Settings, get_settings
from app.core.enums import (
    ApprovalGateKey,
    AuditAction,
    GateStatus,
    ObjectApprovalState,
    ReleaseCandidateStatus,
)
from app.core.errors import ConflictError, NotFoundError
from app.core.storage import CHUNK_BYTES, ProjectStorage
from app.engineering import gates
from app.engineering.revisions import current_revision_number
from app.models.project import Project
from app.models.release import ExportArtifact, ReleaseCandidate, ReleaseManifest
from app.models.release_archive import ReleaseArchive
from app.models.revision import ApprovalGate
from app.models.user import User
from app.security import audit
from app.security.paths import resolve_within

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_WRITE_BITS = stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
_ZIP_MEDIA_TYPE = "application/zip"


class ReleaseArchiveRefused(ConflictError):
    """The approved package cannot safely converge to a delivery archive."""

    code = "release_archive_refused"

    def __init__(self, reason: str, message: str, **details: object) -> None:
        super().__init__(
            message,
            code=f"release_archive.{reason}",
            details={"reason": reason, **details},
        )
        self.reason = reason


@dataclass(slots=True)
class VerifiedArchiveDownload:
    """A pre-hashed archive and the exact open handle that was verified.

    The route must stream :meth:`iter_chunks`, rather than reopening the path.
    That keeps the response attached to the inode whose size and digest were
    checked before HTTP headers were sent.
    """

    handle: BinaryIO
    filename: str
    media_type: str
    size_bytes: int
    sha256: str

    def iter_chunks(self, chunk_size: int = CHUNK_BYTES) -> Iterator[bytes]:
        try:
            while chunk := self.handle.read(chunk_size):
                yield chunk
        finally:
            self.close()

    def close(self) -> None:
        if not self.handle.closed:
            self.handle.close()


def get_release_archive(
    db: Session, release_candidate_id: uuid.UUID
) -> ReleaseArchive | None:
    """Return the persisted archive for a candidate, if finalization occurred."""

    return db.execute(
        select(ReleaseArchive).where(
            ReleaseArchive.release_candidate_id == release_candidate_id
        )
    ).scalar_one_or_none()


def trust_anchor_from_candidate(
    candidate: ReleaseCandidate,
    manifest: ReleaseManifest,
    artifacts: tuple[ExportArtifact, ...],
) -> ReleaseTrustAnchor:
    """Translate exact frozen database facts into the delivery verifier's input."""

    approved_by_id = candidate.approved_by_id
    if approved_by_id is None or manifest.frozen_at is None:
        raise ReleaseArchiveRefused(
            "approval_incomplete",
            "The release candidate has no named approver or frozen timestamp.",
            release_candidate_id=str(candidate.id),
        )
    return ReleaseTrustAnchor(
        project_id=candidate.project_id,
        candidate_id=candidate.id,
        manifest_id=manifest.id,
        approved_by_id=approved_by_id,
        frozen_at=ensure_utc(manifest.frozen_at),
        candidate_status=candidate.status,
        approval_state=candidate.approval_state,
        source_revision=candidate.revision_number,
        validation_level=candidate.validation_level,
        import_verification_status=candidate.import_verification_status,
        import_verification_detail=candidate.import_verification_detail,
        manifest_sha256=manifest.content_sha256,
        checksum_file_sha256=manifest.checksum_file_sha256,
        package_bytes=candidate.package_bytes,
        artifact_count=manifest.artifact_count,
        artifacts=tuple(
            ReleaseFileAnchor(
                relative_path=artifact.relative_path,
                size_bytes=artifact.size_bytes,
                sha256=artifact.sha256,
            )
            for artifact in artifacts
        ),
    )


def finalize_release_archive(
    db: Session,
    *,
    candidate: ReleaseCandidate,
    finalized_by: User | None = None,
    settings: Settings | None = None,
) -> ReleaseArchive:
    """Verify, atomically persist, and record a final customer delivery ZIP.

    A repeat after success returns the same row after re-hashing its bytes.  A
    repeat after a crash between filesystem publication and transaction commit
    adopts the existing sibling ZIP only when it exactly matches the newly
    verified deterministic bytes.
    """

    active = settings or get_settings()
    locked = db.execute(
        select(ReleaseCandidate)
        .where(ReleaseCandidate.id == candidate.id)
        .with_for_update()
    ).scalar_one_or_none()
    if locked is None:
        raise NotFoundError("Release candidate", candidate.id)
    candidate = locked

    existing = get_release_archive(db, candidate.id)
    if existing is not None:
        _verify_persisted_archive(existing, active)
        _converge_candidate_status(candidate, existing.finalized_at)
        db.flush()
        return existing

    project = db.execute(
        select(Project).where(Project.id == candidate.project_id).with_for_update()
    ).scalar_one_or_none()
    if project is None:  # pragma: no cover - the candidate FK prevents this
        raise NotFoundError("Project", candidate.project_id)

    manifest = db.execute(
        select(ReleaseManifest).where(
            ReleaseManifest.release_candidate_id == candidate.id
        )
    ).scalar_one_or_none()
    artifacts = tuple(
        db.execute(
            select(ExportArtifact)
            .where(ExportArtifact.release_candidate_id == candidate.id)
            .order_by(ExportArtifact.relative_path)
        ).scalars()
    )

    _assert_release_gate(db, project=project, candidate=candidate)
    approver = _assert_frozen_release(candidate, manifest, artifacts, db=db)
    assert manifest is not None  # narrowed by _assert_frozen_release

    storage = ProjectStorage(candidate.project_id, active)
    package_relative = _package_relative_path(candidate)
    package_root = _inspect_package_path(storage, package_relative)
    anchor = trust_anchor_from_candidate(candidate, manifest, artifacts)
    try:
        built = build_release_zip(
            package_root,
            project_storage_root=storage.root,
            trust_anchor=anchor,
        )
    except ReleaseContainerError as error:
        raise ReleaseArchiveRefused(
            "package_verification_failed",
            "The frozen release package failed delivery-container verification.",
            release_candidate_id=str(candidate.id),
            container_code=error.code.value,
            container_message=str(error),
        ) from error

    expected_name = f"{package_relative.name}.zip"
    if built.filename != expected_name:
        raise ReleaseArchiveRefused(
            "archive_name_mismatch",
            "The verified delivery container produced an unexpected filename.",
            expected=expected_name,
            actual=built.filename,
        )
    # ``package_relative`` is releases/RC-0001/<customer package>.  The outer
    # container is a sibling of RC-0001, never a member of the directory whose
    # contents it authenticates.  Its stored name is candidate-stable while the
    # HTTP filename remains the descriptive name returned by the ZIP builder.
    archive_relative = PurePosixPath("releases") / f"{package_relative.parts[1]}.zip"
    _publish_or_adopt(storage, archive_relative, built)

    finalized_at = utcnow()
    archive = ReleaseArchive(
        project_id=candidate.project_id,
        release_candidate_id=candidate.id,
        release_manifest_id=manifest.id,
        approved_by_id=approver.id,
        approved_by_display_name=approver.display_name.strip(),
        finalized_by_id=finalized_by.id if finalized_by is not None else None,
        relative_path=archive_relative.as_posix(),
        filename=built.filename,
        media_type=built.media_type,
        size_bytes=built.size_bytes,
        sha256=built.sha256,
        file_count=built.file_count,
        source_revision=built.source_revision,
        validation_level=candidate.validation_level,
        import_verification_status=candidate.import_verification_status,
        import_verification_detail=built.import_verification_detail,
        rockwell_import_verified=built.rockwell_import_verified,
        manifest_sha256=built.manifest_sha256,
        checksum_file_sha256=built.checksum_file_sha256,
        frozen_at=ensure_utc(built.frozen_at),
        finalized_at=finalized_at,
    )

    try:
        with db.begin_nested():
            db.add(archive)
            db.flush()
    except IntegrityError:
        # A concurrent finalizer can win the unique candidate/manifest key after
        # both processes verified the same bytes.  Re-read and accept only its
        # exact persisted result; every other integrity error remains a defect.
        concurrent = get_release_archive(db, candidate.id)
        if concurrent is None:
            raise
        _verify_persisted_archive(concurrent, active)
        _converge_candidate_status(candidate, concurrent.finalized_at)
        db.flush()
        return concurrent

    _converge_candidate_status(candidate, finalized_at)
    audit.record(
        db,
        AuditAction.EXPORT_GENERATED,
        actor_user_id=finalized_by.id if finalized_by is not None else None,
        actor_label=(
            finalized_by.display_name if finalized_by is not None else "autonomous release workflow"
        ),
        project_id=candidate.project_id,
        target_type="release_archive",
        target_id=archive.id,
        summary=(
            f"Finalized release candidate {candidate.candidate_number} as immutable "
            f"archive {archive.filename}."
        ),
        details={
            "release_candidate_id": str(candidate.id),
            "release_manifest_id": str(manifest.id),
            "relative_path": archive.relative_path,
            "size_bytes": archive.size_bytes,
            "sha256": archive.sha256,
            "inner_checksum_sha256": candidate.package_sha256,
        },
    )
    db.flush()
    return archive


def open_verified_archive(
    archive: ReleaseArchive, *, settings: Settings | None = None
) -> VerifiedArchiveDownload:
    """Pre-hash an immutable archive and return that same open handle for streaming."""

    active = settings or get_settings()
    if archive.size_bytes < 0 or _SHA256.fullmatch(archive.sha256) is None:
        raise ReleaseArchiveRefused(
            "archive_metadata_invalid",
            "The persisted archive metadata is incomplete or invalid.",
            release_archive_id=str(archive.id),
        )

    storage = ProjectStorage(archive.project_id, active)
    path, path_identity = _inspect_archive_path(storage, archive.relative_path)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ReleaseArchiveRefused(
            "archive_unreadable",
            "The persisted release archive cannot be opened safely.",
            release_archive_id=str(archive.id),
        ) from error

    handle = os.fdopen(descriptor, "rb")
    try:
        before = os.fstat(handle.fileno())
        if _file_identity(before) != path_identity or not stat.S_ISREG(before.st_mode):
            raise ReleaseArchiveRefused(
                "archive_changed",
                "The release archive changed while it was being opened.",
                release_archive_id=str(archive.id),
            )

        digest = hashlib.sha256()
        size = 0
        while chunk := handle.read(CHUNK_BYTES):
            size += len(chunk)
            digest.update(chunk)
        after = os.fstat(handle.fileno())
        try:
            final_path_identity = _file_identity(path.lstat())
        except OSError as error:
            raise ReleaseArchiveRefused(
                "archive_changed",
                "The release archive disappeared while it was being verified.",
                release_archive_id=str(archive.id),
            ) from error

        actual_sha256 = digest.hexdigest()
        if (
            _file_identity(after) != _file_identity(before)
            or final_path_identity != path_identity
            or size != archive.size_bytes
            or actual_sha256 != archive.sha256
        ):
            raise ReleaseArchiveRefused(
                "archive_bytes_mismatch",
                "The persisted release archive no longer matches its immutable record.",
                release_archive_id=str(archive.id),
                expected_size=archive.size_bytes,
                actual_size=size,
                expected_sha256=archive.sha256,
                actual_sha256=actual_sha256,
            )
        handle.seek(0)
        return VerifiedArchiveDownload(
            handle=handle,
            filename=archive.filename,
            media_type=archive.media_type or _ZIP_MEDIA_TYPE,
            size_bytes=archive.size_bytes,
            sha256=archive.sha256,
        )
    except BaseException:
        handle.close()
        raise


def _assert_release_gate(
    db: Session, *, project: Project, candidate: ReleaseCandidate
) -> None:
    blocker = gates.gate_blocking(
        db,
        project=project,
        gate_key=ApprovalGateKey.RELEASE,
    )
    if blocker is not None:
        raise ReleaseArchiveRefused(
            "release_gate_blocked",
            "The final release gate has not been settled.",
            gate_key=blocker.gate_key.value,
            gate_status=blocker.status.value,
            release_candidate_id=str(candidate.id),
        )

    gate = db.execute(
        select(ApprovalGate).where(
            ApprovalGate.project_id == project.id,
            ApprovalGate.gate_key == ApprovalGateKey.RELEASE,
        )
    ).scalar_one_or_none()
    if gate is None:  # pragma: no cover - gate evaluation always creates it
        raise ReleaseArchiveRefused(
            "release_gate_missing",
            "The project has no final release gate record.",
            project_id=str(project.id),
        )

    if not gate.is_enabled:
        if not gate.disabled_reason.strip():
            raise ReleaseArchiveRefused(
                "release_gate_unstated_skip",
                "The final release gate is disabled without a stated reason.",
                project_id=str(project.id),
            )
        return

    head_revision = current_revision_number(db, project.id)
    decider = db.get(User, gate.decided_by_id) if gate.decided_by_id is not None else None
    if (
        gate.status is not GateStatus.PASSED
        or gate.decided_by_id is None
        or decider is None
        or not decider.display_name.strip()
        or gate.decided_at is None
        or gate.decided_revision_number != head_revision
        or not gate.decision_comment.strip()
    ):
        raise ReleaseArchiveRefused(
            "release_gate_incomplete",
            "The enabled final release gate lacks a current, named, commented approval.",
            gate_status=gate.status.value,
            decided_revision_number=gate.decided_revision_number,
            current_revision_number=head_revision,
            has_named_decider=decider is not None and bool(decider.display_name.strip()),
            has_comment=bool(gate.decision_comment.strip()),
        )


def _assert_frozen_release(
    candidate: ReleaseCandidate,
    manifest: ReleaseManifest | None,
    artifacts: tuple[ExportArtifact, ...],
    *,
    db: Session,
) -> User:
    if candidate.status not in {
        ReleaseCandidateStatus.APPROVED,
        ReleaseCandidateStatus.RELEASED,
    } or candidate.approval_state is not ObjectApprovalState.APPROVED:
        raise ReleaseArchiveRefused(
            "candidate_not_approved",
            "Only a human-approved release candidate can be finalized.",
            release_candidate_id=str(candidate.id),
            status=candidate.status.value,
            approval_state=candidate.approval_state.value,
        )
    if manifest is None:
        raise ReleaseArchiveRefused(
            "manifest_missing",
            "The approved release candidate has no frozen manifest.",
            release_candidate_id=str(candidate.id),
        )
    if (
        manifest.project_id != candidate.project_id
        or manifest.release_candidate_id != candidate.id
        or manifest.approval_state is not ObjectApprovalState.APPROVED
        or candidate.deleted_at is not None
        or manifest.deleted_at is not None
        or candidate.approved_at is None
        or manifest.approved_at is None
        or manifest.frozen_at is None
        or not manifest.content_sha256
        or not manifest.checksum_file_sha256
    ):
        raise ReleaseArchiveRefused(
            "manifest_not_frozen",
            "The release manifest is incomplete or does not belong to this candidate.",
            release_candidate_id=str(candidate.id),
        )

    approval_times = {
        ensure_utc(candidate.approved_at),
        ensure_utc(manifest.approved_at),
        ensure_utc(manifest.frozen_at),
    }
    if len(approval_times) != 1:
        raise ReleaseArchiveRefused(
            "approval_timestamp_mismatch",
            "The candidate approval and manifest freeze do not record the same instant.",
            release_candidate_id=str(candidate.id),
        )

    approver_ids = {
        candidate.approved_by_id,
        manifest.approved_by_id,
        manifest.frozen_by_id,
    }
    if None in approver_ids or len(approver_ids) != 1:
        raise ReleaseArchiveRefused(
            "approval_identity_mismatch",
            "The candidate and frozen manifest do not name the same human approver.",
            release_candidate_id=str(candidate.id),
        )
    approver_id = candidate.approved_by_id
    assert approver_id is not None
    approver = db.get(User, approver_id)
    if approver is None or not approver.display_name.strip():
        raise ReleaseArchiveRefused(
            "approver_missing",
            "The frozen release approval no longer resolves to a named user.",
            release_candidate_id=str(candidate.id),
        )

    if not artifacts or manifest.artifact_count != len(artifacts):
        raise ReleaseArchiveRefused(
            "artifact_count_mismatch",
            "The frozen manifest does not cover the exact artifact row set.",
            manifest_artifact_count=manifest.artifact_count,
            database_artifact_count=len(artifacts),
        )
    if any(
        artifact.project_id != candidate.project_id
        or artifact.release_candidate_id != candidate.id
        or artifact.deleted_at is not None
        or artifact.size_bytes < 0
        or _SHA256.fullmatch(artifact.sha256) is None
        for artifact in artifacts
    ):
        raise ReleaseArchiveRefused(
            "artifact_trust_invalid",
            "One or more release artifact rows have invalid scope or checksum metadata.",
            release_candidate_id=str(candidate.id),
        )
    if candidate.package_bytes != sum(artifact.size_bytes for artifact in artifacts):
        raise ReleaseArchiveRefused(
            "package_size_mismatch",
            "Artifact sizes do not add up to the candidate package byte count.",
            candidate_package_bytes=candidate.package_bytes,
            artifact_bytes=sum(artifact.size_bytes for artifact in artifacts),
        )
    if (
        _SHA256.fullmatch(candidate.package_sha256) is None
        or candidate.package_sha256 != manifest.checksum_file_sha256
    ):
        raise ReleaseArchiveRefused(
            "package_checksum_mismatch",
            "The candidate checksum identity differs from its frozen manifest.",
            release_candidate_id=str(candidate.id),
        )
    return approver


def _package_relative_path(candidate: ReleaseCandidate) -> PurePosixPath:
    relative = PurePosixPath(candidate.package_path)
    candidate_directory = f"RC-{candidate.candidate_number:04d}"
    if (
        not candidate.package_path
        or relative.is_absolute()
        or relative.parts[:1] != ("releases",)
        or len(relative.parts) != 3
        or relative.parts[1] != candidate_directory
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise ReleaseArchiveRefused(
            "package_path_invalid",
            "The candidate package is not at its canonical project release path.",
            release_candidate_id=str(candidate.id),
            package_path=candidate.package_path,
        )
    return relative


def _publish_or_adopt(
    storage: ProjectStorage,
    relative_path: PurePosixPath,
    built: ReleaseZipArtifact,
) -> None:
    target = _inspect_publish_path(storage, relative_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() or target.is_symlink():
        _adopt_exact_existing(target, built)
        return

    occupied = storage.total_bytes()
    if occupied + built.size_bytes > storage.settings.max_project_bytes:
        raise ReleaseArchiveRefused(
            "project_quota_exceeded",
            "Persisting the release archive would exceed the project storage quota.",
            occupied_bytes=occupied,
            archive_bytes=built.size_bytes,
            limit_bytes=storage.settings.max_project_bytes,
        )

    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{built.filename}.",
            suffix=".tmp",
            dir=target.parent,
            delete=False,
        ) as sink:
            temporary = Path(sink.name)
            sink.write(built.data)
            sink.flush()
            os.fsync(sink.fileno())

        # A hard-link publish is an atomic create-if-absent on the same volume.
        # Unlike os.replace it cannot overwrite a path planted by another
        # process after the preflight check.
        try:
            os.link(temporary, target)
        except FileExistsError:
            _adopt_exact_existing(target, built)
        except OSError as error:
            if target.exists() or target.is_symlink():
                _adopt_exact_existing(target, built)
            else:
                raise ReleaseArchiveRefused(
                    "archive_publish_failed",
                    "The verified archive could not be atomically published.",
                    relative_path=relative_path.as_posix(),
                ) from error
        else:
            # On Windows chmod marks every hard link read-only, after which the
            # temporary name may no longer be deletable.  Remove that name
            # while it is still writable; the published target retains the
            # inode and all verified bytes.
            temporary.unlink()
            temporary = None
            _fsync_directory(target.parent)
            _seal_read_only(target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _adopt_exact_existing(target: Path, built: ReleaseZipArtifact) -> None:
    try:
        metadata = target.lstat()
    except OSError as error:
        raise ReleaseArchiveRefused(
            "archive_existing_unreadable",
            "An existing archive path cannot be safely inspected.",
            path=str(target),
        ) from error
    if (
        stat.S_ISLNK(metadata.st_mode)
        or bool(getattr(metadata, "st_file_attributes", 0) & _REPARSE_POINT)
        or not stat.S_ISREG(metadata.st_mode)
    ):
        raise ReleaseArchiveRefused(
            "archive_existing_unsafe",
            "The deterministic archive path is occupied by a non-regular entry.",
            path=str(target),
        )
    path_identity = _file_identity(metadata)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(target, flags)
    except OSError as error:
        raise ReleaseArchiveRefused(
            "archive_existing_unreadable",
            "An existing archive path cannot be opened safely.",
            path=str(target),
        ) from error
    digest = hashlib.sha256()
    size = 0
    with os.fdopen(descriptor, "rb") as handle:
        before = os.fstat(handle.fileno())
        if _file_identity(before) != path_identity:
            raise ReleaseArchiveRefused(
                "archive_existing_changed",
                "The existing archive path changed while it was being opened.",
                path=str(target),
            )
        while chunk := handle.read(CHUNK_BYTES):
            size += len(chunk)
            digest.update(chunk)
        after = os.fstat(handle.fileno())
    try:
        final_identity = _file_identity(target.lstat())
    except OSError as error:
        raise ReleaseArchiveRefused(
            "archive_existing_changed",
            "The existing archive path disappeared during verification.",
            path=str(target),
        ) from error
    if (
        _file_identity(after) != path_identity
        or final_identity != path_identity
        or size != built.size_bytes
        or digest.hexdigest() != built.sha256
    ):
        raise ReleaseArchiveRefused(
            "archive_existing_mismatch",
            "An existing archive path differs from the deterministic verified ZIP.",
            path=str(target),
            expected_size=built.size_bytes,
            actual_size=size,
            expected_sha256=built.sha256,
            actual_sha256=digest.hexdigest(),
        )
    _seal_read_only(target)


def _inspect_package_path(
    storage: ProjectStorage, relative_path: PurePosixPath
) -> Path:
    root = storage.root.resolve()
    current = root
    for component in relative_path.parts:
        current = current / component
        try:
            metadata = current.lstat()
        except OSError as error:
            raise ReleaseArchiveRefused(
                "package_missing",
                "The frozen package is missing from canonical project storage.",
                package_path=relative_path.as_posix(),
            ) from error
        if stat.S_ISLNK(metadata.st_mode) or bool(
            getattr(metadata, "st_file_attributes", 0) & _REPARSE_POINT
        ):
            raise ReleaseArchiveRefused(
                "package_path_unsafe",
                "The frozen package path contains a link or reparse point.",
                package_path=relative_path.as_posix(),
            )
        if not stat.S_ISDIR(metadata.st_mode):
            raise ReleaseArchiveRefused(
                "package_path_unsafe",
                "A component of the frozen package path is not a directory.",
                package_path=relative_path.as_posix(),
            )
    if not current.is_dir() or current.resolve() != resolve_within(root, relative_path):
        raise ReleaseArchiveRefused(
            "package_path_unsafe",
            "The frozen package path is not a canonical directory.",
            package_path=relative_path.as_posix(),
        )
    return current


def _inspect_publish_path(
    storage: ProjectStorage, relative_path: PurePosixPath
) -> Path:
    root = storage.root.resolve()
    current = root
    for component in relative_path.parts[:-1]:
        current = current / component
        try:
            metadata = current.lstat()
        except OSError as error:
            raise ReleaseArchiveRefused(
                "archive_parent_missing",
                "The deterministic archive parent is missing from project storage.",
                relative_path=relative_path.as_posix(),
            ) from error
        if (
            stat.S_ISLNK(metadata.st_mode)
            or bool(getattr(metadata, "st_file_attributes", 0) & _REPARSE_POINT)
            or not stat.S_ISDIR(metadata.st_mode)
        ):
            raise ReleaseArchiveRefused(
                "archive_path_unsafe",
                "The deterministic archive parent contains an unsafe filesystem entry.",
                relative_path=relative_path.as_posix(),
            )
    target = current / relative_path.name
    # Resolve only the inspected parent: resolving the final component would
    # follow an attacker-planted symlink before _adopt_exact_existing can reject
    # it by lstat.
    if current.resolve() != resolve_within(root, relative_path.parent):
        raise ReleaseArchiveRefused(
            "archive_path_unsafe",
            "The deterministic archive parent is outside canonical project storage.",
            relative_path=relative_path.as_posix(),
        )
    return target


def _seal_read_only(path: Path) -> None:
    try:
        path.chmod(path.stat().st_mode & ~_WRITE_BITS)
        if path.stat().st_mode & _WRITE_BITS:
            raise OSError("filesystem retained write permission bits")
    except OSError as error:
        raise ReleaseArchiveRefused(
            "archive_seal_failed",
            "The release archive could not be sealed read-only.",
            path=str(path),
        ) from error


def _fsync_directory(directory: Path) -> None:
    try:
        descriptor = os.open(directory, os.O_RDONLY)
    except OSError:
        return  # Windows does not permit opening directories this way.
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _verify_persisted_archive(archive: ReleaseArchive, settings: Settings) -> None:
    download = open_verified_archive(archive, settings=settings)
    download.close()


def _inspect_archive_path(
    storage: ProjectStorage, relative_path: str
) -> tuple[Path, tuple[int, int, int, int, int]]:
    relative = PurePosixPath(relative_path)
    if (
        not relative_path
        or relative.is_absolute()
        or relative.parts[:1] != ("releases",)
        or len(relative.parts) != 2
        or relative.suffix.lower() != ".zip"
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise ReleaseArchiveRefused(
            "archive_path_invalid",
            "The persisted archive path is not a canonical project-relative ZIP path.",
            relative_path=relative_path,
        )

    root = storage.root.resolve()
    unresolved = root
    final_metadata: os.stat_result | None = None
    for index, component in enumerate(relative.parts):
        unresolved = unresolved / component
        try:
            metadata = unresolved.lstat()
        except OSError as error:
            raise ReleaseArchiveRefused(
                "archive_missing",
                "The persisted release archive is missing from project storage.",
                relative_path=relative_path,
            ) from error
        if stat.S_ISLNK(metadata.st_mode) or bool(
            getattr(metadata, "st_file_attributes", 0) & _REPARSE_POINT
        ):
            raise ReleaseArchiveRefused(
                "archive_path_unsafe",
                "The persisted archive path contains a link or reparse point.",
                relative_path=relative_path,
            )
        if index < len(relative.parts) - 1 and not stat.S_ISDIR(metadata.st_mode):
            raise ReleaseArchiveRefused(
                "archive_path_unsafe",
                "A parent of the persisted release archive is not a directory.",
                relative_path=relative_path,
            )
        final_metadata = metadata

    assert final_metadata is not None
    if not stat.S_ISREG(final_metadata.st_mode):
        raise ReleaseArchiveRefused(
            "archive_path_unsafe",
            "The persisted release archive is not a regular file.",
            relative_path=relative_path,
        )
    resolved = resolve_within(root, relative)
    if resolved != unresolved.resolve():
        raise ReleaseArchiveRefused(
            "archive_path_unsafe",
            "The persisted release archive does not resolve to its recorded path.",
            relative_path=relative_path,
        )
    return unresolved, _file_identity(final_metadata)


def _file_identity(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_size,
        metadata.st_mtime_ns,
    )


def _converge_candidate_status(
    candidate: ReleaseCandidate, finalized_at: datetime
) -> None:
    if candidate.status is ReleaseCandidateStatus.APPROVED:
        candidate.status = ReleaseCandidateStatus.RELEASED
    if candidate.released_at is None:
        candidate.released_at = ensure_utc(finalized_at)


__all__ = [
    "ReleaseArchiveRefused",
    "VerifiedArchiveDownload",
    "finalize_release_archive",
    "get_release_archive",
    "open_verified_archive",
    "trust_anchor_from_candidate",
]
