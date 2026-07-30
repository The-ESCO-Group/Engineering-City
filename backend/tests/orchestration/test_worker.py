"""The worker loop and the deterministic steps, driven through the real queue.

Nothing here waits on a background thread: ``settings.worker_enabled`` is False in
tests, so every test claims and runs tasks itself via :func:`worker.tick` or
:func:`worker.drain`. That is what makes the assertions about ordering, retries
and restart recovery mean something — a test that slept and hoped would prove
only that the machine was fast enough that day.

Time is exercised by writing the column the queue reads (``lease_expires_at``) or
by overriding the setting that controls it, never by sleeping.

The agent task kinds run on the deterministic MockProvider, so a drained run
goes all the way to the approval gate. Where a test needs a task with no handler
at all it registers a throwaway kind rather than relying on
explanation, and that is asserted here as the correct present behaviour: the
alternative — failing mysteriously — is what the block exists to prevent.
"""

from __future__ import annotations

import io
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

import app.orchestration.steps  # noqa: F401  (importing registers the handlers)
from app.core.clock import utcnow
from app.core.config import Settings
from app.core.enums import (
    ApprovalKind,
    DocumentRole,
    EventType,
    FileKind,
    FileStatus,
    FindingSeverity,
    FindingStatus,
    TaskKind,
    TaskStatus,
)
from app.core.services import files as file_service
from app.core.services import parsing
from app.models.approval import Approval
from app.models.event import Event
from app.models.evidence import EvidenceAnchor
from app.models.file import ParsedArtifact, UploadedFile
from app.models.finding import Finding
from app.models.project import Project
from app.models.task import Task
from app.models.user import User
from app.orchestration import engine as workflow_engine
from app.orchestration import handlers, worker
from app.orchestration.handlers import HandlerResult, TaskContext
from app.orchestration.queue import DatabaseTaskQueue, EnqueueRequest

WORKER_ID = "test-worker"

#: A task kind the engine never plans, so a throwaway handler for it cannot
#: collide with a real one or perturb a real run.
SPARE_KIND = TaskKind.INTAKE


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
@contextmanager
def installed_handler(
    kind: TaskKind, handler: Callable[[TaskContext], HandlerResult]
) -> Iterator[None]:
    """Swap in a handler for the duration of one test.

    Restores whatever was there before rather than just removing the entry.
    Popping unconditionally destroyed the *real* handler registered at import for
    any kind that has one, so every later test in the session saw that step as
    uninstalled — which is why these tests passed alone and failed in the suite.
    """
    previous = handlers._HANDLERS.pop(kind, None)
    handlers.register(kind)(handler)
    try:
        yield
    finally:
        handlers._HANDLERS.pop(kind, None)
        if previous is not None:
            handlers._HANDLERS[kind] = previous


def start_run(db: Session, project: Project, settings: Settings, actor: User) -> uuid.UUID:
    summary = workflow_engine.start_run(db, project, settings, actor=actor)
    db.commit()
    return summary.run_id


def enqueue(
    db: Session,
    project: Project,
    settings: Settings,
    *,
    kind: TaskKind,
    key: str,
    payload: dict[str, object] | None = None,
    priority: int = 100,
    max_attempts: int = 3,
) -> Task:
    """Put one task on the queue and commit, as the engine would."""
    queue = DatabaseTaskQueue(db, settings)
    task = queue.enqueue(
        project.id,
        EnqueueRequest(
            kind=kind,
            title=f"Test task: {kind.value}",
            idempotency_key=key,
            payload=payload,
            priority=priority,
            max_attempts=max_attempts,
        ),
    )
    db.commit()
    return task


def tasks_of(db: Session, project: Project) -> list[Task]:
    db.expire_all()
    return list(db.execute(select(Task).where(Task.project_id == project.id)).scalars())


def one_task_of_kind(db: Session, project: Project, kind: TaskKind) -> Task:
    matches = [task for task in tasks_of(db, project) if task.kind is kind]
    assert len(matches) == 1, f"expected exactly one {kind.value} task, got {len(matches)}"
    return matches[0]


