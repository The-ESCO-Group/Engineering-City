"""Controller organisation and the ladder-logic intermediate representation.

Two things live here: how a Logix project is organised — controller, tasks,
programs, routines — and the IR that says what the ladder logic actually does,
down to the individual operand.

**The IR is the source of truth for logic, and L5X is emitted from it. No agent
ever writes XML.** A model proposes structured rows — this rung, these
instructions, these operands, expanded from this template with these parameters.
Deterministic code validates that proposal against the target profile's
instruction set and tag scope, merges it if it survives, and only then does a
generator read these tables and emit XML with correct escaping. That ordering is
the load-bearing decision of this module: it is what makes the same approved
revision produce byte-identical artifacts, and what lets a generated rung answer
"which requirement is this from" instead of being a blob of markup whose only
adjudicator is Studio 5000.

The consequence for every table below is that anything textual is a *rendering*.
``LogicRung.text`` is regenerated from the instruction, operand and branch rows
beneath it and never parsed back into them. A generator that reads text and
writes structure has the arrow the wrong way round, and that is exactly how
logic tools end up producing XML nobody can validate.

Two design choices exist purely to turn engineering questions into queries:

* ``is_destructive`` — carried on the instruction, the operand and the
  cross-reference row — makes "two rungs both drive this coil" a group-by rather
  than a review somebody has to be sharp enough to notice.
* :class:`LogicReference` together with ``Routine.is_called`` makes "this routine
  is never called" a filter rather than an audit nobody schedules.
"""

from __future__ import annotations

import uuid

from sqlalchemy import Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import (
    ControllerTaskType,
    InstructionCategory,
    LogicElementKind,
    OperandKind,
    ProgramType,
    RoutineLanguage,
)
from app.database.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.database.types import GUID
from app.models.core import EngineeringObjectMixin

# ---------------------------------------------------------------------------
# Controller organisation
# ---------------------------------------------------------------------------


class ControllerModel(UUIDPrimaryKeyMixin, EngineeringObjectMixin, TimestampMixin, Base):
    """The controller a project targets.

    Exactly one per project, enforced by a unique constraint on ``project_id``
    alone. A project with two controllers is a scoping decision, not a row: the
    tag namespace, the task set and the emitted controller L5X are all singular,
    so a second controller means a second project rather than a second row here.

    The fields are deliberately a description of the target and not a
    configuration we compose. Where the source states something — catalogue
    number, firmware revision, communication path — it is stored as stated.
    """

    __tablename__ = "controller_models"
    __table_args__ = (UniqueConstraint("project_id", name="uq_controller_models_project_id"),)

    name: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    #: e.g. ``1756-L83E``. The catalogue number is what pins the instruction set
    #: and tag limits the generator validates against, so it is a column.
    catalog_number: Mapped[str] = mapped_column(String(60), default="", nullable=False)

    #: Firmware revision. Nullable rather than zero-defaulted: a controller whose
    #: revision the source never stated is a documented gap, and ``0`` would read
    #: as a real revision nobody wrote down.
    major_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    minor_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)

    description: Mapped[str] = mapped_column(Text, default="", nullable=False)

    redundancy_enabled: Mapped[bool] = mapped_column(default=False, nullable=False)
    #: True when the controller carries a safety task. Reading it does not by
    #: itself authorise generating safety logic; the restricted-domain rules do.
    safety_enabled: Mapped[bool] = mapped_column(default=False, nullable=False)

    #: The path a workstation uses to reach the controller, as configured.
    comm_path: Mapped[str] = mapped_column(String(200), default="", nullable=False)

    #: Stored exactly as the source wrote it, and never parsed into a datetime.
    #: An L5X timestamp is provenance — it says what the exporting tool claimed,
    #: in whatever format and whatever timezone that tool used. Reinterpreting it
    #: as a moment in time would be us inventing precision the source never had,
    #: and the only honest use of the value is to show it back verbatim.
    project_creation_date: Mapped[str] = mapped_column(String(40), default="", nullable=False)

    #: Studio 5000's ``UseAsFolder`` flag, carried through so a round-trip export
    #: does not silently change how the project presents in the tool.
    use_as_folder: Mapped[bool] = mapped_column(default=False, nullable=False)


