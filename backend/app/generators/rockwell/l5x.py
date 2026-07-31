"""Component L5X emission from the canonical engineering model.

What this module emits are **import-ready candidates**: files that our own
parser reads back into the same structure we started from. That is worth real
money and it is not a claim that Studio 5000 will import them. Nothing here sets
:attr:`~app.core.enums.ValidationLevel.ROCKWELL_IMPORT_VERIFIED`, and nothing
here can — that level needs the licensed-workstation adapter, which is out of
scope and off by default.

Rung text is rendered **from** the structured rows and never from
:attr:`~app.models.plc.LogicRung.text`. That field is a rendering maintained for
listings and diffs; treating it as input would mean emitting a string this
generator never validated, which is precisely the failure the IR exists to
prevent. A rung is rendered by walking its instructions in ``ordinal`` order and
reading their ``branch_level`` / ``branch_index`` coordinates as a small state
machine — descend a level and a ``[`` is opened, step to the next leg and a
``,`` is written, come back up and a ``]`` closes it. When the rung also carries
:class:`~app.models.plc.LogicBranch` rows the leg counts implied by the
coordinates are checked against them, which is the cross-check those two views
exist to make possible.

Refusals rather than plausible files
------------------------------------
A generator that emits a rung referencing a tag that will not exist has produced
a file that fails at import for a reason nobody recorded. So this module raises
:class:`GenerationRefused` — carrying *what* and *where* — rather than emit when:

* an operand is :attr:`~app.core.enums.OperandKind.UNRESOLVED`;
* an instruction is :func:`~app.generators.plc.instructions.is_restricted`;
* an instruction is not :func:`~app.generators.plc.instructions.supports` for
  the target controller family, and is not a call to an Add-On Instruction the
  project declares;
* a rung references a tag, or a routine, that is not in the declared set;
* the instruction row itself says ``is_supported`` is false;
* the operand count is outside the arity the instruction catalogue states;
* the branch coordinates do not describe a well-formed rung.

Every one of those is a caller's cue to record a
:class:`~app.models.release.ValidationResult` naming the offending object,
instead of shipping a file that will fail in front of the customer.

What is guessed, and said so
----------------------------
The wrapper shape — ``<RSLogix5000Content SchemaRevision TargetType TargetName
ContainsContext>`` around a ``<Controller Use="Context">`` holding the target
component — is taken from ``fixtures/demo_project/Conveyor_Line3.L5X`` and from
what :mod:`app.parsers.l5x_parser` reads. ``ExportOptions`` is copied from that
fixture. ``ExportDate`` is **omitted** unless the caller supplies one, because a
clock reading in the file would destroy the byte-identity that makes a manifest
checksum mean anything; a caller that needs the attribute passes a value that is
itself part of the approved input.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from app.core.enums import ControllerFamily, InstructionCategory, OperandKind
from app.core.errors import AppError
from app.generators.plc.instructions import (
    is_restricted,
    restriction_reason,
    spec_for,
    supports,
)
from app.generators.rockwell.xml_writer import XmlElement, element, serialise
from app.models.plant import HardwareModule
from app.models.plc import (
    ControllerModel,
    ControllerTask,
    LogicInstruction,
    LogicRung,
    Program,
    Routine,
)
from app.models.tags import AOIDefinitionReference, Tag, UDTDefinition, UDTMember

__all__ = [
    "AOI_REFERENCE_NOTICE",
    "DEFAULT_EXPORT_OPTIONS",
    "DEFAULT_SCHEMA_REVISION",
    "ControllerContext",
    "GenerationRefused",
    "LogicScope",
    "RefusalReason",
    "RockwellGenerationError",
    "emit_aoi_reference",
    "emit_controller_project",
    "emit_program",
    "emit_routine",
    "emit_udt",
    "render_instruction_text",
    "render_rung_text",
]

#: The schema revision written when a target profile does not state one. Studio
#: 5000 has used ``1.0`` throughout the versions this platform targets.
DEFAULT_SCHEMA_REVISION = "1.0"

#: Copied verbatim from the demo fixture. Studio writes this list to say what it
#: put in the file; ours says the same because we put the same things in.
DEFAULT_EXPORT_OPTIONS = "References DecoratedData Context"

#: Prefixed to the description of every emitted Add-On Instruction file. This
#: platform references AOIs and never fabricates them: the emitted file carries
#: the interface so a tag of that type resolves, and carries no logic because we
#: do not have any and inventing some would put unreviewed control code under
#: somebody else's name.
AOI_REFERENCE_NOTICE = (
    "INTERFACE REFERENCE ONLY - this file declares the parameters of an Add-On "
    "Instruction this project uses. It contains no logic and is not the definition. "
    "Import the definition supplied with the project; do not import this file over "
    "an existing Add-On Instruction."
)

#: ``[16]`` or ``[16,8]`` as written on a tag or UDT member.
_DIMENSIONS = re.compile(r"^\[\s*\d+(?:\s*,\s*\d+)*\s*\]$")


class RefusalReason(StrEnum):
    """Why a generation was refused, as a stable code a caller can branch on."""

    UNRESOLVED_OPERAND = "unresolved_operand"
    RESTRICTED_INSTRUCTION = "restricted_instruction"
    UNSUPPORTED_INSTRUCTION = "unsupported_instruction"
    OPERAND_COUNT = "operand_count"
    UNDECLARED_TAG = "undeclared_tag"
    UNDECLARED_ROUTINE = "undeclared_routine"
    MALFORMED_BRANCH = "malformed_branch"
    UNSUPPORTED_SHAPE = "unsupported_shape"


class RockwellGenerationError(AppError):
    """Base for every failure raised by the Rockwell generators."""

    status_code = 422
    code = "rockwell_generation_error"


class GenerationRefused(RockwellGenerationError):
    """The model contains something this generator will not write to a file.

    Carries the offending name and where it was found, so a caller can turn it
    into a validation result without re-deriving either. The message is written
    to be shown to an engineer unedited: a refusal without a subject is
    indistinguishable from a broken tool.
    """

    code = "rockwell_generation_refused"

    def __init__(self, reason: RefusalReason, subject: str, location: str, detail: str) -> None:
        super().__init__(
            f"Refused to generate: {subject} at {location}. {detail}",
            details={
                "reason": str(reason),
                "subject": subject,
                "location": location,
                "detail": detail,
            },
        )
        self.reason = reason
        self.subject = subject
        self.location = location
        self.detail = detail


def _normalise(name: str) -> str:
    """Case-folded identifier used for "is this declared?" comparisons.

    Deliberately *not*
    :func:`~app.parsers.base.normalise_tag_name`: that one folds punctuation so
    ``Guard-Door`` and ``guard door`` match, which is right for comparing a
    spreadsheet against an export and wrong here. A Logix identifier differs
    from another Logix identifier by exactly its characters, and a generator
    that treated ``Motor_Run`` and ``MotorRun`` as the same tag would emit a
    reference to a tag that does not exist.
    """
    return name.strip().upper()


def _base_symbol(operand_text: str) -> str:
    """The declared tag an operand names: ``CV101_Status.Running`` -> ``CV101_Status``."""
    text = operand_text.strip()
    if text.startswith("Program:"):
        # ``Program:Other.TagName`` reaches into another program's scope.
        text = text.split(".", 1)[1] if "." in text else ""
    return re.split(r"[.\[]", text, maxsplit=1)[0].strip()


# ---------------------------------------------------------------------------
# Context the emitters need
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ControllerContext:
    """The controller header every component export is wrapped in.

    A component L5X is not a free-standing document: Studio 5000 writes the
    target inside a ``<Controller Use="Context">`` so the importer knows what it
    belongs to, and :mod:`app.parsers.l5x_parser` refuses a file with no
    ``<Controller>`` at all. So every emitter takes one of these.
    """

    name: str
    processor_type: str = ""
    major_revision: int | None = None
    minor_revision: int | None = None
    description: str = ""
    project_creation_date: str = ""
    use_as_folder: bool = False
    schema_revision: str = DEFAULT_SCHEMA_REVISION
    software_revision: str = ""
    #: Empty by default and written only when supplied. See the module docstring:
    #: a clock reading here would make two generations of one revision differ.
    export_date: str = ""

    @classmethod
    def from_model(
        cls,
        controller: ControllerModel,
        *,
        schema_revision: str = DEFAULT_SCHEMA_REVISION,
        software_revision: str = "",
        export_date: str = "",
    ) -> ControllerContext:
        return cls(
            name=controller.name or "",
            processor_type=controller.catalog_number or "",
            major_revision=controller.major_revision,
            minor_revision=controller.minor_revision,
            description=controller.description or "",
            project_creation_date=controller.project_creation_date or "",
            use_as_folder=bool(controller.use_as_folder),
            schema_revision=schema_revision or DEFAULT_SCHEMA_REVISION,
            software_revision=software_revision,
            export_date=export_date,
        )


@dataclass(frozen=True, slots=True)
class LogicScope:
    """What names a rung is allowed to reference, and for which controller.

    Held as pre-normalised frozen sets because the check runs once per operand
    over an entire project, and because building it once at the top is what
    makes "this tag is not declared" a decision with a single definition rather
    than a lookup each caller improvises.
    """

    family: ControllerFamily
    tag_names: frozenset[str] = frozenset()
    routine_names: frozenset[str] = frozenset()
    aoi_names: frozenset[str] = frozenset()

    @classmethod
    def build(
        cls,
        *,
        family: ControllerFamily,
        tags: Iterable[str] = (),
        routines: Iterable[str] = (),
        aois: Iterable[str] = (),
    ) -> LogicScope:
        return cls(
            family=family,
            tag_names=frozenset(_normalise(name) for name in tags if name),
            routine_names=frozenset(_normalise(name) for name in routines if name),
            aoi_names=frozenset(_normalise(name) for name in aois if name),
        )


# ---------------------------------------------------------------------------
# Rung rendering
# ---------------------------------------------------------------------------


def render_instruction_text(instruction: LogicInstruction) -> str:
    """``XIC(Motor_Run)``, rendered from the operand rows.

    Operands are written in ``ordinal`` order using their ``text`` exactly as
    stored, because argument position is what gives an operand its meaning and
    the stored text is what carries subscripts, bit selectors and members. A
    zero-operand instruction is written with empty parentheses, which is the
    form Studio 5000 writes and which the parser reads either way.

    A catalogued mnemonic is written in the catalogue's canonical upper case. An
    Add-On Instruction call is written **exactly as the model stored it**:
    ``MotorStarter`` is a name the customer chose and Logix matches it
    case-sensitively, so upper-casing it would emit a call to an instruction
    that does not exist.
    """
    operands = sorted(instruction.operands or [], key=lambda operand: operand.ordinal)
    rendered = ",".join((operand.text or "").strip() for operand in operands)
    spec = spec_for(instruction.mnemonic)
    mnemonic = spec.mnemonic if spec is not None else instruction.mnemonic.strip()
    return f"{mnemonic}({rendered})"


def _check_instruction(instruction: LogicInstruction, scope: LogicScope, location: str) -> None:
    mnemonic = _normalise(instruction.mnemonic)
    if not mnemonic:
        raise GenerationRefused(
            RefusalReason.UNSUPPORTED_INSTRUCTION,
            "(unnamed instruction)",
            location,
            "The instruction row carries no mnemonic, so there is nothing to emit.",
        )
    if is_restricted(mnemonic):
        raise GenerationRefused(
            RefusalReason.RESTRICTED_INSTRUCTION,
            mnemonic,
            location,
            restriction_reason(mnemonic),
        )
    # ``is False`` rather than ``not``: an instruction row that never had the
    # flag written is not the same as one the model marked unsupported, and a
    # transient object carries ``None`` until it is flushed.
    if instruction.is_supported is False:
        raise GenerationRefused(
            RefusalReason.UNSUPPORTED_INSTRUCTION,
            mnemonic,
            location,
            instruction.unsupported_reason
            or "The model records this instruction as unsupported, so it is not regenerated.",
        )

    operand_count = len(instruction.operands or [])
    spec = spec_for(mnemonic)
    if spec is None:
        # The one legitimate way to be absent from the catalogue: a call to an
        # Add-On Instruction this project declares. Rockwell's catalogue cannot
        # contain those, because the customer invents them.
        is_aoi_call = (
            instruction.category is InstructionCategory.ADDON_INSTRUCTION
            and mnemonic in scope.aoi_names
        )
        if not is_aoi_call:
            raise GenerationRefused(
                RefusalReason.UNSUPPORTED_INSTRUCTION,
                mnemonic,
                location,
                "This mnemonic is not in the instruction catalogue and is not an Add-On "
                "Instruction this project declares, so nothing here can say what it writes "
                "or which controllers accept it.",
            )
    else:
        if not supports(mnemonic, scope.family):
            raise GenerationRefused(
                RefusalReason.UNSUPPORTED_INSTRUCTION,
                mnemonic,
                location,
                f"{spec.name} is not generated for controller family "
                f"'{scope.family}'. Supported families: "
                f"{', '.join(sorted(str(item) for item in spec.supported_families))}.",
            )
        if not spec.accepts_operand_count(operand_count):
            raise GenerationRefused(
                RefusalReason.OPERAND_COUNT,
                mnemonic,
                location,
                f"{operand_count} operand(s) given; {spec.name} takes "
                f"{spec.min_operands} to {spec.max_operands}.",
            )

    for operand in sorted(instruction.operands or [], key=lambda item: item.ordinal):
        text = (operand.text or "").strip()
        where = f"{location} / {mnemonic} operand {operand.ordinal}"
        if operand.kind is OperandKind.UNRESOLVED:
            raise GenerationRefused(
                RefusalReason.UNRESOLVED_OPERAND,
                text or "(empty operand)",
                where,
                operand.unresolved_reason
                or "The operand was never resolved and the model states no reason.",
            )
        if operand.kind is OperandKind.TAG:
            symbol = (operand.resolved_name or "").strip() or _base_symbol(text)
            if not symbol or _normalise(symbol) not in scope.tag_names:
                raise GenerationRefused(
                    RefusalReason.UNDECLARED_TAG,
                    symbol or text or "(empty operand)",
                    where,
                    "No tag of that name is declared in controller scope or in this "
                    "program's scope, so a rung referencing it would fail at import.",
                )
        elif operand.kind is OperandKind.ROUTINE_REFERENCE:
            if _normalise(text) not in scope.routine_names:
                raise GenerationRefused(
                    RefusalReason.UNDECLARED_ROUTINE,
                    text or "(empty operand)",
                    where,
                    "No routine of that name is declared in this program, so the call "
                    "would have no target.",
                )


def _check_branch_rows(rung: LogicRung, legs_by_level: dict[int, int], location: str) -> None:
    """Cross-check the coordinates against the branch tree, when there is one.

    A :class:`~app.models.plc.LogicBranch` row is one *leg*, so the number of
    rows at a level is the number of legs at that level. Comparing that against
    what the instruction coordinates produced is the check the two views exist
    to make possible; a mismatch means the rung is not the rung somebody
    reviewed, and it is refused rather than rendered plausibly.
    """
    branches = list(rung.branches or [])
    if not branches:
        return
    declared: dict[int, int] = {}
    for branch in branches:
        if branch.level < 1:
            raise GenerationRefused(
                RefusalReason.MALFORMED_BRANCH,
                f"branch level {branch.level}",
                location,
                "A branch leg sits at level 0, which is the main rung line rather than a branch.",
            )
        declared[branch.level] = declared.get(branch.level, 0) + 1
    if declared != legs_by_level:
        expected = ", ".join(f"level {level}: {count}" for level, count in sorted(declared.items()))
        actual = ", ".join(
            f"level {level}: {count}" for level, count in sorted(legs_by_level.items())
        )
        raise GenerationRefused(
            RefusalReason.MALFORMED_BRANCH,
            f"rung {rung.ordinal}",
            location,
            f"The branch rows describe {expected or 'no legs'} but the instruction "
            f"coordinates describe {actual or 'no legs'}. The two views of this rung "
            "disagree, so neither can be trusted to be the reviewed one.",
        )


def render_rung_text(rung: LogicRung, scope: LogicScope, *, location: str = "") -> str:
    """Render one rung: conditions in series, branches as ``[leg,leg]``, ``;`` last.

    The instruction list in ``ordinal`` order plus each instruction's
    ``(branch_level, branch_index)`` fully determines the nesting: a deeper
    level opens a branch, an incremented index steps to the next leg, a
    shallower level closes back out. Rendering is therefore one flat pass, and
    the branch rows are used to check the result rather than to drive it.
    """
    where = location or f"rung {rung.ordinal}"
    instructions = sorted(rung.instructions or [], key=lambda item: item.ordinal)
    parts: list[str] = []
    #: One entry per open branch level, holding the leg currently being written.
    stack: list[int] = []
    #: Legs completed per level, for the cross-check against the branch rows.
    legs_by_level: dict[int, int] = {}

    def close_to(level: int) -> None:
        while len(stack) > level:
            finished = stack.pop()
            depth = len(stack) + 1
            legs_by_level[depth] = legs_by_level.get(depth, 0) + finished + 1
            parts.append("]")

    for instruction in instructions:
        target_level = instruction.branch_level or 0
        target_leg = instruction.branch_index or 0
        if target_level < 0 or target_leg < 0:
            raise GenerationRefused(
                RefusalReason.MALFORMED_BRANCH,
                _normalise(instruction.mnemonic),
                where,
                f"Negative branch coordinates ({target_level}, {target_leg}).",
            )
        close_to(target_level)
        if target_level > len(stack):
            if target_level > len(stack) + 1:
                raise GenerationRefused(
                    RefusalReason.MALFORMED_BRANCH,
                    _normalise(instruction.mnemonic),
                    where,
                    f"Branch level jumps from {len(stack)} to {target_level}. A branch "
                    "level with no instruction on it cannot be rendered, because nothing "
                    "says which leg of the outer branch it sits on.",
                )
            if target_leg != 0:
                raise GenerationRefused(
                    RefusalReason.MALFORMED_BRANCH,
                    _normalise(instruction.mnemonic),
                    where,
                    f"A branch opens at leg {target_leg}. The first instruction inside a "
                    "branch is on leg 0.",
                )
            parts.append("[")
            stack.append(0)
        elif target_level == 0:
            if target_leg != 0:
                raise GenerationRefused(
                    RefusalReason.MALFORMED_BRANCH,
                    _normalise(instruction.mnemonic),
                    where,
                    f"An instruction on the main rung line carries leg {target_leg}. "
                    "Level 0 has one leg and it is leg 0.",
                )
        else:
            current = stack[-1]
            if target_leg == current + 1:
                parts.append(",")
                stack[-1] = target_leg
            elif target_leg != current:
                raise GenerationRefused(
                    RefusalReason.MALFORMED_BRANCH,
                    _normalise(instruction.mnemonic),
                    where,
                    f"Branch leg goes from {current} to {target_leg}. Legs are written "
                    "in order and a skipped leg would render as a leg that is not there.",
                )
        _check_instruction(instruction, scope, where)
        parts.append(render_instruction_text(instruction))

    close_to(0)
    _check_branch_rows(rung, legs_by_level, where)
    return "".join(parts) + ";"


# ---------------------------------------------------------------------------
# Shared element builders
# ---------------------------------------------------------------------------


def _boolean(value: bool) -> str:
    return "true" if value else "false"


def _description(text: str) -> XmlElement | None:
    """``<Description>`` when there is one, and nothing at all when there is not.

    An empty element would claim the source stated a blank description, which is
    a different fact from having stated none.
    """
    stripped = (text or "").strip()
    return element("Description", cdata=stripped) if stripped else None


def _dimension_attribute(dimensions: str, subject: str, location: str) -> str:
    """A UDT member's ``Dimension``: the integer inside ``[16]``, else ``0``."""
    text = (dimensions or "").strip()
    if not text:
        return "0"
    if not _DIMENSIONS.match(text):
        raise GenerationRefused(
            RefusalReason.UNSUPPORTED_SHAPE,
            subject,
            location,
            f"Dimensions {text!r} are not a form this generator writes. A member "
            "dimension has to be a single bracketed integer such as [16].",
        )
    inner = text.strip("[]").split(",")
    if len(inner) > 1:
        raise GenerationRefused(
            RefusalReason.UNSUPPORTED_SHAPE,
            subject,
            location,
            f"Dimensions {text!r} declare a multi-dimensional member, which Logix does "
            "not accept inside a user-defined type.",
        )
    return inner[0].strip()