def events_of(db: Session, project: Project, event_type: EventType) -> list[Event]:
    db.expire_all()
    return list(
        db.execute(
            select(Event)
            .where(Event.project_id == project.id, Event.event_type == event_type)
            .order_by(Event.seq)
        ).scalars()
    )


def artifact_count(db: Session, project: Project) -> int:
    db.expire_all()
    return int(
        db.execute(
            select(func.count(ParsedArtifact.id)).where(ParsedArtifact.project_id == project.id)
        ).scalar()
        or 0
    )


def anchor_count(db: Session, project: Project) -> int:
    db.expire_all()
    return int(
        db.execute(
            select(func.count(EvidenceAnchor.id)).where(EvidenceAnchor.project_id == project.id)
        ).scalar()
        or 0
    )


def ingest(
    db: Session,
    project: Project,
    settings: Settings,
    uploader: User,
    *,
    path: Path,
    role: DocumentRole,
) -> UploadedFile:
    data = path.read_bytes()
    outcome = file_service.ingest_upload(
        db,
        project=project,
        filename=path.name,
        stream=io.BytesIO(data),
        size_bytes=len(data),
        declared_role=role,
        uploader=uploader,
        settings=settings,
    )
    assert outcome.accepted, outcome.rejection_reason
    assert outcome.file is not None
    return outcome.file


def make_finding(
    db: Session,
    project: Project,
    *,
    dedupe_key: str,
    severity: FindingSeverity,
    status: FindingStatus = FindingStatus.OPEN,
    is_reviewed: bool = False,
) -> Finding:
    finding = Finding(
        project_id=project.id,
        title=f"Synthetic finding {dedupe_key}",
        severity=severity,
        status=status,
        is_reviewed=is_reviewed,
        dedupe_key=dedupe_key,
    )
    db.add(finding)
    db.flush()
    return finding


def make_pending_approval(db: Session, project: Project) -> Approval:
    approval = Approval(
        project_id=project.id,
        kind=ApprovalKind.SAFETY_RECOMMENDATION,
        title="Confirm the missing jam interlock",
        question="Do you accept responsibility for this recommendation?",
    )
    db.add(approval)
    db.flush()
    return approval


@pytest.fixture
def io_list_only_project(
    db: Session,
    project: Project,
    seeded: dict[str, User],
    settings: Settings,
    fixtures_dir: Path,
) -> Project:
    """A project holding one readable document that is neither logic nor a schedule."""
    record = ingest(
        db,
        project,
        settings,
        seeded["engineer"],
        path=fixtures_dir / "IO_List.csv",
        role=DocumentRole.IO_LIST,
    )
    db.commit()
    outcome = parsing.parse_file(db, record, settings)
    assert outcome.succeeded, outcome.error_message
    db.commit()
    return project


# ---------------------------------------------------------------------------
# A full run, end to end
# ---------------------------------------------------------------------------
def test_a_drained_run_finishes_every_deterministic_step(
    db: Session, ingested_project: Project, seeded: dict[str, User], settings: Settings
) -> None:
    """Inventory, every parse and the evidence graph complete without help."""
    start_run(db, ingested_project, settings, seeded["engineer"])

    totals = worker.drain(settings)
    assert totals.claimed > 0

    inventory = one_task_of_kind(db, ingested_project, TaskKind.INVENTORY_FILES)
    assert inventory.status is TaskStatus.COMPLETED, inventory.error_message
    assert inventory.result["by_kind"]["l5x"] == 1
    assert inventory.result["by_document_role"]["interlock_schedule"] == 1
    assert inventory.result["missing_expected_inputs"] == []

    parse_tasks = [
        task for task in tasks_of(db, ingested_project) if task.kind is TaskKind.PARSE_FILE
    ]
    assert parse_tasks, "the run planned no parse tasks"
    for task in parse_tasks:
        assert task.status is TaskStatus.COMPLETED, f"{task.title}: {task.error_message}"
        assert task.result["succeeded"] is True

    graph = one_task_of_kind(db, ingested_project, TaskKind.BUILD_EVIDENCE_GRAPH)
    assert graph.status is TaskStatus.COMPLETED, graph.error_message
    assert graph.result["counts"]["rungs"] > 0
    assert graph.result["counts"]["interlocks"] > 0


