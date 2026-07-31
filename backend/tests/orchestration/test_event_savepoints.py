"""A savepoint rollback must not cancel events recorded outside it.

Found by attacking the change-set merge rather than by any test: the merge runs
its whole apply inside ``begin_nested()`` and rolls it back on a dry run, and
SQLAlchemy fires ``after_soft_rollback`` for a ``SAVEPOINT`` exactly as it does
for the outer transaction. The old listener simply emptied the buffer, so a dry
run silently cancelled the live delivery of every event the surrounding request
had already recorded.

Nothing errored. The rows were committed and the REST history was complete; only
the WebSocket went quiet, which is the kind of failure a user reports as "the
city stopped moving sometimes".
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy.orm import Session

from app.core.enums import EventType
from app.models.project import Project
from app.orchestration import events as events_module
from app.orchestration.events import record_event


@pytest.fixture
def published(monkeypatch: pytest.MonkeyPatch) -> list[tuple[uuid.UUID, dict[str, Any]]]:
    """Capture what actually reached the broker."""
    sent: list[tuple[uuid.UUID, dict[str, Any]]] = []

    class _Recorder:
        def publish_threadsafe(self, project_id: uuid.UUID, envelope: dict[str, Any]) -> None:
            sent.append((project_id, envelope))

    monkeypatch.setattr(events_module, "get_broker", lambda: _Recorder())
    return sent


def _seqs(sent: list[tuple[uuid.UUID, dict[str, Any]]]) -> list[int]:
    return [envelope["seq"] for _, envelope in sent]


def test_a_savepoint_rollback_keeps_events_recorded_before_it(
    db: Session, project: Project, published: list[tuple[uuid.UUID, dict[str, Any]]]
) -> None:
    """The defect, stated directly."""
    record_event(db, project_id=project.id, event_type=EventType.WORKFLOW_STARTED)

    savepoint = db.begin_nested()
    record_event(db, project_id=project.id, event_type=EventType.TASK_CREATED)
    savepoint.rollback()

    db.commit()

    assert len(published) == 1, "the event recorded before the savepoint must still be delivered"
    assert published[0][1]["event_type"] == EventType.WORKFLOW_STARTED.value


def test_a_savepoint_rollback_drops_the_events_recorded_inside_it(
    db: Session, project: Project, published: list[tuple[uuid.UUID, dict[str, Any]]]
) -> None:
    """The other half: those rows are gone, so broadcasting them would be a lie."""
    savepoint = db.begin_nested()
    record_event(db, project_id=project.id, event_type=EventType.TASK_CREATED)
    record_event(db, project_id=project.id, event_type=EventType.TASK_COMPLETED)
    savepoint.rollback()

    db.commit()

    assert published == []


def test_a_committed_savepoint_keeps_its_events(
    db: Session, project: Project, published: list[tuple[uuid.UUID, dict[str, Any]]]
) -> None:
    record_event(db, project_id=project.id, event_type=EventType.WORKFLOW_STARTED)

    savepoint = db.begin_nested()
    record_event(db, project_id=project.id, event_type=EventType.TASK_CREATED)
    savepoint.commit()

    db.commit()

    assert len(published) == 2
    assert _seqs(published) == sorted(_seqs(published)), "delivery order must follow seq"


def test_nested_savepoints_truncate_to_the_right_point(
    db: Session, project: Project, published: list[tuple[uuid.UUID, dict[str, Any]]]
) -> None:
    """Two levels deep, rolling back only the inner one.

    The mark has to belong to a specific savepoint, not to "the last one seen".
    """
    record_event(db, project_id=project.id, event_type=EventType.WORKFLOW_STARTED)

    outer = db.begin_nested()
    record_event(db, project_id=project.id, event_type=EventType.TASK_CREATED)

    inner = db.begin_nested()
    record_event(db, project_id=project.id, event_type=EventType.TASK_FAILED)
    inner.rollback()

    outer.commit()
    db.commit()

    kinds = [envelope["event_type"] for _, envelope in published]
    assert kinds == [EventType.WORKFLOW_STARTED.value, EventType.TASK_CREATED.value]


def test_rolling_back_the_outer_savepoint_drops_the_inner_events_too(
    db: Session, project: Project, published: list[tuple[uuid.UUID, dict[str, Any]]]
) -> None:
    record_event(db, project_id=project.id, event_type=EventType.WORKFLOW_STARTED)

    outer = db.begin_nested()
    record_event(db, project_id=project.id, event_type=EventType.TASK_CREATED)
    inner = db.begin_nested()
    record_event(db, project_id=project.id, event_type=EventType.TASK_FAILED)
    inner.commit()
    outer.rollback()

    db.commit()

    kinds = [envelope["event_type"] for _, envelope in published]
    assert kinds == [EventType.WORKFLOW_STARTED.value]


def test_a_savepoint_opened_before_any_event_is_harmless(
    db: Session, project: Project, published: list[tuple[uuid.UUID, dict[str, Any]]]
) -> None:
    """The mark is zero, and rolling back must not eat later work.

    Guards the branch where a savepoint opens on an empty buffer — the case
    where an off-by-one would delete everything.
    """
    savepoint = db.begin_nested()
    savepoint.rollback()

    record_event(db, project_id=project.id, event_type=EventType.WORKFLOW_STARTED)
    db.commit()

    assert len(published) == 1


def test_a_savepoint_commit_does_not_publish_early(
    db: Session, project: Project, published: list[tuple[uuid.UUID, dict[str, Any]]]
) -> None:
    """SQLAlchemy fires ``after_commit`` for a savepoint too.

    The second defect of the same family, and the worse one. A savepoint commit
    is not a commit in any sense a client should hear about — the outer
    transaction can still roll back. Publishing there breaks the guarantee the
    whole persist-then-broadcast arrangement exists to provide.
    """
    savepoint = db.begin_nested()
    record_event(db, project_id=project.id, event_type=EventType.TASK_CREATED)
    savepoint.commit()

    assert published == [], "a savepoint commit is not a commit"

    db.commit()
    assert len(published) == 1


def test_a_savepoint_that_committed_is_still_lost_if_the_transaction_rolls_back(
    db: Session, project: Project, published: list[tuple[uuid.UUID, dict[str, Any]]]
) -> None:
    """The sequence the early flush made dangerous.

    The savepoint commits, so its rows look settled — and then the outer
    transaction rolls back and they are gone. A client that had already been
    told about them would be holding a change that never happened.
    """
    savepoint = db.begin_nested()
    record_event(db, project_id=project.id, event_type=EventType.FINDING_CREATED)
    savepoint.commit()
    db.rollback()
    db.commit()

    assert published == []


def test_an_outer_rollback_still_discards_everything(
    db: Session, project: Project, published: list[tuple[uuid.UUID, dict[str, Any]]]
) -> None:
    """The original guarantee, unchanged.

    A client must never observe a change that rolled back; a phantom "finding
    created" is a serious thing to have told an engineer.
    """
    record_event(db, project_id=project.id, event_type=EventType.WORKFLOW_STARTED)
    db.rollback()
    db.commit()

    assert published == []