class ControllerTask(UUIDPrimaryKeyMixin, EngineeringObjectMixin, TimestampMixin, Base):
    """A controller task: the scheduling unit programs are attached to.

    Scan behaviour lives here rather than on the program, because that is where
    it actually is in the controller. A rung's execution rate is a property of
    the task its program hangs off, which is why an interlock reviewed in
    isolation can still be too slow.
    """

    __tablename__ = "controller_tasks"
    __table_args__ = (
        UniqueConstraint("project_id", "name", name="uq_controller_tasks_project_id_name"),
    )

    name: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    #: No default: a task whose type nobody stated is a question to ask, not a
    #: continuous task to assume.
    task_type: Mapped[ControllerTaskType] = mapped_column(nullable=False)

    priority: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: Null means "does not apply or was not stated" — a continuous task has no
    #: period at all, so a numeric default would be a fabricated scan rate.
    rate_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    watchdog_ms: Mapped[float | None] = mapped_column(Float, nullable=True)

    disable_update_outputs: Mapped[bool] = mapped_column(default=False, nullable=False)
    #: An inhibited task does not scan. Carried as a column because "the logic is
    #: correct but the task is inhibited" is a real defect, and an invisible one.
    inhibit_task: Mapped[bool] = mapped_column(default=False, nullable=False)

    #: The trigger for an event task, as named by the source.
    event_trigger: Mapped[str] = mapped_column(String(60), default="", nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)

    programs: Mapped[list[Program]] = relationship(
        back_populates="controller_task", lazy="select", order_by="Program.ordinal"
    )


class Program(UUIDPrimaryKeyMixin, EngineeringObjectMixin, TimestampMixin, Base):
    """A program: a scoped tag namespace plus the routines inside it."""

    __tablename__ = "programs"
    __table_args__ = (UniqueConstraint("project_id", "name", name="uq_programs_project_id_name"),)

    #: Nullable and ``SET NULL``: an unscheduled program is a legitimate state in
    #: Logix, and losing the task must not delete the logic underneath it.
    controller_task_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("controller_tasks.id", ondelete="SET NULL"), nullable=True, index=True
    )

    name: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    program_type: Mapped[ProgramType] = mapped_column(default=ProgramType.NORMAL, nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)

    #: Names rather than foreign keys. The controller identifies these routines
    #: by name, and the name is often set before the routine exists — storing an
    #: id would make "the main routine is missing" unrepresentable, when it is
    #: precisely the condition worth reporting.
    main_routine_name: Mapped[str] = mapped_column(String(60), default="", nullable=False)
    fault_routine_name: Mapped[str] = mapped_column(String(60), default="", nullable=False)

    disabled: Mapped[bool] = mapped_column(default=False, nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    #: True when the program was exported with pending test edits. Kept because
    #: it means the logic in the file is not necessarily the logic running.
    test_edits: Mapped[bool] = mapped_column(default=False, nullable=False)

    controller_task: Mapped[ControllerTask | None] = relationship(back_populates="programs")
    routines: Mapped[list[Routine]] = relationship(
        back_populates="program",
        cascade="all, delete-orphan",
        lazy="select",
        order_by="Routine.ordinal",
    )


class Routine(UUIDPrimaryKeyMixin, EngineeringObjectMixin, TimestampMixin, Base):
    """One routine within a program.

    ``template_id`` / ``template_version`` are recorded on the routine as well as
    on the rungs, so a routine generated wholesale from an approved layout stays
    distinguishable from one assembled rung by rung.
    """

    __tablename__ = "routines"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "program_id", "name", name="uq_routines_project_id_program_id_name"
        ),
    )

    program_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("programs.id", ondelete="CASCADE"), nullable=False, index=True
    )

    name: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    language: Mapped[RoutineLanguage] = mapped_column(
        default=RoutineLanguage.LADDER, nullable=False
    )
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)

    #: True when this is the program's main routine, i.e. the controller calls it
    #: without a JSR.
    is_main: Mapped[bool] = mapped_column(default=False, nullable=False)
    #: Set by the cross-reference pass when some rung JSRs to this routine. A
    #: routine nothing calls is dead logic, and this flag is what makes that a
    #: query — ``is_called == False and is_main == False`` — instead of an
    #: observation that depends on a reviewer reading every JSR in the project.
    is_called: Mapped[bool] = mapped_column(default=False, nullable=False)

    #: Denormalised count, maintained when rungs change. Present so a routine
    #: listing does not have to aggregate the whole rung table to say "0 rungs",
    #: which is the state worth spotting.
    rung_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    template_id: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    template_version: Mapped[str] = mapped_column(String(20), default="", nullable=False)

    program: Mapped[Program] = relationship(back_populates="routines")
    rungs: Mapped[list[LogicRung]] = relationship(
        back_populates="routine",
        cascade="all, delete-orphan",
        lazy="select",
        order_by="LogicRung.ordinal",
    )
    networks: Mapped[list[LogicNetwork]] = relationship(
        back_populates="routine",
        cascade="all, delete-orphan",
        lazy="select",
        order_by="LogicNetwork.ordinal",
    )

    @property
    def is_dead_logic(self) -> bool:
        """Whether nothing in the project reaches this routine.

        Only meaningful after the cross-reference pass has run; before that
        ``is_called`` is simply unset for every routine.
        """
        return not self.is_main and not self.is_called


