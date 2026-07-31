"""Interlocks, permissives, cause-and-effect, modes, states, sequences and faults.

This is the safety-adjacent part of the canonical engineering model, and it is
written on one rule: nothing in these tables is generated without a stated
basis. Every row is either read out of a customer document, expanded from an
approved template, or proposed by an agent against a cited requirement, and
which of those it was is recorded on the row itself — ``origin`` and
``source_reference`` here, the citation in
:class:`~app.models.core.ObjectEvidenceLink`.

The rule that matters most is the negative one. An interlock the documentation
describes but whose implementation cannot be found in the logic is *reported*,
never assumed present: it is stored with ``is_implemented`` False and surfaces
through :attr:`Interlock.is_documented_but_missing`. Filling that gap in with a
plausible rung — or quietly setting the flag because a similarly named tag
exists somewhere — is the exact failure this product exists to prevent. The
other honest-gap columns work the same way: ``InterlockAction.UNDEFINED``,
``ResetBehaviorKind.UNDEFINED`` and ``FaultDefinition.is_driven`` all mean "the
source did not say", and none of them is a slot to put a guess in.

Two conventions worth knowing before reading the tables:

* Wherever a document names a tag, the name is kept as text (``*_tag_name``)
  *and* an optional foreign key to the resolved tag is offered (``*_tag_id``).
  The text survives when resolution fails, so an unresolvable reference stays a
  reportable fact instead of becoming a null nobody can explain.
* An enum column is only given a default where the enum has a member that
  asserts nothing (``UNDEFINED``, ``OTHER``). Where it has none —
  :class:`~app.core.enums.InterlockKind`, :class:`~app.core.enums.FaultClass` —
  the column is required, so the classification has to be stated rather than
  inherited from whichever member happens to be listed first.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import (
    AlarmSeverity,
    FaultClass,
    InterlockAction,
    InterlockKind,
    ModeKind,
    ResetBehaviorKind,
)
from app.database.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.database.types import GUID, JSONList
from app.models.core import EngineeringObjectMixin


class Interlock(UUIDPrimaryKeyMixin, EngineeringObjectMixin, TimestampMixin, Base):
    """A condition that stops, prevents or inhibits something.

    Split deliberately from :class:`Permissive`: an interlock acts on a running
    machine, a permissive gates a start. Conflating them loses the distinction
    an engineer needs most when reading a trip that fired.

    The columns fall into three groups, and keeping them apart is the point of
    the table. What the document *said* (``trip_condition``, ``trip_tag_name``,
    ``threshold_value``, ``source_reference``), what the logic *does*
    (``is_implemented``, ``implementing_rung_id``), and what resolution
    *succeeded* (``trip_tag_id``, ``affected_device_id``). A comparison between
    the first two groups is the product's headline output; it only stays
    trustworthy while the groups are not allowed to overwrite each other.
    """

    __tablename__ = "interlocks"
    __table_args__ = (
        UniqueConstraint("project_id", "reference", name="uq_interlocks_project_id_reference"),
        # The documented-but-not-implemented sweep runs on every project and is
        # what the report is built from, so it gets an index rather than a scan.
        Index("ix_interlocks_project_id_is_implemented", "project_id", "is_implemented"),
    )

    #: The identifier the source document used, e.g. ``ILK-003``. Required and
    #: unique per project because it is how a human cross-references this row
    #: back to the interlock schedule; a synthesised one is still a reference.
    reference: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)

    #: No ``UNDEFINED`` member exists on InterlockKind, so this is required
    #: rather than defaulted — see the module docstring.
    kind: Mapped[InterlockKind] = mapped_column(nullable=False, index=True)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)

    #: The trip condition as documented, in the document's own words. Not
    #: normalised: the phrasing is evidence, and rewriting it into a canonical
    #: form loses the ability to show an engineer what was actually written.
    trip_condition: Mapped[str] = mapped_column(Text, default="", nullable=False)
    #: The tag the documentation names. Kept as text because it may not resolve
    #: — a schedule routinely names a tag that was renamed or never created,
    #: and that mismatch is a finding rather than something to discard.
    trip_tag_name: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    trip_tag_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("tags.id", ondelete="SET NULL"), nullable=True, index=True
    )

    #: The threshold exactly as written, units included, e.g. ``"40 psi"``.
    #: Keeping the units inside the string preserves what the document actually
    #: said. Reinterpreting it — deciding that means 40.0 in engineering units,
    #: or converting to bar — is the agent's job, done with evidence and
    #: recorded as its own object. It is not the model's job, and a numeric
    #: column here would force the conversion to happen silently at parse time.
    threshold_value: Mapped[str] = mapped_column(String(60), default="", nullable=False)
    #: The comparison as written: ``<``, ``>=``, ``"falls below"``. Free text
    #: for the same reason as ``threshold_value``.
    comparison: Mapped[str] = mapped_column(String(20), default="", nullable=False)

    affected_device_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("devices.id", ondelete="SET NULL"), nullable=True, index=True
    )
    affected_tag_name: Mapped[str] = mapped_column(String(120), default="", nullable=False)

    action: Mapped[InterlockAction] = mapped_column(
        default=InterlockAction.UNDEFINED, nullable=False
    )
    reset_behavior_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("reset_behaviors.id", ondelete="SET NULL"), nullable=True
    )

    #: Set only from an explicit statement in a source or a recorded human
    #: decision. An interlock is not classified as safety-related because its
    #: name contains "guard".
    is_safety_related: Mapped[bool] = mapped_column(default=False, nullable=False)

    #: False until logic implementing this interlock has actually been located.
    #: The default is the honest one: not yet found is not the same as absent,
    #: and both are different from present.
    is_implemented: Mapped[bool] = mapped_column(default=False, nullable=False)
    implementing_rung_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("logic_rungs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    #: How the implementation search concluded what it did — including when it
    #: found nothing, and what it looked for. Read by the engineer who has to
    #: decide whether to trust a "missing" verdict.
    implementation_note: Mapped[str] = mapped_column(Text, default="", nullable=False)

    #: The requirement this interlock traces to, e.g. ``REQ-014``.
    requirement_reference: Mapped[str] = mapped_column(String(60), default="", nullable=False)
    #: Where in the source document this came from, in human terms — sheet and
    #: row, section number, page. Complements the machine-resolvable evidence
    #: anchor; this one is what gets printed in a report.
    source_reference: Mapped[str] = mapped_column(String(200), default="", nullable=False)

    @property
    def has_implementation(self) -> bool:
        """True only when the implementing logic can actually be pointed at.

        ``is_implemented`` on its own is a claim; ``implementing_rung_id`` is
        the evidence for it. Requiring both means "implemented" cannot be
        asserted by a flag nobody can follow back to a rung.
        """
        return self.is_implemented and self.implementing_rung_id is not None

    @property
    def is_documented_but_missing(self) -> bool:
        """Documented in a source, and nothing in the logic implements it.

        This exact condition is the highest-value finding the product makes:
        the schedule says the pump trips on low suction pressure and nothing in
        the controller does. It is a plain, cheap property rather than an agent
        judgement on purpose — the report can be regenerated and re-checked
        without re-running a model, and the answer is the same every time.
        """
        documented = bool(self.source_reference or self.requirement_reference)
        return documented and not self.is_implemented


class Permissive(UUIDPrimaryKeyMixin, EngineeringObjectMixin, TimestampMixin, Base):
    """A condition that must be true before something may start.

    The mirror image of :class:`Interlock` and a separate table for a reason:
    an interlock stops equipment that is running, a permissive refuses to let it
    start. They fail differently, they are tested differently, and a report that
    merges them cannot tell an engineer which one they are looking at.

    ``bypass_allowed`` is recorded rather than assumed in either direction. A
    permissive that may be bypassed and one whose bypass nobody documented look
    identical in a functional description, and the difference matters.
    """

    __tablename__ = "permissives"
    __table_args__ = (
        Index("ix_permissives_project_id_is_implemented", "project_id", "is_implemented"),
    )

    #: Defaulted to empty rather than required: unlike an interlock schedule, a
    #: functional description often states a start condition in prose without
    #: ever numbering it.
    reference: Mapped[str] = mapped_column(String(40), default="", nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)

    #: The condition as documented, in the document's own words.
    condition: Mapped[str] = mapped_column(Text, default="", nullable=False)
    condition_tag_name: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    condition_tag_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("tags.id", ondelete="SET NULL"), nullable=True, index=True
    )

    #: What the permissive gates.
    device_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("devices.id", ondelete="SET NULL"), nullable=True, index=True
    )

    is_implemented: Mapped[bool] = mapped_column(default=False, nullable=False)
    implementing_rung_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("logic_rungs.id", ondelete="SET NULL"), nullable=True, index=True
    )

    #: False by default: a bypass is a capability someone has to have granted,
    #: never something read into silence.
    bypass_allowed: Mapped[bool] = mapped_column(default=False, nullable=False)
    #: Under what circumstances, and with whose authority. Empty while
    #: ``bypass_allowed`` is true is itself worth reporting.
    bypass_conditions: Mapped[str] = mapped_column(Text, default="", nullable=False)


class CauseEffectEntry(UUIDPrimaryKeyMixin, EngineeringObjectMixin, TimestampMixin, Base):
    """One cell of a cause-and-effect matrix.

    Stored per cell rather than per row or per matrix because that is the unit
    an engineer argues about: "does trip X really shut down Y, and after how
    long". A cell carries its own implementation status, so a matrix can be
    reported as ninety per cent implemented with the missing ten per cent named
    instead of being marked as one large partial failure.

    The cause and effect are held as text alongside optional resolved tag ids
    for the usual reason: matrices name signals that no longer exist, and that
    is a finding rather than a parse error to swallow.
    """

    __tablename__ = "cause_effect_entries"

    reference: Mapped[str] = mapped_column(String(40), default="", nullable=False, index=True)

    cause_description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    cause_tag_name: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    cause_tag_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("tags.id", ondelete="SET NULL"), nullable=True, index=True
    )

    effect_description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    effect_tag_name: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    effect_tag_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("tags.id", ondelete="SET NULL"), nullable=True, index=True
    )

    #: The interlock this cell restates, when the matrix and the schedule
    #: describe the same protection. Nullable because a matrix frequently
    #: contains effects that appear in no interlock schedule at all — which is
    #: precisely the discrepancy worth surfacing.
    interlock_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("interlocks.id", ondelete="SET NULL"), nullable=True, index=True
    )

    #: Nullable, and null means "the matrix stated no delay" — not zero. A zero
    #: here would assert an immediate trip the document never specified.
    delay_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)

    is_implemented: Mapped[bool] = mapped_column(default=False, nullable=False)
    notes: Mapped[str] = mapped_column(Text, default="", nullable=False)

    #: Position within the matrix, so the original reading order survives.
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)


class ModeDefinition(UUIDPrimaryKeyMixin, EngineeringObjectMixin, TimestampMixin, Base):
    """An operating mode the equipment can be in.

    Modes are first-class because almost every other object in this module is
    qualified by one: a permissive that applies in AUTO and not in MANUAL is a
    different engineering statement from one that always applies, and a sequence
    step usually belongs to exactly one mode.

    ``allowed_transitions`` is a JSON list of mode *names* rather than a join
    table. Modes are few, the list is read whole or not at all, and no
    validation needs to query "which modes can reach MAINTENANCE" from the
    database side. That reasoning does not hold for state transitions — see
    :class:`TransitionDefinition`, which is a table for exactly that reason.
    """

    __tablename__ = "mode_definitions"
    __table_args__ = (
        UniqueConstraint("project_id", "name", name="uq_mode_definitions_project_id_name"),
    )

    reference: Mapped[str] = mapped_column(String(40), default="", nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    #: ``OTHER`` is a member that asserts nothing, so it is a safe default: a
    #: mode the document names but does not classify stays unclassified.
    mode_kind: Mapped[ModeKind] = mapped_column(default=ModeKind.OTHER, nullable=False, index=True)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)

    #: How the mode is selected in the running system.
    selection_tag_name: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    selection_tag_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("tags.id", ondelete="SET NULL"), nullable=True, index=True
    )
    #: The value that selects this mode, as written — an integer, a bit name or
    #: a switch position, so text rather than a number.
    selection_value: Mapped[str] = mapped_column(String(40), default="", nullable=False)

    #: Never inferred from ordering. A project where no mode is marked default
    #: is reported as a gap, not resolved by picking the first one.
    is_default: Mapped[bool] = mapped_column(default=False, nullable=False)

    #: Mode names this mode may move to. Names, not ids, so a documented
    #: transition to a mode that was never defined survives as a dangling name
    #: the validator can report.
    allowed_transitions: Mapped[list[Any]] = mapped_column(JSONList, default=list, nullable=False)

    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)


class StateDefinition(UUIDPrimaryKeyMixin, EngineeringObjectMixin, TimestampMixin, Base):
    """A state within an equipment state machine.

    Entry and exit actions are text because they are usually documented as
    prose ("open the inlet valve, start the agitator") long before any logic
    exists. Structuring them into commands would mean inventing a mapping onto
    tags that the document did not make.
    """

    __tablename__ = "state_definitions"
    __table_args__ = (
        # Unique per project rather than per equipment module: test cases, HMI
        # bindings and reports refer to a state by name, so two different
        # "RUNNING" states in one project would make every one of those
        # references ambiguous. The cost is that state names have to be
        # qualified by their equipment, which is the cheaper of the two evils.
        UniqueConstraint("project_id", "name", name="uq_state_definitions_project_id_name"),
    )

    #: The mode this state belongs to, when the document scopes it to one.
    mode_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("mode_definitions.id", ondelete="SET NULL"), nullable=True, index=True
    )
    equipment_module_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("equipment_modules.id", ondelete="SET NULL"), nullable=True, index=True
    )

    reference: Mapped[str] = mapped_column(String(40), default="", nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)

    #: A state machine with no initial state, or with two, is a defect the
    #: validator reports. Neither is repaired by defaulting.
    is_initial: Mapped[bool] = mapped_column(default=False, nullable=False)
    is_terminal: Mapped[bool] = mapped_column(default=False, nullable=False)

    entry_actions: Mapped[str] = mapped_column(Text, default="", nullable=False)
    exit_actions: Mapped[str] = mapped_column(Text, default="", nullable=False)

    #: The tag holding the current state, and the value this state corresponds
    #: to. ``state_value`` is nullable because "the state exists but nobody
    #: assigned it a number yet" is a real and reportable condition; zero is a
    #: legitimate state number and must not double as "unset".
    state_tag_name: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    state_value: Mapped[int | None] = mapped_column(Integer, nullable=True)

    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)

    #: Within-module relationship, and it earns its place: the unreachable-exit
    #: check is "len(state.outgoing_transitions) == 0 and not state.is_terminal",
    #: which is only that cheap because transitions are rows.
    outgoing_transitions: Mapped[list[TransitionDefinition]] = relationship(
        back_populates="from_state",
        foreign_keys="TransitionDefinition.from_state_id",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="selectin",
    )
    incoming_transitions: Mapped[list[TransitionDefinition]] = relationship(
        back_populates="to_state",
        foreign_keys="TransitionDefinition.to_state_id",
        passive_deletes=True,
        lazy="select",
    )


class TransitionDefinition(UUIDPrimaryKeyMixin, EngineeringObjectMixin, TimestampMixin, Base):
    """One permitted move between two states, and what causes it.

    Transitions are rows rather than a JSON blob hanging off the state on
    purpose. A state with no outgoing transition is an unreachable-exit
    condition — equipment that can enter a state and never leave it — and that
    is one of the checks the validator has to run over every state machine in
    the project. As rows the check is a join and a count; as JSON it is a full
    scan and a decode per state, and every future query about transitions
    (which ones time out, which ones are fault paths, which condition tags are
    never written) inherits the same cost.

    Both endpoints cascade: a transition to or from a state that no longer
    exists is not a record of anything.
    """

    __tablename__ = "transition_definitions"
    __table_args__ = (
        Index(
            "ix_transition_definitions_from_state_id_ordinal",
            "from_state_id",
            "ordinal",
        ),
    )

    from_state_id: Mapped[uuid.UUID] = mapped_column(
        GUID,
        ForeignKey("state_definitions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    to_state_id: Mapped[uuid.UUID] = mapped_column(
        GUID,
        ForeignKey("state_definitions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    reference: Mapped[str] = mapped_column(String(40), default="", nullable=False, index=True)

    #: What causes the move, in the document's own words.
    condition: Mapped[str] = mapped_column(Text, default="", nullable=False)
    condition_tag_name: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    condition_tag_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("tags.id", ondelete="SET NULL"), nullable=True, index=True
    )

    #: Null means the document stated no timeout, which is different from a
    #: timeout of zero and different again from "waits forever by design".
    timeout_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    #: True when this is the abnormal exit. Kept separate from the condition
    #: text so "can this state machine reach a fault state at all" is a query.
    is_fault_transition: Mapped[bool] = mapped_column(default=False, nullable=False)

    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)

    from_state: Mapped[StateDefinition] = relationship(
        back_populates="outgoing_transitions", foreign_keys=[from_state_id]
    )
    to_state: Mapped[StateDefinition] = relationship(
        back_populates="incoming_transitions", foreign_keys=[to_state_id]
    )


class SequenceStep(UUIDPrimaryKeyMixin, EngineeringObjectMixin, TimestampMixin, Base):
    """One step of a documented operating sequence.

    ``step_number`` is the number the document used and ``next_step_number``
    the successor it named. Both are plain integers rather than foreign keys
    because a functional description routinely sends step 40 to a step 90 that
    was deleted in a later revision — as an integer that dangling successor
    survives to be reported, whereas a foreign key would have refused the insert
    and lost the evidence that the document is inconsistent.

    ``ordinal`` is kept alongside ``step_number`` because they are not the same
    thing: ``step_number`` is the document's label, ``ordinal`` is the order the
    steps were read in. When a document numbers steps 10, 20, 15 that
    discrepancy is only visible if both are stored.
    """

    __tablename__ = "sequence_steps"
    __table_args__ = (
        UniqueConstraint("project_id", "reference", name="uq_sequence_steps_project_id_reference"),
        Index("ix_sequence_steps_equipment_module_id_ordinal", "equipment_module_id", "ordinal"),
    )

    equipment_module_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("equipment_modules.id", ondelete="SET NULL"), nullable=True, index=True
    )
    mode_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("mode_definitions.id", ondelete="SET NULL"), nullable=True, index=True
    )

    #: Required and unique per project: a sequence step is cited by reference in
    #: test cases, so it needs a stable handle even when the source numbered it
    #: only within its own document.
    reference: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    step_number: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)

    #: What the step does, and what ends it, both as documented.
    action: Mapped[str] = mapped_column(Text, default="", nullable=False)
    completion_condition: Mapped[str] = mapped_column(Text, default="", nullable=False)
    completion_tag_name: Mapped[str] = mapped_column(String(120), default="", nullable=False)

    #: Null means no timeout was documented. A step with a timeout and no
    #: ``timeout_action`` is a gap worth reporting rather than filling in.
    timeout_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    timeout_action: Mapped[str] = mapped_column(Text, default="", nullable=False)
    next_step_number: Mapped[int | None] = mapped_column(Integer, nullable=True)

    is_implemented: Mapped[bool] = mapped_column(default=False, nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)


class FaultDefinition(UUIDPrimaryKeyMixin, EngineeringObjectMixin, TimestampMixin, Base):
    """A fault or alarm condition the system can report.

    ``operator_message`` and ``corrective_action`` are stored because they are
    the part of an alarm an operator actually experiences, and they are also the
    part most often missing: an alarm that fires with no message and no stated
    remedy is a documented defect even when the logic behind it is perfect.
    """

    __tablename__ = "fault_definitions"
    __table_args__ = (
        # "Which alarms can never fire" is a whole-project sweep run at every
        # validation, so the flag is indexed with the project rather than
        # scanned. See ``is_driven`` for why the question matters.
        Index("ix_fault_definitions_project_id_is_driven", "project_id", "is_driven"),
    )

    reference: Mapped[str] = mapped_column(String(40), default="", nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    #: FaultClass has no "unclassified" member, so this is required rather than
    #: defaulted — see the module docstring.
    fault_class: Mapped[FaultClass] = mapped_column(nullable=False, index=True)
    severity: Mapped[AlarmSeverity] = mapped_column(default=AlarmSeverity.MEDIUM, nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)

    trigger_condition: Mapped[str] = mapped_column(Text, default="", nullable=False)
    trigger_tag_name: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    trigger_tag_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("tags.id", ondelete="SET NULL"), nullable=True, index=True
    )

    #: True once logic has been found that actually writes the trigger tag.
    #:
    #: An alarm whose trigger tag nothing ever writes is a defect: the alarm is
    #: configured, it appears in the alarm list, it passes every review that
    #: only reads the alarm list — and the operator will never see it, because
    #: no rung can ever set the bit. Recording the answer as a column is what
    #: turns "is this alarm reachable" from a manual cross-reference exercise
    #: into a query. False is the honest default: not yet established.
    is_driven: Mapped[bool] = mapped_column(default=False, nullable=False)

    affected_device_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("devices.id", ondelete="SET NULL"), nullable=True, index=True
    )
    reset_behavior_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("reset_behaviors.id", ondelete="SET NULL"), nullable=True
    )

    #: What the operator is shown, and what they are told to do about it.
    operator_message: Mapped[str] = mapped_column(Text, default="", nullable=False)
    corrective_action: Mapped[str] = mapped_column(Text, default="", nullable=False)


# ``ResetBehavior`` is referenced by ``Interlock.reset_behavior_id`` and
# ``FaultDefinition.reset_behavior_id`` above, yet is declared last. That is not
# an oversight and not a hazard: a ``ForeignKey`` names its target as the string
# ``"reset_behaviors.id"``, resolved against the metadata at mapper
# configuration time rather than at class-definition time, so declaration order
# within a module — and across modules — carries no meaning. The order here is
# chosen for reading: the objects an engineer looks for first come first.
class ResetBehavior(UUIDPrimaryKeyMixin, EngineeringObjectMixin, TimestampMixin, Base):
    """How a tripped interlock or an active fault is cleared.

    A separate object rather than a column on each because reset behaviour is
    usually specified once for a whole class of faults ("all E-stops require a
    local reset at the panel") and then referenced. Duplicating it per fault
    would make the single documented rule impossible to distinguish from twenty
    independent decisions that happen to agree.

    ``kind`` defaults to :attr:`~app.core.enums.ResetBehaviorKind.UNDEFINED`,
    and that is the honest default rather than a placeholder. A fault whose
    reset behaviour nobody specified is a documentation gap to report — it is
    the difference between an operator pressing a button and a machine
    restarting on its own, and guessing which one the customer meant, in either
    direction, would be inventing an engineering decision nobody made.
    """

    __tablename__ = "reset_behaviors"

    reference: Mapped[str] = mapped_column(String(40), default="", nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    kind: Mapped[ResetBehaviorKind] = mapped_column(
        default=ResetBehaviorKind.UNDEFINED, nullable=False, index=True
    )
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)

    reset_tag_name: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    reset_tag_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("tags.id", ondelete="SET NULL"), nullable=True, index=True
    )

    #: Defaults to True because a reset that takes effect while the condition is
    #: still present is the unusual, dangerous case — one that should have to be
    #: stated rather than arrived at by leaving a field alone.
    requires_condition_cleared: Mapped[bool] = mapped_column(default=True, nullable=False)
    requires_operator_acknowledge: Mapped[bool] = mapped_column(default=False, nullable=False)

    notes: Mapped[str] = mapped_column(Text, default="", nullable=False)
