"""Project endpoints."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, status

from app.api.deps import (
    AppSettings,
    CurrentUser,
    DbSession,
    ProjectRead,
    ProjectWrite,
    Provenance,
)
from app.api.schemas.common import Page
from app.api.schemas.project import (
    AddProjectMemberRequest,
    CreateProjectRequest,
    ProjectCounts,
    ProjectDetail,
    ProjectMemberRead,
    UpdateProjectRequest,
)
from app.api.schemas.project import (
    ProjectRead as ProjectReadSchema,
)
from app.core.enums import ProjectStatus, RoleName
from app.core.services import projects as project_service
from app.security import permissions

router = APIRouter(prefix="/projects", tags=["projects"])


def _detail(db: DbSession, access: ProjectRead | ProjectWrite) -> ProjectDetail:
    project = access.project
    detail = ProjectDetail.model_validate(project)
    detail.members = [
        ProjectMemberRead.model_validate(row) for row in project_service.member_rows(db, project.id)
    ]
    detail.counts = ProjectCounts(**project_service.project_counts(db, project.id))
    return detail


@router.post(
    "",
    response_model=ProjectDetail,
    status_code=status.HTTP_201_CREATED,
    summary="Create a project",
)
def create_project(
    payload: CreateProjectRequest,
    user: CurrentUser,
    db: DbSession,
    settings: AppSettings,
    provenance: Provenance,
) -> ProjectDetail:
    permissions.require_global_role(user, RoleName.ENGINEER)
    project = project_service.create_project(
        db,
        name=payload.name,
        description=payload.description,
        client_reference=payload.client_reference,
        attributes=payload.attributes,
        creator=user,
        settings=settings,
        source_ip=provenance.source_ip,
        user_agent=provenance.user_agent,
    )
    db.commit()

    detail = ProjectDetail.model_validate(project)
    detail.members = [
        ProjectMemberRead.model_validate(row) for row in project_service.member_rows(db, project.id)
    ]
    detail.counts = ProjectCounts(**project_service.project_counts(db, project.id))
    return detail


@router.get(
    "",
    response_model=Page[ProjectReadSchema],
    summary="List projects the caller can see",
)
def list_projects(
    user: CurrentUser,
    db: DbSession,
    status_filter: Annotated[ProjectStatus | None, Query(alias="status")] = None,
    include_archived: Annotated[bool, Query()] = True,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[ProjectReadSchema]:
    rows, total = project_service.list_projects(
        db,
        user,
        status=status_filter,
        include_archived=include_archived,
        limit=limit,
        offset=offset,
    )
    return Page[ProjectReadSchema](
        items=[ProjectReadSchema.model_validate(row) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/{project_id}",
    response_model=ProjectDetail,
    summary="Project detail with membership and roll-up counts",
)
def get_project(access: ProjectRead, db: DbSession) -> ProjectDetail:
    return _detail(db, access)


@router.patch(
    "/{project_id}",
    response_model=ProjectDetail,
    summary="Update project metadata",
)
def update_project(
    payload: UpdateProjectRequest,
    access: ProjectWrite,
    db: DbSession,
) -> ProjectDetail:
    """Update editable metadata.

    ``status`` is deliberately not accepted here — it belongs to the workflow
    engine. Use the workflow endpoints to change what a project is doing.
    """
    project_service.update_project(
        db,
        access.project,
        changes=payload.model_dump(exclude_unset=True),
        actor=access.user,
    )
    db.commit()
    return _detail(db, access)


@router.post(
    "/{project_id}/archive",
    response_model=ProjectDetail,
    summary="Archive a project (read-only from then on)",
)
def archive_project(access: ProjectWrite, db: DbSession) -> ProjectDetail:
    project_service.archive_project(db, access.project, actor=access.user)
    db.commit()
    return _detail(db, access)


@router.get(
    "/{project_id}/members",
    response_model=list[ProjectMemberRead],
    summary="List project members",
)
def list_members(access: ProjectRead, db: DbSession) -> list[ProjectMemberRead]:
    return [
        ProjectMemberRead.model_validate(row)
        for row in project_service.member_rows(db, access.project.id)
    ]


@router.post(
    "/{project_id}/members",
    response_model=list[ProjectMemberRead],
    summary="Add a member or change their project role",
)
def add_member(
    payload: AddProjectMemberRequest,
    access: ProjectWrite,
    db: DbSession,
) -> list[ProjectMemberRead]:
    project_service.add_member(
        db,
        access.project,
        user_id=payload.user_id,
        role=payload.role,
        actor=access.user,
    )
    db.commit()
    return [
        ProjectMemberRead.model_validate(row)
        for row in project_service.member_rows(db, access.project.id)
    ]