def test_a_task_kind_with_no_handler_parks_rather_than_failing(
    db: Session, ingested_project: Project, seeded: dict[str, User], settings: Settings
) -> None:
    """An unavailable step is blocked, not failed.

    The distinction matters to an operator: "the review failed" sends them
    looking for a defect, while "this step is not installed" tells them to
    install the plugin and resume. Exercised against a kind whose handler is
    genuinely absent — the delivery kinds are all registered now — by removing
    one from the registry for the duration of the test.
    """
    from app.orchestration import handlers

    start_run(db, ingested_project, settings, seeded["engineer"])
    db.commit()

    removed = handlers._HANDLERS.pop(TaskKind.PLAN_PROJECT)
    try:
        worker.drain(settings)
    finally:
        handlers._HANDLERS[TaskKind.PLAN_PROJECT] = removed

    plan = one_task_of_kind(db, ingested_project, TaskKind.PLAN_PROJECT)
    assert plan.status is TaskStatus.BLOCKED
    assert "No handler is installed" in plan.blocked_reason
    assert plan.error_message == "", "an unavailable step is not an error"

    blocked_events = events_of(db, ingested_project, EventType.TASK_BLOCKED)
    assert plan.id in {event.task_id for event in blocked_events}


def test_started_and_completed_events_are_persisted_for_every_completed_task(
    db: Session, ingested_project: Project, seeded: dict[str, User], settings: Settings
) -> None:
    """The event log is the durable record; a task that ran silently is invisible."""
    start_run(db, ingested_project, settings, seeded["engineer"])
    worker.drain(settings)

    completed = [
        task for task in tasks_of(db, ingested_project) if task.status is TaskStatus.COMPLETED
    ]
    assert completed

    started_ids = {
        event.task_id for event in events_of(db, ingested_project, EventType.TASK_STARTED)
    }
    completed_ids = {
        event.task_id for event in events_of(db, ingested_project, EventType.TASK_COMPLETED)
    }
    for task in completed:
        assert task.id in started_ids, f"no task.started event for {task.title}"
        assert task.id in completed_ids, f"no task.completed event for {task.title}"

    payloads = {
        event.task_id: event.payload
        for event in events_of(db, ingested_project, EventType.TASK_COMPLETED)
    }
    inventory = one_task_of_kind(db, ingested_project, TaskKind.INVENTORY_FILES)
    assert payloads[inventory.id]["kind"] == TaskKind.INVENTORY_FILES.value
    assert payloads[inventory.id]["status"] == TaskStatus.COMPLETED.value


# ---------------------------------------------------------------------------
# Handler results
# ---------------------------------------------------------------------------
def test_a_handler_that_returns_blocked_parks_the_task_with_its_reason(
    db: Session, project: Project, settings: Settings
) -> None:
    reason = "The interlock schedule is missing; upload it and start the review again."

    def handler(_: TaskContext) -> HandlerResult:
        return HandlerResult.blocked(reason)

    with installed_handler(SPARE_KIND, handler):
        task = enqueue(db, project, settings, kind=SPARE_KIND, key="spare:blocked")
        outcome = worker.tick(settings, WORKER_ID)

    assert outcome.blocked == 1
    db.refresh(task)
    assert task.status is TaskStatus.BLOCKED
    assert task.blocked_reason == reason
    assert task.locked_by is None
    assert task.lease_expires_at is None


