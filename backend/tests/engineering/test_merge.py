"""The merge: what lands, what does not, and what a reviewer is told either way.

Every test here is a product rule rather than a code path. The three that matter
most are the ones about *not* changing anything — a conflicted merge, a partially
conflicted merge and a dry run all have to leave the database exactly as they
found it, and each of them asserts that afterwards rather than trusting the
outcome enum.
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agents.base.actions import (
    ChangeOperationProposal,
    ObjectBasis,
    ProposeChangesetPayload,
)
from app.core.clock import utcnow
from app.core.enums import (
    ApprovalKind,
    ApprovalStatus,
    AuditAction,
    ChangeOperationType,
    ChangeSetStatus,
    ConflictKind,
    EventType,
    EvidenceKind,
    LogicOrigin,
    MergeOutcome,
    ObjectApprovalState,
    ObjectType,
    ProjectStatus,
)
from app.engineering import changesets, merge, registry, revisions
from app.models.approval import Approval
from app.models.audit import AuditEntry
from app.models.core import ObjectEvidenceLink
from app.models.event import Event
from app.models.plant import Device
from app.models.project import Project
from app.models.revision import ChangeSet, MergeResult, ProjectRevision
from app.models.tags import Tag
from app.models.user import User

TEMPLATE_ID = "esco.motor.standard@1.0.0"


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------
def _basis() -> ObjectBasis:
    return ObjectBasis(
        kind=EvidenceKind.TEMPLATE_RULE,
        template_id=TEMPLATE_ID,
        note="Standard motor start/stop pattern.",
    )


def _create_tag(
    name: str = "MTR_101_Cmd_Run", *, temp_key: str = "", **values: object
) -> ChangeOperationProposal:
    body: dict[str, object] = {"name": name}
    body.update(values)
    return ChangeOperationProposal(
        operation_type=ChangeOperationType.CREATE_OBJECT,
        object_type=ObjectType.TAG,
        temp_key=temp_key or name.lower(),
        values=body,
        basis=[_basis()],
        description=f"Create {name}.",
    )


def _update_description(
    tag: Tag, *, based_on: int | None = None, text: str = "Run command from the HMI."
) -> ChangeOperationProposal:
    return ChangeOperationProposal(
        operation_type=ChangeOperationType.UPDATE_FIELD,
        object_type=ObjectType.TAG,
        object_id=tag.id,
        based_on_object_revision=tag.object_revision if based_on is None else based_on,
        field_name="description",
        values={"description": text},
    )


def _payload(*operations: ChangeOperationProposal, **overrides: object) -> ProposeChangesetPayload:
    body: dict[str, object] = {
        "title": "Motor 101 tags",
        "summary": "Create and describe the motor 101 command tags.",
        "base_revision_number": 0,
        "operations": list(operations),
    }
    body.update(overrides)
    return ProposeChangesetPayload(**body)  # type: ignore[arg-type]


def _submitted(
    db: Session,
    project: Project,
    *operations: ChangeOperationProposal,
    allowed_object_types: list[ObjectType] | None = None,
    **overrides: object,
) -> ChangeSet:
    changeset = changesets.build_changeset(
        db,
        project=project,
        payload=_payload(*operations, **overrides),
        author_agent="tag_architecture",
        allowed_object_types=allowed_object_types or [],
    )
    changesets.submit(db, changeset)
    db.flush()
    return changeset


def _existing_tag(
    db: Session, project: Project, name: str = "MTR_100_Sts_Run", **kwargs: object
) -> Tag:
    tag = Tag(project_id=project.id, name=name, description="As parsed.", **kwargs)
    db.add(tag)
    db.flush()
    return tag


def _tag_named(db: Session, project: Project, name: str) -> Tag | None:
    return db.execute(
        select(Tag).where(Tag.project_id == project.id, Tag.name == name)
    ).scalar_one_or_none()


def _conflicts(db: Session, result: MergeResult) -> list[ConflictKind]:
    db.flush()
    return [conflict.kind for conflict in result.conflicts]


# ---------------------------------------------------------------------------
# A clean merge
# ---------------------------------------------------------------------------
def test_a_clean_merge_creates_one_revision_and_bumps_what_it_touched(
    db: Session, project: Project
) -> None:
    """The whole mechanism in one assertion set.

    One revision, one head bump, and the updated row moves from
    ``object_revision`` 1 to 2 — which is the token every future change set is
    checked against.
    """
    revisions.ensure_initial_revision(db, project)
    existing = _existing_tag(db, project)
    before = revisions.revision_count(db, project.id)

    changeset = _submitted(db, project, _create_tag(), _update_description(existing))
    result = merge.merge_changeset(db, changeset=changeset)
    db.flush()

    assert result.outcome is MergeOutcome.MERGED
    assert revisions.revision_count(db, project.id) == before + 1
    assert project.current_revision_number == 1
    assert result.resulting_revision_number == 1

    db.refresh(existing)
    assert existing.object_revision == 2
    assert existing.updated_in_revision == 1
    assert existing.description == "Run command from the HMI."

    created = _tag_named(db, project, "MTR_101_Cmd_Run")
    assert created is not None
    assert created.object_revision == 1
    assert created.created_in_revision == 1
    assert created.source_changeset_id == changeset.id
    assert created.origin is LogicOrigin.TEMPLATE_EXPANSION

    assert changeset.status is ChangeSetStatus.MERGED
    assert changeset.merged_revision_number == 1
    assert "Merged 2 operation(s) into revision 1" in result.explanation


def test_a_merge_records_the_diff_the_basis_and_the_trail(db: Session, project: Project) -> None:
    """Everything a reviewer needs afterwards, written in the same transaction."""
    existing = _existing_tag(db, project)
    changeset = _submitted(db, project, _create_tag(), _update_description(existing))
    merge.merge_changeset(db, changeset=changeset)
    db.flush()

    create_op, update_op = sorted(changeset.operations, key=lambda op: op.ordinal)
    assert create_op.applied and create_op.object_id is not None
    assert update_op.before == {"description": "As parsed."}
    assert update_op.after["description"] == "Run command from the HMI."

    link = db.execute(
        select(ObjectEvidenceLink).where(ObjectEvidenceLink.object_id == create_op.object_id)
    ).scalar_one()
    assert link.template_id == TEMPLATE_ID
    assert link.basis_kind is EvidenceKind.TEMPLATE_RULE
    assert link.satisfies_traceability

    assert _audit(db, project, AuditAction.CHANGESET_MERGED)
    assert _events(db, project, EventType.CHANGESET_MERGED)
    assert _events(db, project, EventType.REVISION_CREATED)


# ---------------------------------------------------------------------------
# Conflicts
# ---------------------------------------------------------------------------
def test_a_stale_change_set_conflicts_and_changes_nothing(db: Session, project: Project) -> None:
    """Somebody edited the tag while the agent was thinking.

    The conflict has to carry both revision numbers *and* both values, or the
    reviewer is being asked to choose between two things they cannot see.
    """
    existing = _existing_tag(db, project)
    changeset = _submitted(db, project, _update_description(existing, based_on=1))

    existing.description = "Edited by an engineer."
    existing.object_revision = 2
    db.flush()

    result = merge.merge_changeset(db, changeset=changeset)
    db.flush()

    assert result.outcome is MergeOutcome.CONFLICT
    assert _conflicts(db, result) == [ConflictKind.STALE_REVISION]
    conflict = result.conflicts[0]
    assert conflict.expected_revision == 1
    assert conflict.actual_revision == 2
    assert conflict.object_label == "Tag MTR_100_Sts_Run"
    assert conflict.field_name == "description"
    assert conflict.proposed_value == {"description": "Run command from the HMI."}
    assert conflict.current_value == {"description": "Edited by an engineer."}
    assert "Run command from the HMI." in conflict.message
    assert "Edited by an engineer." in conflict.message

    db.refresh(existing)
    assert existing.description == "Edited by an engineer."
    assert existing.object_revision == 2
    assert project.current_revision_number == 0
    assert changeset.status is ChangeSetStatus.CONFLICTED
    assert revisions.revision_count(db, project.id) == 0


def test_an_approved_object_is_never_overwritten(db: Session, project: Project) -> None:
    """Superseding something an engineer signed off is an explicit, separate act."""
    approved = _existing_tag(db, project, approval_state=ObjectApprovalState.APPROVED)
    changeset = _submitted(db, project, _update_description(approved))

    result = merge.merge_changeset(db, changeset=changeset)
    db.flush()

    assert result.outcome is MergeOutcome.CONFLICT
    assert _conflicts(db, result) == [ConflictKind.PROTECTED_OBJECT]
    conflict = result.conflicts[0]
    assert conflict.object_label == "Tag MTR_100_Sts_Run"
    assert "approved" in conflict.message
    assert "Supersede" in conflict.suggested_resolution

    db.refresh(approved)
    assert approved.description == "As parsed."
    assert approved.object_revision == 1


def test_an_operation_outside_the_agents_scope_is_refused(db: Session, project: Project) -> None:
    """The Tag Architecture Agent proposing a rung is a bug, not a request.

    Scope is enforced rather than advisory, so the failure is visible instead of
    becoming logic nobody expected in a routine nobody reviewed.
    """
    changeset = _submitted(db, project, _create_tag(), allowed_object_types=[ObjectType.LOGIC_RUNG])
    result = merge.merge_changeset(db, changeset=changeset)
    db.flush()

    assert result.outcome is MergeOutcome.CONFLICT
    assert _conflicts(db, result) == [ConflictKind.SCOPE_VIOLATION]
    assert "outside this change set's scope" in result.conflicts[0].message
    assert _tag_named(db, project, "MTR_101_Cmd_Run") is None


def test_an_update_to_something_that_is_not_there_conflicts(db: Session, project: Project) -> None:
    missing = Tag(project_id=project.id, id=uuid.uuid4(), name="GHOST")
    changeset = _submitted(db, project, _update_description(missing, based_on=1))
    result = merge.merge_changeset(db, changeset=changeset)
    db.flush()

    assert _conflicts(db, result) == [ConflictKind.OBJECT_MISSING]
    assert str(missing.id) in result.conflicts[0].message


def test_creating_something_that_already_exists_conflicts(db: Session, project: Project) -> None:
    """Stricter than the constraint: NULL never equals NULL.

    Two controller-scoped tags of the same name slip past the unique index
    because ``scope_program_id`` is NULL in both, and only surface as a duplicate
    in the export. The merge catches it here instead.
    """
    _existing_tag(db, project, name="MTR_101_Cmd_Run")
    changeset = _submitted(db, project, _create_tag("MTR_101_Cmd_Run"))
    result = merge.merge_changeset(db, changeset=changeset)
    db.flush()

    assert _conflicts(db, result) == [ConflictKind.DUPLICATE_KEY]
    assert "already exists" in result.conflicts[0].message
    assert (
        db.execute(
            select(func.count(Tag.id)).where(
                Tag.project_id == project.id, Tag.name == "MTR_101_Cmd_Run"
            )
        ).scalar_one()
        == 1
    )


def test_a_reference_to_something_absent_conflicts(db: Session, project: Project) -> None:
    changeset = _submitted(db, project, _create_tag(device_id=str(uuid.uuid4())))
    result = merge.merge_changeset(db, changeset=changeset)
    db.flush()

    assert _conflicts(db, result) == [ConflictKind.DEPENDENCY_MISSING]
    assert "device_id" in result.conflicts[0].message
    assert result.conflicts[0].field_name == "device_id"


def test_a_basis_citing_a_requirement_that_does_not_exist_conflicts(
    db: Session, project: Project
) -> None:
    """A citation nobody can follow is worse than no citation at all."""
    operation = ChangeOperationProposal(
        operation_type=ChangeOperationType.CREATE_OBJECT,
        object_type=ObjectType.TAG,
        temp_key="tag-1",
        values={"name": "MTR_101_Cmd_Run"},
        basis=[ObjectBasis(kind=EvidenceKind.INFERENCE, requirement_reference="REQ-014")],
    )
    changeset = _submitted(db, project, operation)
    result = merge.merge_changeset(db, changeset=changeset)
    db.flush()

    assert _conflicts(db, result) == [ConflictKind.DEPENDENCY_MISSING]
    assert "REQ-014" in result.conflicts[0].message


# ---------------------------------------------------------------------------
# All or nothing
# ---------------------------------------------------------------------------
def test_a_single_conflicting_operation_stops_the_whole_batch(
    db: Session, project: Project
) -> None:
    """Operation 3 of 4 conflicts, so operations 1 and 2 do not land.

    A half-applied change set is the worst outcome available: the model would
    hold tags whose logic never arrived, and no revision would describe it.
    """
    missing = uuid.uuid4()
    changeset = _submitted(
        db,
        project,
        _create_tag("MTR_101_Cmd_Run"),
        _create_tag("MTR_101_Cmd_Stop"),
        ChangeOperationProposal(
            operation_type=ChangeOperationType.UPDATE_FIELD,
            object_type=ObjectType.TAG,
            object_id=missing,
            based_on_object_revision=1,
            field_name="description",
            values={"description": "Never applied."},
        ),
        _create_tag("MTR_101_Sts_Running"),
    )
    result = merge.merge_changeset(db, changeset=changeset)
    db.flush()

    assert result.outcome is MergeOutcome.CONFLICT
    assert result.operations_applied == 0
    assert result.operations_skipped == 4
    for name in ("MTR_101_Cmd_Run", "MTR_101_Cmd_Stop", "MTR_101_Sts_Running"):
        assert _tag_named(db, project, name) is None, f"{name} should not have been created"
    assert project.current_revision_number == 0
    assert revisions.revision_count(db, project.id) == 0
    assert not any(operation.applied for operation in changeset.operations)


# ---------------------------------------------------------------------------
# Forward references
# ---------------------------------------------------------------------------
def test_an_operation_can_reference_an_object_an_earlier_one_creates(
    db: Session, project: Project
) -> None:
    """A device and the tag bound to it arrive in one change set.

    The tag cannot name a real device id, because none exists until the merge
    assigns one, so it names the temp key and the merge substitutes the id.
    """
    device = ChangeOperationProposal(
        operation_type=ChangeOperationType.CREATE_OBJECT,
        object_type=ObjectType.DEVICE,
        temp_key="dev-1",
        values={"name": "Conveyor motor 101", "tag_prefix": "MTR_101"},
        basis=[_basis()],
    )
    tag = _create_tag("MTR_101_Cmd_Run", device_id=changesets.temp_ref("dev-1"))
    changeset = _submitted(db, project, device, tag)

    result = merge.merge_changeset(db, changeset=changeset)
    db.flush()

    assert result.outcome is MergeOutcome.MERGED
    created_device = db.execute(
        select(Device).where(Device.project_id == project.id, Device.tag_prefix == "MTR_101")
    ).scalar_one()
    created_tag = _tag_named(db, project, "MTR_101_Cmd_Run")
    assert created_tag is not None
    assert created_tag.device_id == created_device.id


def test_a_forward_reference_to_nothing_conflicts(db: Session, project: Project) -> None:
    """Ordering matters, and getting it wrong is reported rather than guessed at."""
    changeset = _submitted(
        db, project, _create_tag("MTR_101_Cmd_Run", device_id=changesets.temp_ref("dev-1"))
    )
    result = merge.merge_changeset(db, changeset=changeset)
    db.flush()

    assert _conflicts(db, result) == [ConflictKind.DEPENDENCY_MISSING]
    assert "temp_key 'dev-1'" in result.conflicts[0].message


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
def test_an_operation_naming_a_protected_field_is_rejected_by_name(
    db: Session, project: Project
) -> None:
    """A change set that could set ``approval_state`` could approve its own work."""
    changeset = _submitted(db, project, _create_tag(approval_state="approved"))
    result = merge.merge_changeset(db, changeset=changeset)
    db.flush()

    assert result.outcome is MergeOutcome.VALIDATION_FAILED
    assert result.validation_error_count == 1
    assert "approval_state" in result.explanation
    assert "written by the merge" in result.explanation
    assert _tag_named(db, project, "MTR_101_Cmd_Run") is None
    assert changeset.status is ChangeSetStatus.VALIDATION_FAILED


def test_creating_a_traceable_object_with_no_basis_is_rejected(
    db: Session, project: Project
) -> None:
    """The action schema refuses this too; the merge refuses it independently.

    The basis is stripped off the stored operation here precisely because the
    payload model will not build one without it — and the merge must not depend
    on having been handed well-formed input to enforce the traceability rule.
    """
    changeset = _submitted(db, project, _create_tag())
    operation = changeset.operations[0]
    operation.after = changesets.proposed_values(operation)
    db.flush()

    result = merge.merge_changeset(db, changeset=changeset)
    db.flush()

    assert result.outcome is MergeOutcome.VALIDATION_FAILED
    assert "requires at least one basis" in result.explanation
    assert _tag_named(db, project, "MTR_101_Cmd_Run") is None


def test_a_create_missing_a_required_field_is_rejected_before_anything_is_written(
    db: Session, project: Project
) -> None:
    operation = ChangeOperationProposal(
        operation_type=ChangeOperationType.CREATE_OBJECT,
        object_type=ObjectType.DEVICE,
        temp_key="dev-1",
        values={"name": "Conveyor motor 101"},
        basis=[_basis()],
    )
    changeset = _submitted(db, project, operation)
    result = merge.merge_changeset(db, changeset=changeset)
    db.flush()

    assert result.outcome is MergeOutcome.VALIDATION_FAILED
    assert "'tag_prefix'" in result.explanation


def test_a_value_the_column_cannot_hold_is_rejected_by_name(db: Session, project: Project) -> None:
    changeset = _submitted(db, project, _create_tag(hmi_exposed="yes"))
    result = merge.merge_changeset(db, changeset=changeset)
    db.flush()

    assert result.outcome is MergeOutcome.VALIDATION_FAILED
    assert "hmi_exposed" in result.explanation


# ---------------------------------------------------------------------------
# The human gate
# ---------------------------------------------------------------------------
def test_a_change_set_needing_approval_with_none_recorded_applies_nothing(
    db: Session, project: Project
) -> None:
    """Nothing is merged on an agent's own authority."""
    changeset = _submitted(db, project, _create_tag(), requires_human_approval=True)
    result = merge.merge_changeset(db, changeset=changeset)
    db.flush()

    assert result.outcome is MergeOutcome.APPROVAL_REQUIRED
    assert "named human approval" in result.explanation
    assert _tag_named(db, project, "MTR_101_Cmd_Run") is None
    assert project.current_revision_number == 0
    assert changeset.status is ChangeSetStatus.AWAITING_APPROVAL


