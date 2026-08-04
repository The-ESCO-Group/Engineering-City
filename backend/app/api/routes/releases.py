"""Authenticated HTTP surface for engineering release delivery."""

from __future__ import annotations

import uuid
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Path, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select

from app.api.deps import (
    AppSettings,
    CurrentUser,
    DbSession,
    ProjectRead,
    ProjectWrite,
    authorise_project,
)
from app.api.schemas.common import Page
from app.api.schemas.release import (
    CreateReleaseCandidateRequest,
    ExportArtifactRead,
    FinalizeReleaseRequest,
    ReleaseActionRead,
    ReleaseActionsRead,
    ReleaseArchiveRead,
    ReleaseCandidateRead,
    ReleaseManifestRead,
    ReleaseReadinessRead,
)
from app.core.enums import (
    ProjectRole,
    ReleaseCandidateStatus,
    RoleName,
)
from app.core.errors import NotFoundError, PermissionDeniedError
from app.engineering import release as release_service
from app.engineering.release_archive import (
    finalize_release_archive,
    get_release_archive,
    open_verified_archive,
)
from app.models.release import ExportArtifact, ExportProfile, ReleaseCandidate
from app.models.release_archive import ReleaseArchive
from app.models.user import User
from app.security import permissions
from app.security.paths import safe_filename

router = APIRouter(tags=["releases"])

_GENERATABLE = frozenset(
    {
        ReleaseCandidateStatus.DRAFT,
        ReleaseCandidateStatus.GENERATING,
        ReleaseCandidateStatus.GENERATED,
        ReleaseCandidateStatus.VALIDATION_FAILED,
    }
)
_FINALIZABLE = frozenset(
    {ReleaseCandidateStatus.APPROVED, ReleaseCandidateStatus.RELEASED}
)


def _archive_read(archive: ReleaseArchive) -> ReleaseArchiveRead:
    payload = ReleaseArchiveRead.model_validate(archive)
    payload.download_path = f"/api/release-candidates/{archive.release_candidate_id}/download"
    return payload


def _content_disposition(filename: str) -> str:
    safe = safe_filename(filename, fallback="release.zip")
    ascii_name = safe_filename(
        safe.encode("ascii", "ignore").decode("ascii"),
        fallback="release.zip",
    )
    return (
        f'attachment; filename="{ascii_name}"; '
        f"filename*=UTF-8''{quote(safe, safe='')}"
    )


def _candidate_read(
    candidate: ReleaseCandidate, archive: ReleaseArchive | None
) -> ReleaseCandidateRead:
    candidate_id = candidate.id
    manifest = (
        ReleaseManifestRead.model_validate(candidate.manifest)
        if candidate.manifest is not None
        else None
    )
    can_generate = candidate.status in _GENERATABLE and candidate.manifest is None
    can_finalize = candidate.status in _FINALIZABLE and candidate.manifest is not None
    actions = ReleaseActionsRead(
        generate=ReleaseActionRead(
            available=can_generate,
            path=f"/api/release-candidates/{candidate_id}/artifacts",
            reason=(
                ""
                if can_generate
                else "This candidate is immutable or is not in a generatable state."
            ),
        ),
        finalize=ReleaseActionRead(
            available=can_finalize and archive is None,
            path=f"/api/release-candidates/{candidate_id}/finalize",
            reason=(
                "The deterministic delivery archive is already finalized."
                if archive is not None
                else (
                    ""
                    if can_finalize
                    else "A human-approved candidate with a frozen manifest is required."
                )
            ),
        ),
        download=ReleaseActionRead(
            available=archive is not None,
            path=f"/api/release-candidates/{candidate_id}/download",
            reason="" if archive is not None else "No immutable delivery archive exists yet.",
        ),
    )
    return ReleaseCandidateRead(
        id=candidate.id,
        project_id=candidate.project_id,
        revision_number=candidate.revision_number,
        candidate_number=candidate.candidate_number,
        name=candidate.name,
        status=candidate.status,
        approval_state=candidate.approval_state,
        export_profile_id=candidate.export_profile_id,
        validation_run_id=candidate.validation_run_id,
        validation_level=candidate.validation_level,
        import_verification_status=candidate.import_verification_status,
        import_verification_detail=candidate.import_verification_detail,
        generator_version=candidate.generator_version,
        package_path=candidate.package_path,
        package_sha256=candidate.package_sha256,
        package_bytes=candidate.package_bytes,
        limitations=list(candidate.limitations),
        unsupported_items=list(candidate.unsupported_items),
        approved_by_id=candidate.approved_by_id,
        approved_at=candidate.approved_at,
        released_at=candidate.released_at,
        created_at=candidate.created_at,
        updated_at=candidate.updated_at,
        artifact_count=len(candidate.artifacts),
        manifest=manifest,
        archive=_archive_read(archive) if archive is not None else None,
        actions=actions,
    )