def _tag_data_type(tag: Tag) -> str:
    concrete = (tag.data_type_name or "").strip()
    base = concrete or str(tag.base_data_type)
    return f"{base}{(tag.dimensions or '').strip()}"


def _tag_element(tag: Tag) -> XmlElement:
    is_alias = bool(tag.is_alias)
    alias_target = (tag.alias_for or "").strip()
    return element(
        "Tag",
        {
            "Name": tag.name,
            "TagType": "Alias" if is_alias else "Base",
            # An alias takes its type from its target, so stating one here would
            # be this generator asserting something the model never resolved.
            "DataType": None if is_alias else _tag_data_type(tag),
            "AliasFor": (alias_target or None) if is_alias else None,
            "Dimensions": (tag.dimensions or "").strip() or None,
            "Constant": _boolean(bool(tag.is_constant)),
            "ExternalAccess": (tag.external_access or "").strip() or None,
        },
        children=(_description(tag.description),),
    )


def _rung_element(rung: LogicRung, scope: LogicScope, *, location: str, number: int) -> XmlElement:
    comment = (rung.comment or "").strip()
    return element(
        "Rung",
        {"Number": str(number), "Type": "N"},
        children=(
            element("Comment", cdata=comment) if comment else None,
            element("Text", cdata=render_rung_text(rung, scope, location=location)),
        ),
    )