def test_a_pending_approval_is_not_a_decision(db: Session, project: Project) -> None:
    changeset = _submitted(db, project, _create_tag(), requires_human_approval=True)
    changeset.approval_id = _approval(db, project, ApprovalStatus.PENDING).id
    db.flush()

    result = merge.merge_changeset(db, changeset=changeset)
    assert result.outcome is MergeOutcome.APPROVAL_REQUIRED
    assert "still pending" in result.explanation


def test_a_rejected_approval_stops_the_merge_outright(db: Session, project: Project) -> None:
    """Merging something a human declined would be the worst available bug."""
    changeset = _submitted(db, project, _create_tag(), requires_human_approval=True)
    changeset.approval_id = _approval(db, project, ApprovalStatus.REJECTED).id
    db.flush()

    result = merge.merge_changeset(db, changeset=changeset)
    assert result.outcome is MergeOutcome.APPROVAL_REQUIRED
    assert "declined" in result.explanation
    assert _tag_named(db, project, "MTR_101_Cmd_Run") is None


def test_a_decided_approval_lets_the_merge_through(
    db: Session, project: Project, seeded: dict[str, User]
) -> None:
    changeset = _submitted(db, project, _create_tag(), requires_human_approval=True)
    approval = _approval(db, project, ApprovalStatus.APPROVED)
    approval.decided_by_id = seeded["reviewer"].id
    approval.decided_at = utcnow()
    changeset.approval_id = approval.id
    db.flush()

    result = merge.merge_changeset(db, changeset=changeset, actor_user_id=seeded["reviewer"].id)
    assert result.outcome is MergeOutcome.MERGED
    assert _tag_named(db, project, "MTR_101_Cmd_Run") is not None


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------
def test_a_clean_dry_run_reports_what_the_merge_would_do_and_writes_nothing(
    db: Session, project: Project
) -> None:
    """The rehearsal takes the same route and leaves no trace of having run it.

    Same conflicts, same validation, same apply — then the savepoint is rolled
    back. What survives is the ``MergeResult``, because "we checked and it would
    have worked" is information a reviewer wants.
    """
    existing = _existing_tag(db, project)
    changeset = _submitted(db, project, _create_tag(), _update_description(existing))

    rehearsal = merge.merge_changeset(db, changeset=changeset, dry_run=True)
    db.flush()

    assert rehearsal.outcome is MergeOutcome.DRY_RUN_OK
    assert rehearsal.is_dry_run
    assert rehearsal.operations_applied == 0
    assert "Nothing was written" in rehearsal.explanation

    assert _tag_named(db, project, "MTR_101_Cmd_Run") is None
    db.refresh(existing)
    assert existing.description == "As parsed."
    assert existing.object_revision == 1
    assert project.current_revision_number == 0
    assert revisions.revision_count(db, project.id) == 0
    assert changeset.status is ChangeSetStatus.SUBMITTED
    assert not _events(db, project, EventType.CHANGESET_MERGED)
    assert not _audit(db, project, AuditAction.CHANGESET_MERGED)

    # The diff is still recorded: seeing it is the point of rehearsing.
    update_op = sorted(changeset.operations, key=lambda op: op.ordinal)[1]
    assert update_op.before == {"description": "As parsed."}
    assert update_op.after["description"] == "Run command from the HMI."
    assert not update_op.applied

    # And the real merge now reaches the outcome the rehearsal predicted.
    real = merge.merge_changeset(db, changeset=changeset)
    db.flush()
    assert real.outcome is MergeOutcome.MERGED
    assert _tag_named(db, project, "MTR_101_Cmd_Run") is not None


