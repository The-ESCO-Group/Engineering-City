"""What a change operation may name, and what it may write.

Every operation that reaches the merge arrives as ``(object_type, values)`` — a
string and a bag of JSON. This module is where that stops being untyped input:
it resolves the type to an ORM class, decides which of that class's columns a
proposal is allowed to set, coerces the JSON into real Python values, and
produces a label a human can follow back to the thing being changed.

Three deliberate shapes:

* **The field allowlist is a denylist applied to the mapper.** It is derived as
  "every mapped column except :data:`PROTECTED_FIELDS` and the object-specific
  :data:`FIELD_WRITE_RESTRICTIONS`". The service-owned columns can never become
  writable by accident, while a newly introduced field whose invariants are not
  yet enforced can be held fail-closed without freezing every ordinary model
  addition.
* **Natural keys come from the table's unique constraints**, so duplicate
  detection tracks the schema rather than a hand-maintained copy of it. It is
  also stricter than the database: ``NULL != NULL`` means a unique constraint
  over a nullable column does not catch a duplicate pair of NULLs, and the check
  here does.
* **Labels are mandatory, not decorative.** A conflict that says "object
  ``9f2c…`` changed" leaves a reviewer with nothing to decide, so every object
  type resolves to something like ``Tag MTR_101_Cmd_Run`` or
  ``MainProgram / Conveyor_Interlocks / Rung 12``.

:data:`ObjectType.FINDING` is deliberately absent. ``Finding`` predates the
canonical model, carries none of its versioning columns, and is written through
the ``create_finding`` action instead — mapping it here would let an operation
into the merge that conflict detection could not reason about.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import datetime
from enum import Enum
from typing import Any, cast

from sqlalchemy import Boolean, Float, Integer, String, Table, Text, UniqueConstraint
from sqlalchemy import inspect as sa_inspect

from app.core.clock import ensure_utc
from app.core.enums import ObjectType
from app.database.base import Base
from app.database.types import GUID, JSONDict, JSONList, StrEnumType, UTCDateTime
from app.models import (
    AcceptanceCriterion,
    AddressAssignment,
    AlarmDefinition,
    AOIDefinitionReference,
    Area,
    Assumption,
    CauseEffectEntry,
    Clarification,
    ControllerTask,
    DataTypeDefinition,
    DesignConstraint,
    Device,
    DeviceType,
    EquipmentModule,
    ExportArtifact,
    FaultDefinition,
    HardwareModule,
    HMIBinding,
    HMIScreen,
    Interlock,
    IOPoint,
    LogicInstruction,
    LogicNetwork,
    LogicRung,
    ModeDefinition,
    NetworkNode,
    Permissive,
    PlantUnit,
    Program,
    ProjectDecision,
    ProjectProfile,
    ProjectQuestion,
    ReleaseCandidate,
    Requirement,
    ResetBehavior,
    Routine,
    SequenceStep,
    Signal,
    StateDefinition,
    Tag,
    TagAlias,
    TagGroup,
    TestCase,
    TransitionDefinition,
    UDTDefinition,
)

__all__ = [
    "FIELD_WRITE_RESTRICTIONS",
    "OBJECT_TYPE_MODELS",
    "PROTECTED_FIELDS",
    "UNWRITABLE_OBJECT_TYPES",
    "FieldValueError",
    "UnknownObjectTypeError",
    "allowed_fields",
    "coerce_value",
    "describe",
    "field_write_restriction",
    "foreign_key_targets",
    "is_writable",
    "label_for",
    "model_for",
    "natural_keys",
    "object_type_for",
    "restricted_fields",
    "type_label",
]


class UnknownObjectTypeError(LookupError):
    """The object type has no ORM class a change operation may write."""


class FieldValueError(ValueError):
    """A proposed value cannot be stored in the column it names."""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field
        self.message = message


#: Columns the service layer owns outright.
#:
#: Named individually rather than derived, because each one is a rule: an agent
#: that could write ``approval_state`` could approve its own work, one that could
#: write ``object_revision`` could defeat conflict detection, and one that could
#: write ``created_by_agent`` could attribute its output to somebody else.
#: ``approved_object_revision`` is the same rule once more: it is the revision a
#: reviewer signed, so an agent that could move it forward would silence the
#: integrity check on an object it had just edited — which is worse than being
#: unable to edit the object at all, because the signature would survive looking
#: intact. ``deleted_at`` is here because deletion is an operation type, not a
#: field edit — a soft delete has to bump the revision and record a before-state.
PROTECTED_FIELDS: frozenset[str] = frozenset(
    {
        "id",
        "project_id",
        "object_revision",
        "created_in_revision",
        "updated_in_revision",
        "approval_state",
        "approved_at",
        "approved_by_id",
        "approved_object_revision",
        "deleted_at",
        "superseded_by_id",
        "origin",
        "source_changeset_id",
        "created_by_agent",
        "created_by_id",
        "created_at",
        "updated_at",
    }
)


#: Object-specific fields that exist in the canonical model but are not yet a
#: safe change-set surface. Keeping these separate from ``PROTECTED_FIELDS``
#: matters: they are not service-owned metadata, and another object's column
#: with the same name must not become unwritable by accident.
#:
#: ``LogicInstruction.branch_id`` is intentionally held closed while canonical
#: import and Rockwell generation still use ``branch_level``/``branch_index``.
#: ``LogicBranch`` is not itself a registered change-set object, so the generic
#: foreign-key checker cannot prove that a proposed leg belongs to the same
#: project *and* the instruction's rung. Allowing the id before those checks
#: exist would let a client attach an instruction to an unrelated branch row.
FIELD_WRITE_RESTRICTIONS: Mapping[ObjectType, Mapping[str, str]] = {
    ObjectType.LOGIC_INSTRUCTION: {
        "branch_id": (
            "branch membership is not writable until canonical import and generation "
            "validate branch identity; use branch_level and branch_index coordinates"
        )
    }
}


#: The canonical object each :class:`ObjectType` addresses.
OBJECT_TYPE_MODELS: Mapping[ObjectType, type[Base]] = {
    ObjectType.PROJECT_PROFILE: ProjectProfile,
    ObjectType.PROJECT_QUESTION: ProjectQuestion,
    ObjectType.PROJECT_DECISION: ProjectDecision,
    ObjectType.REQUIREMENT: Requirement,
    ObjectType.ACCEPTANCE_CRITERION: AcceptanceCriterion,
    ObjectType.ASSUMPTION: Assumption,
    ObjectType.CONSTRAINT: DesignConstraint,
    ObjectType.CLARIFICATION: Clarification,
    ObjectType.AREA: Area,
    ObjectType.UNIT: PlantUnit,
    ObjectType.EQUIPMENT_MODULE: EquipmentModule,
    ObjectType.DEVICE: Device,
    ObjectType.DEVICE_TYPE: DeviceType,
    ObjectType.NETWORK_NODE: NetworkNode,
    ObjectType.MODULE: HardwareModule,
    ObjectType.IO_POINT: IOPoint,
    ObjectType.SIGNAL: Signal,
    ObjectType.ADDRESS_ASSIGNMENT: AddressAssignment,
    ObjectType.TAG: Tag,
    ObjectType.TAG_ALIAS: TagAlias,
    ObjectType.TAG_GROUP: TagGroup,
    ObjectType.DATA_TYPE_DEFINITION: DataTypeDefinition,
    ObjectType.UDT_DEFINITION: UDTDefinition,
    ObjectType.AOI_REFERENCE: AOIDefinitionReference,
    ObjectType.CONTROLLER_TASK: ControllerTask,
    ObjectType.PROGRAM: Program,
    ObjectType.ROUTINE: Routine,
    ObjectType.LOGIC_NETWORK: LogicNetwork,
    ObjectType.LOGIC_RUNG: LogicRung,
    ObjectType.LOGIC_INSTRUCTION: LogicInstruction,
    ObjectType.INTERLOCK: Interlock,
    ObjectType.PERMISSIVE: Permissive,
    ObjectType.CAUSE_EFFECT_ENTRY: CauseEffectEntry,
    ObjectType.MODE_DEFINITION: ModeDefinition,
    ObjectType.STATE_DEFINITION: StateDefinition,
    ObjectType.TRANSITION_DEFINITION: TransitionDefinition,
    ObjectType.SEQUENCE_STEP: SequenceStep,
    ObjectType.FAULT_DEFINITION: FaultDefinition,
    ObjectType.RESET_BEHAVIOR: ResetBehavior,
    ObjectType.HMI_SCREEN: HMIScreen,
    ObjectType.HMI_BINDING: HMIBinding,
    ObjectType.ALARM_DEFINITION: AlarmDefinition,
    ObjectType.TEST_CASE: TestCase,
    ObjectType.EXPORT_ARTIFACT: ExportArtifact,
    ObjectType.RELEASE_CANDIDATE: ReleaseCandidate,
}

#: Object types a change set may never write, with the reason a caller can show.
UNWRITABLE_OBJECT_TYPES: Mapping[ObjectType, str] = {
    ObjectType.FINDING: (
        "findings are part of the review layer, carry no object_revision, and are "
        "created through the create_finding action rather than through a change set"
    ),
}

#: Reverse map, so an ORM instance can name its own object type.
_TYPE_BY_CLASS: Mapping[type[Any], ObjectType] = {
    model: object_type for object_type, model in OBJECT_TYPE_MODELS.items()
}

#: Table name to object type, used to decide whether a foreign key points at
#: something this layer is able to check the existence of.
_TYPE_BY_TABLE: Mapping[str, ObjectType] = {
    model.__tablename__: object_type for object_type, model in OBJECT_TYPE_MODELS.items()
}


#: Human name per object type. The first word of every conflict message.
_TYPE_LABELS: Mapping[ObjectType, str] = {
    ObjectType.PROJECT_PROFILE: "Project profile",
    ObjectType.PROJECT_QUESTION: "Question",
    ObjectType.PROJECT_DECISION: "Decision",
    ObjectType.REQUIREMENT: "Requirement",
    ObjectType.ACCEPTANCE_CRITERION: "Acceptance criterion",
    ObjectType.ASSUMPTION: "Assumption",
    ObjectType.CONSTRAINT: "Constraint",
    ObjectType.CLARIFICATION: "Clarification",
    ObjectType.AREA: "Area",
    ObjectType.UNIT: "Unit",
    ObjectType.EQUIPMENT_MODULE: "Equipment module",
    ObjectType.DEVICE: "Device",
    ObjectType.DEVICE_TYPE: "Device type",
    ObjectType.NETWORK_NODE: "Network node",
    ObjectType.MODULE: "Module",
    ObjectType.IO_POINT: "I/O point",
    ObjectType.SIGNAL: "Signal",
    ObjectType.ADDRESS_ASSIGNMENT: "Address assignment",
    ObjectType.TAG: "Tag",
    ObjectType.TAG_ALIAS: "Tag alias",
    ObjectType.TAG_GROUP: "Tag group",
    ObjectType.DATA_TYPE_DEFINITION: "Data type",
    ObjectType.UDT_DEFINITION: "UDT",
    ObjectType.AOI_REFERENCE: "Add-On Instruction",
    ObjectType.CONTROLLER_TASK: "Controller task",
    ObjectType.PROGRAM: "Program",
    ObjectType.ROUTINE: "Routine",
    ObjectType.LOGIC_NETWORK: "Logic network",
    ObjectType.LOGIC_RUNG: "Rung",
    ObjectType.LOGIC_INSTRUCTION: "Instruction",
    ObjectType.INTERLOCK: "Interlock",
    ObjectType.PERMISSIVE: "Permissive",
    ObjectType.CAUSE_EFFECT_ENTRY: "Cause/effect entry",
    ObjectType.MODE_DEFINITION: "Mode",
    ObjectType.STATE_DEFINITION: "State",
    ObjectType.TRANSITION_DEFINITION: "Transition",
    ObjectType.SEQUENCE_STEP: "Sequence step",
    ObjectType.FAULT_DEFINITION: "Fault",
    ObjectType.RESET_BEHAVIOR: "Reset behaviour",
    ObjectType.HMI_SCREEN: "HMI screen",
    ObjectType.HMI_BINDING: "HMI binding",
    ObjectType.ALARM_DEFINITION: "Alarm",
    ObjectType.TEST_CASE: "Test case",
    ObjectType.FINDING: "Finding",
    ObjectType.EXPORT_ARTIFACT: "Export artifact",
    ObjectType.RELEASE_CANDIDATE: "Release candidate",
}

#: Tried in order for the identifying half of a generic label.
_NAME_FIELDS: tuple[str, ...] = (
    "name",
    "reference",
    "code",
    "key",
    "address",
    "tag_prefix",
    "title",
    "relative_path",
    "property_name",
)


def type_label(object_type: ObjectType) -> str:
    """The human name of an object type, e.g. ``"I/O point"``."""
    return _TYPE_LABELS.get(object_type, object_type.value)


def is_writable(object_type: ObjectType) -> bool:
    """Whether a change operation may target this object type at all."""
    return object_type in OBJECT_TYPE_MODELS


def model_for(object_type: ObjectType) -> type[Base]:
    """The ORM class an object type addresses.

    Raises :class:`UnknownObjectTypeError` with the reason when the type is not
    writable, so the caller can turn it into a validation error a human can act
    on rather than a bare ``KeyError``.
    """
    try:
        return OBJECT_TYPE_MODELS[object_type]
    except KeyError:
        reason = UNWRITABLE_OBJECT_TYPES.get(
            object_type, "no canonical object is registered for this type"
        )
        raise UnknownObjectTypeError(
            f"object type {object_type.value!r} cannot be written by a change set: {reason}"
        ) from None


def object_type_for(obj: object) -> ObjectType | None:
    """The object type of an ORM instance, or ``None`` if it is not canonical."""
    return _TYPE_BY_CLASS.get(type(obj))


def _columns(object_type: ObjectType) -> dict[str, Any]:
    mapper = sa_inspect(model_for(object_type))
    return {column.key: column for column in mapper.columns}


def allowed_fields(object_type: ObjectType) -> frozenset[str]:
    """The columns a change operation may set on this object type.

    Every mapped column minus :data:`PROTECTED_FIELDS` and any field held closed
    by :data:`FIELD_WRITE_RESTRICTIONS`.
    """
    return frozenset(_columns(object_type)) - PROTECTED_FIELDS - restricted_fields(object_type)


def restricted_fields(object_type: ObjectType) -> frozenset[str]:
    """Mapped fields deliberately excluded from this object's write surface."""
    return frozenset(FIELD_WRITE_RESTRICTIONS.get(object_type, {}))


