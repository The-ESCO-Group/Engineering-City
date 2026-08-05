"""The persisted, leased job queue.

Everything here is about the properties the rest of the workflow depends on and
cannot verify for itself:

* enqueuing the same logical work twice is a no-op, so a replayed plan is safe;
* a claim leases a task to exactly one worker, so no task ever runs twice at
  once;
* every terminal transition *releases* the lease, because a finished task still
  holding one would be requeued by the recovery sweep and run again;
* a crash costs exactly one attempt, not two, so a genuinely flaky task still
  gets the retries it was configured for;
* a task that can never succeed ends up ``failed`` with a reason attached, and
  never silently disappears.

No wall clock is waited on anywhere: time is exercised by writing the relevant
column (a lease that already expired, a backoff that has not elapsed) rather
than by sleeping.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.core.clock import utcnow
from app.core.config import Settings
from app.core.enums import AgentKind, ProjectStatus, TaskKind, TaskStatus
from app.core.services import projects as project_service
from app.database.session import get_session_factory
from app.models.project import Project
from app.models.task import Task, TaskDependency
from app.models.user import User
from app.orchestration.queue import DatabaseTaskQueue, EnqueueRequest

# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def queue(db: Session, settings: Settings) -> DatabaseTaskQueue:
    return DatabaseTaskQueue(db, settings)


@pytest.fixture
def other_project(db: Session, seeded: dict[str, User], settings: Settings) -> Project:
    """A second project, used to prove per-project isolation."""
    created = project_service.create_project(
        db,
        name="Neighbouring Line 4",
        description="A second project that must not affect the first.",
        client_reference="TEST-002",
        attributes={},
        creator=seeded["engineer"],
        settings=settings,
    )
    db.commit()
    return created


def _request(key: str, **overrides: Any) -> EnqueueRequest:
    """A minimal, valid enqueue request with a stable key."""
    fields: dict[str, Any] = {
        "kind": TaskKind.PARSE_FILE,
        "title": f"Task {key}",
        "idempotency_key": key,
    }
    fields.update(overrides)
    return EnqueueRequest(**fields)


def _enqueue(queue: DatabaseTaskQueue, project: Project, key: str, **overrides: Any) -> Task:
    return queue.enqueue(project.id, _request(key, **overrides))


def _settle(db: Session) -> None:
    """Flush, then drop every cached attribute and collection.

    The queue's dependency check reads ``Task.dependencies``. A worker always
    claims in a session that has never seen the task before, so the tests read
    the same way: arranged state is written and then re-read from the database
    rather than asserted against whatever happens to be in the identity map.
    """
    db.flush()
    db.expire_all()


def _reload(db: Session, task: Task) -> Task:
    """Persist pending changes and read the row back."""
    task_id = task.id
    _settle(db)
    reloaded = db.get(Task, task_id)
    assert reloaded is not None
    return reloaded


def _assert_lease_released(task: Task) -> None:
    """A task holding a lease it is no longer working on would be requeued."""
    assert task.locked_by is None, f"{task.status} still names a worker"
    assert task.locked_at is None
    assert task.lease_expires_at is None


def _task_count(db: Session, project_id: uuid.UUID) -> int:
    return int(
        db.execute(select(func.count(Task.id)).where(Task.project_id == project_id)).scalar_one()
    )


# ---------------------------------------------------------------------------
# enqueue
# ---------------------------------------------------------------------------
def test_enqueue_creates_a_queued_unclaimed_task(
    queue: DatabaseTaskQueue, db: Session, project: Project
) -> None:
    run_id = uuid.uuid4()
    task = queue.enqueue(
        project.id,
        EnqueueRequest(
            kind=TaskKind.PARSE_FILE,
            title="Parse Conveyor_Line3.L5X",
            description="Extract normalised content and evidence anchors.",
            idempotency_key="run:parse:1",
            agent_kind=AgentKind.PLC_REVIEW.value,
            payload={"file_id": "abc", "force": False},
            priority=20,
            max_attempts=4,
            run_id=run_id,
        ),
    )

    stored = _reload(db, task)
    assert stored.status is TaskStatus.QUEUED
    assert stored.kind is TaskKind.PARSE_FILE
    assert stored.project_id == project.id
    assert stored.run_id == run_id
    assert stored.agent_kind == AgentKind.PLC_REVIEW.value
    assert stored.payload == {"file_id": "abc", "force": False}
    assert stored.priority == 20
    assert stored.max_attempts == 4
    assert stored.attempts == 0
    assert stored.available_at is None, "a fresh task is runnable immediately"
    _assert_lease_released(stored)


def test_enqueuing_the_same_key_twice_returns_the_existing_task(
    queue: DatabaseTaskQueue, db: Session, project: Project
) -> None:
    """A replayed plan must not double the work it already created."""
    first = _enqueue(queue, project, "run:plan", title="Plan the review")
    second = _enqueue(queue, project, "run:plan", title="Plan the review (replayed)")

    assert second.id == first.id
    assert second.title == "Plan the review", "the original row wins, not the replay"
    assert _task_count(db, project.id) == 1

    _settle(db)
    assert _task_count(db, project.id) == 1


def test_losing_an_enqueue_uniqueness_race_preserves_the_callers_transaction(
    db: Session,
    project: Project,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The duplicate INSERT must fail inside, not before, its savepoint."""
    db.commit()
    factory = get_session_factory()
    loser = factory()
    winner = factory()
    try:
        loser_project = loser.get(Project, project.id)
        assert loser_project is not None
        loser_project.description = "This caller-owned edit must survive the enqueue race."

        original_begin_nested = loser.begin_nested
        winner_inserted = False

        def begin_nested_after_winner() -> Any:
            nonlocal winner_inserted
            if not winner_inserted:
                winner_inserted = True
                DatabaseTaskQueue(winner, settings).enqueue(
                    project.id, _request("race:one", title="The winning task")
                )
                winner.commit()
            return original_begin_nested()

        monkeypatch.setattr(loser, "begin_nested", begin_nested_after_winner)
        queue = DatabaseTaskQueue(loser, settings)
        duplicate = queue.enqueue(
            project.id, _request("race:one", title="The losing duplicate")
        )
        follow_up = queue.enqueue(project.id, _request("race:two"))
        loser.commit()

        assert duplicate.title == "The winning task"
        assert follow_up.id != duplicate.id
    finally:
        loser.close()
        winner.close()

    db.expire_all()
    stored_project = db.get(Project, project.id)
    assert stored_project is not None
    assert stored_project.description == "This caller-owned edit must survive the enqueue race."
    assert db.scalar(
        select(func.count(Task.id)).where(
            Task.project_id == project.id,
            Task.idempotency_key.in_(["race:one", "race:two"]),
        )
    ) == 2