# ---------------------------------------------------------------------------
# Ladder logic IR
# ---------------------------------------------------------------------------


class LogicNetwork(UUIDPrimaryKeyMixin, EngineeringObjectMixin, TimestampMixin, Base):
    """A named, ordered group of rungs within a routine.

    This is the unit a template expands into, and the reason it exists as a row:
    "the motor-control block for MTR-101" is a thing an engineer reviews, revises
    and re-expands as a whole, whereas rungs 14 through 19 are an accident of
    where the block happened to land. Without it, re-expanding a template at a
    new version means diffing loose rungs and guessing which ones belonged
    together.

    Rungs point at their network rather than the other way round, and the link is
    nullable: hand-written logic belongs to no network, and that is not a defect.
    """

    __tablename__ = "logic_networks"
    __table_args__ = (Index("ix_logic_networks_routine_id_ordinal", "routine_id", "ordinal"),)

    routine_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("routines.id", ondelete="CASCADE"), nullable=False, index=True
    )

    name: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    #: One line an engineer can check the rungs against: what this block is for.
    purpose: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)

    template_id: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    template_version: Mapped[str] = mapped_column(String(20), default="", nullable=False)
    #: The requirement this block implements, by its project-visible identifier.
    #: The authoritative citation is an ``ObjectEvidenceLink``; this is the
    #: human-readable label carried alongside so a rung listing reads sensibly.
    requirement_reference: Mapped[str] = mapped_column(String(60), default="", nullable=False)

    rung_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    routine: Mapped[Routine] = relationship(back_populates="networks")
    rungs: Mapped[list[LogicRung]] = relationship(
        back_populates="logic_network", lazy="select", order_by="LogicRung.ordinal"
    )


class LogicRung(UUIDPrimaryKeyMixin, EngineeringObjectMixin, TimestampMixin, Base):
    """One rung of ladder logic.

    The rung is the unit of review, of approval and of traceability: it is in
    ``TRACEABLE_OBJECT_TYPES``, so a rung that cites no requirement, template
    rule or human decision is refused at merge.
    """

    __tablename__ = "logic_rungs"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "routine_id",
            "ordinal",
            name="uq_logic_rungs_project_id_routine_id_ordinal",
        ),
    )

    routine_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("routines.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: Nullable: hand-written and parsed rungs belong to no template block.
    logic_network_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("logic_networks.id", ondelete="SET NULL"), nullable=True, index=True
    )

    #: Rung number within the routine. Execution order, not a label — ladder is
    #: scanned top to bottom, so this is semantically significant.
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)

    title: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    comment: Mapped[str] = mapped_column(Text, default="", nullable=False)

    #: The neutral rendering, e.g. ``XIC(A)XIC(B)OTE(C);``.
    #:
    #: This is a *rendering* of the instruction, operand and branch rows below,
    #: regenerated from them whenever they change, and never the other way round.
    #: Nothing parses this field back into structure. The moment text becomes the
    #: input, the structured rows stop being trustworthy and the generator is
    #: emitting strings it cannot validate — which is the failure this whole
    #: module is arranged to prevent. Stored rather than computed on read only so
    #: that listings, diffs and exports are cheap.
    text: Mapped[str] = mapped_column(Text, default="", nullable=False)

    is_safety_related: Mapped[bool] = mapped_column(default=False, nullable=False)
    #: Forces a human decision before this rung can reach a release candidate,
    #: independent of the project-wide gate configuration.
    requires_human_approval: Mapped[bool] = mapped_column(default=False, nullable=False)

    template_id: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    template_version: Mapped[str] = mapped_column(String(20), default="", nullable=False)

    #: What could go wrong if this rung is wrong, in one or two sentences, for the
    #: engineer deciding how hard to look at it.
    risk_note: Mapped[str] = mapped_column(Text, default="", nullable=False)

    #: Denormalised from the operands, so "show me every rung referring to
    #: something that does not exist" does not require a join across the whole
    #: operand table. Maintained wherever operands are written.
    has_unresolved_operands: Mapped[bool] = mapped_column(default=False, nullable=False)

    routine: Mapped[Routine] = relationship(back_populates="rungs")
    logic_network: Mapped[LogicNetwork | None] = relationship(back_populates="rungs")
    instructions: Mapped[list[LogicInstruction]] = relationship(
        back_populates="rung",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="LogicInstruction.ordinal",
    )
    branches: Mapped[list[LogicBranch]] = relationship(
        back_populates="rung",
        cascade="all, delete-orphan",
        lazy="select",
        order_by="LogicBranch.ordinal",
    )
    comments: Mapped[list[LogicComment]] = relationship(
        back_populates="rung", cascade="all, delete-orphan", lazy="select"
    )


