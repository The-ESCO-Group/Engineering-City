"""The shapes every validator shares, and the loaded model it runs against.

Three things live here:

* :class:`Issue` — what a check found, written for an engineer rather than for a
  log. It is deliberately not a :class:`~app.models.release.ValidationResult`:
  a validator produces findings, and whether a run persists them, and against
  which run row, is not a validator's business.
* :class:`Validator` — the protocol a check satisfies. Four members, none of them
  a base class to inherit, so a validator is a small object that can be built and
  called in a test without a database session or a run.
* :class:`ValidationContext` — the project as the validators see it. Every
  collection is loaded once, lazily, and shared, because twenty validators each
  re-querying the tag table turns a release gate into something nobody runs.

**This module assembles facts. It does not decide what they mean.** A tag with no
description is recorded as a tag with no description; whether that stops a
release is the severity a validator assigns, and severity is argued for at the
point it is chosen rather than here.

Names are matched the way Logix matches them: case-insensitively, and with
structure preserved. ``normalise_tag_name`` from the parser layer is deliberately
*not* used — it folds ``.`` and ``[`` into spaces, which is right for comparing a
spreadsheet cell against an export and wrong for deciding whether
``Motor.Cmd`` and ``Motor_Cmd`` are the same operand.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from functools import cached_property
from typing import Any, Protocol, TypeVar

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.enums import (
    ObjectType,
    ValidationDiscipline,
    ValidationSeverity,
)
from app.generators.plc.instructions import destructive_operands
from app.models.core import EngineeringObjectMixin, ObjectEvidenceLink
from app.models.delivery import AlarmDefinition, HMIBinding, HMIScreen, TestCase, TestStep
from app.models.interlocks import (
    FaultDefinition,
    Interlock,
    ModeDefinition,
    Permissive,
    ResetBehavior,
    StateDefinition,
    TransitionDefinition,
)
from app.models.plant import AddressAssignment, Device, HardwareModule, IOPoint, Signal
from app.models.plc import (
    LogicInstruction,
    LogicOperand,
    LogicReference,
    LogicRung,
    Program,
    Routine,
)
from app.models.profile import ProjectProfile, ProjectQuestion, TargetPlatform
from app.models.project import Project
from app.models.release import ReleaseCandidate
from app.models.requirements import Clarification, Requirement
from app.models.tags import AOIDefinitionReference, Tag, TagAlias, UDTDefinition

__all__ = [
    "MAX_OBJECT_LABEL_CHARS",
    "DestructiveWrite",
    "Issue",
    "ValidationContext",
    "Validator",
    "base_name",
    "fold",
    "label",
    "references",
]

#: Matches ``ValidationResult.object_label``. Bounded here so a validator can
#: compose a label without remembering the column width, and so a truncated
#: label is truncated in one place rather than by the database driver.
MAX_OBJECT_LABEL_CHARS = 200


def fold(name: str) -> str:
    """Case-fold an identifier the way Logix compares them.

    Structure is preserved: ``Motor.Cmd`` stays ``motor.cmd`` and does not become
    ``motor cmd``. Two operands are the same operand only if they are written the
    same way apart from case.
    """
    return name.strip().lower()


def base_name(name: str) -> str:
    """The tag an operand ultimately names, without member or subscript.

    ``CV101_Status.Running`` and ``Buffer[3]`` are references to ``CV101_Status``
    and ``Buffer``. A check that compares whole operand strings would report a
    declared UDT's every member as an undeclared tag, which is the false-positive
    class that makes a validator worthless.
    """
    return fold(name).split(".", 1)[0].split("[", 1)[0]


def references(operand_text: str, tag_name: str) -> bool:
    """Whether ``operand_text`` reaches ``tag_name``.

    True for the tag itself, for one of its members and for one of its elements.
    Writing ``Motor.Cmd`` is writing something inside ``Motor``, and a check that
    said otherwise would report a driven output as undriven.
    """
    reference, target = fold(operand_text), fold(tag_name)
    if not reference or not target:
        return False
    return (
        reference == target
        or reference.startswith(f"{target}.")
        or reference.startswith(f"{target}[")
    )


def label(text: str) -> str:
    """Bound a composed object label to what the column will hold."""
    cleaned = " ".join(text.split())
    if len(cleaned) <= MAX_OBJECT_LABEL_CHARS:
        return cleaned
    return cleaned[: MAX_OBJECT_LABEL_CHARS - 1] + "…"


@dataclass(frozen=True, slots=True)
class Issue:
    """One thing a validator found.

    Frozen because an issue is an observation: a caller that wants a different
    severity has to produce a different issue, with its own argument for the
    change, rather than editing one in flight.
    """

    #: Stable, discipline-namespaced, e.g. ``logic.duplicate_destructive_output``.
    #: Published API — see the package docstring for the rule.
    code: str
    discipline: ValidationDiscipline
    severity: ValidationSeverity
    #: Shown to an engineer verbatim. Not a template key and not a log line.
    message: str

    #: What the issue is about. Both ``None`` for a project-wide finding.
    object_type: ObjectType | None = None
    object_id: uuid.UUID | None = None
    #: Where to look, in the words the engineer uses, e.g.
    #: ``MainProgram / Conveyor_Interlocks / Rung 12``.
    object_label: str = ""

    #: The concrete next step, not a restatement of the problem.
    suggested_action: str = ""
    #: Machine-readable specifics — the two conflicting addresses, the operand
    #: that would not resolve, the limit that was exceeded.
    detail: dict[str, Any] = field(default_factory=dict)

    #: Set when no amount of further checking can settle this and a qualified
    #: person has to choose.
    requires_human_decision: bool = False


class Validator(Protocol):
    """One coherent group of checks over the canonical model.

    A protocol rather than a base class, so a validator owns no machinery: it is
    constructed with nothing, holds no session, and ``check`` is a pure function
    of the context it is handed. That is what makes a check testable without a
    run, and what makes a run able to survive one of them raising.
    """

    #: Stable identifier for the group, e.g. ``logic.destructive_writes``.
    #: Reported on ``validator.failed`` so a broken check can be named.
    key: str
    discipline: ValidationDiscipline
    #: One sentence: what this group checks, in an engineer's terms.
    description: str

    def check(self, context: ValidationContext) -> Iterable[Issue]:
        """Yield everything this group found. Must not write to the database."""
        ...


@dataclass(frozen=True, slots=True)
class DestructiveWrite:
    """One instruction position that writes one tag.

    Assembled once, on the context, because three separate checks need it —
    duplicate destinations, undriven outputs and unlatched latches — and each
    walking the operand table independently is both slow and a place for the
    three to disagree about what "writes" means.
    """

    tag_name: str
    #: Operand exactly as written.  ``resolved_name`` is deliberately the base
    #: declaration (``Buffer`` for ``Buffer[5]``), which is right for output
    #: coverage but not enough to distinguish two array elements.  Duplicate
    #: and range checks therefore use this lossless spelling.
    operand_text: str
    mnemonic: str
    rung_id: uuid.UUID
    rung_label: str
    operand_ordinal: int
    #: True when the catalogue named the position, False when it was taken from
    #: ``LogicOperand.is_destination``. The distinction matters: the second is
    #: whatever the parser concluded about an instruction we do not describe.
    from_catalogue: bool


_ModelT = TypeVar("_ModelT", bound=EngineeringObjectMixin)


@dataclass
class ValidationContext:
    """The project the validators run against, loaded once.

    Deliberately not a snapshot object with an independent life: it holds the
    live session and the collections it has been asked for so far. Nothing here
    is written, and a validator that mutates a loaded row is a bug — a validation
    run is a statement *about* a revision, and one that edited the thing it was
    measuring would be describing a model that never existed.
    """

    db: Session
    project: Project
    #: The project revision this run is a statement about. Recorded rather than
    #: read back from the project at report time, so a later merge cannot make an
    #: old run look like it examined new content.
    revision_number: int = 0

    # -- generic loaders ---------------------------------------------------
    def _live(self, model: type[_ModelT]) -> list[_ModelT]:
        """Every undeleted row of ``model`` in this project, in creation order.

        Soft-deleted rows are excluded because a deleted object is not part of
        the model any more; its row survives only so a citation resolves to a
        tombstone.
        """
        statement = (
            select(model)
            .where(
                model.project_id == self.project.id,
                model.deleted_at.is_(None),
            )
            .order_by(model.created_at, model.id)  # type: ignore[attr-defined]
        )
        return list(self.db.execute(statement).scalars())

    # -- requirements ------------------------------------------------------
    @cached_property
    def requirements(self) -> list[Requirement]:
        return self._live(Requirement)

    @cached_property
    def clarifications(self) -> list[Clarification]:
        return self._live(Clarification)

    @cached_property
    def questions(self) -> list[ProjectQuestion]:
        return self._live(ProjectQuestion)

    @cached_property
    def profile(self) -> ProjectProfile | None:
        rows = self._live(ProjectProfile)
        return rows[0] if rows else None

    @cached_property
    def target_platform(self) -> TargetPlatform | None:
        rows = self._live(TargetPlatform)
        return rows[0] if rows else None

    @cached_property
    def evidence_links(self) -> list[ObjectEvidenceLink]:
        statement = (
            select(ObjectEvidenceLink)
            .where(ObjectEvidenceLink.project_id == self.project.id)
            .order_by(ObjectEvidenceLink.created_at, ObjectEvidenceLink.id)
        )
        return list(self.db.execute(statement).scalars())

    @cached_property
    def evidence_by_object(self) -> dict[uuid.UUID, list[ObjectEvidenceLink]]:
        grouped: dict[uuid.UUID, list[ObjectEvidenceLink]] = {}
        for link in self.evidence_links:
            grouped.setdefault(link.object_id, []).append(link)
        return grouped

    # -- tags --------------------------------------------------------------
    @cached_property
    def tags(self) -> list[Tag]:
        return self._live(Tag)

    @cached_property
    def tags_by_folded_name(self) -> dict[str, list[Tag]]:
        """Every tag under its case-folded name.

        A list rather than a single tag: the same name legitimately exists
        controller-scoped and once per program, and collapsing that would hide
        the scope collision the tag validator reports.
        """
        grouped: dict[str, list[Tag]] = {}
        for tag in self.tags:
            grouped.setdefault(fold(tag.name), []).append(tag)
        return grouped

    @cached_property
    def tags_by_id(self) -> dict[uuid.UUID, Tag]:
        return {tag.id: tag for tag in self.tags}

    @cached_property
    def tag_aliases(self) -> list[TagAlias]:
        return self._live(TagAlias)

    @cached_property
    def udt_definitions(self) -> list[UDTDefinition]:
        return self._live(UDTDefinition)

    @cached_property
    def aoi_references(self) -> list[AOIDefinitionReference]:
        return self._live(AOIDefinitionReference)

    # -- plant and I/O -----------------------------------------------------
    @cached_property
    def devices(self) -> list[Device]:
        return self._live(Device)

    @cached_property
    def signals(self) -> list[Signal]:
        return self._live(Signal)

    @cached_property
    def signals_by_device(self) -> dict[uuid.UUID, list[Signal]]:
        grouped: dict[uuid.UUID, list[Signal]] = {}
        for signal in self.signals:
            grouped.setdefault(signal.device_id, []).append(signal)
        return grouped

    @cached_property
    def address_assignments(self) -> list[AddressAssignment]:
        return self._live(AddressAssignment)

    @cached_property
    def assignments_by_signal(self) -> dict[uuid.UUID, list[AddressAssignment]]:
        grouped: dict[uuid.UUID, list[AddressAssignment]] = {}
        for assignment in self.address_assignments:
            grouped.setdefault(assignment.signal_id, []).append(assignment)
        return grouped

    @cached_property
    def io_points(self) -> list[IOPoint]:
        return self._live(IOPoint)

    @cached_property
    def io_points_by_id(self) -> dict[uuid.UUID, IOPoint]:
        return {point.id: point for point in self.io_points}

    @cached_property
    def hardware_modules(self) -> list[HardwareModule]:
        return self._live(HardwareModule)

    @cached_property
    def devices_by_id(self) -> dict[uuid.UUID, Device]:
        return {device.id: device for device in self.devices}

    @cached_property
    def signals_by_id(self) -> dict[uuid.UUID, Signal]:
        return {signal.id: signal for signal in self.signals}

    # -- PLC organisation and logic ---------------------------------------
    @cached_property
    def programs(self) -> list[Program]:
        return self._live(Program)

    @cached_property
    def programs_by_id(self) -> dict[uuid.UUID, Program]:
        return {program.id: program for program in self.programs}

    @cached_property
    def routines(self) -> list[Routine]:
        return self._live(Routine)

    @cached_property
    def routines_by_id(self) -> dict[uuid.UUID, Routine]:
        return {routine.id: routine for routine in self.routines}

    @cached_property
    def rungs(self) -> list[LogicRung]:
        return self._live(LogicRung)

    @cached_property
    def rungs_by_id(self) -> dict[uuid.UUID, LogicRung]:
        return {rung.id: rung for rung in self.rungs}

    @cached_property
    def instructions(self) -> list[LogicInstruction]:
        return self._live(LogicInstruction)

    @cached_property
    def instructions_by_rung(self) -> dict[uuid.UUID, list[LogicInstruction]]:
        grouped: dict[uuid.UUID, list[LogicInstruction]] = {}
        for instruction in self.instructions:
            grouped.setdefault(instruction.rung_id, []).append(instruction)
        for items in grouped.values():
            items.sort(key=lambda item: item.ordinal)
        return grouped

    @cached_property
    def operands(self) -> list[LogicOperand]:
        return self._live(LogicOperand)

    @cached_property
    def operands_by_instruction(self) -> dict[uuid.UUID, list[LogicOperand]]:
        grouped: dict[uuid.UUID, list[LogicOperand]] = {}
        for operand in self.operands:
            grouped.setdefault(operand.instruction_id, []).append(operand)
        for items in grouped.values():
            items.sort(key=lambda item: item.ordinal)
        return grouped

    @cached_property
    def logic_references(self) -> list[LogicReference]:
        """The cross-reference rows, which are derived and may not exist.

        Never used as the authority for what logic does — the instruction and
        operand rows are that. It is here so a validator can tell whether the
        cross-reference pass has run at all, which is the difference between
        "this tag is unused" and "nobody has looked".
        """
        statement = (
            select(LogicReference)
            .where(LogicReference.project_id == self.project.id)
            .order_by(LogicReference.created_at, LogicReference.id)
        )
        return list(self.db.execute(statement).scalars())

    # -- interlocks, modes, states ----------------------------------------
    @cached_property
    def interlocks(self) -> list[Interlock]:
        return self._live(Interlock)

    @cached_property
    def permissives(self) -> list[Permissive]:
        return self._live(Permissive)

    @cached_property
    def modes(self) -> list[ModeDefinition]:
        return self._live(ModeDefinition)

    @cached_property
    def states(self) -> list[StateDefinition]:
        return self._live(StateDefinition)

    @cached_property
    def transitions(self) -> list[TransitionDefinition]:
        return self._live(TransitionDefinition)

    @cached_property
    def fault_definitions(self) -> list[FaultDefinition]:
        return self._live(FaultDefinition)

    @cached_property
    def reset_behaviors(self) -> list[ResetBehavior]:
        return self._live(ResetBehavior)

    @cached_property
    def reset_behaviors_by_id(self) -> dict[uuid.UUID, ResetBehavior]:
        return {behaviour.id: behaviour for behaviour in self.reset_behaviors}

    # -- delivery ----------------------------------------------------------
    @cached_property
    def alarms(self) -> list[AlarmDefinition]:
        return self._live(AlarmDefinition)

    @cached_property
    def hmi_screens(self) -> list[HMIScreen]:
        return self._live(HMIScreen)

    @cached_property
    def hmi_screens_by_id(self) -> dict[uuid.UUID, HMIScreen]:
        return {screen.id: screen for screen in self.hmi_screens}

    @cached_property
    def hmi_bindings(self) -> list[HMIBinding]:
        return self._live(HMIBinding)

    @cached_property
    def test_cases(self) -> list[TestCase]:
        return self._live(TestCase)

    @cached_property
    def test_steps(self) -> list[TestStep]:
        return self._live(TestStep)

    @cached_property
    def release_candidates(self) -> list[ReleaseCandidate]:
        return self._live(ReleaseCandidate)

    # -- derived views over the logic IR ----------------------------------
    @cached_property
    def rung_labels(self) -> dict[uuid.UUID, str]:
        """``Program / Routine / Rung N`` for every rung, composed once.

        Denormalised here rather than at each call site because a result nobody
        can locate is not actionable, and every logic issue needs one.
        """
        labels: dict[uuid.UUID, str] = {}
        for rung in self.rungs:
            routine = self.routines_by_id.get(rung.routine_id)
            routine_name = routine.name if routine else "unknown routine"
            program_name = "unknown program"
            if routine is not None:
                program = self.programs_by_id.get(routine.program_id)
                if program is not None:
                    program_name = program.name
            labels[rung.id] = label(f"{program_name} / {routine_name} / Rung {rung.ordinal}")
        return labels

    def rung_label(self, rung_id: uuid.UUID) -> str:
        return self.rung_labels.get(rung_id, "unknown rung")

    @staticmethod
    def operand_name(operand: LogicOperand) -> str:
        """The name an operand refers to, preferring the resolved form.

        ``resolved_name`` is the canonical tag this operand was matched to;
        ``text`` is what the source wrote. Falling back to the text keeps an
        unresolved operand nameable, which is the whole point of storing both.
        """
        return operand.resolved_name or operand.text

    @cached_property
    def destructive_writes(self) -> list[DestructiveWrite]:
        """Every position in the project's ladder that writes a tag.

        Two sources, and both are needed. The catalogue names the destructive
        operand positions of the instructions this platform describes, which is
        the only trustworthy answer for those. For an instruction it does not
        describe — an Add-On Instruction, above all — the catalogue answers
        ``()``, and that is "nothing to say" rather than "writes nothing"; the
        parser's ``LogicOperand.is_destination`` is then the best available
        statement, and the instruction is separately reported as unsupported so
        the gap is visible rather than assumed away.
        """
        writes: list[DestructiveWrite] = []
        for instruction in self.instructions:
            catalogued = set(destructive_operands(instruction.mnemonic))
            for operand in self.operands_by_instruction.get(instruction.id, ()):
                by_catalogue = operand.ordinal in catalogued
                if not by_catalogue and not operand.is_destination:
                    continue
                name = self.operand_name(operand)
                if not name:
                    continue
                writes.append(
                    DestructiveWrite(
                        tag_name=name,
                        operand_text=operand.text.strip() or name,
                        mnemonic=instruction.mnemonic.upper(),
                        rung_id=instruction.rung_id,
                        rung_label=self.rung_label(instruction.rung_id),
                        operand_ordinal=operand.ordinal,
                        from_catalogue=by_catalogue,
                    )
                )
        return writes

    @cached_property
    def written_names(self) -> frozenset[str]:
        """Case-folded operand strings that some instruction writes."""
        return frozenset(fold(write.tag_name) for write in self.destructive_writes)

    @cached_property
    def referenced_names(self) -> frozenset[str]:
        """Case-folded operand strings that appear anywhere in the ladder."""
        names: set[str] = set()
        for operand in self.operands:
            name = self.operand_name(operand)
            if name:
                names.add(fold(name))
        return frozenset(names)

    @cached_property
    def read_names(self) -> frozenset[str]:
        """Operand strings that are referenced somewhere other than as a destination."""
        return self.referenced_names - self.written_names

    def is_written(self, tag_name: str) -> bool:
        """Whether any instruction writes this tag, or a member or element of it."""
        return any(references(name, tag_name) for name in self.written_names)

    def is_referenced(self, tag_name: str) -> bool:
        """Whether the ladder mentions this tag, a member of it, or an element of it."""
        return any(references(name, tag_name) for name in self.referenced_names)

    @cached_property
    def has_ladder_logic(self) -> bool:
        """Whether there is any ladder to check at all.

        Checks that ask "does anything drive this" are meaningless against a
        project with no rungs — every output would be reported as undriven, which
        is true, useless, and would bury the findings that matter.
        """
        return bool(self.rungs)
