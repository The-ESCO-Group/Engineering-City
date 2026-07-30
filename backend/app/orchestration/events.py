"""Event recording: persist first, broadcast after commit.

Two ordering guarantees, both load-bearing:

1. **Persist before broadcast.** The ``events`` table is the durable record; the
   WebSocket is a live view of it. A crash between the state change and the
   notification loses nothing, because the client can replay from ``seq``.

2. **Broadcast only after commit.** Envelopes are buffered on the session and
   flushed by an ``after_commit`` hook. Publishing earlier would let a client
   observe a change that then rolled back — and a rolled-back "finding created"
   is a serious thing to have told an engineer.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import event as sa_event
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core.clock import isoformat_z, utcnow
from app.core.enums import EventType
from app.core.logging import get_logger
from app.models.event import Event
from app.models.project import Project
from app.orchestration.broadcast import get_broker

logger = get_logger(__name__)

#: Key under which pending envelopes are buffered on ``Session.info``.
_PENDING_KEY = "esco_pending_events"

SCHEMA_VERSION = 1


def _next_seq(db: Session, project_id: uuid.UUID) -> int:
    """Allocate the next per-project sequence number.

    Done as an atomic increment on the project row so two concurrent writers
    cannot mint the same ``seq`` and violate the uniqueness the replay contract
    depends on.
    """
    db.execute(
        update(Project).where(Project.id == project_id).values(event_seq=Project.event_seq + 1)
    )
    seq = db.execute(select(Project.event_seq).where(Project.id == project_id)).scalar_one()
    return int(seq)


def envelope_of(record: Event) -> dict[str, Any]:
    """Render a persisted event as the JSON envelope clients receive."""
    return {
        "id": str(record.id),
        "schema_version": record.schema_version,
        "seq": record.seq,
        "event_type": str(record.event_type),
        "project_id": str(record.project_id),
        "agent_id": str(record.agent_id) if record.agent_id else None,
        "task_id": str(record.task_id) if record.task_id else None,
        "actor_user_id": str(record.actor_user_id) if record.actor_user_id else None,
        "occurred_at": isoformat_z(record.occurred_at),
        "payload": record.payload,
    }


def record_event(
    db: Session,
    *,
    project_id: uuid.UUID,
    event_type: EventType,
    payload: dict[str, Any] | None = None,
    agent_id: uuid.UUID | None = None,
    task_id: uuid.UUID | None = None,
    actor_user_id: uuid.UUID | None = None,
) -> Event:
    """Persist an event and queue it for broadcast when the transaction commits.

    Does not commit. The caller owns the transaction, so the event lands if and
    only if the change it describes lands.
    """
    record = Event(
        schema_version=SCHEMA_VERSION,
        seq=_next_seq(db, project_id),
        project_id=project_id,
        event_type=event_type,
        agent_id=agent_id,
        task_id=task_id,
        actor_user_id=actor_user_id,
        occurred_at=utcnow(),
        payload=payload or {},
    )
    db.add(record)
    db.flush()

    pending: list[tuple[uuid.UUID, dict[str, Any]]] = db.info.setdefault(_PENDING_KEY, [])
    pending.append((project_id, envelope_of(record)))
    return record


def _flush_pending(session: Session) -> None:
    """Publish buffered envelopes. Registered as an ``after_commit`` listener."""
    pending: list[tuple[uuid.UUID, dict[str, Any]]] = session.info.pop(_PENDING_KEY, [])
    if not pending:
        return
    broker = get_broker()
    for project_id, envelope in pending:
        broker.publish_threadsafe(project_id, envelope)


def _discard_pending(session: Session, *_: object) -> None:
    """Drop buffered envelopes when a transaction rolls back."""
    session.info.pop(_PENDING_KEY, None)


sa_event.listen(Session, "after_commit", _flush_pending)
sa_event.listen(Session, "after_soft_rollback", _discard_pending)


def read_events(
    db: Session,
    project_id: uuid.UUID,
    *,
    since_seq: int | None = None,
    event_types: list[EventType] | None = None,
    limit: int = 200,
    offset: int = 0,
) -> tuple[list[Event], int]:
    """Read a project's event log in sequence order.

    Returns ``(events, current_seq)`` so a caller knows whether it has caught up.
    """
    statement = select(Event).where(Event.project_id == project_id)
    if since_seq is not None:
        statement = statement.where(Event.seq > since_seq)
    if event_types:
        statement = statement.where(Event.event_type.in_(event_types))

    rows = list(db.execute(statement.order_by(Event.seq).limit(limit).offset(offset)).scalars())
    current_seq = (
        db.execute(select(Project.event_seq).where(Project.id == project_id)).scalar() or 0
    )
    return rows, int(current_seq)