def field_write_restriction(object_type: ObjectType, field: str) -> str:
    """Why ``field`` is held closed for ``object_type``, or an empty string."""
    return FIELD_WRITE_RESTRICTIONS.get(object_type, {}).get(field, "")


def required_fields(object_type: ObjectType) -> frozenset[str]:
    """Columns a create must supply because nothing else will.

    Checked before anything is written, so a proposal that forgot a tag's name
    is a validation error naming the field rather than an integrity error
    halfway through applying a batch.
    """
    required: set[str] = set()
    held_closed = restricted_fields(object_type)
    for key, column in _columns(object_type).items():
        if key in PROTECTED_FIELDS or key in held_closed or column.primary_key:
            continue
        if column.nullable or column.default is not None or column.server_default is not None:
            continue
        required.add(key)
    return frozenset(required)


def natural_keys(object_type: ObjectType) -> tuple[tuple[str, ...], ...]:
    """Attribute groups that must be unique, read off the table's constraints.

    Returned as mapper attribute names rather than column names so a caller can
    build a query from them without a second lookup.
    """
    model = model_for(object_type)
    mapper = sa_inspect(model)
    table = cast(Table, model.__table__)
    attribute_of = {column: key for key, column in mapper.columns.items()}
    return tuple(
        tuple(attribute_of[column] for column in constraint.columns if column in attribute_of)
        for constraint in sorted(table.constraints, key=lambda c: str(c.name or ""))
        if isinstance(constraint, UniqueConstraint)
    )


