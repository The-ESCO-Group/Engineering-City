"""Validate a change set, detect what moved underneath it, and merge it — or not.

This is the only door into the canonical engineering model. Everything an agent
proposes arrives here, and four rules hold without exception:

* **Never partially apply.** Either every operation lands or none does. The work
  happens inside a ``SAVEPOINT`` so that "operation 3 of 4 conflicts" leaves the
  database exactly as it was, rather than half-changed in a way nobody planned.
* **Never silently skip an operation.** An operation that cannot be applied is a
  conflict or a validation error carrying a message, and the merge stops. There
  is no path through this module that drops an operation quietly.
* **Never overwrite an approved object.** Superseding something an engineer
  signed off is an explicit, separate act.
* **A conflict names the object, the field and both competing values.** "The
  object changed" leaves a reviewer with nothing to decide.

**Dry run.** A rehearsal takes the same route as the real thing: it allocates a
revision number, applies every operation and then rolls the savepoint back. If
the rehearsal ran different code it would prove nothing about the merge. What a
dry run does *not* do is announce itself — no events, no audit entry, no status
change on the change set — because nothing happened. The
:class:`~app.models.revision.MergeResult` row it writes, with ``is_dry_run`` set,
is the durable record that the rehearsal took place and what it found.

**Base revisions are not compared.** A change set written against revision 4 and
merged at revision 9 is fine, and deliberately so: conflict detection is
per-object, via ``object_revision``, so unrelated concurrent work does not
invalidate a proposal that touches nothing it touched.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.clock import utcnow
from app.core.enums import (
    TRACEABLE_OBJECT_TYPES,
    ApprovalStatus,
    AuditAction,
    ChangeOperationType,
    ChangeSetStatus,
    ConflictKind,
    EventType,
    EvidenceKind,
    LogicOrigin,
    MergeOutcome,
    ObjectType,
)
from app.engineering import registry
from app.engineering.changesets import (
    BASIS_KEY,
    is_temp_reference,
    proposed_values,
    stated_basis,
    temp_key_of,
)
from app.engineering.revisions import (
    allocate_revision_number,
    create_revision,
    ensure_initial_revision,
)
from app.models.approval import Approval
from app.models.core import ObjectEvidenceLink
from app.models.evidence import EvidenceAnchor
from app.models.profile import ProjectDecision
from app.models.project import Project
from app.models.requirements import Requirement
from app.models.revision import ChangeOperation, ChangeSet, MergeConflict, MergeResult
from app.orchestration.events import record_event
from app.security import audit

__all__ = ["MERGEABLE_STATUSES", "merge_changeset"]


#: Statuses a merge may be attempted from.
#:
#: ``DRAFT`` is excluded on purpose, for dry runs as well as real merges: a dry
#: run promises to report what the real merge would do, and if a draft could be
#: rehearsed but not merged that promise would already be broken before any
#: engineering content was examined. Submit first, then rehearse.
MERGEABLE_STATUSES: frozenset[ChangeSetStatus] = frozenset(
    {
        ChangeSetStatus.SUBMITTED,
        ChangeSetStatus.VALIDATING,
        ChangeSetStatus.VALIDATION_FAILED,
        ChangeSetStatus.IN_REVIEW,
        ChangeSetStatus.AWAITING_APPROVAL,
        ChangeSetStatus.APPROVED,
        ChangeSetStatus.CONFLICTED,
    }
)

#: Approval statuses that let a gated change set through. ``RISK_ACCEPTED`` is
#: included because a named engineer knowingly carrying the risk is a decision;
#: it stays distinguishable from ``APPROVED`` in the approval record itself.
_SETTLED_APPROVALS: frozenset[ApprovalStatus] = frozenset(
    {ApprovalStatus.APPROVED, ApprovalStatus.RISK_ACCEPTED}
)

#: Operations that bring a new object into existence.
_CREATE_OPERATIONS: frozenset[ChangeOperationType] = frozenset(
    {
        ChangeOperationType.CREATE_OBJECT,
        ChangeOperationType.CREATE_LOGIC,
        ChangeOperationType.CREATE_FINDING,
        ChangeOperationType.CREATE_EXPORT,
        ChangeOperationType.REQUEST_CLARIFICATION,
    }
)

#: Operations that retire one.
_DELETE_OPERATIONS: frozenset[ChangeOperationType] = frozenset({ChangeOperationType.DELETE_OBJECT})

#: The state a change set is left in by each outcome that settles it. ``MERGED``
#: is absent because the caller sets it alongside the revision number, and
#: ``DRY_RUN_OK``/``REFUSED`` are absent because neither changes anything.
_STATUS_FOR_OUTCOME: Mapping[MergeOutcome, ChangeSetStatus] = {
    MergeOutcome.CONFLICT: ChangeSetStatus.CONFLICTED,
    MergeOutcome.VALIDATION_FAILED: ChangeSetStatus.VALIDATION_FAILED,
    MergeOutcome.APPROVAL_REQUIRED: ChangeSetStatus.AWAITING_APPROVAL,
}


# ---------------------------------------------------------------------------
# Working types. None of these are persisted directly; they are the merge's
# notes on itself, turned into rows once the outcome is known.
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class _Conflict:
    """One reason the merge cannot proceed, with everything a reviewer needs."""

    kind: ConflictKind
    object_type: ObjectType
    message: str
    operation_id: uuid.UUID | None = None
    object_id: uuid.UUID | None = None
    object_label: str = ""
    field_name: str = ""
    expected_revision: int | None = None
    actual_revision: int | None = None
    proposed_value: dict[str, Any] = dataclass_field(default_factory=dict)
    current_value: dict[str, Any] = dataclass_field(default_factory=dict)
    suggested_resolution: str = ""


@dataclass(slots=True)
class _Issue:
    """A validation error: the proposal is malformed, not merely out of date."""

    message: str
    ordinal: int
    object_type: ObjectType
    object_label: str = ""
    field_name: str = ""

    def render(self) -> str:
        return f"operation {self.ordinal}: {self.message}"


@dataclass(slots=True)
class _Plan:
    """A resolved, checked operation, ready to apply verbatim."""

    operation: ChangeOperation
    object_type: ObjectType
    model: type[Any]
    fields: dict[str, Any]
    basis: list[dict[str, Any]]
    label: str
    creates: bool
    deletes: bool
    target: Any = None


@dataclass(slots=True)
class _Applied:
    """What one operation actually did, captured before any rollback."""

    operation: ChangeOperation
    object_type: ObjectType
    object_id: uuid.UUID
    label: str
    before: dict[str, Any]
    after: dict[str, Any]
    creates: bool
    deletes: bool


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def merge_changeset(
    db: Session,
    *,
    changeset: ChangeSet,
    actor_user_id: uuid.UUID | None = None,
    dry_run: bool = False,
) -> MergeResult:
    """Merge a change set into the canonical model, or explain why not.

    Returns a :class:`MergeResult` in every case, including refusal. The result
    is the record: its ``explanation`` is written for a human, and its conflicts
    name objects, fields and both competing values.
    """
    project = db.get(Project, changeset.project_id)
    if project is None:  # pragma: no cover - a change set cannot outlive its project
        raise LookupError(f"change set {changeset.id} references a project that no longer exists")

    head = project.current_revision_number
    operations = _operations_of(db, changeset)

    refusal = _refusal_reason(project, changeset)
    if refusal:
        return _record(
            db,
            project=project,
            changeset=changeset,
            outcome=MergeOutcome.REFUSED,
            head=head,
            dry_run=dry_run,
            explanation=f"Refused. {refusal} Nothing was applied.",
            operations_total=len(operations),
            actor_user_id=actor_user_id,
        )

    if not operations:
        return _record(
            db,
            project=project,
            changeset=changeset,
            outcome=MergeOutcome.NO_OP,
            head=head,
            dry_run=dry_run,
            explanation="The change set contains no operations, so there was nothing to merge.",
            operations_total=0,
            actor_user_id=actor_user_id,
        )

    plans, conflicts, issues = _examine(
        db, project=project, changeset=changeset, operations=operations
    )

    if conflicts or issues:
        outcome = MergeOutcome.CONFLICT if conflicts else MergeOutcome.VALIDATION_FAILED
        return _record(
            db,
            project=project,
            changeset=changeset,
            outcome=outcome,
            head=head,
            dry_run=dry_run,
            explanation=_explain_refusal(conflicts, issues, len(operations)),
            operations_total=len(operations),
            conflicts=conflicts,
            issues=issues,
            actor_user_id=actor_user_id,
        )

    gate = _approval_gate_reason(db, changeset)
    if gate:
        return _record(
            db,
            project=project,
            changeset=changeset,
            outcome=MergeOutcome.APPROVAL_REQUIRED,
            head=head,
            dry_run=dry_run,
            explanation=f"Nothing was applied. {gate}",
            operations_total=len(operations),
            actor_user_id=actor_user_id,
        )

    # Everything below is the merge itself. It runs identically for a dry run;
    # only the savepoint's fate differs. Rolling the savepoint back discards the
    # events recorded inside it and leaves the caller's earlier ones intact --
    # the event buffer is savepoint-aware, so a rehearsal has no side effect in
    # either direction.
    savepoint = db.begin_nested()
    try:
        revision_number = allocate_revision_number(db, project)
        applied = _apply(
            db, project=project, changeset=changeset, plans=plans, revision=revision_number
        )
    except Exception:
        savepoint.rollback()
        raise

    if dry_run:
        savepoint.rollback()
    else:
        savepoint.commit()

    _write_back(applied, dry_run=dry_run)
    counts = _object_counts(applied)

    if dry_run:
        return _record(
            db,
            project=project,
            changeset=changeset,
            outcome=MergeOutcome.DRY_RUN_OK,
            head=head,
            dry_run=True,
            explanation=(
                f"Dry run: {_describe_work(applied, counts)} "
                f"A real merge would apply all {len(applied)} operation(s) and create "
                f"revision {revision_number}. Nothing was written."
            ),
            operations_total=len(operations),
            operations_applied=0,
            actor_user_id=actor_user_id,
        )

    ensure_initial_revision(db, project)
    summary = changeset.summary or changeset.title
    revision = create_revision(
        db,
        project,
        changeset=changeset,
        summary=summary,
        object_counts=counts,
        number=revision_number,
        operation_count=len(applied),
        actor_user_id=actor_user_id,
    )

    changeset.status = ChangeSetStatus.MERGED
    changeset.merged_revision_number = revision.number
    changeset.merged_at = utcnow()
    changeset.disposition_reason = ""
    db.flush()

    explanation = (
        f"Merged {len(applied)} operation(s) into revision {revision.number}. "
        f"{_describe_work(applied, counts)}"
    )
    result = _record(
        db,
        project=project,
        changeset=changeset,
        outcome=MergeOutcome.MERGED,
        head=head,
        dry_run=False,
        explanation=explanation,
        operations_total=len(operations),
        operations_applied=len(applied),
        resulting_revision_number=revision.number,
        actor_user_id=actor_user_id,
    )

    audit.record(
        db,
        AuditAction.CHANGESET_MERGED,
        actor_user_id=actor_user_id,
        actor_agent_kind=changeset.author_agent,
        project_id=project.id,
        target_type="change_set",
        target_id=changeset.id,
        summary=f"Merged change set '{changeset.title}' into revision {revision.number}",
        details={
            "revision_number": revision.number,
            "operations_applied": len(applied),
            "object_counts": counts,
        },
    )
    record_event(
        db,
        project_id=project.id,
        event_type=EventType.CHANGESET_MERGED,
        actor_user_id=actor_user_id,
        task_id=changeset.task_id,
        payload={
            "changeset_id": str(changeset.id),
            "title": changeset.title,
            "revision_number": revision.number,
            "operations_applied": len(applied),
            "object_counts": counts,
        },
    )
    return result


# ---------------------------------------------------------------------------
# Step 1 — refusal
# ---------------------------------------------------------------------------
def _refusal_reason(project: Project, changeset: ChangeSet) -> str:
    if project.is_archived:
        return f"Project '{project.name}' is archived and read-only."
    if changeset.status is ChangeSetStatus.MERGED:
        return (
            f"This change set was already merged into revision {changeset.merged_revision_number}."
        )
    if changeset.status not in MERGEABLE_STATUSES:
        return (
            f"A change set in state '{changeset.status.value}' cannot be merged; "
            f"it must be one of "
            f"{', '.join(sorted(status.value for status in MERGEABLE_STATUSES))}."
        )
    return ""


def _operations_of(db: Session, changeset: ChangeSet) -> list[ChangeOperation]:
    return list(
        db.execute(
            select(ChangeOperation)
            .where(ChangeOperation.change_set_id == changeset.id)
            .order_by(ChangeOperation.ordinal)
        ).scalars()
    )


# ---------------------------------------------------------------------------
# Steps 2-4 — scope, conflicts, validation
# ---------------------------------------------------------------------------
def _examine(
    db: Session,
    *,
    project: Project,
    changeset: ChangeSet,
    operations: Sequence[ChangeOperation],
) -> tuple[list[_Plan], list[_Conflict], list[_Issue]]:
    """One ordered pass over the operations, collecting everything wrong.

    Everything, deliberately: stopping at the first problem would send an author
    round the loop once per defect, and a reviewer looking at a conflicted merge
    wants the whole picture in front of them at once.
    """
    plans: list[_Plan] = []
    conflicts: list[_Conflict] = []
    issues: list[_Issue] = []

    allowed = _allowed_types(changeset)
    #: temp_key -> the object type the creating operation will produce.
    pending: dict[str, ObjectType] = {}
    #: Natural keys already claimed by an earlier create in this same change set.
    claimed: set[tuple[Any, ...]] = set()

    for operation in operations:
        object_type = operation.object_type

        if allowed and object_type not in allowed:
            conflicts.append(
                _Conflict(
                    kind=ConflictKind.SCOPE_VIOLATION,
                    object_type=object_type,
                    operation_id=operation.id,
                    object_id=operation.object_id,
                    object_label=registry.describe(object_type, object_id=operation.object_id),
                    message=(
                        f"Operation {operation.ordinal} targets "
                        f"{registry.type_label(object_type).lower()}, which is outside this "
                        f"change set's scope "
                        f"({', '.join(sorted(t.value for t in allowed))})."
                    ),
                    suggested_resolution=(
                        "Propose this change from an agent whose scope covers the object "
                        "type, or widen the change set's scope deliberately."
                    ),
                )
            )
            continue

        if not registry.is_writable(object_type):
            issues.append(
                _Issue(
                    message=str(_unwritable_message(object_type)),
                    ordinal=operation.ordinal,
                    object_type=object_type,
                )
            )
            continue

        model = registry.model_for(object_type)
        creates = operation.operation_type in _CREATE_OPERATIONS
        deletes = operation.operation_type in _DELETE_OPERATIONS

        fields, shape_error = _intended_fields(operation)
        if shape_error:
            issues.append(
                _Issue(
                    message=shape_error,
                    ordinal=operation.ordinal,
                    object_type=object_type,
                    field_name=operation.field_name,
                )
            )
            continue

        if creates:
            plan = _examine_create(
                db,
                project=project,
                operation=operation,
                model=model,
                fields=fields,
                pending=pending,
                claimed=claimed,
                conflicts=conflicts,
                issues=issues,
            )
        else:
            plan = _examine_existing(
                db,
                project=project,
                operation=operation,
                model=model,
                fields=fields,
                deletes=deletes,
                pending=pending,
                conflicts=conflicts,
                issues=issues,
            )
        if plan is not None:
            plans.append(plan)

    return plans, conflicts, issues


def _examine_create(
    db: Session,
    *,
    project: Project,
    operation: ChangeOperation,
    model: type[Any],
    fields: dict[str, Any],
    pending: dict[str, ObjectType],
    claimed: set[tuple[Any, ...]],
    conflicts: list[_Conflict],
    issues: list[_Issue],
) -> _Plan | None:
    object_type = operation.object_type
    label = _proposed_label(object_type, fields)

    if operation.object_id is not None:
        issues.append(
            _Issue(
                message=(
                    f"{operation.operation_type.value} names object_id "
                    f"{operation.object_id}, but a create is assigned its id at merge time"
                ),
                ordinal=operation.ordinal,
                object_type=object_type,
                object_label=label,
            )
        )
        return None

    basis = stated_basis(operation)
    if object_type in TRACEABLE_OBJECT_TYPES and not basis:
        issues.append(
            _Issue(
                message=(
                    f"creating a {object_type.value} requires at least one basis "
                    "(a requirement, a template rule, a human decision or a cited "
                    "document); an object that exists for no stated reason cannot be "
                    "reviewed"
                ),
                ordinal=operation.ordinal,
                object_type=object_type,
                object_label=label,
            )
        )
        return None

    coerced = _coerce_fields(operation, fields, issues, label)
    if coerced is None:
        return None

    absent = sorted(registry.required_fields(object_type) - set(coerced))
    if absent:
        issues.append(
            _Issue(
                message=(
                    f"creating a {object_type.value} requires "
                    f"{', '.join(repr(name) for name in absent)}, which the operation does "
                    f"not supply"
                ),
                ordinal=operation.ordinal,
                object_type=object_type,
                object_label=label,
                field_name=absent[0],
            )
        )
        return None

    _check_references(
        db,
        project=project,
        operation=operation,
        fields=coerced,
        label=label,
        pending=pending,
        conflicts=conflicts,
        issues=issues,
    )
    _check_basis(
        db,
        project=project,
        operation=operation,
        basis=basis,
        label=label,
        conflicts=conflicts,
        issues=issues,
    )
    _check_duplicate(
        db,
        project=project,
        operation=operation,
        model=model,
        fields=coerced,
        label=label,
        claimed=claimed,
        conflicts=conflicts,
    )

    if operation.temp_key:
        pending[operation.temp_key] = object_type

    return _Plan(
        operation=operation,
        object_type=object_type,
        model=model,
        fields=coerced,
        basis=basis,
        label=label,
        creates=True,
        deletes=False,
    )


def _examine_existing(
    db: Session,
    *,
    project: Project,
    operation: ChangeOperation,
    model: type[Any],
    fields: dict[str, Any],
    deletes: bool,
    pending: dict[str, ObjectType],
    conflicts: list[_Conflict],
    issues: list[_Issue],
) -> _Plan | None:
    object_type = operation.object_type

    if operation.object_id is None:
        issues.append(
            _Issue(
                message=(f"{operation.operation_type.value} must name the object_id it changes"),
                ordinal=operation.ordinal,
                object_type=object_type,
            )
        )
        return None

    target = db.get(model, operation.object_id)
    if target is None or target.project_id != project.id:
        conflicts.append(
            _Conflict(
                kind=ConflictKind.OBJECT_MISSING,
                object_type=object_type,
                operation_id=operation.id,
                object_id=operation.object_id,
                object_label=registry.describe(object_type, object_id=operation.object_id),
                message=(
                    f"{registry.describe(object_type, object_id=operation.object_id)} does not "
                    f"exist in this project, so operation {operation.ordinal} has nothing to "
                    f"change."
                ),
                suggested_resolution=(
                    "Re-read the current model and propose the change against an object "
                    "that exists."
                ),
            )
        )
        return None

    label = registry.label_for(target)

    if target.is_deleted:
        conflicts.append(
            _Conflict(
                kind=ConflictKind.OBJECT_DELETED,
                object_type=object_type,
                operation_id=operation.id,
                object_id=target.id,
                object_label=label,
                message=(
                    f"{label} was deleted in revision {target.updated_in_revision}; a deleted "
                    f"object cannot be edited."
                ),
                current_value={"deleted_at": registry.jsonable(target.deleted_at)},
                proposed_value=registry.jsonable(fields),
                suggested_resolution=(
                    "Create a replacement object rather than editing the tombstone."
                ),
            )
        )
        return None

    if target.is_protected:
        conflicts.append(
            _Conflict(
                kind=ConflictKind.PROTECTED_OBJECT,
                object_type=object_type,
                operation_id=operation.id,
                object_id=target.id,
                object_label=label,
                field_name=next(iter(fields), ""),
                actual_revision=target.object_revision,
                expected_revision=operation.based_on_object_revision,
                proposed_value=registry.jsonable(fields),
                current_value=registry.jsonable(_current_of(target, fields)),
                message=(
                    f"{label} is approved and cannot be edited in place. Operation "
                    f"{operation.ordinal} would change "
                    f"{_field_list(fields)}."
                ),
                suggested_resolution=(
                    "Supersede the approved object with a replacement and win a fresh "
                    "approval. Superseding is an explicit act, never a side effect of an "
                    "edit."
                ),
            )
        )
        return None

    if operation.based_on_object_revision is None:
        issues.append(
            _Issue(
                message=(
                    f"{operation.operation_type.value} must state based_on_object_revision; "
                    "without it a concurrent edit cannot be detected and would be silently "
                    "overwritten"
                ),
                ordinal=operation.ordinal,
                object_type=object_type,
                object_label=label,
                field_name="based_on_object_revision",
            )
        )
        return None

    if operation.based_on_object_revision != target.object_revision:
        conflicts.append(
            _Conflict(
                kind=ConflictKind.STALE_REVISION,
                object_type=object_type,
                operation_id=operation.id,
                object_id=target.id,
                object_label=label,
                field_name=next(iter(fields), ""),
                expected_revision=operation.based_on_object_revision,
                actual_revision=target.object_revision,
                proposed_value=registry.jsonable(fields),
                current_value=registry.jsonable(_current_of(target, fields)),
                message=(
                    f"{label} was written against object revision "
                    f"{operation.based_on_object_revision} but is now at revision "
                    f"{target.object_revision}. "
                    f"{_value_comparison(target, fields)}"
                ),
                suggested_resolution=(
                    "Re-read the object at its current revision and re-propose, keeping "
                    "whichever value is correct."
                ),
            )
        )
        return None

    coerced = _coerce_fields(operation, fields, issues, label) if not deletes else {}
    if coerced is None:
        return None

    if operation.operation_type is ChangeOperationType.RENAME_TAG:
        expected_name = str(proposed_values(operation).get("from_name", ""))
        current_name = str(getattr(target, "name", ""))
        if expected_name != current_name:
            conflicts.append(
                _Conflict(
                    kind=ConflictKind.CONCURRENT_FIELD_EDIT,
                    object_type=object_type,
                    operation_id=operation.id,
                    object_id=target.id,
                    object_label=label,
                    field_name="name",
                    expected_revision=operation.based_on_object_revision,
                    actual_revision=target.object_revision,
                    proposed_value={"from_name": expected_name, "to_name": coerced.get("name")},
                    current_value={"name": current_name},
                    message=(
                        f"The rename states the current name is {expected_name!r}, but "
                        f"{label} is named {current_name!r}."
                    ),
                    suggested_resolution=(
                        "Re-read the tag and re-propose the rename from its current name, "
                        "with a fresh impact report."
                    ),
                )
            )
            return None

    if not deletes:
        _check_references(
            db,
            project=project,
            operation=operation,
            fields=coerced,
            label=label,
            pending=pending,
            conflicts=conflicts,
            issues=issues,
        )

    basis = stated_basis(operation)
    if basis:
        _check_basis(
            db,
            project=project,
            operation=operation,
            basis=basis,
            label=label,
            conflicts=conflicts,
            issues=issues,
        )

    return _Plan(
        operation=operation,
        object_type=object_type,
        model=model,
        fields=coerced,
        basis=basis,
        label=label,
        creates=False,
        deletes=deletes,
        target=target,
    )


def _allowed_types(changeset: ChangeSet) -> frozenset[ObjectType]:
    """The scope the change set was given. Empty means unrestricted."""
    allowed: set[ObjectType] = set()
    for raw in changeset.allowed_object_types:
        try:
            allowed.add(ObjectType(raw))
        except ValueError:  # pragma: no cover - written by this layer only
            continue
    return frozenset(allowed)


def _unwritable_message(object_type: ObjectType) -> str:
    try:
        registry.model_for(object_type)
    except registry.UnknownObjectTypeError as exc:
        return str(exc)
    return ""  # pragma: no cover - only reached if the registry changes mid-call


def _intended_fields(operation: ChangeOperation) -> tuple[dict[str, Any], str]:
    """The columns this operation intends to write, whatever its shape.

    Every operation type is reduced to a plain field map here so that the
    allowlist, the coercion and the reference checks below run once, uniformly,
    rather than once per operation type with the differences quietly diverging.
    """
    values = proposed_values(operation)
    kind = operation.operation_type

    if kind is ChangeOperationType.UPDATE_FIELD:
        if not operation.field_name:
            return {}, "update_field must name the field it changes"
        if operation.field_name not in values:
            return {}, (
                f"update_field names field {operation.field_name!r} but carries no value for it"
            )
        return {operation.field_name: values[operation.field_name]}, ""

    if kind is ChangeOperationType.RENAME_TAG:
        to_name = values.get("to_name")
        if not values.get("from_name") or not to_name:
            return {}, "rename_tag requires both from_name and to_name"
        return {"name": to_name}, ""

    if kind in {ChangeOperationType.LINK_OBJECTS, ChangeOperationType.UNLINK_OBJECTS}:
        if not operation.relationship_key:
            return {}, f"{kind.value} must name the relationship_key it sets"
        linking = kind is ChangeOperationType.LINK_OBJECTS
        if linking and operation.related_object_id is None:
            return {}, "link_objects must name the related_object_id it links to"
        return {operation.relationship_key: operation.related_object_id if linking else None}, ""

    if kind in _DELETE_OPERATIONS:
        return {}, ""

    return dict(values), ""


def _coerce_fields(
    operation: ChangeOperation,
    fields: dict[str, Any],
    issues: list[_Issue],
    label: str,
) -> dict[str, Any] | None:
    """Check the allowlist and convert JSON into what the columns store.

    Returns ``None`` when anything was wrong, having recorded an issue naming the
    field — a proposal that names a protected column is a defect worth reporting
    precisely, not a value to drop on the floor.
    """
    object_type = operation.object_type
    allowed = registry.allowed_fields(object_type)
    coerced: dict[str, Any] = {}
    failed = False

    for name, value in fields.items():
        if name in registry.PROTECTED_FIELDS:
            issues.append(
                _Issue(
                    message=(
                        f"field {name!r} is written by the merge, not by a change set; "
                        f"an operation may not set it"
                    ),
                    ordinal=operation.ordinal,
                    object_type=object_type,
                    object_label=label,
                    field_name=name,
                )
            )
            failed = True
            continue
        if name not in allowed:
            issues.append(
                _Issue(
                    message=(
                        f"field {name!r} is not a field of "
                        f"{registry.type_label(object_type).lower()}"
                    ),
                    ordinal=operation.ordinal,
                    object_type=object_type,
                    object_label=label,
                    field_name=name,
                )
            )
            failed = True
            continue
        if is_temp_reference(value):
            # Resolved at apply time, once the earlier operation has an id.
            coerced[name] = value
            continue
        try:
            coerced[name] = registry.coerce_value(object_type, name, value)
        except registry.FieldValueError as exc:
            issues.append(
                _Issue(
                    message=exc.message,
                    ordinal=operation.ordinal,
                    object_type=object_type,
                    object_label=label,
                    field_name=exc.field,
                )
            )
            failed = True

    return None if failed else coerced


def _check_references(
    db: Session,
    *,
    project: Project,
    operation: ChangeOperation,
    fields: dict[str, Any],
    label: str,
    pending: dict[str, ObjectType],
    conflicts: list[_Conflict],
    issues: list[_Issue],
) -> None:
    """Every foreign key must name something that is actually there."""
    targets = registry.foreign_key_targets(operation.object_type)

    for name, value in fields.items():
        if value is None:
            continue
        referenced = targets.get(name)

        if is_temp_reference(value):
            key = temp_key_of(value)
            created_type = pending.get(key)
            if created_type is None:
                conflicts.append(
                    _Conflict(
                        kind=ConflictKind.DEPENDENCY_MISSING,
                        object_type=operation.object_type,
                        operation_id=operation.id,
                        object_id=operation.object_id,
                        object_label=label,
                        field_name=name,
                        proposed_value={name: value},
                        message=(
                            f"{label}: field {name!r} references temp_key {key!r}, which no "
                            f"earlier operation in this change set creates."
                        ),
                        suggested_resolution=(
                            "Order the operations so the object is created before it is "
                            "referenced, or name a real object id."
                        ),
                    )
                )
            elif referenced is not None and created_type is not referenced:
                issues.append(
                    _Issue(
                        message=(
                            f"field {name!r} expects a {referenced.value} but temp_key "
                            f"{key!r} creates a {created_type.value}"
                        ),
                        ordinal=operation.ordinal,
                        object_type=operation.object_type,
                        object_label=label,
                        field_name=name,
                    )
                )
            continue

        if referenced is None or not isinstance(value, uuid.UUID):
            continue
        related: Any = db.get(registry.model_for(referenced), value)
        if related is None or related.project_id != project.id or related.is_deleted:
            conflicts.append(
                _Conflict(
                    kind=ConflictKind.DEPENDENCY_MISSING,
                    object_type=operation.object_type,
                    operation_id=operation.id,
                    object_id=operation.object_id,
                    object_label=label,
                    field_name=name,
                    proposed_value={name: str(value)},
                    message=(
                        f"{label}: field {name!r} names "
                        f"{registry.describe(referenced, object_id=value)}, which is not "
                        f"present in this project."
                    ),
                    suggested_resolution=(
                        "Create the referenced object in the same change set, before the "
                        "operation that needs it."
                    ),
                )
            )


def _check_basis(
    db: Session,
    *,
    project: Project,
    operation: ChangeOperation,
    basis: Sequence[dict[str, Any]],
    label: str,
    conflicts: list[_Conflict],
    issues: list[_Issue],
) -> None:
    """A stated basis must resolve to something a human can go and read."""
    for entry in basis:
        raw_kind = entry.get("kind", "")
        try:
            EvidenceKind(raw_kind)
        except ValueError:
            issues.append(
                _Issue(
                    message=f"basis names an unknown evidence kind {raw_kind!r}",
                    ordinal=operation.ordinal,
                    object_type=operation.object_type,
                    object_label=label,
                    field_name="basis",
                )
            )
            continue

        resolved, missing = _resolve_basis(db, project, entry)
        if missing:
            conflicts.append(
                _Conflict(
                    kind=ConflictKind.DEPENDENCY_MISSING,
                    object_type=operation.object_type,
                    operation_id=operation.id,
                    object_id=operation.object_id,
                    object_label=label,
                    field_name="basis",
                    proposed_value=registry.jsonable(entry),
                    message=f"{label}: {missing}",
                    suggested_resolution=(
                        "Cite a basis that exists, or record the decision or requirement first."
                    ),
                )
            )
        elif not resolved:
            issues.append(
                _Issue(
                    message=(
                        "basis names an anchor, requirement, decision or template — this "
                        "one names none of them, so it asserts traceability without "
                        "providing it"
                    ),
                    ordinal=operation.ordinal,
                    object_type=operation.object_type,
                    object_label=label,
                    field_name="basis",
                )
            )


def _resolve_basis(
    db: Session, project: Project, entry: dict[str, Any]
) -> tuple[dict[str, Any], str]:
    """Turn a stated basis into link columns, or say what could not be found."""
    resolved: dict[str, Any] = {}

    anchor_id = entry.get("anchor_id")
    if anchor_id:
        anchor = db.get(EvidenceAnchor, uuid.UUID(str(anchor_id)))
        if anchor is None or anchor.project_id != project.id:
            return {}, f"basis cites evidence anchor {anchor_id}, which is not in this project."
        resolved["anchor_id"] = anchor.id

    reference = str(entry.get("requirement_reference") or "")
    if reference:
        requirement = db.execute(
            select(Requirement).where(
                Requirement.project_id == project.id, Requirement.reference == reference
            )
        ).scalar_one_or_none()
        if requirement is None:
            return {}, f"basis cites requirement {reference!r}, which is not in this project."
        resolved["requirement_id"] = requirement.id

    decision_reference = str(entry.get("decision_reference") or "")
    if decision_reference:
        decision = db.execute(
            select(ProjectDecision).where(
                ProjectDecision.project_id == project.id,
                ProjectDecision.reference == decision_reference,
            )
        ).scalar_one_or_none()
        if decision is None:
            return {}, (
                f"basis cites decision {decision_reference!r}, which is not recorded in "
                f"this project."
            )
        resolved["decision_id"] = decision.id

    template_id = str(entry.get("template_id") or "")
    if template_id:
        resolved["template_id"] = template_id

    return resolved, ""


def _check_duplicate(
    db: Session,
    *,
    project: Project,
    operation: ChangeOperation,
    model: type[Any],
    fields: dict[str, Any],
    label: str,
    claimed: set[tuple[Any, ...]],
    conflicts: list[_Conflict],
) -> None:
    """A create whose natural key already exists is a conflict, not an update.

    Stricter than the database: a unique constraint over a nullable column does
    not fire for two NULLs, so a second controller-scoped tag of the same name
    would slip past the constraint and only surface as a duplicate in the export.
    """
    object_type = operation.object_type

    for key_columns in registry.natural_keys(object_type):
        effective: dict[str, Any] = {}
        identifying = False
        unresolved = False
        for column in key_columns:
            if column == "project_id":
                effective[column] = project.id
                continue
            value = (
                fields[column] if column in fields else registry.column_default(object_type, column)
            )
            effective[column] = value
            identifying = identifying or value is not None
            unresolved = unresolved or is_temp_reference(value)
        if not identifying:
            continue
        if unresolved:
            # Part of this key is an object an earlier operation has not created
            # yet, so there is nothing to compare against — and querying with the
            # placeholder would fail on the way to the database rather than
            # report anything. A resolved forward reference is never NULL, so the
            # table's own unique constraint is a real backstop for this case,
            # unlike the all-NULL one this check exists to cover.
            continue

        signature = (
            object_type.value,
            tuple(
                sorted((name, str(registry.jsonable(value))) for name, value in effective.items())
            ),
        )
        readable = ", ".join(
            f"{name}={registry.jsonable(value)!r}"
            for name, value in effective.items()
            if name != "project_id"
        )

        if signature in claimed:
            conflicts.append(
                _Conflict(
                    kind=ConflictKind.DUPLICATE_KEY,
                    object_type=object_type,
                    operation_id=operation.id,
                    object_label=label,
                    proposed_value=registry.jsonable(effective),
                    message=(
                        f"{label}: an earlier operation in this change set already creates "
                        f"a {registry.type_label(object_type).lower()} with {readable}."
                    ),
                    suggested_resolution="Remove the duplicate operation.",
                )
            )
            return

        conditions = [
            getattr(model, name).is_(None) if value is None else getattr(model, name) == value
            for name, value in effective.items()
        ]
        existing = (
            db.execute(select(model).where(*conditions, model.deleted_at.is_(None)).limit(1))
            .scalars()
            .first()
        )
        if existing is not None:
            conflicts.append(
                _Conflict(
                    kind=ConflictKind.DUPLICATE_KEY,
                    object_type=object_type,
                    operation_id=operation.id,
                    object_id=existing.id,
                    object_label=registry.label_for(existing),
                    proposed_value=registry.jsonable(effective),
                    current_value={"object_id": str(existing.id)},
                    actual_revision=existing.object_revision,
                    message=(
                        f"{registry.label_for(existing)} already exists with {readable}; "
                        f"operation {operation.ordinal} would create a second one."
                    ),
                    suggested_resolution=(
                        "Update the existing object instead of creating a duplicate, or "
                        "choose a different name."
                    ),
                )
            )
            return

        claimed.add(signature)


# ---------------------------------------------------------------------------
# Step 5 — approval gate
# ---------------------------------------------------------------------------
def _approval_gate_reason(db: Session, changeset: ChangeSet) -> str:
    """Why the human gate blocks this merge, or ``""`` if it does not."""
    if not changeset.requires_human_approval:
        return ""
    if changeset.approval_id is None:
        return (
            "This change set requires a named human approval and none has been requested. "
            "Nothing is merged on an agent's own authority."
        )
    approval = db.get(Approval, changeset.approval_id)
    if approval is None:
        return (
            "This change set names an approval that no longer exists, so there is no "
            "recorded decision to merge on."
        )
    if approval.status in _SETTLED_APPROVALS:
        return ""
    if approval.is_pending:
        return f"Approval '{approval.title}' is still pending a decision from a named reviewer."
    return (
        f"Approval '{approval.title}' was decided as '{approval.status.value}'. A change "
        f"set a human declined is not merged."
    )


# ---------------------------------------------------------------------------
# Step 6 — apply
# ---------------------------------------------------------------------------
def _apply(
    db: Session,
    *,
    project: Project,
    changeset: ChangeSet,
    plans: Sequence[_Plan],
    revision: int,
) -> list[_Applied]:
    """Write every planned operation. All of it, or the caller rolls it back."""
    applied: list[_Applied] = []
    temp_ids: dict[str, uuid.UUID] = {}
    now = utcnow()

    for plan in plans:
        fields = _resolve_temp_references(plan, temp_ids)

        if plan.creates:
            record = plan.model(
                project_id=project.id,
                object_revision=1,
                created_in_revision=revision,
                updated_in_revision=revision,
                origin=_origin_for(changeset, plan.basis),
                source_changeset_id=changeset.id,
                created_by_agent=changeset.author_agent,
                created_by_id=changeset.author_user_id,
                **fields,
            )
            db.add(record)
            db.flush()
            if plan.operation.temp_key:
                temp_ids[plan.operation.temp_key] = record.id
            before: dict[str, Any] = {}
            after = registry.jsonable(fields)
            target: Any = record
        elif plan.deletes:
            target = plan.target
            before = {"deleted_at": None}
            after = {"deleted_at": registry.jsonable(now)}
            target.deleted_at = now
            target.object_revision += 1
            target.updated_in_revision = revision
        else:
            target = plan.target
            before = registry.jsonable(_current_of(target, fields))
            for name, value in fields.items():
                setattr(target, name, value)
            after = registry.jsonable(fields)
            target.object_revision += 1
            target.updated_in_revision = revision

        _write_evidence_links(db, project=project, plan=plan, object_id=target.id)

        applied.append(
            _Applied(
                operation=plan.operation,
                object_type=plan.object_type,
                object_id=target.id,
                label=registry.label_for(target),
                before=before,
                after=after,
                creates=plan.creates,
                deletes=plan.deletes,
            )
        )

    db.flush()
    return applied


def _resolve_temp_references(plan: _Plan, temp_ids: dict[str, uuid.UUID]) -> dict[str, Any]:
    """Swap ``temp:`` references for the ids the earlier operations were given."""
    resolved: dict[str, Any] = {}
    for name, value in plan.fields.items():
        if not is_temp_reference(value):
            resolved[name] = value
            continue
        key = temp_key_of(value)
        # Verified during examination; a miss here would be a programming error.
        resolved[name] = temp_ids[key]
    return resolved


def _origin_for(changeset: ChangeSet, basis: Sequence[dict[str, Any]]) -> LogicOrigin:
    """How the object came to exist. Recorded once and never inferred later."""
    if any(entry.get("kind") == EvidenceKind.TEMPLATE_RULE.value for entry in basis):
        return LogicOrigin.TEMPLATE_EXPANSION
    if not changeset.author_agent and changeset.author_user_id is not None:
        return LogicOrigin.HUMAN_AUTHORED
    return LogicOrigin.AGENT_PROPOSED


def _matches(column: Any, value: Any) -> Any:
    """``column = value``, or ``column IS NULL`` — ``= NULL`` matches nothing."""
    return column.is_(None) if value is None else column == value


def _write_evidence_links(
    db: Session, *, project: Project, plan: _Plan, object_id: uuid.UUID
) -> None:
    """Record the stated basis against the object it justifies."""
    for entry in plan.basis:
        resolved, missing = _resolve_basis(db, project, entry)
        if missing or not resolved:  # pragma: no cover - examination already refused these
            continue
        existing = (
            db.execute(
                select(ObjectEvidenceLink).where(
                    ObjectEvidenceLink.object_type == plan.object_type,
                    ObjectEvidenceLink.object_id == object_id,
                    _matches(ObjectEvidenceLink.anchor_id, resolved.get("anchor_id")),
                    _matches(ObjectEvidenceLink.requirement_id, resolved.get("requirement_id")),
                    _matches(ObjectEvidenceLink.decision_id, resolved.get("decision_id")),
                    ObjectEvidenceLink.template_id == resolved.get("template_id", ""),
                )
            )
            .scalars()
            .first()
        )
        if existing is not None:
            continue
        db.add(
            ObjectEvidenceLink(
                project_id=project.id,
                object_type=plan.object_type,
                object_id=object_id,
                anchor_id=resolved.get("anchor_id"),
                requirement_id=resolved.get("requirement_id"),
                decision_id=resolved.get("decision_id"),
                template_id=resolved.get("template_id", ""),
                basis_kind=EvidenceKind(entry["kind"]),
                note=str(entry.get("note") or ""),
            )
        )


def _write_back(applied: Sequence[_Applied], *, dry_run: bool) -> None:
    """Record the diff on each operation.

    A dry run records ``before`` and ``after`` too, and nothing else: knowing
    what *would* change is the entire point of rehearsing, but the operation was
    not applied and must not claim to have been.
    """
    now = utcnow()
    for item in applied:
        operation = item.operation
        basis = operation.after.get(BASIS_KEY)
        after = dict(item.after)
        if basis is not None:
            after[BASIS_KEY] = basis
        operation.before = item.before
        operation.after = after
        operation.failure_reason = ""
        if not dry_run:
            operation.object_id = item.object_id
            operation.applied = True
            operation.applied_at = now


# ---------------------------------------------------------------------------
# Step 7 — record
# ---------------------------------------------------------------------------
def _record(
    db: Session,
    *,
    project: Project,
    changeset: ChangeSet,
    outcome: MergeOutcome,
    head: int,
    dry_run: bool,
    explanation: str,
    operations_total: int,
    operations_applied: int = 0,
    resulting_revision_number: int | None = None,
    conflicts: Sequence[_Conflict] = (),
    issues: Sequence[_Issue] = (),
    actor_user_id: uuid.UUID | None = None,
) -> MergeResult:
    """Persist the outcome, and settle the change set unless this was a rehearsal."""
    result = MergeResult(
        project_id=project.id,
        change_set_id=changeset.id,
        outcome=outcome,
        is_dry_run=dry_run,
        base_revision_number=changeset.base_revision_number,
        head_revision_number=head,
        resulting_revision_number=resulting_revision_number,
        operations_applied=operations_applied,
        operations_skipped=operations_total - operations_applied,
        conflict_count=len(conflicts),
        validation_error_count=len(issues),
        explanation=explanation,
        performed_by_id=actor_user_id,
    )
    db.add(result)
    db.flush()

    for conflict in conflicts:
        db.add(
            MergeConflict(
                project_id=project.id,
                merge_result_id=result.id,
                operation_id=conflict.operation_id,
                kind=conflict.kind,
                object_type=conflict.object_type,
                object_id=conflict.object_id,
                object_label=conflict.object_label[:200],
                field_name=conflict.field_name[:80],
                expected_revision=conflict.expected_revision,
                actual_revision=conflict.actual_revision,
                proposed_value=conflict.proposed_value,
                current_value=conflict.current_value,
                message=conflict.message,
                suggested_resolution=conflict.suggested_resolution,
            )
        )
    db.flush()

    if not dry_run:
        _settle(db, changeset, outcome, explanation, issues, actor_user_id)
    return result


def _settle(
    db: Session,
    changeset: ChangeSet,
    outcome: MergeOutcome,
    explanation: str,
    issues: Sequence[_Issue],
    actor_user_id: uuid.UUID | None,
) -> None:
    """Move the change set to the state the outcome implies, and say so."""
    if outcome is MergeOutcome.REFUSED:
        return  # Refused *because* of its state; changing it would hide that.

    if outcome is MergeOutcome.NO_OP:
        changeset.status = ChangeSetStatus.MERGED
        changeset.merged_at = utcnow()
        changeset.disposition_reason = explanation
        db.flush()
        return

    status = _STATUS_FOR_OUTCOME.get(outcome)
    if status is None:
        # MERGED is settled by the caller, which owns the revision number, and
        # DRY_RUN_OK never reaches here. Anything else would be a new outcome
        # nobody has decided the disposition for, and guessing one would leave a
        # change set in a state that does not describe what happened.
        return

    changeset.status = status
    changeset.disposition_reason = explanation
    db.flush()

    if outcome in {MergeOutcome.CONFLICT, MergeOutcome.VALIDATION_FAILED}:
        record_event(
            db,
            project_id=changeset.project_id,
            event_type=EventType.CHANGESET_VALIDATION_FAILED,
            actor_user_id=actor_user_id,
            task_id=changeset.task_id,
            payload={
                "changeset_id": str(changeset.id),
                "title": changeset.title,
                "outcome": outcome.value,
                "status": changeset.status.value,
                "validation_errors": [issue.render() for issue in issues],
            },
        )


# ---------------------------------------------------------------------------
# Explanations, written to be read by a person
# ---------------------------------------------------------------------------
def _explain_refusal(
    conflicts: Sequence[_Conflict], issues: Sequence[_Issue], operations_total: int
) -> str:
    lines = [
        f"Nothing was applied. None of the {operations_total} operation(s) in this change "
        f"set landed, because a merge is all or nothing."
    ]
    if conflicts:
        lines.append(f"{len(conflicts)} conflict(s):")
        lines.extend(f"  - {conflict.kind.value}: {conflict.message}" for conflict in conflicts)
    if issues:
        lines.append(f"{len(issues)} validation error(s):")
        lines.extend(f"  - {issue.render()}" for issue in issues)
    return "\n".join(lines)


def _describe_work(applied: Sequence[_Applied], counts: dict[str, Any]) -> str:
    if not applied:
        return "No objects were affected."
    parts: list[str] = []
    for object_type, actions in sorted(counts.items()):
        for action, number in sorted(actions.items()):
            parts.append(f"{number} {object_type} {action}")
    return f"{'; '.join(parts)}."


def _object_counts(applied: Iterable[_Applied]) -> dict[str, Any]:
    counts: dict[str, dict[str, int]] = {}
    for item in applied:
        action = "created" if item.creates else "deleted" if item.deletes else "updated"
        bucket = counts.setdefault(item.object_type.value, {})
        bucket[action] = bucket.get(action, 0) + 1
    return dict(counts)


def _current_of(target: Any, fields: dict[str, Any]) -> dict[str, Any]:
    return {name: getattr(target, name, None) for name in fields}


def _field_list(fields: dict[str, Any]) -> str:
    return ", ".join(sorted(fields)) if fields else "the object"


def _value_comparison(target: Any, fields: dict[str, Any]) -> str:
    """Both competing values, named. Without these a reviewer cannot decide."""
    if not fields:
        return "The change set proposes no field values to compare."
    rendered = [
        (
            f"Field {name!r}: the change set proposes "
            f"{registry.jsonable(value)!r}; the object currently holds "
            f"{registry.jsonable(getattr(target, name, None))!r}."
        )
        for name, value in sorted(fields.items())
    ]
    return " ".join(rendered)


def _proposed_label(object_type: ObjectType, fields: dict[str, Any]) -> str:
    """Name an object that does not exist yet, from what is being proposed."""
    for name in ("name", "reference", "code", "key", "address", "tag_prefix", "title"):
        value = fields.get(name)
        if isinstance(value, str) and value:
            return f"{registry.type_label(object_type)} {value}"
    return f"a new {registry.type_label(object_type).lower()}"
