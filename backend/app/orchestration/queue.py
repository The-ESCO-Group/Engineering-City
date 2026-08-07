"""The persisted job queue.

The database *is* the queue. No Redis, no broker: a local single-process
deployment does not need one, and a durable queue that survives a restart is
worth more here than throughput.

The contract is **at-least-once with leases**:

* a worker claims a task by writing ``locked_by`` and ``lease_expires_at``;
* if it finishes, the task moves to a terminal state;
* if the process dies, the lease simply expires and a sweep requeues the task.

At-least-once means a handler can run twice for the same task. Handlers must
therefore be idempotent — which is what ``Task.idempotency_key`` and the
checkpoint field are for. Exactly-once would need distributed consensus to buy a
guarantee that careful handlers give for free.

:class:`TaskQueue` is a protocol so a future out-of-process queue can be
substituted without touching the engine.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Protocol

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.clock import utcnow
from app.core.config import Settings
from app.core.enums import TaskKind, TaskStatus
from app.core.logging import get_logger
from app.database.locking import lock_project_root
from app.models.project import Project
from app.models.task import Task, TaskDependency

logger = get_logger(__name__)


@dataclass(slots=True)
class EnqueueRequest:
    """One task to create."""

    kind: TaskKind
    title: str
    #: Stable per-project key. Enqueuing the same key twice is a no-op, which is
    #: what makes a retried or replayed plan safe.
    idempotency_key: str
    description: str = ""
    agent_kind: str | None = None
    payload: dict[str, Any] | None = None
    priority: int = 100
    max_attempts: int = 3
    run_id: uuid.UUID | None = None
    depends_on: list[uuid.UUID] | None = None


class TaskQueue(Protocol):
    """What the workflow engine needs from a queue."""

    def enqueue(self, project_id: uuid.UUID, request: EnqueueRequest) -> Task: ...

    def claim(self, worker_id: str, *, limit: int = 1) -> list[Task]: ...

    def complete(self, task: Task, result: dict[str, Any] | None = None) -> None: ...

    def fail(self, task: Task, *, code: str, message: str, retry: bool = True) -> None: ...

    def requeue_expired(self) -> int: ...


class DatabaseTaskQueue:
    """The database-backed implementation.

    Holds a :class:`Session` and does not commit: the caller owns the transaction
    boundary, so a claim and the work that follows land or roll back together.
    """

    def __init__(self, db: Session, settings: Settings) -> None:
        self.db = db
        self.settings = settings

    # -- writing ---------------------------------------------------------
    def enqueue(self, project_id: uuid.UUID, request: EnqueueRequest) -> Task:
        """Create a task, or return the existing one with the same key.

        Idempotency is enforced by a unique constraint rather than a
        check-then-insert, so two workers planning concurrently cannot both slip
        a duplicate past a read.
        """
        existing = self.db.execute(
            select(Task).where(
                Task.project_id == project_id,
                Task.idempotency_key == request.idempotency_key,
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing

        task = Task(
            project_id=project_id,
            run_id=request.run_id,
            kind=request.kind,
            title=request.title,
            description=request.description,
            status=TaskStatus.QUEUED,
            priority=request.priority,
            agent_kind=request.agent_kind,
            payload=request.payload or {},
            idempotency_key=request.idempotency_key,
            max_attempts=request.max_attempts or self.settings.task_max_attempts,
        )
        try:
            # A SAVEPOINT, so losing the uniqueness race undoes only this INSERT.
            # The object is added *inside* it. ``begin_nested`` unconditionally
            # flushes pending ORM state before opening the savepoint; adding the
            # task earlier put the uniqueness violation in the outer transaction
            # and poisoned all work the caller had already staged.
            with self.db.begin_nested():
                self.db.add(task)
                self.db.flush()
        except IntegrityError:
            # Lost the race. The other writer's row is the one that counts.
            if task in self.db:
                self.db.expunge(task)
            duplicate = self.db.execute(
                select(Task).where(
                    Task.project_id == project_id,
                    Task.idempotency_key == request.idempotency_key,
                )
            ).scalar_one_or_none()
            if duplicate is None:
                # The violated constraint belonged to caller-owned pending
                # state, not this idempotency key. Preserve the real error.
                raise
            logger.info(
                "task already enqueued by a concurrent writer",
                extra={"task_id": str(duplicate.id), "key": request.idempotency_key},
            )
            return duplicate

        for dependency_id in request.depends_on or []:
            self.add_dependency(task, dependency_id)

        self.db.flush()
        return task

    def add_dependency(self, task: Task, depends_on_id: uuid.UUID, *, hard: bool = True) -> None:
        """Record that ``task`` must wait for ``depends_on_id``."""
        if depends_on_id == task.id:
            raise ValueError("a task cannot depend on itself")
        existing = self.db.execute(
            select(TaskDependency).where(
                TaskDependency.task_id == task.id,
                TaskDependency.depends_on_id == depends_on_id,
            )
        ).scalar_one_or_none()
        if existing is None:
            self.db.add(TaskDependency(task_id=task.id, depends_on_id=depends_on_id, hard=hard))

    # -- claiming --------------------------------------------------------
    def claim(self, worker_id: str, *, limit: int = 1) -> list[Task]:
        """Take up to ``limit`` runnable tasks and lease them to ``worker_id``.

        A task is runnable when it is queued, past any retry backoff, belongs to
        a project that is accepting work, and has no unfinished dependencies.

        The claim is a conditional ``UPDATE ... WHERE status = 'queued'``, so two
        workers racing for the same row cannot both win: the second update
        matches nothing.
        """
        now = utcnow()
        # Deliberately unbounded. A window sized from ``limit`` lets a run of
        # dependency-blocked tasks at the front of the priority order hide every
        # runnable task behind them, and the worker then idles while there is
        # work to do. Runnable tasks are filtered in Python because the
        # dependency rule is not expressible as a simple join, and the queue is
        # small enough per project that scanning it is cheaper than a stalled run.
        candidates = list(
            self.db.execute(
                select(Task)
                .join(Project, Project.id == Task.project_id)
                .where(
                    Task.status == TaskStatus.QUEUED,
                    Task.locked_by.is_(None),
                    (Task.available_at.is_(None)) | (Task.available_at <= now),
                    Project.is_paused.is_(False),
                    Project.status.notin_(
                        ["archived", "completed", "failed"],
                    ),
                )
                .order_by(Task.priority, Task.created_at)
            ).scalars()
        )

        claimed: list[Task] = []
        for task in candidates:
            if len(claimed) >= limit:
                break
            # A cheap first pass avoids taking a project write lock for work
            # that is plainly blocked.  A positive answer is never trusted:
            # acquire the root, refresh the task and reread dependencies before
            # the claim transition so a reopening gate cannot slip between the
            # dependency check and the lease.
            if not self.dependencies_satisfied(task):
                continue
            project = lock_project_root(self.db, task.project_id)
            if project is None or not project.accepts_work:
                continue
            task = self.db.execute(
                select(Task)
                .where(Task.id == task.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            ).scalar_one()
            if (
                task.status is not TaskStatus.QUEUED
                or task.locked_by is not None
                or (task.available_at is not None and task.available_at > now)
                or not self.dependencies_satisfied(task)
            ):
                continue

            lease_until = now + timedelta(seconds=self.settings.task_lease_seconds)
            result = self.db.execute(
                update(Task)
                .where(
                    Task.id == task.id,
                    Task.status == TaskStatus.QUEUED,
                    Task.locked_by.is_(None),
                )
                .values(
                    status=TaskStatus.RUNNING,
                    locked_by=worker_id,
                    locked_at=now,
                    lease_expires_at=lease_until,
                    started_at=task.started_at or now,
                    attempts=Task.attempts + 1,
                )
            )
            if int(getattr(result, "rowcount", 0) or 0) == 0:
                continue  # another worker got there first

            self.db.refresh(task)
            claimed.append(task)

        return claimed

    def dependencies_satisfied(self, task: Task) -> bool:
        """Whether nothing is still holding this task back."""
        return not self.blocking_dependencies(task)

    def blocking_dependencies(self, task: Task) -> list[Task]:
        """Dependencies that still prevent this task from running.

        ``TaskDependency.hard`` matters here, and getting it wrong stalls a run
        forever. The evidence-graph task depends *softly* on every parse task
        precisely so that one unreadable document does not stop the review of the
        rest — so a soft dependency that has reached a terminal state, whatever
        that state is, stops blocking. The graph then records the file as a
        limitation, which is the honest outcome.

        A hard dependency that failed or was cancelled also stops *blocking*: it
        will never complete, so waiting on it is waiting forever. It is surfaced
        by :meth:`failed_hard_dependencies` instead, which the worker turns into
        a failed task with a reason rather than a task that sits queued and
        silent while the project still reports itself as running.
        """
        blocking: list[Task] = []
        for _, upstream in self._dependency_rows(task):
            if upstream.status is TaskStatus.COMPLETED:
                continue
            if upstream.is_terminal:
                # Failed or cancelled: it is never going to complete, so it
                # cannot be a reason to keep waiting.
                continue
            blocking.append(upstream)
        return blocking

    def failed_hard_dependencies(self, task: Task) -> list[Task]:
        """Hard dependencies that ended without completing.

        A task whose hard prerequisite failed cannot do its job, so it is failed
        with that reason rather than run against missing input or left queued.
        """
        return [
            upstream
            for dependency, upstream in self._dependency_rows(task)
            if dependency.hard and upstream.status in {TaskStatus.FAILED, TaskStatus.CANCELLED}
        ]

    def incomplete_hard_dependencies(self, task: Task) -> list[Task]:
        """Authoritative hard prerequisites that are no longer complete."""
        return [
            upstream
            for dependency, upstream in self._dependency_rows(task)
            if dependency.hard and upstream.status is not TaskStatus.COMPLETED
        ]

    def _dependency_rows(self, task: Task) -> list[tuple[TaskDependency, Task]]:
        return [
            (dependency, upstream)
            for dependency, upstream in self.db.execute(
                select(TaskDependency, Task)
                .join(Task, Task.id == TaskDependency.depends_on_id)
                .where(TaskDependency.task_id == task.id)
                .order_by(TaskDependency.id)
                .execution_options(populate_existing=True)
            ).all()
        ]

    def heartbeat(self, task: Task) -> None:
        """Extend a lease for a handler that is still working.

        Long parses would otherwise look like a crashed worker and be requeued
        underneath themselves.
        """
        task.lease_expires_at = utcnow() + timedelta(seconds=self.settings.task_lease_seconds)
        self.db.flush()

    # -- finishing -------------------------------------------------------
    def acquire_execution(self, task: Task, *, worker_id: str, attempt: int) -> bool:
        """Revalidate a claimed attempt immediately before any side effect.

        Claiming and handler dispatch deliberately use different transactions.
        A cancellation or lease sweep can win in that gap, while the dispatch
        Session still holds a stale ``RUNNING`` identity.  This conditional
        no-op update both refreshes that identity and proves that this exact
        worker attempt still owns a live lease before presence, events or the
        handler are allowed to run.
        """
        now = utcnow()
        transition = self.db.execute(
            update(Task)
            .where(
                Task.id == task.id,
                Task.status == TaskStatus.RUNNING,
                Task.locked_by == worker_id,
                Task.attempts == attempt,
                Task.lease_expires_at.isnot(None),
                Task.lease_expires_at >= now,
            )
            .values(locked_by=worker_id)
            .execution_options(synchronize_session=False)
        )
        won = int(getattr(transition, "rowcount", 0) or 0) == 1
        self.db.refresh(task)
        return won

    def acquire_finalization(self, task: Task, *, worker_id: str, attempt: int) -> bool:
        """Atomically claim the right to publish one handler outcome.

        The worker intentionally commits ``task.started`` before running the
        handler and the Session keeps ORM instances live across that commit. A
        cancellation or lease recovery can therefore replace this attempt while
        the handler is working. This compare-and-set is a real ``UPDATE`` rather
        than ``SELECT FOR UPDATE`` so it also serialises writers on SQLite,
        where row-locking selects are ignored.
        """
        transition = self.db.execute(
            update(Task)
            .where(
                Task.id == task.id,
                Task.status == TaskStatus.RUNNING,
                Task.locked_by == worker_id,
                Task.attempts == attempt,
            )
            # A no-op value change acquires the write lock without deciding the
            # outcome. The existing helpers below remain the single source of
            # retry/backoff and terminal-state semantics.
            .values(locked_by=worker_id)
            .execution_options(synchronize_session=False)
        )
        won = int(getattr(transition, "rowcount", 0) or 0) == 1
        self.db.refresh(task)
        return won

    def requeue_claim_after_dependency_change(
        self,
        task: Task,
        *,
        worker_id: str,
        attempt: int,
        reason: str,
    ) -> bool:
        """Discard a claimed attempt whose hard prerequisite reopened."""
        transition = self.db.execute(
            update(Task)
            .where(
                Task.id == task.id,
                Task.status == TaskStatus.RUNNING,
                Task.locked_by == worker_id,
                Task.attempts == attempt,
            )
            .values(
                status=TaskStatus.QUEUED,
                available_at=None,
                finished_at=None,
                blocked_reason="",
                error_code="dependency_reopened",
                error_message=reason,
                locked_by=None,
                locked_at=None,
                lease_expires_at=None,
            )
            .execution_options(synchronize_session=False)
        )
        changed = int(getattr(transition, "rowcount", 0) or 0) == 1
        self.db.refresh(task)
        return changed

    def complete(self, task: Task, result: dict[str, Any] | None = None) -> None:
        task.status = TaskStatus.COMPLETED
        task.result = result or {}
        task.finished_at = utcnow()
        task.error_code = ""
        task.error_message = ""
        task.blocked_reason = ""
        self._release(task)

    def fail(self, task: Task, *, code: str, message: str, retry: bool = True) -> None:
        """Fail a task, scheduling a retry when attempts remain.

        Backoff is exponential from ``task_retry_base_delay_seconds``. A task out
        of attempts stays failed with the reason recorded — never silently
        dropped, because a task nobody can see failed is worse than one that did.
        """
        task.error_code = code
        task.error_message = message

        if retry and task.attempts < task.max_attempts:
            delay = self.settings.task_retry_base_delay_seconds * (2 ** (task.attempts - 1))
            task.status = TaskStatus.QUEUED
            task.available_at = utcnow() + timedelta(seconds=delay)
            self._release(task)
            logger.info(
                "task will retry",
                extra={
                    "task_id": str(task.id),
                    "kind": str(task.kind),
                    "attempt": task.attempts,
                    "max_attempts": task.max_attempts,
                    "retry_in_seconds": round(delay, 1),
                    "error_code": code,
                },
            )
            return

        task.status = TaskStatus.FAILED
        task.finished_at = utcnow()
        self._release(task)
        logger.warning(
            "task failed permanently",
            extra={
                "task_id": str(task.id),
                "kind": str(task.kind),
                "attempts": task.attempts,
                "error_code": code,
                "detail": message,
            },
        )

    def block(self, task: Task, reason: str) -> None:
        """Park a task that cannot proceed, with a reason a human can act on."""
        task.status = TaskStatus.BLOCKED
        task.blocked_reason = reason
        self._release(task)

    def await_approval(self, task: Task, reason: str = "") -> None:
        task.status = TaskStatus.AWAITING_APPROVAL
        task.blocked_reason = reason
        self._release(task)

    def cancel(self, task: Task, reason: str = "") -> bool:
        """Cancel a nonterminal task without overwriting a concurrent finish."""
        transition = self.db.execute(
            update(Task)
            .where(
                Task.id == task.id,
                Task.status.notin_([TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED]),
            )
            .values(
                status=TaskStatus.CANCELLED,
                blocked_reason=reason,
                finished_at=utcnow(),
                locked_by=None,
                locked_at=None,
                lease_expires_at=None,
            )
            .execution_options(synchronize_session=False)
        )
        changed = int(getattr(transition, "rowcount", 0) or 0) == 1
        self.db.refresh(task)
        return changed

    def unblock(self, task: Task) -> None:
        """Return a blocked or approval-gated task to the queue."""
        if task.status not in {TaskStatus.BLOCKED, TaskStatus.AWAITING_APPROVAL}:
            return
        task.status = TaskStatus.QUEUED
        task.blocked_reason = ""
        task.available_at = None
        self._release(task)

    def _release(self, task: Task) -> None:
        task.locked_by = None
        task.locked_at = None
        task.lease_expires_at = None
        self.db.flush()

    # -- recovery --------------------------------------------------------
    def requeue_expired(self) -> int:
        """Requeue tasks whose lease has expired.

        This is what makes a crash recoverable: a worker that died mid-task left
        a lease behind, and the task returns to the queue rather than sitting in
        ``running`` forever. Runs at startup and periodically.

        The attempt count is *not* incremented — the attempt was already counted
        at claim time, so a crash costs one attempt, not two.
        """
        now = utcnow()
        candidates = list(
            self.db.execute(
                select(Task).where(
                    Task.status == TaskStatus.RUNNING,
                    Task.lease_expires_at.isnot(None),
                    Task.lease_expires_at < now,
                )
            ).scalars()
        )

        expired: list[Task] = []
        for task in candidates:
            values: dict[str, Any]
            if task.attempts >= task.max_attempts:
                values = {
                    "status": TaskStatus.FAILED,
                    "finished_at": now,
                    "error_code": "lease_expired",
                    "error_message": (
                        "The worker holding this task stopped responding and no attempts remain."
                    ),
                }
            else:
                values = {
                    "status": TaskStatus.QUEUED,
                    "available_at": now,
                    "error_code": "lease_expired",
                    "error_message": (
                        "The worker holding this task stopped responding; it was requeued."
                    ),
                }
            transition = self.db.execute(
                update(Task)
                .where(
                    Task.id == task.id,
                    Task.status == TaskStatus.RUNNING,
                    Task.locked_by == task.locked_by,
                    Task.attempts == task.attempts,
                    Task.lease_expires_at == task.lease_expires_at,
                    Task.lease_expires_at < now,
                )
                .values(
                    **values,
                    locked_by=None,
                    locked_at=None,
                    lease_expires_at=None,
                )
                .execution_options(synchronize_session=False)
            )
            if int(getattr(transition, "rowcount", 0) or 0) != 1:
                self.db.refresh(task)
                continue
            self.db.refresh(task)
            expired.append(task)

        if expired:
            logger.warning(
                "requeued tasks abandoned by a stopped worker",
                extra={"count": len(expired), "task_ids": [str(t.id) for t in expired]},
            )
        return len(expired)

    # -- queries ---------------------------------------------------------
    def pending_count(self, project_id: uuid.UUID) -> int:
        from sqlalchemy import func

        return int(
            self.db.execute(
                select(func.count(Task.id)).where(
                    Task.project_id == project_id,
                    Task.status.in_(
                        [
                            TaskStatus.QUEUED,
                            TaskStatus.RUNNING,
                            TaskStatus.BLOCKED,
                            TaskStatus.AWAITING_APPROVAL,
                        ]
                    ),
                )
            ).scalar()
            or 0
        )

    def status_counts(self, project_id: uuid.UUID) -> dict[str, int]:
        from sqlalchemy import func

        rows = self.db.execute(
            select(Task.status, func.count(Task.id))
            .where(Task.project_id == project_id)
            .group_by(Task.status)
        ).all()
        counts = {status.value: 0 for status in TaskStatus}
        for status, count in rows:
            counts[str(status)] = int(count)
        return counts
