"""Persist rendered reports and their traceability records."""

from __future__ import annotations

from pathlib import PurePosixPath

from sqlalchemy import select

from app.agents.base.plugins import ReportProvider
from app.core.enums import EventType, ReportStatus
from app.core.storage import ProjectStorage
from app.models.report import Report
from app.orchestration.events import record_event
from app.orchestration.handlers import TaskContext
from app.reporting.formats import export_format_for
from app.reporting.types import ReportSnapshot


def _validate_rendered_report(
    rendered: object, snapshot: ReportSnapshot, export_format: str
) -> None:
    """Refuse malformed or overclaiming provider output before writing bytes."""

    formatter = export_format_for(export_format)
    filename = getattr(rendered, "filename", "")
    data = getattr(rendered, "data", None)
    media_type = getattr(rendered, "media_type", "")
    finding_ids = tuple(getattr(rendered, "finding_ids", ()))
    if not isinstance(filename, str) or not filename.strip():
        raise ValueError("report provider returned an empty filename")
    if filename != PurePosixPath(filename).name or "\\" in filename or filename in {".", ".."}:
        raise ValueError("report provider returned a path instead of a filename")
    if not filename.casefold().endswith(formatter.file_extension.casefold()):
        raise ValueError(
            f"report filename {filename!r} does not match {export_format!r} extension "
            f"{formatter.file_extension!r}"
        )
    if not isinstance(data, bytes) or not data:
        raise ValueError("report provider returned no artifact bytes")
    if media_type != formatter.media_type:
        raise ValueError(
            f"report provider returned media type {media_type!r}; expected "
            f"{formatter.media_type!r} for {export_format!r}"
        )
    if not set(finding_ids).issubset(set(snapshot.finding_ids)):
        raise ValueError("report provider cited finding ids outside the captured snapshot")


def generate_report(
    context: TaskContext,
    provider: ReportProvider,
    snapshot: ReportSnapshot,
    export_format: str,
) -> tuple[Report, bool]:
    """Render and persist one report, converging on an existing task output."""

    existing = context.db.execute(
        select(Report).where(
            Report.project_id == context.project.id,
            Report.task_id == context.task.id,
            Report.report_type == provider.report_type,
            Report.export_format == export_format,
            Report.status == ReportStatus.GENERATED,
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing, False

    if export_format not in provider.supported_formats:
        raise ValueError(
            f"report provider {provider.report_type!r} does not support {export_format!r}"
        )
    rendered = provider.render(snapshot, export_format)
    _validate_rendered_report(rendered, snapshot, export_format)
    formatter = export_format_for(export_format)
    storage = ProjectStorage(context.project.id, context.settings)
    blob = storage.write_report(rendered.filename, rendered.data)
    summary = {
        **dict(rendered.summary),
        "document_status": rendered.summary.get(
            "document_status", "DRAFT - GENERATED; NOT REPORT-APPROVED"
        ),
        "human_approved": bool(rendered.summary.get("human_approved", False)),
        "validation_level": rendered.summary.get("validation_level", "generated"),
        "source_snapshot_sha256": snapshot.source_digest,
        "canonical_revision": snapshot.revision_number,
        "export_generator_version": formatter.version,
    }
    # A newly rendered report has no report-specific approval record yet. A
    # provider cannot promote it by writing a truthy summary flag.
    summary["human_approved"] = False
    report = Report(
        project_id=context.project.id,
        task_id=context.task.id,
        report_type=provider.report_type,
        export_format=export_format,
        provider_name=provider.display_name,
        provider_version=provider.version,
        title=rendered.filename,
        status=ReportStatus.GENERATED,
        relative_path=str(blob.relative_path),
        media_type=rendered.media_type,
        size_bytes=blob.size_bytes,
        sha256=blob.sha256,
        finding_ids=[str(value) for value in rendered.finding_ids],
        summary=summary,
    )
    try:
        context.db.add(report)
        context.db.flush()
        record_event(
            context.db,
            project_id=context.project.id,
            task_id=context.task.id,
            event_type=EventType.REPORT_CREATED,
            payload={
                "report_id": str(report.id),
                "report_type": report.report_type,
                "export_format": report.export_format,
                "title": report.title,
                "status": report.status.value,
                "provider_name": report.provider_name,
                "document_status": summary["document_status"],
                "validation_level": summary["validation_level"],
                "source_snapshot_sha256": snapshot.source_digest,
            },
        )
    except BaseException:
        storage.delete(blob.relative_path)
        raise
    return report, True
