"""The event log: numbering, transaction safety, replay, and live fan-out.

The ``events`` table is the durable record of everything the platform did, and
the WebSocket is only a live view of it. Three properties make that true, and
each is tested here:

* every event carries a gap-free per-project ``seq``, or replay cannot work;
* an event is published **only** after the transaction that produced it commits,
  because telling an engineer about a finding that then rolled back is worse
  than telling them nothing;
* a subscriber that stops reading loses frames rather than stalling the engine.

The contract-conformance tests are the ones that stop the published event schema
drifting away from what the code actually emits.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

# Importing the step module is what registers the deterministic handlers, which
# is exactly what ``create_app`` does. Without it every task would block as
# unhandled and a "real run" would not be real.
import app.orchestration.steps  # noqa: F401
from app.api.schemas.events import EVENT_PAYLOAD_MODELS, EventEnvelope
from app.core.config import Settings
from app.core.enums import EventType, TaskKind
from app.core.services import projects as project_service
from app.models.event import Event
from app.models.project import Project
from app.models.user import User
from app.orchestration import engine as workflow
from app.orchestration import events as events_module
from app.orchestration import worker
from app.orchestration.broadcast import SUBSCRIBER_QUEUE_SIZE, EventBroker
from app.orchestration.events import _PENDING_KEY, envelope_of, read_events, record_event


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
class _RecordingBroker:
    """Stands in for the process-wide broker so publishing is observable."""

    def __init__(self) -> None:
        self.published: list[tuple[uuid.UUID, dict[str, Any]]] = []

    def publish_threadsafe(self, project_id: uuid.UUID, envelope: dict[str, Any]) -> None:
        self.published.append((project_id, envelope))


@pytest.fixture
def recording_broker(monkeypatch: pytest.MonkeyPatch) -> _RecordingBroker:
    broker = _RecordingBroker()
    monkeypatch.setattr(events_module, "get_broker", lambda: broker)
    return broker


def _task_payload(title: str, status: str = "queued") -> dict[str, Any]:
    return {
        "task_id": str(uuid.uuid4()),
        "kind": TaskKind.PARSE_FILE.value,
        "title": title,
        "status": status,
    }


def _seed_log(db: Session, project: Project, count: int) -> list[Event]:
    """Record ``count`` alternating task events and commit them."""
    records = [
        record_event(
            db,
            project_id=project.id,
            event_type=EventType.TASK_CREATED if index % 2 == 0 else EventType.TASK_COMPLETED,
            payload=_task_payload(f"Task {index}"),
        )
        for index in range(count)
    ]
    db.commit()
    return records


def _all_events(db: Session, project_id: uuid.UUID) -> list[Event]:
    return list(
        db.execute(
            select(Event).where(Event.project_id == project_id).order_by(Event.seq)
        ).scalars()
    )


def _drive_a_run(db: Session, project: Project, settings: Settings, actor: User) -> list[Event]:
    """Start a run, work the queue to a standstill, and return the whole log."""
    workflow.start_run(db, project, settings, actor=actor)
    db.commit()
    worker.drain(settings)
    db.expire_all()
    return _all_events(db, project.id)


# ---------------------------------------------------------------------------
# Sequence allocation
# ---------------------------------------------------------------------------
def test_events_are_numbered_from_one_without_gaps(db: Session, project: Project) -> None:
    _seed_log(db, project, 3)
    db.refresh(project)

    records = _all_events(db, project.id)
    # The project's own creation event is the first thing that ever happened.
    assert records[0].event_type == EventType.PROJECT_CREATED
    assert [record.seq for record in records] == [1, 2, 3, 4]
    assert project.event_seq == 4, "the project row must track the latest seq"


def test_sequence_numbers_are_per_project_and_never_interleave(
    db: Session, project: Project, seeded: dict[str, User], settings: Settings
) -> None:
    """Two projects each count from 1, so one project's traffic cannot make
    another client think it missed events."""
    other = project_service.create_project(
        db,
        name="Second Synthetic Project",
        description="A separate event stream.",
        client_reference="TEST-002",
        attributes={},
        creator=seeded["engineer"],
        settings=settings,
    )
    db.commit()

    # Interleave writes across both projects.
    for index in range(3):
        record_event(
            db,
            project_id=project.id,
            event_type=EventType.TASK_CREATED,
            payload=_task_payload(f"First {index}"),
        )
        record_event(
            db,
            project_id=other.id,
            event_type=EventType.TASK_CREATED,
            payload=_task_payload(f"Second {index}"),
        )
    db.commit()
    db.refresh(project)
    db.refresh(other)

    assert [record.seq for record in _all_events(db, project.id)] == [1, 2, 3, 4]
    assert [record.seq for record in _all_events(db, other.id)] == [1, 2, 3, 4]
    assert project.event_seq == 4
    assert other.event_seq == 4

    first_titles = {record.payload.get("title") for record in _all_events(db, project.id)}
    assert not any(str(title).startswith("Second") for title in first_titles if title)


# ---------------------------------------------------------------------------
# Transaction safety — the point of this module
# ---------------------------------------------------------------------------
def test_a_rolled_back_event_leaves_no_row_and_is_never_published(
    db: Session, project: Project, recording_broker: _RecordingBroker
) -> None:
    """A client must never be told about a change that did not happen."""
    before = len(_all_events(db, project.id))

    record_event(
        db,
        project_id=project.id,
        event_type=EventType.FINDING_CREATED,
        payload={
            "finding_id": str(uuid.uuid4()),
            "title": "Interlock never implemented",
            "category": "interlock",
            "severity": "high",
            "status": "open",
            "confidence": 0.9,
            "requires_approval": True,
        },
    )
    assert len(db.info[_PENDING_KEY]) == 1, "the envelope should be buffered, not published"
    assert recording_broker.published == [], "nothing may be published before commit"

    db.rollback()

    assert _PENDING_KEY not in db.info, "the rolled-back envelope must be discarded"
    assert recording_broker.published == []
    assert len(_all_events(db, project.id)) == before
    assert (
        db.execute(
            select(Event).where(
                Event.project_id == project.id, Event.event_type == EventType.FINDING_CREATED
            )
        ).first()
        is None
    )


def test_a_rolled_back_event_does_not_consume_a_sequence_number(
    db: Session, project: Project
) -> None:
    """The seq is allocated in the same transaction, so it rolls back too."""
    record_event(
        db,
        project_id=project.id,
        event_type=EventType.WORKFLOW_PAUSED,
        payload={"reason": "never happened"},
    )
    db.rollback()

    record_event(
        db,
        project_id=project.id,
        event_type=EventType.WORKFLOW_PAUSED,
        payload={"reason": "this one is real"},
    )
    db.commit()
    db.refresh(project)

    records = _all_events(db, project.id)
    assert [record.seq for record in records] == [1, 2]
    assert records[-1].payload["reason"] == "this one is real"
    assert project.event_seq == 2


def test_a_committed_event_is_persisted_and_then_published(
    db: Session, project: Project, recording_broker: _RecordingBroker
) -> None:
    record = record_event(
        db,
        project_id=project.id,
        event_type=EventType.WORKFLOW_STARTED,
        actor_user_id=project.created_by_id,
        payload={"run_id": str(uuid.uuid4()), "task_count": 3, "reason": "committed"},
    )
    expected_seq = record.seq

    db.commit()

    assert _PENDING_KEY not in db.info, "the buffer must be drained by the commit"
    assert len(recording_broker.published) == 1
    published_project_id, envelope = recording_broker.published[0]
    assert published_project_id == project.id
    assert envelope["seq"] == expected_seq
    assert envelope["event_type"] == EventType.WORKFLOW_STARTED.value

    stored = db.execute(select(Event).where(Event.seq == expected_seq)).scalar_one()
    assert stored.project_id == project.id
    assert stored.payload["reason"] == "committed"


# ---------------------------------------------------------------------------
# Reading the log back
# ---------------------------------------------------------------------------
def test_read_events_returns_the_log_in_sequence_order_with_the_current_seq(
    db: Session, project: Project
) -> None:
    _seed_log(db, project, 6)

    records, current_seq = read_events(db, project.id)

    assert [record.seq for record in records] == [1, 2, 3, 4, 5, 6, 7]
    assert current_seq == 7, "a caller needs the head of the log to know it has caught up"


def test_since_seq_returns_only_events_the_caller_has_not_seen(
    db: Session, project: Project
) -> None:
    _seed_log(db, project, 6)

    records, current_seq = read_events(db, project.id, since_seq=4)

    assert [record.seq for record in records] == [5, 6, 7]
    assert current_seq == 7


def test_since_seq_at_the_head_returns_nothing(db: Session, project: Project) -> None:
    _seed_log(db, project, 6)

    records, current_seq = read_events(db, project.id, since_seq=7)

    assert records == []
    assert current_seq == 7, "an up-to-date client still needs to know where the head is"


def test_event_types_filters_the_log(db: Session, project: Project) -> None:
    _seed_log(db, project, 6)

    records, current_seq = read_events(db, project.id, event_types=[EventType.TASK_COMPLETED])

    assert [record.seq for record in records] == [3, 5, 7]
    assert {record.event_type for record in records} == {EventType.TASK_COMPLETED}
    assert current_seq == 7, "the head is the project's, not the filtered subset's"


def test_event_types_accepts_several_types(db: Session, project: Project) -> None:
    _seed_log(db, project, 6)

    records, _ = read_events(
        db, project.id, event_types=[EventType.PROJECT_CREATED, EventType.TASK_CREATED]
    )

    assert [record.seq for record in records] == [1, 2, 4, 6]


def test_limit_and_offset_page_through_the_log_without_overlap(
    db: Session, project: Project
) -> None:
    _seed_log(db, project, 6)

    first, _ = read_events(db, project.id, limit=3, offset=0)
    second, _ = read_events(db, project.id, limit=3, offset=3)
    third, _ = read_events(db, project.id, limit=3, offset=6)
    beyond, _ = read_events(db, project.id, limit=3, offset=9)

    assert [record.seq for record in first] == [1, 2, 3]
    assert [record.seq for record in second] == [4, 5, 6]
    assert [record.seq for record in third] == [7]
    assert beyond == []


def test_paging_and_filtering_compose(db: Session, project: Project) -> None:
    _seed_log(db, project, 6)

    records, _ = read_events(
        db, project.id, since_seq=2, event_types=[EventType.TASK_CREATED], limit=1, offset=1
    )

    assert [record.seq for record in records] == [6]


def test_read_events_for_a_project_with_no_events_is_empty_not_an_error(
    db: Session, project: Project
) -> None:
    unknown = uuid.uuid4()

    records, current_seq = read_events(db, unknown)

    assert records == []
    assert current_seq == 0


# ---------------------------------------------------------------------------
# The envelope
# ---------------------------------------------------------------------------
def test_envelope_carries_every_documented_field(db: Session, project: Project) -> None:
    agent_id = uuid.uuid4()
    task_id = uuid.uuid4()
    record = record_event(
        db,
        project_id=project.id,
        event_type=EventType.TASK_STARTED,
        agent_id=agent_id,
        task_id=task_id,
        actor_user_id=project.created_by_id,
        payload=_task_payload("Parse IO_List.csv", status="running"),
    )
    db.commit()

    envelope = envelope_of(record)

    assert set(envelope) == {
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
    assert envelope["id"] == str(record.id)
    assert envelope["schema_version"] == events_module.SCHEMA_VERSION
    assert envelope["seq"] == record.seq
    assert envelope["event_type"] == EventType.TASK_STARTED.value
    assert envelope["project_id"] == str(project.id)
    assert envelope["agent_id"] == str(agent_id)
    assert envelope["task_id"] == str(task_id)
    assert envelope["actor_user_id"] == str(project.created_by_id)
    assert envelope["payload"] == record.payload


def test_envelope_ids_are_strings_and_timestamps_are_rfc_3339_z(
    db: Session, project: Project
) -> None:
    record = record_event(
        db,
        project_id=project.id,
        event_type=EventType.TASK_STARTED,
        agent_id=uuid.uuid4(),
        task_id=uuid.uuid4(),
        actor_user_id=project.created_by_id,
        payload=_task_payload("Parse IO_List.csv", status="running"),
    )
    db.commit()

    envelope = envelope_of(record)

    for key in ("id", "project_id", "agent_id", "task_id", "actor_user_id"):
        assert isinstance(envelope[key], str), f"{key} must be rendered as a string"
        uuid.UUID(envelope[key])

    occurred_at = envelope["occurred_at"]
    assert isinstance(occurred_at, str)
    assert occurred_at.endswith("Z"), occurred_at
    assert "+00:00" not in occurred_at
    parsed = datetime.fromisoformat(occurred_at.replace("Z", "+00:00"))
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() is not None and parsed.utcoffset().total_seconds() == 0  # type: ignore[union-attr]


def test_envelope_leaves_absent_actors_null(db: Session, project: Project) -> None:
    record = record_event(
        db,
        project_id=project.id,
        event_type=EventType.PROJECT_UPDATED,
        payload={"project_id": str(project.id), "name": project.name, "status": "running"},
    )
    db.commit()

    envelope = envelope_of(record)

    assert envelope["agent_id"] is None
    assert envelope["task_id"] is None
    assert envelope["actor_user_id"] is None


def test_envelope_is_json_serialisable_and_validates_against_the_published_schema(
    db: Session, project: Project
) -> None:
    """The envelope goes down a WebSocket as JSON; anything unserialisable here
    is a runtime error in front of a user."""
    record = record_event(
        db,
        project_id=project.id,
        event_type=EventType.TASK_COMPLETED,
        task_id=uuid.uuid4(),
        payload=_task_payload("Build the project evidence graph", status="completed"),
    )
    db.commit()

    envelope = envelope_of(record)
    encoded = json.dumps(envelope)

    assert json.loads(encoded) == envelope
    EventEnvelope.model_validate(envelope)


# ---------------------------------------------------------------------------
# Contract conformance over a real run
# ---------------------------------------------------------------------------
def test_every_event_a_real_run_emits_matches_its_published_payload_model(
    db: Session, ingested_project: Project, seeded: dict[str, User], settings: Settings
) -> None:
    """What the engine emits and what the frontend is promised must be the same
    thing. A ValidationError here means the published contract has drifted."""
    records = _drive_a_run(db, ingested_project, settings, seeded["engineer"])
    assert records, "the run emitted no events at all"

    for record in records:
        event_type = EventType(record.event_type)
        model = EVENT_PAYLOAD_MODELS.get(event_type)
        assert model is not None, f"{event_type.value} has no documented payload model"
        # Let a ValidationError fail the test: the payload is the contract.
        model.model_validate(record.payload)

    emitted = {EventType(record.event_type) for record in records}
    assert {
        EventType.PROJECT_CREATED,
        EventType.FILE_UPLOADED,
        EventType.WORKFLOW_STARTED,
        EventType.TASK_CREATED,
        EventType.TASK_STARTED,
        EventType.TASK_COMPLETED,
    } <= emitted, f"the run was less complete than expected: {sorted(emitted)}"


def test_a_drained_run_blocks_at_planning_because_no_handler_is_installed(
    db: Session, ingested_project: Project, seeded: dict[str, User], settings: Settings
) -> None:
    """A run that parks for a human decision must say so in the event stream.

    The approval gate is the one place a run is meant to stop, so the events have
    to make it distinguishable from a stall: a task.blocked carrying a reason,
    not merely an absence of further events.
    """
    records = _drive_a_run(db, ingested_project, settings, seeded["engineer"])

    blocked = [
        record
        for record in records
        if EventType(record.event_type) is EventType.TASK_BLOCKED
        and record.payload.get("kind") == TaskKind.REQUEST_APPROVALS.value
    ]
    assert blocked, "the approval gate should have emitted a blocked event"
    assert "decision" in blocked[-1].payload["detail"].lower()

    # And the review reached the gate under its own steam.
    completed = {
        record.payload.get("kind")
        for record in records
        if EventType(record.event_type) is EventType.TASK_COMPLETED
    }
    assert TaskKind.PLAN_PROJECT.value in completed
    assert TaskKind.SENIOR_REVIEW.value in completed


def test_a_full_run_leaves_a_contiguous_sequence(
    db: Session, ingested_project: Project, seeded: dict[str, User], settings: Settings
) -> None:
    """Replay is defined as "give me everything after seq N". A gap or a
    duplicate silently breaks that for every connected client."""
    records = _drive_a_run(db, ingested_project, settings, seeded["engineer"])
    db.refresh(ingested_project)

    sequences = [record.seq for record in records]
    assert sequences == list(range(1, len(records) + 1))
    assert len(set(sequences)) == len(sequences)
    assert ingested_project.event_seq == len(records)

    _, current_seq = read_events(db, ingested_project.id, limit=1)
    assert current_seq == len(records)


# ---------------------------------------------------------------------------
# The broker
# ---------------------------------------------------------------------------
async def test_subscribing_and_unsubscribing_adjusts_the_subscriber_count() -> None:
    broker = EventBroker()
    project_id = uuid.uuid4()

    assert broker.subscriber_count(project_id) == 0
    async with broker.subscribe(project_id):
        assert broker.subscriber_count(project_id) == 1
        async with broker.subscribe(project_id):
            assert broker.subscriber_count(project_id) == 2
        assert broker.subscriber_count(project_id) == 1
    assert broker.subscriber_count(project_id) == 0


async def test_publish_reaches_every_subscriber_of_that_project_and_no_other() -> None:
    broker = EventBroker()
    watched = uuid.uuid4()
    unrelated = uuid.uuid4()
    envelope = {"seq": 1, "event_type": EventType.TASK_STARTED.value}

    async with (
        broker.subscribe(watched) as first,
        broker.subscribe(watched) as second,
        broker.subscribe(unrelated) as elsewhere,
    ):
        await broker.publish(watched, envelope)

        assert first.get_nowait() == envelope
        assert second.get_nowait() == envelope
        assert elsewhere.empty(), "another project's client must never see this"


async def test_publishing_to_a_project_with_no_subscribers_is_harmless() -> None:
    broker = EventBroker()

    await broker.publish(uuid.uuid4(), {"seq": 1})

    assert broker.subscriber_count(uuid.uuid4()) == 0


async def test_an_unsubscribed_queue_stops_receiving() -> None:
    broker = EventBroker()
    project_id = uuid.uuid4()

    async with broker.subscribe(project_id) as queue:
        pass
    await broker.publish(project_id, {"seq": 1})

    assert queue.empty()
    assert broker.subscriber_count(project_id) == 0


async def test_a_full_subscriber_queue_drops_frames_instead_of_blocking() -> None:
    """One stalled browser tab must not be able to block the workflow engine."""
    broker = EventBroker()
    project_id = uuid.uuid4()

    async with broker.subscribe(project_id) as stalled, broker.subscribe(project_id) as healthy:
        for index in range(SUBSCRIBER_QUEUE_SIZE):
            stalled.put_nowait({"seq": index})
        assert stalled.full()

        # If this applied back-pressure it would never return.
        await broker.publish(project_id, {"seq": SUBSCRIBER_QUEUE_SIZE, "event_type": "task.done"})

        assert stalled.qsize() == SUBSCRIBER_QUEUE_SIZE, "the frame should have been dropped"
        assert healthy.get_nowait() == {
            "seq": SUBSCRIBER_QUEUE_SIZE,
            "event_type": "task.done",
        }, "a healthy subscriber must not suffer for a stalled one"


async def test_publish_threadsafe_is_a_silent_no_op_when_no_loop_is_bound() -> None:
    """Persistence has already happened; a CLI script with no running loop must
    not raise on its way out."""
    broker = EventBroker()
    project_id = uuid.uuid4()

    async with broker.subscribe(project_id) as queue:
        broker.publish_threadsafe(project_id, {"seq": 1})
        for _ in range(10):
            await asyncio.sleep(0)

        assert queue.empty()


async def test_publish_threadsafe_delivers_once_a_loop_is_bound() -> None:
    broker = EventBroker()
    broker.bind_loop(asyncio.get_running_loop())
    project_id = uuid.uuid4()
    envelope = {"seq": 7, "event_type": EventType.TASK_COMPLETED.value}

    async with broker.subscribe(project_id) as queue:
        broker.publish_threadsafe(project_id, envelope)
        for _ in range(10):
            if not queue.empty():
                break
            await asyncio.sleep(0)

        assert queue.get_nowait() == envelope