def _load_candidate(
    db: DbSession,
    user: User,
    candidate_id: uuid.UUID,
    *,
    roles: frozenset[ProjectRole],
    operation: str,
) -> tuple[ReleaseCandidate, ProjectRole]:
    candidate = db.get(ReleaseCandidate, candidate_id)
    if candidate is None:
        raise NotFoundError("Release candidate", candidate_id)
    access = authorise_project(db, user, candidate.project_id, roles, operation)
    return candidate, access.role


@router.get(
    "/projects/{project_id}/release-readiness",
    response_model=ReleaseReadinessRead,
    summary="Explain whether the current project revision can produce a release candidate",
)
def release_readiness(
    access: ProjectRead, db: DbSession, settings: AppSettings
) -> ReleaseReadinessRead:
    readiness = release_service.release_readiness(
        db,
        project=access.project,
        settings=settings,
    )
    # Gate evaluation is a durable projection of current model state.
    db.commit()
    return ReleaseReadinessRead(**readiness.as_document())


@router.post(
    "/projects/{project_id}/release-candidates",
    response_model=ReleaseCandidateRead,
    status_code=status.HTTP_201_CREATED,
    summary="Reserve a release candidate at the current project revision",
)
def create_release_candidate(
    payload: CreateReleaseCandidateRequest,
    access: ProjectWrite,
    db: DbSession,
    settings: AppSettings,
) -> ReleaseCandidateRead:
    permissions.require_global_role(access.user, RoleName.ENGINEER)
    profile: ExportProfile | None = None
    if payload.export_profile_id is not None:
        profile = db.get(ExportProfile, payload.export_profile_id)
        if (
            profile is None
            or profile.project_id != access.project.id
            or profile.deleted_at is not None
        ):
            raise NotFoundError("Export profile", payload.export_profile_id)

    candidate = release_service.create_release_candidate(
        db,
        project=access.project,
        export_profile=profile,
        requested_by=access.user,
        force=payload.force,
        settings=settings,
    )
    db.commit()
    return _candidate_read(candidate, None)