def column_default(object_type: ObjectType, field: str) -> Any:
    """The scalar default for a column, or ``None`` if it has no static one.

    Used when checking a natural key: a tag created without an explicit
    ``scope`` still lands on ``TagScope.CONTROLLER``, and a duplicate check that
    ignored that would miss the collision it exists to catch.
    """
    column = _columns(object_type).get(field)
    if column is None or column.default is None:
        return None
    arg = getattr(column.default, "arg", None)
    return None if callable(arg) else arg


def foreign_key_targets(object_type: ObjectType) -> Mapping[str, ObjectType]:
    """Columns that reference another canonical object, and which type.

    Foreign keys onto non-canonical tables (``users``, ``uploaded_files``) are
    omitted: this map exists so the merge can check that a reference resolves,
    and it only makes that claim where it can actually check.
    """
    targets: dict[str, ObjectType] = {}
    for key, column in _columns(object_type).items():
        for foreign_key in column.foreign_keys:
            referenced = _TYPE_BY_TABLE.get(foreign_key.column.table.name)
            if referenced is not None:
                targets[key] = referenced
    return targets


# ---------------------------------------------------------------------------
# Value coercion
# ---------------------------------------------------------------------------
def coerce_value(object_type: ObjectType, field: str, value: Any) -> Any:
    """Turn a JSON value into what the column actually stores.

    Strict on purpose. A proposal that puts a dictionary in a text column, or a
    string that is not a member of an enum, is a defect worth naming at merge
    time rather than a row that fails to mean anything later.
    """
    column = _columns(object_type).get(field)
    if column is None:  # pragma: no cover - callers check the allowlist first
        raise FieldValueError(field, f"{field!r} is not a column of {object_type.value}")

    if value is None:
        if not column.nullable:
            raise FieldValueError(field, f"{field!r} cannot be null")
        return None

    column_type = column.type
    try:
        if isinstance(column_type, GUID):
            return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
        if isinstance(column_type, StrEnumType):
            return column_type.enum_cls(value)
        if isinstance(column_type, UTCDateTime):
            parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
            return ensure_utc(parsed)
        if isinstance(column_type, JSONDict):
            if not isinstance(value, dict):
                raise FieldValueError(field, f"{field!r} expects an object, got {_kind(value)}")
            return value
        if isinstance(column_type, JSONList):
            if not isinstance(value, list):
                raise FieldValueError(field, f"{field!r} expects a list, got {_kind(value)}")
            return value
        if isinstance(column_type, Boolean):
            if not isinstance(value, bool):
                raise FieldValueError(field, f"{field!r} expects true or false, got {_kind(value)}")
            return value
        if isinstance(column_type, Integer):
            if isinstance(value, bool) or not isinstance(value, int):
                raise FieldValueError(field, f"{field!r} expects an integer, got {_kind(value)}")
            return value
        if isinstance(column_type, Float):
            if isinstance(value, bool) or not isinstance(value, int | float):
                raise FieldValueError(field, f"{field!r} expects a number, got {_kind(value)}")
            return float(value)
        if isinstance(column_type, String | Text):
            if not isinstance(value, str):
                raise FieldValueError(field, f"{field!r} expects text, got {_kind(value)}")
            length = getattr(column_type, "length", None)
            if length and len(value) > length:
                raise FieldValueError(
                    field, f"{field!r} is {len(value)} characters; the column holds {length}"
                )
            return value
    except FieldValueError:
        raise
    except (TypeError, ValueError) as exc:
        raise FieldValueError(field, f"{field!r} rejected the proposed value: {exc}") from exc
    return value


