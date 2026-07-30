"""Task handlers.

A handler does one unit of workflow work. It receives a
:class:`TaskContext` — a session, the task, the project and settings — and
returns a :class:`HandlerResult` saying what happened.

Handlers must be **idempotent**. The queue is at-least-once, so a handler can run
twice for the same task after a crash. Two defences: ``Task.idempotency_key``
stops duplicate tasks being created at all, and each handler is written so that
repeating its work converges rather than duplicating.

A handler never commits. The worker owns the transaction, so the work and the
task's new state land together or not at all.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.enums import EventType, TaskKind
from app.core.errors import AppError
from app.core.logging import get_logger
from app.models.project import Project
from app.models.task import Task

logger = get_logger(__name__)


@dataclass(slots=True)
class TaskContext:
    """Everything a handler is given."""

    db: Session
    task: Task
    project: Project
    settings: Settings

    @property
    def payload(self) -> dict[str, Any]:
        return self.task.payload

    def checkpoint(self, **values: Any) -> None:
        """Record partial progress so a resumed run can skip finished work.

        Long handlers (parsing twenty files) use this so a crash costs the
        remaining files, not all of them.
        """
        self.task.checkpoint = {**self.task.checkpoint, **values}
        self.db.flush()

    def done(self) -> set[str]:
        """Items this task has already processed, from its checkpoint."""
        completed = self.task.checkpoint.get("completed")
        return set(completed) if isinstance(completed, list) else set()

    def mark_done(self, item: str) -> None:
        completed = sorted(self.done() | {item})
        self.checkpoint(completed=completed)


@dataclass(slots=True)
class HandlerResult:
    """The outcome of one handler run.

    Four distinct outcomes, deliberately not collapsed into a boolean:
    completed, blocked (a human must supply something), awaiting approval, and
    failed. Reporting "blocked because no interlock schedule was uploaded" as a
    failure would tell an engineer the platform broke, when in fact it is waiting
    for them.
    """

    status: str = "completed"
    result: dict[str, Any] = field(default_factory=dict)
    #: Follow-on tasks to enqueue.
    enqueue: list[Any] = field(default_factory=list)
    reason: str = ""
    error_code: str = ""

    @classmethod
    def completed(cls, **result: Any) -> HandlerResult:
        return cls(status="completed", result=result)

    @classmethod
    def blocked(cls, reason: str, **result: Any) -> HandlerResult:
        return cls(status="blocked", reason=reason, result=result)

    @classmethod
    def awaiting_approval(cls, reason: str, **result: Any) -> HandlerResult:
        return cls(status="awaiting_approval", reason=reason, result=result)

    @classmethod
    def failed(cls, code: str, reason: str, **result: Any) -> HandlerResult:
        return cls(status="failed", error_code=code, reason=reason, result=result)


Handler = Callable[[TaskContext], HandlerResult]

_HANDLERS: dict[TaskKind, Handler] = {}


def register(kind: TaskKind) -> Callable[[Handler], Handler]:
    """Register the handler for a task kind."""

    def decorator(handler: Handler) -> Handler:
        if kind in _HANDLERS:
            raise ValueError(f"a handler is already registered for {kind.value}")
        _HANDLERS[kind] = handler
        return handler

    return decorator


def handler_for(kind: TaskKind) -> Handler | None:
    return _HANDLERS.get(kind)


def registered_kinds() -> list[TaskKind]:
    return sorted(_HANDLERS, key=lambda kind: kind.value)


def unhandled_kinds() -> list[TaskKind]:
    """Task kinds with no handler.

    Reported by the capabilities endpoint. A task of an unhandled kind blocks
    with an explanation instead of failing mysteriously — the usual cause is a
    GPT-owned delivery plugin that is not installed.
    """
    return [kind for kind in TaskKind if kind not in _HANDLERS]


def run(context: TaskContext) -> HandlerResult:
    """Dispatch a task to its handler.

    An unhandled kind blocks rather than fails: the work is not wrong, it is
    unavailable, and saying so lets an operator install the missing plugin and
    resume.
    """
    handler = handler_for(context.task.kind)
    if handler is None:
        return HandlerResult.blocked(
            f"No handler is installed for task kind '{context.task.kind.value}'. "
            "If this is a delivery task, the delivery plugin is not installed."
        )

    try:
        return handler(context)
    except AppError as exc:
        return HandlerResult.failed(exc.code, exc.message)


# ---------------------------------------------------------------------------
# Event helpers shared by handlers
# ---------------------------------------------------------------------------
def emit_task_event(
    context: TaskContext,
    event_type: EventType,
    *,
    detail: str = "",
    agent_id: uuid.UUID | None = None,
) -> None:
    from app.orchestration.events import record_event

    task = context.task
    record_event(
        context.db,
        project_id=task.project_id,
        event_type=event_type,
        task_id=task.id,
        agent_id=agent_id,
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