@router.get(
    "/projects/{project_id}/release-candidates",
    response_model=Page[ReleaseCandidateRead],
    summary="List release candidates for a project",
)
def list_release_candidates(
    access: ProjectRead,
    db: DbSession,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[ReleaseCandidateRead]:
    statement = select(ReleaseCandidate).where(
        ReleaseCandidate.project_id == access.project.id,
        ReleaseCandidate.deleted_at.is_(None),
    )
    total = int(db.execute(select(func.count()).select_from(statement.subquery())).scalar() or 0)
    candidates = list(
        db.execute(
            statement.order_by(ReleaseCandidate.candidate_number.desc())
            .limit(limit)
            .offset(offset)
        ).scalars()
    )
    candidate_ids = [candidate.id for candidate in candidates]
    archives = (
        {
            row.release_candidate_id: row
            for row in db.execute(
                select(ReleaseArchive).where(
                    ReleaseArchive.release_candidate_id.in_(candidate_ids)
                )
            ).scalars()
        }
        if candidate_ids
        else {}
    )
    return Page[ReleaseCandidateRead](
        items=[_candidate_read(row, archives.get(row.id)) for row in candidates],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/release-candidates/{candidate_id}",
    response_model=ReleaseCandidateRead,
    summary="Read one release candidate and its available actions",
)
def get_release_candidate(
    candidate_id: Annotated[uuid.UUID, Path()],
    db: DbSession,
    user: CurrentUser,
) -> ReleaseCandidateRead:
    candidate, _role = _load_candidate(
        db,
        user,
        candidate_id,
        roles=permissions.READ_ROLES,
        operation="view release candidates",
    )
    return _candidate_read(candidate, get_release_archive(db, candidate.id))


@router.post(
    "/release-candidates/{candidate_id}/artifacts",
    response_model=ReleaseCandidateRead,
    summary="Generate or regenerate the candidate's verified package artifacts",
)
def generate_release_artifacts(
    candidate_id: Annotated[uuid.UUID, Path()],
    db: DbSession,
    user: CurrentUser,
    settings: AppSettings,
) -> ReleaseCandidateRead:
    candidate, _role = _load_candidate(
        db,
        user,
        candidate_id,
        roles=permissions.WRITE_ROLES,
        operation="generate release artifacts",
    )
    permissions.require_global_role(user, RoleName.ENGINEER)
    candidate = release_service.generate_artifacts(
        db,
        candidate=candidate,
        settings=settings,
    )
    db.commit()
    return _candidate_read(candidate, get_release_archive(db, candidate.id))


@router.get(
    "/release-candidates/{candidate_id}/artifacts",
    response_model=Page[ExportArtifactRead],
    summary="List the exact artifact rows in a release candidate",
)
def list_release_artifacts(
    candidate_id: Annotated[uuid.UUID, Path()],
    db: DbSession,
    user: CurrentUser,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[ExportArtifactRead]:
    candidate, _role = _load_candidate(
        db,
        user,
        candidate_id,
        roles=permissions.READ_ROLES,
        operation="view release artifacts",
    )
    statement = select(ExportArtifact).where(
        ExportArtifact.release_candidate_id == candidate.id
    )
    total = int(db.execute(select(func.count()).select_from(statement.subquery())).scalar() or 0)
    rows = db.execute(
        statement.order_by(ExportArtifact.import_order, ExportArtifact.relative_path)
        .limit(limit)
        .offset(offset)
    ).scalars()
    return Page[ExportArtifactRead](
        items=[ExportArtifactRead.model_validate(row) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post(
    "/release-candidates/{candidate_id}/finalize",
    response_model=ReleaseArchiveRead,
    summary="Finalize an approved candidate into an immutable deterministic ZIP",
)
def finalize_release_candidate(
    candidate_id: Annotated[uuid.UUID, Path()],
    payload: FinalizeReleaseRequest,
    db: DbSession,
    user: CurrentUser,
    settings: AppSettings,
) -> ReleaseArchiveRead:
    candidate, role = _load_candidate(
        db,
        user,
        candidate_id,
        roles=permissions.REVIEW_ROLES,
        operation="finalize release delivery",
    )
    if not permissions.can_decide_approval(user, role):
        raise PermissionDeniedError(
            "Finalizing customer delivery requires the reviewer role and project review access."
        )
    _ = payload.confirm  # Literal[True] makes omission or false a request validation error.
    archive = finalize_release_archive(
        db,
        candidate=candidate,
        finalized_by=user,
        settings=settings,
    )
    db.commit()
    return _archive_read(archive)


@router.get(
    "/release-candidates/{candidate_id}/download",
    response_class=StreamingResponse,
    summary="Download a pre-verified immutable release ZIP",
)
def download_release_candidate(
    candidate_id: Annotated[uuid.UUID, Path()],
    db: DbSession,
    user: CurrentUser,
    settings: AppSettings,
) -> StreamingResponse:
    candidate, _role = _load_candidate(
        db,
        user,
        candidate_id,
        roles=permissions.READ_ROLES,
        operation="download release delivery",
    )
    archive = get_release_archive(db, candidate.id)
    if archive is None:
        raise NotFoundError("Release archive", candidate.id)

    download = open_verified_archive(archive, settings=settings)
    try:
        return StreamingResponse(
            download.iter_chunks(),
            media_type=download.media_type,
            headers={
                "Content-Disposition": _content_disposition(download.filename),
                "Content-Length": str(download.size_bytes),
                "X-Content-Sha256": download.sha256,
            },
        )
    except BaseException:
        download.close()
        raise


__all__ = ["router"]