def _routine_element(
    routine: Routine,
    rungs: Sequence[LogicRung],
    scope: LogicScope,
    *,
    program_name: str,
    use: str,
) -> XmlElement:
    ordered = sorted(rungs, key=lambda rung: rung.ordinal)
    # ``Number`` is the position in the routine, not ``LogicRung.ordinal``. Logix
    # numbers the rungs of a routine 0..n-1 with no gaps, and a model whose
    # ordinals have gaps (a deleted rung, a re-expanded template block) is still
    # a routine we can write. Diagnostics keep the model's ordinal, because that
    # is the number an engineer will search the model for.
    rung_elements = [
        _rung_element(
            rung,
            scope,
            location=f"{program_name} / {routine.name} / Rung {rung.ordinal}",
            number=number,
        )
        for number, rung in enumerate(ordered)
    ]
    return element(
        "Routine",
        {"Use": use, "Name": routine.name, "Type": "RLL"},
        children=(
            _description(routine.description),
            element("RLLContent", children=rung_elements),
        ),
    )


def _document(
    controller: ControllerContext,
    *,
    target_type: str,
    target_name: str,
    body: Sequence[XmlElement],
    contains_context: bool,
) -> XmlElement:
    """Wrap ``body`` in the ``<RSLogix5000Content><Controller>`` envelope."""
    if contains_context:
        controller_attributes: dict[str, str | None] = {
            "Use": "Context",
            "Name": controller.name,
            "ProcessorType": controller.processor_type or None,
        }
    else:
        controller_attributes = {
            "Use": "Target",
            "Name": controller.name,
            "ProcessorType": controller.processor_type or None,
            "MajorRev": None
            if controller.major_revision is None
            else str(controller.major_revision),
            "MinorRev": None
            if controller.minor_revision is None
            else str(controller.minor_revision),
            "ProjectCreationDate": controller.project_creation_date or None,
            "UseAsFolder": _boolean(controller.use_as_folder),
        }
    return element(
        "RSLogix5000Content",
        {
            "SchemaRevision": controller.schema_revision or DEFAULT_SCHEMA_REVISION,
            "SoftwareRevision": controller.software_revision or None,
            "TargetName": target_name,
            "TargetType": target_type,
            "ContainsContext": _boolean(contains_context),
            "ExportDate": controller.export_date or None,
            "ExportOptions": DEFAULT_EXPORT_OPTIONS,
        },
        children=(element("Controller", controller_attributes, children=body),),
    )