class LogicInstruction(UUIDPrimaryKeyMixin, EngineeringObjectMixin, TimestampMixin, Base):
    """One instruction within a rung.

    ``branch_level`` and ``branch_index`` place the instruction in the rung's
    branch structure; :class:`LogicBranch` describes that structure itself. Both
    are needed — the coordinates make rendering a flat pass, the tree makes the
    nesting checkable.

    The coordinates alone are not quite enough, which is what ``branch_id`` is
    for. They say *this instruction is on leg 1 of a level-2 branch* but not
    which leg of the level-1 branch that level-2 branch hangs off, so a nested
    branch opening on a non-zero leg of its parent has no way to say so and a
    renderer has to assume the leg it is already on. Naming the leg row removes
    the assumption.
    """

    __tablename__ = "logic_instructions"
    __table_args__ = (Index("ix_logic_instructions_rung_id_ordinal", "rung_id", "ordinal"),)

    rung_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("logic_rungs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: Position in the rung's flattened left-to-right order.
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)

    #: ``XIC``, ``OTE``, ``TON``, or the name of an Add-On Instruction.
    mnemonic: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    #: No default: an instruction whose category nobody determined should fail
    #: loudly at write time rather than arrive as a plausible-looking ``BIT``.
    category: Mapped[InstructionCategory] = mapped_column(nullable=False)
    element_kind: Mapped[LogicElementKind] = mapped_column(
        default=LogicElementKind.CONDITION, nullable=False
    )

    #: Nesting depth (0 = the main rung line) and which parallel leg at that
    #: depth this instruction sits on.
    branch_level: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    branch_index: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    #: The :class:`LogicBranch` leg this instruction sits on, which fixes the
    #: whole path from the main rung line rather than just the last step of it.
    #: Null on the main line, and null on every row written before this column
    #: existed — a reader must fall back to the coordinates rather than treat
    #: null as "the main line". Cascades: an instruction cannot outlive the leg
    #: it is on, and ``SET NULL`` would silently promote it to the main line,
    #: which is a different rung.
    branch_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("logic_branches.id", ondelete="CASCADE"), nullable=True, index=True
    )

    #: Destructive means the instruction *writes* its operand — the coil of an
    #: OTE, OTL or OTU, the destination of a MOV or a CPT. Two destructive
    #: references to the same tag from different rungs is a real defect (the
    #: last-scanned one wins, silently), and it is one of the few defects a
    #: validator can find with certainty. Storing it here is what makes that a
    #: group-by over :class:`LogicReference` rather than an instruction-by-
    #: instruction reading of the whole project.
    is_destructive: Mapped[bool] = mapped_column(default=False, nullable=False)

    #: Rendering of this instruction alone, e.g. ``XIC(Motor_Run_Cmd)``. Derived
    #: from the operands below, on the same terms as ``LogicRung.text``.
    text: Mapped[str] = mapped_column(String(300), default="", nullable=False)

    #: False when this platform can read the instruction but will not reason
    #: about or regenerate it. Recorded per instruction so an unsupported one
    #: degrades a single rung's confidence instead of the whole routine's.
    is_supported: Mapped[bool] = mapped_column(default=True, nullable=False)
    unsupported_reason: Mapped[str] = mapped_column(String(200), default="", nullable=False)

    rung: Mapped[LogicRung] = relationship(back_populates="instructions")
    operands: Mapped[list[LogicOperand]] = relationship(
        back_populates="instruction",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="LogicOperand.ordinal",
    )


