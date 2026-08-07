"""Revisions, change sets, merge results and approval gates.

This is the spine of the production platform: **agents never write to the
canonical model directly.** An agent proposes a :class:`ChangeSet` against a
known base revision, deterministic validators inspect it, conflicts are detected
against anything that moved underneath it, a human gate is applied where the
change warrants one, and only then is it merged in a single transaction that
produces a new :class:`ProjectRevision`.

The representation is *current state plus a change log* rather than a snapshot
per revision or bitemporal object rows — see
``docs/backend/adr/0001-extend-the-review-platform-into-a-production-platform.md``
for why. Three consequences follow, and they are what the tables below encode:

* **Conflict detection is a comparison, not a guess.** Every canonical object
  carries ``object_revision``; an operation states the value it was written
  against, and a mismatch is a conflict with a name.
* **A diff is a query.** The operations merged between two revisions *are* the
  difference between them.
* **Rollback is data, not reconstruction.** Every operation stores the value
  before it as well as after, so undoing one does not require replaying history.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import (
    ApprovalGateKey,
    ChangeOperationType,
    ChangeSetStatus,
    ConflictKind,
    GateStatus,
    MergeOutcome,
    ObjectType,
    ValidationSeverity,
)
from app.database.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.database.types import GUID, JSONDict, JSONList, UTCDateTime


class ProjectRevision(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A numbered, immutable point on a project's timeline.

    Revision 0 is created with the project and represents "nothing has been
    merged yet", so an agent always has a real base to check out against rather
    than a null that every caller has to special-case.

    A revision is a *label*, not a copy. Rebuilding the state as of an older
    revision means reverse-applying the operations merged since — accepted as
    the cost of not duplicating the whole model on every merge, and recorded as
    a known limitation rather than hidden.
    """

    __tablename__ = "project_revisions"
    __table_args__ = (
        UniqueConstraint("project_id", "number", name="uq_project_revisions_project_id_number"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    parent_revision_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("project_revisions.id", ondelete="SET NULL"), nullable=True
    )

    #: The change set whose merge produced this revision. Null on revision 0.
    changeset_id: Mapped[uuid.UUID | None] = mapped_column(GUID, nullable=True, index=True)

    summary: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_by_agent: Mapped[str] = mapped_column(String(40), default="", nullable=False)

    #: Counts by object type, so "what changed here" is answerable without
    #: reading every operation.
    object_counts: Mapped[dict[str, Any]] = mapped_column(JSONDict, default=dict, nullable=False)
    operation_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    #: Set when a release candidate was generated from this revision. A baseline
    #: revision must never be the base for an in-place edit — a package with a
    #: checksum has to keep meaning what it meant.
    is_release_baseline: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class ChangeSet(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A proposed, scoped, reviewable batch of changes to the canonical model.

    ``allowed_object_types`` is the scope an agent was given, and it is enforced
    rather than advisory: the Tag Architecture Agent proposing a change to a
    logic rung is a bug, and the merge refuses it with
    :attr:`ConflictKind.SCOPE_VIOLATION` instead of quietly applying it.
    """

    __tablename__ = "change_sets"
    __table_args__ = (Index("ix_change_sets_project_status", "project_id", "status"),)

    project_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: The revision this change set was written against.
    base_revision_number: Mapped[int] = mapped_column(Integer, nullable=False)
    #: Set once merged.
    merged_revision_number: Mapped[int | None] = mapped_column(Integer, nullable=True)

    title: Mapped[str] = mapped_column(String(300), nullable=False)
    summary: Mapped[str] = mapped_column(Text, default="", nullable=False)
    #: Short, user-facing. Never a transcript of model reasoning.
    rationale: Mapped[str] = mapped_column(Text, default="", nullable=False)

    status: Mapped[ChangeSetStatus] = mapped_column(
        default=ChangeSetStatus.DRAFT, nullable=False, index=True
    )

    author_agent: Mapped[str] = mapped_column(String(40), default="", nullable=False)
    author_user_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    task_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True, index=True
    )
    agent_run_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True
    )

    #: The object types this agent was permitted to touch.
    allowed_object_types: Mapped[list[Any]] = mapped_column(JSONList, default=list, nullable=False)

    #: Deduplicates a retried proposal. The queue is at-least-once, so an agent
    #: whose task is re-run must converge rather than propose the batch twice.
    idempotency_key: Mapped[str] = mapped_column(String(120), default="", nullable=False)

    operation_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    #: Populated by the agent; a change set that states no uncertainty is more
    #: suspicious than one that does.
    assumptions: Mapped[list[Any]] = mapped_column(JSONList, default=list, nullable=False)
    warnings: Mapped[list[Any]] = mapped_column(JSONList, default=list, nullable=False)

    requires_human_approval: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    approval_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("approvals.id", ondelete="SET NULL"), nullable=True
    )
    #: Non-empty when the change touches a restricted domain, naming which one.
    restricted_domains: Mapped[list[Any]] = mapped_column(JSONList, default=list, nullable=False)

    submitted_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    merged_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    #: Why it was rejected or abandoned, in a sentence an engineer can act on.
    disposition_reason: Mapped[str] = mapped_column(Text, default="", nullable=False)

    operations: Mapped[list[ChangeOperation]] = relationship(
        back_populates="change_set",
        cascade="all, delete-orphan",
        lazy="select",
        order_by="ChangeOperation.ordinal",
    )

    @property
    def is_open(self) -> bool:
        """Whether the change set can still change state."""
        return self.status not in {
            ChangeSetStatus.MERGED,
            ChangeSetStatus.REJECTED,
            ChangeSetStatus.SUPERSEDED,
            ChangeSetStatus.ABANDONED,
        }

    @property
    def is_merged(self) -> bool:
        return self.status is ChangeSetStatus.MERGED


class ChangeOperation(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One structured edit within a change set.

    ``before`` and ``after`` are the whole point. They make the operation a
    complete description of the edit — enough to render a diff, enough to undo,
    and enough to explain to a reviewer what will happen *before* it happens,
    which is what makes a merge dry-run meaningful rather than reassuring.

    The proposal is kept apart from the outcome. ``values`` and ``basis`` are
    what the agent asked for and why; ``before`` and ``after`` are what the merge
    found and what it wrote. They used to share ``after``, which meant a merged
    operation could no longer answer "what was proposed" — the merge had
    overwritten it with what landed. Those are the same thing exactly when
    nothing went wrong, and the audit trail exists for the times something did.
    """

    __tablename__ = "change_operations"
    __table_args__ = (
        UniqueConstraint(
            "change_set_id", "ordinal", name="uq_change_operations_change_set_id_ordinal"
        ),
        Index("ix_change_operations_target", "object_type", "object_id"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    change_set_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("change_sets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)

    operation_type: Mapped[ChangeOperationType] = mapped_column(nullable=False, index=True)
    object_type: Mapped[ObjectType] = mapped_column(nullable=False)
    #: Null for a create, which is assigned an id at merge time.
    object_id: Mapped[uuid.UUID | None] = mapped_column(GUID, nullable=True)
    #: Lets later operations in the same change set refer to something this one
    #: creates, before it has a real id.
    temp_key: Mapped[str] = mapped_column(String(80), default="", nullable=False)

    #: The ``object_revision`` this edit was written against. A mismatch at
    #: merge time is a stale-revision conflict.
    based_on_object_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)

    #: Set for UPDATE_FIELD; empty for whole-object operations.
    field_name: Mapped[str] = mapped_column(String(80), default="", nullable=False)

    #: The field values the agent proposed, exactly as proposed. Written once,
    #: at build time, and never touched again — a merge that coerces, rejects or
    #: partially applies them leaves this untouched so the proposal stays
    #: readable next to what became of it.
    values: Mapped[dict[str, Any]] = mapped_column(JSONDict, default=dict, nullable=False)
    #: Why the operation should happen: requirement, template rule, human
    #: decision or cited document, each as one entry. The merge resolves these
    #: into :class:`~app.models.core.ObjectEvidenceLink` rows; keeping the
    #: stated form here means an operation that was refused still says what it
    #: claimed, which is what a reviewer needs in order to disagree with it.
    basis: Mapped[list[Any]] = mapped_column(JSONList, default=list, nullable=False)

    #: The object's state before this operation, and after it. Both are the
    #: merge's account of what happened, written when the operation is applied
    #: or rehearsed, and empty until then.
    before: Mapped[dict[str, Any]] = mapped_column(JSONDict, default=dict, nullable=False)
    after: Mapped[dict[str, Any]] = mapped_column(JSONDict, default=dict, nullable=False)

    #: LINK_OBJECTS / UNLINK_OBJECTS: the other end of the relationship.
    related_object_type: Mapped[ObjectType | None] = mapped_column(nullable=True)
    related_object_id: Mapped[uuid.UUID | None] = mapped_column(GUID, nullable=True)
    relationship_key: Mapped[str] = mapped_column(String(60), default="", nullable=False)

    #: One line, shown in the review UI next to the diff.
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)

    applied: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    applied_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    #: Populated when this specific operation could not be applied.
    failure_reason: Mapped[str] = mapped_column(Text, default="", nullable=False)

    change_set: Mapped[ChangeSet] = relationship(back_populates="operations")


class MergeResult(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """The outcome of attempting a merge — including a dry run.

    A dry run writes a row exactly like a real merge does, with
    ``is_dry_run`` set. Recording the rehearsal is deliberate: "we checked and
    it would have conflicted" is information a reviewer wants, and discarding it
    would make the dry-run feature invisible in the audit trail.
    """

    __tablename__ = "merge_results"

    project_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    change_set_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("change_sets.id", ondelete="CASCADE"), nullable=False, index=True
    )

    outcome: Mapped[MergeOutcome] = mapped_column(nullable=False, index=True)
    is_dry_run: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    base_revision_number: Mapped[int] = mapped_column(Integer, nullable=False)
    #: The revision at merge time, which may be ahead of the base.
    head_revision_number: Mapped[int] = mapped_column(Integer, nullable=False)
    resulting_revision_number: Mapped[int | None] = mapped_column(Integer, nullable=True)

    operations_applied: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    operations_skipped: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    conflict_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    validation_error_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    #: Written for a human: what this merge did, or precisely why it did not.
    explanation: Mapped[str] = mapped_column(Text, default="", nullable=False)
    #: One entry per validation error, each naming the operation ordinal, the
    #: object and the field. The same errors are rendered into ``explanation``
    #: for a reader; they are here as well because prose cannot be grouped or
    #: counted, and "which change sets failed validation, and on what" is a
    #: question the platform has to answer over the whole project rather than
    #: one merge at a time. Conflicts are rows of their own — see
    #: :class:`MergeConflict` — because each carries two competing values a
    #: reviewer has to choose between; a validation error has no such choice.
    validation_errors: Mapped[list[Any]] = mapped_column(JSONList, default=list, nullable=False)
    performed_by_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    conflicts: Mapped[list[MergeConflict]] = relationship(
        back_populates="merge_result", cascade="all, delete-orphan", lazy="selectin"
    )

    @property
    def succeeded(self) -> bool:
        return self.outcome in {MergeOutcome.MERGED, MergeOutcome.DRY_RUN_OK, MergeOutcome.NO_OP}


class MergeConflict(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One reason a merge could not proceed.

    Every conflict names the object, the field where relevant, and both
    competing values. A conflict report that says only "the object changed"
    leaves a reviewer with nothing to decide.
    """

    __tablename__ = "merge_conflicts"

    project_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    merge_result_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("merge_results.id", ondelete="CASCADE"), nullable=False, index=True
    )
    operation_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("change_operations.id", ondelete="SET NULL"), nullable=True
    )

    kind: Mapped[ConflictKind] = mapped_column(nullable=False, index=True)
    object_type: Mapped[ObjectType] = mapped_column(nullable=False)
    object_id: Mapped[uuid.UUID | None] = mapped_column(GUID, nullable=True)
    #: Human-followable, e.g. "Tag MTR_101_Cmd_Run".
    object_label: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    field_name: Mapped[str] = mapped_column(String(80), default="", nullable=False)

    expected_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    actual_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    proposed_value: Mapped[dict[str, Any]] = mapped_column(JSONDict, default=dict, nullable=False)
    current_value: Mapped[dict[str, Any]] = mapped_column(JSONDict, default=dict, nullable=False)

    message: Mapped[str] = mapped_column(Text, nullable=False)
    #: What a human could do about it. Empty when there is genuinely no
    #: automatic option, which is itself worth saying.
    suggested_resolution: Mapped[str] = mapped_column(Text, default="", nullable=False)

    merge_result: Mapped[MergeResult] = relationship(back_populates="conflicts")


class ImpactReport(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """What a proposed change would break.

    Mandatory before a tag rename is approved. A rename that looks like a
    cosmetic tidy-up in the tag table can silently orphan an HMI binding, an
    alarm trigger and three test steps, and the only honest way to approve one
    is to see that list first.
    """

    __tablename__ = "impact_reports"

    project_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    change_set_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("change_sets.id", ondelete="CASCADE"), nullable=True, index=True
    )
    operation_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("change_operations.id", ondelete="SET NULL"), nullable=True
    )

    object_type: Mapped[ObjectType] = mapped_column(nullable=False)
    object_id: Mapped[uuid.UUID | None] = mapped_column(GUID, nullable=True)
    object_label: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    change_description: Mapped[str] = mapped_column(Text, default="", nullable=False)

    #: Counts per discipline: logic, hmi, alarms, tests, documentation, io.
    affected_counts: Mapped[dict[str, Any]] = mapped_column(JSONDict, default=dict, nullable=False)
    #: Every affected object, each with type, id, label and how it is affected.
    affected_objects: Mapped[list[Any]] = mapped_column(JSONList, default=list, nullable=False)
    #: References the analysis could not resolve — the ones most likely to break
    #: quietly, so they are listed separately rather than folded into the total.
    unresolved_references: Mapped[list[Any]] = mapped_column(JSONList, default=list, nullable=False)

    #: No default. ``INFORMATION`` is the "this breaks nothing" end of the
    #: scale, and a report that has not been computed must not be able to
    #: present itself there by omission. ``app.engineering.impact`` builds the
    #: row only after the sweeps have run and always states this, so requiring
    #: it costs nothing and makes any future writer that forgets fail at the
    #: flush rather than in front of a reviewer.
    highest_severity: Mapped[ValidationSeverity] = mapped_column(nullable=False)
    requires_human_approval: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    #: Written to be read aloud in a review: what breaks, and what must be done.
    summary: Mapped[str] = mapped_column(Text, default="", nullable=False)

    @property
    def total_affected(self) -> int:
        return len(self.affected_objects)

    @property
    def is_safe_to_apply(self) -> bool:
        """No affected object anywhere and nothing unresolved."""
        return not self.affected_objects and not self.unresolved_references


class ApprovalGate(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One of the six configurable gates a project passes through.

    A gate is a row rather than a computed state so that its criteria, its
    decision and the revision it was decided against are all durable. "This was
    approved" is only useful alongside "approved on the basis of what".
    """

    __tablename__ = "approval_gates"
    __table_args__ = (
        UniqueConstraint("project_id", "gate_key", name="uq_approval_gates_project_id_gate_key"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    gate_key: Mapped[ApprovalGateKey] = mapped_column(nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)

    status: Mapped[GateStatus] = mapped_column(
        default=GateStatus.NOT_READY, nullable=False, index=True
    )
    #: An administrator may turn a gate off for a project. Recorded rather than
    #: silently skipped, because "nobody reviewed the tag model" needs to stay
    #: visible in the release record.
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    disabled_reason: Mapped[str] = mapped_column(Text, default="", nullable=False)

    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    #: What must hold before this gate can be presented, each with its state.
    criteria: Mapped[list[Any]] = mapped_column(JSONList, default=list, nullable=False)
    unmet_criteria: Mapped[list[Any]] = mapped_column(JSONList, default=list, nullable=False)

    approval_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("approvals.id", ondelete="SET NULL"), nullable=True
    )
    decided_by_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    decided_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    #: The revision the decision was made against. A later change does not
    #: silently inherit the approval — it re-opens the gate.
    decided_revision_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    decision_comment: Mapped[str] = mapped_column(Text, default="", nullable=False)

    @property
    def is_passed(self) -> bool:
        return self.status is GateStatus.PASSED

    @property
    def blocks_progress(self) -> bool:
        """Whether work behind this gate must wait."""
        if not self.is_enabled:
            return False
        return self.status in {GateStatus.NOT_READY, GateStatus.READY, GateStatus.AWAITING_DECISION}
