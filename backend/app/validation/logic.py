"""PLC-logic checks over the ladder IR.

Codes in this module are namespaced ``logic.*`` and are published API — see the
package docstring.

Everything here reads the structured rows — rungs, instructions, operands — and
never ``LogicRung.text``. The text is a rendering of those rows and nothing parses
it back; a validator that read it would be checking a string the generator
produced rather than the model the generator will produce from.

Instruction knowledge comes from :mod:`app.generators.plc.instructions` and is not
re-derived here. That matters most for the destructive positions: that ``MOV``
writes its second operand and ``CPT`` its first is exactly the sort of asymmetry a
second implementation would quietly "tidy up", and the duplicate-destination check
would then stop finding the defect it exists for.

Where the catalogue does not describe an instruction it answers ``()``, which is
"nothing to say" and not "writes nothing". Every such mnemonic is reported as
``UNSUPPORTED`` by name, so a rung this platform could not reason about is visible
in the record rather than indistinguishable from one that was checked and found
clean.

An empty tuple is not the only way the catalogue can under-state a write, and
this module reads
:attr:`~app.generators.plc.instructions.InstructionSpec.destination_coverage`
rather than the tuple alone:

* ``JSR`` and ``SBR`` write positions that depend on their own operand list, so
  their fixed tuple is empty. :func:`~app.generators.plc.instructions.resolved_destructive_operands`
  works the real positions out and :func:`operand_dependent_writes` turns them
  into the writes the checks here group over. Without that, a ``JSR`` return
  parameter is a tag nothing writes: missed as a duplicate destination and
  reported as an undriven output.
* ``COP``, ``CPS`` and ``FLL`` name the *first element* of a destination range.
  Two writers of ``Buffer[0]`` and ``Buffer[5]`` are different group keys and
  the collision between a 20-element copy and a ``MOV`` is invisible to a check
  that compares operand text. :class:`DestructiveWriteValidator` expands the
  range when the start and the length are both literals, and says it could not
  when they are not.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass

from app.core.enums import (
    AlarmSeverity,
    ControllerFamily,
    ObjectType,
    OperandKind,
    ResetBehaviorKind,
    RoutineLanguage,
    SignalDirection,
    SignalType,
    ValidationDiscipline,
    ValidationSeverity,
)
from app.generators.plc.instructions import (
    DestinationCoverage,
    is_known,
    is_restricted,
    resolved_destructive_operands,
    restriction_reason,
    spec_for,
    supports,
)
from app.models.plc import LogicInstruction
from app.validation.base import (
    DestructiveWrite,
    Issue,
    ValidationContext,
    base_name,
    fold,
    label,
    references,
)

__all__ = [
    "DestructiveWriteValidator",
    "InstructionSupportValidator",
    "InterlockImplementationValidator",
    "ModeLogicValidator",
    "OperandValidator",
    "OutputCoverageValidator",
    "ResetBehaviourValidator",
    "RoutineCallValidator",
    "SequenceStateValidator",
    "all_destructive_writes",
    "operand_dependent_writes",
]

_DISCIPLINE = ValidationDiscipline.PLC_LOGIC

#: Operand positions that name something other than a tag, per the instruction
#: catalogue's own notes. The L5X parser has no ``<Tags>`` entry to resolve a
#: label, a routine name or an SFC step against, so it reports them unresolved.
#: Reporting those would be a confident error against correct logic — and worse,
#: it invites somebody to "fix" it by inventing a tag.
_NON_TAG_OPERAND_POSITIONS: dict[str, frozenset[int]] = {
    "JSR": frozenset({0}),
    # The L5X parser treats JXR's first operand as a routine target exactly as
    # it treats JSR's (``_ROUTINE_TARGET_MNEMONICS``). An external routine is
    # not declared in this project, so the operand always arrives unresolved.
    "JXR": frozenset({0}),
    "JMP": frozenset({0}),
    "LBL": frozenset({0}),
    "SFR": frozenset({0, 1}),
    "SFP": frozenset({0, 1}),
    "FOR": frozenset({0}),
    "EVENT": frozenset({0}),
    # GSV and SSV address a CIP object: class name, instance name, attribute
    # name. None of the three is a tag, and the instance is routinely written
    # empty — ``GSV(WallClockTime,,LocalDateTime,Now[0])``.
    "GSV": frozenset({0, 1, 2}),
    "SSV": frozenset({0, 1, 2}),
    # Keyword operands: SWPB's byte order, and the mode of the file
    # instructions. Written as words rather than as tags or numbers.
    "SWPB": frozenset({1}),
    "FAL": frozenset({3}),
    "FSC": frozenset({3}),
}

#: Mnemonic combinations that write one tag on purpose. A latch needs its
#: unlatch, a counter needs its reset, and an up-counter paired with a
#: down-counter is a design rather than a collision. Each set is only accepted
#: when the writers are all *different* instructions: two ``OTL`` on one bit is
#: still a duplicate destination.
_COMPLEMENTARY_WRITER_SETS: tuple[frozenset[str], ...] = (
    frozenset({"OTL", "OTU"}),
    frozenset({"CTU", "CTD", "RES"}),
    frozenset({"TON", "RES"}),
    frozenset({"TOF", "RES"}),
    frozenset({"RTO", "RES"}),
    # A queue is loaded by one instruction and unloaded by another, and the two
    # share the CONTROL structure and the array by design. Reporting the pair
    # would be a confident error against the only correct way to write a FIFO.
    frozenset({"FFL", "FFU", "RES"}),
    frozenset({"LFL", "LFU", "RES"}),
    # A sequencer's input, output and load steps share one CONTROL for the same
    # reason: they are one sequencer, stepped together.
    frozenset({"SQI", "SQO", "SQL", "RES"}),
    frozenset({"BSL", "RES"}),
    frozenset({"BSR", "RES"}),
    frozenset({"FAL", "RES"}),
    frozenset({"FSC", "RES"}),
)

#: Instructions that write only part of their destination. Two of them on one
#: tag conflict if and only if their masks overlap, and this validator does not
#: decode masks — so it reports the pair and says why it cannot decide.
_MASKED_WRITERS = frozenset({"MVM", "BTD"})

#: Instructions whose destination operand names the *first element* of a range,
#: mapped to the operand that gives the range's length. The catalogue records
#: these as :attr:`DestinationCoverage.PARTIAL`; this table is what turns that
#: into a check, because the length is an operand and its position is per
#: mnemonic. ``FAL`` is deliberately absent: its extent depends on the mode
#: operand as well as the length, and this module does not decode it.
_RANGE_WRITER_LENGTH_OPERANDS: dict[str, int] = {
    "COP": 2,
    "CPS": 2,
    "FLL": 2,
}

#: Signal types that reach a field output.
_OUTPUT_SIGNAL_TYPES = frozenset(
    {SignalType.DISCRETE_OUTPUT, SignalType.ANALOG_OUTPUT, SignalType.NETWORK_OUTPUT}
)


# ---------------------------------------------------------------------------
# Writes the catalogue's fixed tuples cannot state on their own
# ---------------------------------------------------------------------------
def operand_dependent_writes(context: ValidationContext) -> list[DestructiveWrite]:
    """Writes by instructions whose destinations depend on their own operands.

    ``JSR``'s return parameters and ``SBR``'s parameter list. Both are recorded
    in the catalogue with an empty destructive tuple because no fixed tuple can
    name them, so
    :attr:`~app.validation.base.ValidationContext.destructive_writes` — which
    reads that tuple — leaves them out. Nothing else puts them back:
    ``LogicOperand.is_destination`` is never populated anywhere in the backend.

    Returned separately rather than folded into the context so this module owns
    the special case, and so a position already recorded by either mechanism is
    not counted twice.
    """
    writes: list[DestructiveWrite] = []
    for instruction in context.instructions:
        spec = spec_for(instruction.mnemonic)
        if spec is None or spec.destination_coverage is not DestinationCoverage.OPERAND_DEPENDENT:
            continue
        operands = context.operands_by_instruction.get(instruction.id, ())
        if [operand.ordinal for operand in operands] != list(range(len(operands))):
            # A gap in the ordinals means the operand list is not the one the
            # instruction was written with, and a position counted off it would
            # name the wrong operand. Reported as unsupported elsewhere.
            continue
        resolved = set(
            resolved_destructive_operands(spec.mnemonic, [item.text for item in operands])
        )
        already = set(spec.destructive_operand_indexes)
        for operand in operands:
            if operand.ordinal not in resolved or operand.ordinal in already:
                continue
            if operand.is_destination:
                # Already counted by ``destructive_writes``.
                continue
            name = context.operand_name(operand)
            if not name:
                continue
            writes.append(
                DestructiveWrite(
                    tag_name=name,
                    mnemonic=spec.mnemonic,
                    rung_id=instruction.rung_id,
                    rung_label=context.rung_label(instruction.rung_id),
                    operand_ordinal=operand.ordinal,
                    from_catalogue=True,
                )
            )
    return writes


def all_destructive_writes(context: ValidationContext) -> list[DestructiveWrite]:
    """Every write in the project's ladder this module can establish.

    The context's own list plus :func:`operand_dependent_writes`. Every check
    here uses this rather than the context's list directly, so "which tags does
    the ladder write" has one answer inside this module.
    """
    return [*context.destructive_writes, *operand_dependent_writes(context)]


def _subscript_index(operand_text: str) -> int | None:
    """The literal element index of ``Buffer[7]``, or ``None``.

    ``None`` for anything that is not a single integer subscript — a variable
    index ``Buffer[i]``, a multi-dimensional ``Buffer[1,2]``, a member path.
    An array reference with no subscript at all starts at element 0, which is
    what Logix does, so that answers ``0``.
    """
    text = operand_text.strip()
    if "." in text.split("[", 1)[0] or text.endswith("."):
        return None
    if "[" not in text:
        return 0
    if not text.endswith("]"):
        return None
    inner = text[text.index("[") + 1 : -1].strip()
    if not inner.isdigit():
        return None
    return int(inner)


def _literal_count(operand_text: str) -> int | None:
    """A plain non-negative integer operand, or ``None`` when it is not one."""
    text = operand_text.strip()
    return int(text) if text.isdigit() else None


class OperandValidator:
    """Operands that name something the model does not hold."""

    key = "logic.operands"
    discipline = _DISCIPLINE
    description = "Rung operands that could not be resolved to a tag."

    def check(self, context: ValidationContext) -> Iterable[Issue]:
        issues: list[Issue] = []
        instructions = {instruction.id: instruction for instruction in context.instructions}

        for operand in context.operands:
            if operand.kind is not OperandKind.UNRESOLVED:
                continue
            instruction = instructions.get(operand.instruction_id)
            if instruction is None:
                continue
            mnemonic = instruction.mnemonic.upper()
            if operand.ordinal in _NON_TAG_OPERAND_POSITIONS.get(mnemonic, frozenset()):
                continue

            reason = operand.unresolved_reason.strip()
            location = context.rung_label(instruction.rung_id)
            issues.append(
                Issue(
                    code="logic.unresolved_operand",
                    discipline=_DISCIPLINE,
                    # ERROR: the rung references something that does not exist.
                    # Exported as it stands, the instruction addresses nothing
                    # and Studio 5000 refuses the import or creates the tag
                    # undefined — either way the logic does not do what it reads
                    # as doing.
                    severity=ValidationSeverity.ERROR,
                    message=(
                        f"{mnemonic} operand {operand.ordinal} '{operand.text}' does not "
                        "resolve to any tag."
                        + (
                            f" {reason}"
                            if reason
                            else " No reason for the failure was recorded, so there is "
                            "nothing here to act on beyond the operand text."
                        )
                    ),
                    object_type=ObjectType.LOGIC_RUNG,
                    object_id=instruction.rung_id,
                    object_label=location,
                    suggested_action=(
                        "Declare the tag the operand names, or correct the operand to the "
                        "tag that was meant. Do not create a tag to silence the message "
                        "without establishing which signal it is."
                    ),
                    detail={
                        "mnemonic": mnemonic,
                        "operand_ordinal": operand.ordinal,
                        "operand_text": operand.text,
                        "unresolved_reason": reason,
                    },
                )
            )
        return issues


@dataclass(frozen=True, slots=True)
class _RangeWrite:
    """A ``COP``/``CPS``/``FLL`` destination, with the extent it covers.

    The destructive tuple names ``Buffer[0]``; the instruction writes
    ``Buffer[0]`` through ``Buffer[length - 1]``. ``start`` and ``length`` are
    ``None`` when the operands do not state them literally, which is the
    difference between proving an overlap and admitting one was not ruled out.
    """

    tag_name: str
    mnemonic: str
    rung_id: uuid.UUID
    rung_label: str
    start: int | None
    length: int | None

    def covers(self, index: int | None) -> bool | None:
        """Whether this range writes element ``index``. ``None`` if undecidable."""
        if self.start is None or self.length is None or index is None:
            return None
        return self.start <= index < self.start + self.length


def _range_writes(context: ValidationContext) -> list[_RangeWrite]:
    """Every ``COP``/``CPS``/``FLL`` destination in the ladder, with its extent."""
    found: list[_RangeWrite] = []
    for instruction in context.instructions:
        mnemonic = instruction.mnemonic.upper()
        length_ordinal = _RANGE_WRITER_LENGTH_OPERANDS.get(mnemonic)
        if length_ordinal is None:
            continue
        spec = spec_for(mnemonic)
        if spec is None:  # pragma: no cover - the table only names catalogued entries
            continue
        by_ordinal = {
            operand.ordinal: operand
            for operand in context.operands_by_instruction.get(instruction.id, ())
        }
        length = by_ordinal.get(length_ordinal)
        for ordinal in spec.destructive_operand_indexes:
            destination = by_ordinal.get(ordinal)
            if destination is None:
                continue
            name = context.operand_name(destination)
            if not name:
                continue
            found.append(
                _RangeWrite(
                    tag_name=name,
                    mnemonic=mnemonic,
                    rung_id=instruction.rung_id,
                    rung_label=context.rung_label(instruction.rung_id),
                    start=_subscript_index(destination.text or name),
                    length=_literal_count(length.text) if length is not None else None,
                )
            )
    return found


class DestructiveWriteValidator:
    """Tags that more than one instruction writes."""

    key = "logic.destructive_writes"
    discipline = _DISCIPLINE
    description = "Two or more instructions writing the same tag."

    def check(self, context: ValidationContext) -> Iterable[Issue]:
        writes = all_destructive_writes(context)
        grouped: dict[str, list[DestructiveWrite]] = {}
        for write in writes:
            grouped.setdefault(fold(write.tag_name), []).append(write)

        issues: list[Issue] = []
        for writes in grouped.values():
            if len(writes) < 2:
                continue

            mnemonics = {write.mnemonic for write in writes}
            if len(mnemonics) == len(writes) and any(
                mnemonics <= allowed for allowed in _COMPLEMENTARY_WRITER_SETS
            ):
                continue

            locations = sorted({write.rung_label for write in writes})
            name = writes[0].tag_name
            detail = {
                "tag_name": name,
                "mnemonics": sorted(mnemonics),
                "locations": locations,
                "write_count": len(writes),
            }

            if mnemonics <= _MASKED_WRITERS:
                issues.append(
                    Issue(
                        code="logic.masked_duplicate_destination",
                        discipline=_DISCIPLINE,
                        # WARNING, not ERROR: MVM and BTD write only the bits
                        # their mask selects, so two of them on one tag conflict
                        # only if the masks overlap. Deciding that means
                        # evaluating the mask operands, which this validator
                        # does not do — so it reports the pair and leaves the
                        # judgement where the evidence is.
                        severity=ValidationSeverity.WARNING,
                        message=(
                            f"'{name}' is written by {len(writes)} masked instructions "
                            f"({', '.join(sorted(mnemonics))}) at: {'; '.join(locations)}. "
                            "Whether they conflict depends on whether their masks overlap, "
                            "which was not evaluated."
                        ),
                        object_type=ObjectType.LOGIC_RUNG,
                        object_id=writes[0].rung_id,
                        object_label=writes[0].rung_label,
                        suggested_action=(
                            "Check the masks against each other; if they overlap, one of the "
                            "two writes is being lost."
                        ),
                        detail=detail,
                    )
                )
                continue

            issues.append(
                Issue(
                    code="logic.duplicate_destructive_output",
                    discipline=_DISCIPLINE,
                    # ERROR, and one of the few defects a validator can find
                    # with certainty. The last rung scanned wins and the earlier
                    # write is lost every scan, silently — the logic reads
                    # correctly rung by rung and the machine does not do what
                    # either rung says.
                    severity=ValidationSeverity.ERROR,
                    message=(
                        f"'{name}' is written by {len(writes)} instructions "
                        f"({', '.join(sorted(mnemonics))}) at: {'; '.join(locations)}. "
                        "The last one scanned wins and the others have no effect."
                    ),
                    object_type=ObjectType.LOGIC_RUNG,
                    object_id=writes[0].rung_id,
                    object_label=writes[0].rung_label,
                    suggested_action=(
                        "Combine the conditions onto one rung that writes the tag once, or "
                        "give each writer its own tag."
                    ),
                    detail=detail,
                )
            )

        issues.extend(self._range_overlaps(context, writes))
        return issues

    def _range_overlaps(
        self, context: ValidationContext, writes: list[DestructiveWrite]
    ) -> list[Issue]:
        """Copies and fills whose written range reaches another writer's element.

        The check above groups by operand text, so a ``COP`` into ``Buffer[0]``
        and a ``MOV`` into ``Buffer[5]`` are two groups of one and nothing is
        reported — while both write ``Buffer[5]`` every scan and the operator's
        manual entry is lost. Grouping by the array instead, and comparing the
        copy's extent against the other writer's element, is what makes that
        findable.
        """
        ranges = _range_writes(context)
        if not ranges:
            return []

        by_base: dict[str, list[DestructiveWrite]] = {}
        for write in writes:
            by_base.setdefault(base_name(write.tag_name), []).append(write)

        issues: list[Issue] = []
        for base in sorted({base_name(item.tag_name) for item in ranges}):
            in_range = [item for item in ranges if base_name(item.tag_name) == base]
            # Every writer of the same array, compared against every range that
            # is not itself. One copy reaching into another copy's range is the
            # same defect as a copy reaching into a MOV's element, so the two
            # cases are not separated here.
            proven = False
            reached: list[tuple[_RangeWrite, DestructiveWrite]] = []
            for item in in_range:
                for write in by_base.get(base, []):
                    if fold(write.tag_name) == fold(item.tag_name):
                        continue
                    covered = item.covers(_subscript_index(write.tag_name))
                    if covered is False:
                        continue
                    proven = proven or covered is True
                    reached.append((item, write))
            if not reached:
                # Either nothing else writes the array, or every element was
                # accounted for and none of them collide.
                continue

            first = reached[0][0]
            involved = [write for _, write in reached]
            locations = sorted(
                {item.rung_label for item, _ in reached} | {write.rung_label for write in involved}
            )
            elements = sorted({write.tag_name for write in involved})
            detail: dict[str, object] = {
                "array": base,
                "range_writers": sorted({item.mnemonic for item, _ in reached}),
                "other_writers": sorted({write.mnemonic for write in involved}),
                "elements": elements[:20],
                "locations": locations,
                "overlap_proven": proven,
            }
            issues.append(
                Issue(
                    code="logic.range_duplicate_destination",
                    discipline=_DISCIPLINE,
                    # ERROR once the overlap is proven from literal operands:
                    # it is the same defect as two coils on one tag, and the
                    # copy wins or loses by scan order every scan. WARNING when
                    # the start element or the length is not a literal, because
                    # then the extent genuinely was not established and a
                    # confident error would be a guess.
                    severity=(ValidationSeverity.ERROR if proven else ValidationSeverity.WARNING),
                    message=(
                        f"'{first.tag_name}' is the start of a {first.mnemonic} destination "
                        f"range in '{base}', and {', '.join(elements[:5])} "
                        f"{'is' if len(elements) == 1 else 'are'} written separately at: "
                        f"{'; '.join(locations)}. "
                        + (
                            "The copied range covers those elements, so one of the two "
                            "writes is lost every scan."
                            if proven
                            else "Whether the range covers them was not established: the "
                            "start element or the length is not a literal."
                        )
                    ),
                    object_type=ObjectType.LOGIC_RUNG,
                    object_id=first.rung_id,
                    object_label=first.rung_label,
                    suggested_action=(
                        "Check the copy's length against the elements the other rungs "
                        "write; if they overlap, copy into a range nothing else drives or "
                        "drop the separate write."
                    ),
                    detail=detail,
                )
            )
        return issues


class OutputCoverageValidator:
    """Outputs, alarms and latches that no rung ever drives or clears."""

    key = "logic.output_coverage"
    discipline = _DISCIPLINE
    description = "Field outputs, alarm triggers and latched bits nothing in the ladder writes."

    def check(self, context: ValidationContext) -> Iterable[Issue]:
        if not context.has_ladder_logic:
            # Every output would be reported as undriven, which is true, useless
            # and would bury everything that matters. Stated once so a run over a
            # model with no logic does not read as a run that found the logic
            # clean.
            return [
                Issue(
                    code="logic.no_ladder_logic_present",
                    discipline=_DISCIPLINE,
                    severity=ValidationSeverity.INFORMATION,
                    message=(
                        "The model contains no ladder rungs, so output coverage, alarm "
                        "triggers and latched bits were not examined."
                    ),
                    object_label=label(context.project.name),
                    suggested_action="Run this check again once logic has been added.",
                    detail={"routines": len(context.routines)},
                )
            ]

        # Not ``context.is_written``: that reads the catalogue's fixed tuples
        # only, so a JSR return parameter is a tag nothing writes and the field
        # output it drives is reported as one that can never energise — a
        # confident error against correct logic.
        writes = all_destructive_writes(context)
        written = frozenset(fold(write.tag_name) for write in writes)

        issues: list[Issue] = []
        issues.extend(self._undriven_outputs(context, written))
        issues.extend(self._undriven_alarms(context, written))
        issues.extend(self._unlatched_latches(writes))
        return issues

    @staticmethod
    def _is_written(written: frozenset[str], tag_name: str) -> bool:
        """Whether the ladder writes this tag, a member of it or an element of it."""
        return any(references(name, tag_name) for name in written)

    def _undriven_outputs(self, context: ValidationContext, written: frozenset[str]) -> list[Issue]:
        issues: list[Issue] = []
        for tag in context.tags:
            if not self._is_field_output(context, tag.signal_id, tag.io_point_id):
                continue
            if self._is_written(written, tag.name):
                continue
            issues.append(
                Issue(
                    code="logic.output_without_driving_rung",
                    discipline=_DISCIPLINE,
                    # ERROR: a tag bound to a physical output that no
                    # instruction writes is a terminal that can never energise.
                    # The device is wired, documented and dead, and no amount of
                    # reading the tag list or the I/O schedule shows it.
                    severity=ValidationSeverity.ERROR,
                    message=(
                        f"'{tag.name}' is bound to a field output but no rung writes it. "
                        "The output can never be energised."
                    ),
                    object_type=ObjectType.TAG,
                    object_id=tag.id,
                    object_label=label(f"Output / {tag.name}"),
                    suggested_action=(
                        "Add the rung that drives this output, or remove the binding if the "
                        "output is not in scope."
                    ),
                    detail={"tag_name": tag.name, "role": tag.role.value},
                )
            )
        return issues

    def _is_field_output(
        self,
        context: ValidationContext,
        signal_id: uuid.UUID | None,
        io_point_id: uuid.UUID | None,
    ) -> bool:
        if signal_id is not None:
            signal = context.signals_by_id.get(signal_id)
            if signal is not None and (
                signal.signal_type in _OUTPUT_SIGNAL_TYPES
                or signal.direction is SignalDirection.OUTPUT
            ):
                return True
        if io_point_id is not None:
            point = context.io_points_by_id.get(io_point_id)
            if point is not None and point.direction is SignalDirection.OUTPUT:
                return True
        return False

    def _undriven_alarms(self, context: ValidationContext, written: frozenset[str]) -> list[Issue]:
        issues: list[Issue] = []
        io_bound = {fold(tag.name) for tag in context.tags if tag.is_io_bound or tag.is_alias}

        triggers: list[tuple[str, str, ObjectType, uuid.UUID, str, AlarmSeverity | None]] = []
        for alarm in context.alarms:
            if alarm.trigger_tag_name.strip():
                triggers.append(
                    (
                        alarm.trigger_tag_name,
                        f"Alarm {alarm.reference} '{alarm.name}'",
                        ObjectType.ALARM_DEFINITION,
                        alarm.id,
                        label(f"Alarm / {alarm.reference} / {alarm.name}"),
                        alarm.severity,
                    )
                )
        for fault in context.fault_definitions:
            if fault.trigger_tag_name.strip():
                triggers.append(
                    (
                        fault.trigger_tag_name,
                        f"Fault {fault.reference or fault.name}",
                        ObjectType.FAULT_DEFINITION,
                        fault.id,
                        label(f"Fault / {fault.reference or fault.name}"),
                        fault.severity,
                    )
                )

        for trigger, subject, object_type, object_id, location, severity in triggers:
            if fold(trigger) in io_bound:
                # Driven by the I/O module rather than by a rung. Reporting it
                # would be a confident error against a perfectly normal alarm on
                # a hardwired input.
                continue
            if self._is_written(written, trigger):
                continue
            issues.append(
                Issue(
                    code="logic.alarm_trigger_never_written",
                    discipline=_DISCIPLINE,
                    # ERROR: the alarm is configured, appears in the alarm list
                    # and passes every review that only reads the alarm list.
                    # The operator will never see it, because no rung can set
                    # the bit.
                    severity=ValidationSeverity.ERROR,
                    message=(
                        f"{subject} is triggered by '{trigger}', which no rung writes and "
                        "which is not bound to an input. The alarm can never be raised."
                    ),
                    object_type=object_type,
                    object_id=object_id,
                    object_label=location,
                    suggested_action=(
                        "Add the logic that detects the condition and sets the trigger, or "
                        "point the alarm at the tag that already carries it."
                    ),
                    detail={
                        "trigger_tag_name": trigger,
                        "severity": severity.value if severity is not None else "",
                    },
                )
            )
        return issues

    def _unlatched_latches(self, writes: list[DestructiveWrite]) -> list[Issue]:
        latched: dict[str, DestructiveWrite] = {}
        unlatched: set[str] = set()
        for write in writes:
            if write.mnemonic == "OTL":
                latched.setdefault(fold(write.tag_name), write)
            elif write.mnemonic == "OTU":
                unlatched.add(fold(write.tag_name))

        issues: list[Issue] = []
        for key, write in latched.items():
            if key in unlatched:
                continue
            issues.append(
                Issue(
                    code="logic.latched_output_without_unlatch",
                    discipline=_DISCIPLINE,
                    # ERROR: OTL leaves the bit set when the rung goes false,
                    # across a fault and across a power cycle. With no OTU
                    # anywhere, nothing in the controller can ever clear it —
                    # the machine latches once and stays latched.
                    severity=ValidationSeverity.ERROR,
                    message=(
                        f"'{write.tag_name}' is latched by OTL at {write.rung_label} and no "
                        "OTU anywhere in the project clears it."
                    ),
                    object_type=ObjectType.LOGIC_RUNG,
                    object_id=write.rung_id,
                    object_label=write.rung_label,
                    suggested_action=(
                        "Add the OTU that clears this bit, on the reset condition the "
                        "specification states."
                    ),
                    detail={"tag_name": write.tag_name, "latched_at": write.rung_label},
                )
            )
        return issues


class RoutineCallValidator:
    """Routines nothing reaches, and calls that reach nothing."""

    key = "logic.routine_calls"
    discipline = _DISCIPLINE
    description = "JSR targets that do not exist, and routines nothing calls."

    def check(self, context: ValidationContext) -> Iterable[Issue]:
        issues: list[Issue] = []
        called: set[str] = set()
        routine_names = {fold(routine.name) for routine in context.routines}

        for instruction in context.instructions:
            if instruction.mnemonic.upper() != "JSR":
                continue
            operands = context.operands_by_instruction.get(instruction.id, ())
            target = next((item for item in operands if item.ordinal == 0), None)
            if target is None:
                continue
            name = context.operand_name(target).strip()
            if not name:
                continue
            called.add(fold(name))
            if fold(name) in routine_names:
                continue
            issues.append(
                Issue(
                    code="logic.jsr_target_missing",
                    discipline=_DISCIPLINE,
                    # ERROR: a JSR to a routine that does not exist faults the
                    # controller on the scan that reaches it, and the import
                    # refuses the rung outright.
                    severity=ValidationSeverity.ERROR,
                    message=(f"JSR calls routine '{name}', which does not exist in this project."),
                    object_type=ObjectType.LOGIC_RUNG,
                    object_id=instruction.rung_id,
                    object_label=context.rung_label(instruction.rung_id),
                    suggested_action=(
                        "Create the routine the call names, or correct the call to the "
                        "routine that was meant."
                    ),
                    detail={"target_routine": name},
                )
            )

        # Programs name their main and fault routines by name rather than by id,
        # and the controller calls those without a JSR.
        entry_points = {
            fold(name)
            for program in context.programs
            for name in (program.main_routine_name, program.fault_routine_name)
            if name.strip()
        }

        if context.has_ladder_logic:
            for routine in context.routines:
                if routine.is_main:
                    continue
                key = fold(routine.name)
                if key in called or key in entry_points:
                    continue
                program = context.programs_by_id.get(routine.program_id)
                issues.append(
                    Issue(
                        code="logic.uncalled_routine",
                        discipline=_DISCIPLINE,
                        # WARNING: dead logic ships and does nothing, which is
                        # wasteful rather than wrong. It is often the visible
                        # end of a real defect — the JSR that should call it was
                        # never written — so it is reported for a human to
                        # decide between the two.
                        severity=ValidationSeverity.WARNING,
                        message=(
                            f"Routine '{routine.name}' is not the program's main routine and "
                            "no JSR calls it. Nothing in the controller executes it."
                        ),
                        object_type=ObjectType.ROUTINE,
                        object_id=routine.id,
                        object_label=label(
                            f"{program.name if program else 'unknown program'} / {routine.name}"
                        ),
                        suggested_action=(
                            "Add the JSR that should call it, or delete the routine if it is "
                            "left over from an earlier design."
                        ),
                        detail={
                            "routine": routine.name,
                            "rung_count": routine.rung_count,
                            "language": routine.language.value,
                        },
                    )
                )

        return issues


class InstructionSupportValidator:
    """Instructions this platform will not generate, cannot describe, or must not judge."""

    key = "logic.instruction_support"
    discipline = _DISCIPLINE
    description = "Restricted, unknown and unsupported instructions, and non-ladder routines."

    def check(self, context: ValidationContext) -> Iterable[Issue]:
        issues: list[Issue] = []
        by_mnemonic: dict[str, list[LogicInstruction]] = {}
        for instruction in context.instructions:
            by_mnemonic.setdefault(instruction.mnemonic.upper(), []).append(instruction)

        platform = context.target_platform
        family = platform.controller_family if platform is not None else None
        family_known = family is not None and family is not ControllerFamily.OTHER

        if by_mnemonic and not family_known:
            issues.append(
                Issue(
                    code="logic.controller_family_unknown",
                    discipline=_DISCIPLINE,
                    severity=ValidationSeverity.UNSUPPORTED,
                    message=(
                        "No target controller family is recorded for this project, so "
                        "whether each instruction is supported by the controller it will "
                        "run on was not checked."
                    ),
                    object_label=label(context.project.name),
                    suggested_action=(
                        "Record the controller family on the target platform and run the "
                        "validation again."
                    ),
                    detail={"distinct_mnemonics": len(by_mnemonic)},
                )
            )

        for mnemonic, instructions in sorted(by_mnemonic.items()):
            first = instructions[0]
            locations = sorted({context.rung_label(item.rung_id) for item in instructions})
            base_detail: dict[str, object] = {
                "mnemonic": mnemonic,
                "occurrences": len(instructions),
                "locations": locations[:20],
            }

            if is_restricted(mnemonic):
                issues.append(
                    Issue(
                        code="logic.restricted_instruction",
                        discipline=_DISCIPLINE,
                        # UNSUPPORTED rather than ERROR: the instruction is
                        # read, reported and cited perfectly well. What this
                        # platform refuses is to *generate* it, and that refusal
                        # is a scope statement rather than a defect in the
                        # customer's logic. It needs a named human because the
                        # competence to review it is a formal qualification.
                        severity=ValidationSeverity.UNSUPPORTED,
                        message=(
                            f"{mnemonic} appears {len(instructions)} time(s) at: "
                            f"{'; '.join(locations[:5])}. {restriction_reason(mnemonic)}"
                        ),
                        object_type=ObjectType.LOGIC_RUNG,
                        object_id=first.rung_id,
                        object_label=context.rung_label(first.rung_id),
                        suggested_action=(
                            "A qualified engineer must review this logic and decide whether "
                            "it may form part of the release."
                        ),
                        detail=base_detail | {"reason": restriction_reason(mnemonic)},
                        requires_human_decision=True,
                    )
                )
                continue

            spec = spec_for(mnemonic)
            if spec is None or not is_known(mnemonic):
                issues.append(
                    Issue(
                        code="logic.unknown_instruction",
                        discipline=_DISCIPLINE,
                        severity=ValidationSeverity.UNSUPPORTED,
                        message=(
                            f"{mnemonic} is not described by this platform's instruction "
                            f"catalogue, so its {len(instructions)} occurrence(s) were read "
                            "but not checked. Its operand arity and, in particular, which "
                            "operands it writes are unknown here."
                        ),
                        object_type=ObjectType.LOGIC_RUNG,
                        object_id=first.rung_id,
                        object_label=context.rung_label(first.rung_id),
                        suggested_action=(
                            "If this is an Add-On Instruction, supply its definition. "
                            "Otherwise have an engineer confirm what it writes before the "
                            "duplicate-destination result is relied on."
                        ),
                        detail=base_detail,
                    )
                )
                continue

            if family_known and family is not None and not supports(mnemonic, family):
                issues.append(
                    Issue(
                        code="logic.unsupported_instruction",
                        discipline=_DISCIPLINE,
                        severity=ValidationSeverity.UNSUPPORTED,
                        message=(
                            f"{mnemonic} ({spec.name}) is not generated for a "
                            f"{family.value} controller. It appears "
                            f"{len(instructions)} time(s) and was read rather than checked."
                        ),
                        object_type=ObjectType.LOGIC_RUNG,
                        object_id=first.rung_id,
                        object_label=context.rung_label(first.rung_id),
                        suggested_action=(
                            "Replace the instruction with one the target controller supports, "
                            "or confirm the target family is recorded correctly."
                        ),
                        detail=base_detail | {"controller_family": family.value},
                    )
                )

            if spec.requires_human_review:
                issues.append(
                    Issue(
                        code="logic.instruction_requires_human_review",
                        discipline=_DISCIPLINE,
                        severity=ValidationSeverity.HUMAN_REVIEW_REQUIRED,
                        message=(
                            f"{mnemonic} ({spec.name}) appears {len(instructions)} time(s) "
                            f"at: {'; '.join(locations[:5])}. {spec.notes}"
                        ),
                        object_type=ObjectType.LOGIC_RUNG,
                        object_id=first.rung_id,
                        object_label=context.rung_label(first.rung_id),
                        suggested_action=(
                            "Read each occurrence in context; the hazard is in what the "
                            "surrounding rungs do, not in the instruction itself."
                        ),
                        detail=base_detail,
                        requires_human_decision=True,
                    )
                )

        for routine in context.routines:
            if routine.language is RoutineLanguage.LADDER:
                continue
            program = context.programs_by_id.get(routine.program_id)
            issues.append(
                Issue(
                    code="logic.non_ladder_routine_unchecked",
                    discipline=_DISCIPLINE,
                    severity=ValidationSeverity.UNSUPPORTED,
                    message=(
                        f"Routine '{routine.name}' is written in "
                        f"{routine.language.value.replace('_', ' ')}. This platform's logic "
                        "checks read ladder only, so nothing in it was examined."
                    ),
                    object_type=ObjectType.ROUTINE,
                    object_id=routine.id,
                    object_label=label(
                        f"{program.name if program else 'unknown program'} / {routine.name}"
                    ),
                    suggested_action=(
                        "Have an engineer review this routine by hand, and treat every "
                        "whole-project result here as excluding it."
                    ),
                    detail={"routine": routine.name, "language": routine.language.value},
                )
            )

        return issues


class ModeLogicValidator:
    """Operating modes that contradict each other."""

    key = "logic.modes"
    discipline = _DISCIPLINE
    description = "Modes selected by the same value, and more than one default mode."

    def check(self, context: ValidationContext) -> Iterable[Issue]:
        issues: list[Issue] = []

        defaults = [mode for mode in context.modes if mode.is_default]
        if len(defaults) > 1:
            names = sorted(mode.name for mode in defaults)
            issues.append(
                Issue(
                    code="logic.multiple_default_modes",
                    discipline=_DISCIPLINE,
                    # ERROR: which mode the equipment powers up in is decided by
                    # whichever row the generator writes last. Nothing in the
                    # documents says, and picking one here would be inventing
                    # the decision rather than reporting that it was never made.
                    severity=ValidationSeverity.ERROR,
                    message=(
                        f"{len(defaults)} modes are marked as the default: "
                        f"{', '.join(names)}. Only one can be."
                    ),
                    object_type=ObjectType.MODE_DEFINITION,
                    object_id=defaults[0].id,
                    object_label=label(f"Mode / {defaults[0].name}"),
                    suggested_action=(
                        "Clear the default flag on all but the mode the specification names."
                    ),
                    detail={"modes": names},
                )
            )

        by_selection: dict[tuple[str, str], list[str]] = {}
        identity: dict[tuple[str, str], uuid.UUID] = {}
        for mode in context.modes:
            tag_name = mode.selection_tag_name.strip()
            value = mode.selection_value.strip()
            if not tag_name or not value:
                continue
            key = (fold(tag_name), fold(value))
            by_selection.setdefault(key, []).append(mode.name)
            identity.setdefault(key, mode.id)

        for (tag_name, value), names in by_selection.items():
            if len(names) < 2:
                continue
            issues.append(
                Issue(
                    code="logic.conflicting_mode_selection",
                    discipline=_DISCIPLINE,
                    # ERROR: one selector value selects two modes at once. The
                    # equipment is in both, or in whichever the logic happens to
                    # test first, and the operator has no way to tell.
                    severity=ValidationSeverity.ERROR,
                    message=(
                        f"Modes {', '.join(sorted(names))} are all selected by "
                        f"'{tag_name}' = '{value}'."
                    ),
                    object_type=ObjectType.MODE_DEFINITION,
                    object_id=identity[(tag_name, value)],
                    object_label=label(f"Mode selection / {tag_name} = {value}"),
                    suggested_action=(
                        "Give each mode its own selector value, or merge the modes if they "
                        "are the same mode under two names."
                    ),
                    detail={
                        "selection_tag_name": tag_name,
                        "selection_value": value,
                        "modes": sorted(names),
                    },
                )
            )

        return issues


class InterlockImplementationValidator:
    """Documented protections that no rung implements."""

    key = "logic.interlock_implementation"
    discipline = _DISCIPLINE
    description = "Interlocks and permissives with no implementing rung."

    def check(self, context: ValidationContext) -> Iterable[Issue]:
        issues: list[Issue] = []

        for interlock in context.interlocks:
            location = label(f"Interlock / {interlock.reference} / {interlock.name}")
            if interlock.is_documented_but_missing:
                issues.append(
                    Issue(
                        code="logic.interlock_not_implemented",
                        discipline=_DISCIPLINE,
                        # ERROR, and the highest-value finding this product
                        # makes. The schedule says the pump trips on low suction
                        # pressure and nothing in the controller does. Every
                        # document reads as complete; the protection does not
                        # exist.
                        severity=ValidationSeverity.ERROR,
                        message=(
                            f"{interlock.reference} '{interlock.name}' is documented in "
                            f"{interlock.source_reference or interlock.requirement_reference} "
                            "and no logic implementing it was found."
                            + (
                                f" {interlock.implementation_note}"
                                if interlock.implementation_note
                                else ""
                            )
                        ),
                        object_type=ObjectType.INTERLOCK,
                        object_id=interlock.id,
                        object_label=location,
                        suggested_action=(
                            "Implement the interlock and link the rung, or record why the "
                            "documented protection is out of scope for this delivery."
                        ),
                        detail={
                            "reference": interlock.reference,
                            "kind": interlock.kind.value,
                            "action": interlock.action.value,
                            "trip_condition": interlock.trip_condition,
                            "trip_tag_name": interlock.trip_tag_name,
                            "is_safety_related": interlock.is_safety_related,
                        },
                    )
                )
                continue

            if interlock.is_implemented and interlock.implementing_rung_id is None:
                issues.append(
                    Issue(
                        code="logic.interlock_implementation_unverifiable",
                        discipline=_DISCIPLINE,
                        # ERROR: "implemented" is a claim and the rung is the
                        # evidence for it. A claim nobody can follow back to a
                        # rung is exactly the thing a reviewer would have to
                        # take on trust, which is what this platform exists to
                        # avoid.
                        severity=ValidationSeverity.ERROR,
                        message=(
                            f"{interlock.reference} '{interlock.name}' is marked implemented "
                            "but names no rung, so the claim cannot be checked."
                        ),
                        object_type=ObjectType.INTERLOCK,
                        object_id=interlock.id,
                        object_label=location,
                        suggested_action=(
                            "Link the rung that implements it, or clear the implemented flag."
                        ),
                        detail={
                            "reference": interlock.reference,
                            "implementation_note": interlock.implementation_note,
                        },
                    )
                )

        for permissive in context.permissives:
            if permissive.is_implemented and permissive.implementing_rung_id is not None:
                continue
            reason = (
                "is marked implemented but names no rung"
                if permissive.is_implemented
                else "has no implementing logic"
            )
            issues.append(
                Issue(
                    code="logic.permissive_not_implemented",
                    discipline=_DISCIPLINE,
                    # ERROR: a permissive gates a start. Missing, the equipment
                    # starts under conditions somebody wrote down as forbidden,
                    # and every document still says it cannot.
                    severity=ValidationSeverity.ERROR,
                    message=(
                        f"Permissive '{permissive.name}' {reason}. The start condition it "
                        "describes is not enforced by any logic in the model."
                    ),
                    object_type=ObjectType.PERMISSIVE,
                    object_id=permissive.id,
                    object_label=label(f"Permissive / {permissive.reference or permissive.name}"),
                    suggested_action=(
                        "Implement the start condition and link the rung, or record why it "
                        "is not being enforced."
                    ),
                    detail={
                        "reference": permissive.reference,
                        "condition": permissive.condition,
                        "condition_tag_name": permissive.condition_tag_name,
                        "is_implemented": permissive.is_implemented,
                    },
                )
            )

        return issues


class ResetBehaviourValidator:
    """Latched alarms nothing is stated to clear."""

    key = "logic.reset_behaviour"
    discipline = _DISCIPLINE
    description = "Latched alarms with no reset behaviour recorded."

    def check(self, context: ValidationContext) -> Iterable[Issue]:
        issues: list[Issue] = []
        faults_by_id = {fault.id: fault for fault in context.fault_definitions}

        for alarm in context.alarms:
            if not alarm.latched:
                continue

            behaviour = None
            fault = (
                faults_by_id.get(alarm.fault_definition_id)
                if alarm.fault_definition_id is not None
                else None
            )
            if fault is not None and fault.reset_behavior_id is not None:
                behaviour = context.reset_behaviors_by_id.get(fault.reset_behavior_id)

            if behaviour is not None and behaviour.kind is not ResetBehaviorKind.UNDEFINED:
                continue

            stated = (
                "the reset behaviour it names is UNDEFINED"
                if behaviour is not None
                else "no reset behaviour is recorded for it"
            )
            issues.append(
                Issue(
                    code="logic.latched_fault_without_reset",
                    discipline=_DISCIPLINE,
                    # ERROR: a latched alarm stays raised until something clears
                    # it. With nothing stated, the difference between an
                    # operator pressing a button and the machine restarting on
                    # its own has been left to whoever writes the logic next —
                    # and no default is chosen here, because choosing one would
                    # invent the decision.
                    severity=ValidationSeverity.ERROR,
                    message=(
                        f"Alarm {alarm.reference} '{alarm.name}' is latched and {stated}. "
                        "Nothing states how it is cleared."
                    ),
                    object_type=ObjectType.ALARM_DEFINITION,
                    object_id=alarm.id,
                    object_label=label(f"Alarm / {alarm.reference} / {alarm.name}"),
                    suggested_action=(
                        "Record the reset behaviour the specification states — who resets it, "
                        "from where, and whether the condition must have cleared first."
                    ),
                    detail={
                        "reference": alarm.reference,
                        "severity": alarm.severity.value,
                        "fault_definition_id": (
                            str(alarm.fault_definition_id)
                            if alarm.fault_definition_id is not None
                            else ""
                        ),
                        "reset_behavior_kind": (
                            behaviour.kind.value if behaviour is not None else ""
                        ),
                    },
                )
            )

        return issues


class SequenceStateValidator:
    """States the equipment can never enter."""

    key = "logic.sequence_states"
    discipline = _DISCIPLINE
    description = "Sequence states with no incoming transition that are not the initial state."

    def check(self, context: ValidationContext) -> Iterable[Issue]:
        if not context.states:
            return []

        if not context.transitions:
            # With no transitions recorded, every non-initial state is
            # unreachable — which says nothing about the design and everything
            # about what has been modelled so far.
            return [
                Issue(
                    code="logic.state_transitions_absent",
                    discipline=_DISCIPLINE,
                    severity=ValidationSeverity.UNSUPPORTED,
                    message=(
                        f"{len(context.states)} state(s) are defined and no transitions "
                        "between them are recorded, so state reachability could not be "
                        "established."
                    ),
                    object_label=label(context.project.name),
                    suggested_action=(
                        "Record the transitions the functional description states, then run "
                        "this check again."
                    ),
                    detail={"states": len(context.states)},
                )
            ]

        reachable = {transition.to_state_id for transition in context.transitions}
        issues: list[Issue] = []
        for state in context.states:
            if state.is_initial or state.id in reachable:
                continue
            issues.append(
                Issue(
                    code="logic.unreachable_state",
                    discipline=_DISCIPLINE,
                    # ERROR: nothing can put the equipment into this state, so
                    # everything documented as happening in it never happens.
                    # The sequence reads as complete and stops short.
                    severity=ValidationSeverity.ERROR,
                    message=(
                        f"State '{state.name}' has no incoming transition and is not the "
                        "initial state. Nothing can enter it."
                    ),
                    object_type=ObjectType.STATE_DEFINITION,
                    object_id=state.id,
                    object_label=label(f"State / {state.name}"),
                    suggested_action=(
                        "Record the transition that enters this state, or mark it initial if "
                        "it is where the sequence starts."
                    ),
                    detail={
                        "state": state.name,
                        "reference": state.reference,
                        "is_terminal": state.is_terminal,
                    },
                )
            )
        return issues
