"""The ESCO control-pattern template registry.

A template is the written-down form of an ESCO control pattern: what tags a
motor starter needs, which rungs implement a travel timeout, what a fault reset
is *not* allowed to do. Templates live in ``templates/`` at the repository root
as YAML, which makes them reviewable, diffable and versioned exactly like the
code that expands them.

Three rules make the registry worth trusting:

1. **The checksum is computed here, never read from the file.** It is a SHA-256
   over the canonically-serialised *loaded template*, so an expansion can record
   ``esco.motor.standard@1.0.0`` *and* the digest of the pattern that produced
   it. A checksum a human can edit in the YAML proves nothing; one derived from
   the content proves the template has not drifted since the expansion was made.
   It answers "does this template still expand to the same thing", which is the
   question a re-review exists to settle — so a reflowed file, a trailing space
   inside a quoted scalar or an added documentary key, none of which reach the
   loaded template, do not manufacture drift against approved logic.

2. **Loading is deterministic.** Files are discovered by suffix rather than by a
   platform-dependent glob, visited in one sorted order on every operating
   system, and hashed from the *loaded template* — not from the raw YAML body —
   through a canonicaliser that refuses any value it cannot serialise without
   guessing. Two loads of the same tree, in two processes started with different
   ``PYTHONHASHSEED`` values, give byte-identical checksums.

3. **A bad file is recorded, not raised.** One malformed template must not stop
   the platform from starting — it degrades to a clearly-reported missing
   pattern. Same defensive posture as :mod:`app.agents.base.plugins`.

The vocabulary is the published one in :mod:`app.core.enums`. A template that
names a category, controller family, tag role, data type, routine language or
approval level outside those enums is a load error, not a silent pass-through.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml

from app.core.enums import (
    BaseDataType,
    ControllerFamily,
    ControllerTaskType,
    LogicElementKind,
    ProgramType,
    RestrictedDomain,
    RoutineLanguage,
    TagRole,
    TemplateApprovalLevel,
    TemplateCategory,
)
from app.core.logging import get_logger

logger = get_logger(__name__)

__all__ = [
    "PARAMETER_TYPES",
    "GeneratedObjects",
    "ProgramSpec",
    "RequiredTag",
    "RoutineSpec",
    "RungElement",
    "RungSpec",
    "TaskSpec",
    "Template",
    "TemplateLoadError",
    "TemplateParameter",
    "TemplateRegistry",
    "TemplateTest",
    "canonical_parameter_value",
    "compute_checksum",
    "default_template_root",
    "get_registry",
    "load_templates",
    "reload_templates",
    "render_parameter_value",
    "template_checksum",
    "template_for",
]

#: Repository root, discovered from this file rather than from the working
#: directory or an environment variable: the templates ship *with* the code, so
#: the code is what knows where they are.
#: ``backend/app/generators/plc/template_registry.py`` -> repository root.
REPO_ROOT: Path = Path(__file__).resolve().parents[4]

#: Semantic version, strictly ``MAJOR.MINOR.PATCH``. No pre-release suffixes:
#: a template either is the approved pattern or it is a different version of it.
_VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")

#: A template identifier: dotted, lower-case, e.g. ``esco.motor.standard``.
_ID_RE = re.compile(r"^[a-z][a-z0-9]*(\.[a-z0-9_]+)+$")

#: File extensions the loader reads, compared case-folded so the same tree loads
#: identically on Windows and on Linux.
_TEMPLATE_SUFFIXES: frozenset[str] = frozenset({".yaml", ".yml"})

#: An instruction mnemonic as it appears in ladder: ``XIC``, ``TON``, ``MOV``,
#: or the name of an Add-On Instruction. Validated structurally here; whether
#: the mnemonic is one this platform supports is the instruction catalogue's
#: business, not the registry's.
_MNEMONIC_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")

#: A Logix identifier used as a tag stem or a routine/program name.
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: A tag reference, which may address a structure member, a bit or an element.
_TAG_REFERENCE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*([.\[\]][A-Za-z0-9_.\[\]]*)*$")

#: Parameter value kinds a template may declare. Deliberately a small closed set
#: of strings rather than a new member of :mod:`app.core.enums`: these describe
#: how the loader validates a supplied value, and are not published contract.
PARAMETER_TYPES: frozenset[str] = frozenset(
    {"string", "identifier", "tag_reference", "boolean", "integer", "number", "enum"}
)


def _is_real_number(value: Any) -> bool:
    """``True`` for an int or float, and never for a bool.

    ``bool`` is an ``int`` in Python, and a ``True`` that rendered as ``1`` in a
    CPT expression would be a silent type change nobody wrote down.
    """
    return isinstance(value, int | float) and not isinstance(value, bool)


def canonical_parameter_value(parameter_type: str, value: Any) -> Any:
    """The one stored form of a supplied parameter value.

    Only ``number`` is affected, and it is the whole reason this exists.
    ``TemplateParameter.check`` accepts both ``100`` and ``100.0`` for a
    ``type: number`` parameter, so without this the same engineering value
    arrives in two spellings — and a browser cannot even express the second,
    because ``JSON.stringify(100.0)`` is ``"100"``. Storing and rendering the
    float form means an expansion re-submitted from the UI regenerates the same
    bytes and the release manifest checksum does not move for a change nobody
    made.

    Values of the wrong type are returned untouched: reporting them is
    :meth:`TemplateParameter.check`'s job, and coercing here would hide it.
    """
    if parameter_type == "number" and _is_real_number(value):
        return float(value)
    return value


def render_parameter_value(parameter_type: str, value: Any) -> str:
    """The operand text a parameter value substitutes into a rung.

    One rule, shared by the reviewer's preview and anything that later expands
    the same template, so the two cannot disagree about what was generated.

    A ``number`` keeps its decimal point — ``100.0``, not ``100`` — even when it
    is whole. That is deliberate and is *not* the rule
    :func:`app.generators.rockwell.l5x._number` uses for a task scan rate: these
    values land inside CPT expressions, where Logix evaluates an expression whose
    operands are all integers with integer arithmetic. Rendering a calibrated
    engineering range of ``100.0`` as ``100`` would turn
    ``(Raw * (100 - 0) / 32767)`` into a truncating integer divide — a scaling
    defect introduced while fixing a formatting one.
    """
    if parameter_type == "number" and _is_real_number(value):
        # repr() of a float is the shortest string that round-trips, and has been
        # platform-independent in CPython since 3.1.
        return repr(float(value))
    if parameter_type == "integer" and _is_real_number(value) and not isinstance(value, float):
        return str(int(value))
    return str(value)


class TemplateFormatError(ValueError):
    """A template file was structurally wrong.

    Raised while parsing one file and caught by the loader, which turns it into
    a :class:`TemplateLoadError`. It never escapes :func:`load_templates`.
    """


# ---------------------------------------------------------------------------
# Small typed readers
# ---------------------------------------------------------------------------
def _mapping(value: Any, where: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TemplateFormatError(f"{where}: expected a mapping, got {type(value).__name__}")
    for key in value:
        if not isinstance(key, str):
            raise TemplateFormatError(f"{where}: keys must be strings, got {key!r}")
    return value


def _sequence(data: Mapping[str, Any], key: str, where: str) -> list[Any]:
    raw = data.get(key)
    if raw is None:
        return []
    if isinstance(raw, str) or not isinstance(raw, Sequence):
        raise TemplateFormatError(f"{where}: '{key}' must be a list")
    return list(raw)


def _require_str(data: Mapping[str, Any], key: str, where: str) -> str:
    raw = data.get(key)
    if not isinstance(raw, str) or not raw.strip():
        raise TemplateFormatError(f"{where}: '{key}' is required and must be a non-empty string")
    return raw.strip()


def _optional_str(data: Mapping[str, Any], key: str, where: str, default: str = "") -> str:
    raw = data.get(key, default)
    if raw is None:
        return default
    if not isinstance(raw, str):
        raise TemplateFormatError(f"{where}: '{key}' must be a string")
    return raw.strip()


def _optional_bool(data: Mapping[str, Any], key: str, where: str, default: bool) -> bool:
    raw = data.get(key, default)
    if not isinstance(raw, bool):
        raise TemplateFormatError(f"{where}: '{key}' must be true or false")
    return raw


def _optional_int(data: Mapping[str, Any], key: str, where: str) -> int | None:
    raw = data.get(key)
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise TemplateFormatError(f"{where}: '{key}' must be an integer")
    return raw


def _enum_member[EnumT: StrEnum](enum_cls: type[EnumT], raw: Any, key: str, where: str) -> EnumT:
    """Resolve a published enum member, naming the alternatives when it fails."""
    if not isinstance(raw, str):
        raise TemplateFormatError(f"{where}: '{key}' must be a string")
    try:
        return enum_cls(raw)
    except ValueError as exc:
        allowed = ", ".join(sorted(member.value for member in enum_cls))
        raise TemplateFormatError(
            f"{where}: '{key}' = {raw!r} is not a {enum_cls.__name__}. Allowed: {allowed}"
        ) from exc


def _version_key(version: str) -> tuple[int, int, int]:
    match = _VERSION_RE.match(version)
    if match is None:
        return (0, 0, 0)
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


# ---------------------------------------------------------------------------
# Template body
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class TemplateParameter:
    """One value an expansion must supply.

    ``default`` is only meaningful when ``required`` is false. A required
    parameter with a default would let an expansion proceed on a value nobody
    chose, which is the kind of quiet assumption this platform exists to avoid.
    """

    name: str
    type: str
    required: bool
    description: str
    default: Any = None
    #: Permitted values when ``type`` is ``enum``.
    choices: tuple[str, ...] = ()

    @classmethod
    def from_mapping(cls, raw: Any, where: str) -> TemplateParameter:
        data = _mapping(raw, where)
        name = _require_str(data, "name", where)
        param_type = _require_str(data, "type", where)
        if param_type not in PARAMETER_TYPES:
            allowed = ", ".join(sorted(PARAMETER_TYPES))
            raise TemplateFormatError(
                f"{where}: parameter {name!r} has type {param_type!r}. Allowed: {allowed}"
            )
        choices_raw = _sequence(data, "choices", where)
        choices = tuple(str(choice) for choice in choices_raw)
        if param_type == "enum" and not choices:
            raise TemplateFormatError(f"{where}: enum parameter {name!r} declares no choices")
        required = _optional_bool(data, "required", where, default=True)
        default = data.get("default")
        if required and default is not None:
            raise TemplateFormatError(
                f"{where}: parameter {name!r} is required and must not carry a default"
            )
        return cls(
            name=name,
            type=param_type,
            required=required,
            description=_optional_str(data, "description", where),
            # Canonicalised at load: a template that writes ``default: 100`` and
            # one that writes ``default: 100.0`` for the same ``type: number``
            # parameter expand identically, so they must not hash differently.
            default=canonical_parameter_value(param_type, default),
            choices=choices,
        )

    def canonical(self, value: Any) -> Any:
        """``value`` in the single form this parameter is stored in."""
        return canonical_parameter_value(self.type, value)

    def render(self, value: Any) -> str:
        """``value`` as the text it substitutes into a rung operand."""
        return render_parameter_value(self.type, value)

    def check(self, value: Any) -> str | None:
        """Return a problem with ``value``, or ``None`` if it is acceptable."""
        if self.type in {"string", "identifier", "tag_reference"}:
            if not isinstance(value, str) or not value:
                return f"parameter {self.name!r} expects a non-empty string"
            if self.type == "identifier" and not _IDENTIFIER_RE.match(value):
                return f"parameter {self.name!r} must be a Logix identifier, got {value!r}"
            if self.type == "tag_reference" and not _TAG_REFERENCE_RE.match(value):
                return f"parameter {self.name!r} must be a tag reference, got {value!r}"
            return None
        if self.type == "boolean":
            return None if isinstance(value, bool) else f"parameter {self.name!r} expects a boolean"
        if self.type == "integer":
            if isinstance(value, bool) or not isinstance(value, int):
                return f"parameter {self.name!r} expects an integer"
            return None
        if self.type == "number":
            if isinstance(value, bool) or not isinstance(value, int | float):
                return f"parameter {self.name!r} expects a number"
            return None
        # enum
        if value not in self.choices:
            allowed = ", ".join(self.choices)
            return f"parameter {self.name!r} must be one of: {allowed}"
        return None


@dataclass(frozen=True, slots=True)
class RequiredTag:
    """A tag an expansion of this template cannot do without.

    ``suffix`` is appended to the device tag stem the expansion supplies, so the
    template names the *shape* of a tag and the project supplies the stem. The
    template never invents a whole tag name.

    ``when_parameter`` names a parameter that gates the tag — run feedback
    exists only if the device has it. Stating the dependency here means a
    validator can tell "this tag is missing" from "this tag does not apply".
    """

    role: TagRole
    suffix: str
    data_type: BaseDataType
    description: str
    #: Concrete UDT or Add-On Instruction type name when ``data_type`` is
    #: ``UDT`` / ``ADDON_INSTRUCTION``; that name is project data, not vocabulary.
    type_name: str = ""
    #: Name of a parameter that gates this tag, and the value it must hold.
    when_parameter: str = ""
    when_value: Any = True
    #: Engineering units, for analog tags. Empty is a documented gap, not a zero.
    units: str = ""

    def applies(self, values: Mapping[str, Any], default: Any) -> bool:
        """Whether this tag is needed given the supplied parameter values."""
        if not self.when_parameter:
            return True
        return values.get(self.when_parameter, default) == self.when_value

    @classmethod
    def from_mapping(cls, raw: Any, where: str) -> RequiredTag:
        data = _mapping(raw, where)
        data_type = _enum_member(BaseDataType, data.get("data_type"), "data_type", where)
        type_name = _optional_str(data, "type_name", where)
        structured = {BaseDataType.UDT, BaseDataType.ADDON_INSTRUCTION}
        if data_type in structured and not type_name:
            raise TemplateFormatError(
                f"{where}: data_type {data_type.value} requires a 'type_name'"
            )
        return cls(
            role=_enum_member(TagRole, data.get("role"), "role", where),
            suffix=_require_str(data, "suffix", where),
            data_type=data_type,
            description=_require_str(data, "description", where),
            type_name=type_name,
            when_parameter=_optional_str(data, "when_parameter", where),
            when_value=data.get("when_value", True),
            units=_optional_str(data, "units", where),
        )


@dataclass(frozen=True, slots=True)
class RungElement:
    """One element of a rung: an instruction, or a set of parallel branches.

    Branches are an explicit nested structure rather than a flag on a flat list.
    A parallel leg is a *list of elements*, and ``branch`` is a list of legs, so
    nesting is expressible and a renderer never has to reconstruct the topology
    from branch-start/branch-end markers it hopes are balanced. Ladder is a
    tree; pretending otherwise is how a generator emits logic it cannot check.

    Operand strings carry ``{parameter}`` placeholders, e.g.
    ``"{device_tag}_Cmd_Start"``. The registry does not substitute them — it
    ships the pattern; expansion is somebody else's job and is where a value
    gets recorded against a requirement.
    """

    instruction: str = ""
    operands: tuple[str, ...] = ()
    kind: LogicElementKind = LogicElementKind.CONDITION
    #: Parallel legs. Non-empty exactly when this element is a branch.
    branch: tuple[tuple[RungElement, ...], ...] = ()

    @property
    def is_branch(self) -> bool:
        return bool(self.branch)

    def iter_instructions(self) -> Iterator[RungElement]:
        """Depth-first walk yielding every instruction element, branches included."""
        if self.is_branch:
            for leg in self.branch:
                for element in leg:
                    yield from element.iter_instructions()
            return
        yield self

    @classmethod
    def from_mapping(cls, raw: Any, where: str) -> RungElement:
        data = _mapping(raw, where)
        if "branch" in data:
            if "instruction" in data:
                raise TemplateFormatError(
                    f"{where}: an element is either a branch or an instruction, not both"
                )
            legs_raw = _sequence(data, "branch", where)
            if len(legs_raw) < 2:
                raise TemplateFormatError(
                    f"{where}: a branch needs at least two parallel legs; "
                    "a single leg is just a series of elements"
                )
            legs: list[tuple[RungElement, ...]] = []
            for index, leg_raw in enumerate(legs_raw):
                if isinstance(leg_raw, str) or not isinstance(leg_raw, Sequence):
                    raise TemplateFormatError(f"{where}: branch leg {index} must be a list")
                leg_where = f"{where} branch leg {index}"
                legs.append(tuple(cls.from_mapping(item, leg_where) for item in leg_raw))
            return cls(kind=LogicElementKind.BRANCH_LEVEL, branch=tuple(legs))

        mnemonic = _require_str(data, "instruction", where)
        if not _MNEMONIC_RE.match(mnemonic):
            raise TemplateFormatError(
                f"{where}: instruction {mnemonic!r} is not a plain upper-case mnemonic"
            )
        if "operand" in data and "operands" in data:
            raise TemplateFormatError(f"{where}: use 'operand' or 'operands', not both")
        operands: tuple[str, ...]
        if "operand" in data:
            operands = (_require_str(data, "operand", where),)
        else:
            operands = tuple(str(item) for item in _sequence(data, "operands", where))
        kind = _enum_member(
            LogicElementKind, data.get("kind", LogicElementKind.CONDITION.value), "kind", where
        )
        if kind in {LogicElementKind.BRANCH_START, LogicElementKind.BRANCH_END}:
            raise TemplateFormatError(
                f"{where}: branch structure is expressed with 'branch', "
                f"not with kind {kind.value!r}"
            )
        return cls(instruction=mnemonic, operands=operands, kind=kind)


@dataclass(frozen=True, slots=True)
class RungSpec:
    """One rung the template generates.

    ``comment`` is not decoration. A generated rung that nobody can read is a
    liability, so the template carries the comment the expansion will attach.

    ``when_parameter`` gates the rung on a parameter value, which is how one
    template covers a motor with run feedback and a motor without it. The
    alternative — two near-identical templates — makes the difference between
    them something a reader has to diff rather than something stated.
    """

    routine: str
    title: str
    comment: str
    elements: tuple[RungElement, ...]
    when_parameter: str = ""
    when_value: Any = True

    def applies(self, values: Mapping[str, Any], default: Any) -> bool:
        """Whether this rung is generated given the supplied parameter values."""
        if not self.when_parameter:
            return True
        return values.get(self.when_parameter, default) == self.when_value

    @classmethod
    def from_mapping(cls, raw: Any, where: str) -> RungSpec:
        data = _mapping(raw, where)
        elements_raw = _sequence(data, "elements", where)
        if not elements_raw:
            raise TemplateFormatError(f"{where}: a rung must declare at least one element")
        title = _require_str(data, "title", where)
        return cls(
            routine=_require_str(data, "routine", where),
            title=title,
            comment=_require_str(data, "comment", where),
            elements=tuple(
                RungElement.from_mapping(item, f"{where} rung {title!r} element {index}")
                for index, item in enumerate(elements_raw)
            ),
            when_parameter=_optional_str(data, "when_parameter", where),
            when_value=data.get("when_value", True),
        )

    def iter_instructions(self) -> Iterator[RungElement]:
        for element in self.elements:
            yield from element.iter_instructions()


@dataclass(frozen=True, slots=True)
class RoutineSpec:
    """A routine the template generates.

    ``program`` is empty on an equipment pattern, which contributes routines to
    whatever program the expansion places it in, and set on a controller layout,
    which creates the programs itself. Two programs may each hold a
    ``MainRoutine``, so :attr:`qualified_name` — not ``name`` — is what a rung
    refers to.
    """

    name: str
    program: str
    language: RoutineLanguage
    description: str
    is_main: bool = False

    @property
    def qualified_name(self) -> str:
        return f"{self.program}/{self.name}" if self.program else self.name

    @classmethod
    def from_mapping(cls, raw: Any, where: str) -> RoutineSpec:
        data = _mapping(raw, where)
        name = _require_str(data, "name", where)
        if not _IDENTIFIER_RE.match(name):
            raise TemplateFormatError(f"{where}: routine name {name!r} is not a Logix identifier")
        return cls(
            name=name,
            program=_optional_str(data, "program", where),
            language=_enum_member(
                RoutineLanguage,
                data.get("language", RoutineLanguage.LADDER.value),
                "language",
                where,
            ),
            description=_require_str(data, "description", where),
            is_main=_optional_bool(data, "is_main", where, default=False),
        )


@dataclass(frozen=True, slots=True)
class ProgramSpec:
    """A program the template generates, and the routines inside it."""

    name: str
    program_type: ProgramType
    description: str
    main_routine: str
    routines: tuple[str, ...] = ()

    @classmethod
    def from_mapping(cls, raw: Any, where: str) -> ProgramSpec:
        data = _mapping(raw, where)
        name = _require_str(data, "name", where)
        if not _IDENTIFIER_RE.match(name):
            raise TemplateFormatError(f"{where}: program name {name!r} is not a Logix identifier")
        return cls(
            name=name,
            program_type=_enum_member(
                ProgramType, data.get("type", ProgramType.NORMAL.value), "type", where
            ),
            description=_require_str(data, "description", where),
            main_routine=_require_str(data, "main_routine", where),
            routines=tuple(str(item) for item in _sequence(data, "routines", where)),
        )


@dataclass(frozen=True, slots=True)
class TaskSpec:
    """A controller task the template generates.

    ``rate_ms`` is required for a periodic task and refused for a continuous
    one: a continuous task with a scan rate is a contradiction, and accepting it
    would mean the emitted project quietly disagreed with the template.
    """

    name: str
    task_type: ControllerTaskType
    description: str
    programs: tuple[str, ...] = ()
    rate_ms: int | None = None
    watchdog_ms: int | None = None

    @classmethod
    def from_mapping(cls, raw: Any, where: str) -> TaskSpec:
        data = _mapping(raw, where)
        name = _require_str(data, "name", where)
        if not _IDENTIFIER_RE.match(name):
            raise TemplateFormatError(f"{where}: task name {name!r} is not a Logix identifier")
        task_type = _enum_member(ControllerTaskType, data.get("type"), "type", where)
        rate_ms = _optional_int(data, "rate_ms", where)
        if task_type is ControllerTaskType.PERIODIC and rate_ms is None:
            raise TemplateFormatError(f"{where}: periodic task {name!r} must declare 'rate_ms'")
        if task_type is ControllerTaskType.CONTINUOUS and rate_ms is not None:
            raise TemplateFormatError(f"{where}: continuous task {name!r} cannot have a 'rate_ms'")
        return cls(
            name=name,
            task_type=task_type,
            description=_require_str(data, "description", where),
            programs=tuple(str(item) for item in _sequence(data, "programs", where)),
            rate_ms=rate_ms,
            watchdog_ms=_optional_int(data, "watchdog_ms", where),
        )


@dataclass(frozen=True, slots=True)
class GeneratedObjects:
    """Everything an expansion of this template creates.

    Stated up front so that "what will this template do to my project" is
    answerable before it runs, and so an impact analysis is a read rather than a
    dry-run.
    """

    tasks: tuple[TaskSpec, ...] = ()
    programs: tuple[ProgramSpec, ...] = ()
    routines: tuple[RoutineSpec, ...] = ()
    rungs: tuple[RungSpec, ...] = ()

    @classmethod
    def from_mapping(cls, raw: Any, where: str) -> GeneratedObjects:
        data = _mapping(raw, where)
        return cls(
            tasks=tuple(
                TaskSpec.from_mapping(item, f"{where}.tasks[{index}]")
                for index, item in enumerate(_sequence(data, "tasks", where))
            ),
            programs=tuple(
                ProgramSpec.from_mapping(item, f"{where}.programs[{index}]")
                for index, item in enumerate(_sequence(data, "programs", where))
            ),
            routines=tuple(
                RoutineSpec.from_mapping(item, f"{where}.routines[{index}]")
                for index, item in enumerate(_sequence(data, "routines", where))
            ),
            rungs=tuple(
                RungSpec.from_mapping(item, f"{where}.rungs[{index}]")
                for index, item in enumerate(_sequence(data, "rungs", where))
            ),
        )

    def iter_instructions(self) -> Iterator[RungElement]:
        for rung in self.rungs:
            yield from rung.iter_instructions()


@dataclass(frozen=True, slots=True)
class TemplateTest:
    """A behaviour an expansion of this template must exhibit.

    Carried in the template rather than only in the test suite because it is the
    engineering acceptance criterion, and it is what a generated test plan is
    built from. ``then`` is the assertion; a test whose ``then`` is vague is not
    a test.
    """

    name: str
    given: str
    when: str
    then: str

    @classmethod
    def from_mapping(cls, raw: Any, where: str) -> TemplateTest:
        data = _mapping(raw, where)
        return cls(
            name=_require_str(data, "name", where),
            given=_require_str(data, "given", where),
            when=_require_str(data, "when", where),
            then=_require_str(data, "then", where),
        )


@dataclass(frozen=True, slots=True)
class Template:
    """One versioned ESCO control pattern."""

    id: str
    version: str
    checksum: str
    category: TemplateCategory
    display_name: str
    description: str
    supported_families: tuple[ControllerFamily, ...]
    approval_level: TemplateApprovalLevel
    restricted_domains: tuple[RestrictedDomain, ...]
    parameters: tuple[TemplateParameter, ...]
    required_tags: tuple[RequiredTag, ...]
    generated_objects: GeneratedObjects
    engineering_notes: str
    restrictions: tuple[str, ...]
    tests: tuple[TemplateTest, ...]
    #: Path the template was read from, relative to the template root, so a load
    #: error and a loaded template are reported in the same terms.
    source: str = ""

    def qualified_id(self) -> str:
        """``esco.motor.standard@1.0.0`` — what an expansion records."""
        return f"{self.id}@{self.version}"

    def supports(self, family: ControllerFamily) -> bool:
        return family in self.supported_families

    def parameter(self, name: str) -> TemplateParameter | None:
        for parameter in self.parameters:
            if parameter.name == name:
                return parameter
        return None

    def canonical_parameters(self, values: Mapping[str, Any]) -> dict[str, Any]:
        """``values`` with every declared parameter in its one stored form.

        Call this at the boundary — before the values are persisted against an
        expansion and before anything renders them — so that a value which
        arrived as ``100`` and a value which arrived as ``100.0`` cannot go on to
        produce two different rungs, two different L5X byte streams and two
        different package checksums for the same engineering intent.

        An undeclared name is passed through untouched.
        :meth:`validate_parameters` is what reports it; silently dropping it here
        would expand the template on the old default while the caller believed
        otherwise.
        """
        declared = {parameter.name: parameter for parameter in self.parameters}
        return {
            name: declared[name].canonical(value) if name in declared else value
            for name, value in values.items()
        }

    def render_parameter(self, name: str, value: Any) -> str:
        """``value`` as the operand text it substitutes into a rung."""
        parameter = self.parameter(name)
        return parameter.render(value) if parameter is not None else str(value)

    def validate_parameters(self, values: Mapping[str, Any]) -> list[str]:
        """Check supplied values against the declared parameters.

        Returns a list of human-readable problems; empty means acceptable. An
        unknown parameter is an error rather than a shrug: it usually means a
        renamed parameter, and silently ignoring it would expand the template
        with the old default while the caller believed otherwise.
        """
        problems: list[str] = []
        declared = {parameter.name: parameter for parameter in self.parameters}

        for parameter in self.parameters:
            if parameter.required and parameter.name not in values:
                problems.append(f"missing required parameter {parameter.name!r}")

        for name, value in values.items():
            # Deliberately not reusing ``parameter`` from the loop above: the
            # lookup is optional and the loop variable is not, and conflating
            # them hides a real None from the type checker.
            declared_parameter = declared.get(name)
            if declared_parameter is None:
                problems.append(f"unknown parameter {name!r}")
                continue
            problem = declared_parameter.check(value)
            if problem is not None:
                problems.append(problem)

        return sorted(problems)

    def _gate_default(self, parameter_name: str) -> Any:
        parameter = self.parameter(parameter_name)
        return parameter.default if parameter is not None else None

    def required_tags_for(self, values: Mapping[str, Any]) -> tuple[RequiredTag, ...]:
        """The subset of ``required_tags`` that applies given these parameters."""
        return tuple(
            tag
            for tag in self.required_tags
            if tag.applies(values, self._gate_default(tag.when_parameter))
        )

    def rungs_for(self, values: Mapping[str, Any]) -> tuple[RungSpec, ...]:
        """The subset of generated rungs that applies given these parameters."""
        return tuple(
            rung
            for rung in self.generated_objects.rungs
            if rung.applies(values, self._gate_default(rung.when_parameter))
        )

    @property
    def needs_senior_review(self) -> bool:
        return self.approval_level is not TemplateApprovalLevel.STANDARD

    @classmethod
    def from_mapping(cls, raw: Any, source: str) -> Template:
        """Build a template from a parsed YAML body, computing its checksum."""
        data = _mapping(raw, source)
        template_id = _require_str(data, "id", source)
        if not _ID_RE.match(template_id):
            raise TemplateFormatError(
                f"{source}: id {template_id!r} must be dotted lower-case, "
                "e.g. 'esco.motor.standard'"
            )
        version = _require_str(data, "version", source)
        if not _VERSION_RE.match(version):
            raise TemplateFormatError(f"{source}: version {version!r} must be MAJOR.MINOR.PATCH")

        families_raw = _sequence(data, "supported_families", source)
        if not families_raw:
            raise TemplateFormatError(f"{source}: 'supported_families' must not be empty")
        families = tuple(
            _enum_member(ControllerFamily, item, "supported_families", source)
            for item in families_raw
        )

        restrictions = tuple(
            str(item).strip()
            for item in _sequence(data, "restrictions", source)
            if str(item).strip()
        )
        if not restrictions:
            raise TemplateFormatError(
                f"{source}: 'restrictions' must state plainly what this template does not do"
            )

        approval_level = _enum_member(
            TemplateApprovalLevel, data.get("approval_level"), "approval_level", source
        )
        domains = tuple(
            _enum_member(RestrictedDomain, item, "restricted_domains", source)
            for item in _sequence(data, "restricted_domains", source)
        )
        if domains and approval_level is not TemplateApprovalLevel.QUALIFIED_REVIEW_REQUIRED:
            raise TemplateFormatError(
                f"{source}: a template touching a restricted domain must set "
                "approval_level: qualified_review_required"
            )

        template = cls(
            id=template_id,
            version=version,
            # Filled in below, once there is a template to hash. The digest is
            # over what this file loaded to, not over the bytes it was written in.
            checksum="",
            category=_enum_member(TemplateCategory, data.get("category"), "category", source),
            display_name=_require_str(data, "display_name", source),
            description=_require_str(data, "description", source),
            supported_families=families,
            approval_level=approval_level,
            restricted_domains=domains,
            parameters=tuple(
                TemplateParameter.from_mapping(item, f"{source}.parameters[{index}]")
                for index, item in enumerate(_sequence(data, "parameters", source))
            ),
            required_tags=tuple(
                RequiredTag.from_mapping(item, f"{source}.required_tags[{index}]")
                for index, item in enumerate(_sequence(data, "required_tags", source))
            ),
            generated_objects=GeneratedObjects.from_mapping(
                data.get("generated_objects", {}), f"{source}.generated_objects"
            ),
            engineering_notes=_require_str(data, "engineering_notes", source),
            restrictions=restrictions,
            tests=tuple(
                TemplateTest.from_mapping(item, f"{source}.tests[{index}]")
                for index, item in enumerate(_sequence(data, "tests", source))
            ),
            source=source,
        )
        _check_internal_references(template, source)
        return replace(template, checksum=template_checksum(template, source))


def _check_internal_references(template: Template, source: str) -> None:
    """Refuse a template whose own parts do not line up.

    Catching a rung that names a routine the template never creates here means
    the failure is a load error naming the file, rather than a generator
    crashing halfway through an expansion.
    """
    parameter_names = {parameter.name for parameter in template.parameters}
    for tag in template.required_tags:
        if tag.when_parameter and tag.when_parameter not in parameter_names:
            raise TemplateFormatError(
                f"{source}: required tag {tag.suffix!r} is gated on unknown "
                f"parameter {tag.when_parameter!r}"
            )
    for rung in template.generated_objects.rungs:
        if rung.when_parameter and rung.when_parameter not in parameter_names:
            raise TemplateFormatError(
                f"{source}: rung {rung.title!r} is gated on unknown "
                f"parameter {rung.when_parameter!r}"
            )

    routine_names = {routine.qualified_name for routine in template.generated_objects.routines}
    for rung in template.generated_objects.rungs:
        if rung.routine not in routine_names:
            raise TemplateFormatError(
                f"{source}: rung {rung.title!r} targets routine {rung.routine!r}, "
                "which this template does not create"
            )

    program_names = {program.name for program in template.generated_objects.programs}
    for program in template.generated_objects.programs:
        for routine_name in [*program.routines, program.main_routine]:
            if f"{program.name}/{routine_name}" not in routine_names:
                raise TemplateFormatError(
                    f"{source}: program {program.name!r} lists routine {routine_name!r}, "
                    "which this template does not create"
                )
    for task in template.generated_objects.tasks:
        for program_name in task.programs:
            if program_name not in program_names:
                raise TemplateFormatError(
                    f"{source}: task {task.name!r} lists program {program_name!r}, "
                    "which this template does not create"
                )
    if program_names:
        for routine in template.generated_objects.routines:
            if routine.program and routine.program not in program_names:
                raise TemplateFormatError(
                    f"{source}: routine {routine.name!r} belongs to program "
                    f"{routine.program!r}, which this template does not create"
                )


def _canonical_value(value: Any, where: str) -> Any:
    """``value`` as a JSON-native structure, or a load error saying why not.

    Refusing is the whole point. The obvious spelling of this function is
    ``json.dumps(..., default=str)``, which accepts anything by stringifying it,
    and that breaks the digest in both directions at once:

    * ``str()`` of a ``set`` renders in iteration order, which CPython
      randomises per process. ``yaml.safe_load`` produces a ``set`` for the
      standard ``!!set`` tag, so one worker would approve an expansion against a
      digest the next worker recomputes differently and reports as drift, for a
      file nobody touched;
    * a ``datetime.date`` and the quoted string that spells it stringify to the
      same text, so two genuinely different values hash alike — the opposite
      failure, and the one a checksum is least allowed to have.

    So anything that is not JSON-native is a :class:`TemplateFormatError` naming
    the path and the type, which the loader records against the file. Quoting the
    value in the YAML is the fix, and it makes the author state which of the two
    things they meant.
    """
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        # StrEnum members are strings; take the value, not the member.
        return str(value)
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise TemplateFormatError(
                f"{where}: {value!r} is not a finite number, so it has no canonical "
                "JSON form and cannot be checksummed"
            )
        return float(value)
    if isinstance(value, Mapping):
        canonical: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TemplateFormatError(
                    f"{where}: key {key!r} is a {type(key).__name__}, not a string. "
                    "Quote it — JSON has only string keys, so 1 and '1' would "
                    "otherwise hash alike."
                )
            name = str(key)
            canonical[name] = _canonical_value(item, f"{where}.{name}")
        return canonical
    if isinstance(value, list | tuple):
        return [_canonical_value(item, f"{where}[{index}]") for index, item in enumerate(value)]
    raise TemplateFormatError(
        f"{where}: a {type(value).__name__} has no canonical JSON form, so no honest "
        "checksum can be taken over it. Quote the value in the YAML: a date and the "
        "string that spells it, or a !!set and its own text, must not hash alike."
    )


def compute_checksum(body: Mapping[str, Any], *, where: str = "template") -> str:
    """SHA-256 over a canonically-serialised mapping.

    Canonical means sorted keys, a fixed separator, and — through
    :func:`_canonical_value` — no value that had to be guessed at. Key order and
    layout cannot change the digest while the content is unchanged, and no
    content change can leave it unchanged.

    ``where`` labels the structure in any error, so a refusal reads as a place in
    a template rather than as an anonymous type name.
    """
    canonical = json.dumps(
        _canonical_value(body, where),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def template_checksum(template: Template, where: str = "template") -> str:
    """SHA-256 over the loaded template, excluding its own checksum and source.

    Taken over the *constructed* :class:`Template` rather than over the raw YAML
    body, because the loader normalises: every string is stripped and every key
    the schema does not name is discarded. Two files that build an identical
    template expand to identical logic, so they must carry an identical digest —
    otherwise a trailing space inside a quoted ``display_name``, or a documentary
    ``reviewed_by:`` key, reports every approved expansion pinned to that
    template as drifted and forces a re-review of control logic that did not
    change.

    ``source`` is excluded for the same reason: the same pattern read from a
    customer template root is the same pattern.
    """
    body = asdict(template)
    del body["checksum"]
    del body["source"]
    return compute_checksum(body, where=where)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class TemplateLoadError:
    """A template file that exists but could not be used.

    Carries the filename and the reason, because "the motor pattern is missing"
    is not actionable and "templates/patterns/motor_standard.yaml: 'version'
    must be MAJOR.MINOR.PATCH" is.
    """

    path: str
    message: str


@dataclass(slots=True)
class TemplateRegistry:
    """Every template that loaded, and every file that did not.

    Indexed by id and then by version, because a project pins a version: an
    approved expansion must keep referring to the template it was reviewed
    against even after a newer one lands.
    """

    root: Path
    #: ``{template_id: {version: Template}}``.
    templates: dict[str, dict[str, Template]] = field(default_factory=dict)
    load_errors: list[TemplateLoadError] = field(default_factory=list)

    # -- population --------------------------------------------------------
    def add(self, template: Template) -> None:
        versions = self.templates.setdefault(template.id, {})
        existing = versions.get(template.version)
        if existing is not None:
            raise ValueError(
                f"duplicate template {template.qualified_id()}: "
                f"already loaded from {existing.source}"
            )
        versions[template.version] = template

    # -- queries -----------------------------------------------------------
    def __len__(self) -> int:
        return sum(len(versions) for versions in self.templates.values())

    @property
    def ids(self) -> list[str]:
        return sorted(self.templates)

    def all(self) -> list[Template]:
        """Every version of every template, deterministically ordered."""
        return sorted(
            (template for versions in self.templates.values() for template in versions.values()),
            key=lambda template: (template.id, _version_key(template.version)),
        )

    def versions(self, template_id: str) -> list[str]:
        return sorted(self.templates.get(template_id, {}), key=_version_key)

    def get(self, template_id: str, version: str | None = None) -> Template | None:
        """One template. With no version, the highest semantic version present."""
        versions = self.templates.get(template_id)
        if not versions:
            return None
        if version is not None:
            return versions.get(version)
        newest = max(versions, key=_version_key)
        return versions[newest]

    def latest(self) -> list[Template]:
        """The newest version of each template."""
        return [
            template for template_id in self.ids if (template := self.get(template_id)) is not None
        ]

    def by_category(self, category: TemplateCategory) -> list[Template]:
        return [template for template in self.latest() if template.category is category]

    def for_family(self, family: ControllerFamily) -> list[Template]:
        return [template for template in self.latest() if template.supports(family)]

    def capability_rows(self) -> list[dict[str, Any]]:
        """Flat rows for an operator-facing capability listing."""
        rows: list[dict[str, Any]] = [
            {
                "name": f"template:{template.id}",
                "available": True,
                "version": template.version,
                "detail": template.display_name,
            }
            for template in self.latest()
        ]
        rows.extend(
            {
                "name": f"template:{error.path}",
                "available": False,
                "version": "",
                "detail": error.message,
            }
            for error in self.load_errors
        )
        return rows


def default_template_root() -> Path:
    """``<repository root>/templates``."""
    return REPO_ROOT / "templates"


def load_templates(root: Path | None = None) -> TemplateRegistry:
    """Load every template under ``root``.

    Never raises. A file that is unreadable, is not YAML, or does not describe a
    valid template is recorded in ``load_errors`` with its path and the reason,
    and the rest of the tree still loads — one bad template must not stop the
    platform from starting.

    Files are discovered by case-folded suffix and visited in one sorted order on
    every platform, so the registry, its errors and every checksum are identical
    between runs, between processes and between operating systems.
    """
    template_root = (root or default_template_root()).resolve()
    registry = TemplateRegistry(root=template_root)

    if not template_root.is_dir():
        registry.load_errors.append(
            TemplateLoadError(
                path=str(template_root),
                message="template directory does not exist",
            )
        )
        logger.warning("template root missing", extra={"template_root": str(template_root)})
        return registry

    # Not ``rglob("*.yaml")``: pathlib inherits the platform's case rules, so on
    # Windows that pattern matches ``Motor_Standard.YAML`` and on Linux it does
    # not. A template that loads on a developer's machine and is *never visited*
    # in the container is the one failure this loader's "record it, don't raise"
    # design cannot report — there is no file to report. Sorting on ``as_posix()``
    # for the same reason: comparing Path objects is case-folded on Windows only.
    paths = sorted(
        (
            path
            for path in template_root.rglob("*")
            if path.suffix.lower() in _TEMPLATE_SUFFIXES and path.is_file()
        ),
        key=lambda path: path.as_posix(),
    )
    for path in paths:
        relative = path.relative_to(template_root).as_posix()
        try:
            body = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError) as exc:
            registry.load_errors.append(
                TemplateLoadError(path=relative, message=f"cannot read file: {exc}")
            )
            continue
        except yaml.YAMLError as exc:
            # str(exc) on a YAML error is multi-line and includes the position,
            # which is exactly what somebody fixing the file needs.
            registry.load_errors.append(
                TemplateLoadError(
                    path=relative,
                    message=f"invalid YAML: {' '.join(str(exc).split())}",
                )
            )
            continue

        if body is None:
            registry.load_errors.append(TemplateLoadError(path=relative, message="file is empty"))
            continue

        try:
            template = Template.from_mapping(body, relative)
            registry.add(template)
        except (TemplateFormatError, ValueError) as exc:
            registry.load_errors.append(TemplateLoadError(path=relative, message=str(exc)))
            continue
        except Exception as exc:  # pragma: no cover - defensive
            registry.load_errors.append(
                TemplateLoadError(path=relative, message=f"{type(exc).__name__}: {exc}")
            )
            continue

    logger.info(
        "template registry loaded",
        extra={
            "template_root": str(template_root),
            "template_count": len(registry),
            "load_errors": [error.path for error in registry.load_errors],
        },
    )
    return registry


_registry: TemplateRegistry | None = None


def get_registry() -> TemplateRegistry:
    """The cached registry over the shipped templates."""
    global _registry
    if _registry is None:
        _registry = load_templates()
    return _registry


def reload_templates() -> TemplateRegistry:
    """Re-read the shipped templates, replacing the cache."""
    global _registry
    _registry = load_templates()
    return _registry


def template_for(template_id: str, version: str | None = None) -> Template | None:
    """Look one template up in the cached registry."""
    return get_registry().get(template_id, version)