# ---------------------------------------------------------------------------
# Emitters
# ---------------------------------------------------------------------------


def emit_routine(
    *,
    controller: ControllerContext,
    program_name: str,
    routine: Routine,
    rungs: Sequence[LogicRung],
    scope: LogicScope,
) -> bytes:
    """One routine, inside its program's context.

    The export carries no tag declarations: importing it needs the controller
    tags and the program to be in place already, which is exactly what the
    package's import order says.
    """
    routine_element = _routine_element(
        routine, rungs, scope, program_name=program_name, use="Target"
    )
    body = (
        element(
            "Programs",
            children=(
                element(
                    "Program",
                    {"Use": "Context", "Name": program_name},
                    children=(element("Routines", children=(routine_element,)),),
                ),
            ),
        ),
    )
    return serialise(
        _document(
            controller,
            target_type="Routine",
            target_name=routine.name,
            body=body,
            contains_context=True,
        )
    )


def emit_program(
    *,
    controller: ControllerContext,
    program: Program,
    tags: Sequence[Tag],
    routines: Sequence[tuple[Routine, Sequence[LogicRung]]],
    scope: LogicScope,
) -> bytes:
    """One program: its program-scoped tags and its routines.

    The routines are written in full, which is what Studio 5000 does when it
    exports a program. The package also emits each routine on its own, for
    review and for replacing a single routine later; the manifest and
    ``IMPORT_ORDER.md`` say so rather than leaving the duplication to be
    discovered.
    """
    tag_elements = [_tag_element(tag) for tag in sorted(tags, key=lambda tag: tag.name)]
    routine_elements = [
        _routine_element(routine, rungs, scope, program_name=program.name, use="Context")
        for routine, rungs in sorted(routines, key=lambda pair: (pair[0].ordinal, pair[0].name))
    ]
    program_element = element(
        "Program",
        {
            "Use": "Target",
            "Name": program.name,
            "TestEdits": _boolean(bool(program.test_edits)),
            "MainRoutineName": (program.main_routine_name or "").strip() or None,
            "FaultRoutineName": (program.fault_routine_name or "").strip() or None,
            "Disabled": _boolean(bool(program.disabled)),
            "UseAsFolder": "false",
        },
        children=(
            _description(program.description),
            element("Tags", children=tag_elements),
            element("Routines", children=routine_elements),
        ),
    )
    return serialise(
        _document(
            controller,
            target_type="Program",
            target_name=program.name,
            body=(element("Programs", children=(program_element,)),),
            contains_context=True,
        )
    )


