"""Deterministic workflow steps.

The mechanical parts of a run: inventory, parsing, building the evidence graph,
and finalising. No model is involved in any of them — they are the same every
time for the same inputs, so making them agent tasks would add nondeterminism
and cost for nothing.

The steps that require judgement (planning, the specialist reviews, senior
review) are agent tasks and live in :mod:`app.agents`.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select

from app.core.enums import EventType, FileKind, FileStatus, TaskKind
from app.core.logging import get_logger
from app.core.services import evidence_graph, parsing
from app.models.approval import Approval
from app.models.file import UploadedFile
from app.models.finding import Finding
from app.orchestration.events import record_event
from app.orchestration.handlers import HandlerResult, TaskContext, register

logger = get_logger(__name__)


@register(TaskKind.INVENTORY_FILES)
def inventory_files(context: TaskContext) -> HandlerResult:
    """Record what was supplied, and name what was expected but is not here.

    Absence is the finding nobody makes. A review over four documents when six
    were expected is a different thing from a complete review, and this is where
    that gets written down.
    """
    files = list(
        context.db.execute(
            select(UploadedFile).where(
                UploadedFile.project_id == context.project.id,
                UploadedFile.deleted_at.is_(None),
            )
        ).scalars()
    )

    by_kind: dict[str, int] = {}
    by_role: dict[str, int] = {}
    rejected: list[dict[str, str]] = []
    for record in files:
        by_kind[record.file_kind.value] = by_kind.get(record.file_kind.value, 0) + 1
        by_role[record.document_role.value] = by_role.get(record.document_role.value, 0) + 1
        if record.status is FileStatus.REJECTED:
            rejected.append({"filename": record.original_filename, "reason": record.status_detail})

    analysable = [
        record
        for record in files
        if record.file_kind is not FileKind.ZIP and record.status is not FileStatus.REJECTED
    ]

    expected = {
        "control_logic": "Studio 5000 L5X export",
        "interlock_schedule": "Interlock schedule",
        "io_list": "I/O list",
        "functional_description": "Functional description",
    }
    missing = [label for role, label in expected.items() if not by_role.get(role)]

    if not analysable:
        return HandlerResult.blocked(
            "No analysable files were supplied. Upload engineering documents and start "
            "the review again.",
            file_count=len(files),
            rejected=rejected,
        )

    return HandlerResult.completed(
        file_count=len(files),
        analysable_count=len(analysable),
        by_kind=by_kind,
        by_document_role=by_role,
        rejected=rejected,
        missing_expected_inputs=missing,
    )


@register(TaskKind.PARSE_FILE)
def parse_one_file(context: TaskContext) -> HandlerResult:
    """Parse a single file.

    One task per file so that an unreadable document costs only itself. A parse
    failure completes the task rather than failing it: the failure is already
    recorded on the file row and carried into the evidence graph as a limitation,
    and failing the task would stall a run over a document that simply cannot be
    read.
    """
    raw_id = context.payload.get("file_id")
    if not raw_id:
        return HandlerResult.failed("bad_payload", "The task carries no file_id.")

    try:
        file_id = uuid.UUID(str(raw_id))
    except ValueError:
        return HandlerResult.failed("bad_payload", f"'{raw_id}' is not a valid file id.")

    record = context.db.get(UploadedFile, file_id)
    if record is None:
        return HandlerResult.completed(
            file_id=str(file_id), skipped=True, reason="The file was deleted before parsing."
        )

    outcome = parsing.parse_file(
        context.db,
        record,
        context.settings,
        force=bool(context.payload.get("force")),
        task_id=context.task.id,
    )

    return HandlerResult.completed(
        file_id=str(file_id),
        filename=record.original_filename,
        succeeded=outcome.succeeded,
        skipped=outcome.skipped,
        anchor_count=outcome.anchor_count,
        warning_count=outcome.warning_count,
        error_code=outcome.error_code,
        error_message=outcome.error_message,
    )


@register(TaskKind.BUILD_EVIDENCE_GRAPH)
def build_evidence_graph(context: TaskContext) -> HandlerResult:
    """Join the parsed artifacts into one cross-file structure.

    The graph is rebuilt from artifacts rather than persisted as its own table:
    it is a pure function of them, and a stale cached graph would be worse than
    no cache. Only the counts and the shape are stored on the task.
    """
    graph = evidence_graph.build_graph(context.db, context.project.id)
    counts = graph.counts()

    if not graph.has_logic and not graph.has_interlock_schedule:
        return HandlerResult.blocked(
            "Neither control logic nor an interlock schedule could be read from the "
            "uploaded files, so there is nothing to compare. "
            + (
                "Files that could not be read: "
                + ", ".join(item["filename"] for item in graph.unreadable_files)
                if graph.unreadable_files
                else "Upload an L5X export and an interlock schedule."
            ),
            counts=counts,
        )

    # Named, not just counted. A document that parsed successfully but yielded
    # nothing readable — a scanned drawing with no text layer — is invisible in
    # ``unreadable_files`` because the parse did not fail. Recording the
    # filenames here is what stops the run implying that document was examined.
    unread = sorted(
        {
            item.filename
            for item in graph.limitations
            if item.code in {"no_text_layer", "low_text_yield", "unsupported_content", "truncated"}
        }
    )

    return HandlerResult.completed(
        counts=counts,
        missing_inputs=graph.missing_inputs,
        unreadable_files=graph.unreadable_files,
        partially_read_files=unread,
        tag_usage=evidence_graph.tag_usage_summary(graph),
        limitation_count=len(graph.limitations),
        limitations=[
            {"filename": item.filename, "code": item.code, "message": item.message}
            for item in graph.limitations[:50]
        ],
    )


@register(TaskKind.FINALIZE_PROJECT)
def finalize_project(context: TaskContext) -> HandlerResult:
    """Complete the run, but only if nothing is still waiting on a human.

    The guard is the point. A project marked complete while a safety-related
    approval is still pending would tell an engineer the review was finished when
    the most consequential part of it had not been read by anyone.
    """
    pending = list(
        context.db.execute(
            select(Approval).where(
                Approval.project_id == context.project.id,
                Approval.status == "pending",
            )
        ).scalars()
    )
    if pending:
        return HandlerResult.awaiting_approval(
            f"{len(pending)} approval(s) are still awaiting an engineering decision.",
            pending_approvals=len(pending),
        )

    findings = list(
        context.db.execute(
            select(Finding).where(Finding.project_id == context.project.id)
        ).scalars()
    )
    unreviewed = [finding for finding in findings if not finding.is_reviewed]
    if unreviewed:
        return HandlerResult.blocked(
            f"{len(unreviewed)} finding(s) have not been through senior review, so the "
            "review cannot be reported as complete.",
            unreviewed=len(unreviewed),
        )

    by_severity: dict[str, int] = {}
    for finding in findings:
        by_severity[finding.severity.value] = by_severity.get(finding.severity.value, 0) + 1

    record_event(
        context.db,
        project_id=context.project.id,
        event_type=EventType.PROJECT_COMPLETED,
        task_id=context.task.id,
        payload={
            "project_id": str(context.project.id),
            "name": context.project.name,
            "status": "completed",
            "finding_count": len(findings),
        },
    )

    return HandlerResult.completed(
        finding_count=len(findings),
        findings_by_severity=by_severity,
        accepted=sum(1 for f in findings if f.status.value == "accepted"),
        rejected=sum(1 for f in findings if f.status.value == "rejected"),
    )