def test_a_dry_run_reports_the_same_conflicts_as_the_real_merge(
    db: Session, project: Project
) -> None:
    existing = _existing_tag(db, project)
    changeset = _submitted(db, project, _update_description(existing, based_on=1))
    existing.object_revision = 2
    db.flush()

    rehearsal = merge.merge_changeset(db, changeset=changeset, dry_run=True)
    db.flush()
    assert rehearsal.outcome is MergeOutcome.CONFLICT
    assert _conflicts(db, rehearsal) == [ConflictKind.STALE_REVISION]
    assert changeset.status is ChangeSetStatus.SUBMITTED

    real = merge.merge_changeset(db, changeset=changeset)
    db.flush()
    assert real.outcome is rehearsal.outcome
    assert _conflicts(db, real) == _conflicts(db, rehearsal)
    assert changeset.status is ChangeSetStatus.CONFLICTED


def test_both_attempts_are_recorded(db: Session, project: Project) -> None:
    """Discarding the rehearsal would make the dry run invisible afterwards."""
    changeset = _submitted(db, project, _create_tag())
    merge.merge_changeset(db, changeset=changeset, dry_run=True)
    merge.merge_changeset(db, changeset=changeset)
    db.flush()

    results = list(
        db.execute(
            select(MergeResult)
            .where(MergeResult.change_set_id == changeset.id)
            .order_by(MergeResult.created_at)
        ).scalars()
    )
    assert [result.is_dry_run for result in results] == [True, False]


