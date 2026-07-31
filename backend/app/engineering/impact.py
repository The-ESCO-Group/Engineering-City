"""What a rename or a delete would break, before anybody agrees to it.

A tag rename looks like a cosmetic tidy-up in the tag table. It is not: the tag
name is the contract between the controller, the HMI, the alarm list, the test
plan and the I/O schedule, and changing it can silently orphan an HMI binding,
an alarm trigger, three test steps and a rung operand at once. The product brief
makes the impact report **mandatory before a rename is approved**, and
``ProposeChangesetPayload`` already refuses a change set that renames while
claiming to need no approval. This module is what puts the consequences in front
of the reviewer.

Four rules shape everything below.

* **Search the text as well as the foreign keys.** Every delivery and interlock
  object stores a referenced tag twice — the name as written, and a nullable
  foreign key to whatever that name resolved to (see the module docstring of
  :mod:`app.models.delivery`). Searching only the foreign keys would find the
  references that already work and miss every one that does not, which is the
  wrong half.
* **A substring match is a false positive.** ``MTR_101`` must not match
  ``MTR_1010``, and ``Conveyor.MTR_101`` names a member of ``Conveyor`` rather
  than the tag being renamed. Matching is on identifier boundaries, with
  ``Tag.Member``, ``Tag[3]`` and ``Tag.Member[2]`` all counted as references to
  ``Tag``. A false positive in an impact report is worse than a slow one: it
  teaches reviewers to skim the report, and then the true positives go with it.
* **Unresolved references are reported separately.** A reference that names the
  tag as text but was never bound to it is the one most likely to break quietly,
  because nothing downstream — validator, cross-reference pass, export — knows
  the two are the same object. Folding it into the affected total would hide the
  worst case inside the average one.
* **The report never proposes the fix.** It states what is affected. Rewriting
  the references is a change set of its own, with its own approval.

**Known limits, stated rather than hidden.** A program-qualified reference
(``Program:MainProgram.MTR_101``) is not matched, because the boundary rule
rejects an identifier preceded by a dot and distinguishing the qualifier from a
structure member needs a parser this module does not have. Membership rows that
are not canonical objects — ``TagGroupMember``, ``UDTMember`` — are not swept;
neither carries a tag name, so a rename does not break them, but a delete does
orphan a group membership. Free prose in requirements, decisions and clarification
text is not searched: a tag name in a sentence is a citation, not a binding, and
reporting every mention of ``MTR_101`` in a functional description would drown
the bindings that actually break.

Identifier matching is deliberately implemented here rather than taken from
:func:`app.validation.base.references`. That function answers the narrower
question "is this whole operand a reference to that tag", which is a special
case of the one this module has to answer for an HMI expression such as
``MTR_101_Run AND NOT MTR_1010_Run``; and pulling the validator layer — which
imports the generators — into the service layer for a three-line helper is the
wrong dependency to create.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import ColumnElement, func, or_, select
from sqlalchemy.orm import Session

from app.core.enums import ChangeOperationType, ObjectType, ValidationSeverity
from app.core.errors import ValidationError
from app.engineering import registry
from app.engineering.changesets import proposed_values
from app.models.delivery import AlarmDefinition, HMIBinding, TestStep
from app.models.interlocks import (
    CauseEffectEntry,
    FaultDefinition,
    Interlock,
    ModeDefinition,
    Permissive,
    ResetBehavior,
    SequenceStep,
    StateDefinition,
    TransitionDefinition,
)
from app.models.plant import AddressAssignment, IOPoint, Signal
from app.models.plc import LogicComment, LogicInstruction, LogicOperand, LogicReference, LogicRung
from app.models.project import Project
from app.models.revision import ChangeOperation, ChangeSet, ImpactReport
from app.models.tags import Tag, TagAlias

__all__ = [
    "DISCIPLINES",
    "analyse_changeset",
    "analyse_delete",
    "analyse_rename",
]

#: The disciplines ``ImpactReport.affected_counts`` is grouped by, in the order
#: a reviewer reads them. Every report carries all seven keys, zeros included,
#: so a consumer never has to tell "no HMI bindings" from "the analysis did not
#: look at HMI".
DISCIPLINES: tuple[str, ...] = (
    "logic",
    "hmi",
    "alarms",
    "tests",
    "interlocks",
    "io",
    "documentation",
)

#: Why an unresolved reference is called out separately, in the words the
#: reviewer sees. One constant rather than a phrase per sweep, because the
#: condition is the same everywhere it occurs.
_UNRESOLVED_REASON = (
    "the reference names the tag as text but was never bound to it, so nothing "
    "downstream connects the two and the rename cannot be followed through"
)

#: ``ImpactReport.object_label`` is ``String(200)``.
_MAX_LABEL_CHARS = 200


# ---------------------------------------------------------------------------
# Identifier matching
# ---------------------------------------------------------------------------
def _reference_pattern(name: str) -> re.Pattern[str]:
    """A pattern matching ``name`` used as a tag, and nothing else.

    The lookbehind rejects an identifier character *and a dot*: ``AMTR_101`` is a
    different identifier, and ``Conveyor.MTR_101`` is a member of ``Conveyor``,
    not this tag. The lookahead rejects only identifier characters, so ``.`` and
    ``[`` are allowed to follow — writing ``MTR_101.Running`` or ``MTR_101[3]``
    is writing something inside ``MTR_101``, and a check that said otherwise
    would report a referenced tag as unreferenced.

    Case-insensitive because Logix compares names case-insensitively, so
    ``mtr_101`` and ``MTR_101`` are the same operand.
    """
    return re.compile(rf"(?<![A-Za-z0-9_.]){re.escape(name)}(?![A-Za-z0-9_])", re.IGNORECASE)


def _like(name: str) -> str:
    """A SQL ``LIKE`` prefilter for ``name``, narrowing before Python decides.

    ``_`` and ``%`` are wildcards to ``LIKE`` and tag names are full of
    underscores, so this pattern over-matches — ``MTR_101`` also matches
    ``MTRX101``. That is harmless and deliberate: the prefilter only has to
    return a superset, and :func:`_reference_pattern` makes the actual decision.
    Escaping would buy nothing and would add a dialect-specific ``ESCAPE``
    clause to every query here.
    """
    return f"%{name}%"


def _bounded(text: str) -> str:
    """Collapse whitespace and bound a label to what the column will hold."""
    cleaned = " ".join(text.split())
    if len(cleaned) <= _MAX_LABEL_CHARS:
        return cleaned
    return cleaned[: _MAX_LABEL_CHARS - 1] + "…"


# ---------------------------------------------------------------------------
# Disciplines and the nouns a summary is written from
# ---------------------------------------------------------------------------
#: Which discipline each canonical object belongs to. Complete over
#: :class:`ObjectType`, so a new object type fails a test rather than landing in
#: a silent default bucket. The plant hierarchy sits under ``io`` because that is
#: the deliverable its rows appear in — the I/O schedule — and the requirements
#: and profile objects sit under ``documentation`` for the same reason.
_DISCIPLINE_BY_TYPE: Mapping[ObjectType, str] = {
    ObjectType.PROJECT_PROFILE: "documentation",
    ObjectType.PROJECT_QUESTION: "documentation",
    ObjectType.PROJECT_DECISION: "documentation",
    ObjectType.REQUIREMENT: "documentation",
    ObjectType.ACCEPTANCE_CRITERION: "documentation",
    ObjectType.ASSUMPTION: "documentation",
    ObjectType.CONSTRAINT: "documentation",
    ObjectType.CLARIFICATION: "documentation",
    ObjectType.FINDING: "documentation",
    ObjectType.EXPORT_ARTIFACT: "documentation",
    ObjectType.RELEASE_CANDIDATE: "documentation",
    ObjectType.AREA: "io",
    ObjectType.UNIT: "io",
    ObjectType.EQUIPMENT_MODULE: "io",
    ObjectType.DEVICE: "io",
    ObjectType.DEVICE_TYPE: "io",
    ObjectType.NETWORK_NODE: "io",
    ObjectType.MODULE: "io",
    ObjectType.IO_POINT: "io",
    ObjectType.SIGNAL: "io",
    ObjectType.ADDRESS_ASSIGNMENT: "io",
    ObjectType.TAG: "logic",
    ObjectType.TAG_ALIAS: "logic",
    ObjectType.TAG_GROUP: "logic",
    ObjectType.DATA_TYPE_DEFINITION: "logic",
    ObjectType.UDT_DEFINITION: "logic",
    ObjectType.AOI_REFERENCE: "logic",
    ObjectType.CONTROLLER_TASK: "logic",
    ObjectType.PROGRAM: "logic",
    ObjectType.ROUTINE: "logic",
    ObjectType.LOGIC_NETWORK: "logic",
    ObjectType.LOGIC_RUNG: "logic",
    ObjectType.LOGIC_INSTRUCTION: "logic",
    ObjectType.INTERLOCK: "interlocks",
    ObjectType.PERMISSIVE: "interlocks",
    ObjectType.CAUSE_EFFECT_ENTRY: "interlocks",
    ObjectType.MODE_DEFINITION: "interlocks",
    ObjectType.STATE_DEFINITION: "interlocks",
    ObjectType.TRANSITION_DEFINITION: "interlocks",
    ObjectType.SEQUENCE_STEP: "interlocks",
    ObjectType.FAULT_DEFINITION: "interlocks",
    ObjectType.RESET_BEHAVIOR: "interlocks",
    ObjectType.HMI_SCREEN: "hmi",
    ObjectType.HMI_BINDING: "hmi",
    ObjectType.ALARM_DEFINITION: "alarms",
    ObjectType.TEST_CASE: "tests",
}

#: Nouns the summary sentence is built from, where the registry's own type label
#: would not do. Everything else is derived from :func:`registry.type_label` in
#: lower case, so a new object type reads correctly without a second table; the
#: entries here are the ones where lower-casing would eat an acronym, plus the
#: reference-shaped nouns — a reviewer hears "one alarm trigger", not "one alarm".
_NOUN_OVERRIDES: Mapping[str, str] = {
    ObjectType.HMI_SCREEN.value: "HMI screen",
    ObjectType.HMI_BINDING.value: "HMI binding",
    ObjectType.IO_POINT.value: "I/O point",
    ObjectType.UDT_DEFINITION.value: "UDT",
    ObjectType.AOI_REFERENCE.value: "Add-On Instruction",
    "alarm_trigger": "alarm trigger",
    "aliasing_tag": "aliasing tag",
    "colliding_tag": "colliding tag",
    "operand_comment": "operand comment",
    "test_step": "test step",
}


def _noun(kind: str) -> str:
    override = _NOUN_OVERRIDES.get(kind)
    if override is not None:
        return override
    try:
        return registry.type_label(ObjectType(kind)).lower()
    except ValueError:
        return kind.replace("_", " ")


def _plural(noun: str) -> str:
    if noun.endswith(("s", "x", "ch", "sh")):
        return f"{noun}es"
    if noun.endswith("y") and not noun.endswith(("ay", "ey", "iy", "oy", "uy")):
        return f"{noun[:-1]}ies"
    return f"{noun}s"


def _count_phrase(kind: str, count: int) -> str:
    noun = _noun(kind)
    return f"{count} {noun}" if count == 1 else f"{count} {_plural(noun)}"


def _join(parts: Sequence[str]) -> str:
    """``"a, b and c"`` — a list a reviewer can read aloud."""
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    return f"{', '.join(parts[:-1])} and {parts[-1]}"


# ---------------------------------------------------------------------------
# Collected findings
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class _Hit:
    """One object the change reaches, and how."""

    object_type: str
    object_id: uuid.UUID
    label: str
    discipline: str
    kind: str
    effect: str
    fields: set[str] = field(default_factory=set)
    references: set[str] = field(default_factory=set)
    unresolved: bool = False
    #: True when the reference *writes* the tag. Carried so that a rung driving
    #: a coil is not reported in the same breath as one merely reading it.
    is_write: bool = False

    def merge(self, other: _Hit) -> None:
        """Fold a second reference to the same object into this one."""
        self.fields |= other.fields
        self.references |= other.references
        # One resolved reference is enough to make the object affected: the
        # rename can be followed through for it, so it does not belong in the
        # unresolved list even if another of its columns names the tag loosely.
        self.unresolved = self.unresolved and other.unresolved
        if other.is_write and not self.is_write:
            self.effect = other.effect
            self.is_write = True

    def to_json(self) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "object_type": self.object_type,
            "object_id": str(self.object_id),
            "label": self.label,
            "discipline": self.discipline,
            "kind": self.kind,
            "effect": self.effect,
            "fields": sorted(self.fields),
            "references": sorted(self.references),
        }
        if self.unresolved:
            entry["reason"] = _UNRESOLVED_REASON
        return entry


class _Findings:
    """Every hit, deduplicated to one entry per affected object.

    Deduplication is the reason this is a class rather than a list. A rung that
    names the tag in four operands is one affected rung, and a report that said
    "four rungs" would be wrong in the direction that matters — it would inflate
    the consequence of a rename and, worse, would not survive being checked.
    """

    def __init__(self) -> None:
        self._hits: dict[tuple[str, uuid.UUID], _Hit] = {}

    def add(self, hit: _Hit) -> None:
        key = (hit.object_type, hit.object_id)
        existing = self._hits.get(key)
        if existing is None:
            self._hits[key] = hit
        else:
            existing.merge(hit)

    def _ordered(self, *, unresolved: bool) -> list[_Hit]:
        rank = {discipline: index for index, discipline in enumerate(DISCIPLINES)}
        return sorted(
            (hit for hit in self._hits.values() if hit.unresolved is unresolved),
            key=lambda hit: (rank.get(hit.discipline, len(rank)), hit.kind, hit.label),
        )

    @property
    def affected(self) -> list[_Hit]:
        return self._ordered(unresolved=False)

    @property
    def unresolved(self) -> list[_Hit]:
        return self._ordered(unresolved=True)

    def discipline_counts(self) -> dict[str, int]:
        counts = dict.fromkeys(DISCIPLINES, 0)
        for hit in self.affected:
            counts[hit.discipline] = counts.get(hit.discipline, 0) + 1
        return counts

    def kind_counts(self) -> dict[str, int]:
        """Counts per noun, in the order the summary reads them."""
        counts: dict[str, int] = {}
        for hit in self.affected:
            counts[hit.kind] = counts.get(hit.kind, 0) + 1
        return counts


# ---------------------------------------------------------------------------
# Where a tag name is written as text
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class _TextReference:
    """One column pair where an object names a tag.

    ``id_field`` is the nullable foreign key beside the text, and its absence is
    meaningful: where the schema offers no foreign key — ``state_tag_name``,
    ``affected_tag_name``, ``completion_tag_name`` — the text *is* the binding,
    so such a row can never be "unresolved". It is simply affected, and the
    rename has nothing to follow but the string.
    """

    model: type[Any]
    object_type: str
    name_fields: tuple[str, ...]
    id_field: str | None
    discipline: str
    kind: str
    effect: str
    label: Callable[[Any], str] | None = None


def _test_step_label(step: Any) -> str:
    """``"Test case TC-014 / Step 3"`` — the sheet and the line on it."""
    case = step.test_case
    if case is None:  # pragma: no cover - the foreign key is not nullable
        return f"Test step {step.step_number}"
    return f"{registry.label_for(case)} / Step {step.step_number}"


def _comment_label(comment: Any) -> str:
    rung = comment.rung
    path = registry.label_for(rung) if rung is not None else "Rung"
    return f"{path} comment on {comment.operand_text}"


#: Every column outside logic and I/O where a tag is named as text.
#:
#: ``LogicComment.operand_text`` is filed under documentation rather than logic
#: on purpose: the comment attaches to an *address string* rather than to an
#: operand row (see :class:`~app.models.plc.LogicComment`), so a rename detaches
#: the only written statement of what the bit means without changing any logic.
_TAG_REFERENCES: tuple[_TextReference, ...] = (
    _TextReference(
        model=TagAlias,
        object_type=ObjectType.TAG_ALIAS.value,
        name_fields=("target_expression",),
        id_field="target_tag_id",
        discipline="logic",
        kind=ObjectType.TAG_ALIAS.value,
        effect="aliases the tag, or a member or element of it",
    ),
    _TextReference(
        model=Tag,
        object_type=ObjectType.TAG.value,
        name_fields=("alias_for",),
        id_field=None,
        discipline="logic",
        kind="aliasing_tag",
        effect="is an alias whose target expression names the tag",
    ),
    _TextReference(
        model=HMIBinding,
        object_type=ObjectType.HMI_BINDING.value,
        name_fields=("tag_name",),
        id_field="tag_id",
        discipline="hmi",
        kind=ObjectType.HMI_BINDING.value,
        effect="animates an HMI property from the tag",
    ),
    # A second spec rather than a second name field on the one above, because
    # ``tag_id`` resolves ``tag_name`` and nothing else. An animation expression
    # such as ``MTR_101_Run AND NOT Bypass`` names several tags and can never
    # resolve to one, so sharing the foreign key would file every expression
    # binding as an unresolved reference.
    _TextReference(
        model=HMIBinding,
        object_type=ObjectType.HMI_BINDING.value,
        name_fields=("expression",),
        id_field=None,
        discipline="hmi",
        kind=ObjectType.HMI_BINDING.value,
        effect="names the tag inside an HMI animation expression",
    ),
    _TextReference(
        model=AlarmDefinition,
        object_type=ObjectType.ALARM_DEFINITION.value,
        name_fields=("trigger_tag_name",),
        id_field="trigger_tag_id",
        discipline="alarms",
        kind="alarm_trigger",
        effect="is raised by the tag",
    ),
    _TextReference(
        model=TestStep,
        object_type="test_step",
        name_fields=("tag_name",),
        id_field="tag_id",
        discipline="tests",
        kind="test_step",
        effect="manipulates or observes the tag",
        label=_test_step_label,
    ),
    _TextReference(
        model=Interlock,
        object_type=ObjectType.INTERLOCK.value,
        name_fields=("trip_tag_name",),
        id_field="trip_tag_id",
        discipline="interlocks",
        kind=ObjectType.INTERLOCK.value,
        effect="trips on the tag",
    ),
    # ``affected_tag_name`` has no foreign key of its own — the schema never
    # offers to resolve what an interlock acts *on* — so it is swept separately
    # rather than borrowing ``trip_tag_id`` and being reported as unresolved.
    _TextReference(
        model=Interlock,
        object_type=ObjectType.INTERLOCK.value,
        name_fields=("affected_tag_name",),
        id_field=None,
        discipline="interlocks",
        kind=ObjectType.INTERLOCK.value,
        effect="acts on the tag when it trips",
    ),
    _TextReference(
        model=Permissive,
        object_type=ObjectType.PERMISSIVE.value,
        name_fields=("condition_tag_name",),
        id_field="condition_tag_id",
        discipline="interlocks",
        kind=ObjectType.PERMISSIVE.value,
        effect="gates a start on the tag",
    ),
    _TextReference(
        model=CauseEffectEntry,
        object_type=ObjectType.CAUSE_EFFECT_ENTRY.value,
        name_fields=("cause_tag_name",),
        id_field="cause_tag_id",
        discipline="interlocks",
        kind=ObjectType.CAUSE_EFFECT_ENTRY.value,
        effect="names the tag as the cause",
    ),
    _TextReference(
        model=CauseEffectEntry,
        object_type=ObjectType.CAUSE_EFFECT_ENTRY.value,
        name_fields=("effect_tag_name",),
        id_field="effect_tag_id",
        discipline="interlocks",
        kind=ObjectType.CAUSE_EFFECT_ENTRY.value,
        effect="names the tag as the effect",
    ),
    _TextReference(
        model=ModeDefinition,
        object_type=ObjectType.MODE_DEFINITION.value,
        name_fields=("selection_tag_name",),
        id_field="selection_tag_id",
        discipline="interlocks",
        kind=ObjectType.MODE_DEFINITION.value,
        effect="is selected by the tag",
    ),
    _TextReference(
        model=StateDefinition,
        object_type=ObjectType.STATE_DEFINITION.value,
        name_fields=("state_tag_name",),
        id_field=None,
        discipline="interlocks",
        kind=ObjectType.STATE_DEFINITION.value,
        effect="is held in the tag",
    ),
    _TextReference(
        model=TransitionDefinition,
        object_type=ObjectType.TRANSITION_DEFINITION.value,
        name_fields=("condition_tag_name",),
        id_field="condition_tag_id",
        discipline="interlocks",
        kind=ObjectType.TRANSITION_DEFINITION.value,
        effect="is conditioned on the tag",
    ),
    _TextReference(
        model=SequenceStep,
        object_type=ObjectType.SEQUENCE_STEP.value,
        name_fields=("completion_tag_name",),
        id_field=None,
        discipline="interlocks",
        kind=ObjectType.SEQUENCE_STEP.value,
        effect="completes on the tag",
    ),
    _TextReference(
        model=FaultDefinition,
        object_type=ObjectType.FAULT_DEFINITION.value,
        name_fields=("trigger_tag_name",),
        id_field="trigger_tag_id",
        discipline="interlocks",
        kind=ObjectType.FAULT_DEFINITION.value,
        effect="is triggered by the tag",
    ),
    _TextReference(
        model=ResetBehavior,
        object_type=ObjectType.RESET_BEHAVIOR.value,
        name_fields=("reset_tag_name",),
        id_field="reset_tag_id",
        discipline="interlocks",
        kind=ObjectType.RESET_BEHAVIOR.value,
        effect="is reset by the tag",
    ),
    _TextReference(
        model=LogicComment,
        object_type="logic_comment",
        name_fields=("operand_text",),
        id_field=None,
        discipline="documentation",
        kind="operand_comment",
        effect="documents the tag, and is matched to it by name alone",
        label=_comment_label,
    ),
)


# ---------------------------------------------------------------------------
# Sweeps
# ---------------------------------------------------------------------------
def _live(model: type[Any], statement: Any) -> Any:
    """Exclude soft-deleted rows where the model has a ``deleted_at``."""
    deleted_at = getattr(model, "deleted_at", None)
    if deleted_at is None:
        return statement
    return statement.where(deleted_at.is_(None))


def _matches(row: Any, spec: _TextReference, pattern: re.Pattern[str]) -> tuple[set[str], set[str]]:
    """The columns of ``row`` that genuinely name the tag, and what they say."""
    fields: set[str] = set()
    references: set[str] = set()
    for name_field in spec.name_fields:
        text = getattr(row, name_field, "") or ""
        if pattern.search(text):
            fields.add(name_field)
            references.add(text)
    return fields, references


def _sweep_text_references(
    db: Session,
    *,
    project: Project,
    tag: Tag,
    pattern: re.Pattern[str],
    findings: _Findings,
) -> None:
    """Every object outside logic and I/O that names the tag."""
    for spec in _TAG_REFERENCES:
        model = spec.model
        clauses = [getattr(model, name).ilike(_like(tag.name)) for name in spec.name_fields]
        if spec.id_field is not None:
            clauses.append(getattr(model, spec.id_field) == tag.id)
        statement = _live(model, select(model).where(model.project_id == project.id, or_(*clauses)))

        for row in db.execute(statement).scalars():
            if row.id == tag.id:
                # Only reachable through the ``Tag`` spec, and only when a tag's
                # own ``alias_for`` names itself. An object is not an impact of
                # changing itself.
                continue
            bound_to_tag = False
            unresolved = False
            if spec.id_field is not None:
                bound = getattr(row, spec.id_field)
                if bound is not None and bound != tag.id:
                    # The foreign key is authoritative: this row resolved to a
                    # different tag, so it is not a reference to this one however
                    # the text reads.
                    continue
                bound_to_tag = bound is not None
                unresolved = bound is None

            fields, references = _matches(row, spec, pattern)
            if not fields and not bound_to_tag:
                # Only the ``LIKE`` prefilter matched — ``_`` is a wildcard, so
                # ``MTR_1010`` comes back for ``MTR_101`` — and no foreign key
                # binds the row either. This is the false positive the whole
                # boundary rule exists to reject.
                continue

            label = spec.label(row) if spec.label is not None else registry.label_for(row)
            findings.add(
                _Hit(
                    object_type=spec.object_type,
                    object_id=row.id,
                    label=_bounded(label),
                    discipline=spec.discipline,
                    kind=spec.kind,
                    effect=spec.effect,
                    fields=fields,
                    references=references,
                    unresolved=unresolved,
                )
            )


def _sweep_logic(
    db: Session,
    *,
    project: Project,
    tag: Tag,
    pattern: re.Pattern[str],
    findings: _Findings,
) -> None:
    """Every rung that reads or writes the tag.

    The affected object is the *rung*, not the operand or the cross-reference
    row: a rung is what an engineer opens, reviews and approves, and neither
    :class:`~app.models.plc.LogicOperand` nor
    :class:`~app.models.plc.LogicReference` is addressable as a canonical object
    anyway. Both are swept because they answer the question differently — the
    operand is the source of truth, the cross-reference row is what the
    validators and reports read, and a tag present in one but not the other is
    itself worth seeing.
    """
    operands = (
        select(LogicOperand, LogicRung)
        .join(LogicInstruction, LogicInstruction.id == LogicOperand.instruction_id)
        .join(LogicRung, LogicRung.id == LogicInstruction.rung_id)
        .where(
            LogicOperand.project_id == project.id,
            LogicOperand.deleted_at.is_(None),
            LogicRung.deleted_at.is_(None),
            or_(
                LogicOperand.tag_id == tag.id,
                LogicOperand.resolved_name.ilike(_like(tag.name)),
                LogicOperand.text.ilike(_like(tag.name)),
            ),
        )
    )
    for operand, rung in db.execute(operands).all():
        if operand.tag_id is not None and operand.tag_id != tag.id:
            continue
        fields = {
            name
            for name in ("resolved_name", "text")
            if pattern.search(getattr(operand, name) or "")
        }
        unresolved = operand.tag_id is None
        if unresolved and not fields:
            continue
        findings.add(
            _Hit(
                object_type=ObjectType.LOGIC_RUNG.value,
                object_id=rung.id,
                label=_bounded(registry.label_for(rung)),
                discipline="logic",
                kind=ObjectType.LOGIC_RUNG.value,
                effect="writes the tag" if operand.is_destination else "reads the tag",
                fields={f"operand.{name}" for name in fields},
                references={operand.text} if operand.text else set(),
                unresolved=unresolved,
                is_write=operand.is_destination,
            )
        )

    cross_references = (
        select(LogicReference, LogicRung)
        .join(LogicRung, LogicRung.id == LogicReference.rung_id)
        .where(
            LogicReference.project_id == project.id,
            LogicRung.deleted_at.is_(None),
            or_(
                LogicReference.tag_id == tag.id,
                LogicReference.tag_name.ilike(_like(tag.name)),
            ),
        )
    )
    for reference, rung in db.execute(cross_references).all():
        if reference.tag_id is not None and reference.tag_id != tag.id:
            continue
        unresolved = reference.tag_id is None
        if unresolved and not pattern.search(reference.tag_name or ""):
            continue
        findings.add(
            _Hit(
                object_type=ObjectType.LOGIC_RUNG.value,
                object_id=rung.id,
                label=_bounded(registry.label_for(rung)),
                discipline="logic",
                kind=ObjectType.LOGIC_RUNG.value,
                effect="writes the tag" if reference.is_destructive else "reads the tag",
                fields={"logic_reference.tag_name"},
                references={reference.tag_name} if reference.tag_name else set(),
                unresolved=unresolved,
                is_write=reference.is_destructive,
            )
        )


def _sweep_io(db: Session, *, tag: Tag, findings: _Findings) -> None:
    """The physical bindings the tag name is printed against.

    The signal, the terminal and the assignment that joins them all appear in
    the I/O schedule with this tag's name beside them, so renaming the tag
    changes a delivered document even though no column in these tables holds the
    name. The device is deliberately not included: a device carries a
    ``tag_prefix``, which a tag rename does not touch.
    """
    if tag.signal_id is not None:
        signal = db.get(Signal, tag.signal_id)
        if signal is not None and not signal.is_deleted:
            findings.add(
                _Hit(
                    object_type=ObjectType.SIGNAL.value,
                    object_id=signal.id,
                    label=_bounded(registry.label_for(signal)),
                    discipline="io",
                    kind=ObjectType.SIGNAL.value,
                    effect="is the signal this tag represents in the I/O schedule",
                    fields={"tag.signal_id"},
                )
            )
            assignments = _live(
                AddressAssignment,
                select(AddressAssignment).where(AddressAssignment.signal_id == signal.id),
            )
            for assignment in db.execute(assignments).scalars():
                findings.add(
                    _Hit(
                        object_type=ObjectType.ADDRESS_ASSIGNMENT.value,
                        object_id=assignment.id,
                        label=_bounded(_assignment_label(assignment, signal)),
                        discipline="io",
                        kind=ObjectType.ADDRESS_ASSIGNMENT.value,
                        effect="binds the signal this tag names to a terminal",
                        fields={"address_assignment.signal_id"},
                    )
                )

    if tag.io_point_id is not None:
        io_point = db.get(IOPoint, tag.io_point_id)
        if io_point is not None and not io_point.is_deleted:
            findings.add(
                _Hit(
                    object_type=ObjectType.IO_POINT.value,
                    object_id=io_point.id,
                    label=_bounded(registry.label_for(io_point)),
                    discipline="io",
                    kind=ObjectType.IO_POINT.value,
                    effect="is the terminal this tag is wired to",
                    fields={"tag.io_point_id"},
                )
            )


def _assignment_label(assignment: Any, signal: Any) -> str:
    """An assignment has no name of its own, so it borrows the signal's."""
    return f"Address assignment for {registry.label_for(signal)}"


def _sweep_inbound_foreign_keys(
    db: Session,
    *,
    project: Project,
    object_type: ObjectType,
    object_id: uuid.UUID,
    findings: _Findings,
) -> None:
    """Every canonical object holding a foreign key to this one.

    Derived from the registry rather than listed, so an object type or a column
    added tomorrow is swept without anyone remembering to update this module.
    Only canonical objects are reachable this way: join rows such as
    ``TagGroupMember`` are not registered object types, and are named as a limit
    in the module docstring rather than half-covered here.
    """
    for holder, holder_model in registry.OBJECT_TYPE_MODELS.items():
        targets = registry.foreign_key_targets(holder)
        naming = [column for column, referenced in targets.items() if referenced is object_type]
        if not naming:
            continue
        model: Any = holder_model
        statement = _live(
            model,
            select(model).where(
                model.project_id == project.id,
                or_(*[getattr(model, column) == object_id for column in naming]),
            ),
        )
        for row in db.execute(statement).scalars():
            if holder is object_type and row.id == object_id:
                continue
            held = sorted(column for column in naming if getattr(row, column) == object_id)
            findings.add(
                _Hit(
                    object_type=holder.value,
                    object_id=row.id,
                    label=_bounded(registry.label_for(row)),
                    discipline=_DISCIPLINE_BY_TYPE[holder],
                    kind=holder.value,
                    effect=f"references it through {_join(held)}",
                    fields=set(held),
                )
            )


def _check_collision(db: Session, *, project: Project, tag: Tag, new_name: str) -> Tag | None:
    """The live tag already holding ``new_name`` in the same scope, if any.

    Scope-aware because Logix identity is ``(scope, program, name)``: the same
    name may legitimately exist controller-scoped and once per program, so a
    project-wide name check would report collisions that are not collisions.
    """
    same_program: ColumnElement[bool]
    if tag.scope_program_id is None:
        same_program = Tag.scope_program_id.is_(None)
    else:
        same_program = Tag.scope_program_id == tag.scope_program_id
    statement = select(Tag).where(
        Tag.project_id == project.id,
        Tag.id != tag.id,
        Tag.deleted_at.is_(None),
        Tag.scope == tag.scope,
        same_program,
        # Logix compares names case-insensitively, so ``mtr_101`` collides with
        # ``MTR_101`` and a case-sensitive check would let the merge create the
        # pair. ``lower()`` rather than ``ilike``: a tag name is full of
        # underscores, and ``_`` is a single-character wildcard to ``LIKE``,
        # which would report ``MTRX101`` as colliding with ``MTR_101``.
        func.lower(Tag.name) == new_name.lower(),
    )
    return db.execute(statement).scalars().first()


# ---------------------------------------------------------------------------
# Building the report
# ---------------------------------------------------------------------------
def _summarise(action: str, findings: _Findings, *, extra: Iterable[str] = ()) -> str:
    """The report as a reviewer would say it out loud.

    Written as sentences rather than as a rendering of the counts dictionary,
    because this string is what gets read in a design review: "affects 7 objects:
    4 rungs, 1 HMI binding, 1 alarm trigger and 1 test step" is a decision a
    person can make, and a JSON dump is not.
    """
    affected = findings.affected
    sentences: list[str] = []
    if affected:
        phrase = _join([_count_phrase(kind, n) for kind, n in findings.kind_counts().items()])
        noun = "object" if len(affected) == 1 else "objects"
        sentences.append(f"{action} affects {len(affected)} {noun}: {phrase}.")
    else:
        sentences.append(
            f"{action} affects no other object: nothing in logic, HMI, alarms, tests, "
            "interlocks, I/O or documentation references it."
        )

    unresolved = findings.unresolved
    if unresolved:
        subject = "One reference" if len(unresolved) == 1 else f"{len(unresolved)} references"
        sentences.append(f"{subject} could not be resolved.")

    sentences.extend(extra)
    return " ".join(sentences)


def _severity(findings: _Findings) -> ValidationSeverity:
    """``WARNING`` as soon as anything is touched, ``INFORMATION`` otherwise.

    Deliberately flat. Severity here describes the *report*, not a verdict on the
    change: whether orphaning four rungs is acceptable is a judgement, and the
    place that judgement is recorded is the human gate, not this field. Only the
    one condition that makes the merge itself incoherent — a name collision —
    is raised to ``ERROR``, by the caller that found it.
    """
    if findings.affected or findings.unresolved:
        return ValidationSeverity.WARNING
    return ValidationSeverity.INFORMATION


def _persist(
    db: Session,
    *,
    project: Project,
    object_type: ObjectType,
    object_id: uuid.UUID | None,
    object_label: str,
    change_description: str,
    findings: _Findings,
    summary: str,
    severity: ValidationSeverity,
    changeset: ChangeSet | None,
    operation: ChangeOperation | None,
) -> ImpactReport:
    """Write the report row.

    The report is persisted rather than returned as a value object because it is
    evidence: a rename is approved *on the basis of* an impact report, and "the
    report the reviewer saw" has to still exist afterwards for that approval to
    mean anything.
    """
    affected = findings.affected
    unresolved = findings.unresolved
    report = ImpactReport(
        project_id=project.id,
        change_set_id=changeset.id if changeset is not None else None,
        operation_id=operation.id if operation is not None else None,
        object_type=object_type,
        object_id=object_id,
        object_label=_bounded(object_label),
        change_description=change_description,
        affected_counts=findings.discipline_counts(),
        affected_objects=[hit.to_json() for hit in affected],
        unresolved_references=[hit.to_json() for hit in unresolved],
        highest_severity=severity,
        # True unless nothing at all is affected. Nothing means nothing: an
        # unresolved reference is still a reference, and it is the one most
        # likely to break quietly, so it keeps a human in the loop by itself.
        requires_human_approval=bool(affected or unresolved),
        summary=summary,
    )
    db.add(report)
    db.flush()
    return report


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def analyse_rename(
    db: Session,
    *,
    project: Project,
    tag: Tag,
    new_name: str,
    changeset: ChangeSet | None = None,
    operation: ChangeOperation | None = None,
) -> ImpactReport:
    """What renaming ``tag`` to ``new_name`` would reach.

    Sweeps logic, HMI, alarms, tests, interlocks, I/O and operand documentation
    for both the resolved foreign keys and the names written as text, checks the
    new name for a collision, and records the result as an
    :class:`~app.models.revision.ImpactReport`.
    """
    intended = new_name.strip()
    if not intended:
        raise ValidationError("A rename impact analysis needs the name the tag would take.")

    pattern = _reference_pattern(tag.name)
    findings = _Findings()
    _sweep_text_references(db, project=project, tag=tag, pattern=pattern, findings=findings)
    _sweep_logic(db, project=project, tag=tag, pattern=pattern, findings=findings)
    _sweep_io(db, tag=tag, findings=findings)

    extra: list[str] = []
    severity = _severity(findings)
    collision = _check_collision(db, project=project, tag=tag, new_name=intended)
    if collision is not None:
        collision_label = registry.label_for(collision)
        # Recorded as an affected object, not only as a severity. A rename onto
        # an existing name with no other references would otherwise report zero
        # affected objects, and ``requires_human_approval`` would fall to False
        # on the single change that most needs a human.
        findings.add(
            _Hit(
                object_type=ObjectType.TAG.value,
                object_id=collision.id,
                label=_bounded(collision_label),
                discipline="logic",
                kind="colliding_tag",
                effect="already carries the name this rename would take",
                fields={"name"},
                references={collision.name},
            )
        )
        severity = ValidationSeverity.ERROR
        extra.append(
            f"{intended} is already the name of {collision_label} in this scope; "
            "the merge would leave one name pointing at two tags."
        )

    summary = _summarise(f"Renaming {tag.name} to {intended}", findings, extra=extra)
    return _persist(
        db,
        project=project,
        object_type=ObjectType.TAG,
        object_id=tag.id,
        object_label=registry.label_for(tag),
        change_description=f"Rename {tag.name} to {intended}.",
        findings=findings,
        summary=summary,
        severity=severity,
        changeset=changeset,
        operation=operation,
    )


def analyse_delete(
    db: Session,
    *,
    project: Project,
    object_type: ObjectType,
    object_id: uuid.UUID,
    changeset: ChangeSet | None = None,
) -> ImpactReport:
    """What deleting one object would leave dangling.

    For a tag this is the same sweep a rename gets — a deleted tag orphans every
    reference a renamed one would have carried, and the text references are the
    ones a foreign-key sweep alone would miss. For every other object type the
    sweep is the inbound foreign keys, derived from the registry.

    Severity stays at ``WARNING`` even when the delete would orphan a great deal.
    The report describes consequences; whether they are acceptable is the human
    gate's decision, and ``requires_human_approval`` is what carries it.
    """
    findings = _Findings()
    label = registry.describe(object_type, object_id=object_id)

    if object_type is ObjectType.TAG:
        tag = db.get(Tag, object_id)
        if tag is not None:
            label = registry.label_for(tag)
            pattern = _reference_pattern(tag.name)
            _sweep_text_references(db, project=project, tag=tag, pattern=pattern, findings=findings)
            _sweep_logic(db, project=project, tag=tag, pattern=pattern, findings=findings)
            _sweep_io(db, tag=tag, findings=findings)
    elif registry.is_writable(object_type):
        target = db.get(registry.model_for(object_type), object_id)
        if target is not None:
            label = registry.label_for(target)

    # Run regardless of type and after the tag sweeps: it finds the references
    # held purely as a foreign key — an alias row pointing at the tag, a routine
    # inside a program — and anything it re-finds folds into the existing entry.
    _sweep_inbound_foreign_keys(
        db, project=project, object_type=object_type, object_id=object_id, findings=findings
    )

    summary = _summarise(f"Deleting {label}", findings)
    return _persist(
        db,
        project=project,
        object_type=object_type,
        object_id=object_id,
        object_label=label,
        change_description=f"Delete {label}.",
        findings=findings,
        summary=summary,
        severity=_severity(findings),
        changeset=changeset,
        operation=None,
    )


def analyse_changeset(db: Session, *, changeset: ChangeSet) -> list[ImpactReport]:
    """One report per operation in ``changeset`` that renames or deletes.

    An operation whose target cannot be loaded is skipped rather than reported
    against: a missing object is a merge conflict with a name of its own, and
    inventing an impact report for it would put a second, less precise account
    of the same defect in front of the reviewer.
    """
    project = db.get(Project, changeset.project_id)
    if project is None:  # pragma: no cover - a change set cannot outlive its project
        return []

    reports: list[ImpactReport] = []
    for operation in changeset.operations:
        new_name = _renamed_to(operation)
        if new_name is not None:
            tag = db.get(Tag, operation.object_id) if operation.object_id is not None else None
            if tag is None:
                continue
            reports.append(
                analyse_rename(
                    db,
                    project=project,
                    tag=tag,
                    new_name=new_name,
                    changeset=changeset,
                    operation=operation,
                )
            )
            continue

        if operation.operation_type is ChangeOperationType.DELETE_OBJECT:
            if operation.object_id is None or not registry.is_writable(operation.object_type):
                continue
            report = analyse_delete(
                db,
                project=project,
                object_type=operation.object_type,
                object_id=operation.object_id,
                changeset=changeset,
            )
            # ``analyse_delete`` takes no operation — it is callable for an
            # object nobody has proposed changing yet — so the link back to the
            # operation is made here, where there is one.
            report.operation_id = operation.id
            db.flush()
            reports.append(report)

    return reports


def _renamed_to(operation: ChangeOperation) -> str | None:
    """The new name an operation would give a tag, whatever shape it arrived in.

    ``RENAME_TAG`` is the declared form, but an ``UPDATE_FIELD`` writing ``name``
    on a tag renames it just as thoroughly and ``name`` is not a protected
    column. Treating only the declared form as a rename would let the operation
    that skips the impact report be the easier one to write.
    """
    values = proposed_values(operation)
    if operation.operation_type is ChangeOperationType.RENAME_TAG:
        to_name = str(values.get("to_name", "")).strip()
        return to_name or None
    if (
        operation.operation_type is ChangeOperationType.UPDATE_FIELD
        and operation.object_type is ObjectType.TAG
        and operation.field_name == "name"
    ):
        to_name = str(values.get("name", "")).strip()
        return to_name or None
    return None
