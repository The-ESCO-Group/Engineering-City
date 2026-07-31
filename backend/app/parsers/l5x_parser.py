"""Studio 5000 L5X export parser.

An ``.L5X`` file is an *offline* export of a controller project. This parser
records what that export says — controller header, data types, modules, add-on
instruction definitions, tags, programs, routines, rungs, tasks — plus two
derived views cross-file comparison needs: a flat rung list and a tag index
keyed by :func:`~app.parsers.base.normalise_tag_name`.

It deliberately stops there. Rung text is tokenised into mnemonics and operand
*strings* and nothing more: ``GRT(AIR_PRESS_PSI,40.0)`` is recorded as the
mnemonic ``GRT`` over the operands ``AIR_PRESS_PSI`` and ``40.0``. Whether 40.0
is the correct threshold, whether a rung is unreachable, whether an interlock is
fail-safe — those need the surrounding documents, so they belong to an agent.
The parser would have to guess, and a guess that reads like a fact is the one
failure mode this product cannot have. Nothing here describes what a controller
is *running*, either: an export is a file, not a device.

Three consequences are visible in the output:

* An operand that is not declared anywhere in the export becomes an entry in
  ``unresolved_references``, a :data:`~app.parsers.base.WarningCode`
  ``unresolved_reference`` warning, and a ``missing_expected`` anchor carrying
  the expectation ``"declaration of tag X"``. It is never dropped and never
  invented.
* Rung text the tokeniser cannot read becomes an ``unparsed_instruction``
  warning naming the rung, and the raw text is kept verbatim in
  ``rungs[].text`` so a human can still read it.
* Content this parser does not interpret — a non-ladder routine body, the logic
  inside an add-on instruction definition, an unread ``<Controller>`` or
  ``<AddOnInstructionDefinition>`` section, the target of an alias tag — is
  recorded as an :class:`~app.parsers.base.UnsupportedItem` rather than
  omitted, so "we did not look at this" never looks like "there was nothing
  there".

An alias tag illustrates the difference between recording and interpreting: its
``AliasFor`` string is kept verbatim on the tag record, and the fact that the
parser did **not** follow it to the tag or module I/O point it names is a
``tag_alias`` unsupported item. Nothing in the record ever says an alias has no
target when the export gives one.

Every tag and every rung carries the ``anchor_key`` its anchor was filed under.
Two elements can produce the same natural key — two programs of the same name,
two ``<Tags>`` blocks — and when that happens the second anchor is addressed
more specifically (``...#2``) and the clash is reported, so no two anchors ever
share a key. A consumer that wants to cite a tag or rung must copy
``anchor_key`` rather than rebuild it from names, or it will silently cite the
first of two same-named elements.

Branch structure (``[XIC(A),XIC(B)]``) is **flattened** into the instruction
sequence; the brackets survive verbatim in ``rungs[].text``, which is what a
duplicate-rung comparison and a human reviewer both read.
"""

from __future__ import annotations

import re
from typing import Any
from xml.etree.ElementTree import Element

from app.core.enums import AnchorType, EvidenceKind, FileKind
from app.core.errors import ParserError
from app.parsers.base import (
    AnchorDraft,
    ParseContext,
    ParseResult,
    WarningCode,
    normalise_tag_name,
    truncate,
)
from app.security.xml_safety import parse_xml_file, strip_namespace

__all__ = ["L5XParser"]

#: A decimal, real or exponent literal operand.
_NUMERIC_LITERAL = re.compile(r"^[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?$")
#: A radix-qualified literal such as ``16#FF`` or ``2#0101_0101``.
_RADIX_LITERAL = re.compile(r"^[+-]?\d*#[0-9A-Fa-f_.]+$")

#: ``<Controller>`` children this parser reads. Anything else is reported as an
#: unsupported item rather than passed over in silence.
_READ_CONTROLLER_SECTIONS = frozenset(
    {
        "Description",
        "DataTypes",
        "Modules",
        "AddOnInstructionDefinitions",
        "Tags",
        "Programs",
        "Tasks",
    }
)

#: ``<AddOnInstructionDefinition>`` children this parser reads. Anything else —
#: ``<LocalTags>`` above all — is reported rather than passed over in silence.
_READ_ADDON_SECTIONS = frozenset({"Description", "Parameters", "Routines"})

#: Mnemonics whose *first* operand names a routine the export declares. Only
#: there is an operand treated as a subroutine target rather than a signal;
#: anywhere else a name is an operand like any other.
_ROUTINE_TARGET_MNEMONICS = frozenset({"JSR", "JXR"})

#: Mnemonics whose operand is written by the rung. Used only to decide which
#: operands are candidates for ``alarm_references``; no other meaning is
#: attached to them anywhere in this parser.
_OUTPUT_MNEMONICS = frozenset({"OTE", "OTL", "OTU"})

#: Prefix that *suggests* — and only suggests — an alarm tag.
_ALARM_PREFIX = "ALM_"

_UNNAMED = "(unnamed)"


# ---------------------------------------------------------------------------
# XML helpers
# ---------------------------------------------------------------------------


def _child(element: Element, name: str) -> Element | None:
    for candidate in element:
        if strip_namespace(candidate.tag) == name:
            return candidate
    return None


def _children(element: Element, name: str) -> list[Element]:
    return [candidate for candidate in element if strip_namespace(candidate.tag) == name]


