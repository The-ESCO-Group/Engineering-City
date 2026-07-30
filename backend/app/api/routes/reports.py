"""Reports.

Report *content* is produced by GPT-owned providers under ``app.reporting``.
These endpoints are the core-owned record of what was generated and the way to
download it. With no provider installed the list is legitimately empty and
``/api/system/capabilities`` says why, rather than the endpoint pretending the
feature is broken.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Path
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from app.api.deps import AppSettings, CurrentUser, DbSession, ProjectRead, authorise_project
from app.api.schemas.report import ReportRead
from app.core.enums import ReportStatus
from app.core.errors import NotFoundError
from app.core.storage import ProjectStorage
from app.models.report import Report
from app.security import permissions

router = APIRouter(tags=["reports"])


def _read(report: Report) -> ReportRead:
    payload = ReportRead.model_validate(report)
    if report.relative_path and report.status is ReportStatus.GENERATED:
        payload.download_path = f"/api/reports/{report.id}/content"
    return payload


@router.get(
    "/projects/{project_id}/reports",
    response_model=list[ReportRead],
    summary="Reports generated for a project",
)
def list_reports(access: ProjectRead, db: DbSession) -> list[ReportRead]:
    rows = db.execute(
        select(Report)
        .where(Report.project_id == access.project.id)
        .order_by(Report.created_at.desc())
    ).scalars()
    return [_read(row) for row in rows]


@router.get(
    "/reports/{report_id}",
    response_model=ReportRead,
    summary="Report metadata and the findings it covers",
)
def get_report(
    report_id: Annotated[uuid.UUID, Path()], db: DbSession, user: CurrentUser
) -> ReportRead:
    report = db.get(Report, report_id)
    if report is None:
        raise NotFoundError("Report", report_id)
    authorise_project(db, user, report.project_id, permissions.READ_ROLES, "view")
    return _read(report)


@router.get(
    "/reports/{report_id}/content",
    summary="Download a generated report",
    response_class=StreamingResponse,
)
def download_report(
    report_id: Annotated[uuid.UUID, Path()],
    db: DbSession,
    user: CurrentUser,
    settings: AppSettings,
) -> StreamingResponse:
    report = db.get(Report, report_id)
    if report is None:
        raise NotFoundError("Report", report_id)
    authorise_project(db, user, report.project_id, permissions.READ_ROLES, "view")

    if report.status is not ReportStatus.GENERATED or not report.relative_path:
        raise NotFoundError(
            "Report content",
            report_id,
            details={
                "status": report.status.value,
                "reason": report.error_message or "The report was never written.",
            },
        )

    storage = ProjectStorage(report.project_id, settings)
    storage.absolute_path(report.relative_path)

    safe_name = report.title.replace('"', "").replace("\r", "").replace("\n", "") or "report"
    return StreamingResponse(
        storage.iter_chunks(report.relative_path),
        media_type=report.media_type or "application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{safe_name}"',
            "X-Content-Sha256": report.sha256,
        },
    )