def test_a_handler_that_returns_awaiting_approval_gates_on_a_human(
    db: Session, project: Project, settings: Settings
) -> None:
    def handler(_: TaskContext) -> HandlerResult:
        return HandlerResult.awaiting_approval("An engineer must accept this recommendation.")

    with installed_handler(SPARE_KIND, handler):
        task = enqueue(db, project, settings, kind=SPARE_KIND, key="spare:approval")
        outcome = worker.tick(settings, WORKER_ID)

    assert outcome.awaiting_approval == 1
    db.refresh(task)
    assert task.status is TaskStatus.AWAITING_APPROVAL
    assert task.blocked_reason == "An engineer must accept this recommendation."
    assert task.finished_at is None, "a task waiting on a human has not finished"


def test_a_handler_that_fails_retries_while_attempts_remain(
    db: Session, project: Project, settings: Settings
) -> None:
    """A failure is a retry until the attempts are used up, then it stays failed.

    The backoff is set to zero here so the retry is claimable immediately; the
    behaviour under test is the attempt accounting, not the wait.
    """
    no_backoff = settings.model_copy(update={"task_retry_base_delay_seconds": 0.0})

    def handler(_: TaskContext) -> HandlerResult:
        return HandlerResult.failed("provider_unreachable", "The model provider did not answer.")

    with installed_handler(SPARE_KIND, handler):
        task = enqueue(db, project, settings, kind=SPARE_KIND, key="spare:failed", max_attempts=2)

        first = worker.tick(no_backoff, WORKER_ID)
        assert first.claimed == 1
        assert first.failed == 0, "an attempt that will be retried is not a failure yet"
        db.refresh(task)
        assert task.status is TaskStatus.QUEUED
        assert task.attempts == 1
        assert task.error_code == "provider_unreachable"
        assert task.error_message == "The model provider did not answer."

        second = worker.tick(no_backoff, WORKER_ID)

    assert second.failed == 1
    db.refresh(task)
    assert task.status is TaskStatus.FAILED
    assert task.attempts == 2
    assert task.error_code == "provider_unreachable"
    assert task.finished_at is not None


def test_a_retry_is_not_claimable_until_its_backoff_has_elapsed(
    db: Session, project: Project, settings: Settings
) -> None:
    """Without this the worker would spin on a failing task at full speed."""

    def handler(_: TaskContext) -> HandlerResult:
        return HandlerResult.failed("provider_unreachable", "The model provider did not answer.")

    with installed_handler(SPARE_KIND, handler):
        task = enqueue(db, project, settings, kind=SPARE_KIND, key="spare:backoff")
        worker.tick(settings, WORKER_ID)

        db.refresh(task)
        assert task.status is TaskStatus.QUEUED
        assert task.available_at is not None

        assert worker.tick(settings, WORKER_ID).claimed == 0

    db.refresh(task)
    assert task.attempts == 1


def test_a_handler_that_raises_fails_only_its_own_task(
    db: Session, ingested_project: Project, settings: Settings
) -> None:
    """An unexpected exception must not take the rest of the review with it."""

    def handler(_: TaskContext) -> HandlerResult:
        raise RuntimeError("the handler detonated")

    with installed_handler(SPARE_KIND, handler):
        exploding = enqueue(
            db,
            ingested_project,
            settings,
            kind=SPARE_KIND,
            key="spare:raises",
            priority=10,
            max_attempts=1,
        )
        inventory = enqueue(
            db,
            ingested_project,
            settings,
            kind=TaskKind.INVENTORY_FILES,
            key="after:inventory",
            priority=20,
        )

        first = worker.tick(settings, WORKER_ID)
        assert first.claimed == 1
        assert first.failed == 1

        second = worker.tick(settings, WORKER_ID)

    assert second.completed == 1, "the worker must still be running tasks after an exception"

    db.refresh(exploding)
    assert exploding.status is TaskStatus.FAILED
    assert exploding.error_code == "handler_error"
    assert "RuntimeError" in exploding.error_message
    assert "the handler detonated" in exploding.error_message

    db.refresh(inventory)
    assert inventory.status is TaskStatus.COMPLETED

    failed_events = events_of(db, ingested_project, EventType.TASK_FAILED)
    assert exploding.id in {event.task_id for event in failed_events}