def emit_udt(
    *, controller: ControllerContext, udt: UDTDefinition, members: Sequence[UDTMember]
) -> bytes:
    """One user-defined type, with its members in declared order.

    Member order is part of the type's identity — it feeds
    :attr:`~app.models.tags.UDTDefinition.checksum` and, for BOOL members, it
    decides bit packing — so members are written in ``ordinal`` order and never
    sorted by name.
    """
    location = f"UDT {udt.name}"
    member_elements = [
        element(
            "Member",
            {
                "Name": member.name,
                "DataType": (member.data_type_name or "").strip() or str(member.base_data_type),
                "Dimension": _dimension_attribute(
                    member.dimensions, f"{udt.name}.{member.name}", location
                ),
                "Hidden": _boolean(bool(member.hidden)),
                "ExternalAccess": (member.external_access or "").strip() or None,
            },
            children=(_description(member.description),),
        )
        for member in sorted(members, key=lambda item: (item.ordinal, item.name))
    ]
    data_type = element(
        "DataType",
        {"Use": "Target", "Name": udt.name, "Family": "NoFamily", "Class": "User"},
        children=(
            _description(udt.description),
            element("Members", children=member_elements),
        ),
    )
    return serialise(
        _document(
            controller,
            target_type="DataType",
            target_name=udt.name,
            body=(element("DataTypes", children=(data_type,)),),
            contains_context=True,
        )
    )