# ---------------------------------------------------------------------------
# Refusal
# ---------------------------------------------------------------------------
def test_an_archived_project_is_read_only(db: Session, project: Project) -> None:
    changeset = _submitted(db, project, _create_tag())
    project.status = ProjectStatus.ARCHIVED
    db.flush()

    result = merge.merge_changeset(db, changeset=changeset)
    assert result.outcome is MergeOutcome.REFUSED
    assert "archived" in result.explanation
    assert _tag_named(db, project, "MTR_101_Cmd_Run") is None


def test_a_draft_is_not_mergeable_even_as_a_rehearsal(db: Session, project: Project) -> None:
    """A dry run promises to report what the real merge would do.

    If a draft could be rehearsed but not merged, that promise would already be
    broken before any engineering content was looked at. Submit, then rehearse.
    """
    changeset = changesets.build_changeset(db, project=project, payload=_payload(_create_tag()))
    db.flush()

    for dry_run in (True, False):
        result = merge.merge_changeset(db, changeset=changeset, dry_run=dry_run)
        assert result.outcome is MergeOutcome.REFUSED
        assert "'draft'" in result.explanation


def test_merging_twice_refuses_the_second_time(db: Session, project: Project) -> None:
    changeset = _submitted(db, project, _create_tag())
    first = merge.merge_changeset(db, changeset=changeset)
    db.flush()
    second = merge.merge_changeset(db, changeset=changeset)
    db.flush()

    assert first.outcome is MergeOutcome.MERGED
    assert second.outcome is MergeOutcome.REFUSED
    assert "already merged into revision 1" in second.explanation
    assert project.current_revision_number == 1
    assert (
        db.execute(
            select(func.count(Tag.id)).where(
                Tag.project_id == project.id, Tag.name == "MTR_101_Cmd_Run"
            )
        ).scalar_one()
        == 1
    )