# ---------------------------------------------------------------------------
# steps.inventory_files
# ---------------------------------------------------------------------------
def test_inventory_counts_what_was_supplied_and_names_what_is_missing(
    db: Session, io_list_only_project: Project, settings: Settings
) -> None:
    """Absence is the finding nobody makes, so it is written down explicitly."""
    task = enqueue(
        db,
        io_list_only_project,
        settings,
        kind=TaskKind.INVENTORY_FILES,
        key="inventory:partial",
    )
    worker.tick(settings, WORKER_ID)

    db.refresh(task)
    assert task.status is TaskStatus.COMPLETED, task.error_message
    assert task.result["file_count"] == 1
    assert task.result["analysable_count"] == 1
    assert task.result["by_kind"] == {FileKind.CSV.value: 1}
    assert task.result["by_document_role"] == {DocumentRole.IO_LIST.value: 1}
    assert task.result["rejected"] == []

    missing = task.result["missing_expected_inputs"]
    assert "Studio 5000 L5X export" in missing
    assert "Interlock schedule" in missing
    assert "Functional description" in missing
    assert "I/O list" not in missing, "the one document that was supplied is not missing"


def test_inventory_blocks_when_nothing_supplied_can_be_analysed(
    db: Session, project: Project, settings: Settings
) -> None:
    """An archive container and a rejected upload leave nothing to review.

    Completing here would report a clean review of zero documents.
    """
    container = UploadedFile(
        project_id=project.id,
        original_filename="Handover_Pack.zip",
        stored_filename="handover_pack.zip",
        relative_path="originals/handover_pack.zip",
        file_kind=FileKind.ZIP,
        document_role=DocumentRole.OTHER,
        status=FileStatus.PARSED,
        size_bytes=1024,
        sha256="a" * 64,
    )
    refused = UploadedFile(
        project_id=project.id,
        original_filename="setup.exe",
        stored_filename="setup.exe",
        relative_path="originals/setup.exe",
        file_kind=FileKind.UNKNOWN,
        document_role=DocumentRole.OTHER,
        status=FileStatus.REJECTED,
        status_detail="Executable content is not accepted.",
        size_bytes=2048,
        sha256="b" * 64,
    )
    db.add_all([container, refused])
    db.commit()

    task = enqueue(db, project, settings, kind=TaskKind.INVENTORY_FILES, key="inventory:empty")
    outcome = worker.tick(settings, WORKER_ID)

    assert outcome.blocked == 1
    db.refresh(task)
    assert task.status is TaskStatus.BLOCKED
    assert "No analysable files were supplied" in task.blocked_reason
    assert "Upload engineering documents" in task.blocked_reason


# ---------------------------------------------------------------------------
# steps.parse_file
# ---------------------------------------------------------------------------
def test_parsing_a_file_that_no_longer_exists_completes_as_skipped(
    db: Session, project: Project, settings: Settings
) -> None:
    """The file is gone. That is not an error, and failing would strand the run."""
    task = enqueue(
        db,
        project,
        settings,
        kind=TaskKind.PARSE_FILE,
        key="parse:missing",
        payload={"file_id": str(uuid.uuid4())},
    )
    outcome = worker.tick(settings, WORKER_ID)

    assert outcome.completed == 1
    db.refresh(task)
    assert task.status is TaskStatus.COMPLETED
    assert task.result["skipped"] is True
    assert "deleted" in task.result["reason"].lower()
    assert task.error_message == ""