def _items(parent: Element, container: str, item: str) -> list[Element]:
    """``item`` children of **every** ``container`` child, in document order.

    An export normally has one ``<Tags>``, one ``<Programs>`` and so on, but a
    hand-edited or merged file can carry more than one. Reading only the first
    would drop the rest without trace, so all of them are read; the unusual
    structure itself is reported by :meth:`_Reader._note_repeated_containers`.
    """
    return [child for holder in _children(parent, container) for child in _children(holder, item)]


def _text_of(element: Element | None) -> str:
    if element is None:
        return ""
    joined = "".join(element.itertext())
    return joined.replace("\r\n", "\n").replace("\r", "\n").strip()


def _description(element: Element) -> str:
    return _text_of(_child(element, "Description"))


def _int_or_raw(value: str | None) -> int | str | None:
    """Integers as integers; anything else preserved verbatim rather than dropped."""
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return text


def _bool_or_raw(value: str | None) -> bool | str | None:
    if value is None:
        return None
    text = value.strip()
    lowered = text.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    return text or None


def _join(*parts: str) -> str:
    return "\n".join(part for part in parts if part)


def _scope_label(origin: tuple[str, str]) -> str:
    """``('program', 'MainProgram')`` as ``program scope (MainProgram)``."""
    scope, program = origin
    return f"{scope or 'unknown'} scope ({program})" if program else f"{scope or 'unknown'} scope"


# ---------------------------------------------------------------------------
# Rung tokeniser
# ---------------------------------------------------------------------------


class _Instruction:
    """One tokenised instruction: a mnemonic over operand strings."""

    __slots__ = ("mnemonic", "operands", "text")

    def __init__(self, mnemonic: str, operands: list[str], text: str) -> None:
        self.mnemonic = mnemonic
        self.operands = operands
        self.text = text


def _read_operand_list(text: str, start: int) -> tuple[list[str], int] | None:
    """Read ``(a,b)`` starting at the ``(`` in ``start``.

    Returns the operands and the index just past the closing parenthesis, or
    ``None`` when the list is never closed — the caller turns that into a
    warning instead of a silently shortened rung.
    """
    operands: list[str] = []
    current: list[str] = []
    depth = 0
    quote = ""
    index = start
    while index < len(text):
        char = text[index]
        index += 1
        if quote:
            current.append(char)
            if char == quote:
                quote = ""
            continue
        if char in "'\"":
            quote = char
            current.append(char)
            continue
        if char == "(":
            depth += 1
            if depth > 1:
                current.append(char)
            continue
        if char == ")":
            depth -= 1
            if depth:
                current.append(char)
                continue
            operand = "".join(current).strip()
            if operand or operands:
                operands.append(operand)
            return operands, index
        if char == "," and depth == 1:
            operands.append("".join(current).strip())
            current = []
            continue
        current.append(char)
    return None


def _tokenise_rung(text: str) -> tuple[list[_Instruction], str]:
    """Tokenise rung text into instructions.

    Returns the instructions read and the first fragment that could not be read
    (empty when the whole rung tokenised). Branch delimiters are skipped, which
    flattens branches into the sequence.

    A bare identifier with no operand list (``AFI``, ``NOP``) is a real
    instruction only if the rest of the rung tokenises. Until then it is held
    back: ``Out := In1 AND In2`` also begins with a bare identifier, and turning
    that into an instruction named ``Out`` would invent a mnemonic out of text
    the tokeniser is about to admit it could not read. Held-back identifiers are
    reported as the start of the unread fragment instead.
    """
    instructions: list[_Instruction] = []
    pending: list[_Instruction] = []
    pending_start = 0
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char.isspace() or char in ";,[]":
            index += 1
            continue
        if not (char.isalpha() or char == "_"):
            return instructions, text[pending_start if pending else index :].strip()
        start = index
        while index < length and (text[index].isalnum() or text[index] == "_"):
            index += 1
        mnemonic = text[start:index]
        cursor = index
        while cursor < length and text[cursor].isspace():
            cursor += 1
        if cursor >= length or text[cursor] != "(":
            if not pending:
                pending_start = start
            pending.append(_Instruction(mnemonic, [], mnemonic))
            continue
        parsed = _read_operand_list(text, cursor)
        if parsed is None:
            return instructions, text[pending_start if pending else start :].strip()
        operands, end = parsed
        instructions.extend(pending)
        pending = []
        instructions.append(_Instruction(mnemonic, operands, text[start:end].strip()))
        index = end
    instructions.extend(pending)
    return instructions, ""


def _operand_symbol(operand: str) -> tuple[str, str] | None:
    """The declarable symbol an operand names, and what kind of thing it is.

    ``None`` for a literal. ``CV101_Status.Running`` yields the base tag
    ``CV101_Status``; ``Local:1:I.Data[0]`` yields the module ``Local`` and the
    kind ``module_io``, because module-scoped aliases are declared by a module
    rather than in ``<Tags>``.
    """
    text = operand.strip()
    if not text or text == "?":
        return None
    if text[0] in "'\"":
        return None
    if _NUMERIC_LITERAL.match(text) or _RADIX_LITERAL.match(text):
        return None
    if ":" in text:
        base = text.split(":", 1)[0].strip()
        return (base, "module_io") if base else None
    base = re.split(r"[.\[]", text, maxsplit=1)[0].strip()
    if not base or not (base[0].isalpha() or base[0] == "_"):
        return None
    return base, "tag"


# ---------------------------------------------------------------------------
# Reader
# ---------------------------------------------------------------------------