# ---------------------------------------------------------------------------
# Deletes and renames
# ---------------------------------------------------------------------------
def test_a_delete_is_a_tombstone_rather_than_a_removal(db: Session, project: Project) -> None:
    """A citation must resolve to a tombstone, never to a dangling id."""
    existing = _existing_tag(db, project)
    changeset = _submitted(
        db,
        project,
        ChangeOperationProposal(
            operation_type=ChangeOperationType.DELETE_OBJECT,
            object_type=ObjectType.TAG,
            object_id=existing.id,
            based_on_object_revision=1,
        ),
    )
    result = merge.merge_changeset(db, changeset=changeset)
    db.flush()

    assert result.outcome is MergeOutcome.MERGED
    db.refresh(existing)
    assert existing.is_deleted
    assert existing.object_revision == 2
    assert changeset.operations[0].before == {"deleted_at": None}


def test_a_rename_whose_starting_name_is_wrong_conflicts(db: Session, project: Project) -> None:
    """Renaming from a name the tag no longer has would rename the wrong thing."""
    existing = _existing_tag(db, project, name="MTR_100_Sts_Run")
    changeset = _submitted(
        db,
        project,
        ChangeOperationProposal(
            operation_type=ChangeOperationType.RENAME_TAG,
            object_type=ObjectType.TAG,
            object_id=existing.id,
            based_on_object_revision=1,
            values={"from_name": "MTR_099_Sts_Run", "to_name": "MTR_101_Sts_Run"},
        ),
        # A rename can never be self-approved, so the payload carries the gate. The
        # conflict is still what is reported: there is no point asking a human to
        # approve a rename that could not be applied.
        requires_human_approval=True,
    )
    result = merge.merge_changeset(db, changeset=changeset)
    db.flush()

    assert _conflicts(db, result) == [ConflictKind.CONCURRENT_FIELD_EDIT]
    conflict = result.conflicts[0]
    assert conflict.proposed_value["from_name"] == "MTR_099_Sts_Run"
    assert conflict.current_value == {"name": "MTR_100_Sts_Run"}
    db.refresh(existing)
    assert existing.name == "MTR_100_Sts_Run"


