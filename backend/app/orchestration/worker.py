"""The worker loop.

Claims leased tasks, runs their handlers, and records the outcome. Runs as a
daemon thread inside the API process for the local single-process deployment, and
can equally be run standalone via ``scripts/backend/worker.py`` for an internal
multi-user install.

Each task gets its own session and its own transaction. That is deliberate: a
handler that fails must not roll back the task before it, and the task's new
state must land with whatever the handler wrote — a completed task whose findings
were rolled back would be a silent, invisible data loss.
"""

from __future__ import annotations

import os
import socket
import threading
import time
import uuid
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.agents.base.presence import begin_task_presence, finish_task_presence
from app.core.config import Settings
from app.core.enums import EventType, TaskStatus
from app.core.logging import get_logger, set_request_context
from app.database.session import session_scope
from app.models.project import Project
from app.models.task import Task
from app.orchestration import engine, handlers
from app.orchestration.events import record_event
from app.orchestration.queue import DatabaseTaskQueue, EnqueueRequest

logger = get_logger(__name__)


def worker_identity(suffix: str = "") -> str:
    """A stable-per-process identity, useful when reading leases in the database."""
    host = socket.gethostname()[:40]
    return f"{host}:{os.getpid()}{f':{suffix}' if suffix else ''}"


@dataclass(slots=True)
class TickResult:
    claimed: int = 0
    completed: int = 0
    failed: int = 0
    blocked: int = 0
    awaiting_approval: int = 0

    @property
    def did_work(self) -> bool:
        return self.claimed > 0


def run_one(db: Session, task: Task, settings: Settings) -> str:
    """Execute one claimed task. Returns its resulting status.

    The caller owns the transaction. Any exception the handler did not convert is
    caught here and recorded as a task failure — a worker that dies on one bad
    file would take the whole project's review with it.
    """
    queue = DatabaseTaskQueue(db, settings)
    project = db.get(Project, task.project_id)
    if project is None:  # pragma: no cover - FK makes this unreachable
        queue.fail(task, code="project_missing", message="Project no longer exists.", retry=False)
        return TaskStatus.FAILED.value

    set_request_context(project_id=str(project.id))

    # A hard prerequisite that failed or was cancelled will never complete, so
    # running this task would work from missing input and leaving it queued would
    # stall the run silently while the project still reported itself as running.
    broken = queue.failed_hard_dependencies(task)
    if broken:
        reason = "A prerequisite task did not complete: " + "; ".join(
            f"{upstream.title} ({upstream.status.value})" for upstream in broken[:3]
        )
        queue.fail(task, code="dependency_failed", message=reason, retry=False)
        finish_task_presence(db, task)
        record_event(
            db,
            project_id=project.id,
            event_type=EventType.TASK_FAILED,
            agent_id=task.agent_instance_id,
            task_id=task.id,
            payload={
                "task_id": str(task.id),
                "kind": task.kind.value,
                "title": task.title,
                "status": task.status.value,
                "agent_kind": task.agent_kind or "",
                "attempts": task.attempts,
                "detail": reason,
            },
        )
        engine.refresh_project_status(db, project)
        return task.status.value

    context = handlers.TaskContext(db=db, task=task, project=project, settings=settings)

    # Presence is deliberately committed before the handler begins.  Provider
    # calls and document parsing can take long enough that keeping this in the
    # handler transaction would make the city show an idle agent for the whole
    # task and publish its "working" event only after the work had finished.
    # ``run_one`` owns a fresh session containing no handler writes yet, so this
    # commit persists only the claimed task's assignment/presence and starts a
    # new transaction for the handler's authoritative engineering output.
    begin_task_presence(db, task, project)
    record_event(
        db,
        project_id=project.id,
        event_type=EventType.TASK_STARTED,
        agent_id=task.agent_instance_id,
        task_id=task.id,
        payload={
            "task_id": str(task.id),
            "kind": task.kind.value,
            "title": task.title,
            "status": TaskStatus.RUNNING.value,
            "agent_kind": task.agent_kind or "",
            "attempts": task.attempts,
        },
    )
    db.commit()

    started = time.perf_counter()
    try:
        outcome = handlers.run(context)
    except Exception as exc:
        logger.exception(
            "task handler raised",
            extra={"task_id": str(task.id), "kind": task.kind.value},
        )
        outcome = handlers.HandlerResult.failed("handler_error", f"{type(exc).__name__}: {exc}")
    duration_ms = int((time.perf_counter() - started) * 1000)

    for request in outcome.enqueue:
        if isinstance(request, EnqueueRequest):
            queue.enqueue(project.id, request)

    # Recorded for every outcome, not just success: the detail a handler
    # attaches to a block ("these files were rejected, and why") is exactly what
    # the UI needs to explain the block to an engineer.
    task.result = {**outcome.result, "duration_ms": duration_ms}

    if outcome.status == "completed":
        queue.complete(task, task.result)
        event = EventType.TASK_COMPLETED
        detail = ""
    elif outcome.status == "blocked":
        queue.block(task, outcome.reason)
        event = EventType.TASK_BLOCKED
        detail = outcome.reason
    elif outcome.status == "awaiting_approval":
        queue.await_approval(task, outcome.reason)
        event = EventType.TASK_BLOCKED
        detail = outcome.reason
    else:
        queue.fail(task, code=outcome.error_code or "task_failed", message=outcome.reason)
        event = (
            EventType.TASK_FAILED if task.status is TaskStatus.FAILED else EventType.TASK_BLOCKED
        )
        detail = outcome.reason

    finish_task_presence(db, task)
    record_event(
        db,
        project_id=project.id,
        event_type=event,
        agent_id=task.agent_instance_id,
        task_id=task.id,
        payload={
            "task_id": str(task.id),
            "kind": task.kind.value,
            "title": task.title,
            "status": task.status.value,
            "agent_kind": task.agent_kind or "",
            "attempts": task.attempts,
            "detail": detail,
        },
    )

    engine.refresh_project_status(db, project)
    logger.info(
        "task finished",
        extra={
            "task_id": str(task.id),
            "kind": task.kind.value,
            "status": task.status.value,
            "duration_ms": duration_ms,
            "attempts": task.attempts,
        },
    )
    return task.status.value


