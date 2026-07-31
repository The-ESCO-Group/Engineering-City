"""Recording a proposal, without applying any of it.

The interesting behaviour here is the boring kind: the same proposal arriving
twice must produce one change set, and a change set that is closed must say why.
Both exist because the alternative is a project whose history nobody can read.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agents.base.actions import (
    ChangeOperationProposal,
    ObjectBasis,
    ProposeChangesetPayload,
)
from app.core.enums import (
    ChangeOperationType,
    ChangeSetStatus,
    EventType,
    EvidenceKind,
    ObjectType,
)
from app.core.errors import InvalidStateTransitionError, ValidationError
from app.engineering import changesets
from app.models.event import Event
from app.models.project import Project
from app.models.revision import ChangeOperation, ChangeSet


def _basis() -> ObjectBasis:
    return ObjectBasis(
        kind=EvidenceKind.TEMPLATE_RULE,
        template_id="esco.motor.standard@1.0.0",
        note="Standard motor start/stop pattern.",
    )


def _create_tag(temp_key: str = "tag-1", name: str = "MTR_101_Cmd_Run") -> ChangeOperationProposal:
    return ChangeOperationProposal(
        operation_type=ChangeOperationType.CREATE_OBJECT,
        object_type=ObjectType.TAG,
        temp_key=temp_key,
        values={"name": name, "description": "Run command from the HMI."},
        basis=[_basis()],
        description=f"Create {name}.",
    )


def _payload(*operations: ChangeOperationProposal, **overrides: object) -> ProposeChangesetPayload:
    body: dict[str, object] = {
        "title": "Motor 101 command tags",
        "summary": "Create the standard command tags for motor 101.",
        "base_revision_number": 0,
        "operations": list(operations) or [_create_tag()],
    }
    body.update(overrides)
    return ProposeChangesetPayload(**body)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Building
# ---------------------------------------------------------------------------
def test_operations_are_recorded_in_the_order_they_were_proposed(
    db: Session, project: Project
) -> None:
    changeset = changesets.build_changeset(
        db,
        project=project,
        payload=_payload(_create_tag("a", "MTR_101_Cmd_Run"), _create_tag("b", "MTR_101_Cmd_Stop")),
        author_agent="tag_architecture",
        allowed_object_types=[ObjectType.TAG],
    )
    db.flush()

    operations = list(
        db.execute(
            select(ChangeOperation)
            .where(ChangeOperation.change_set_id == changeset.id)
            .order_by(ChangeOperation.ordinal)
        ).scalars()
    )
    assert [operation.ordinal for operation in operations] == [0, 1]
    assert [operation.temp_key for operation in operations] == ["a", "b"]
    assert changeset.status is ChangeSetStatus.DRAFT
    assert changeset.operation_count == 2
    assert changeset.allowed_object_types == ["tag"]


def test_the_proposal_survives_on_the_operation(db: Session, project: Project) -> None:
    """Values and basis are readable back, because the merge needs both later.

    ``ChangeOperation`` has no column for either, so both travel in ``after`` —
    the values as themselves, the basis under a reserved key. Callers read them
    through the accessors rather than knowing that.
    """
    changeset = changesets.build_changeset(db, project=project, payload=_payload())
    db.flush()
    operation = changeset.operations[0]

    assert changesets.proposed_values(operation) == {
        "name": "MTR_101_Cmd_Run",
        "description": "Run command from the HMI.",
    }
    assert changesets.stated_basis(operation)[0]["template_id"] == "esco.motor.standard@1.0.0"
    assert "$basis" not in changesets.proposed_values(operation)


def test_building_the_same_change_set_twice_with_one_key_yields_one_change_set(
    db: Session, project: Project
) -> None:
    """The task queue is at-least-once, so a re-run must converge.

    Without this, a retried task proposes the same tags again and the second
    attempt merges as a pile of duplicate-key conflicts nobody caused.
    """
    first = changesets.build_changeset(
        db, project=project, payload=_payload(), idempotency_key="task-7:tags"
    )
    second = changesets.build_changeset(
        db, project=project, payload=_payload(), idempotency_key="task-7:tags"
    )
    db.flush()

    assert second.id == first.id
    total = db.execute(
        select(func.count(ChangeSet.id)).where(ChangeSet.project_id == project.id)
    ).scalar_one()
    assert total == 1
    operations = db.execute(
        select(func.count(ChangeOperation.id)).where(ChangeOperation.change_set_id == first.id)
    ).scalar_one()
    assert operations == 1


def test_without_a_key_two_proposals_are_two_change_sets(db: Session, project: Project) -> None:
    """Deduplication is opt-in. An unkeyed proposal is taken at face value."""
    first = changesets.build_changeset(db, project=project, payload=_payload())
    second = changesets.build_changeset(db, project=project, payload=_payload())
    db.flush()
    assert first.id != second.id


def test_building_announces_the_proposal(db: Session, project: Project) -> None:
    changeset = changesets.build_changeset(db, project=project, payload=_payload())
    db.flush()
    events = _events(db, project, EventType.CHANGESET_CREATED)
    assert [event.payload["changeset_id"] for event in events] == [str(changeset.id)]


# ---------------------------------------------------------------------------
# Transitions
# ---------------------------------------------------------------------------
def test_submitting_moves_a_draft_into_the_queue(db: Session, project: Project) -> None:
    changeset = changesets.build_changeset(db, project=project, payload=_payload())
    changesets.submit(db, changeset)
    db.flush()

    assert changeset.status is ChangeSetStatus.SUBMITTED
    assert changeset.submitted_at is not None
    assert _events(db, project, EventType.CHANGESET_SUBMITTED)


def test_a_merged_change_set_cannot_be_submitted_again(db: Session, project: Project) -> None:
    changeset = changesets.build_changeset(db, project=project, payload=_payload())
    changeset.status = ChangeSetStatus.MERGED
    db.flush()
    with pytest.raises(InvalidStateTransitionError):
        changesets.submit(db, changeset)


def test_a_conflicted_change_set_may_be_corrected_and_resubmitted(
    db: Session, project: Project
) -> None:
    """A retry keeps its history in one place rather than founding a new record."""
    changeset = changesets.build_changeset(db, project=project, payload=_payload())
    changeset.status = ChangeSetStatus.CONFLICTED
    changeset.disposition_reason = "stale revision on MTR_101_Cmd_Run"
    db.flush()

    changesets.submit(db, changeset)
    assert changeset.status is ChangeSetStatus.SUBMITTED
    assert changeset.disposition_reason == ""


def test_abandoning_requires_a_reason(db: Session, project: Project) -> None:
    """A change set that vanishes without one is indistinguishable from a bug."""
    changeset = changesets.build_changeset(db, project=project, payload=_payload())
    with pytest.raises(ValidationError, match="requires a reason"):
        changesets.abandon(db, changeset, "   ")


def test_abandoning_records_why(db: Session, project: Project) -> None:
    changeset = changesets.build_changeset(db, project=project, payload=_payload())
    changesets.abandon(db, changeset, "Superseded by the revised motor template.")
    db.flush()

    assert changeset.status is ChangeSetStatus.ABANDONED
    assert changeset.disposition_reason == "Superseded by the revised motor template."
    assert not changeset.is_open
    reviewed = _events(db, project, EventType.CHANGESET_REVIEWED)
    assert reviewed[-1].payload["reason"] == "Superseded by the revised motor template."


# ---------------------------------------------------------------------------
# Forward references
# ---------------------------------------------------------------------------
def test_a_temp_reference_round_trips() -> None:
    reference = changesets.temp_ref("dev-1")
    assert reference == "temp:dev-1"
    assert changesets.is_temp_reference(reference)
    assert changesets.temp_key_of(reference) == "dev-1"
    assert not changesets.is_temp_reference("dev-1")
    assert changesets.temp_key_of("dev-1") == ""


def _events(db: Session, project: Project, event_type: EventType) -> list[Event]:
    return list(
        db.execute(
            select(Event)
            .where(Event.project_id == project.id, Event.event_type == event_type)
            .order_by(Event.seq)
        ).scalars()
    )