def _kind(value: Any) -> str:
    return type(value).__name__


def jsonable(value: Any) -> Any:
    """Render a stored value so it survives a JSON column on any backend.

    Enums are unwrapped before the ``str`` check, not after. Every enum in this
    domain is a ``StrEnum`` and therefore *is* a string, so leaving it wrapped
    would put ``<TagScope.CONTROLLER: 'controller'>`` in front of a reviewer
    where ``'controller'`` was meant.
    """
    if isinstance(value, Enum):
        return jsonable(value.value)
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime):
        return ensure_utc(value).isoformat().replace("+00:00", "Z")
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, list | tuple | set):
        return [jsonable(item) for item in value]
    return str(value)


# ---------------------------------------------------------------------------
# Labels
# ---------------------------------------------------------------------------
def label_for(obj: object) -> str:
    """A label a human can follow back to the object.

    Never raises and never returns an empty string: this is called from failure
    paths, and a conflict report that blew up while naming the object would be
    the worst possible time to find out.
    """
    object_type = object_type_for(obj)
    if object_type is None:
        return type(obj).__name__

    try:
        special = _SPECIAL_LABELS.get(object_type)
        if special is not None:
            rendered = special(obj)
            if rendered:
                return rendered
        return _generic_label(object_type, obj)
    except Exception:  # pragma: no cover - defensive; a label must never fail
        return describe(object_type, object_id=getattr(obj, "id", None))


