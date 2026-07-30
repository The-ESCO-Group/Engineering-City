"""Running parsers and persisting what they produce.

The boundary between untrusted content and the database. A parser is handed a
path and settings and nothing else; this module takes its :class:`ParseResult`
and writes the artifact and the evidence anchors.

Re-parsing is idempotent by design. Anchors are keyed by
``(file_id, anchor_key)``, so a second parse of the same bytes updates rows in
place rather than orphaning every existing finding's citations. That matters
because a parser upgrade must not silently invalidate the evidence behind
findings an engineer has already reviewed.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.enums import EventType, FileKind, FileStatus
from app.core.errors import AppError, CapabilityUnavailableError, ParserError
from app.core.logging import get_logger
from app.core.storage import ProjectStorage
from app.models.evidence import EvidenceAnchor
from app.models.file import ParsedArtifact, UploadedFile
from app.orchestration.events import record_event
from app.parsers.base import AnchorDraft, ParseContext, ParseResult
from app.parsers.registry import parser_for

logger = get_logger(__name__)


@dataclass(slots=True)
class ParseOutcome:
    """What a parse attempt produced."""

    file_id: uuid.UUID
    succeeded: bool
    artifact: ParsedArtifact | None = None
    anchor_count: int = 0
    warning_count: int = 0
    #: True when an existing, current-version artifact was reused.
    skipped: bool = False
    error_code: str = ""
    error_message: str = ""


def existing_artifact(db: Session, file_id: uuid.UUID) -> ParsedArtifact | None:
    return db.execute(
        select(ParsedArtifact)
        .where(ParsedArtifact.file_id == file_id)
        .order_by(ParsedArtifact.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()


def is_current(artifact: ParsedArtifact, parser_name: str, parser_version: str) -> bool:
    """Whether an artifact was produced by the parser version now installed."""
    return artifact.parser_name == parser_name and artifact.parser_version == parser_version


def parse_file(
    db: Session,
    record: UploadedFile,
    settings: Settings,
    *,
    force: bool = False,
    task_id: uuid.UUID | None = None,
) -> ParseOutcome:
    """Parse one stored file and persist the artifact and its anchors.

    Records the failure on the file row and returns an unsuccessful outcome
    rather than raising, so one unreadable document does not abandon the rest of
    a project's intake. The reason is always kept and surfaced.
    """
    if record.deleted_at is not None or record.status is FileStatus.DELETED:
        return ParseOutcome(
            file_id=record.id,
            succeeded=False,
            error_code="file_deleted",
            error_message="File was deleted before it could be parsed.",
        )

    if record.file_kind is FileKind.ZIP:
        # The container was expanded at intake; its members are parsed on their
        # own. Reporting this as success with nothing to show would be a lie, and
        # reporting it as a failure would be wrong too.
        record.status = FileStatus.PARSED
        record.status_detail = (
            "Archive container: expanded at intake, its members are parsed individually."
        )
        db.flush()
        return ParseOutcome(file_id=record.id, succeeded=True, skipped=True)

    try:
        parser = parser_for(record.file_kind)
    except AppError as exc:
        return _fail(db, record, exc, task_id=task_id)

    previous = existing_artifact(db, record.id)
    if previous is not None and not force and is_current(previous, parser.name, parser.version):
        return ParseOutcome(
            file_id=record.id,
            succeeded=True,
            artifact=previous,
            anchor_count=_anchor_count(db, record.id),
            warning_count=len(previous.warnings),
            skipped=True,
        )

    storage = ProjectStorage(record.project_id, settings)
    try:
        path = storage.absolute_path(record.relative_path)
    except AppError as exc:
        return _fail(db, record, exc, task_id=task_id)

    record.status = FileStatus.PARSING
    db.flush()
    record_event(
        db,
        project_id=record.project_id,
        event_type=EventType.FILE_PARSING_STARTED,
        task_id=task_id,
        payload=_file_payload(record),
    )

    context = ParseContext(
        file_id=record.id,
        project_id=record.project_id,
        path=path,
        filename=record.original_filename,
        file_kind=record.file_kind,
        document_role=record.document_role,
        settings=settings,
        archive_member_path=record.archive_member_path,
    )

    started = time.perf_counter()
    try:
        result = parser.parse(context)
    except AppError as exc:
        return _fail(db, record, exc, task_id=task_id)
    except Exception as exc:
        logger.exception(
            "parser raised an unexpected error",
            extra={
                "file_id": str(record.id),
                "file_kind": record.file_kind.value,
                "parser": parser.name,
            },
        )
        return _fail(
            db,
            record,
            ParserError(
                f"The {parser.name} parser failed unexpectedly: {type(exc).__name__}: {exc}"
            ),
            task_id=task_id,
        )
    duration_ms = int((time.perf_counter() - started) * 1000)

    artifact = _persist(db, record, result, duration_ms=duration_ms, previous=previous)
    anchor_count = _persist_anchors(db, record, artifact, result.anchors, parser.version)

    record.status = FileStatus.PARSED
    record.status_detail = _status_detail(result)
    db.flush()

    record_event(
        db,
        project_id=record.project_id,
        event_type=EventType.FILE_PARSED,
        task_id=task_id,
        payload={
            **_file_payload(record),
            "warning_count": len(result.warnings),
            "detail": record.status_detail,
        },
    )

    logger.info(
        "file parsed",
        extra={
            "file_id": str(record.id),
            "parser": parser.name,
            "parser_version": parser.version,
            "anchors": anchor_count,
            "warnings": len(result.warnings),
            "unsupported": len(result.unsupported),
            "duration_ms": duration_ms,
        },
    )
    return ParseOutcome(
        file_id=record.id,
        succeeded=True,
        artifact=artifact,
        anchor_count=anchor_count,
        warning_count=len(result.warnings),
    )


def _status_detail(result: ParseResult) -> str:
    """One line summarising the parse, including anything it could not do."""
    parts: list[str] = []
    if result.warnings:
        parts.append(f"{len(result.warnings)} warning(s)")
    if result.unsupported:
        parts.append(f"{len(result.unsupported)} unsupported item(s)")
    if not parts:
        return "Parsed with no warnings."
    return "Parsed with " + ", ".join(parts) + "."


def _file_payload(record: UploadedFile) -> dict[str, object]:
    return {
        "file_id": str(record.id),
        "original_filename": record.original_filename,
        "file_kind": record.file_kind.value,
        "document_role": record.document_role.value,
        "status": record.status.value,
        "size_bytes": record.size_bytes,
        "sha256": record.sha256,
    }


def _fail(
    db: Session,
    record: UploadedFile,
    exc: AppError,
    *,
    task_id: uuid.UUID | None,
) -> ParseOutcome:
    """Record a parse failure on the file and emit the event.

    The reason is written to ``status_detail`` so the UI can explain it. A file
    that could not be read is visible as such rather than merely absent from the
    findings.
    """
    record.status = FileStatus.PARSE_FAILED
    record.status_detail = exc.message
    db.flush()

    record_event(
        db,
        project_id=record.project_id,
        event_type=EventType.FILE_PARSE_FAILED,
        task_id=task_id,
        payload={**_file_payload(record), "detail": exc.message},
    )
    logger.warning(
        "file parse failed",
        extra={
            "file_id": str(record.id),
            "file_kind": record.file_kind.value,
            "error_code": exc.code,
            "detail": exc.message,
        },
    )
    return ParseOutcome(
        file_id=record.id,
        succeeded=False,
        error_code=exc.code,
        error_message=exc.message,
    )


def _persist(
    db: Session,
    record: UploadedFile,
    result: ParseResult,
    *,
    duration_ms: int,
    previous: ParsedArtifact | None,
) -> ParsedArtifact:
    """Write the artifact, replacing a previous one for the same file."""
    if previous is not None:
        # Replace rather than accumulate: two artifacts for one file would leave
        # every consumer guessing which is authoritative.
        db.execute(delete(ParsedArtifact).where(ParsedArtifact.file_id == record.id))
        db.flush()

    artifact = ParsedArtifact(
        project_id=record.project_id,
        file_id=record.id,
        artifact_kind=result.artifact_kind,
        parser_name=result.parser_name,
        parser_version=result.parser_version,
        content=result.content,
        summary=result.summary,
        warnings=[warning.as_dict() for warning in result.warnings],
        unsupported=[item.as_dict() for item in result.unsupported],
        duration_ms=duration_ms,
    )
    db.add(artifact)
    db.flush()
    return artifact


def _persist_anchors(
    db: Session,
    record: UploadedFile,
    artifact: ParsedArtifact,
    drafts: list[AnchorDraft],
    parser_version: str,
) -> int:
    """Upsert anchors by ``(file_id, anchor_key)``.

    Existing rows are updated in place so anchor ids survive a re-parse and the
    findings that cite them keep working. Anchors whose key has disappeared from
    the new parse are deleted — but only after the update pass, so a
    still-present anchor is never briefly absent.

    A duplicate key from a parser is a parser bug: it would make one citation
    ambiguous. It is logged and the first occurrence wins, rather than raising and
    losing the whole parse.
    """
    existing = {
        anchor.anchor_key: anchor
        for anchor in db.execute(
            select(EvidenceAnchor).where(EvidenceAnchor.file_id == record.id)
        ).scalars()
    }

    seen: set[str] = set()
    for draft in drafts:
        if draft.key in seen:
            logger.warning(
                "parser emitted a duplicate anchor key; keeping the first",
                extra={
                    "file_id": str(record.id),
                    "anchor_key": draft.key,
                    "parser_version": parser_version,
                },
            )
            continue
        seen.add(draft.key)

        anchor = existing.get(draft.key)
        if anchor is None:
            anchor = EvidenceAnchor(
                project_id=record.project_id,
                file_id=record.id,
                anchor_key=draft.key,
            )
            db.add(anchor)

        anchor.artifact_id = artifact.id
        anchor.anchor_type = draft.anchor_type
        anchor.evidence_kind = draft.evidence_kind
        anchor.source_label = draft.source_label or record.original_filename
        anchor.display_location = draft.display_location
        anchor.structured_location = draft.structured_location
        anchor.excerpt = draft.excerpt
        anchor.content_hash = draft.content_hash
        anchor.parser_version = parser_version
        anchor.expectation = draft.expectation

    stale = [key for key in existing if key not in seen]
    if stale:
        db.execute(
            delete(EvidenceAnchor).where(
                EvidenceAnchor.file_id == record.id,
                EvidenceAnchor.anchor_key.in_(stale),
            )
        )
        logger.info(
            "removed anchors that the current parse no longer produces",
            extra={"file_id": str(record.id), "removed": len(stale)},
        )

    db.flush()
    return len(seen)


def _anchor_count(db: Session, file_id: uuid.UUID) -> int:
    from sqlalchemy import func

    return int(
        db.execute(
            select(func.count(EvidenceAnchor.id)).where(EvidenceAnchor.file_id == file_id)
        ).scalar()
        or 0
    )


def anchors_by_key(db: Session, file_id: uuid.UUID, keys: list[str]) -> dict[str, EvidenceAnchor]:
    """Resolve parser-assigned keys to anchor rows.

    Agents cite evidence by the key they saw in the normalised content; this is
    how those citations become real anchor ids.
    """
    if not keys:
        return {}
    rows = db.execute(
        select(EvidenceAnchor).where(
            EvidenceAnchor.file_id == file_id,
            EvidenceAnchor.anchor_key.in_(keys),
        )
    ).scalars()
    return {anchor.anchor_key: anchor for anchor in rows}


def parseable_files(db: Session, project_id: uuid.UUID) -> list[UploadedFile]:
    """Files in a project that a parser should be run against.

    Archives are excluded: they were expanded at intake and their members are
    listed separately.
    """
    return list(
        db.execute(
            select(UploadedFile)
            .where(
                UploadedFile.project_id == project_id,
                UploadedFile.deleted_at.is_(None),
                UploadedFile.status != FileStatus.REJECTED,
                UploadedFile.file_kind != FileKind.ZIP,
            )
            .order_by(UploadedFile.created_at)
        ).scalars()
    )


def capability_report() -> list[dict[str, object]]:
    """Per-kind parser availability, for the capabilities endpoint."""
    from app.parsers.registry import parser_capabilities

    return parser_capabilities()


__all__ = [
    "CapabilityUnavailableError",
    "ParseOutcome",
    "anchors_by_key",
    "capability_report",
    "existing_artifact",
    "is_current",
    "parse_file",
    "parseable_files",
]
