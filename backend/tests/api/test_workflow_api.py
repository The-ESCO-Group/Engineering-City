"""Workflow control, task inspection and the event stream, over HTTP and the socket.

These tests drive the queue by hand — ``settings.worker_enabled`` is false and the
app fixture builds the API with no worker thread — so every assertion is about a
state the test itself caused, not one a background thread happened to reach.

The agent task kinds (``plan_project`` onwards) have no handler yet, so a drained
run legitimately stops at the plan with a "no handler is installed" reason. That
is asserted as the current, correct behaviour of the *block* path; nothing here
asserts a total task count that the next milestone would move.
"""

from __future__ import annotations

import uuid
from collections import Counter
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.websockets import WebSocketDisconnect

# Importing the step module is what registers the deterministic handlers; the
# app fixture never starts a worker, so nothing else does it.
import app.orchestration.steps  # noqa: F401
from app.core.clock import utcnow
from app.core.config import Settings, get_settings
from app.core.enums import EventType, ProjectStatus, TaskKind, TaskStatus
from app.models.project import Project
from app.models.task import Task
from app.orchestration import worker
from app.orchestration.events import record_event
from tests.conftest import AuthedClient

WORKER_ID = "test-worker"


@pytest.fixture(autouse=True)
def _process_settings(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Make the process-wide settings agree with the fixture settings.

    The WebSocket route resolves its configuration with ``get_settings()`` rather
    than through the dependency the app overrides, so without this it would verify
    tokens against a different signing secret than the one that minted them. See
    the reported defect; this fixture only makes the two agree, it does not weaken
    any assertion.
    """
    monkeypatch.setenv("ESCO_JWT_SECRET", settings.jwt_secret.get_secret_value())
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _start_run(client: AuthedClient, project_id: uuid.UUID, **payload: Any) -> dict[str, Any]:
    response = client.post(f"/api/projects/{project_id}/runs", json=payload)
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


def _tasks(db: Session, project_id: uuid.UUID) -> list[Task]:
    """Task rows as the API's own session committed them.

    The rollback ends this session's read snapshot and the expiry drops the
    identity map, so rows the API rewrote in its own session are re-read rather
    than served stale from here (the factory sets ``expire_on_commit=False``).
    """
    db.rollback()
    db.expire_all()
    return list(
        db.execute(
            select(Task)
            .where(Task.project_id == project_id)
            .order_by(Task.priority, Task.created_at)
        ).scalars()
    )


def _task_of_kind(db: Session, project_id: uuid.UUID, kind: TaskKind) -> Task:
    matches = [task for task in _tasks(db, project_id) if task.kind is kind]
    assert len(matches) == 1, f"expected exactly one {kind.value} task, got {len(matches)}"
    return matches[0]


def _drain(settings: Settings) -> worker.TickResult:
    return worker.drain(settings)


# ---------------------------------------------------------------------------
# POST /api/projects/{id}/runs
# ---------------------------------------------------------------------------
def test_starting_a_run_returns_the_task_graph_it_created(
    engineer_client: AuthedClient, ingested_project: Project, db: Session
) -> None:
    body = _start_run(engineer_client, ingested_project.id, note="first pass")

    assert body["project_id"] == str(ingested_project.id)
    assert body["status"] == ProjectStatus.RUNNING
    assert body["task_count"] == len(body["tasks"])
    assert body["task_count"] > 0

    run_id = body["run_id"]
    uuid.UUID(run_id)
    assert {task["run_id"] for task in body["tasks"]} == {run_id}
    assert {task["status"] for task in body["tasks"]} == {TaskStatus.QUEUED}

    kinds = [task["kind"] for task in body["tasks"]]
    assert kinds[0] == TaskKind.INVENTORY_FILES, "intake has to be the first task"
    assert TaskKind.PLAN_PROJECT in kinds

    persisted = _tasks(db, ingested_project.id)
    assert len(persisted) == body["task_count"]
    assert {str(task.id) for task in persisted} == {task["id"] for task in body["tasks"]}


def test_one_parse_task_is_created_per_analysable_file(
    engineer_client: AuthedClient, ingested_project: Project, db: Session
) -> None:
    """One task per file is what stops an unreadable document costing the rest."""
    body = _start_run(engineer_client, ingested_project.id)

    parse_tasks = [
        task for task in _tasks(db, ingested_project.id) if task.kind is TaskKind.PARSE_FILE
    ]
    assert len(parse_tasks) > 1
    assert len(parse_tasks) == sum(
        1 for task in body["tasks"] if task["kind"] == TaskKind.PARSE_FILE
    )

    targets = [str(task.payload["file_id"]) for task in parse_tasks]
    assert len(set(targets)) == len(targets), "a file must not be parsed twice in one run"

    listed = engineer_client.get(f"/api/projects/{ingested_project.id}/files").json()
    assert set(targets) <= {record["id"] for record in listed}


def test_a_second_run_while_one_is_in_progress_is_refused(
    engineer_client: AuthedClient, ingested_project: Project, db: Session
) -> None:
    """Two concurrent runs would interleave findings and neither would replay."""
    first = _start_run(engineer_client, ingested_project.id)

    response = engineer_client.post(f"/api/projects/{ingested_project.id}/runs", json={})

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "conflict"
    assert "already in progress" in response.json()["error"]["message"]

    run_ids = {str(task.run_id) for task in _tasks(db, ingested_project.id)}
    assert run_ids == {first["run_id"]}, "the refused call must not have created a second graph"


def test_a_reviewer_cannot_start_a_run(
    reviewer_client: AuthedClient, ingested_project: Project, db: Session
) -> None:
    """A review role judges the work; it does not decide what work happens."""
    response = reviewer_client.post(f"/api/projects/{ingested_project.id}/runs", json={})

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "permission_denied"
    assert _tasks(db, ingested_project.id) == []


def test_an_outsider_cannot_start_a_run(
    outsider_client: AuthedClient, ingested_project: Project, db: Session
) -> None:
    """Holding the engineer role globally grants nothing on someone else's job."""
    response = outsider_client.post(f"/api/projects/{ingested_project.id}/runs", json={})

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "permission_denied"
    assert _tasks(db, ingested_project.id) == []


def test_a_project_with_no_files_cannot_start_a_run(
    engineer_client: AuthedClient, project: Project, db: Session
) -> None:
    """The refusal has to say what to do next, not merely that it declined."""
    response = engineer_client.post(f"/api/projects/{project.id}/runs", json={})

    assert response.status_code == 409
    body = response.json()["error"]
    assert body["code"] == "conflict"
    assert "no files" in body["message"]
    assert "Upload engineering documents" in body["message"]
    assert _tasks(db, project.id) == []


def test_restricting_a_run_to_one_file_parses_only_that_file(
    engineer_client: AuthedClient, ingested_project: Project
) -> None:
    listing = engineer_client.get(f"/api/projects/{ingested_project.id}/files")
    assert listing.status_code == 200
    chosen = listing.json()[0]

    body = _start_run(engineer_client, ingested_project.id, file_ids=[chosen["id"]])

    parse_tasks = [task for task in body["tasks"] if task["kind"] == TaskKind.PARSE_FILE]
    assert len(parse_tasks) == 1
    assert chosen["original_filename"] in parse_tasks[0]["title"]


# ---------------------------------------------------------------------------
# GET /api/projects/{id}/tasks
# ---------------------------------------------------------------------------
def test_task_listing_returns_the_whole_graph_in_execution_order(
    engineer_client: AuthedClient, ingested_project: Project
) -> None:
    body = _start_run(engineer_client, ingested_project.id)

    response = engineer_client.get(f"/api/projects/{ingested_project.id}/tasks")

    assert response.status_code == 200
    listed = response.json()
    assert [task["id"] for task in listed] == [task["id"] for task in body["tasks"]]
    assert [task["priority"] for task in listed] == sorted(task["priority"] for task in listed)


def test_task_listing_filters_by_status(
    engineer_client: AuthedClient, ingested_project: Project, settings: Settings
) -> None:
    _start_run(engineer_client, ingested_project.id)
    _drain(settings)

    everything = engineer_client.get(f"/api/projects/{ingested_project.id}/tasks").json()
    expected = Counter(task["status"] for task in everything)
    assert expected[TaskStatus.COMPLETED] > 0
    # A completed run parks at the approval gate rather than blocking, so the
    # filter is exercised against that status instead.
    assert expected[TaskStatus.AWAITING_APPROVAL] > 0

    for status in (TaskStatus.COMPLETED, TaskStatus.AWAITING_APPROVAL, TaskStatus.QUEUED):
        response = engineer_client.get(
            f"/api/projects/{ingested_project.id}/tasks", params={"status": status.value}
        )
        assert response.status_code == 200
        filtered = response.json()
        assert {task["status"] for task in filtered} <= {status.value}
        assert len(filtered) == expected[status.value]


def test_task_listing_filters_by_run_id(
    engineer_client: AuthedClient, ingested_project: Project
) -> None:
    body = _start_run(engineer_client, ingested_project.id)

    mine = engineer_client.get(
        f"/api/projects/{ingested_project.id}/tasks", params={"run_id": body["run_id"]}
    )
    assert mine.status_code == 200
    assert {task["id"] for task in mine.json()} == {task["id"] for task in body["tasks"]}

    other = engineer_client.get(
        f"/api/projects/{ingested_project.id}/tasks", params={"run_id": str(uuid.uuid4())}
    )
    assert other.status_code == 200
    assert other.json() == []


def test_an_outsider_cannot_list_a_project_s_tasks(
    engineer_client: AuthedClient, outsider_client: AuthedClient, ingested_project: Project
) -> None:
    _start_run(engineer_client, ingested_project.id)

    response = outsider_client.get(f"/api/projects/{ingested_project.id}/tasks")

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "permission_denied"


# ---------------------------------------------------------------------------
# GET /api/tasks/{task_id}
# ---------------------------------------------------------------------------
def test_task_detail_reports_both_directions_of_its_dependency_edges(
    engineer_client: AuthedClient, ingested_project: Project, db: Session
) -> None:
    """The graph is only navigable if a task names what it waits on *and* what
    waits on it — the UI draws the run from these edges."""
    _start_run(engineer_client, ingested_project.id)

    intake = _task_of_kind(db, ingested_project.id, TaskKind.INVENTORY_FILES)
    graph = _task_of_kind(db, ingested_project.id, TaskKind.BUILD_EVIDENCE_GRAPH)
    plan = _task_of_kind(db, ingested_project.id, TaskKind.PLAN_PROJECT)
    parse_ids = {
        str(task.id) for task in _tasks(db, ingested_project.id) if task.kind is TaskKind.PARSE_FILE
    }

    intake_detail = engineer_client.get(f"/api/tasks/{intake.id}").json()
    assert intake_detail["depends_on"] == []
    assert set(intake_detail["blocks"]) == parse_ids

    graph_detail = engineer_client.get(f"/api/tasks/{graph.id}").json()
    assert set(graph_detail["depends_on"]) == parse_ids
    assert graph_detail["blocks"] == [str(plan.id)]

    plan_detail = engineer_client.get(f"/api/tasks/{plan.id}").json()
    assert plan_detail["depends_on"] == [str(graph.id)]
    assert plan_detail["id"] == str(plan.id)
    assert plan_detail["kind"] == TaskKind.PLAN_PROJECT


def test_an_unknown_task_id_is_not_found(engineer_client: AuthedClient) -> None:
    response = engineer_client.get(f"/api/tasks/{uuid.uuid4()}")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_a_non_member_cannot_read_a_task(
    engineer_client: AuthedClient,
    outsider_client: AuthedClient,
    ingested_project: Project,
    db: Session,
) -> None:
    """The task id is not a capability: membership of its project is."""
    _start_run(engineer_client, ingested_project.id)
    task = _task_of_kind(db, ingested_project.id, TaskKind.INVENTORY_FILES)

    response = outsider_client.get(f"/api/tasks/{task.id}")

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "permission_denied"


# ---------------------------------------------------------------------------
# pause / resume / cancel
# ---------------------------------------------------------------------------
def test_pausing_stops_the_worker_claiming_and_resuming_lets_it_claim_again(
    engineer_client: AuthedClient, ingested_project: Project, settings: Settings
) -> None:
    _start_run(engineer_client, ingested_project.id)

    paused = engineer_client.post(
        f"/api/projects/{ingested_project.id}/pause", json={"reason": "lunch"}
    )
    assert paused.status_code == 200
    state = paused.json()
    assert state["is_paused"] is True
    assert state["project_id"] == str(ingested_project.id)
    assert state["message"]

    assert worker.tick(settings, WORKER_ID).claimed == 0, "a paused project must yield no work"

    resumed = engineer_client.post(f"/api/projects/{ingested_project.id}/resume", json={})
    assert resumed.status_code == 200
    assert resumed.json()["is_paused"] is False

    assert worker.tick(settings, WORKER_ID).claimed == 1


def test_pausing_an_already_paused_project_is_refused(
    engineer_client: AuthedClient, ingested_project: Project
) -> None:
    _start_run(engineer_client, ingested_project.id)
    first = engineer_client.post(f"/api/projects/{ingested_project.id}/pause", json={})
    assert first.status_code == 200

    response = engineer_client.post(f"/api/projects/{ingested_project.id}/pause", json={})

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "conflict"
    assert "already paused" in response.json()["error"]["message"]


def test_resuming_a_project_that_is_not_paused_is_refused(
    engineer_client: AuthedClient, ingested_project: Project
) -> None:
    _start_run(engineer_client, ingested_project.id)

    response = engineer_client.post(f"/api/projects/{ingested_project.id}/resume", json={})

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "conflict"


def test_cancelling_stops_every_unfinished_task_and_keeps_finished_work(
    engineer_client: AuthedClient, ingested_project: Project, settings: Settings, db: Session
) -> None:
    """Cancelling a run is not retracting what it already found."""
    _start_run(engineer_client, ingested_project.id)
    assert worker.tick(settings, WORKER_ID).completed == 1, "intake should have run"

    response = engineer_client.post(
        f"/api/projects/{ingested_project.id}/cancel", json={"reason": "wrong drawings"}
    )

    assert response.status_code == 200
    state = response.json()
    assert state["queued"] == 0
    assert state["running"] == 0
    assert state["completed"] == 1
    assert state["cancelled"] > 0
    assert state["is_paused"] is False
    assert str(state["cancelled"]) in state["message"]

    rows = _tasks(db, ingested_project.id)
    assert {task.status for task in rows} == {TaskStatus.COMPLETED, TaskStatus.CANCELLED}
    for task in rows:
        if task.status is TaskStatus.CANCELLED:
            assert "wrong drawings" in task.blocked_reason
            assert task.finished_at is not None

    assert worker.tick(settings, WORKER_ID).claimed == 0, "nothing may run after a cancel"


def test_workflow_control_requires_write_access(
    engineer_client: AuthedClient, reviewer_client: AuthedClient, ingested_project: Project
) -> None:
    _start_run(engineer_client, ingested_project.id)

    for action in ("pause", "resume", "cancel", "recover"):
        response = reviewer_client.post(f"/api/projects/{ingested_project.id}/{action}", json={})
        assert response.status_code == 403, action
        assert response.json()["error"]["code"] == "permission_denied"


# ---------------------------------------------------------------------------
# POST /api/projects/{id}/recover
# ---------------------------------------------------------------------------
def test_recover_requeues_a_task_whose_worker_stopped_responding(
    engineer_client: AuthedClient, ingested_project: Project, settings: Settings, db: Session
) -> None:
    """A task left ``running`` by a killed process must return to the queue.

    The expired lease is written directly rather than waited out, so this asserts
    the sweep itself and not the clock.
    """
    _start_run(engineer_client, ingested_project.id)
    abandoned = _task_of_kind(db, ingested_project.id, TaskKind.INVENTORY_FILES)
    abandoned.status = TaskStatus.RUNNING
    abandoned.locked_by = "worker-that-died"
    abandoned.locked_at = utcnow() - timedelta(hours=2)
    abandoned.lease_expires_at = utcnow() - timedelta(hours=1)
    abandoned.attempts = 1
    db.commit()

    assert worker.tick(settings, WORKER_ID).claimed == 0, "a leased task is not claimable"

    response = engineer_client.post(f"/api/projects/{ingested_project.id}/recover", json={})

    assert response.status_code == 200
    assert "1 task" in response.json()["message"]

    recovered = _task_of_kind(db, ingested_project.id, TaskKind.INVENTORY_FILES)
    assert recovered.status is TaskStatus.QUEUED
    assert recovered.locked_by is None
    assert recovered.lease_expires_at is None
    assert recovered.error_code == "lease_expired"
    assert recovered.attempts == 1, "a crash costs one attempt, not two"

    assert worker.tick(settings, WORKER_ID).completed == 1, "the requeued task must run again"


def test_recover_says_so_when_nothing_was_abandoned(
    engineer_client: AuthedClient, ingested_project: Project
) -> None:
    _start_run(engineer_client, ingested_project.id)

    response = engineer_client.post(f"/api/projects/{ingested_project.id}/recover", json={})

    assert response.status_code == 200
    assert response.json()["message"] == "No abandoned tasks were found."


# ---------------------------------------------------------------------------
# GET /api/projects/{id}/workflow
# ---------------------------------------------------------------------------
def test_workflow_counts_match_the_tasks_in_each_status(
    engineer_client: AuthedClient, ingested_project: Project, settings: Settings
) -> None:
    body = _start_run(engineer_client, ingested_project.id)
    _drain(settings)

    listed = engineer_client.get(f"/api/projects/{ingested_project.id}/tasks").json()
    counted = Counter(task["status"] for task in listed)

    response = engineer_client.get(f"/api/projects/{ingested_project.id}/workflow")

    assert response.status_code == 200
    state = response.json()
    assert state["project_id"] == str(ingested_project.id)
    assert state["active_run_id"] == body["run_id"]
    assert state["is_paused"] is False
    for status in TaskStatus:
        assert state[status.value] == counted[status.value], status.value
    assert sum(state[status.value] for status in TaskStatus) == len(listed)


def test_workflow_state_of_a_project_that_never_ran_is_all_zeroes(
    engineer_client: AuthedClient, project: Project
) -> None:
    response = engineer_client.get(f"/api/projects/{project.id}/workflow")

    assert response.status_code == 200
    state = response.json()
    assert state["project_status"] == ProjectStatus.DRAFT
    assert state["active_run_id"] is None
    assert [state[status.value] for status in TaskStatus] == [0] * len(TaskStatus)


def test_a_drained_run_blocks_at_the_plan_with_a_reason_an_operator_can_act_on(
    engineer_client: AuthedClient, ingested_project: Project, settings: Settings, db: Session
) -> None:
    """A run parks at the approval gate with a reason an operator can act on.

    Waiting for a person is a legitimate resting state, so it must be reported as
    one — a task that is ``awaiting_approval`` and says what it is waiting for,
    rather than a run that has simply gone quiet.
    """
    _start_run(engineer_client, ingested_project.id)
    outcome = _drain(settings)

    assert outcome.failed == 0, "no task in the review should fail"

    plan = _task_of_kind(db, ingested_project.id, TaskKind.PLAN_PROJECT)
    detail = engineer_client.get(f"/api/tasks/{plan.id}").json()
    assert detail["status"] == TaskStatus.COMPLETED

    gate = _task_of_kind(db, ingested_project.id, TaskKind.REQUEST_APPROVALS)
    gate_detail = engineer_client.get(f"/api/tasks/{gate.id}").json()
    assert gate_detail["status"] == TaskStatus.AWAITING_APPROVAL
    assert "decision" in gate_detail["blocked_reason"].lower()

    intake = _task_of_kind(db, ingested_project.id, TaskKind.INVENTORY_FILES)
    graph = _task_of_kind(db, ingested_project.id, TaskKind.BUILD_EVIDENCE_GRAPH)
    assert intake.status is TaskStatus.COMPLETED
    assert graph.status is TaskStatus.COMPLETED, "the prelude must reach the evidence graph"

    workflow = engineer_client.get(f"/api/projects/{ingested_project.id}/workflow").json()
    assert workflow["awaiting_approval"] >= 1, "the run should be parked for a human"
    assert workflow["failed"] == 0


# ---------------------------------------------------------------------------
# GET /api/projects/{id}/parsers
# ---------------------------------------------------------------------------
def test_parser_capabilities_report_one_row_per_file_kind(
    engineer_client: AuthedClient, project: Project
) -> None:
    """An unavailable parser explains why a file was not analysed, rather than
    leaving it looking merely unexamined."""
    response = engineer_client.get(f"/api/projects/{project.id}/parsers")

    assert response.status_code == 200
    rows = response.json()
    assert rows

    names = [row["name"] for row in rows]
    assert len(names) == len(set(names)), "a kind must be reported once"
    assert all(name.startswith("parser:") for name in names)
    assert "parser:l5x" in names

    for row in rows:
        assert set(row) == {"name", "available", "provider", "version", "detail"}
        assert isinstance(row["available"], bool)
        if row["available"]:
            assert row["provider"] and row["version"]
        else:
            assert row["detail"], f"{row['name']} must say why it is unavailable"


def test_an_outsider_cannot_read_parser_capabilities(
    outsider_client: AuthedClient, project: Project
) -> None:
    response = outsider_client.get(f"/api/projects/{project.id}/parsers")

    assert response.status_code == 403


# ---------------------------------------------------------------------------
# GET /api/projects/{id}/events
# ---------------------------------------------------------------------------
def test_event_history_returns_the_documented_envelope_in_sequence_order(
    engineer_client: AuthedClient, ingested_project: Project
) -> None:
    _start_run(engineer_client, ingested_project.id, note="history")

    response = engineer_client.get(f"/api/projects/{ingested_project.id}/events")

    assert response.status_code == 200
    events = response.json()
    assert events

    seqs = [event["seq"] for event in events]
    assert seqs == sorted(seqs)
    assert seqs == list(range(1, len(seqs) + 1)), "sequence numbers must be gap-free"

    for event in events:
        assert set(event) == {
            "id",
            "schema_version",
            "seq",
            "event_type",
            "project_id",
            "agent_id",
            "task_id",
            "actor_user_id",
            "occurred_at",
            "payload",
        }
        assert event["project_id"] == str(ingested_project.id)
        assert event["schema_version"] == 1
        assert isinstance(event["payload"], dict)

    started = [event for event in events if event["event_type"] == EventType.WORKFLOW_STARTED]
    assert len(started) == 1
    assert started[0]["actor_user_id"] == str(engineer_client.user.id)
    assert started[0]["payload"]["reason"] == "history"


def test_since_seq_returns_only_what_followed_it(
    engineer_client: AuthedClient, ingested_project: Project
) -> None:
    """This is how a reconnecting client catches up without refetching the world."""
    _start_run(engineer_client, ingested_project.id)
    everything = engineer_client.get(f"/api/projects/{ingested_project.id}/events").json()
    assert len(everything) > 3

    cutoff = everything[2]["seq"]
    response = engineer_client.get(
        f"/api/projects/{ingested_project.id}/events", params={"since_seq": cutoff}
    )

    assert response.status_code == 200
    later = response.json()
    assert [event["seq"] for event in later] == [event["seq"] for event in everything[3:]]
    assert all(event["seq"] > cutoff for event in later)

    caught_up = engineer_client.get(
        f"/api/projects/{ingested_project.id}/events",
        params={"since_seq": everything[-1]["seq"]},
    )
    assert caught_up.json() == []


def test_event_history_can_be_filtered_by_type_and_paged(
    engineer_client: AuthedClient, ingested_project: Project
) -> None:
    _start_run(engineer_client, ingested_project.id)
    everything = engineer_client.get(f"/api/projects/{ingested_project.id}/events").json()

    filtered = engineer_client.get(
        f"/api/projects/{ingested_project.id}/events",
        params={"event_type": [EventType.TASK_CREATED.value]},
    )
    assert filtered.status_code == 200
    expected = [event for event in everything if event["event_type"] == EventType.TASK_CREATED]
    assert expected, "the run must have created tasks"
    assert [event["id"] for event in filtered.json()] == [event["id"] for event in expected]

    first_page = engineer_client.get(
        f"/api/projects/{ingested_project.id}/events", params={"limit": 2}
    ).json()
    second_page = engineer_client.get(
        f"/api/projects/{ingested_project.id}/events", params={"limit": 2, "offset": 2}
    ).json()
    assert [event["seq"] for event in first_page] == [event["seq"] for event in everything[:2]]
    assert [event["seq"] for event in second_page] == [event["seq"] for event in everything[2:4]]


def test_a_non_member_cannot_read_the_event_history(
    outsider_client: AuthedClient, ingested_project: Project
) -> None:
    response = outsider_client.get(f"/api/projects/{ingested_project.id}/events")

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "permission_denied"


# ---------------------------------------------------------------------------
# WEBSOCKET /ws/projects/{id}
# ---------------------------------------------------------------------------
def _ws_url(project_id: uuid.UUID, **params: str | int) -> str:
    query = "&".join(f"{key}={value}" for key, value in params.items())
    return f"/ws/projects/{project_id}" + (f"?{query}" if query else "")


def _drain_replay(socket: Any, hello: dict[str, Any]) -> list[dict[str, Any]]:
    return [socket.receive_json() for _ in range(hello["replayed"])]


def _next_event(socket: Any) -> dict[str, Any]:
    """The next event envelope, ignoring the idle keep-alive frames."""
    while True:
        frame: dict[str, Any] = socket.receive_json()
        if frame.get("type") != "ping":
            return frame


def test_a_socket_with_no_token_is_closed_and_yields_no_events(
    client: TestClient, project: Project
) -> None:
    """An upgrade request is not trusted merely because it reached the endpoint."""
    with (
        pytest.raises(WebSocketDisconnect) as refused,
        client.websocket_connect(_ws_url(project.id)),
    ):
        pytest.fail("the socket must not be accepted without a token")

    assert refused.value.code == 4401


def test_a_socket_with_an_unparseable_token_is_closed(client: TestClient, project: Project) -> None:
    with (
        pytest.raises(WebSocketDisconnect) as refused,
        client.websocket_connect(_ws_url(project.id, token="not-a-jwt")),
    ):
        pytest.fail("a forged token must not be accepted")

    assert refused.value.code == 4401


def test_a_socket_for_a_non_member_is_closed(
    client: TestClient, outsider_client: AuthedClient, project: Project
) -> None:
    """Authenticated is not authorised: the same project check the REST routes
    apply is repeated on the upgrade."""
    with (
        pytest.raises(WebSocketDisconnect) as refused,
        client.websocket_connect(_ws_url(project.id, token=outsider_client.token)),
    ):
        pytest.fail("a non-member must not be able to watch a project")

    assert refused.value.code == 4403


def test_the_first_frame_is_a_hello_describing_the_stream(
    client: TestClient, engineer_client: AuthedClient, project: Project
) -> None:
    with client.websocket_connect(_ws_url(project.id, token=engineer_client.token)) as socket:
        hello = socket.receive_json()

    assert hello["type"] == "hello"
    assert hello["project_id"] == str(project.id)
    assert hello["schema_version"] == 1
    assert hello["current_seq"] >= 1
    assert hello["replayed"] == hello["current_seq"]
    assert hello["replay_truncated"] is False


def test_connecting_replays_the_persisted_history_in_sequence_order(
    client: TestClient, engineer_client: AuthedClient, ingested_project: Project
) -> None:
    """The socket and the REST history must be interchangeable for catching up."""
    _start_run(engineer_client, ingested_project.id)
    history = engineer_client.get(f"/api/projects/{ingested_project.id}/events").json()
    assert len(history) > 5

    with client.websocket_connect(
        _ws_url(ingested_project.id, token=engineer_client.token)
    ) as socket:
        hello = socket.receive_json()
        replayed = _drain_replay(socket, hello)

    assert hello["current_seq"] == history[-1]["seq"]
    assert hello["replayed"] == len(history)
    assert replayed == history


def test_since_seq_replays_only_the_events_after_it(
    client: TestClient, engineer_client: AuthedClient, ingested_project: Project
) -> None:
    _start_run(engineer_client, ingested_project.id)
    history = engineer_client.get(f"/api/projects/{ingested_project.id}/events").json()
    cutoff = history[2]["seq"]

    with client.websocket_connect(
        _ws_url(ingested_project.id, token=engineer_client.token, since_seq=cutoff)
    ) as socket:
        hello = socket.receive_json()
        replayed = _drain_replay(socket, hello)

    assert hello["replayed"] == len(history) - 3
    assert replayed == history[3:]
    assert all(event["seq"] > cutoff for event in replayed)


def test_the_session_cookie_authenticates_the_socket(
    client: TestClient, engineer_client: AuthedClient, project: Project
) -> None:
    """A browser cannot set headers on a WebSocket handshake, so the cookie login
    already set has to be enough on its own."""
    assert engineer_client.token, "login must have happened"

    with client.websocket_connect(_ws_url(project.id)) as socket:
        hello = socket.receive_json()

    assert hello["type"] == "hello"
    assert hello["project_id"] == str(project.id)


def test_events_recorded_after_connecting_arrive_live(
    client: TestClient, engineer_client: AuthedClient, project: Project, db: Session
) -> None:
    """The socket is a live view, not only a replay."""
    with client.websocket_connect(_ws_url(project.id, token=engineer_client.token)) as socket:
        hello = socket.receive_json()
        _drain_replay(socket, hello)

        record_event(
            db,
            project_id=project.id,
            event_type=EventType.PROJECT_UPDATED,
            payload={"project_id": str(project.id), "name": project.name, "status": "draft"},
        )
        db.commit()

        live = _next_event(socket)

    assert live["event_type"] == EventType.PROJECT_UPDATED
    assert live["seq"] == hello["current_seq"] + 1
    assert live["project_id"] == str(project.id)


def test_a_run_started_over_rest_is_streamed_to_a_connected_socket(
    client: TestClient, engineer_client: AuthedClient, ingested_project: Project
) -> None:
    """Starting a run over HTTP reaches a socket that was already listening.

    What the socket promises is that ``seq`` is gap-free and monotonic per
    project, so a client that loses the connection can name what it missed. It
    does not promise that any two event types arrive next to each other, and
    other events legitimately land inside a run's opening burst — agent presence
    being the current example. So the run's own events are matched by identity
    and ordered by ``seq``, and every frame the socket actually sent is held to
    the sequence contract, including the ones this test did not ask for. Reading
    a fixed number of frames and asserting they were all ``task.created`` tested
    an adjacency nothing guarantees; filtering the others out would have stopped
    testing the socket and started testing a view of it.
    """
    with client.websocket_connect(
        _ws_url(ingested_project.id, token=engineer_client.token)
    ) as socket:
        hello = socket.receive_json()
        _drain_replay(socket, hello)

        body = _start_run(engineer_client, ingested_project.id, note="watched")
        expected_tasks = {task["id"] for task in body["tasks"]}

        # How many frames to expect, counted from the durable log rather than
        # from the number of tasks: the run emits whatever it emits, and reading
        # a task-shaped number of frames is what made this test depend on the
        # burst being uninterrupted. Nothing is running a worker here, so the log
        # is complete once the request has returned.
        history = engineer_client.get(f"/api/projects/{ingested_project.id}/events").json()
        expected_seqs = [event["seq"] for event in history if event["seq"] > hello["current_seq"]]
        frames: list[dict[str, Any]] = [_next_event(socket) for _ in expected_seqs]

    announced = {
        frame["task_id"] for frame in frames if frame["event_type"] == EventType.TASK_CREATED
    }
    assert announced == expected_tasks, (
        f"{len(expected_tasks - announced)} of {len(expected_tasks)} tasks were never announced "
        f"in {len(frames)} frame(s): {Counter(frame['event_type'] for frame in frames)}"
    )

    started = frames[0]
    assert started["event_type"] == EventType.WORKFLOW_STARTED
    assert started["seq"] == hello["current_seq"] + 1
    assert started["payload"]["run_id"] == body["run_id"]
    assert started["payload"]["reason"] == "watched"

    # The contract, over everything that arrived rather than over a chosen
    # subset: one unbroken run of sequence numbers continuing from the greeting,
    # and the same events the log recorded.
    assert [frame["seq"] for frame in frames] == expected_seqs
    assert expected_seqs == list(
        range(hello["current_seq"] + 1, hello["current_seq"] + 1 + len(expected_seqs))
    ), "the project's sequence has a gap in it"
    assert {frame["project_id"] for frame in frames} == {str(ingested_project.id)}

    created = [frame for frame in frames if frame["event_type"] == EventType.TASK_CREATED]
    assert len(created) == body["task_count"]
    assert [frame["seq"] for frame in created] == sorted(frame["seq"] for frame in created)
    assert all(frame["seq"] > started["seq"] for frame in created), (
        "a task was announced before the run that created it"
    )