# ---------------------------------------------------------------------------
# Revision bookkeeping
# ---------------------------------------------------------------------------
def test_revisions_form_a_chain(db: Session, project: Project) -> None:
    """Each merge point names the one before it, so the timeline is walkable."""
    for name in ("MTR_101_Cmd_Run", "MTR_101_Cmd_Stop"):
        changeset = _submitted(db, project, _create_tag(name))
        merge.merge_changeset(db, changeset=changeset)
    db.flush()

    chain = list(
        db.execute(
            select(ProjectRevision)
            .where(ProjectRevision.project_id == project.id)
            .order_by(ProjectRevision.number)
        ).scalars()
    )
    assert [revision.number for revision in chain] == [0, 1, 2]
    assert chain[0].parent_revision_id is None
    assert chain[1].parent_revision_id == chain[0].id
    assert chain[2].parent_revision_id == chain[1].id
    assert chain[2].object_counts == {"tag": {"created": 1}}
    assert revisions.current_revision_number(db, project.id) == 2
    assert revisions.latest_revision(db, project.id) is chain[2]


def test_a_release_baseline_is_marked_rather_than_inferred(db: Session, project: Project) -> None:
    revision = revisions.ensure_initial_revision(db, project)
    assert not revision.is_release_baseline
    revisions.mark_release_baseline(db, revision)
    assert revisions.get_revision(db, project.id, 0) is revision
    assert revision.is_release_baseline