class LogicOperand(UUIDPrimaryKeyMixin, EngineeringObjectMixin, TimestampMixin, Base):
    """One operand of one instruction.

    ``text`` is what the source wrote; ``tag_id`` and ``resolved_name`` are what
    we managed to make of it. Keeping both means a rename can be applied to the
    resolution without rewriting history, and an operand we failed to resolve
    still shows the engineer the exact string that failed.
    """

    __tablename__ = "logic_operands"
    __table_args__ = (
        Index("ix_logic_operands_instruction_id_ordinal", "instruction_id", "ordinal"),
    )

    instruction_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("logic_instructions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: Argument position, which is what gives an operand its meaning: MOV's
    #: second operand is the destination and its first is not.
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)

    #: No default: whether this is a tag, a literal, an expression or something
    #: we could not resolve is a determination, and defaulting it would make the
    #: unresolved case indistinguishable from the undetermined one.
    kind: Mapped[OperandKind] = mapped_column(nullable=False)

    #: The operand exactly as written, including array subscripts, bit selectors
    #: and structure members. Never normalised in place.
    text: Mapped[str] = mapped_column(String(200), default="", nullable=False)

    #: Null while unresolved, and ``SET NULL`` rather than cascade: deleting a tag
    #: must not delete logic that refers to it — it must make that logic visibly
    #: unresolved, which is the condition somebody needs to act on.
    tag_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("tags.id", ondelete="SET NULL"), nullable=True, index=True
    )
    #: The canonical tag name this operand resolved to, base name without
    #: subscripts, so cross-referencing does not have to re-parse ``text``.
    resolved_name: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    data_type_name: Mapped[str] = mapped_column(String(80), default="", nullable=False)

    #: True when this operand is the one the instruction writes.
    is_destination: Mapped[bool] = mapped_column(default=False, nullable=False)

    #: Set whenever ``kind`` is ``UNRESOLVED``. An unresolved operand with no
    #: stated reason is worse than useless: it tells an engineer something is
    #: wrong and gives them nothing to act on, so it costs review time and buys
    #: nothing. "no tag by that name in controller scope" and "operand is an
    #: expression this parser does not support" lead to entirely different next
    #: steps, and only the writer of the row knows which one applies.
    unresolved_reason: Mapped[str] = mapped_column(String(200), default="", nullable=False)

    instruction: Mapped[LogicInstruction] = relationship(back_populates="operands")

    @property
    def states_why_unresolved(self) -> bool:
        """Whether an unresolved operand carries the reason it is unresolved."""
        if self.kind is not OperandKind.UNRESOLVED:
            return True
        return bool(self.unresolved_reason)


class LogicBranch(UUIDPrimaryKeyMixin, EngineeringObjectMixin, TimestampMixin, Base):
    """One branch, or one leg of one branch, within a rung.

    A flat instruction list cannot express parallel legs unambiguously: given
    ``XIC(A) XIC(B) OTE(C)`` there is no way to tell whether A and B are in
    series or on two legs of a branch, and those are different machines. The
    branch rows carry that structure explicitly so rendering is a traversal
    rather than a reconstruction, and so a malformed branch is a validation
    failure rather than a rendering that happens to look plausible.

    Nesting is by ``parent_branch_id``; ``level`` and ``leg_index`` mirror the
    coordinates on :class:`LogicInstruction` so the two views can be checked
    against each other.
    """

    __tablename__ = "logic_branches"
    __table_args__ = (Index("ix_logic_branches_rung_id_ordinal", "rung_id", "ordinal"),)

    rung_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("logic_rungs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: Null for a branch on the main rung line. Cascades rather than nulling on
    #: delete: a nested leg cannot outlive the branch it hangs off, and ``SET
    #: NULL`` would quietly promote it to the top level — which is a different
    #: rung, not a tidier one.
    parent_branch_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("logic_branches.id", ondelete="CASCADE"), nullable=True, index=True
    )

    level: Mapped[int] = mapped_column(Integer, nullable=False)
    #: Which parallel leg this is, counting from the top of the branch.
    leg_index: Mapped[int] = mapped_column(Integer, nullable=False)
    #: Left-to-right position of the branch within its parent.
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)

    description: Mapped[str] = mapped_column(String(200), default="", nullable=False)

    rung: Mapped[LogicRung] = relationship(back_populates="branches")
    parent: Mapped[LogicBranch | None] = relationship(
        back_populates="children", remote_side="LogicBranch.id", lazy="select"
    )
    children: Mapped[list[LogicBranch]] = relationship(
        back_populates="parent", lazy="select", order_by="LogicBranch.leg_index"
    )