def emit_aoi_reference(*, controller: ControllerContext, aoi: AOIDefinitionReference) -> bytes:
    """The *interface* of an Add-On Instruction this project uses.

    Never the definition. See :data:`AOI_REFERENCE_NOTICE`, which is written
    into the file's own description so the warning travels with the bytes rather
    than living only in a manifest nobody opened.
    """
    parameters: list[XmlElement] = []
    for raw in aoi.parameters or []:
        if not isinstance(raw, Mapping):
            raise GenerationRefused(
                RefusalReason.UNSUPPORTED_SHAPE,
                aoi.name,
                f"Add-On Instruction {aoi.name}",
                "A parameter descriptor is not a mapping, so its name and type cannot be read.",
            )
        parameter: Mapping[str, Any] = raw
        name = str(parameter.get("name", "")).strip()
        if not name:
            raise GenerationRefused(
                RefusalReason.UNSUPPORTED_SHAPE,
                aoi.name,
                f"Add-On Instruction {aoi.name}",
                "A parameter descriptor carries no name.",
            )
        parameters.append(
            element(
                "Parameter",
                {
                    "Name": name,
                    "TagType": "Base",
                    "DataType": str(parameter.get("data_type", "")).strip() or None,
                    "Usage": str(parameter.get("usage", "")).strip() or None,
                    "Required": _boolean(bool(parameter.get("required"))),
                    "Visible": _boolean(bool(parameter.get("visible"))),
                },
            )
        )
    description = (aoi.description or "").strip()
    notice = f"{AOI_REFERENCE_NOTICE}\n\n{description}" if description else AOI_REFERENCE_NOTICE
    definition = element(
        "AddOnInstructionDefinition",
        {
            "Use": "Target",
            "Name": aoi.name,
            "Revision": (aoi.revision or "").strip() or None,
        },
        children=(
            _description(notice),
            element("Parameters", children=parameters),
        ),
    )
    return serialise(
        _document(
            controller,
            target_type="AddOnInstructionDefinition",
            target_name=aoi.name,
            body=(element("AddOnInstructionDefinitions", children=(definition,)),),
            contains_context=True,
        )
    )