def test_an_unparseable_file_completes_the_task_with_succeeded_false(
    db: Session, project: Project, settings: Settings
) -> None:
    """Failing the task would stall the whole run over one unreadable document.

    The failure is already recorded on the file row, where the UI can explain it,
    so the task's job — attempting the parse — genuinely did finish.
    """
    record = UploadedFile(
        project_id=project.id,
        original_filename="Panel_Photo.heic",
        stored_filename="panel_photo.heic",
        relative_path="originals/panel_photo.heic",
        file_kind=FileKind.UNKNOWN,
        document_role=DocumentRole.OTHER,
        status=FileStatus.UPLOADED,
        size_bytes=4096,
        sha256="c" * 64,
    )
    db.add(record)
    db.commit()

    task = enqueue(
        db,
        project,
        settings,
        kind=TaskKind.PARSE_FILE,
        key="parse:unreadable",
        payload={"file_id": str(record.id)},
    )
    outcome = worker.tick(settings, WORKER_ID)

    assert outcome.completed == 1, "one bad document must not fail its task"
    assert outcome.failed == 0

    db.refresh(task)
    assert task.status is TaskStatus.COMPLETED
    assert task.result["succeeded"] is False
    assert task.result["error_code"]
    assert task.result["error_message"]
    assert task.attempts == 1, "a completed parse is not retried"

    db.refresh(record)
    assert record.status is FileStatus.PARSE_FAILED
    assert record.status_detail


def test_a_parse_task_with_no_file_id_fails_rather_than_claiming_success(
    db: Session, project: Project, settings: Settings
) -> None:
    """A task that cannot say what it was meant to read is a defect, not a document."""
    task = enqueue(db, project, settings, kind=TaskKind.PARSE_FILE, key="parse:no-payload")
    worker.tick(settings, WORKER_ID)

    db.refresh(task)
    assert task.status is not TaskStatus.COMPLETED
    assert task.error_code == "bad_payload"


# ---------------------------------------------------------------------------
# steps.build_evidence_graph
# ---------------------------------------------------------------------------
def test_the_evidence_graph_step_reports_counts_for_a_complete_project(
    db: Session, ingested_project: Project, settings: Settings
) -> None:
    task = enqueue(
        db, ingested_project, settings, kind=TaskKind.BUILD_EVIDENCE_GRAPH, key="graph:complete"
    )
    outcome = worker.tick(settings, WORKER_ID)

    assert outcome.completed == 1
    db.refresh(task)
    assert task.status is TaskStatus.COMPLETED
    counts = task.result["counts"]
    assert counts["rungs"] > 0
    assert counts["interlocks"] > 0
    assert counts["io_points"] > 0
    assert task.result["missing_inputs"] == []
    assert "tag_usage" in task.result


def test_the_evidence_graph_step_blocks_when_there_is_nothing_to_compare(
    db: Session, io_list_only_project: Project, settings: Settings
) -> None:
    """One readable I/O list is not a review: a discrepancy needs two documents."""
    task = enqueue(
        db,
        io_list_only_project,
        settings,
        kind=TaskKind.BUILD_EVIDENCE_GRAPH,
        key="graph:nothing-to-compare",
    )
    outcome = worker.tick(settings, WORKER_ID)

    assert outcome.blocked == 1
    db.refresh(task)
    assert task.status is TaskStatus.BLOCKED
    assert "Neither control logic nor an interlock schedule" in task.blocked_reason
    assert "Upload an L5X export and an interlock schedule." in task.blocked_reason


# ---------------------------------------------------------------------------
# steps.finalize_project
# ---------------------------------------------------------------------------
def test_finalising_waits_while_an_approval_is_still_pending(
    db: Session, project: Project, settings: Settings
) -> None:
    """Reporting a review complete with a safety approval outstanding would be
    the single worst thing this product could do."""
    make_pending_approval(db, project)
    db.commit()

    task = enqueue(db, project, settings, kind=TaskKind.FINALIZE_PROJECT, key="finalize:pending")
    outcome = worker.tick(settings, WORKER_ID)

    assert outcome.awaiting_approval == 1
    db.refresh(task)
    assert task.status is TaskStatus.AWAITING_APPROVAL
    assert "1 approval(s)" in task.blocked_reason


