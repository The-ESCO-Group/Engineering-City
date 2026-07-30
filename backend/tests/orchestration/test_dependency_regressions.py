"""Dependency handling that used to stall a run forever.

An adversarial review of the workflow engine found three ways a run could stop
making progress while the project still reported itself as ``running`` — no
error, no blocked task, nothing for an operator to act on. That is the worst
shape a failure can take here: an engineer waiting on a review that will never
finish and has not said so.

Each test below fails against the original implementation.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.enums import ProjectStatus, TaskKind, TaskStatus
from app.core.services import parsing
from app.models.project import Project
from app.models.task import Task
from app.models.user import User
from app.orchestration import engine, worker
from app.orchestration.queue import DatabaseTaskQueue, EnqueueRequest


def _queue(db: Session, settings: Settings) -> DatabaseTaskQueue:
    return DatabaseTaskQueue(db, settings)


def _enqueue(db: Session, settings: Settings, project: Project, key: str, **kwargs: object) -> Task:
    return _queue(db, settings).enqueue(
        project.id,
        EnqueueRequest(
            kind=TaskKind.PARSE_FILE,
            title=key,
            idempotency_key=key,
            **kwargs,  # type: ignore[arg-type]
        ),
    )


# ---------------------------------------------------------------------------
# A soft dependency that failed must stop blocking.
# ---------------------------------------------------------------------------
def test_a_failed_soft_dependency_stops_blocking(
    db: Session, project: Project, settings: Settings
) -> None:
    """The graph task depends softly on the parses precisely so that one
    unreadable document does not stop the review of the rest. Honouring the flag
    is what makes that true rather than merely intended."""
    upstream = _enqueue(db, settings, project, "upstream", max_attempts=1)
    downstream = _enqueue(db, settings, project, "downstream")
    queue = _queue(db, settings)
    queue.add_dependency(downstream, upstream.id, hard=False)
    db.commit()

    claimed = queue.claim("worker-1", limit=1)
    assert [task.id for task in claimed] == [upstream.id]
    queue.fail(upstream, code="parse_failed", message="unreadable", retry=False)
    db.commit()
    assert upstream.status is TaskStatus.FAILED

    db.expire_all()
    runnable = queue.claim("worker-2", limit=5)
    assert downstream.id in {task.id for task in runnable}, (
        "a soft dependency that will never complete must stop blocking"
    )


def test_a_cancelled_soft_dependency_stops_blocking(
    db: Session, project: Project, settings: Settings
) -> None:
    upstream = _enqueue(db, settings, project, "upstream")
    downstream = _enqueue(db, settings, project, "downstream")
    queue = _queue(db, settings)
    queue.add_dependency(downstream, upstream.id, hard=False)
    queue.cancel(upstream, "operator cancelled it")
    db.commit()

    db.expire_all()
    runnable = queue.claim("worker-1", limit=5)
    assert downstream.id in {task.id for task in runnable}


def test_a_run_survives_one_file_that_cannot_be_parsed(
    db: Session, ingested_project: Project, seeded: dict[str, User], settings: Settings
) -> None:
    """End to end: the graph must still be built from what could be read.

    Previously a single failed parse left the graph task queued forever, and with
    it the plan, senior review, approvals and finalise — while the project sat at
    ``running`` with nothing to show an operator.
    """
    engine.start_run(db, ingested_project, settings, actor=seeded["engineer"])
    db.commit()

    parse_task = (
        db.query(Task)
        .filter(Task.project_id == ingested_project.id, Task.kind == TaskKind.PARSE_FILE)
        .first()
    )
    assert parse_task is not None
    parse_task.status = TaskStatus.FAILED
    parse_task.error_message = "simulated unreadable document"
    db.commit()

    worker.drain(settings)

    db.expire_all()
    graph_task = (
        db.query(Task)
        .filter(
            Task.project_id == ingested_project.id,
            Task.kind == TaskKind.BUILD_EVIDENCE_GRAPH,
        )
        .one()
    )
    assert graph_task.status is TaskStatus.COMPLETED, (
        f"the evidence graph should still be built; it is {graph_task.status.value}"
    )

    # And the run reached the point where it legitimately stops today.
    plan_task = (
        db.query(Task)
        .filter(Task.project_id == ingested_project.id, Task.kind == TaskKind.PLAN_PROJECT)
        .one()
    )
    assert plan_task.status is not TaskStatus.QUEUED


# ---------------------------------------------------------------------------
# A hard dependency that failed must fail its dependants, not silence them.
# ---------------------------------------------------------------------------
def test_a_failed_hard_dependency_fails_the_dependent_task(
    db: Session, project: Project, settings: Settings
) -> None:
    """Waiting on something that can never complete is waiting forever.

    The dependent task cannot do its job, so it fails with a reason naming the
    prerequisite — an operator gets a signal instead of a silent stall.
    """
    upstream = _enqueue(db, settings, project, "upstream", max_attempts=1)
    downstream = _enqueue(db, settings, project, "downstream")
    queue = _queue(db, settings)
    queue.add_dependency(downstream, upstream.id, hard=True)
    db.commit()

    queue.claim("worker-1", limit=1)
    queue.fail(upstream, code="boom", message="it failed", retry=False)
    db.commit()

    worker.drain(settings)

    db.expire_all()
    db.refresh(downstream)
    assert downstream.status is TaskStatus.FAILED
    assert downstream.error_code == "dependency_failed"
    assert "upstream" in downstream.error_message


def test_a_stalled_run_does_not_sit_at_running_forever(
    db: Session, project: Project, settings: Settings
) -> None:
    """Whatever happens, the project must reach a status that says something."""
    upstream = _enqueue(db, settings, project, "upstream", max_attempts=1)
    downstream = _enqueue(db, settings, project, "downstream")
    queue = _queue(db, settings)
    queue.add_dependency(downstream, upstream.id, hard=True)
    project.status = ProjectStatus.RUNNING
    db.commit()

    queue.claim("worker-1", limit=1)
    queue.fail(upstream, code="boom", message="it failed", retry=False)
    db.commit()

    worker.drain(settings)

    db.expire_all()
    db.refresh(project)
    assert project.status is ProjectStatus.FAILED
    assert project.failure_reason


# ---------------------------------------------------------------------------
# A blocked task must not hide a runnable one.
# ---------------------------------------------------------------------------
def test_blocked_tasks_do_not_starve_a_runnable_one(
    db: Session, project: Project, settings: Settings
) -> None:
    """The claim window used to be sized from ``limit``.

    A run of dependency-blocked tasks at the front of the priority order filled
    it completely, so the runnable task behind them was never examined and the
    worker idled with work outstanding.
    """
    queue = _queue(db, settings)
    gate = _enqueue(db, settings, project, "gate", priority=1)
    for index in range(8):
        waiter = _enqueue(db, settings, project, f"waiter-{index}", priority=10 + index)
        queue.add_dependency(waiter, gate.id)
    independent = _enqueue(db, settings, project, "independent", priority=50)
    db.commit()

    claimed = queue.claim("worker-1", limit=1)
    assert [task.id for task in claimed] == [gate.id]
    db.commit()

    db.expire_all()
    next_claim = queue.claim("worker-2", limit=1)
    assert [task.id for task in next_claim] == [independent.id], (
        "a runnable task must not be hidden behind dependency-blocked ones"
    )


def test_a_starved_task_is_eventually_reached_by_the_worker(
    db: Session, project: Project, settings: Settings
) -> None:
    """Every task must get its turn, whatever sits in front of it.

    Asserted on attempt counts rather than on final status: these tasks carry no
    payload, so their handler legitimately fails and schedules a retry. What
    matters is that the independent task was *claimed at all* — under the old
    ``limit * 4`` window it never was, and the worker idled with it queued.
    """
    queue = _queue(db, settings)
    gate = _enqueue(db, settings, project, "gate", priority=1)
    for index in range(8):
        waiter = _enqueue(db, settings, project, f"waiter-{index}", priority=10 + index)
        queue.add_dependency(waiter, gate.id)
    independent = _enqueue(db, settings, project, "independent", priority=50)
    db.commit()

    worker.drain(settings)

    db.expire_all()
    db.refresh(gate)
    db.refresh(independent)
    assert gate.attempts >= 1
    assert independent.attempts >= 1, (
        "the independent task was never claimed; it was hidden behind the "
        "dependency-blocked tasks ahead of it in the priority order"
    )


# ---------------------------------------------------------------------------
# Enqueue must not discard the caller's transaction.
# ---------------------------------------------------------------------------
def test_a_duplicate_enqueue_keeps_the_callers_earlier_writes(
    db: Session, project: Project, settings: Settings
) -> None:
    """The uniqueness-race recovery used to call ``db.rollback()``.

    Inside ``start_run`` that would silently discard every task, dependency,
    audit row and event created so far, and the function would carry on wiring
    dependencies to ids that no longer existed.
    """
    queue = _queue(db, settings)
    first = _enqueue(db, settings, project, "shared-key")
    db.commit()

    earlier = _enqueue(db, settings, project, "earlier-in-the-same-transaction")
    # The same key again: enqueue returns the existing row rather than inserting.
    again = queue.enqueue(
        project.id,
        EnqueueRequest(kind=TaskKind.PARSE_FILE, title="shared-key", idempotency_key="shared-key"),
    )
    assert again.id == first.id

    db.commit()
    db.expire_all()
    assert db.get(Task, earlier.id) is not None, (
        "the caller's earlier write was discarded by the duplicate recovery"
    )


# ---------------------------------------------------------------------------
# Handlers must be registered wherever the package is imported.
# ---------------------------------------------------------------------------
def test_handlers_are_registered_by_importing_the_package() -> None:
    """Registration used to happen only when an in-process worker was enabled.

    An API process paired with an external worker, or a CLI drain, would then
    report every task kind as unhandled and block the entire run.
    """
    from app.orchestration import handlers

    for kind in (
        TaskKind.INVENTORY_FILES,
        TaskKind.PARSE_FILE,
        TaskKind.BUILD_EVIDENCE_GRAPH,
        TaskKind.FINALIZE_PROJECT,
    ):
        assert handlers.handler_for(kind) is not None, f"{kind.value} has no handler"


def test_a_blocked_task_keeps_the_handlers_structured_detail(
    db: Session, project: Project, seeded: dict[str, User], settings: Settings
) -> None:
    """The reason is for a human; the payload is for the UI. Both must survive.

    Only ``blocked_reason`` used to be persisted, so the machine-readable detail
    a handler attached to a block was thrown away.
    """
    queue = _queue(db, settings)
    task = queue.enqueue(
        project.id,
        EnqueueRequest(
            kind=TaskKind.INVENTORY_FILES,
            title="Inventory",
            idempotency_key="inventory-only",
        ),
    )
    db.commit()
    assert parsing.parseable_files(db, project.id) == []

    worker.drain(settings)

    db.expire_all()
    db.refresh(task)
    assert task.status is TaskStatus.BLOCKED
    assert task.blocked_reason
    assert task.result, "the handler's structured result was discarded"
    assert "file_count" in task.result
