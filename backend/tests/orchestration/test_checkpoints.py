"""Checkpoint durability, and what actually protects partial progress today.

No core handler uses ``Task.checkpoint``, and that is deliberate rather than an
oversight: the workflow decomposes to **one task per file**, which is finer
grained than a checkpoint would be. A run that dies after parsing six of nine
files loses nothing, because the six are already six completed tasks.

The mechanism is kept and tested because a *plugin* handler is the case it exists
for — a delivery agent generating twenty reports is one task doing twenty things,
and it needs somewhere to record which nineteen are done. These tests are the
contract that extension point rests on.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy.orm import Session

from app.core.clock import utcnow
from app.core.config import Settings
from app.core.enums import TaskKind, TaskStatus
from app.models.project import Project
from app.models.task import Task
from app.models.user import User
from app.orchestration import engine, handlers, worker
from app.orchestration.queue import DatabaseTaskQueue, EnqueueRequest


def _enqueue(db: Session, settings: Settings, project: Project, key: str) -> Task:
    task = DatabaseTaskQueue(db, settings).enqueue(
        project.id,
        EnqueueRequest(kind=TaskKind.PARSE_FILE, title=key, idempotency_key=key),
    )
    db.commit()
    return task


def test_checkpoint_state_survives_a_requeue(
    db: Session, project: Project, settings: Settings
) -> None:
    """The whole point: a crash must not lose what the handler already recorded."""
    queue = DatabaseTaskQueue(db, settings)
    task = _enqueue(db, settings, project, "long-running")

    queue.claim("worker-1", limit=1)
    task.checkpoint = {"completed": ["report-a", "report-b"]}
    db.commit()

    # The worker dies: its lease goes stale and the sweep takes the task back.
    task.lease_expires_at = utcnow() - timedelta(hours=1)
    db.commit()
    assert queue.requeue_expired() == 1
    db.commit()

    db.expire_all()
    db.refresh(task)
    assert task.status is TaskStatus.QUEUED
    assert task.checkpoint == {"completed": ["report-a", "report-b"]}
    assert task.attempts == 1, "a crash costs one attempt, not two"


def test_a_handler_can_record_and_read_back_its_progress(
    db: Session, project: Project, settings: Settings
) -> None:
    """The ``TaskContext`` helpers a plugin handler would actually use."""
    task = _enqueue(db, settings, project, "incremental")
    context = handlers.TaskContext(db=db, task=task, project=project, settings=settings)

    assert context.done() == set()
    context.mark_done("report-a")
    context.mark_done("report-b")
    db.commit()

    db.expire_all()
    db.refresh(task)
    reloaded = handlers.TaskContext(db=db, task=task, project=project, settings=settings)
    assert reloaded.done() == {"report-a", "report-b"}
    # Sorted on write, so two runs over the same items produce identical state.
    assert task.checkpoint["completed"] == ["report-a", "report-b"]


def test_marking_the_same_item_twice_does_not_duplicate_it(
    db: Session, project: Project, settings: Settings
) -> None:
    """The queue is at-least-once, so a resumed handler will re-mark items."""
    task = _enqueue(db, settings, project, "repeat")
    context = handlers.TaskContext(db=db, task=task, project=project, settings=settings)

    context.mark_done("report-a")
    context.mark_done("report-a")
    assert task.checkpoint["completed"] == ["report-a"]


def test_a_resumed_handler_skips_the_work_it_already_did(
    db: Session, project: Project, settings: Settings
) -> None:
    """End to end through the worker, with a handler that crashes halfway.

    This is the shape a delivery agent would take: one task, many items, and a
    record of which are finished so a retry does not redo them.
    """
    processed: list[str] = []
    items = ["a", "b", "c", "d"]

    def flaky(context: handlers.TaskContext) -> handlers.HandlerResult:
        done = context.done()
        for item in items:
            if item in done:
                continue
            if item == "c" and "crashed" not in context.task.checkpoint:
                context.checkpoint(crashed=True)
                raise RuntimeError("simulated crash partway through")
            processed.append(item)
            context.mark_done(item)
        return handlers.HandlerResult.completed(processed=sorted(context.done()))

    # Zero the retry backoff so the resume happens within one drain; the delay
    # itself is covered in test_queue.py and only slows this test down.
    immediate = settings.model_copy(update={"task_retry_base_delay_seconds": 0.0})

    previous = handlers._HANDLERS.pop(TaskKind.PARSE_FILE, None)
    handlers.register(TaskKind.PARSE_FILE)(flaky)
    try:
        task = _enqueue(db, immediate, project, "resumable")
        worker.drain(immediate)
    finally:
        handlers._HANDLERS.pop(TaskKind.PARSE_FILE, None)
        if previous is not None:
            handlers._HANDLERS[TaskKind.PARSE_FILE] = previous

    db.expire_all()
    db.refresh(task)
    assert task.status is TaskStatus.COMPLETED, task.error_message
    # 'a' and 'b' were done before the crash and are not redone on the retry.
    assert processed == ["a", "b", "c", "d"], processed
    assert set(task.result["processed"]) == set(items)


def test_per_file_tasks_are_what_protect_a_real_run(
    db: Session, ingested_project: Project, seeded: dict[str, User], settings: Settings
) -> None:
    """Why no core handler needs a checkpoint.

    A crash after six of nine parses loses nothing, because those six are six
    completed tasks — a finer granularity than any checkpoint inside a single
    task could give.
    """
    engine.start_run(db, ingested_project, settings, actor=seeded["engineer"])
    db.commit()

    for _ in range(4):
        worker.tick(settings, "worker-1", limit=1)

    db.expire_all()
    parse_tasks = (
        db.query(Task)
        .filter(Task.project_id == ingested_project.id, Task.kind == TaskKind.PARSE_FILE)
        .all()
    )
    completed = [task for task in parse_tasks if task.status is TaskStatus.COMPLETED]
    assert completed, "some parses should already be done"
    assert len(completed) < len(parse_tasks), "and some should still be outstanding"

    # A restart re-runs nothing that finished.
    finished_ids = {task.id for task in completed}
    worker.drain(settings)

    db.expire_all()
    for task in db.query(Task).filter(Task.id.in_(finished_ids)).all():
        assert task.status is TaskStatus.COMPLETED
        assert task.attempts == 1, "a completed task was needlessly re-run"