def test_a_different_idempotency_key_creates_a_second_task(
    queue: DatabaseTaskQueue, db: Session, project: Project
) -> None:
    first = _enqueue(queue, project, "run:parse:1")
    second = _enqueue(queue, project, "run:parse:2")

    assert second.id != first.id
    _settle(db)
    assert _task_count(db, project.id) == 2


def test_the_same_key_in_another_project_is_a_separate_task(
    queue: DatabaseTaskQueue, db: Session, project: Project, other_project: Project
) -> None:
    """Idempotency is scoped per project; two runs must not collide across them."""
    mine = _enqueue(queue, project, "run:plan")
    theirs = _enqueue(queue, other_project, "run:plan")

    assert mine.id != theirs.id
    _settle(db)
    assert _task_count(db, project.id) == 1
    assert _task_count(db, other_project.id) == 1


def test_depends_on_records_a_dependency_row_per_upstream_task(
    queue: DatabaseTaskQueue, db: Session, project: Project
) -> None:
    first = _enqueue(queue, project, "run:parse:1")
    second = _enqueue(queue, project, "run:parse:2")
    graph = _enqueue(
        queue,
        project,
        "run:graph",
        kind=TaskKind.BUILD_EVIDENCE_GRAPH,
        depends_on=[first.id, second.id],
    )
    graph_id = graph.id
    upstream_ids = {first.id, second.id}
    _settle(db)

    rows = list(
        db.execute(select(TaskDependency).where(TaskDependency.task_id == graph_id)).scalars()
    )
    assert {row.depends_on_id for row in rows} == upstream_ids
    assert all(row.hard for row in rows), "depends_on creates hard dependencies"