def emit_controller_project(
    *,
    controller: ControllerContext,
    modules: Sequence[HardwareModule],
    tasks: Sequence[tuple[ControllerTask, Sequence[str]]],
    programs: Sequence[Program],
) -> bytes:
    """The controller skeleton everything else is imported into.

    Modules, tasks, and a shell for each program — name, description, main and
    fault routine names, disabled and test-edit flags. Program tags and routines
    are **not** here; they come from the component files, so nothing in the
    package declares the same object twice with different content. The shells
    exist so a task's ``<ScheduledPrograms>`` list resolves to something.

    That split has not been proved against a Studio 5000 import, and
    ``LIMITATIONS.md`` says so.
    """
    module_elements = [
        element(
            "Module",
            {
                "Name": module.name,
                "CatalogNumber": (module.catalog_number or "").strip() or None,
                "Major": None if module.major_revision is None else str(module.major_revision),
                "Minor": None if module.minor_revision is None else str(module.minor_revision),
                "ParentModule": _parent_module_name(module, modules),
                "Inhibited": "false",
                "MajorFault": "false",
            },
            children=(_description(module.description),),
        )
        for module in sorted(modules, key=lambda item: item.name)
    ]
    task_elements = [
        element(
            "Task",
            {
                "Name": task.name,
                "Type": str(task.task_type).upper(),
                "Rate": None if task.rate_ms is None else _number(task.rate_ms),
                "Priority": None if task.priority is None else str(task.priority),
                "Watchdog": None if task.watchdog_ms is None else _number(task.watchdog_ms),
                "DisableUpdateOutputs": _boolean(bool(task.disable_update_outputs)),
                "InhibitTask": _boolean(bool(task.inhibit_task)),
            },
            children=(
                _description(task.description),
                element(
                    "ScheduledPrograms",
                    children=[
                        element("ScheduledProgram", {"Name": name}) for name in scheduled if name
                    ],
                ),
            ),
        )
        for task, scheduled in sorted(tasks, key=lambda pair: (pair[0].ordinal, pair[0].name))
    ]
    program_elements = [
        element(
            "Program",
            {
                "Name": program.name,
                "TestEdits": _boolean(bool(program.test_edits)),
                "MainRoutineName": (program.main_routine_name or "").strip() or None,
                "FaultRoutineName": (program.fault_routine_name or "").strip() or None,
                "Disabled": _boolean(bool(program.disabled)),
                "UseAsFolder": "false",
            },
            children=(
                _description(program.description),
                element("Tags"),
                element("Routines"),
            ),
        )
        for program in sorted(programs, key=lambda item: (item.ordinal, item.name))
    ]
    body = (
        _description(controller.description),
        element("Modules", children=module_elements),
        element("Programs", children=program_elements),
        element("Tasks", children=task_elements),
    )
    return serialise(
        _document(
            controller,
            target_type="Controller",
            target_name=controller.name,
            body=tuple(item for item in body if item is not None),
            contains_context=False,
        )
    )


def _number(value: float) -> str:
    """Render a scan rate without a trailing ``.0`` when it is a whole number."""
    return str(int(value)) if float(value).is_integer() else repr(float(value))


def _parent_module_name(module: HardwareModule, modules: Sequence[HardwareModule]) -> str | None:
    if module.parent_module_id is None:
        return None
    by_id: dict[uuid.UUID, HardwareModule] = {
        item.id: item for item in modules if item.id is not None
    }
    parent = by_id.get(module.parent_module_id)
    return parent.name if parent is not None else None
