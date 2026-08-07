"""Import parser facts into the canonical engineering model.

This is deliberately a narrow source importer, not a design pass.  It consumes
the latest :class:`~app.models.file.ParsedArtifact` for each live upload and
copies only values the L5X or I/O-list parser actually recorded.  It never
derives safety classifications, fail states, device identities, or tag names
from naming conventions.

Rows created here carry ``LogicOrigin.PARSED_FROM_SOURCE``.  A later import may
refresh such a row, but it will not edit a deleted, approved, generated, agent-
proposed, or human-authored row that occupies the same canonical key.  That
rule is what makes this service safe to call more than once and safe to place
beside work already performed by an engineer.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.enums import (
    AddressAssignmentStatus,
    AlarmSeverity,
    BaseDataType,
    ControllerTaskType,
    DeviceCategory,
    DocumentRole,
    FailState,
    FileKind,
    InstructionCategory,
    LogicElementKind,
    LogicOrigin,
    ModuleRole,
    OperandKind,
    RoutineLanguage,
    SignalDirection,
    SignalType,
    TagRole,
    TagScope,
)
from app.generators.plc.instructions import (
    is_restricted,
    resolved_destructive_operands,
    restriction_reason,
    spec_for,
)
from app.models.core import EngineeringObjectMixin
from app.models.delivery import (
    AlarmDefinition,
    AlarmMessage,
    HMIBinding,
    HMIObjectInstance,
    HMIScreen,
)
from app.models.file import ParsedArtifact, UploadedFile
from app.models.interlocks import Interlock
from app.models.plant import AddressAssignment, Device, HardwareModule, IOPoint, Signal
from app.models.plc import (
    ControllerModel,
    ControllerTask,
    LogicInstruction,
    LogicNetwork,
    LogicOperand,
    LogicRung,
    Program,
    Routine,
)
from app.models.project import Project
from app.models.tags import (
    AOIDefinitionReference,
    DataTypeDefinition,
    Tag,
    UDTDefinition,
    UDTMember,
)

_IMPORT_ATTRIBUTE = "canonical_source_import"

_INTEGER = re.compile(r"^[+-]?\d+$")
_FLOAT = re.compile(r"^[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?$")
_RADIX = re.compile(r"^[+-]?\d*#[0-9A-Fa-f_.]+$")
_TAG_OPERAND = re.compile(
    r"^(?:Program:(?P<program>[A-Za-z_][A-Za-z0-9_]*)\.)?"
    r"(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?:[.\[].*)?$"
)
_FACTORYTALK_REFERENCE = re.compile(r"^\{::\[[^\]]+\](?P<reference>[^}]+)\}$")

_TASK_TYPES = {
    "CONTINUOUS": ControllerTaskType.CONTINUOUS,
    "PERIODIC": ControllerTaskType.PERIODIC,
    "EVENT": ControllerTaskType.EVENT,
}
_ROUTINE_LANGUAGES = {
    "RLL": RoutineLanguage.LADDER,
    "LADDER": RoutineLanguage.LADDER,
    "ST": RoutineLanguage.STRUCTURED_TEXT,
    "STRUCTURED_TEXT": RoutineLanguage.STRUCTURED_TEXT,
    "FBD": RoutineLanguage.FUNCTION_BLOCK,
    "FUNCTION_BLOCK": RoutineLanguage.FUNCTION_BLOCK,
    "SFC": RoutineLanguage.SEQUENTIAL_FUNCTION_CHART,
    "SEQUENTIAL_FUNCTION_CHART": RoutineLanguage.SEQUENTIAL_FUNCTION_CHART,
}
_ATOMIC_TYPES = {
    item.value.upper(): item
    for item in BaseDataType
    if item
    not in {
        BaseDataType.UDT,
        BaseDataType.ADDON_INSTRUCTION,
        BaseDataType.UNKNOWN,
    }
}

_IO_HEADER_ALIASES: dict[str, tuple[str, ...]] = {
    "tag": ("tag", "tag name", "device tag"),
    "description": ("description", "service", "point description"),
    "signal_type": ("type", "io type", "i o type", "signal type", "point type"),
    "module": ("module", "card", "rack"),
    "module_catalog": ("module catalog", "catalog number", "catalog no"),
    "slot": ("slot", "module slot"),
    "channel": ("channel", "point channel"),
    "address": ("address", "io address", "i o address", "terminal"),
    "units": ("engineering units", "units", "uom"),
    "device": ("device", "device name", "equipment", "asset"),
    "device_prefix": ("device tag prefix", "tag prefix", "device id", "equipment id"),
    "device_category": ("device category", "equipment category"),
    "signal": ("signal", "signal name", "point name", "io signal", "i o signal"),
    "fail_state": ("fail state", "failure state"),
    "safety_related": ("safety related", "safety", "is safety related"),
    "required": ("required", "is required"),
    "status": ("status", "point status"),
}

#: ``reference`` reads ``Alarm Tag`` as the alarm's identity rather than as its
#: trigger, which is the reading :mod:`app.core.services.evidence_graph` already
#: takes of the same column: a register of that shape names the trigger in a
#: separate ``Source Tag``.
_ALARM_HEADER_ALIASES: dict[str, tuple[str, ...]] = {
    "reference": (
        "alarm id",
        "alarm reference",
        "alarm number",
        "alarm no",
        "alarm tag",
        "reference",
        "id",
        "alarm",
        "tag",
    ),
    "name": ("alarm name", "name", "alarm title", "title"),
    "message": ("message", "alarm message", "operator message", "text", "description"),
    "operator_guidance": (
        "operator response",
        "operator guidance",
        "operator action",
        "response",
    ),
    "severity": ("severity", "alarm severity", "priority", "alarm priority"),
    "group": ("group", "alarm group", "area", "category"),
    "alarm_class": ("alarm class", "class"),
    "trigger": ("trigger tag", "trigger", "trigger tag name", "input tag", "source tag"),
    "trigger_type": ("trigger type", "alarm type", "condition type"),
    "limit_value": ("limit", "limit value", "setpoint", "set point", "threshold", "trip point"),
    "deadband": ("deadband", "dead band", "hysteresis"),
    "delay_seconds": ("delay", "on delay", "delay seconds", "on delay seconds"),
    #: Recognised so the column is not reported as one nothing here understands,
    #: and then deliberately not written — see :func:`_import_alarm_csv`.
    "latched": ("latched", "latch", "is latched"),
    "requires_acknowledge": (
        "ack required",
        "acknowledge required",
        "requires acknowledge",
        "acknowledgement required",
    ),
    "message_key": ("message key",),
}

#: Alarm-list severity words this importer will read, and nothing else.
#: :attr:`~app.models.delivery.AlarmDefinition.severity` states how fast an
#: operator has to respond, so an unrecognised word is reported rather than
#: bent onto the nearest member.  ``critical`` maps to ``URGENT`` rather than
#: ``HIGH`` because a register that uses both words orders them, and collapsing
#: the two into one member would lose that order — the only property anything
#: downstream reads severity for.
_ALARM_SEVERITIES: dict[str, AlarmSeverity] = {
    "diagnostic": AlarmSeverity.DIAGNOSTIC,
    "diagnostics": AlarmSeverity.DIAGNOSTIC,
    "low": AlarmSeverity.LOW,
    "medium": AlarmSeverity.MEDIUM,
    "med": AlarmSeverity.MEDIUM,
    "high": AlarmSeverity.HIGH,
    "urgent": AlarmSeverity.URGENT,
    "critical": AlarmSeverity.URGENT,
}

_AFFIRMATIVE = {"true", "yes", "y", "1"}
_NEGATIVE = {"false", "no", "n", "0"}


@dataclass(frozen=True, slots=True)
class CanonicalImportIssue:
    """One source fact that could not safely become or update a canonical row."""

    code: str
    message: str
    artifact_id: uuid.UUID
    source_key: str = ""


@dataclass(slots=True)
class CanonicalImportResult:
    """Deterministic accounting for one import pass."""

    project_id: uuid.UUID
    artifact_ids: list[uuid.UUID] = field(default_factory=list)
    created: dict[str, int] = field(default_factory=dict)
    updated: dict[str, int] = field(default_factory=dict)
    issues: list[CanonicalImportIssue] = field(default_factory=list)

    @property
    def created_count(self) -> int:
        return sum(self.created.values())

    @property
    def updated_count(self) -> int:
        return sum(self.updated.values())


@dataclass(slots=True)
class _Context:
    db: Session
    project: Project
    file: UploadedFile
    artifact: ParsedArtifact
    result: CanonicalImportResult

    def issue(self, code: str, message: str, source_key: str = "") -> None:
        self.result.issues.append(
            CanonicalImportIssue(
                code=code,
                message=message,
                artifact_id=self.artifact.id,
                source_key=source_key,
            )
        )

    def count(self, bucket: str, model_name: str) -> None:
        target = self.result.created if bucket == "created" else self.result.updated
        target[model_name] = target.get(model_name, 0) + 1


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalised = value.strip().lower()
        if normalised == "true":
            return True
        if normalised == "false":
            return False
    return None


def _flag(value: Any) -> bool | None:
    """``_bool`` widened to the words a schedule column actually uses.

    ``None`` for anything else, because these values feed columns where ``False``
    is a positive engineering claim rather than an absence.
    """
    result = _bool(value)
    if result is not None:
        return result
    normalised = _text(value).lower()
    if normalised in _AFFIRMATIVE:
        return True
    if normalised in _NEGATIVE:
        return False
    return None


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    text = _text(value)
    return int(text) if _INTEGER.fullmatch(text) else None


def _float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = _text(value)
    return float(text) if _FLOAT.fullmatch(text) else None


def _normalise(value: str) -> str:
    return " ".join("".join(char.lower() if char.isalnum() else " " for char in value).split())


def _record_keys(content: dict[str, Any]) -> list[str]:
    """The keys each parsed CSV row is actually keyed by, in column order.

    The parser keeps ``headers`` verbatim and derives ``column_names`` from it —
    trimming whitespace, naming a blank heading by position and suffixing a
    repeat — because a duplicate key would drop a whole column out of every
    record.  Reading the verbatim headers would then look up names no record
    holds, so the derived names are what a header map has to be built from.
    """
    names = content.get("column_names")
    if isinstance(names, list) and names:
        return [str(name) for name in names if name is not None]
    headers = content.get("headers")
    if isinstance(headers, list):
        return [str(header) for header in headers if header is not None]
    return []


def _resolve_headers(keys: Sequence[str], aliases: dict[str, tuple[str, ...]]) -> dict[str, str]:
    """Map canonical field names onto the columns this source actually has."""

    available = {_normalise(key): key for key in keys}
    resolved: dict[str, str] = {}
    claimed: set[str] = set()
    for field_name, field_aliases in aliases.items():
        for alias in field_aliases:
            key = _normalise(alias)
            if key in available and key not in claimed:
                resolved[field_name] = available[key]
                claimed.add(key)
                break
    return resolved


def _io_header_map(keys: Sequence[str]) -> dict[str, str]:
    resolved = _resolve_headers(keys, _IO_HEADER_ALIASES)

    # ``Device Tag`` is intentionally shared.  Many real I/O schedules use one
    # column as both the controller tag and the only explicit identity for the
    # field device/signal on that row.  Treating the header as a tag *only*
    # discarded the plant side of an otherwise complete schedule: no Device,
    # Signal or AddressAssignment rows were created, so the devices/I-O/tags
    # approval gate could never become ready.  Sharing is safe here because the
    # source explicitly calls the value a device tag; no identity is derived
    # from a naming convention.  More-specific Device/Signal columns still win.
    device_tag = next((key for key in keys if _normalise(key) == _normalise("device tag")), None)
    if device_tag is not None:
        resolved.setdefault("tag", device_tag)
        resolved.setdefault("device", device_tag)
        resolved.setdefault("device_prefix", device_tag)
        resolved.setdefault("signal", device_tag)
    return resolved


def _report_unplaced_columns(
    ctx: _Context,
    keys: Sequence[str],
    resolved: dict[str, str],
    subject: str,
) -> None:
    """Record every column of a tabular source this importer cannot store.

    A column nobody placed is not nothing.  It is the mechanism by which a whole
    document goes unread with no trace: the file parses, rows appear, counts look
    right, and a statement the customer wrote down never reaches the model or the
    review.  Naming each dropped column here is what lets a report say which part
    of the schedule was examined and which part was not.
    """
    placed = set(resolved.values())
    for key in keys:
        if key in placed:
            continue
        ctx.issue(
            "unplaced_source_column",
            f"Column {key!r} of this {subject} matches no field of the canonical model, so "
            "its values were not imported and nothing downstream can cite them.",
            f"column:{key}",
        )


def _value(record: dict[str, Any], headers: dict[str, str], field_name: str) -> str:
    header = headers.get(field_name)
    return _text(record.get(header)) if header is not None else ""


def _source_document(ctx: _Context, source_key: str) -> dict[str, Any]:
    return {
        "artifact_id": str(ctx.artifact.id),
        "file_id": str(ctx.file.id),
        "filename": ctx.file.original_filename,
        "parser_name": ctx.artifact.parser_name,
        "parser_version": ctx.artifact.parser_version,
        "source_key": source_key,
    }


def _with_provenance(
    current: dict[str, Any] | None, ctx: _Context, source_key: str
) -> dict[str, Any]:
    attributes = dict(current or {})
    raw_import = attributes.get(_IMPORT_ATTRIBUTE)
    import_data = dict(raw_import) if isinstance(raw_import, dict) else {}
    raw_sources = import_data.get("sources")
    sources = dict(raw_sources) if isinstance(raw_sources, dict) else {}
    # A canonical object can be named by several rows in one source (an I/O
    # module is the common case).  Keying by both file and source location keeps
    # every citation and, importantly, prevents repeated imports from cycling a
    # single provenance value through each row on every pass.
    sources[f"{ctx.file.id}:{source_key}"] = _source_document(ctx, source_key)
    import_data["sources"] = sources
    attributes[_IMPORT_ATTRIBUTE] = import_data
    return attributes


def _occupant[T: EngineeringObjectMixin](
    rows: Sequence[T], ctx: _Context, source_key: str
) -> tuple[T | None, bool]:
    """Return a mutable source row, or report that another row owns the key."""

    if not rows:
        return None, False
    live_rows = [row for row in rows if row.deleted_at is None]
    blockers = [
        row
        for row in live_rows
        if row.origin is not LogicOrigin.PARSED_FROM_SOURCE or row.is_protected
    ]
    if not blockers:
        for row in live_rows:
            if row.origin is LogicOrigin.PARSED_FROM_SOURCE and not row.is_protected:
                return row, True
    owner = blockers[0] if blockers else rows[0]
    ctx.issue(
        "canonical_key_owned",
        f"{type(owner).__name__} {owner.__dict__.get('id')} already owns this canonical key; "
        "source import did not alter it.",
        source_key,
    )
    return None, True


def _create[T: EngineeringObjectMixin](ctx: _Context, row: T, source_key: str) -> T:
    row.origin = LogicOrigin.PARSED_FROM_SOURCE
    row.created_in_revision = ctx.project.current_revision_number
    row.updated_in_revision = ctx.project.current_revision_number
    row.attributes = _with_provenance({}, ctx, source_key)
    ctx.db.add(row)
    ctx.db.flush()
    ctx.count("created", type(row).__name__)
    return row


def _update[T: EngineeringObjectMixin](
    ctx: _Context, row: T, values: dict[str, Any], source_key: str
) -> T:
    changed = False
    for name, value in values.items():
        if getattr(row, name) != value:
            setattr(row, name, value)
            changed = True
    attributes = _with_provenance(row.attributes, ctx, source_key)
    if attributes != row.attributes:
        row.attributes = attributes
        changed = True
    if changed:
        row.object_revision += 1
        row.updated_in_revision = ctx.project.current_revision_number
        ctx.count("updated", type(row).__name__)
    return row


def _import_rank(record: UploadedFile) -> int:
    """Which pass a document belongs to, so a citation is never read too early.

    A document that only *cites* tags — an alarm register, an HMI export — has to
    be imported after every document that *declares* them, or the same files
    import differently depending on the order the uploads happened to be
    timestamped in.  The failure that shape produces is subtle: the first pass
    records an unresolved trigger, a later pass finds the declaration and
    "updates" a row nothing in the source changed, so the import stops being
    idempotent and a revision gains an edit with no author.
    """
    if record.file_kind is FileKind.L5X:
        return 0
    if record.file_kind is FileKind.CSV and record.document_role is DocumentRole.IO_LIST:
        return 1
    return 2


def _latest_inputs(db: Session, project_id: uuid.UUID) -> list[tuple[UploadedFile, ParsedArtifact]]:
    files = list(
        db.execute(
            select(UploadedFile)
            .where(UploadedFile.project_id == project_id, UploadedFile.deleted_at.is_(None))
            .order_by(
                UploadedFile.created_at,
                UploadedFile.original_filename,
                UploadedFile.id,
            )
        ).scalars()
    )
    latest: dict[uuid.UUID, ParsedArtifact] = {}
    for artifact in db.execute(
        select(ParsedArtifact)
        .where(ParsedArtifact.project_id == project_id)
        .order_by(ParsedArtifact.created_at, ParsedArtifact.id)
    ).scalars():
        latest[artifact.file_id] = artifact
    return [
        (record, latest[record.id])
        for record in sorted(
            (item for item in files if item.id in latest),
            key=lambda item: (
                _import_rank(item),
                item.created_at,
                item.original_filename,
                item.id,
            ),
        )
    ]


def import_latest_parsed_artifacts(db: Session, project_id: uuid.UUID) -> CanonicalImportResult:
    """Import current L5X, I/O-list, alarm-register and HMI-export facts.

    The caller owns the surrounding transaction.  This function flushes so all
    returned rows and relationships have stable identifiers, but never commits.
    Documents are imported in :func:`_import_rank` order, not upload order.
    """

    project = db.get(Project, project_id)
    if project is None:
        raise ValueError(f"Project {project_id} does not exist.")

    result = CanonicalImportResult(project_id=project_id)
    for record, artifact in _latest_inputs(db, project_id):
        ctx = _Context(db=db, project=project, file=record, artifact=artifact, result=result)
        if artifact.artifact_kind == FileKind.L5X.value and record.file_kind is FileKind.L5X:
            result.artifact_ids.append(artifact.id)
            _import_l5x(ctx)
        elif (
            artifact.artifact_kind == FileKind.CSV.value
            and record.file_kind is FileKind.CSV
            and record.document_role is DocumentRole.IO_LIST
        ):
            result.artifact_ids.append(artifact.id)
            _import_io_csv(ctx)
        elif (
            artifact.artifact_kind == FileKind.CSV.value
            and record.file_kind is FileKind.CSV
            and record.document_role is DocumentRole.ALARM_LIST
        ):
            result.artifact_ids.append(artifact.id)
            _import_alarm_csv(ctx)
        elif (
            artifact.artifact_kind == FileKind.XML.value
            and record.file_kind is FileKind.XML
            and record.document_role is DocumentRole.HMI_EXPORT
        ):
            result.artifact_ids.append(artifact.id)
            _import_hmi_xml(ctx)
    db.flush()
    return result


def _import_hmi_xml(ctx: _Context) -> None:
    """Import explicit FactoryTalk-style display and connection facts.

    The generic XML parser deliberately preserves product-specific elements
    instead of pretending every XML document is an HMI export.  The intake role
    supplies that missing context here.  Only explicit ``Display`` elements and
    ``Connection`` children are imported; a graphic with no declared binding is
    not assigned one, and a reference that resolves to no canonical tag remains
    an unresolved binding for validation to report.
    """
    elements = _as_list(_as_dict(ctx.artifact.content).get("elements"))
    by_path = {
        _text(element.get("xpath")): element for element in elements if _text(element.get("xpath"))
    }
    display_elements = [
        element for element in elements if _normalise(_text(element.get("tag"))) == "display"
    ]
    screens: list[tuple[str, HMIScreen]] = []
    for fallback_ordinal, element in enumerate(display_elements):
        path = _text(element.get("xpath"))
        attributes = _as_dict(element.get("attributes"))
        name = _text(attributes.get("Name") or attributes.get("name"))
        if not name or len(name) > 60:
            ctx.issue(
                "invalid_hmi_screen_identity",
                "HMI Display name is missing or exceeds the canonical 60-character key limit.",
                path,
            )
            continue
        description = _text(_as_dict(by_path.get(f"{path}/Description")).get("text"))
        screen_rows = list(
            ctx.db.execute(
                select(HMIScreen)
                .where(HMIScreen.project_id == ctx.project.id, HMIScreen.key == name)
                .order_by(HMIScreen.created_at, HMIScreen.id)
            ).scalars()
        )
        existing, occupied = _occupant(screen_rows, ctx, path)
        values = {
            "name": name[:120],
            "title": name[:200],
            "description": description,
            "screen_type": _text(attributes.get("Type") or attributes.get("type"))[:40],
            "width": _int(attributes.get("Width") or attributes.get("width")),
            "height": _int(attributes.get("Height") or attributes.get("height")),
            "ordinal": _int(attributes.get("Number") or attributes.get("number"))
            or fallback_ordinal,
        }
        if existing is not None:
            screen = _update(ctx, existing, values, path)
        elif occupied:
            screen = screen_rows[0]
        else:
            screen = _create(
                ctx,
                HMIScreen(project_id=ctx.project.id, key=name, **values),
                path,
            )
        if screen.deleted_at is None:
            screens.append((path, screen))

    all_tags = list(
        ctx.db.execute(
            select(Tag)
            .where(Tag.project_id == ctx.project.id, Tag.deleted_at.is_(None))
            .order_by(Tag.created_at, Tag.id)
        ).scalars()
    )
    programs = list(
        ctx.db.execute(
            select(Program)
            .where(Program.project_id == ctx.project.id, Program.deleted_at.is_(None))
            .order_by(Program.created_at, Program.id)
        ).scalars()
    )
    for display_path, screen in screens:
        connections = [
            element
            for element in elements
            if _normalise(_text(element.get("tag"))) == "connection"
            and _text(element.get("xpath")).startswith(f"{display_path}/")
        ]
        object_ordinals: dict[str, int] = {}
        objects: dict[str, HMIObjectInstance] = {}
        for connection in connections:
            connection_path = _text(connection.get("xpath"))
            object_path = connection_path.rsplit("/", 1)[0]
            object_source = _as_dict(by_path.get(object_path))
            object_attributes = _as_dict(object_source.get("attributes"))
            object_key = _text(object_attributes.get("Name") or object_attributes.get("name"))
            if not object_key:
                # A screen-level connection is representable without inventing
                # a graphic object.  The binding below keeps object_instance_id
                # null while remaining attributable to this screen.
                object_instance: HMIObjectInstance | None = None
                object_type = ""
            elif len(object_key) > 60:
                ctx.issue(
                    "invalid_hmi_object_identity",
                    "HMI graphic object name exceeds the canonical 60-character key limit.",
                    object_path,
                )
                object_instance = None
                object_type = _text(object_source.get("tag"))[:60]
            else:
                object_type = _text(object_source.get("tag"))[:60]
                object_instance = objects.get(object_path)
                if object_instance is None:
                    object_rows = list(
                        ctx.db.execute(
                            select(HMIObjectInstance)
                            .where(
                                HMIObjectInstance.screen_id == screen.id,
                                HMIObjectInstance.key == object_key,
                            )
                            .order_by(HMIObjectInstance.created_at, HMIObjectInstance.id)
                        ).scalars()
                    )
                    object_existing, object_occupied = _occupant(object_rows, ctx, object_path)
                    label = _text(_as_dict(by_path.get(f"{object_path}/Caption")).get("text"))
                    ordinal = object_ordinals.get(display_path, 0)
                    object_ordinals[display_path] = ordinal + 1
                    values = {
                        "object_type": object_type,
                        "label": label[:120],
                        "x": _int(object_attributes.get("Left") or object_attributes.get("left")),
                        "y": _int(object_attributes.get("Top") or object_attributes.get("top")),
                        "width": _int(
                            object_attributes.get("Width") or object_attributes.get("width")
                        ),
                        "height": _int(
                            object_attributes.get("Height") or object_attributes.get("height")
                        ),
                        "ordinal": ordinal,
                    }
                    if object_existing is not None:
                        object_instance = _update(ctx, object_existing, values, object_path)
                    elif object_occupied:
                        object_instance = object_rows[0]
                    else:
                        object_instance = _create(
                            ctx,
                            HMIObjectInstance(
                                project_id=ctx.project.id,
                                screen_id=screen.id,
                                key=object_key,
                                **values,
                            ),
                            object_path,
                        )
                    objects[object_path] = object_instance

            attributes = _as_dict(connection.get("attributes"))
            expression = _text(attributes.get("Expression") or attributes.get("expression"))
            property_name = _text(attributes.get("Name") or attributes.get("name"))[:60]
            reference = _hmi_tag_reference(expression)
            tag = _resolve_tag_reference(reference, all_tags, programs)
            access = _hmi_access(object_type)
            binding_rows = list(
                ctx.db.execute(
                    select(HMIBinding)
                    .where(
                        HMIBinding.project_id == ctx.project.id,
                        HMIBinding.screen_id == screen.id,
                        HMIBinding.object_instance_id
                        == (object_instance.id if object_instance is not None else None),
                        HMIBinding.property_name == property_name,
                    )
                    .order_by(HMIBinding.created_at, HMIBinding.id)
                ).scalars()
            )
            binding_existing, binding_occupied = _occupant(binding_rows, ctx, connection_path)
            binding_values: dict[str, Any] = {
                "tag_name": reference[:120],
                "tag_id": tag.id if tag is not None else None,
                "is_resolved": tag is not None,
                "access": access,
                "expression": expression[:300],
                "unresolved_reason": (
                    "" if tag is not None else "No unique canonical tag matches this HMI reference."
                ),
            }
            if binding_existing is not None:
                _update(ctx, binding_existing, binding_values, connection_path)
            elif not binding_occupied:
                _create(
                    ctx,
                    HMIBinding(
                        project_id=ctx.project.id,
                        screen_id=screen.id,
                        object_instance_id=(
                            object_instance.id if object_instance is not None else None
                        ),
                        property_name=property_name,
                        **binding_values,
                    ),
                    connection_path,
                )


def _hmi_tag_reference(expression: str) -> str:
    match = _FACTORYTALK_REFERENCE.fullmatch(expression.strip())
    return match.group("reference").strip() if match is not None else ""


def _resolve_tag_reference(
    reference: str,
    tags: list[Tag],
    programs: list[Program],
) -> Tag | None:
    """The one canonical tag a written reference names, or ``None``.

    Shared by the HMI export and the alarm register because both cite a tag the
    same way — optionally program-qualified, optionally down a member or element
    path — and both have to fail the same way. An ambiguous reference resolves to
    nothing rather than to a first match: a binding or a trigger attached to the
    wrong tag reads as verified and is worse than one left unresolved.
    """
    if not reference:
        return None
    program_id: uuid.UUID | None = None
    name = reference
    if reference.casefold().startswith("program:") and "." in reference:
        program_name, name = reference[8:].split(".", 1)
        matching_programs = [
            program for program in programs if program.name.casefold() == program_name.casefold()
        ]
        if len(matching_programs) != 1:
            return None
        program_id = matching_programs[0].id
    base_name = name.split(".", 1)[0].split("[", 1)[0]
    candidates = [
        tag
        for tag in tags
        if tag.name.casefold() == base_name.casefold()
        and (
            (program_id is None and tag.scope is TagScope.CONTROLLER)
            or (program_id is not None and tag.scope_program_id == program_id)
        )
    ]
    return candidates[0] if len(candidates) == 1 else None


def _hmi_access(object_type: str) -> str:
    folded = object_type.casefold()
    if any(marker in folded for marker in ("button", "input", "entry", "selector")):
        return "write"
    if any(marker in folded for marker in ("indicator", "display", "gauge", "trend")):
        return "read"
    return ""


def _import_l5x(ctx: _Context) -> None:
    content = _as_dict(ctx.artifact.content)
    _import_controller(ctx, _as_dict(content.get("controller")))
    udts = _import_udts(ctx, _as_list(content.get("datatypes")))
    aois = _import_aois(ctx, _as_list(content.get("addon_instructions")))
    modules = _import_l5x_modules(ctx, _as_list(content.get("modules")))
    programs = _import_controller_structure(
        ctx,
        _as_list(content.get("tasks")),
        _as_list(content.get("programs")),
        _as_list(content.get("rungs")),
    )
    # Before the rungs, so a rung is created already inside its network rather
    # than created loose and then edited to join one — which would report every
    # first import as having updated logic nothing had touched.
    networks = _import_logic_networks(ctx, programs)
    tags = _import_l5x_tags(ctx, _as_list(content.get("tags")), programs, udts, aois)
    _import_l5x_logic(ctx, _as_list(content.get("rungs")), programs, tags, networks)
    _link_module_parents(ctx, modules, _as_list(content.get("modules")))


def _import_controller(ctx: _Context, source: dict[str, Any]) -> ControllerModel | None:
    name = _text(source.get("name"))
    if not name or len(name) > 60:
        ctx.issue(
            "invalid_controller_name",
            "L5X controller name is missing or too long.",
            "controller",
        )
        return None
    rows = list(
        ctx.db.execute(
            select(ControllerModel)
            .where(ControllerModel.project_id == ctx.project.id)
            .order_by(ControllerModel.created_at, ControllerModel.id)
        ).scalars()
    )
    existing, occupied = _occupant(rows, ctx, "controller")
    values = {
        "name": name,
        "catalog_number": _text(source.get("processor_type"))[:60],
        "major_revision": _int(source.get("major_rev")),
        "minor_revision": _int(source.get("minor_rev")),
        "description": _text(source.get("description")),
    }
    if existing is not None:
        return _update(ctx, existing, values, "controller")
    if occupied:
        return rows[0] if rows[0].deleted_at is None else None
    return _create(ctx, ControllerModel(project_id=ctx.project.id, **values), "controller")


def _base_type(
    data_type: str, udt_names: set[str], aoi_names: set[str]
) -> tuple[BaseDataType, str]:
    canonical = data_type.strip().upper()
    atomic = _ATOMIC_TYPES.get(canonical)
    if atomic is not None:
        return atomic, ""
    if canonical in udt_names:
        return BaseDataType.UDT, data_type
    if canonical in aoi_names:
        return BaseDataType.ADDON_INSTRUCTION, data_type
    return BaseDataType.UNKNOWN, data_type


def _import_udts(ctx: _Context, sources: list[dict[str, Any]]) -> dict[str, UDTDefinition]:
    imported: dict[str, UDTDefinition] = {}
    # Resolve member references against the complete declaration set, including
    # forward references to a UDT that appears later in the source document.
    source_udt_names = {
        _text(source.get("name")).upper() for source in sources if _text(source.get("name"))
    }
    for source in sources:
        name = _text(source.get("name"))
        key = f"udt:{name}"
        if not name or len(name) > 80:
            ctx.issue("invalid_udt_name", "UDT name is missing or too long.", key)
            continue
        rows = list(
            ctx.db.execute(
                select(UDTDefinition)
                .where(UDTDefinition.project_id == ctx.project.id, UDTDefinition.name == name)
                .order_by(UDTDefinition.created_at, UDTDefinition.id)
            ).scalars()
        )
        existing, occupied = _occupant(rows, ctx, key)
        values = {
            "description": _text(source.get("description")),
            "family": _text(source.get("family"))[:40],
            "member_count": len(_as_list(source.get("members"))),
        }
        writable = True
        if existing is not None:
            udt = _update(ctx, existing, values, key)
        elif occupied:
            udt = rows[0]
            writable = False
        else:
            udt = _create(
                ctx,
                UDTDefinition(project_id=ctx.project.id, name=name, **values),
                key,
            )
        if udt.deleted_at is None:
            imported[name.upper()] = udt
        if not writable:
            continue

        definition_rows = list(
            ctx.db.execute(
                select(DataTypeDefinition)
                .where(
                    DataTypeDefinition.project_id == ctx.project.id,
                    DataTypeDefinition.name == name,
                )
                .order_by(DataTypeDefinition.created_at, DataTypeDefinition.id)
            ).scalars()
        )
        definition, definition_occupied = _occupant(definition_rows, ctx, f"datatype:{name}")
        definition_values = {
            "base_data_type": BaseDataType.UDT,
            "is_builtin": False,
            "description": _text(source.get("description")),
            "source": "l5x_import",
        }
        if definition is not None:
            _update(ctx, definition, definition_values, f"datatype:{name}")
        elif not definition_occupied:
            _create(
                ctx,
                DataTypeDefinition(
                    project_id=ctx.project.id,
                    name=name,
                    **definition_values,
                ),
                f"datatype:{name}",
            )

        member_sources = _as_list(source.get("members"))
        for ordinal, member_source in enumerate(member_sources):
            member_name = _text(member_source.get("name"))
            member_key = f"{key}/member:{member_name}"
            if not member_name or len(member_name) > 80:
                ctx.issue(
                    "invalid_udt_member_name",
                    "UDT member name is missing or too long.",
                    member_key,
                )
                continue
            member_rows = list(
                ctx.db.execute(
                    select(UDTMember)
                    .where(UDTMember.udt_id == udt.id, UDTMember.name == member_name)
                    .order_by(UDTMember.created_at, UDTMember.id)
                ).scalars()
            )
            member, member_occupied = _occupant(member_rows, ctx, member_key)
            raw_type = _text(member_source.get("data_type"))
            base, concrete = _base_type(raw_type, source_udt_names, set())
            dimension = member_source.get("dimension")
            member_values = {
                "base_data_type": base,
                "data_type_name": concrete[:80],
                "dimensions": "" if dimension is None else _text(dimension)[:40],
                "ordinal": ordinal,
                "description": _text(member_source.get("description")),
            }
            if member is not None:
                _update(ctx, member, member_values, member_key)
            elif not member_occupied:
                _create(
                    ctx,
                    UDTMember(
                        project_id=ctx.project.id,
                        udt_id=udt.id,
                        name=member_name,
                        **member_values,
                    ),
                    member_key,
                )
    return imported


def _import_aois(ctx: _Context, sources: list[dict[str, Any]]) -> dict[str, AOIDefinitionReference]:
    imported: dict[str, AOIDefinitionReference] = {}
    for source in sources:
        name = _text(source.get("name"))
        key = f"aoi:{name}"
        if not name or len(name) > 80:
            ctx.issue("invalid_aoi_name", "AOI name is missing or too long.", key)
            continue
        rows = list(
            ctx.db.execute(
                select(AOIDefinitionReference)
                .where(
                    AOIDefinitionReference.project_id == ctx.project.id,
                    AOIDefinitionReference.name == name,
                )
                .order_by(AOIDefinitionReference.created_at, AOIDefinitionReference.id)
            ).scalars()
        )
        existing, occupied = _occupant(rows, ctx, key)
        parameters = _as_list(source.get("parameters"))
        values = {
            "revision": _text(source.get("revision"))[:20],
            "description": _text(source.get("description")),
            "parameter_count": len(parameters),
            "parameters": parameters,
            "is_available": True,
            "availability_note": "Definition present in the parsed L5X source.",
            "source_file_id": ctx.file.id,
        }
        if existing is not None:
            row = _update(ctx, existing, values, key)
        elif occupied:
            row = rows[0]
        else:
            row = _create(
                ctx,
                AOIDefinitionReference(project_id=ctx.project.id, name=name, **values),
                key,
            )
        if row.deleted_at is None:
            imported[name.upper()] = row
    return imported


def _import_l5x_modules(ctx: _Context, sources: list[dict[str, Any]]) -> dict[str, HardwareModule]:
    imported: dict[str, HardwareModule] = {}
    for source in sources:
        name = _text(source.get("name"))
        key = f"module:{name}"
        if not name or len(name) > 60:
            ctx.issue("invalid_module_name", "Module name is missing or too long.", key)
            continue
        rows = list(
            ctx.db.execute(
                select(HardwareModule)
                .where(HardwareModule.project_id == ctx.project.id, HardwareModule.name == name)
                .order_by(HardwareModule.created_at, HardwareModule.id)
            ).scalars()
        )
        existing, occupied = _occupant(rows, ctx, key)
        values = {
            "catalog_number": _text(source.get("catalog_number"))[:60],
            "role": ModuleRole.OTHER,
            "description": _text(source.get("description")),
        }
        if existing is not None:
            row = _update(ctx, existing, values, key)
        elif occupied:
            row = rows[0]
        else:
            row = _create(
                ctx,
                HardwareModule(project_id=ctx.project.id, name=name, **values),
                key,
            )
        if row.deleted_at is None:
            imported[name] = row
    return imported


def _link_module_parents(
    ctx: _Context,
    modules: dict[str, HardwareModule],
    sources: list[dict[str, Any]],
) -> None:
    for source in sources:
        name = _text(source.get("name"))
        parent_name = _text(source.get("parent_module"))
        row = modules.get(name)
        parent = modules.get(parent_name)
        if (
            row is None
            or parent is None
            or row.id == parent.id
            or row.origin is not LogicOrigin.PARSED_FROM_SOURCE
            or row.is_protected
        ):
            continue
        _update(ctx, row, {"parent_module_id": parent.id}, f"module:{name}/parent")


def _import_controller_structure(
    ctx: _Context,
    task_sources: list[dict[str, Any]],
    program_sources: list[dict[str, Any]],
    rung_sources: list[dict[str, Any]],
) -> dict[str, tuple[Program, dict[str, Routine]]]:
    tasks: dict[str, ControllerTask] = {}
    scheduled: dict[str, list[ControllerTask]] = {}
    for ordinal, source in enumerate(task_sources):
        name = _text(source.get("name"))
        key = f"task:{name}"
        task_type = _TASK_TYPES.get(_text(source.get("type")).upper())
        if not name or len(name) > 60 or task_type is None:
            ctx.issue(
                "invalid_controller_task",
                "Task requires a source name and an explicit Continuous, Periodic, or Event type.",
                key,
            )
            continue
        task_rows = list(
            ctx.db.execute(
                select(ControllerTask)
                .where(ControllerTask.project_id == ctx.project.id, ControllerTask.name == name)
                .order_by(ControllerTask.created_at, ControllerTask.id)
            ).scalars()
        )
        task_existing, task_occupied = _occupant(task_rows, ctx, key)
        task_values = {
            "task_type": task_type,
            "priority": _int(source.get("priority")),
            "watchdog_ms": _float(source.get("watchdog")),
            "ordinal": ordinal,
        }
        if task_existing is not None:
            task_row = _update(ctx, task_existing, task_values, key)
        elif task_occupied:
            task_row = task_rows[0]
        else:
            task_row = _create(
                ctx,
                ControllerTask(project_id=ctx.project.id, name=name, **task_values),
                key,
            )
        if task_row.deleted_at is None:
            tasks[name] = task_row
            for program_name in source.get("scheduled_programs", []):
                explicit = _text(program_name)
                if explicit:
                    scheduled.setdefault(explicit, []).append(task_row)

    programs: dict[str, tuple[Program, dict[str, Routine]]] = {}
    rungs_by_routine: dict[tuple[str, str], int] = {}
    called: set[tuple[str, str]] = set()
    for rung_source in rung_sources:
        program_name = _text(rung_source.get("program"))
        routine_name = _text(rung_source.get("routine"))
        rungs_by_routine[(program_name, routine_name)] = (
            rungs_by_routine.get((program_name, routine_name), 0) + 1
        )
        for instruction in _as_list(rung_source.get("instructions")):
            if _text(instruction.get("mnemonic")).upper() not in {"JSR", "JXR"}:
                continue
            operands = instruction.get("operands")
            if isinstance(operands, list) and operands:
                target = _text(operands[0])
                if target:
                    called.add((program_name, target))

    for program_ordinal, source in enumerate(program_sources):
        name = _text(source.get("name"))
        key = f"program:{name}"
        if not name or len(name) > 60:
            ctx.issue("invalid_program_name", "Program name is missing or too long.", key)
            continue
        matches = scheduled.get(name, [])
        task_id: uuid.UUID | None = None
        if len(matches) == 1:
            task_id = matches[0].id
        elif len(matches) > 1:
            ctx.issue(
                "ambiguous_task_schedule",
                f"Program {name!r} is explicitly scheduled by more than one task.",
                key,
            )
        program_rows = list(
            ctx.db.execute(
                select(Program)
                .where(Program.project_id == ctx.project.id, Program.name == name)
                .order_by(Program.created_at, Program.id)
            ).scalars()
        )
        program_existing, program_occupied = _occupant(program_rows, ctx, key)
        program_values: dict[str, Any] = {
            "controller_task_id": task_id,
            "description": _text(source.get("description")),
            "main_routine_name": _text(source.get("main_routine"))[:60],
            "ordinal": program_ordinal,
        }
        disabled = _bool(source.get("disabled"))
        if disabled is not None:
            program_values["disabled"] = disabled
        writable = True
        if program_existing is not None:
            program = _update(ctx, program_existing, program_values, key)
        elif program_occupied:
            program = program_rows[0]
            writable = False
        else:
            program = _create(
                ctx,
                Program(project_id=ctx.project.id, name=name, **program_values),
                key,
            )
        if program.deleted_at is not None or not writable:
            continue

        routines: dict[str, Routine] = {}
        for routine_ordinal, routine_source in enumerate(_as_list(source.get("routines"))):
            routine_name = _text(routine_source.get("name"))
            routine_key = f"routine:{name}/{routine_name}"
            language = _ROUTINE_LANGUAGES.get(_text(routine_source.get("type")).upper())
            if not routine_name or len(routine_name) > 60 or language is None:
                ctx.issue(
                    "invalid_routine",
                    "Routine requires a source name and an explicitly supported language.",
                    routine_key,
                )
                continue
            routine_rows = list(
                ctx.db.execute(
                    select(Routine)
                    .where(Routine.program_id == program.id, Routine.name == routine_name)
                    .order_by(Routine.created_at, Routine.id)
                ).scalars()
            )
            routine, routine_occupied = _occupant(routine_rows, ctx, routine_key)
            routine_values = {
                "language": language,
                "description": _text(routine_source.get("description")),
                "ordinal": routine_ordinal,
                "is_main": routine_name == _text(source.get("main_routine")),
                "is_called": (name, routine_name) in called,
                "rung_count": rungs_by_routine.get((name, routine_name), 0),
            }
            if routine is not None:
                routine = _update(ctx, routine, routine_values, routine_key)
            elif not routine_occupied:
                routine = _create(
                    ctx,
                    Routine(
                        project_id=ctx.project.id,
                        program_id=program.id,
                        name=routine_name,
                        **routine_values,
                    ),
                    routine_key,
                )
            else:
                continue
            routines[routine_name] = routine
        programs[name] = (program, routines)
    return programs


def _requirement_claims(ctx: _Context) -> dict[uuid.UUID, set[str]]:
    """Which requirements each routine is already recorded as implementing.

    Read off the model rather than worked out here.  :class:`Interlock` carries
    both the requirement a documented protection traces to and the rung an
    implementation search located, and ``has_implementation`` is the model's own
    rule for when that pair may be believed — ``is_implemented`` without a rung
    behind it is a claim rather than evidence for one.  Nothing in this function
    decides that a rung implements a requirement; it asks only which routine the
    rung somebody already named is in.
    """
    claims: dict[uuid.UUID, set[str]] = {}
    for interlock in ctx.db.execute(
        select(Interlock).where(
            Interlock.project_id == ctx.project.id,
            Interlock.deleted_at.is_(None),
            Interlock.requirement_reference != "",
        )
    ).scalars():
        if not interlock.has_implementation:
            continue
        rung = ctx.db.get(LogicRung, interlock.implementing_rung_id)
        if rung is None or rung.deleted_at is not None:
            continue
        claims.setdefault(rung.routine_id, set()).add(interlock.requirement_reference)
    return claims


def _import_logic_networks(
    ctx: _Context,
    programs: dict[str, tuple[Program, dict[str, Routine]]],
) -> dict[tuple[str, str], LogicNetwork]:
    """Record the one group of rungs each imported routine actually evidences.

    A network is the unit a template expands into, so generated logic arrives
    already grouped and parsed logic does not: between a ``<Routine>`` and its
    ``<Rung>`` elements the export states nothing at all.  Rung comments do name
    the interlock a rung belongs to, and cutting a routine up by reading them
    would be parsing text back into structure — the direction
    :mod:`app.models.plc` exists to forbid — so the blocks would be this
    importer's invention rather than the customer's export.

    The boundary is therefore the routine, and it is knowingly coarser than the
    ladder: a routine usually holds several unrelated patterns, so this records
    one group where a reviewer reading it would see three.  Of the two answers
    available that is the honest one, because the alternative is not a finer
    grouping but a guessed one.  ``template_id`` and ``template_version`` stay
    empty for the same reason — these rungs were expanded from no template, and
    naming the project's controller template here would assert that they were.
    """
    claims = _requirement_claims(ctx)
    imported: dict[tuple[str, str], LogicNetwork] = {}
    for program_name, (_, routines) in programs.items():
        for routine in routines.values():
            if routine.rung_count < 1:
                # No rungs in the source means no group of rungs to name.  A
                # routine the export declares and leaves empty is a fact about
                # the controller worth keeping, not a network.
                continue
            # The parsed anchor for the routine.  The network is exactly that
            # routine's rungs, so the routine element is what cites it.
            key = f"routine:{program_name}/{routine.name}"
            references = claims.get(routine.id, set())
            if len(references) > 1:
                ctx.issue(
                    "ambiguous_network_requirement",
                    f"Routine {routine.name!r} holds the implementing rung of "
                    f"{len(references)} documented requirements "
                    f"({', '.join(sorted(references))}). The routine is the only rung "
                    "grouping the export states, so its network records none of them.",
                    key,
                )
            rows = list(
                ctx.db.execute(
                    select(LogicNetwork)
                    .where(
                        LogicNetwork.routine_id == routine.id,
                        LogicNetwork.name == routine.name,
                    )
                    .order_by(LogicNetwork.created_at, LogicNetwork.id)
                ).scalars()
            )
            existing, occupied = _occupant(rows, ctx, key)
            values: dict[str, Any] = {
                # The routine's own description: the network is coextensive with
                # the routine, so this is the one line the export offers about
                # what these rungs are for.  Empty when it offers none.
                "purpose": routine.description[:200],
                # A routine holds exactly one imported network, so it is the
                # first.  Ordering only becomes a question once a template adds
                # a second one beside it.
                "ordinal": 0,
                "rung_count": routine.rung_count,
                # One reference or none.  Two documented requirements implemented
                # inside a single routine cannot both belong to a network that is
                # the whole routine, and picking between them is a judgement the
                # source does not support — so the network claims neither and the
                # provider goes on asking about both.
                "requirement_reference": next(iter(references)) if len(references) == 1 else "",
            }
            if existing is not None:
                network = _update(ctx, existing, values, key)
            elif occupied:
                continue
            else:
                network = _create(
                    ctx,
                    LogicNetwork(
                        project_id=ctx.project.id,
                        routine_id=routine.id,
                        name=routine.name,
                        **values,
                    ),
                    key,
                )
            if network.deleted_at is None:
                imported[(program_name, routine.name)] = network
    return imported


def _import_l5x_tags(
    ctx: _Context,
    sources: list[dict[str, Any]],
    programs: dict[str, tuple[Program, dict[str, Routine]]],
    udts: dict[str, UDTDefinition],
    aois: dict[str, AOIDefinitionReference],
) -> dict[tuple[str, str, str], Tag]:
    imported: dict[tuple[str, str, str], Tag] = {}
    udt_names = set(udts)
    aoi_names = set(aois)
    for source in sources:
        name = _text(source.get("name"))
        scope_text = _text(source.get("scope")).lower()
        program_name = _text(source.get("program"))
        key = f"tag:{scope_text}/{program_name}/{name}"
        if not name or len(name) > 80:
            ctx.issue("invalid_tag_name", "Tag name is missing or too long.", key)
            continue
        if scope_text == TagScope.CONTROLLER.value:
            scope = TagScope.CONTROLLER
            program_id = None
        elif scope_text == TagScope.PROGRAM.value:
            program_entry = programs.get(program_name)
            if program_entry is None:
                ctx.issue(
                    "program_scope_unavailable",
                    f"Program-scoped tag {name!r} cannot be imported without its source program.",
                    key,
                )
                continue
            scope = TagScope.PROGRAM
            program_id = program_entry[0].id
        else:
            ctx.issue("unsupported_tag_scope", f"Tag scope {scope_text!r} is not imported.", key)
            continue
        rows = list(
            ctx.db.execute(
                select(Tag)
                .where(
                    Tag.project_id == ctx.project.id,
                    Tag.scope == scope,
                    Tag.scope_program_id == program_id,
                    Tag.name == name,
                )
                .order_by(Tag.created_at, Tag.id)
            ).scalars()
        )
        existing, occupied = _occupant(rows, ctx, key)
        raw_type = _text(source.get("data_type"))
        base_type, concrete = _base_type(raw_type, udt_names, aoi_names)
        tag_type = _text(source.get("tag_type")).lower()
        values: dict[str, Any] = {
            "scope": scope,
            "scope_program_id": program_id,
            "role": TagRole.OTHER,
            "base_data_type": base_type,
            "data_type_name": concrete[:80],
            "dimensions": _text(source.get("dimensions"))[:40],
            "description": _text(source.get("description")),
            "external_access": _text(source.get("external_access"))[:20],
            "is_alias": bool(_text(source.get("alias_for"))) or tag_type == "alias",
            "alias_for": _text(source.get("alias_for"))[:120],
            "initial_value": _text(source.get("value"))[:120],
            "source_name": name,
            "is_generated": False,
        }
        constant = _bool(source.get("constant"))
        if constant is not None:
            values["is_constant"] = constant
        if existing is not None:
            tag = _update(ctx, existing, values, key)
        elif occupied:
            tag = rows[0]
        else:
            tag = _create(ctx, Tag(project_id=ctx.project.id, name=name, **values), key)
        if tag.deleted_at is None:
            imported[(scope.value, program_name, name.upper())] = tag
    return imported


def _tag_for_operand(
    tags: dict[tuple[str, str, str], Tag], program_name: str, operand: str
) -> tuple[Tag | None, str]:
    match = _TAG_OPERAND.fullmatch(operand.strip())
    if match is None:
        return None, ""
    explicit_program = match.group("program")
    name = match.group("name")
    if explicit_program:
        return tags.get((TagScope.PROGRAM.value, explicit_program, name.upper())), name
    local = tags.get((TagScope.PROGRAM.value, program_name, name.upper()))
    return local or tags.get((TagScope.CONTROLLER.value, "", name.upper())), name


def _operand_kind(
    tags: dict[tuple[str, str, str], Tag],
    program_name: str,
    mnemonic: str,
    ordinal: int,
    operand: str,
) -> tuple[OperandKind, Tag | None, str, str]:
    text = operand.strip()
    if _FLOAT.fullmatch(text) or _RADIX.fullmatch(text):
        return OperandKind.LITERAL, None, "", ""
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        return OperandKind.LITERAL, None, "", ""
    if mnemonic in {"JSR", "JXR"} and ordinal == 0:
        return OperandKind.ROUTINE_REFERENCE, None, text, ""
    if mnemonic in {"JMP", "LBL"}:
        return OperandKind.EXPRESSION, None, "", ""
    if text in {"", "?"}:
        return OperandKind.EXPRESSION, None, "", ""
    tag, base_name = _tag_for_operand(tags, program_name, text)
    if tag is not None:
        return OperandKind.TAG, tag, tag.name, ""
    if base_name:
        return (
            OperandKind.UNRESOLVED,
            None,
            base_name,
            f"No imported tag declaration resolves source operand {text!r}.",
        )
    return (
        OperandKind.UNRESOLVED,
        None,
        "",
        f"Source operand {text!r} is not a literal or a resolvable canonical tag.",
    )


def _import_l5x_logic(
    ctx: _Context,
    rung_sources: list[dict[str, Any]],
    programs: dict[str, tuple[Program, dict[str, Routine]]],
    tags: dict[tuple[str, str, str], Tag],
    networks: dict[tuple[str, str], LogicNetwork],
) -> None:
    for source in rung_sources:
        program_name = _text(source.get("program"))
        routine_name = _text(source.get("routine"))
        program_entry = programs.get(program_name)
        routine = program_entry[1].get(routine_name) if program_entry is not None else None
        ordinal = _int(source.get("number"))
        key = f"rung:{program_name}/{routine_name}/{_text(source.get('number'))}"
        if routine is None or ordinal is None:
            ctx.issue(
                "rung_parent_unavailable",
                "Rung has no imported routine or integer number.",
                key,
            )
            continue
        rows = list(
            ctx.db.execute(
                select(LogicRung)
                .where(LogicRung.routine_id == routine.id, LogicRung.ordinal == ordinal)
                .order_by(LogicRung.created_at, LogicRung.id)
            ).scalars()
        )
        existing, occupied = _occupant(rows, ctx, key)
        instruction_sources = _as_list(source.get("instructions"))
        has_unresolved = False
        for instruction_source in instruction_sources:
            mnemonic = _text(instruction_source.get("mnemonic")).upper()
            operands = instruction_source.get("operands")
            if not isinstance(operands, list):
                continue
            if any(
                _operand_kind(tags, program_name, mnemonic, index, _text(operand))[0]
                is OperandKind.UNRESOLVED
                for index, operand in enumerate(operands)
            ):
                has_unresolved = True
                break
        rung_values: dict[str, Any] = {
            "comment": _text(source.get("comment")),
            "text": _text(source.get("text")),
            "has_unresolved_operands": has_unresolved,
        }
        network = networks.get((program_name, routine_name))
        if network is not None:
            # Stated only where this importer owns the network.  Where another
            # row owns that canonical key there is no link to state, and writing
            # NULL would retract a membership this importer never recorded.
            rung_values["logic_network_id"] = network.id
        writable = True
        if existing is not None:
            rung = _update(ctx, existing, rung_values, key)
        elif occupied:
            rung = rows[0]
            writable = False
        else:
            rung = _create(
                ctx,
                LogicRung(
                    project_id=ctx.project.id,
                    routine_id=routine.id,
                    ordinal=ordinal,
                    **rung_values,
                ),
                key,
            )
        if not writable:
            continue
        for fallback_ordinal, instruction_source in enumerate(instruction_sources):
            _import_instruction(
                ctx,
                rung,
                program_name,
                instruction_source,
                fallback_ordinal,
                tags,
                key,
            )


def _import_instruction(
    ctx: _Context,
    rung: LogicRung,
    program_name: str,
    source: dict[str, Any],
    fallback_ordinal: int,
    tags: dict[tuple[str, str, str], Tag],
    rung_key: str,
) -> None:
    mnemonic = _text(source.get("mnemonic")).upper()
    ordinal = _int(source.get("index"))
    ordinal = fallback_ordinal if ordinal is None else ordinal
    key = f"{rung_key}/instruction:{ordinal}"
    if not mnemonic or len(mnemonic) > 20:
        ctx.issue(
            "invalid_instruction_mnemonic",
            "Instruction mnemonic is missing or too long.",
            key,
        )
        return
    raw_operands = source.get("operands")
    operands = [_text(item) for item in raw_operands] if isinstance(raw_operands, list) else []
    spec = spec_for(mnemonic)
    restricted = is_restricted(mnemonic)
    if spec is not None and not restricted:
        category = spec.category
        is_supported = True
        unsupported_reason = ""
        destructive = set(resolved_destructive_operands(mnemonic, operands))
        element_kind = LogicElementKind.OUTPUT if spec.is_output else LogicElementKind.CONDITION
    else:
        category = InstructionCategory.UNSUPPORTED
        is_supported = False
        unsupported_reason = (
            restriction_reason(mnemonic)
            if restricted
            else f"Instruction {mnemonic} is not in the generator instruction catalogue."
        )[:200]
        destructive = set()
        element_kind = LogicElementKind.CONDITION
    rows = list(
        ctx.db.execute(
            select(LogicInstruction)
            .where(LogicInstruction.rung_id == rung.id, LogicInstruction.ordinal == ordinal)
            .order_by(LogicInstruction.created_at, LogicInstruction.id)
        ).scalars()
    )
    existing, occupied = _occupant(rows, ctx, key)
    values = {
        "mnemonic": mnemonic,
        "category": category,
        "element_kind": element_kind,
        "is_destructive": bool(destructive),
        "text": _text(source.get("text"))[:300],
        "is_supported": is_supported,
        "unsupported_reason": unsupported_reason,
    }
    writable = True
    if existing is not None:
        instruction = _update(ctx, existing, values, key)
    elif occupied:
        instruction = rows[0]
        writable = False
    else:
        instruction = _create(
            ctx,
            LogicInstruction(
                project_id=ctx.project.id,
                rung_id=rung.id,
                ordinal=ordinal,
                **values,
            ),
            key,
        )
    if not writable:
        return
    for operand_ordinal, operand_text in enumerate(operands):
        operand_key = f"{key}/operand:{operand_ordinal}"
        if len(operand_text) > 200:
            ctx.issue(
                "operand_too_long",
                "Operand exceeds the canonical 200-character limit.",
                operand_key,
            )
            continue
        kind, tag, resolved_name, unresolved_reason = _operand_kind(
            tags,
            program_name,
            mnemonic,
            operand_ordinal,
            operand_text,
        )
        operand_rows = list(
            ctx.db.execute(
                select(LogicOperand)
                .where(
                    LogicOperand.instruction_id == instruction.id,
                    LogicOperand.ordinal == operand_ordinal,
                )
                .order_by(LogicOperand.created_at, LogicOperand.id)
            ).scalars()
        )
        operand, operand_occupied = _occupant(operand_rows, ctx, operand_key)
        operand_values = {
            "kind": kind,
            "text": operand_text,
            "tag_id": tag.id if tag is not None else None,
            "resolved_name": resolved_name[:120],
            "data_type_name": (
                tag.data_type_name or tag.base_data_type.value if tag is not None else ""
            )[:80],
            "is_destination": operand_ordinal in destructive,
            "unresolved_reason": unresolved_reason[:200],
        }
        if operand is not None:
            _update(ctx, operand, operand_values, operand_key)
        elif not operand_occupied:
            _create(
                ctx,
                LogicOperand(
                    project_id=ctx.project.id,
                    instruction_id=instruction.id,
                    ordinal=operand_ordinal,
                    **operand_values,
                ),
                operand_key,
            )


def _io_type(raw: str) -> tuple[SignalType, SignalDirection] | None:
    value = _normalise(raw).upper().replace(" ", "_")
    if value in {"DI", "DIGITAL_INPUT", "DISCRETE_INPUT"} or value.startswith("DI_"):
        return SignalType.DISCRETE_INPUT, SignalDirection.INPUT
    if value in {"DO", "DIGITAL_OUTPUT", "DISCRETE_OUTPUT"} or value.startswith("DO_"):
        return SignalType.DISCRETE_OUTPUT, SignalDirection.OUTPUT
    if value in {"AI", "ANALOG_INPUT"} or value.startswith("AI_"):
        return SignalType.ANALOG_INPUT, SignalDirection.INPUT
    if value in {"AO", "ANALOG_OUTPUT"} or value.startswith("AO_"):
        return SignalType.ANALOG_OUTPUT, SignalDirection.OUTPUT
    if value == "NETWORK_INPUT":
        return SignalType.NETWORK_INPUT, SignalDirection.INPUT
    if value == "NETWORK_OUTPUT":
        return SignalType.NETWORK_OUTPUT, SignalDirection.OUTPUT
    if value == "VIRTUAL":
        return SignalType.VIRTUAL, SignalDirection.BIDIRECTIONAL
    return None


def _fail_state(raw: str) -> FailState:
    value = _normalise(raw).replace(" ", "_")
    return next((item for item in FailState if item.value == value), FailState.UNDEFINED)


def _device_category(raw: str) -> DeviceCategory:
    value = _normalise(raw).replace(" ", "_")
    return next((item for item in DeviceCategory if item.value == value), DeviceCategory.OTHER)


def _import_io_csv(ctx: _Context) -> None:
    content = _as_dict(ctx.artifact.content)
    keys = _record_keys(content)
    headers = _io_header_map(keys)
    _report_unplaced_columns(ctx, keys, headers, "I/O schedule")
    if "address" not in headers and "tag" not in headers and "module" not in headers:
        ctx.issue(
            "not_an_io_schedule",
            "CSV marked as an I/O list has no explicit tag, module, or address column.",
        )
        return
    device_signal_ordinals: dict[uuid.UUID, int] = {}
    points_by_address: dict[str, IOPoint] = {}
    for fallback_ordinal, row_source in enumerate(_as_list(content.get("rows"))):
        record = _as_dict(row_source.get("record"))
        row_number = _int(row_source.get("row")) or fallback_ordinal + 1
        row_key = f"csv-row:{row_number}"
        module = _io_module(ctx, record, headers, row_key)
        io_type = _io_type(_value(record, headers, "signal_type"))
        point = _io_point(ctx, record, headers, row_key, module, io_type, points_by_address)
        device = _io_device(ctx, record, headers, row_key)
        signal = _io_signal(
            ctx,
            record,
            headers,
            row_key,
            device,
            io_type,
            device_signal_ordinals,
        )
        _io_assignment(ctx, row_key, signal, point)
        _io_tag(ctx, record, headers, row_key, io_type, device, signal, point)


def _io_module(
    ctx: _Context,
    record: dict[str, Any],
    headers: dict[str, str],
    row_key: str,
) -> HardwareModule | None:
    name = _value(record, headers, "module")
    if not name:
        return None
    key = f"{row_key}/module:{name}"
    if len(name) > 60:
        ctx.issue("invalid_module_name", "CSV module name exceeds 60 characters.", key)
        return None
    rows = list(
        ctx.db.execute(
            select(HardwareModule)
            .where(HardwareModule.project_id == ctx.project.id, HardwareModule.name == name)
            .order_by(HardwareModule.created_at, HardwareModule.id)
        ).scalars()
    )
    existing, occupied = _occupant(rows, ctx, key)
    source_catalog = _value(record, headers, "module_catalog")[:60]
    source_slot = _int(_value(record, headers, "slot"))
    if existing is not None:
        # An I/O list often repeats a module name on every point but carries no
        # catalogue or slot.  Those blanks must not erase facts imported from
        # the controller export earlier in the same pass.
        values: dict[str, Any] = {}
        if source_catalog:
            values["catalog_number"] = source_catalog
        if source_slot is not None:
            values["slot"] = source_slot
        return _update(ctx, existing, values, key)
    if occupied:
        return rows[0] if rows[0].deleted_at is None else None
    return _create(
        ctx,
        HardwareModule(
            project_id=ctx.project.id,
            name=name,
            catalog_number=source_catalog,
            role=ModuleRole.OTHER,
            slot=source_slot,
        ),
        key,
    )


def _io_point(
    ctx: _Context,
    record: dict[str, Any],
    headers: dict[str, str],
    row_key: str,
    module: HardwareModule | None,
    io_type: tuple[SignalType, SignalDirection] | None,
    seen: dict[str, IOPoint],
) -> IOPoint | None:
    """The terminal one I/O-list row names, created or refreshed.

    ``seen`` carries the terminals this schedule has already named, and it is
    what makes a duplicated address safe.  A terminal is one physical thing, so
    two rows claiming it produce one :class:`IOPoint` and two assignments — the
    shape the duplicate-assignment rule reads.  Letting the second row also write
    the terminal's own description and channel would leave the point describing
    whichever conflicting row happened to come last, and would make the import
    non-idempotent: every pass would rewrite the row twice and bump its revision,
    so a re-import looked like somebody had edited an approved terminal.
    """
    address = _value(record, headers, "address")
    if not address:
        return None
    key = f"{row_key}/point:{address}"
    if len(address) > 120:
        ctx.issue("invalid_io_address", "CSV I/O address exceeds 120 characters.", key)
        return None
    if io_type is None:
        ctx.issue(
            "unknown_io_type",
            f"Address {address!r} has no explicit, recognized I/O type; no point was created.",
            key,
        )
        return None
    first = seen.get(address)
    if first is not None:
        ctx.issue(
            "repeated_io_address",
            f"Address {address!r} is named by more than one row of this I/O schedule. The "
            "terminal keeps the values of the first row that named it; both rows still get "
            "their own address assignment, so the conflict stays reportable.",
            key,
        )
        return first
    rows = list(
        ctx.db.execute(
            select(IOPoint)
            .where(IOPoint.project_id == ctx.project.id, IOPoint.address == address)
            .order_by(IOPoint.created_at, IOPoint.id)
        ).scalars()
    )
    existing, occupied = _occupant(rows, ctx, key)
    status = _normalise(_value(record, headers, "status"))
    values = {
        "hardware_module_id": module.id if module is not None else None,
        "channel": _int(_value(record, headers, "channel")),
        "signal_type": io_type[0],
        "direction": io_type[1],
        "description": _value(record, headers, "description"),
        "engineering_units": _value(record, headers, "units")[:24],
        "fail_state": _fail_state(_value(record, headers, "fail_state")),
        "is_spare": status == "spare",
    }
    point: IOPoint | None
    if existing is not None:
        point = _update(ctx, existing, values, key)
    elif occupied:
        point = rows[0] if rows[0].deleted_at is None else None
    else:
        point = _create(ctx, IOPoint(project_id=ctx.project.id, address=address, **values), key)
    if point is not None:
        seen[address] = point
    return point


def _io_device(
    ctx: _Context,
    record: dict[str, Any],
    headers: dict[str, str],
    row_key: str,
) -> Device | None:
    name = _value(record, headers, "device")
    prefix = _value(record, headers, "device_prefix") or name
    if not name:
        return None
    key = f"{row_key}/device:{prefix}"
    if not prefix or len(prefix) > 60 or len(name) > 120:
        ctx.issue(
            "invalid_device_identity",
            "CSV device name or explicit tag prefix is invalid.",
            key,
        )
        return None
    rows = list(
        ctx.db.execute(
            select(Device)
            .where(Device.project_id == ctx.project.id, Device.tag_prefix == prefix)
            .order_by(Device.created_at, Device.id)
        ).scalars()
    )
    existing, occupied = _occupant(rows, ctx, key)
    values: dict[str, Any] = {
        "name": name,
        "description": _value(record, headers, "description"),
        "category": _device_category(_value(record, headers, "device_category")),
        "fail_state": _fail_state(_value(record, headers, "fail_state")),
    }
    safety = _bool(_value(record, headers, "safety_related"))
    if safety is not None:
        values["is_safety_related"] = safety
    if existing is not None:
        return _update(ctx, existing, values, key)
    if occupied:
        return rows[0] if rows[0].deleted_at is None else None
    return _create(
        ctx,
        Device(project_id=ctx.project.id, tag_prefix=prefix, **values),
        key,
    )


def _io_signal(
    ctx: _Context,
    record: dict[str, Any],
    headers: dict[str, str],
    row_key: str,
    device: Device | None,
    io_type: tuple[SignalType, SignalDirection] | None,
    ordinals: dict[uuid.UUID, int],
) -> Signal | None:
    name = _value(record, headers, "signal")
    if not name or device is None:
        return None
    key = f"{row_key}/signal:{name}"
    if len(name) > 80:
        ctx.issue("invalid_signal_name", "CSV signal name exceeds 80 characters.", key)
        return None
    if io_type is None:
        ctx.issue("unknown_io_type", f"Signal {name!r} has no recognized explicit I/O type.", key)
        return None
    rows = list(
        ctx.db.execute(
            select(Signal)
            .where(Signal.device_id == device.id, Signal.name == name)
            .order_by(Signal.created_at, Signal.id)
        ).scalars()
    )
    existing, occupied = _occupant(rows, ctx, key)
    ordinal = ordinals.get(device.id, 0)
    ordinals[device.id] = ordinal + 1
    values: dict[str, Any] = {
        "signal_type": io_type[0],
        "direction": io_type[1],
        "description": _value(record, headers, "description"),
        "engineering_units": _value(record, headers, "units")[:24],
        "fail_state": _fail_state(_value(record, headers, "fail_state")),
        "ordinal": ordinal,
    }
    required = _bool(_value(record, headers, "required"))
    if required is not None:
        values["is_required"] = required
    if existing is not None:
        return _update(ctx, existing, values, key)
    if occupied:
        return rows[0] if rows[0].deleted_at is None else None
    return _create(
        ctx,
        Signal(project_id=ctx.project.id, device_id=device.id, name=name, **values),
        key,
    )


def _io_assignment(
    ctx: _Context,
    row_key: str,
    signal: Signal | None,
    point: IOPoint | None,
) -> AddressAssignment | None:
    if signal is None or point is None:
        return None
    key = f"{row_key}/assignment"
    rows = list(
        ctx.db.execute(
            select(AddressAssignment)
            .where(
                AddressAssignment.project_id == ctx.project.id,
                AddressAssignment.signal_id == signal.id,
            )
            .order_by(AddressAssignment.created_at, AddressAssignment.id)
        ).scalars()
    )
    existing, occupied = _occupant(rows, ctx, key)
    values = {"io_point_id": point.id, "status": AddressAssignmentStatus.ASSIGNED}
    if existing is not None:
        return _update(ctx, existing, values, key)
    if occupied:
        return rows[0] if rows[0].deleted_at is None else None
    return _create(
        ctx,
        AddressAssignment(project_id=ctx.project.id, signal_id=signal.id, **values),
        key,
    )


def _io_tag(
    ctx: _Context,
    record: dict[str, Any],
    headers: dict[str, str],
    row_key: str,
    io_type: tuple[SignalType, SignalDirection] | None,
    device: Device | None,
    signal: Signal | None,
    point: IOPoint | None,
) -> Tag | None:
    name = _value(record, headers, "tag")
    if not name:
        return None
    key = f"{row_key}/tag:{name}"
    if len(name) > 80:
        ctx.issue("invalid_tag_name", "CSV tag name exceeds 80 characters.", key)
        return None
    rows = list(
        ctx.db.execute(
            select(Tag)
            .where(
                Tag.project_id == ctx.project.id,
                Tag.scope == TagScope.CONTROLLER,
                Tag.scope_program_id.is_(None),
                Tag.name == name,
            )
            .order_by(Tag.created_at, Tag.id)
        ).scalars()
    )
    existing, occupied = _occupant(rows, ctx, key)
    if io_type is None:
        base_type = BaseDataType.UNKNOWN
    elif io_type[0] in {SignalType.ANALOG_INPUT, SignalType.ANALOG_OUTPUT}:
        base_type = BaseDataType.REAL
    elif io_type[0] in {SignalType.DISCRETE_INPUT, SignalType.DISCRETE_OUTPUT}:
        base_type = BaseDataType.BOOL
    else:
        base_type = BaseDataType.UNKNOWN
    values = {
        "scope": TagScope.CONTROLLER,
        "role": TagRole.OTHER,
        "base_data_type": base_type,
        "description": _value(record, headers, "description"),
        "engineering_units": _value(record, headers, "units")[:24],
        "device_id": device.id if device is not None else None,
        "signal_id": signal.id if signal is not None else None,
        "io_point_id": point.id if point is not None else None,
        "source_name": name,
        "is_generated": False,
    }
    if existing is not None:
        # L5X is authoritative for declaration fields.  CSV contributes the
        # explicit physical binding and fills only declaration gaps.
        csv_values: dict[str, Any] = {
            "device_id": values["device_id"] or existing.device_id,
            "signal_id": values["signal_id"] or existing.signal_id,
            "io_point_id": values["io_point_id"] or existing.io_point_id,
            "engineering_units": existing.engineering_units or values["engineering_units"],
            "description": existing.description or values["description"],
            "is_generated": False,
        }
        if existing.base_data_type is BaseDataType.UNKNOWN:
            csv_values["base_data_type"] = base_type
        return _update(ctx, existing, csv_values, key)
    if occupied:
        return rows[0] if rows[0].deleted_at is None else None
    return _create(ctx, Tag(project_id=ctx.project.id, name=name, **values), key)


def _operand_base_name(operand: str) -> str:
    match = _TAG_OPERAND.fullmatch(operand.strip())
    return match.group("name") if match is not None else ""


def _written_tag_names(ctx: _Context) -> set[str]:
    """Folded names of every tag an imported rung writes.

    :attr:`~app.models.delivery.AlarmDefinition.is_driven` is the flag that finds
    an alarm nothing in the controller can ever raise, and this is the only place
    both halves of that comparison are in hand: the L5X is imported before any
    other artifact, so the destination operands it recorded are already rows.
    Those operands were marked destructive from the instruction catalogue during
    that pass, so this reads a recorded fact rather than deciding again which
    operand of which mnemonic is a write.
    """
    names: set[str] = set()
    for resolved_name, operand_text in ctx.db.execute(
        select(LogicOperand.resolved_name, LogicOperand.text).where(
            LogicOperand.project_id == ctx.project.id,
            LogicOperand.is_destination.is_(True),
            LogicOperand.deleted_at.is_(None),
        )
    ):
        for candidate in (_text(resolved_name), _text(operand_text)):
            base = _operand_base_name(candidate)
            if base:
                names.add(base.casefold())
    return names


def _import_alarm_csv(ctx: _Context) -> None:
    """Import an alarm register as canonical alarm definitions.

    The register is the operator's side of the plant, and until it reaches the
    model no validator can compare it with anything: an alarm whose trigger no
    rung writes, one that names no trigger at all, and one whose trigger the
    controller does not declare are all invisible while the rows sit in a parsed
    artifact.  Only columns the source states are read.  Severity in particular is
    never defaulted — see :data:`_ALARM_SEVERITIES`.
    """
    content = _as_dict(ctx.artifact.content)
    keys = _record_keys(content)
    headers = _resolve_headers(keys, _ALARM_HEADER_ALIASES)
    _report_unplaced_columns(ctx, keys, headers, "alarm register")
    if "reference" not in headers:
        ctx.issue(
            "not_an_alarm_register",
            "CSV marked as an alarm list has no column this importer reads as the alarm "
            "identifier, so no alarm definition was created from it.",
        )
        return
    if "latched" in headers:
        # Read, and deliberately not applied.  ``AlarmDefinition.latched`` says
        # whether the *controller* holds the alarm until something clears it, and
        # it is the sole gate on the release-blocking reset-behaviour rule.  A
        # terminal alarm register describes annunciation — it sits beside an
        # acknowledge column for that reason — so it is not evidence about the
        # ladder, and the ladder's own OTL/OTU pairs are already in the model
        # where the logic validator reads them directly.  Writing the column here
        # produced a release blocker stating that nothing clears an alarm the
        # export unlatches on the next rung.
        ctx.issue(
            "unapplied_source_column",
            f"Column {headers['latched']!r} of this alarm register was read but not applied to "
            "the canonical latch state, which is a statement about the controller rather than "
            "about the terminal. Its stated value is in the parsed artifact against this "
            "register's row anchors.",
            f"column:{headers['latched']}",
        )

    written = _written_tag_names(ctx)
    all_tags = list(
        ctx.db.execute(
            select(Tag)
            .where(Tag.project_id == ctx.project.id, Tag.deleted_at.is_(None))
            .order_by(Tag.created_at, Tag.id)
        ).scalars()
    )
    programs = list(
        ctx.db.execute(
            select(Program)
            .where(Program.project_id == ctx.project.id, Program.deleted_at.is_(None))
            .order_by(Program.created_at, Program.id)
        ).scalars()
    )

    for fallback_ordinal, row_source in enumerate(_as_list(content.get("rows"))):
        record = _as_dict(row_source.get("record"))
        row_number = _int(row_source.get("row")) or fallback_ordinal + 1
        row_key = f"csv-row:{row_number}"
        reference = _value(record, headers, "reference")
        if not reference or len(reference) > 40:
            ctx.issue(
                "invalid_alarm_reference",
                "Alarm identifier is missing or exceeds the canonical 40-character key limit.",
                row_key,
            )
            continue
        key = f"{row_key}/alarm:{reference}"
        severity = _ALARM_SEVERITIES.get(_normalise(_value(record, headers, "severity")))
        if severity is None:
            stated = _value(record, headers, "severity")
            ctx.issue(
                "unsupported_alarm_severity",
                f"Alarm {reference} states severity {stated!r}, which is not a severity band "
                "this importer reads. Severity is how fast an operator must respond, so no "
                "band was chosen for it and the alarm was not imported.",
                key,
            )
            continue

        rows = list(
            ctx.db.execute(
                select(AlarmDefinition)
                .where(
                    AlarmDefinition.project_id == ctx.project.id,
                    AlarmDefinition.reference == reference,
                )
                .order_by(AlarmDefinition.created_at, AlarmDefinition.id)
            ).scalars()
        )
        existing, occupied = _occupant(rows, ctx, key)
        trigger = _value(record, headers, "trigger")
        tag = _resolve_tag_reference(trigger, all_tags, programs)
        message = _value(record, headers, "message")
        values: dict[str, Any] = {
            # The register carries one piece of human-readable text per alarm.
            # It is the alarm's name to an engineer reading the list and the
            # operator's message at the terminal, so it is recorded as both
            # rather than picked for one and dropped from the other.
            "name": (_value(record, headers, "name") or message)[:120],
            "message_key": _value(record, headers, "message_key")[:60],
            "severity": severity,
            "alarm_class": _value(record, headers, "alarm_class")[:40],
            "trigger_tag_name": trigger[:120],
            "trigger_tag_id": tag.id if tag is not None else None,
            "trigger_type": _value(record, headers, "trigger_type")[:24],
            "limit_value": _value(record, headers, "limit_value")[:60],
            "deadband": _value(record, headers, "deadband")[:60],
            "delay_seconds": _float(_value(record, headers, "delay_seconds")),
            "is_resolved": tag is not None,
            "is_driven": bool(trigger) and _operand_base_name(trigger).casefold() in written,
            "group_name": _value(record, headers, "group")[:60],
            "ordinal": fallback_ordinal,
        }
        # Written only when the register states it: ``False`` here is the
        # positive claim that the operator does not have to acknowledge this
        # alarm, which is not the same as a register that says nothing.
        acknowledge = _flag(_value(record, headers, "requires_acknowledge"))
        if acknowledge is not None:
            values["requires_acknowledge"] = acknowledge

        if existing is not None:
            alarm = _update(ctx, existing, values, key)
        elif occupied:
            continue
        else:
            alarm = _create(
                ctx,
                AlarmDefinition(project_id=ctx.project.id, reference=reference, **values),
                key,
            )
        if alarm.deleted_at is None:
            _import_alarm_message(ctx, record, headers, key, alarm, message)


def _import_alarm_message(
    ctx: _Context,
    record: dict[str, Any],
    headers: dict[str, str],
    alarm_key: str,
    alarm: AlarmDefinition,
    message: str,
) -> None:
    """Record the operator text the register states, and nothing more.

    ``operator_guidance`` stays empty unless the source has a column for it. An
    alarm that states a condition and no response is a documented gap the alarm
    delivery audit reports, and filling the field in with the condition text
    would close that gap without anybody having decided what the operator does.
    """
    guidance = _value(record, headers, "operator_guidance")
    if not message and not guidance:
        return
    key = f"{alarm_key}/message"
    rows = list(
        ctx.db.execute(
            select(AlarmMessage)
            .where(AlarmMessage.alarm_id == alarm.id)
            .order_by(AlarmMessage.created_at, AlarmMessage.id)
        ).scalars()
    )
    existing, occupied = _occupant(rows, ctx, key)
    values = {"text": message, "operator_guidance": guidance, "ordinal": 0}
    if existing is not None:
        _update(ctx, existing, values, key)
    elif not occupied:
        _create(
            ctx,
            AlarmMessage(project_id=ctx.project.id, alarm_id=alarm.id, **values),
            key,
        )


__all__ = [
    "CanonicalImportIssue",
    "CanonicalImportResult",
    "import_latest_parsed_artifacts",
]