def test_a_task_cannot_depend_on_itself(
    queue: DatabaseTaskQueue, db: Session, project: Project
) -> None:
    """A self-dependency is a deadlock the queue could never resolve."""
    task = _enqueue(queue, project, "run:plan")
    _settle(db)

    with pytest.raises(ValueError, match="cannot depend on itself"):
        queue.add_dependency(task, task.id)

    _settle(db)
    assert int(db.execute(select(func.count(TaskDependency.id))).scalar_one()) == 0


def test_adding_the_same_dependency_twice_records_it_once(
    queue: DatabaseTaskQueue, db: Session, project: Project
) -> None:
    upstream = _enqueue(queue, project, "run:parse:1")
    downstream = _enqueue(queue, project, "run:graph", kind=TaskKind.BUILD_EVIDENCE_GRAPH)
    upstream_id = upstream.id
    downstream_id = downstream.id
    _settle(db)

    queue.add_dependency(downstream, upstream_id, hard=False)
    db.flush()
    queue.add_dependency(downstream, upstream_id, hard=False)
    _settle(db)

    rows = list(
        db.execute(select(TaskDependency).where(TaskDependency.task_id == downstream_id)).scalars()
    )
    assert len(rows) == 1
    assert rows[0].hard is False


# ---------------------------------------------------------------------------
# claim
# ---------------------------------------------------------------------------
def test_claim_leases_a_queued_task_to_the_worker(
    queue: DatabaseTaskQueue, db: Session, project: Project, settings: Settings
) -> None:
    task = _enqueue(queue, project, "run:parse:1")
    task_id = task.id
    _settle(db)

    before = utcnow()
    claimed = queue.claim("worker-one")
    assert [item.id for item in claimed] == [task_id]

    stored = _reload(db, claimed[0])
    assert stored.status is TaskStatus.RUNNING
    assert stored.locked_by == "worker-one"
    assert stored.locked_at is not None and stored.locked_at >= before
    assert stored.lease_expires_at is not None
    assert stored.lease_expires_at - stored.locked_at == timedelta(
        seconds=settings.task_lease_seconds
    )
    assert stored.attempts == 1, "the attempt is counted at claim time"
    assert stored.started_at is not None


def test_claim_returns_no_more_than_the_limit(
    queue: DatabaseTaskQueue, db: Session, project: Project
) -> None:
    for index in range(5):
        _enqueue(queue, project, f"run:parse:{index}")
    _settle(db)

    claimed = queue.claim("worker-one", limit=2)
    assert len(claimed) == 2

    _settle(db)
    still_queued = int(
        db.execute(
            select(func.count(Task.id)).where(
                Task.project_id == project.id, Task.status == TaskStatus.QUEUED
            )
        ).scalar_one()
    )
    assert still_queued == 3


def test_claim_orders_by_priority_then_by_age(
    queue: DatabaseTaskQueue, db: Session, project: Project
) -> None:
    """Lower priority number first; among equals, the task that waited longest."""
    now = utcnow()
    late = _enqueue(queue, project, "run:late", priority=50)
    early = _enqueue(queue, project, "run:early", priority=50)
    urgent = _enqueue(queue, project, "run:urgent", priority=10)
    db.flush()

    # Written explicitly so the tie-break does not depend on flush timing.
    late.created_at = now - timedelta(minutes=1)
    early.created_at = now - timedelta(minutes=5)
    urgent.created_at = now
    expected = [urgent.id, early.id, late.id]
    _settle(db)

    claimed = queue.claim("worker-one", limit=3)
    assert [item.id for item in claimed] == expected


def test_claim_skips_a_task_whose_dependency_has_not_completed(
    queue: DatabaseTaskQueue, db: Session, project: Project
) -> None:
    upstream = _enqueue(queue, project, "run:parse:1", priority=10)
    downstream = _enqueue(
        queue,
        project,
        "run:graph",
        kind=TaskKind.BUILD_EVIDENCE_GRAPH,
        priority=20,
        depends_on=[upstream.id],
    )
    upstream_id = upstream.id
    downstream_id = downstream.id
    _settle(db)

    assert [item.id for item in queue.claim("worker-one", limit=5)] == [upstream_id]

    _settle(db)
    assert queue.claim("worker-two", limit=5) == [], "the gate is still closed"
    assert db.get(Task, downstream_id).status is TaskStatus.QUEUED  # type: ignore[union-attr]