def test_finalising_blocks_while_a_finding_has_not_been_senior_reviewed(
    db: Session, project: Project, settings: Settings
) -> None:
    make_finding(
        db,
        project,
        dedupe_key="unreviewed-1",
        severity=FindingSeverity.HIGH,
        is_reviewed=False,
    )
    db.commit()

    task = enqueue(db, project, settings, kind=TaskKind.FINALIZE_PROJECT, key="finalize:unreviewed")
    outcome = worker.tick(settings, WORKER_ID)

    assert outcome.blocked == 1
    db.refresh(task)
    assert task.status is TaskStatus.BLOCKED
    assert "1 finding(s)" in task.blocked_reason
    assert "senior review" in task.blocked_reason


def test_finalising_completes_and_reports_the_finding_counts(
    db: Session, project: Project, settings: Settings
) -> None:
    make_finding(
        db,
        project,
        dedupe_key="reviewed-high",
        severity=FindingSeverity.HIGH,
        status=FindingStatus.ACCEPTED,
        is_reviewed=True,
    )
    make_finding(
        db,
        project,
        dedupe_key="reviewed-low",
        severity=FindingSeverity.LOW,
        status=FindingStatus.REJECTED,
        is_reviewed=True,
    )
    db.commit()

    task = enqueue(db, project, settings, kind=TaskKind.FINALIZE_PROJECT, key="finalize:clean")
    outcome = worker.tick(settings, WORKER_ID)

    assert outcome.completed == 1
    db.refresh(task)
    assert task.status is TaskStatus.COMPLETED
    assert task.result["finding_count"] == 2
    assert task.result["findings_by_severity"] == {
        FindingSeverity.HIGH.value: 1,
        FindingSeverity.LOW.value: 1,
    }
    assert task.result["accepted"] == 1
    assert task.result["rejected"] == 1

    completed_events = events_of(db, project, EventType.PROJECT_COMPLETED)
    assert task.id in {event.task_id for event in completed_events}


# ---------------------------------------------------------------------------
# Restart recovery
# ---------------------------------------------------------------------------
def test_a_task_abandoned_by_a_stopped_worker_is_requeued_and_the_run_finishes(
    db: Session, ingested_project: Project, seeded: dict[str, User], settings: Settings
) -> None:
    """The crash case, simulated exactly: a lease left behind by a dead process.

    Without the sweep the task would sit in ``running`` forever and the review
    would never resume — the product would need a human to notice and restart it.
    """
    start_run(db, ingested_project, settings, seeded["engineer"])

    queue = DatabaseTaskQueue(db, settings)
    claimed = queue.claim("crashed-worker", limit=1)
    assert len(claimed) == 1
    task = claimed[0]
    assert task.kind is TaskKind.INVENTORY_FILES
    assert task.status is TaskStatus.RUNNING
    assert task.attempts == 1

    # The process died here; its lease simply ran out.
    task.lease_expires_at = utcnow() - timedelta(hours=1)
    db.commit()

    assert worker.recover(settings) == 1

    db.refresh(task)
    assert task.status is TaskStatus.QUEUED
    assert task.locked_by is None
    assert task.locked_at is None
    assert task.lease_expires_at is None
    assert task.attempts == 1, "a crash costs one attempt, not two"
    assert task.error_code == "lease_expired"

    worker.drain(settings)

    db.refresh(task)
    assert task.status is TaskStatus.COMPLETED
    assert task.attempts == 2
    assert task.error_code == ""

    graph = one_task_of_kind(db, ingested_project, TaskKind.BUILD_EVIDENCE_GRAPH)
    assert graph.status is TaskStatus.COMPLETED
    plan = one_task_of_kind(db, ingested_project, TaskKind.PLAN_PROJECT)
    assert plan.status is TaskStatus.COMPLETED, (
        "after recovery the run should continue through planning, not stall"
    )