def tick(settings: Settings, worker_id: str, *, limit: int = 1) -> TickResult:
    """Claim and run up to ``limit`` tasks. One transaction per task."""
    result = TickResult()

    with session_scope() as db:
        queue = DatabaseTaskQueue(db, settings)
        claimed = queue.claim(worker_id, limit=limit)
        claimed_ids = [task.id for task in claimed]
    result.claimed = len(claimed_ids)

    for task_id in claimed_ids:
        with session_scope() as db:
            task = db.get(Task, task_id)
            if task is None:  # pragma: no cover - deleted mid-flight
                continue
            status = run_one(db, task, settings)

        if status == TaskStatus.COMPLETED.value:
            result.completed += 1
        elif status == TaskStatus.FAILED.value:
            result.failed += 1
        elif status == TaskStatus.BLOCKED.value:
            result.blocked += 1
        elif status == TaskStatus.AWAITING_APPROVAL.value:
            result.awaiting_approval += 1

    return result


def drain(settings: Settings, *, max_iterations: int = 500) -> TickResult:
    """Run tasks until nothing is runnable.

    For tests and the offline demo, where waiting on a poll interval is pointless.
    ``max_iterations`` is a backstop against a handler that enqueues work forever.
    """
    worker_id = worker_identity("drain")
    totals = TickResult()

    for iteration in range(max_iterations):
        outcome = tick(settings, worker_id, limit=1)
        totals.claimed += outcome.claimed
        totals.completed += outcome.completed
        totals.failed += outcome.failed
        totals.blocked += outcome.blocked
        totals.awaiting_approval += outcome.awaiting_approval
        if not outcome.did_work:
            return totals
        if iteration == max_iterations - 1:  # pragma: no cover - runaway guard
            logger.error(
                "drain hit its iteration cap; a handler may be enqueueing work forever",
                extra={"max_iterations": max_iterations},
            )
    return totals


def recover(settings: Settings) -> int:
    """Requeue tasks abandoned by a stopped worker.

    Run at startup and periodically. This is what makes the product survive a
    restart: a task left ``running`` by a killed process returns to the queue
    instead of sitting there forever.
    """
    with session_scope() as db:
        queue = DatabaseTaskQueue(db, settings)
        return queue.requeue_expired()


class Worker:
    """A background worker thread."""

    def __init__(self, settings: Settings, *, name: str = "esco-worker") -> None:
        self.settings = settings
        self.name = name
        self.worker_id = worker_identity(uuid.uuid4().hex[:6])
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_sweep = 0.0

    def start(self) -> None:
        if self._thread is not None:
            return
        recover(self.settings)
        self._thread = threading.Thread(target=self._loop, name=self.name, daemon=True)
        self._thread.start()
        logger.info(
            "worker started",
            extra={
                "worker_id": self.worker_id,
                "poll_interval": self.settings.worker_poll_interval_seconds,
            },
        )

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
        logger.info("worker stopped", extra={"worker_id": self.worker_id})

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _loop(self) -> None:
        interval = self.settings.worker_poll_interval_seconds
        # Sweep for abandoned leases about as often as a lease lasts; more
        # frequently would be wasted queries, less would leave crashed work
        # stranded for longer than necessary.
        sweep_interval = max(self.settings.task_lease_seconds / 3.0, 30.0)

        while not self._stop.is_set():
            try:
                now = time.monotonic()
                if now - self._last_sweep > sweep_interval:
                    recover(self.settings)
                    self._last_sweep = now

                outcome = tick(
                    self.settings, self.worker_id, limit=self.settings.worker_concurrency
                )
                if outcome.did_work:
                    continue
            except Exception:
                logger.exception("worker loop error", extra={"worker_id": self.worker_id})

            self._stop.wait(interval)


_worker: Worker | None = None


def get_worker(settings: Settings) -> Worker:
    global _worker
    if _worker is None:
        _worker = Worker(settings)
    return _worker


def stop_worker() -> None:
    global _worker
    if _worker is not None:
        _worker.stop()
        _worker = None