class _Reader:
    """Single-use extraction over one parsed L5X tree.

    Kept off :class:`L5XParser` so the parser itself stays stateless and safe to
    share between threads.
    """

    def __init__(self, context: ParseContext, result: ParseResult, root: Element) -> None:
        self.context = context
        self.result = result
        self.root = root
        self.content: dict[str, Any] = {}
        self.rungs: list[dict[str, Any]] = []
        self.alarm_references: list[dict[str, Any]] = []
        self.unresolved: list[dict[str, Any]] = []
        self.tag_index: dict[str, dict[str, Any]] = {}
        self.unparsed_count = 0
        self._anchor_keys: set[str] = set()
        #: Where each ``tag_index`` key came from, as ``(scope, program)``.
        self._index_origin: dict[str, tuple[str, str]] = {}
        # Lookup tables, all keyed by normalised name.
        self._controller_tags: dict[str, str] = {}
        self._program_tags: dict[str, dict[str, str]] = {}
        self._routines: dict[str, set[str]] = {}
        self._aois: set[str] = set()
        self._datatypes: set[str] = set()
        self._modules: set[str] = set()

    # -- entry point --------------------------------------------------------

    def run(self) -> None:
        controller = self._locate_controller()
        self.content["controller"] = self._read_controller(controller)
        self.content["datatypes"] = self._read_datatypes(controller)
        self.content["modules"] = self._read_modules(controller)
        self.content["addon_instructions"] = self._read_addon_instructions(controller)
        tags = self._read_tags(controller, scope="controller", program="")
        self.content["programs"] = self._read_programs(controller, tags)
        self.content["tags"] = tags
        self.content["rungs"] = self.rungs
        self.content["tasks"] = self._read_tasks(controller)
        self._report_unread_sections(controller)
        self._resolve_references()
        self.content["alarm_references"] = self.alarm_references
        self.content["tag_index"] = self.tag_index
        self.content["unresolved_references"] = self.unresolved
        self._order_content()
        self.result.summary = self._build_summary()
        self._warn_if_empty()

    def _locate_controller(self) -> Element:
        root_tag = strip_namespace(self.root.tag)
        if root_tag != "RSLogix5000Content":
            self.result.warn(
                WarningCode.STRUCTURE_UNRECOGNISED,
                f"Root element is <{root_tag}>, not <RSLogix5000Content>. "
                "Read as an L5X export regardless; anything unrecognised is reported.",
                location=f"/{root_tag}",
            )
        controller = _child(self.root, "Controller")
        if controller is None and root_tag == "Controller":
            controller = self.root
        if controller is None:
            raise ParserError(
                "This file has no <Controller> element, so it is not a Studio 5000 "
                "L5X controller export. Re-export the controller, or classify the "
                "file as generic XML.",
                details={"root_element": root_tag},
            )
        return controller

    def _order_content(self) -> None:
        """Fix the key order of ``content`` so re-parses compare byte-for-byte."""
        order = (
            "controller",
            "datatypes",
            "modules",
            "addon_instructions",
            "tags",
            "programs",
            "rungs",
            "tasks",
            "alarm_references",
            "tag_index",
            "unresolved_references",
        )
        self.content = {key: self.content[key] for key in order}

    def _note_repeated_containers(self, parent: Element, container: str, location: str) -> None:
        """Report a second ``<container>`` where an export normally has one.

        The items inside every one of them are read (see :func:`_items`), so
        nothing is lost; what is reported is that the structure is not the one
        this parser expects, which is a fact about the file a reader should see.
        """
        count = len(_children(parent, container))
        if count > 1:
            self.result.warn(
                WarningCode.STRUCTURE_UNRECOGNISED,
                f"{count} <{container}> elements are present here where an export "
                "normally has one. All of them were read, in document order, so "
                "nothing is dropped.",
                location=location,
            )

    # -- controller ---------------------------------------------------------

    def _read_controller(self, controller: Element) -> dict[str, Any]:
        name = (controller.get("Name") or "").strip()
        if not name:
            self.result.warn(
                WarningCode.STRUCTURE_UNRECOGNISED,
                "The <Controller> element has no Name attribute.",
                location="/RSLogix5000Content/Controller",
            )
        record: dict[str, Any] = {
            "name": name,
            "processor_type": (controller.get("ProcessorType") or "").strip(),
            "major_rev": _int_or_raw(controller.get("MajorRev")),
            "minor_rev": _int_or_raw(controller.get("MinorRev")),
            "description": _description(controller),
            "software_revision": (
                self.root.get("SoftwareRevision") or controller.get("SoftwareRevision") or ""
            ).strip(),
            "export_date": (self.root.get("ExportDate") or "").strip(),
            "schema_revision": (self.root.get("SchemaRevision") or "").strip(),
        }
        label = name or _UNNAMED
        # The attributes are quoted as the attributes they are. "Firmware
        # revision X.Y" would be a claim about a device, and this is a file.
        revision_parts = [
            f"{attribute} {record[field]}"
            for attribute, field in (("MajorRev", "major_rev"), ("MinorRev", "minor_rev"))
            if record[field] is not None
        ]
        revision = f"{' / '.join(revision_parts)} (as exported)" if revision_parts else ""
        self._anchor(
            key=f"controller:{label}",
            anchor_type=AnchorType.L5X_CONTROLLER,
            display_location=f"Controller {label}",
            structured_location={
                "controller": name,
                "processor_type": record["processor_type"],
            },
            excerpt=_join(
                f"Controller {label} ({record['processor_type'] or 'processor not stated'})",
                revision,
                f"Exported {record['export_date']}" if record["export_date"] else "",
                record["description"],
            ),
        )
        return record

    def _read_datatypes(self, controller: Element) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        self._note_repeated_containers(controller, "DataTypes", "Controller / DataTypes")
        for element in _items(controller, "DataTypes", "DataType"):
            name = (element.get("Name") or "").strip()
            members = [
                {
                    "name": (member.get("Name") or "").strip(),
                    "data_type": (member.get("DataType") or "").strip(),
                    "dimension": _int_or_raw(member.get("Dimension")),
                    "radix": (member.get("Radix") or "").strip(),
                    "description": _description(member),
                }
                for member in _items(element, "Members", "Member")
            ]
            records.append(
                {
                    "name": name,
                    "family": (element.get("Family") or "").strip(),
                    "class": (element.get("Class") or "").strip(),
                    "members": members,
                }
            )
            self._datatypes.add(normalise_tag_name(name))
        return records

    def _read_modules(self, controller: Element) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        self._note_repeated_containers(controller, "Modules", "Controller / Modules")
        for element in _items(controller, "Modules", "Module"):
            name = (element.get("Name") or "").strip()
            record: dict[str, Any] = {
                "name": name,
                "catalog_number": (element.get("CatalogNumber") or "").strip(),
                "vendor": (element.get("Vendor") or "").strip(),
                "product_type": (element.get("ProductType") or "").strip(),
                "parent_module": (element.get("ParentModule") or "").strip(),
                "inhibited": _bool_or_raw(element.get("Inhibited")),
                "major_fault": _bool_or_raw(element.get("MajorFault")),
                "description": _description(element),
            }
            records.append(record)
            self._modules.add(normalise_tag_name(name))
            label = name or _UNNAMED
            # An attribute the export does not carry is left out of the excerpt
            # rather than rendered as "None", which would read like a value.
            flags = " | ".join(
                f"{attribute}: {record[field]}"
                for attribute, field in (("Inhibited", "inhibited"), ("MajorFault", "major_fault"))
                if record[field] is not None
            )
            self._anchor(
                key=f"module:{label}",
                anchor_type=AnchorType.L5X_MODULE,
                display_location=f"Module {label}",
                structured_location={
                    "module": name,
                    "catalog_number": record["catalog_number"],
                    "parent_module": record["parent_module"],
                },
                excerpt=_join(
                    f"Module {label} | catalog {record['catalog_number'] or 'not stated'} "
                    f"| parent {record['parent_module'] or 'none'}",
                    flags,
                    record["description"],
                ),
            )
        return records

    def _read_addon_instructions(self, controller: Element) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        container = "AddOnInstructionDefinitions"
        self._note_repeated_containers(controller, container, f"Controller / {container}")
        for element in _items(controller, container, "AddOnInstructionDefinition"):
            name = (element.get("Name") or "").strip()
            parameters = [
                {
                    "name": (parameter.get("Name") or "").strip(),
                    "data_type": (parameter.get("DataType") or "").strip(),
                    "usage": (parameter.get("Usage") or "").strip(),
                    "required": _bool_or_raw(parameter.get("Required")),
                    "visible": _bool_or_raw(parameter.get("Visible")),
                }
                for parameter in _items(element, "Parameters", "Parameter")
            ]
            routines = _items(element, "Routines", "Routine")
            routine_names = [(routine.get("Name") or "").strip() for routine in routines]
            record: dict[str, Any] = {
                "name": name,
                "revision": (element.get("Revision") or "").strip(),
                "description": _description(element),
                "parameters": parameters,
                "routine_names": routine_names,
            }
            records.append(record)
            self._aois.add(normalise_tag_name(name))
            label = name or _UNNAMED
            for section in dict.fromkeys(
                strip_namespace(child.tag)
                for child in element
                if strip_namespace(child.tag) not in _READ_ADDON_SECTIONS
            ):
                self.result.mark_unsupported(
                    "addon_instruction_section",
                    f"<{section}> inside this add-on instruction definition is not "
                    "read by this parser, so nothing it declares — a local tag "
                    "among other things — appears in the parsed content or the "
                    "tag index.",
                    location=f"Add-On Instruction {label} / {section}",
                )
            for routine_name in routine_names:
                self.result.mark_unsupported(
                    "addon_instruction_logic",
                    "The logic inside an add-on instruction definition is recorded by "
                    "routine name only; its rungs are not tokenised, so no claim is "
                    "made about what the instruction does internally.",
                    location=f"Add-On Instruction {label} / {routine_name or _UNNAMED}",
                )
            signature = ", ".join(
                f"{parameter['name']} ({parameter['usage'] or '?'} {parameter['data_type'] or '?'})"
                for parameter in parameters
            )
            self._anchor(
                key=f"aoi:{label}",
                anchor_type=AnchorType.L5X_ADDON_INSTRUCTION,
                display_location=f"Add-On Instruction {label}",
                structured_location={
                    "addon_instruction": name,
                    "revision": record["revision"],
                },
                excerpt=_join(
                    f"Add-On Instruction {label} revision {record['revision'] or 'not stated'}",
                    record["description"],
                    f"Parameters: {signature}" if signature else "",
                    f"Routines: {', '.join(routine_names)}" if routine_names else "",
                ),
            )
        return records

    # -- tags ---------------------------------------------------------------

    def _read_tags(self, parent: Element, *, scope: str, program: str) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        holder_location = f"{program} / Tags" if program else "Controller / Tags"
        self._note_repeated_containers(parent, "Tags", holder_location)
        table = self._program_tags.setdefault(program, {}) if program else self._controller_tags
        for element in _items(parent, "Tags", "Tag"):
            name = (element.get("Name") or "").strip()
            where = f"{program} / Tag {name}" if program else f"Controller tag {name}"
            record: dict[str, Any] = {
                "name": name,
                "scope": scope,
                "program": program,
                "data_type": (element.get("DataType") or "").strip(),
                "tag_type": (element.get("TagType") or "").strip(),
                "alias_for": (element.get("AliasFor") or "").strip(),
                "dimensions": (element.get("Dimensions") or "").strip(),
                "constant": _bool_or_raw(element.get("Constant")),
                "external_access": (element.get("ExternalAccess") or "").strip(),
                "radix": (element.get("Radix") or "").strip(),
                "description": _description(element),
                "value": self._tag_value(element, where),
            }
            records.append(record)
            self._report_alias(record, where)
            key = normalise_tag_name(name)
            existing = table.get(key)
            if existing is not None and existing != record["data_type"]:
                self.result.warn(
                    WarningCode.STRUCTURE_UNRECOGNISED,
                    f"Two {scope}-scope tags normalise to the same index key "
                    f"'{key}' with different data types ('{existing}' and "
                    f"'{record['data_type']}'). The first is indexed.",
                    location=where,
                )
            elif existing is None:
                table[key] = record["data_type"]
            self._index_tag(record, declared=True, location=where)
            label = name or _UNNAMED
            record["anchor_key"] = self._anchor(
                key=self._tag_anchor_key(label, scope=scope, program=program, where=where),
                anchor_type=AnchorType.L5X_TAG,
                display_location=where or f"Tag {label}",
                structured_location=(
                    {"scope": scope, "program": program, "tag": name}
                    if program
                    else {"scope": scope, "tag": name}
                ),
                excerpt=_join(
                    f"{label} : {record['data_type'] or 'type not stated'} "
                    f"({scope} scope{f', {program}' if program else ''})",
                    f"AliasFor {record['alias_for']}" if record["alias_for"] else "",
                    f"Dimensions {record['dimensions']}" if record["dimensions"] else "",
                    f"Exported value: {record['value']}" if record["value"] is not None else "",
                    record["description"],
                ),
            )
        return records

    def _report_alias(self, record: dict[str, Any], where: str) -> None:
        """Record that an alias target was read but not followed.

        The ``AliasFor`` string is kept verbatim on the tag, but this parser does
        not resolve it to the tag or module I/O point it names. Saying so is the
        difference between "we did not follow this alias" and "this tag has no
        alias", which would otherwise look identical in the record.
        """
        target = record["alias_for"]
        is_alias = record["tag_type"].strip().lower() == "alias"
        if not target and not is_alias:
            return
        named = f"'{target}'" if target else "a target the export does not name in AliasFor"
        self.result.mark_unsupported(
            "tag_alias",
            f"Tag '{record['name']}' is an alias for {named}. The target is recorded "
            "verbatim in the tag record but is not followed, so references to this "
            "tag are not connected to whatever it aliases.",
            location=where,
        )

    def _tag_anchor_key(self, label: str, *, scope: str, program: str, where: str) -> str:
        """``tag:<scope>/<name>``, qualified by program only if it has to be.

        Two programs may each declare a tag of the same name. The plain key would
        make one citation shadow the other, so the second is program-qualified and
        the clash is reported rather than quietly resolved.
        """
        key = f"tag:{scope}/{label}"
        if key not in self._anchor_keys:
            return key
        self.result.warn(
            WarningCode.STRUCTURE_UNRECOGNISED,
            f"A {scope}-scope tag named '{label}' is already anchored in this "
            "export, so this one is addressed more specifically to keep both citable.",
            location=where,
        )
        qualified = f"tag:{scope}/{program}/{label}" if program else key
        ordinal = 2
        while qualified in self._anchor_keys:
            qualified = f"{key}#{ordinal}"
            ordinal += 1
        return qualified

    def _tag_value(self, element: Element, location: str) -> str | None:
        """The scalar value in a decorated ``<Data>`` block, if there is one."""
        blocks = _children(element, "Data")
        if not blocks:
            return None
        decorated = [block for block in blocks if (block.get("Format") or "") == "Decorated"]
        for block in decorated or blocks:
            scalar = _child(block, "DataValue")
            if scalar is not None and scalar.get("Value") is not None:
                return scalar.get("Value")
        self.result.mark_unsupported(
            "tag_data",
            "Tag data is a structure, an array or an undecorated L5K block. Only "
            "scalar decorated values are extracted, so no value is recorded here.",
            location=location,
        )
        return None

    def _index_tag(self, record: dict[str, Any], *, declared: bool, location: str = "") -> None:
        key = normalise_tag_name(record["name"])
        if not key:
            return
        origin = (record["scope"], record.get("program", ""))
        existing = self.tag_index.get(key)
        if existing is not None:
            previous = self._index_origin.get(key, ("", ""))
            if declared and existing["declared"] and previous != origin:
                # Same name, two different scopes. The index keeps one, so the
                # other would vanish from every cross-file match without this.
                self.result.warn(
                    WarningCode.STRUCTURE_UNRECOGNISED,
                    f"'{record['name']}' is declared at {_scope_label(origin)} and a "
                    f"tag with the same index key '{key}' is already declared at "
                    f"{_scope_label(previous)}. The tag index keeps the first, so "
                    "cross-file matching sees only that one; both declarations are "
                    "in content['tags']. Reference resolution inside this export "
                    "prefers the program-scope declaration, which may not be the "
                    "indexed one.",
                    location=location,
                )
            return
        self._index_origin[key] = origin
        self.tag_index[key] = {
            "name": record["name"],
            "scope": record["scope"],
            "data_type": record["data_type"],
            "declared": declared,
        }

    # -- programs, routines, rungs -----------------------------------------

    def _read_programs(
        self, controller: Element, tags: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        self._note_repeated_containers(controller, "Programs", "Controller / Programs")
        for position, element in enumerate(_items(controller, "Programs", "Program")):
            name = (element.get("Name") or "").strip() or f"(program {position})"
            self._routines.setdefault(name, set()).update(
                normalise_tag_name(routine.get("Name") or "")
                for routine in _items(element, "Routines", "Routine")
            )
            record: dict[str, Any] = {
                "name": name,
                "main_routine": (element.get("MainRoutineName") or "").strip(),
                "description": _description(element),
                "disabled": _bool_or_raw(element.get("Disabled")),
                "routines": [],
            }
            self._anchor(
                key=f"program:{name}",
                anchor_type=AnchorType.L5X_PROGRAM,
                display_location=f"Program {name}",
                structured_location={"program": name},
                excerpt=_join(
                    f"Program {name} | main routine "
                    f"{record['main_routine'] or 'not stated'} | disabled "
                    f"{record['disabled']}",
                    record["description"],
                ),
            )
            tags.extend(self._read_tags(element, scope="program", program=name))
            record["routines"] = self._read_routines(element, name)
            records.append(record)
        return records

    def _read_routines(self, program: Element, program_name: str) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        self._note_repeated_containers(program, "Routines", f"{program_name} / Routines")
        for position, element in enumerate(_items(program, "Routines", "Routine")):
            name = (element.get("Name") or "").strip() or f"(routine {position})"
            routine_type = (element.get("Type") or "").strip()
            where = f"{program_name} / {name}"
            description = _description(element)
            rung_total = len(_items(element, "RLLContent", "Rung"))
            # Anchored before its rungs so the anchor list reads in document order.
            self._anchor(
                key=f"routine:{program_name}/{name}",
                anchor_type=AnchorType.L5X_ROUTINE,
                display_location=where,
                structured_location={
                    "program": program_name,
                    "routine": name,
                    "type": routine_type,
                },
                excerpt=_join(
                    f"Routine {name} ({routine_type or 'type not stated'}) in "
                    f"{program_name} | {rung_total} rung(s)",
                    description,
                ),
            )
            records.append(
                {
                    "name": name,
                    "type": routine_type,
                    "description": description,
                    "rungs": self._read_rungs(element, program_name, name, routine_type, where),
                }
            )
        return records

    def _read_rungs(
        self,
        routine: Element,
        program_name: str,
        routine_name: str,
        routine_type: str,
        where: str,
    ) -> list[int]:
        if _child(routine, "RLLContent") is None:
            self._report_absent_ladder_body(routine, routine_type, where)
            return []
        self._note_repeated_containers(routine, "RLLContent", where)
        elements = _items(routine, "RLLContent", "Rung")
        identifiers = self._rung_identifiers(elements, where)
        numbers: list[int] = []
        for element, number in zip(elements, identifiers, strict=True):
            numbers.append(number)
            comment = _text_of(_child(element, "Comment"))
            text_element = _child(element, "Text")
            text = _text_of(text_element)
            location = f"{where} / Rung {number}"
            if text_element is None or not text:
                self.result.warn(
                    WarningCode.STRUCTURE_UNRECOGNISED,
                    "Rung has no <Text> logic to read.",
                    location=location,
                )
            instructions, unparsed = _tokenise_rung(text)
            if unparsed:
                self.unparsed_count += 1
                self.result.warn(
                    WarningCode.UNPARSED_INSTRUCTION,
                    "Could not tokenise rung text into instructions; the raw text is "
                    f"kept in the rung record. Unread from: {truncate(unparsed, 200)}",
                    location=location,
                )
            rung: dict[str, Any] = {
                "program": program_name,
                "routine": routine_name,
                "number": number,
                "type": (element.get("Type") or "").strip(),
                "comment": comment,
                "text": text,
                "instructions": [
                    {
                        "mnemonic": instruction.mnemonic,
                        "operands": list(instruction.operands),
                        "text": instruction.text,
                        "index": index,
                    }
                    for index, instruction in enumerate(instructions)
                ],
                "referenced_tags": [],
            }
            self.rungs.append(rung)
            rung["anchor_key"] = self._anchor(
                key=f"rung:{program_name}/{routine_name}/{number}",
                anchor_type=AnchorType.L5X_RUNG,
                display_location=location,
                structured_location={
                    "program": program_name,
                    "routine": routine_name,
                    "rung": number,
                },
                excerpt=_join(f"Comment: {comment}" if comment else "", text),
            )
            for instruction in rung["instructions"]:
                self._anchor(
                    key=(
                        f"instruction:{program_name}/{routine_name}/{number}/{instruction['index']}"
                    ),
                    anchor_type=AnchorType.L5X_INSTRUCTION,
                    display_location=(
                        f"{location} / Instruction {instruction['index']} "
                        f"({instruction['mnemonic']})"
                    ),
                    structured_location={
                        "program": program_name,
                        "routine": routine_name,
                        "rung": number,
                        "instruction": instruction["index"],
                        "mnemonic": instruction["mnemonic"],
                    },
                    excerpt=instruction["text"],
                )
        return numbers

    def _rung_identifiers(self, elements: list[Element], where: str) -> list[int]:
        """Rung ``Number`` attributes when they are unique integers, else positions.

        An anchor key has to be unique inside its file or one citation becomes
        ambiguous, so a routine with missing or repeated rung numbers is keyed
        positionally and the oddity is reported.
        """
        raw = [(element.get("Number") or "").strip() for element in elements]
        parsed: list[int] = []
        for value in raw:
            try:
                parsed.append(int(value))
            except ValueError:
                self.result.warn(
                    WarningCode.STRUCTURE_UNRECOGNISED,
                    "Rung numbers are missing or not integers, so rungs in this "
                    "routine are addressed by position instead.",
                    location=where,
                )
                return list(range(len(elements)))
        if len(set(parsed)) != len(parsed):
            self.result.warn(
                WarningCode.STRUCTURE_UNRECOGNISED,
                "Rung numbers repeat inside this routine, so rungs are addressed "
                "by position instead.",
                location=where,
            )
            return list(range(len(elements)))
        return list(parsed)

    def _report_absent_ladder_body(self, routine: Element, routine_type: str, where: str) -> None:
        """Report what stands in place of a ladder body, without inferring why.

        A routine carrying an ``<STContent>`` body is a capability boundary: the
        body is in the export and this parser does not read it. A routine with no
        body element at all is not a boundary — there is nothing there to read,
        and describing a routine the export declares as ``RLL`` as "not ladder
        logic" would assert the opposite of what the file says. The two are
        reported differently because they are different facts, and each message
        names the declared ``Type`` and the body elements actually seen rather
        than drawing a conclusion from either.
        """
        declared = routine_type or "unstated"
        bodies = [
            strip_namespace(child.tag)
            for child in routine
            if strip_namespace(child.tag).endswith("Content")
        ]
        if not bodies:
            self.result.warn(
                WarningCode.STRUCTURE_UNRECOGNISED,
                f"This routine declares Type '{declared}' but carries no body "
                "element, so the export holds no logic for it to read. It "
                "contributes no rungs, instructions or tag references.",
                location=where,
            )
            return
        found = ", ".join(f"<{body}>" for body in bodies)
        self.result.mark_unsupported(
            "routine_body",
            f"This routine declares Type '{declared}' and its body is {found}, "
            "not the <RLLContent> ladder body this parser reads. Its logic is not "
            "extracted, so this routine contributes no rungs, instructions or tag "
            "references.",
            location=where,
        )

    # -- tasks --------------------------------------------------------------

    def _read_tasks(self, controller: Element) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        self._note_repeated_containers(controller, "Tasks", "Controller / Tasks")
        for element in _items(controller, "Tasks", "Task"):
            records.append(
                {
                    "name": (element.get("Name") or "").strip(),
                    "type": (element.get("Type") or "").strip(),
                    "priority": _int_or_raw(element.get("Priority")),
                    "watchdog": _int_or_raw(element.get("Watchdog")),
                    "scheduled_programs": [
                        (scheduled.get("Name") or "").strip()
                        for scheduled in _items(element, "ScheduledPrograms", "ScheduledProgram")
                    ],
                }
            )
        return records

    def _report_unread_sections(self, controller: Element) -> None:
        unread = dict.fromkeys(
            strip_namespace(child.tag)
            for child in controller
            if strip_namespace(child.tag) not in _READ_CONTROLLER_SECTIONS
        )
        for section in unread:
            self.result.mark_unsupported(
                "controller_section",
                f"<{section}> is present in the export but is not read by this "
                "parser, so nothing in it is reflected in the parsed content.",
                location=f"Controller / {section}",
            )

    # -- reference resolution ----------------------------------------------

    def _resolve_references(self) -> None:
        """Second pass: classify every operand against what the export declares."""
        for rung in self.rungs:
            program = rung["program"]
            routine = rung["routine"]
            number = rung["number"]
            references: list[str] = []
            seen_references: set[str] = set()
            seen_unresolved: set[str] = set()
            for instruction in rung["instructions"]:
                mnemonic = str(instruction["mnemonic"]).upper()
                for position, operand in enumerate(instruction["operands"]):
                    symbol = _operand_symbol(operand)
                    if symbol is None:
                        continue
                    base, kind = symbol
                    resolution = self._resolve(
                        base, kind, program, mnemonic=mnemonic, position=position
                    )
                    if resolution == "routine":
                        # The first operand of JSR/JXR is a routine the export
                        # declares, not a signal, so it is not a tag reference.
                        # Anywhere else an operand is left alone: deciding that
                        # some other instruction calls a subroutine would be an
                        # interpretation the export does not support.
                        continue
                    operand_text = operand.strip()
                    if operand_text not in seen_references:
                        seen_references.add(operand_text)
                        references.append(operand_text)
                    if resolution is None and base not in seen_unresolved:
                        seen_unresolved.add(base)
                        self._record_unresolved(rung, base, kind)
                    if mnemonic in _OUTPUT_MNEMONICS and base.upper().startswith(_ALARM_PREFIX):
                        self.alarm_references.append(
                            {
                                "tag": operand_text,
                                "rung": number,
                                "program": program,
                                "routine": routine,
                            }
                        )
            rung["referenced_tags"] = references

    def _resolve(
        self, base: str, kind: str, program: str, *, mnemonic: str, position: int
    ) -> str | None:
        key = normalise_tag_name(base)
        if not key:
            return None
        if kind == "module_io":
            return "module" if key in self._modules else None
        if key in self._program_tags.get(program, {}):
            return "tag"
        if key in self._controller_tags:
            return "tag"
        if (
            mnemonic in _ROUTINE_TARGET_MNEMONICS
            and position == 0
            and key in self._routines.get(program, set())
        ):
            return "routine"
        if key in self._aois:
            return "addon_instruction"
        if key in self._datatypes:
            return "datatype"
        return None

    def _record_unresolved(self, rung: dict[str, Any], base: str, kind: str) -> None:
        program = rung["program"]
        routine = rung["routine"]
        number = rung["number"]
        self.unresolved.append(
            {
                "name": base,
                "program": program,
                "routine": routine,
                "rung": number,
                "kind": kind,
            }
        )
        self._index_tag(
            {"name": base, "scope": "unknown", "data_type": ""},
            declared=False,
        )
        noun = "module" if kind == "module_io" else "tag"
        location = f"{program} / {routine} / Rung {number}"
        self.result.warn(
            WarningCode.UNRESOLVED_REFERENCE,
            f"'{base}' is used in logic but is not declared anywhere in this "
            f"export. Whether the {noun} exists in the live project cannot be "
            "determined from an export.",
            location=location,
        )
        self._anchor(
            key=f"missing:{kind}/{base}/{program}/{routine}/{number}",
            anchor_type=AnchorType.L5X_TAG,
            display_location=f"{location} (no declaration of {base})",
            structured_location={
                "program": program,
                "routine": routine,
                "rung": number,
                "tag": base,
            },
            excerpt=_join(
                f"Comment: {rung['comment']}" if rung["comment"] else "",
                rung["text"],
            ),
            evidence_kind=EvidenceKind.MISSING_EXPECTED,
            expectation=f"declaration of {noun} {base}",
        )

    # -- summary ------------------------------------------------------------

    def _build_summary(self) -> dict[str, Any]:
        tags: list[dict[str, Any]] = self.content["tags"]
        programs: list[dict[str, Any]] = self.content["programs"]
        controller: dict[str, Any] = self.content["controller"]
        mnemonics = {
            instruction["mnemonic"] for rung in self.rungs for instruction in rung["instructions"]
        }
        instruction_count = sum(len(rung["instructions"]) for rung in self.rungs)
        return {
            "controller_name": controller["name"],
            "processor_type": controller["processor_type"],
            "datatype_count": len(self.content["datatypes"]),
            "module_count": len(self.content["modules"]),
            "addon_instruction_count": len(self.content["addon_instructions"]),
            "task_count": len(self.content["tasks"]),
            "program_count": len(programs),
            "program_names": [program["name"] for program in programs],
            "routine_count": sum(len(program["routines"]) for program in programs),
            "rung_count": len(self.rungs),
            "instruction_count": instruction_count,
            "tag_count": len(tags),
            "controller_tag_count": sum(1 for tag in tags if tag["scope"] == "controller"),
            "program_tag_count": sum(1 for tag in tags if tag["scope"] == "program"),
            "indexed_tag_count": len(self.tag_index),
            "alarm_reference_count": len(self.alarm_references),
            "unresolved_reference_count": len(self.unresolved),
            "unparsed_rung_count": self.unparsed_count,
            "mnemonics": sorted(mnemonics),
        }

    def _warn_if_empty(self) -> None:
        populated = any(
            self.content[key]
            for key in ("datatypes", "modules", "addon_instructions", "tags", "programs", "tasks")
        )
        if populated:
            return
        self.result.warn(
            WarningCode.EMPTY_DOCUMENT,
            "The export declares a controller but contains no data types, modules, "
            "add-on instructions, tags, programs or tasks. There is nothing here to "
            "review; it is not evidence that the project is empty.",
            location=f"Controller {self.content['controller']['name'] or _UNNAMED}",
        )

    # -- anchors ------------------------------------------------------------

    def _anchor(
        self,
        *,
        key: str,
        anchor_type: AnchorType,
        display_location: str,
        structured_location: dict[str, Any],
        excerpt: str,
        evidence_kind: EvidenceKind = EvidenceKind.DIRECT,
        expectation: str = "",
    ) -> str:
        """Emit one anchor and return the key it was actually filed under."""
        if key in self._anchor_keys:
            # A repeated key would make one citation shadow another, so the
            # second element is addressed more specifically and the clash is
            # reported. Emitting the duplicate instead would leave two anchors
            # answering to one key, and which one a lookup found would depend on
            # whether it read the list or the database.
            unique = key
            ordinal = 2
            while unique in self._anchor_keys:
                unique = f"{key}#{ordinal}"
                ordinal += 1
            self.result.warn(
                WarningCode.STRUCTURE_UNRECOGNISED,
                f"Two elements produced the same anchor key '{key}'. This one is "
                f"addressed as '{unique}' so both stay citable; anything that "
                f"rebuilds '{key}' from names alone will reach the first.",
                location=display_location,
            )
            key = unique
        self._anchor_keys.add(key)
        self.result.add_anchor(
            AnchorDraft(
                key=key,
                anchor_type=anchor_type,
                display_location=display_location,
                structured_location=structured_location,
                excerpt=excerpt,
                evidence_kind=evidence_kind,
                source_label=self.context.source_label,
                expectation=expectation,
            )
        )
        return key


class L5XParser:
    """Parser for Studio 5000 ``.L5X`` controller exports."""

    name = "l5x"
    #: 1.1.0: alias targets recorded, anchor keys de-duplicated, tag and rung
    #: records carry ``anchor_key`` — stored anchors from 1.0.0 do not match.
    version = "1.1.0"
    handles: tuple[FileKind, ...] = (FileKind.L5X,)

    def parse(self, context: ParseContext) -> ParseResult:
        result = ParseResult(
            artifact_kind=FileKind.L5X.value,
            parser_name=self.name,
            parser_version=self.version,
        )
        root = parse_xml_file(context.path, context.settings)
        reader = _Reader(context, result, root)
        reader.run()
        result.content = reader.content
        return result