def test_a_task_whose_lease_is_still_live_is_not_requeued(
    db: Session, ingested_project: Project, seeded: dict[str, User], settings: Settings
) -> None:
    """Requeuing work that is still in flight would run it twice for no reason."""
    start_run(db, ingested_project, settings, seeded["engineer"])

    queue = DatabaseTaskQueue(db, settings)
    task = queue.claim("busy-worker", limit=1)[0]
    db.commit()

    assert worker.recover(settings) == 0

    db.refresh(task)
    assert task.status is TaskStatus.RUNNING
    assert task.locked_by == "busy-worker"
    assert task.lease_expires_at is not None
    assert task.attempts == 1


def test_a_task_out_of_attempts_when_its_lease_expires_fails_visibly(
    db: Session, project: Project, settings: Settings
) -> None:
    """It is never silently dropped: a task nobody can see failed is worse."""

    def handler(_: TaskContext) -> HandlerResult:
        return HandlerResult.completed()

    with installed_handler(SPARE_KIND, handler):
        task = enqueue(
            db, project, settings, kind=SPARE_KIND, key="spare:last-attempt", max_attempts=1
        )

        queue = DatabaseTaskQueue(db, settings)
        claimed = queue.claim("crashed-worker", limit=1)
        assert [item.id for item in claimed] == [task.id]
        task.lease_expires_at = utcnow() - timedelta(hours=1)
        db.commit()

        assert worker.recover(settings) == 1

    db.refresh(task)
    assert task.status is TaskStatus.FAILED
    assert task.error_code == "lease_expired"
    assert "stopped responding" in task.error_message


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------
def test_draining_a_finished_run_a_second_time_does_no_work(
    db: Session, ingested_project: Project, seeded: dict[str, User], settings: Settings
) -> None:
    """The queue is at-least-once, so a replayed pass must converge, not duplicate.

    Duplicate artifacts or anchors would double-count the evidence behind every
    finding and make a citation ambiguous.
    """
    start_run(db, ingested_project, settings, seeded["engineer"])

    first = worker.drain(settings)
    assert first.claimed > 0

    artifacts_before = artifact_count(db, ingested_project)
    anchors_before = anchor_count(db, ingested_project)
    assert artifacts_before > 0
    assert anchors_before > 0

    statuses_before = {task.id: task.status for task in tasks_of(db, ingested_project)}
    attempts_before = {task.id: task.attempts for task in tasks_of(db, ingested_project)}

    second = worker.drain(settings)

    assert second.claimed == 0, "nothing was runnable, so nothing should have been claimed"
    assert artifact_count(db, ingested_project) == artifacts_before
    assert anchor_count(db, ingested_project) == anchors_before
    assert {task.id: task.status for task in tasks_of(db, ingested_project)} == statuses_before
    assert {task.id: task.attempts for task in tasks_of(db, ingested_project)} == attempts_before


def test_re_running_a_completed_parse_task_does_not_duplicate_its_artifacts(
    db: Session, ingested_project: Project, settings: Settings
) -> None:
    """The at-least-once guarantee means a handler can genuinely run twice."""
    record = parsing.parseable_files(db, ingested_project.id)[0]
    artifacts_before = artifact_count(db, ingested_project)
    anchors_before = anchor_count(db, ingested_project)

    task = enqueue(
        db,
        ingested_project,
        settings,
        kind=TaskKind.PARSE_FILE,
        key="parse:replay",
        payload={"file_id": str(record.id)},
    )
    worker.tick(settings, WORKER_ID)
    db.refresh(task)
    assert task.status is TaskStatus.COMPLETED

    # Force the same work to happen a second time, as a lease expiry would.
    task.status = TaskStatus.QUEUED
    task.finished_at = None
    db.commit()
    worker.tick(settings, WORKER_ID)

    db.refresh(task)
    assert task.status is TaskStatus.COMPLETED
    assert artifact_count(db, ingested_project) == artifacts_before
    assert anchor_count(db, ingested_project) == anchors_before