def describe(
    object_type: ObjectType,
    obj: object | None = None,
    object_id: uuid.UUID | None = None,
) -> str:
    """Label an object that may not exist, e.g. a missing update target."""
    if obj is not None:
        return label_for(obj)
    if object_id is None:
        return f"a new {type_label(object_type).lower()}"
    return f"{type_label(object_type)} {object_id}"


def _generic_label(object_type: ObjectType, obj: object) -> str:
    for field in _NAME_FIELDS:
        value = getattr(obj, field, None)
        if isinstance(value, str) and value:
            return f"{type_label(object_type)} {value}"
    ordinal = getattr(obj, "ordinal", None)
    if isinstance(ordinal, int):
        return f"{type_label(object_type)} #{ordinal}"
    return describe(object_type, object_id=getattr(obj, "id", None))


def _rung_path(rung: Any) -> str:
    routine = getattr(rung, "routine", None)
    program = getattr(routine, "program", None) if routine is not None else None
    parts = [
        getattr(program, "name", "") or "",
        getattr(routine, "name", "") or "",
        f"Rung {rung.ordinal}",
    ]
    return " / ".join(part for part in parts if part)


def _routine_label(routine: Any) -> str:
    program = getattr(routine, "program", None)
    program_name = getattr(program, "name", "") or ""
    name = routine.name or ""
    joined = " / ".join(part for part in (program_name, name) if part)
    return f"Routine {joined}" if joined else ""


