"""Turning a validated proposal into a change set.

Nothing here writes to the canonical model. :func:`build_changeset` records what
an agent asked for — ordered, attributable and re-readable — and the merge
decides later whether any of it happens.

Two details carry more weight than they look like they do.

**Idempotency.** The task queue is at-least-once, so a re-run of the same task
must converge rather than propose the same batch twice. A change set is
identified by ``(project_id, idempotency_key)``; building with a key that
already exists returns the existing change set untouched.

**Where the proposal lives.** :class:`~app.models.revision.ChangeOperation`
stores ``before`` and ``after``, and has no separate column for "what was
proposed" — by design, because the proposal *is* the after-state. So ``after``
starts life holding the proposed field values and is finalised at merge time
with what actually landed. The stated basis has nowhere else to go either, so it
travels in the same dict under :data:`BASIS_KEY`, a key no column can collide
with. Read both through :func:`proposed_values` and :func:`stated_basis` rather
than indexing ``after`` directly.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.base.actions import ProposeChangesetPayload
from app.core.clock import utcnow
from app.core.enums import AuditAction, ChangeSetStatus, EventType, ObjectType
from app.core.errors import InvalidStateTransitionError, ValidationError
from app.models.project import Project
from app.models.revision import ChangeOperation, ChangeSet
from app.orchestration.events import record_event
from app.security import audit

__all__ = [
    "BASIS_KEY",
    "RESUBMITTABLE_STATUSES",
    "TEMP_REFERENCE_PREFIX",
    "abandon",
    "build_changeset",
    "find_by_idempotency_key",
    "is_temp_reference",
    "proposed_values",
    "stated_basis",
    "submit",
    "temp_key_of",
    "temp_ref",
]

#: Prefix marking a reference to something an earlier operation in the same
#: change set creates. A change set that creates a device and then a tag bound to
#: it cannot name a real id for the device, because none exists until the merge
#: assigns one — so it writes ``{"device_id": "temp:dev-1"}`` and the merge
#: substitutes the assigned id.
TEMP_REFERENCE_PREFIX = "temp:"

#: Reserved key inside ``ChangeOperation.after`` holding the operation's stated
#: basis. ``$`` cannot begin a Python identifier, so it can never collide with a
#: column name.
BASIS_KEY = "$basis"

#: Statuses a change set may be (re-)submitted from. A change set that came back
#: conflicted or failed validation is corrected and submitted again rather than
#: replaced, so its history stays in one place.
RESUBMITTABLE_STATUSES: frozenset[ChangeSetStatus] = frozenset(
    {
        ChangeSetStatus.DRAFT,
        ChangeSetStatus.VALIDATION_FAILED,
        ChangeSetStatus.CONFLICTED,
    }
)


def temp_ref(temp_key: str) -> str:
    """Render a forward reference to an object created in the same change set."""
    return f"{TEMP_REFERENCE_PREFIX}{temp_key}"


def is_temp_reference(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(TEMP_REFERENCE_PREFIX)


def temp_key_of(value: Any) -> str:
    """The key a temp reference names, or ``""`` if it is not one."""
    return str(value)[len(TEMP_REFERENCE_PREFIX) :] if is_temp_reference(value) else ""


def proposed_values(operation: ChangeOperation) -> dict[str, Any]:
    """The field values an operation proposes, without the reserved keys."""
    return {key: value for key, value in operation.after.items() if not key.startswith("$")}


def stated_basis(operation: ChangeOperation) -> list[dict[str, Any]]:
    """The basis entries recorded with an operation, as plain dictionaries."""
    raw = operation.after.get(BASIS_KEY)
    return [entry for entry in raw if isinstance(entry, dict)] if isinstance(raw, list) else []


def find_by_idempotency_key(
    db: Session, project_id: uuid.UUID, idempotency_key: str
) -> ChangeSet | None:
    """The change set already built for this key, if any."""
    if not idempotency_key:
        return None
    return (
        db.execute(
            select(ChangeSet)
            .where(
                ChangeSet.project_id == project_id,
                ChangeSet.idempotency_key == idempotency_key,
            )
            .order_by(ChangeSet.created_at)
            .limit(1)
        )
        .scalars()
        .first()
    )


def build_changeset(
    db: Session,
    *,
    project: Project,
    payload: ProposeChangesetPayload,
    author_agent: str = "",
    task_id: uuid.UUID | None = None,
    agent_run_id: uuid.UUID | None = None,
    idempotency_key: str = "",
    author_user_id: uuid.UUID | None = None,
    allowed_object_types: Sequence[ObjectType] = (),
    confidence: float = 0.0,
) -> ChangeSet:
    """Record a proposal as a change set plus its ordered operations.

    ``allowed_object_types`` is the scope the agent was *given*, which is why it
    is a parameter here rather than a field of the payload: an agent does not get
    to widen its own remit by declaring a wider scope, and the merge enforces
    this list rather than treating it as advice. An empty sequence means
    unrestricted.
    """
    existing = find_by_idempotency_key(db, project.id, idempotency_key)
    if existing is not None:
        return existing

    changeset = ChangeSet(
        project_id=project.id,
        base_revision_number=payload.base_revision_number,
        title=payload.title,
        summary=payload.summary,
        rationale=payload.rationale,
        status=ChangeSetStatus.DRAFT,
        author_agent=author_agent,
        author_user_id=author_user_id,
        task_id=task_id,
        agent_run_id=agent_run_id,
        allowed_object_types=[object_type.value for object_type in allowed_object_types],
        idempotency_key=idempotency_key,
        operation_count=len(payload.operations),
        confidence=confidence,
        assumptions=list(payload.assumptions),
        warnings=list(payload.warnings),
        requires_human_approval=payload.requires_human_approval,
        restricted_domains=list(payload.restricted_domains),
    )
    db.add(changeset)
    db.flush()

    for ordinal, proposal in enumerate(payload.operations):
        after: dict[str, Any] = dict(proposal.values)
        if proposal.basis:
            after[BASIS_KEY] = [
                basis.model_dump(mode="json", exclude_none=True) for basis in proposal.basis
            ]
        db.add(
            ChangeOperation(
                project_id=project.id,
                change_set_id=changeset.id,
                ordinal=ordinal,
                operation_type=proposal.operation_type,
                object_type=proposal.object_type,
                object_id=proposal.object_id,
                temp_key=proposal.temp_key,
                based_on_object_revision=proposal.based_on_object_revision,
                field_name=proposal.field_name,
                before={},
                after=after,
                related_object_type=proposal.related_object_type,
                related_object_id=proposal.related_object_id,
                relationship_key=proposal.relationship_key,
                description=proposal.description,
            )
        )
    db.flush()

    audit.record(
        db,
        AuditAction.CHANGESET_CREATED,
        actor_user_id=author_user_id,
        actor_agent_kind=author_agent,
        project_id=project.id,
        target_type="change_set",
        target_id=changeset.id,
        summary=f"Proposed change set '{changeset.title}'",
        details={
            "operation_count": changeset.operation_count,
            "base_revision_number": changeset.base_revision_number,
            "idempotency_key": idempotency_key,
        },
    )
    record_event(
        db,
        project_id=project.id,
        event_type=EventType.CHANGESET_CREATED,
        actor_user_id=author_user_id,
        task_id=task_id,
        payload={
            "changeset_id": str(changeset.id),
            "title": changeset.title,
            "status": changeset.status.value,
            "operation_count": changeset.operation_count,
            "base_revision_number": changeset.base_revision_number,
            "requires_human_approval": changeset.requires_human_approval,
        },
    )
    return changeset


def submit(
    db: Session, changeset: ChangeSet, *, actor_user_id: uuid.UUID | None = None
) -> ChangeSet:
    """Move a change set out of draft and into the merge queue."""
    if changeset.status not in RESUBMITTABLE_STATUSES:
        raise InvalidStateTransitionError(
            "change set", changeset.status.value, ChangeSetStatus.SUBMITTED.value
        )

    changeset.status = ChangeSetStatus.SUBMITTED
    changeset.submitted_at = utcnow()
    changeset.disposition_reason = ""
    db.flush()

    audit.record(
        db,
        AuditAction.CHANGESET_SUBMITTED,
        actor_user_id=actor_user_id,
        actor_agent_kind=changeset.author_agent,
        project_id=changeset.project_id,
        target_type="change_set",
        target_id=changeset.id,
        summary=f"Submitted change set '{changeset.title}'",
        details={"operation_count": changeset.operation_count},
    )
    record_event(
        db,
        project_id=changeset.project_id,
        event_type=EventType.CHANGESET_SUBMITTED,
        actor_user_id=actor_user_id,
        task_id=changeset.task_id,
        payload={
            "changeset_id": str(changeset.id),
            "title": changeset.title,
            "status": changeset.status.value,
        },
    )
    return changeset


def abandon(
    db: Session, changeset: ChangeSet, reason: str, *, actor_user_id: uuid.UUID | None = None
) -> ChangeSet:
    """Close a change set that will never be merged, saying why.

    The reason is required. A change set that vanishes without one leaves the
    next person to look at the project unable to tell whether the work was
    rejected, superseded or simply forgotten.
    """
    cleaned = reason.strip()
    if not cleaned:
        raise ValidationError("Abandoning a change set requires a reason.")
    if not changeset.is_open:
        raise InvalidStateTransitionError(
            "change set", changeset.status.value, ChangeSetStatus.ABANDONED.value
        )

    changeset.status = ChangeSetStatus.ABANDONED
    changeset.disposition_reason = cleaned
    db.flush()

    audit.record(
        db,
        AuditAction.CHANGESET_REJECTED,
        actor_user_id=actor_user_id,
        actor_agent_kind=changeset.author_agent,
        project_id=changeset.project_id,
        target_type="change_set",
        target_id=changeset.id,
        summary=f"Abandoned change set '{changeset.title}'",
        details={"reason": cleaned},
    )
    record_event(
        db,
        project_id=changeset.project_id,
        event_type=EventType.CHANGESET_REVIEWED,
        actor_user_id=actor_user_id,
        task_id=changeset.task_id,
        payload={
            "changeset_id": str(changeset.id),
            "title": changeset.title,
            "status": changeset.status.value,
            "reason": cleaned,
        },
    )
    return changeset