def test_the_registry_is_what_the_merge_writes_through(db: Session, project: Project) -> None:
    """A guard on the seam: the merge must not know the ORM class by any other route."""
    assert registry.model_for(ObjectType.TAG) is Tag
    assert registry.object_type_for(Tag(name="X")) is ObjectType.TAG


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _approval(db: Session, project: Project, status: ApprovalStatus) -> Approval:
    approval = Approval(
        project_id=project.id,
        kind=ApprovalKind.CHANGESET_MERGE,
        status=status,
        title="Merge the motor 101 command tags",
        question="Do these tags match the naming standard?",
    )
    db.add(approval)
    db.flush()
    return approval


def _events(db: Session, project: Project, event_type: EventType) -> list[Event]:
    db.flush()
    return list(
        db.execute(
            select(Event)
            .where(Event.project_id == project.id, Event.event_type == event_type)
            .order_by(Event.seq)
        ).scalars()
    )


def _audit(db: Session, project: Project, action: AuditAction) -> list[AuditEntry]:
    db.flush()
    return list(
        db.execute(
            select(AuditEntry).where(
                AuditEntry.project_id == project.id, AuditEntry.action == action
            )
        ).scalars()
    )


def test_a_forward_reference_inside_a_natural_key_does_not_derail_the_check(
    db: Session, project: Project
) -> None:
    """A program-scoped tag whose program is created by the same change set.

    ``scope_program_id`` is part of the tag's natural key, so the duplicate check
    would otherwise query the database with the ``temp:`` placeholder and fail on
    the way there — turning a legal change set into an exception rather than a
    merge.
    """
    program = ChangeOperationProposal(
        operation_type=ChangeOperationType.CREATE_OBJECT,
        object_type=ObjectType.PROGRAM,
        temp_key="prog-1",
        values={"name": "ConveyorProgram", "ordinal": 0},
        basis=[_basis()],
    )
    scoped_tag = _create_tag(
        "Local_Step",
        scope="program",
        scope_program_id=changesets.temp_ref("prog-1"),
    )
    changeset = _submitted(db, project, program, scoped_tag)

    result = merge.merge_changeset(db, changeset=changeset)
    db.flush()

    assert result.outcome is MergeOutcome.MERGED
    created = _tag_named(db, project, "Local_Step")
    assert created is not None
    assert created.scope_program_id is not None