def test_claim_returns_a_dependent_task_once_its_dependency_completes(
    queue: DatabaseTaskQueue, db: Session, project: Project
) -> None:
    upstream = _enqueue(queue, project, "run:parse:1", priority=10)
    downstream = _enqueue(
        queue,
        project,
        "run:graph",
        kind=TaskKind.BUILD_EVIDENCE_GRAPH,
        priority=20,
        depends_on=[upstream.id],
    )
    downstream_id = downstream.id
    _settle(db)

    (running,) = queue.claim("worker-one", limit=5)
    queue.complete(running, {"parsed": True})
    _settle(db)

    assert [item.id for item in queue.claim("worker-two", limit=5)] == [downstream_id]


def test_claim_rereads_dependencies_after_a_gate_reopens_between_check_and_cas(
    queue: DatabaseTaskQueue,
    db: Session,
    project: Project,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    upstream = _enqueue(queue, project, "claim-race:gate", priority=10)
    upstream.status = TaskStatus.COMPLETED
    upstream.finished_at = utcnow()
    downstream = _enqueue(
        queue,
        project,
        "claim-race:dependent",
        priority=20,
        depends_on=[upstream.id],
    )
    db.commit()

    original = queue.dependencies_satisfied
    interleaved = False

    def reopen_after_first_check(task: Task) -> bool:
        nonlocal interleaved
        result = original(task)
        if task.id == downstream.id and result and not interleaved:
            interleaved = True
            with get_session_factory()() as reopening:
                reopening.execute(
                    update(Task)
                    .where(Task.id == upstream.id)
                    .values(
                        status=TaskStatus.BLOCKED,
                        blocked_reason="The approval gate reopened on a newer revision.",
                        finished_at=None,
                    )
                )
                reopening.commit()
        return result

    monkeypatch.setattr(queue, "dependencies_satisfied", reopen_after_first_check)

    assert queue.claim("worker-one", limit=1) == []
    db.expire_all()
    stored = db.get(Task, downstream.id)
    assert stored is not None
    assert stored.status is TaskStatus.QUEUED
    assert stored.locked_by is None
    assert stored.attempts == 0


def test_claim_ignores_a_task_whose_backoff_has_not_elapsed(
    queue: DatabaseTaskQueue, db: Session, project: Project
) -> None:
    task = _enqueue(queue, project, "run:parse:1")
    db.flush()
    task.available_at = utcnow() + timedelta(hours=1)
    task_id = task.id
    _settle(db)

    assert queue.claim("worker-one", limit=5) == []

    db.get(Task, task_id).available_at = utcnow() - timedelta(seconds=1)  # type: ignore[union-attr]
    _settle(db)
    assert [item.id for item in queue.claim("worker-one", limit=5)] == [task_id]


def test_claim_ignores_tasks_belonging_to_a_paused_project(
    queue: DatabaseTaskQueue, db: Session, project: Project
) -> None:
    """Pausing must stop new work starting, not merely hide it in the UI."""
    task = _enqueue(queue, project, "run:parse:1")
    task_id = task.id
    project.is_paused = True
    project.paused_at = utcnow()
    _settle(db)

    assert queue.claim("worker-one", limit=5) == []

    reloaded = db.get(Project, project.id)
    assert reloaded is not None
    reloaded.is_paused = False
    _settle(db)
    assert [item.id for item in queue.claim("worker-one", limit=5)] == [task_id]


@pytest.mark.parametrize(
    "status",
    [ProjectStatus.ARCHIVED, ProjectStatus.COMPLETED, ProjectStatus.FAILED],
    ids=["archived", "completed", "failed"],
)
def test_claim_ignores_tasks_of_a_project_that_stopped_accepting_work(
    queue: DatabaseTaskQueue, db: Session, project: Project, status: ProjectStatus
) -> None:
    _enqueue(queue, project, "run:parse:1")
    project.status = status
    _settle(db)

    assert queue.claim("worker-one", limit=5) == []


def test_a_second_worker_cannot_claim_a_leased_task(
    queue: DatabaseTaskQueue, db: Session, project: Project
) -> None:
    task = _enqueue(queue, project, "run:parse:1")
    task_id = task.id
    _settle(db)

    first = queue.claim("worker-one", limit=1)
    assert [item.id for item in first] == [task_id]

    second = queue.claim("worker-two", limit=1)
    assert second == [], "the same row was issued twice"

    stored = _reload(db, first[0])
    assert stored.locked_by == "worker-one"
    assert stored.attempts == 1, "a rejected claim must not consume an attempt"


def test_two_workers_between_them_claim_each_runnable_task_exactly_once(
    queue: DatabaseTaskQueue, db: Session, project: Project
) -> None:
    """A claim that double-issued a row would show up as a total above three."""
    runnable = [_enqueue(queue, project, f"run:parse:{index}") for index in range(3)]
    runnable_ids = {task.id for task in runnable}
    _settle(db)

    first = queue.claim("worker-one", limit=2)
    second = queue.claim("worker-two", limit=5)

    first_ids = [item.id for item in first]
    second_ids = [item.id for item in second]
    assert set(first_ids).isdisjoint(second_ids)
    assert len(first_ids) + len(second_ids) == len(runnable_ids)
    assert set(first_ids) | set(second_ids) == runnable_ids

    _settle(db)
    for task_id in first_ids:
        assert db.get(Task, task_id).locked_by == "worker-one"  # type: ignore[union-attr]
    for task_id in second_ids:
        assert db.get(Task, task_id).locked_by == "worker-two"  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Terminal and parked transitions
# ---------------------------------------------------------------------------
#: Every transition that takes a task off a worker, with the status it must
#: leave behind. All of them have to release the lease.
_FINISHERS: dict[str, tuple[Callable[[DatabaseTaskQueue, Task], None], TaskStatus]] = {
    "await_approval": (
        lambda queue, task: queue.await_approval(task, "a human must decide"),
        TaskStatus.AWAITING_APPROVAL,
    ),
    "block": (
        lambda queue, task: queue.block(task, "no handler is installed"),
        TaskStatus.BLOCKED,
    ),
    "cancel": (
        lambda queue, task: queue.cancel(task, "the run was cancelled"),
        TaskStatus.CANCELLED,
    ),
    "complete": (
        lambda queue, task: queue.complete(task, {"rungs": 12}),
        TaskStatus.COMPLETED,
    ),
    "fail": (
        lambda queue, task: queue.fail(task, code="boom", message="it broke", retry=False),
        TaskStatus.FAILED,
    ),
}


@pytest.mark.parametrize("transition", sorted(_FINISHERS))
def test_finishing_a_task_sets_its_status_and_releases_the_lease(
    queue: DatabaseTaskQueue, db: Session, project: Project, transition: str
) -> None:
    finish, expected = _FINISHERS[transition]
    _enqueue(queue, project, "run:parse:1")
    _settle(db)
    (claimed,) = queue.claim("worker-one")

    finish(queue, claimed)

    stored = _reload(db, claimed)
    assert stored.status is expected
    _assert_lease_released(stored)
    assert queue.claim("worker-two", limit=5) == [], "a finished task must not be reissued"


def test_complete_records_the_result_and_clears_previous_errors(
    queue: DatabaseTaskQueue, db: Session, project: Project
) -> None:
    _enqueue(queue, project, "run:parse:1")
    _settle(db)
    (claimed,) = queue.claim("worker-one")
    claimed.error_code = "stale"
    claimed.error_message = "from an earlier attempt"
    claimed.blocked_reason = "from an earlier attempt"

    queue.complete(claimed, {"rungs": 12})

    stored = _reload(db, claimed)
    assert stored.status is TaskStatus.COMPLETED
    assert stored.result == {"rungs": 12}
    assert stored.finished_at is not None
    assert stored.error_code == ""
    assert stored.error_message == ""
    assert stored.blocked_reason == "", "a completed task carries no stale blocker"


def test_block_records_a_reason_a_human_can_act_on(
    queue: DatabaseTaskQueue, db: Session, project: Project
) -> None:
    _enqueue(queue, project, "run:plan", kind=TaskKind.PLAN_PROJECT)
    _settle(db)
    (claimed,) = queue.claim("worker-one")

    queue.block(claimed, "No handler is installed for task kind 'plan_project'")

    stored = _reload(db, claimed)
    assert stored.status is TaskStatus.BLOCKED
    assert stored.blocked_reason == "No handler is installed for task kind 'plan_project'"
    _assert_lease_released(stored)


def test_cancel_leaves_an_already_terminal_task_untouched(
    queue: DatabaseTaskQueue, db: Session, project: Project
) -> None:
    """Cancelling a run must not retract work that already finished."""
    _enqueue(queue, project, "run:parse:1")
    _settle(db)
    (claimed,) = queue.claim("worker-one")
    queue.complete(claimed, {"rungs": 12})
    _settle(db)

    queue.cancel(claimed, "the run was cancelled")

    stored = _reload(db, claimed)
    assert stored.status is TaskStatus.COMPLETED
    assert stored.result == {"rungs": 12}
    assert stored.blocked_reason == ""


@pytest.mark.parametrize("parked", ["block", "await_approval"], ids=["blocked", "awaiting"])
def test_unblock_returns_a_parked_task_to_the_queue(
    queue: DatabaseTaskQueue, db: Session, project: Project, parked: str
) -> None:
    task = _enqueue(queue, project, "run:parse:1")
    task_id = task.id
    _settle(db)
    (claimed,) = queue.claim("worker-one")
    if parked == "block":
        queue.block(claimed, "waiting on an engineer")
    else:
        queue.await_approval(claimed, "waiting on an approval")
    # A stale lease left on a parked row must not survive the unblock.
    claimed.locked_by = "worker-one"
    claimed.locked_at = utcnow()
    claimed.lease_expires_at = utcnow() + timedelta(minutes=5)
    claimed.available_at = utcnow() + timedelta(hours=1)
    _settle(db)

    queue.unblock(db.get(Task, task_id))  # type: ignore[arg-type]

    stored = db.get(Task, task_id)
    assert stored is not None
    stored = _reload(db, stored)
    assert stored.status is TaskStatus.QUEUED
    assert stored.blocked_reason == ""
    assert stored.available_at is None, "an unblocked task is runnable at once"
    _assert_lease_released(stored)
    assert [item.id for item in queue.claim("worker-two", limit=5)] == [task_id]


def test_unblock_does_not_disturb_a_task_that_is_not_parked(
    queue: DatabaseTaskQueue, db: Session, project: Project
) -> None:
    _enqueue(queue, project, "run:parse:1")
    _settle(db)
    (claimed,) = queue.claim("worker-one")

    queue.unblock(claimed)

    stored = _reload(db, claimed)
    assert stored.status is TaskStatus.RUNNING
    assert stored.locked_by == "worker-one", "a running task keeps its lease"


# ---------------------------------------------------------------------------
# Retry and backoff
# ---------------------------------------------------------------------------
def test_fail_with_attempts_remaining_requeues_behind_a_future_backoff(
    queue: DatabaseTaskQueue, db: Session, project: Project, settings: Settings
) -> None:
    _enqueue(queue, project, "run:parse:1", max_attempts=3)
    _settle(db)
    (claimed,) = queue.claim("worker-one")
    assert claimed.attempts == 1

    before = utcnow()
    queue.fail(claimed, code="parse_error", message="the file could not be read")

    stored = _reload(db, claimed)
    assert stored.status is TaskStatus.QUEUED
    assert stored.available_at is not None
    assert stored.available_at - before >= timedelta(seconds=settings.task_retry_base_delay_seconds)
    assert stored.error_code == "parse_error"
    assert stored.error_message == "the file could not be read"
    assert stored.finished_at is None, "a task that will retry has not finished"
    _assert_lease_released(stored)
    assert queue.claim("worker-two", limit=5) == [], "the backoff has not elapsed"


def test_the_retry_backoff_grows_with_each_attempt(
    queue: DatabaseTaskQueue, db: Session, project: Project, settings: Settings
) -> None:
    """Retrying a failing task at a fixed interval is a hot loop, not a retry."""
    _enqueue(queue, project, "run:parse:1", max_attempts=6)
    _settle(db)
    (claimed,) = queue.claim("worker-one")
    base = timedelta(seconds=settings.task_retry_base_delay_seconds)

    delays: list[timedelta] = []
    for attempt in (1, 2, 3):
        claimed.attempts = attempt
        db.flush()
        before = utcnow()
        queue.fail(claimed, code="parse_error", message="the file could not be read")
        assert claimed.available_at is not None
        delays.append(claimed.available_at - before)

    assert delays[0] >= base
    assert delays[1] >= 2 * base
    assert delays[2] >= 4 * base
    assert delays[0] < delays[1] < delays[2]


def test_fail_with_no_attempts_left_fails_permanently_with_the_reason_kept(
    queue: DatabaseTaskQueue, db: Session, project: Project
) -> None:
    """A task nobody can see failed is worse than one that did."""
    _enqueue(queue, project, "run:parse:1", max_attempts=1)
    _settle(db)
    (claimed,) = queue.claim("worker-one")
    assert claimed.attempts == 1

    queue.fail(claimed, code="parse_error", message="the file could not be read")

    stored = _reload(db, claimed)
    assert stored.status is TaskStatus.FAILED
    assert stored.error_code == "parse_error"
    assert stored.error_message, "a permanent failure must record why"
    assert stored.error_message == "the file could not be read"
    assert stored.finished_at is not None
    _assert_lease_released(stored)
    assert queue.claim("worker-two", limit=5) == []


def test_fail_without_retry_gives_up_even_with_attempts_left(
    queue: DatabaseTaskQueue, db: Session, project: Project
) -> None:
    _enqueue(queue, project, "run:parse:1", max_attempts=5)
    _settle(db)
    (claimed,) = queue.claim("worker-one")

    queue.fail(claimed, code="unsupported_file", message="nothing here can read it", retry=False)

    stored = _reload(db, claimed)
    assert stored.status is TaskStatus.FAILED
    assert stored.attempts == 1
    assert stored.error_message == "nothing here can read it"


# ---------------------------------------------------------------------------
# Lease recovery
# ---------------------------------------------------------------------------
def test_requeue_expired_returns_an_abandoned_task_to_the_queue(
    queue: DatabaseTaskQueue, db: Session, project: Project
) -> None:
    _enqueue(queue, project, "run:parse:1", max_attempts=3)
    _settle(db)
    (claimed,) = queue.claim("crashed-worker")
    claimed.lease_expires_at = utcnow() - timedelta(hours=1)
    task_id = claimed.id
    _settle(db)

    assert queue.requeue_expired() == 1

    stored = db.get(Task, task_id)
    assert stored is not None
    stored = _reload(db, stored)
    assert stored.status is TaskStatus.QUEUED
    assert stored.attempts == 1, "a crash costs one attempt, not two"
    assert stored.error_code == "lease_expired"
    assert stored.error_message
    _assert_lease_released(stored)

    (reclaimed,) = queue.claim("healthy-worker")
    assert reclaimed.id == task_id
    assert reclaimed.attempts == 2, "the retry is the second attempt, not the third"


def test_requeue_expired_leaves_a_live_lease_alone(
    queue: DatabaseTaskQueue, db: Session, project: Project
) -> None:
    """Requeuing under a working handler would run the same task twice."""
    _enqueue(queue, project, "run:parse:1")
    _settle(db)
    (claimed,) = queue.claim("busy-worker")
    task_id = claimed.id
    _settle(db)

    assert queue.requeue_expired() == 0

    stored = db.get(Task, task_id)
    assert stored is not None
    assert stored.status is TaskStatus.RUNNING
    assert stored.locked_by == "busy-worker"
    assert stored.lease_expires_at is not None
    assert stored.attempts == 1
    assert stored.error_code == ""


def test_requeue_expired_ignores_a_queued_task_with_no_lease(
    queue: DatabaseTaskQueue, db: Session, project: Project
) -> None:
    task = _enqueue(queue, project, "run:parse:1")
    task_id = task.id
    _settle(db)

    assert queue.requeue_expired() == 0

    stored = db.get(Task, task_id)
    assert stored is not None
    assert stored.status is TaskStatus.QUEUED
    assert stored.attempts == 0


def test_an_expired_task_out_of_attempts_fails_instead_of_looping_forever(
    queue: DatabaseTaskQueue, db: Session, project: Project
) -> None:
    _enqueue(queue, project, "run:parse:1", max_attempts=1)
    _settle(db)
    (claimed,) = queue.claim("crashed-worker")
    claimed.lease_expires_at = utcnow() - timedelta(minutes=5)
    task_id = claimed.id
    _settle(db)

    assert queue.requeue_expired() == 1

    stored = db.get(Task, task_id)
    assert stored is not None
    stored = _reload(db, stored)
    assert stored.status is TaskStatus.FAILED
    assert stored.error_code == "lease_expired"
    assert stored.error_message, "the failure must explain itself"
    assert stored.finished_at is not None
    assert stored.attempts == 1
    _assert_lease_released(stored)

    assert queue.claim("healthy-worker", limit=5) == []
    assert queue.requeue_expired() == 0, "a failed task must not be swept again"


def test_requeue_expired_sweeps_only_the_tasks_whose_lease_ran_out(
    queue: DatabaseTaskQueue, db: Session, project: Project
) -> None:
    for index in range(3):
        _enqueue(queue, project, f"run:parse:{index}")
    _settle(db)
    claimed = queue.claim("crashed-worker", limit=3)
    assert len(claimed) == 3
    abandoned = {claimed[0].id, claimed[2].id}
    survivor = claimed[1].id
    claimed[0].lease_expires_at = utcnow() - timedelta(hours=1)
    claimed[2].lease_expires_at = utcnow() - timedelta(seconds=1)
    _settle(db)

    assert queue.requeue_expired() == 2

    _settle(db)
    requeued = {
        task.id
        for task in db.execute(
            select(Task).where(Task.project_id == project.id, Task.status == TaskStatus.QUEUED)
        ).scalars()
    }
    assert requeued == abandoned
    assert db.get(Task, survivor).status is TaskStatus.RUNNING  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------
def test_status_counts_reports_every_status_including_the_empty_ones(
    queue: DatabaseTaskQueue, db: Session, project: Project
) -> None:
    for index in range(5):
        _enqueue(queue, project, f"run:parse:{index}")
    _settle(db)
    claimed = queue.claim("worker-one", limit=4)
    assert len(claimed) == 4

    queue.complete(claimed[0], {"rungs": 12})
    queue.fail(claimed[1], code="parse_error", message="unreadable", retry=False)
    queue.block(claimed[2], "waiting on an engineer")
    queue.await_approval(claimed[3], "waiting on an approval")
    _settle(db)

    assert queue.status_counts(project.id) == {
        TaskStatus.QUEUED.value: 1,
        TaskStatus.RUNNING.value: 0,
        TaskStatus.BLOCKED.value: 1,
        TaskStatus.AWAITING_APPROVAL.value: 1,
        TaskStatus.COMPLETED.value: 1,
        TaskStatus.FAILED.value: 1,
        TaskStatus.CANCELLED.value: 0,
    }


def test_status_counts_of_a_project_with_no_tasks_is_all_zero(
    queue: DatabaseTaskQueue, project: Project
) -> None:
    assert queue.status_counts(project.id) == {status.value: 0 for status in TaskStatus}
    assert queue.pending_count(project.id) == 0


def test_pending_count_counts_unfinished_work_only(
    queue: DatabaseTaskQueue, db: Session, project: Project
) -> None:
    """Queued, running, blocked and awaiting-approval are all still outstanding."""
    for index in range(6):
        _enqueue(queue, project, f"run:parse:{index}")
    _settle(db)
    assert queue.pending_count(project.id) == 6

    claimed = queue.claim("worker-one", limit=5)
    assert len(claimed) == 5
    _settle(db)
    assert queue.pending_count(project.id) == 6, "running work is still pending"

    queue.complete(claimed[0], {})
    queue.fail(claimed[1], code="parse_error", message="unreadable", retry=False)
    queue.cancel(claimed[2], "the run was cancelled")
    _settle(db)
    assert queue.pending_count(project.id) == 3

    queue.block(claimed[3], "waiting on an engineer")
    queue.await_approval(claimed[4], "waiting on an approval")
    _settle(db)
    assert queue.pending_count(project.id) == 3, "parked work is still pending"


def test_counts_are_scoped_to_one_project(
    queue: DatabaseTaskQueue, db: Session, project: Project, other_project: Project
) -> None:
    _enqueue(queue, project, "run:parse:1")
    _enqueue(queue, other_project, "run:parse:1")
    _enqueue(queue, other_project, "run:parse:2")
    _settle(db)

    assert queue.pending_count(project.id) == 1
    assert queue.pending_count(other_project.id) == 2
    assert queue.status_counts(project.id)[TaskStatus.QUEUED.value] == 1
    assert queue.status_counts(other_project.id)[TaskStatus.QUEUED.value] == 2