class LogicComment(UUIDPrimaryKeyMixin, EngineeringObjectMixin, TimestampMixin, Base):
    """A comment attached to an operand or an instruction rather than to a rung.

    Logix keeps these separately from rung comments and they survive different
    edits, so folding them into ``LogicRung.comment`` would lose which text
    belonged to which element — and operand comments are frequently the only
    written statement of what a bit means.

    ``operand_text`` matches by the string as written rather than by operand id,
    because a comment attaches to an address and outlives the particular operand
    row that referenced it.
    """

    __tablename__ = "logic_comments"
    __table_args__ = (Index("ix_logic_comments_rung_id_operand_text", "rung_id", "operand_text"),)

    rung_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("logic_rungs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    instruction_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("logic_instructions.id", ondelete="SET NULL"), nullable=True, index=True
    )

    operand_text: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    #: Logix stores comments per language; the export carries the code and we
    #: keep it, so a multi-language project does not silently lose one.
    language_code: Mapped[str] = mapped_column(String(10), default="en-US", nullable=False)
    text: Mapped[str] = mapped_column(Text, default="", nullable=False)

    rung: Mapped[LogicRung] = relationship(back_populates="comments")


class LogicReference(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One cross-reference row: which rung touches which tag, and how.

    Not an engineering object — it is derived, rebuilt wholesale by the
    cross-reference pass, and nobody approves or supersedes a cross-reference.

    It exists because the questions that matter about a controller are questions
    about the whole project, not about one rung: "what else drives this coil",
    "who reads this permissive", "is this routine ever called". Answering those
    by walking the instruction and operand tables is possible and nobody does it
    routinely; answering them with a filter on this table is what makes the
    validator's checks cheap enough to run on every merge.
    """

    __tablename__ = "logic_references"
    __table_args__ = (Index("ix_logic_references_project_id_tag_name", "project_id", "tag_name"),)

    project_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )

    rung_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("logic_rungs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    instruction_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("logic_instructions.id", ondelete="SET NULL"), nullable=True, index=True
    )
    tag_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("tags.id", ondelete="SET NULL"), nullable=True, index=True
    )

    #: Kept even when ``tag_id`` is null, and it is the indexed column rather
    #: than the id. An unresolved reference is exactly the one worth
    #: cross-referencing — "seventeen rungs read a tag that does not exist" is
    #: only answerable if the name survives the failure to resolve it. It also
    #: keeps the row meaningful after the tag it pointed at is deleted.
    tag_name: Mapped[str] = mapped_column(String(120), nullable=False)

    #: True when the referencing instruction writes this tag. Duplicate
    #: destructive references to one tag are the defect this table is built to
    #: expose.
    is_destructive: Mapped[bool] = mapped_column(default=False, nullable=False)

    #: Denormalised location, so a cross-reference report reads without joining
    #: back up through rung, routine and program.
    routine_name: Mapped[str] = mapped_column(String(60), default="", nullable=False)
    program_name: Mapped[str] = mapped_column(String(60), default="", nullable=False)

    #: ``read`` | ``write`` | ``jsr_target`` and similar. A plain string rather
    #: than an enum: there is no enum for this in the published vocabulary, and
    #: the set grows with every instruction category the cross-reference pass
    #: learns to walk, which is the wrong reason to change a contract.
    reference_kind: Mapped[str] = mapped_column(String(24), default="", nullable=False)