def _instruction_label(instruction: Any) -> str:
    rung = getattr(instruction, "rung", None)
    prefix = _rung_path(rung) if rung is not None else ""
    body = f"{instruction.mnemonic or '?'} #{instruction.ordinal}"
    return f"{prefix} / {body}" if prefix else f"Instruction {body}"


def _network_label(network: Any) -> str:
    routine = getattr(network, "routine", None)
    routine_name = getattr(routine, "name", "") or ""
    joined = " / ".join(part for part in (routine_name, network.name or "") if part)
    return f"Logic network {joined}" if joined else ""


def _signal_label(signal: Any) -> str:
    device = getattr(signal, "device", None)
    prefix = getattr(device, "tag_prefix", "") or ""
    return f"Signal {prefix}.{signal.name}" if prefix else f"Signal {signal.name}"


def _profile_label(profile: Any) -> str:
    name = getattr(profile, "project_name", "") or ""
    return f"Project profile {name}" if name else "Project profile"


#: Types whose useful label is a path rather than a single name. Everything else
#: falls through to ``"<Type> <name>"``.
_SPECIAL_LABELS: Mapping[ObjectType, Any] = {
    ObjectType.LOGIC_RUNG: _rung_path,
    ObjectType.ROUTINE: _routine_label,
    ObjectType.LOGIC_INSTRUCTION: _instruction_label,
    ObjectType.LOGIC_NETWORK: _network_label,
    ObjectType.SIGNAL: _signal_label,
    ObjectType.PROJECT_PROFILE: _profile_label,
}
