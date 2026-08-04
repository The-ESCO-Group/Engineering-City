"""The normalised project evidence graph.

Every parser produces facts about one file. This module joins them into one
cross-file structure so the review agents can compare a documented requirement
against the logic that is supposed to implement it.

**It assembles facts and never draws conclusions.** The distinction is the whole
product. So, concretely:

* a documented interlock whose trip tag matches no declared tag is recorded as
  ``matched_tag=None`` with a list of *candidate* similar names — the graph does
  not decide it is a naming mismatch;
* a tag referenced in logic but never declared is recorded as
  ``declared=False`` — the graph does not decide that is a fault;
* a documented threshold and a logic comparison are both recorded verbatim — the
  graph does not decide they contradict.

Each of those is a judgement for an agent to make, with evidence, at a stated
confidence, and for a human to approve. Every entry therefore carries the
``(file_id, anchor_key)`` pairs it came from, so whatever an agent concludes can
be cited back to the source.
"""

from __future__ import annotations

import difflib
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.enums import DocumentRole, FileKind, FileStatus
from app.core.logging import get_logger
from app.models.file import ParsedArtifact, UploadedFile
from app.parsers.base import normalise_tag_name

logger = get_logger(__name__)

#: Minimum similarity for one tag name to be offered as a candidate match for
#: another. Tuned so ``GUARD_DOOR_CLOSED`` / ``GRD_DR_CLSD`` surfaces as a
#: candidate while unrelated names do not. It selects what an agent is asked to
#: *consider*, never what it concludes.
SIMILARITY_THRESHOLD = 0.55

#: Column headers, normalised, that identify each field of an interlock schedule.
#: Several spellings per field because a real schedule is whatever the engineer
#: who wrote it called things.
_INTERLOCK_HEADER_ALIASES: dict[str, tuple[str, ...]] = {
    "identifier": ("interlock id", "id", "interlock", "ilk", "tag no", "item"),
    "description": ("description", "interlock description", "function", "name"),
    "trip_tag": ("trip tag", "tag", "signal", "input tag", "source tag", "device tag"),
    "trip_condition": ("trip condition", "condition", "setpoint", "trip", "logic"),
    "action": ("action", "effect", "result", "response"),
    "equipment": ("affected equipment", "equipment", "affects", "asset"),
    "failsafe": ("fail-safe behaviour", "fail-safe behavior", "fail safe", "failsafe"),
    "notes": ("notes", "note", "comment", "comments", "remarks"),
}

_IO_HEADER_ALIASES: dict[str, tuple[str, ...]] = {
    "tag": ("tag", "tag name", "signal", "point", "device tag"),
    "description": ("description", "service", "point description"),
    "signal_type": ("type", "io type", "i o type", "signal type", "point type"),
    "module": ("module", "card", "rack"),
    "address": ("address", "io address", "channel", "terminal"),
    "units": ("engineering units", "units", "uom"),
}

_ALARM_HEADER_ALIASES: dict[str, tuple[str, ...]] = {
    "name": ("alarm tag", "tag", "alarm", "name", "alarm name"),
    "message": ("message", "text", "description", "alarm message"),
    "severity": ("severity", "priority", "class"),
    "group": ("group", "area", "category"),
    "source_tag": ("source tag", "trigger", "condition tag", "input tag"),
}


@dataclass(slots=True)
class Provenance:
    """Where a fact came from, precise enough to cite."""

    file_id: uuid.UUID
    filename: str
    anchor_key: str
    display_location: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "file_id": str(self.file_id),
            "filename": self.filename,
            "anchor_key": self.anchor_key,
            "display_location": self.display_location,
        }


@dataclass(slots=True)
class TagFact:
    """Everything known about one signal name across every uploaded document."""

    name: str
    normalised: str
    data_type: str = ""
    scope: str = ""
    description: str = ""
    #: Declared as a tag in an L5X export.
    declared_in_logic: bool = False
    #: For a structured reference like ``CV101_Status.Running``, the base tag
    #: (``CV101_Status``) and whether *it* is declared. Without this, every member
    #: of a declared UDT reads as an undeclared reference, which would hand the
    #: PLC Review Agent a pile of false unresolved-reference candidates.
    base_tag: str = ""
    base_declared: bool = False
    #: Appears as an operand on at least one rung.
    referenced_in_logic: bool = False
    #: Appears in an I/O list.
    in_io_list: bool = False
    #: Appears in an interlock schedule as a trip tag.
    in_interlock_schedule: bool = False
    #: Appears in an alarm list or HMI export.
    in_alarm_list: bool = False
    rung_keys: list[str] = field(default_factory=list)
    sources: list[Provenance] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "normalised": self.normalised,
            "data_type": self.data_type,
            "scope": self.scope,
            "description": self.description,
            "declared_in_logic": self.declared_in_logic,
            "base_tag": self.base_tag,
            "base_declared": self.base_declared,
            "referenced_in_logic": self.referenced_in_logic,
            "in_io_list": self.in_io_list,
            "in_interlock_schedule": self.in_interlock_schedule,
            "in_alarm_list": self.in_alarm_list,
            "rung_keys": self.rung_keys,
            "sources": [source.as_dict() for source in self.sources],
        }


@dataclass(slots=True)
class InterlockRequirement:
    """One documented interlock, exactly as written down.

    ``matched_tag`` is the declared tag whose normalised name equals
    ``trip_tag``'s, or ``None``. ``candidate_tags`` holds near-misses for an agent
    to consider. Neither field decides anything.
    """

    identifier: str
    description: str = ""
    trip_tag: str = ""
    trip_condition: str = ""
    action: str = ""
    equipment: str = ""
    failsafe: str = ""
    notes: str = ""
    #: Worksheet the row came from.
    sheet: str = ""
    #: True when that worksheet is hidden in the workbook. A hidden sheet is
    #: typically superseded or working content, so a row from one must not be
    #: compared against logic as though it were a current requirement — but it
    #: must not be dropped either, because content an engineer cannot see is
    #: exactly what a review should surface.
    from_hidden_sheet: bool = False
    #: Fields the schedule left blank. An incomplete requirement cannot support a
    #: technical claim, and this is how an agent knows that.
    missing_fields: list[str] = field(default_factory=list)
    matched_tag: str | None = None
    candidate_tags: list[dict[str, Any]] = field(default_factory=list)
    #: Rungs that reference the matched or candidate tag, if any.
    related_rung_keys: list[str] = field(default_factory=list)
    sources: list[Provenance] = field(default_factory=list)

    @property
    def is_specified(self) -> bool:
        """Whether the schedule says enough to compare against logic at all."""
        return bool(self.trip_tag) and bool(self.trip_condition)

    @property
    def is_current(self) -> bool:
        """Whether this should be treated as a live requirement."""
        return not self.from_hidden_sheet

    def as_dict(self) -> dict[str, Any]:
        return {
            "identifier": self.identifier,
            "description": self.description,
            "trip_tag": self.trip_tag,
            "trip_condition": self.trip_condition,
            "action": self.action,
            "equipment": self.equipment,
            "failsafe": self.failsafe,
            "notes": self.notes,
            "sheet": self.sheet,
            "from_hidden_sheet": self.from_hidden_sheet,
            "is_current": self.is_current,
            "missing_fields": self.missing_fields,
            "is_specified": self.is_specified,
            "matched_tag": self.matched_tag,
            "candidate_tags": self.candidate_tags,
            "related_rung_keys": self.related_rung_keys,
            "sources": [source.as_dict() for source in self.sources],
        }


@dataclass(slots=True)
class LogicFact:
    """One rung, normalised, with the tags it touches."""

    program: str
    routine: str
    rung: int
    comment: str = ""
    text: str = ""
    instructions: list[dict[str, Any]] = field(default_factory=list)
    referenced_tags: list[str] = field(default_factory=list)
    #: Operands of output instructions (OTE/OTL/OTU and AOI outputs).
    outputs: list[str] = field(default_factory=list)
    anchor_key: str = ""
    file_id: uuid.UUID | None = None
    filename: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "program": self.program,
            "routine": self.routine,
            "rung": self.rung,
            "comment": self.comment,
            "text": self.text,
            "instructions": self.instructions,
            "referenced_tags": self.referenced_tags,
            "outputs": self.outputs,
            "anchor_key": self.anchor_key,
            "file_id": str(self.file_id) if self.file_id else None,
            "filename": self.filename,
        }


@dataclass(slots=True)
class DocumentFact:
    """A narrative document's headings and statements, for citing requirements."""

    file_id: uuid.UUID
    filename: str
    document_role: str
    #: ``[{anchor_key, display_location, text}]`` for citable statements.
    statements: list[dict[str, Any]] = field(default_factory=list)
    headings: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "file_id": str(self.file_id),
            "filename": self.filename,
            "document_role": self.document_role,
            "statements": self.statements,
            "headings": self.headings,
        }


@dataclass(slots=True)
class ParserLimitation:
    """Something a parser could not read, carried forward into the graph.

    Present so an agent can say "this document was not readable" instead of
    reviewing a project as though the document were empty.
    """

    file_id: uuid.UUID
    filename: str
    code: str
    message: str
    location: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "file_id": str(self.file_id),
            "filename": self.filename,
            "code": self.code,
            "message": self.message,
            "location": self.location,
        }


@dataclass(slots=True)
class EvidenceGraph:
    """The joined, cross-file view of one project."""

    project_id: uuid.UUID
    tags: dict[str, TagFact] = field(default_factory=dict)
    interlocks: list[InterlockRequirement] = field(default_factory=list)
    logic: list[LogicFact] = field(default_factory=list)
    io_points: list[dict[str, Any]] = field(default_factory=list)
    alarms: list[dict[str, Any]] = field(default_factory=list)
    documents: list[DocumentFact] = field(default_factory=list)
    limitations: list[ParserLimitation] = field(default_factory=list)
    #: Expected inputs that were not supplied at all, by document role.
    missing_inputs: list[str] = field(default_factory=list)
    #: Files present but unparsed or unreadable.
    unreadable_files: list[dict[str, Any]] = field(default_factory=list)

    # -- queries agents use ----------------------------------------------
    def tag(self, name: str) -> TagFact | None:
        return self.tags.get(normalise_tag_name(name))

    def rungs_referencing(self, name: str) -> list[LogicFact]:
        target = normalise_tag_name(name)
        return [
            fact
            for fact in self.logic
            if any(normalise_tag_name(tag) == target for tag in fact.referenced_tags)
        ]

    @property
    def has_logic(self) -> bool:
        return bool(self.logic)

    @property
    def has_interlock_schedule(self) -> bool:
        return bool(self.interlocks)

    def counts(self) -> dict[str, int]:
        return {
            "tags": len(self.tags),
            "tags_declared": sum(1 for t in self.tags.values() if t.declared_in_logic),
            "tags_referenced_only": sum(
                1 for t in self.tags.values() if t.referenced_in_logic and not t.declared_in_logic
            ),
            "interlocks": sum(1 for i in self.interlocks if i.is_current),
            "interlocks_unspecified": sum(
                1 for i in self.interlocks if i.is_current and not i.is_specified
            ),
            "interlocks_from_hidden_sheets": sum(1 for i in self.interlocks if not i.is_current),
            "rungs": len(self.logic),
            "io_points": len(self.io_points),
            "alarms": len(self.alarms),
            "documents": len(self.documents),
            "limitations": len(self.limitations),
            "unreadable_files": len(self.unreadable_files),
        }

    def as_dict(self) -> dict[str, Any]:
        """Serialise for an agent prompt or a task result. Deterministic ordering."""
        return {
            "project_id": str(self.project_id),
            "counts": self.counts(),
            "tags": [self.tags[key].as_dict() for key in sorted(self.tags)],
            "interlocks": [item.as_dict() for item in self.interlocks],
            "logic": [item.as_dict() for item in self.logic],
            "io_points": self.io_points,
            "alarms": self.alarms,
            "documents": [item.as_dict() for item in self.documents],
            "limitations": [item.as_dict() for item in self.limitations],
            "missing_inputs": self.missing_inputs,
            "unreadable_files": self.unreadable_files,
        }


# ---------------------------------------------------------------------------
# Header matching
# ---------------------------------------------------------------------------
def _match_headers(headers: list[Any], aliases: dict[str, tuple[str, ...]]) -> dict[str, str]:
    """Map a field name to the actual header text that represents it.

    Matching is on the normalised header, and a field is only claimed by one
    header, so a sheet with both "Tag" and "Trip Tag" resolves each once.
    """
    normalised = {
        normalise_tag_name(str(header)): str(header) for header in headers if header is not None
    }
    resolved: dict[str, str] = {}
    claimed: set[str] = set()

    for field_name, candidates in aliases.items():
        for candidate in candidates:
            key = normalise_tag_name(candidate)
            if key in normalised and key not in claimed:
                resolved[field_name] = normalised[key]
                claimed.add(key)
                break
    return resolved


def _value(record: dict[str, Any], headers: dict[str, str], field_name: str) -> str:
    header = headers.get(field_name)
    if header is None:
        return ""
    raw = record.get(header)
    if raw is None:
        return ""
    return str(raw).strip()


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------
class _Builder:
    """Accumulates artifacts into a graph. One instance per build."""

    def __init__(self, project_id: uuid.UUID) -> None:
        self.graph = EvidenceGraph(project_id=project_id)
        self._roles_seen: set[str] = set()

    # -- tags ----------------------------------------------------------
    def _tag(self, name: str) -> TagFact | None:
        """Get or create the fact for a tag name.

        Base-tag folding: ``CV101_Status.Interlocked`` is recorded under
        ``CV101_Status`` as well as itself, because the schedule names the base
        tag while the logic addresses a member of it, and a comparison that
        missed that would report a false discrepancy.
        """
        cleaned = str(name).strip()
        if not cleaned or _looks_like_literal(cleaned):
            return None
        key = normalise_tag_name(cleaned)
        if not key:
            return None
        fact = self.graph.tags.get(key)
        if fact is None:
            fact = TagFact(name=cleaned, normalised=key)
            self.graph.tags[key] = fact
        return fact

    def _base_tag(self, name: str) -> TagFact | None:
        base = str(name).split(".", 1)[0].split("[", 1)[0]
        if base == name:
            return None
        member = self._tag(name)
        if member is not None:
            member.base_tag = base
        return self._tag(base)

    def _resolve_member_declarations(self) -> None:
        """Mark structured references whose base tag is declared.

        ``CV101_Status.Running`` is a member of the declared UDT tag
        ``CV101_Status``. It has no declaration of its own and never will, so
        without this every UDT member looks like an undeclared reference.
        """
        for fact in self.graph.tags.values():
            if not fact.base_tag:
                continue
            base = self.graph.tags.get(normalise_tag_name(fact.base_tag))
            fact.base_declared = bool(base and base.declared_in_logic)

    # -- per-artifact ingestion ----------------------------------------
    def add_l5x(self, record: UploadedFile, artifact: ParsedArtifact) -> None:
        content = artifact.content
        filename = record.original_filename

        for tag in _as_list(content.get("tags")):
            fact = self._tag(str(tag.get("name", "")))
            if fact is None:
                continue
            fact.declared_in_logic = True
            fact.data_type = fact.data_type or str(tag.get("data_type", ""))
            fact.scope = fact.scope or str(tag.get("scope", ""))
            fact.description = fact.description or str(tag.get("description", ""))
            scope = str(tag.get("scope", "controller"))
            fact.sources.append(
                Provenance(
                    file_id=record.id,
                    filename=filename,
                    # Prefer the key the parser actually emitted. Reconstructing
                    # it here would silently diverge whenever the parser
                    # re-qualifies a key to break a collision — two programs
                    # declaring the same tag name — and the citation would then
                    # resolve to the other program's tag.
                    anchor_key=str(tag.get("anchor_key") or f"tag:{scope}/{tag.get('name', '')}"),
                    display_location=f"{filename} / {scope} tag {tag.get('name', '')}",
                )
            )

        for rung in _as_list(content.get("rungs")):
            program = str(rung.get("program", ""))
            routine = str(rung.get("routine", ""))
            number = _as_int(rung.get("number"))
            anchor_key = str(rung.get("anchor_key") or f"rung:{program}/{routine}/{number}")
            referenced = [str(tag) for tag in _as_list_of_str(rung.get("referenced_tags"))]
            instructions = _as_list(rung.get("instructions"))

            logic_fact = LogicFact(
                program=program,
                routine=routine,
                rung=number,
                comment=str(rung.get("comment", "")),
                text=str(rung.get("text", "")),
                instructions=instructions,
                referenced_tags=referenced,
                outputs=_output_operands(instructions),
                anchor_key=anchor_key,
                file_id=record.id,
                filename=filename,
            )
            self.graph.logic.append(logic_fact)

            for name in referenced:
                tag_fact = self._tag(name)
                if tag_fact is None:
                    continue
                tag_fact.referenced_in_logic = True
                if anchor_key not in tag_fact.rung_keys:
                    tag_fact.rung_keys.append(anchor_key)
                base = self._base_tag(name)
                if base is not None:
                    base.referenced_in_logic = True
                    if anchor_key not in base.rung_keys:
                        base.rung_keys.append(anchor_key)

        # The parser already worked out which operands are undeclared; trust its
        # answer rather than recomputing it differently here.
        for unresolved in _as_list(content.get("unresolved_references")):
            fact = self._tag(str(unresolved.get("name", "")))
            if fact is not None:
                fact.declared_in_logic = False

    def add_interlock_schedule(self, record: UploadedFile, artifact: ParsedArtifact) -> None:
        filename = record.original_filename
        workbook = _as_dict(artifact.content.get("workbook"))
        hidden_sheets = {
            normalise_tag_name(name) for name in _as_list_of_str(workbook.get("hidden_sheet_names"))
        }

        for table in _as_list(artifact.content.get("candidate_tables")):
            headers = _match_headers(
                _as_list_of_str(table.get("headers")), _INTERLOCK_HEADER_ALIASES
            )
            if "identifier" not in headers and "trip_tag" not in headers:
                # Not an interlock table. Ignoring it here is safe because the
                # parser already surfaced any sheet it could not structure.
                continue
            sheet = str(table.get("sheet", ""))

            for entry in _as_list(table.get("records")):
                record_values = _as_dict(entry.get("values"))
                row = _as_int(entry.get("row"))
                identifier = _value(record_values, headers, "identifier")
                trip_tag = _value(record_values, headers, "trip_tag")
                if not identifier and not trip_tag:
                    continue

                requirement = InterlockRequirement(
                    identifier=identifier or f"{sheet} row {row}",
                    description=_value(record_values, headers, "description"),
                    trip_tag=trip_tag,
                    trip_condition=_value(record_values, headers, "trip_condition"),
                    action=_value(record_values, headers, "action"),
                    equipment=_value(record_values, headers, "equipment"),
                    failsafe=_value(record_values, headers, "failsafe"),
                    notes=_value(record_values, headers, "notes"),
                    sheet=sheet,
                    from_hidden_sheet=normalise_tag_name(sheet) in hidden_sheets,
                )
                requirement.missing_fields = [
                    name
                    for name in ("trip_tag", "trip_condition", "action", "failsafe")
                    if not getattr(requirement, name)
                ]
                # The key the parser actually emitted for this row, not one
                # composed here. A fabricated key resolves to no anchor, and the
                # failure is silent: the Interlock Review Agent simply has
                # nothing to cite and reports no discrepancies at all.
                ref = str(entry.get("ref") or "").strip()
                anchor_key = f"range:{sheet}!{ref}" if ref else f"cell:{sheet}!A{row}"
                requirement.sources.append(
                    Provenance(
                        file_id=record.id,
                        filename=filename,
                        anchor_key=anchor_key,
                        display_location=f"{filename} / {sheet} / row {row}",
                    )
                )
                self.graph.interlocks.append(requirement)

                # A tag named only on a hidden sheet is not a current documented
                # requirement, so it must not make the tag look documented.
                if trip_tag and requirement.is_current:
                    fact = self._tag(trip_tag)
                    if fact is not None:
                        fact.in_interlock_schedule = True
                        fact.sources.append(requirement.sources[-1])

    def add_io_list(self, record: UploadedFile, artifact: ParsedArtifact) -> None:
        filename = record.original_filename
        headers = _match_headers(
            _as_list_of_str(artifact.content.get("headers")), _IO_HEADER_ALIASES
        )
        if "tag" not in headers:
            return

        for row_entry in _as_list(artifact.content.get("rows")):
            values = _as_dict(row_entry.get("record"))
            row = _as_int(row_entry.get("row"))
            tag_name = _value(values, headers, "tag")
            if not tag_name:
                continue

            point: dict[str, Any] = {
                "tag": tag_name,
                "description": _value(values, headers, "description"),
                "signal_type": _value(values, headers, "signal_type"),
                "module": _value(values, headers, "module"),
                "address": _value(values, headers, "address"),
                "units": _value(values, headers, "units"),
                "source": {
                    "file_id": str(record.id),
                    "filename": filename,
                    "anchor_key": f"row:{row}",
                    "display_location": f"{filename} / row {row}",
                },
            }
            self.graph.io_points.append(point)

            fact = self._tag(tag_name)
            if fact is not None:
                fact.in_io_list = True
                fact.description = fact.description or str(point["description"])
                fact.sources.append(
                    Provenance(
                        file_id=record.id,
                        filename=filename,
                        anchor_key=f"row:{row}",
                        display_location=f"{filename} / row {row}",
                    )
                )

    def add_alarm_list(self, record: UploadedFile, artifact: ParsedArtifact) -> None:
        filename = record.original_filename
        content = artifact.content

        if artifact.artifact_kind == FileKind.CSV.value:
            headers = _match_headers(_as_list_of_str(content.get("headers")), _ALARM_HEADER_ALIASES)
            if "name" not in headers:
                return
            for row_entry in _as_list(content.get("rows")):
                values = _as_dict(row_entry.get("record"))
                row = _as_int(row_entry.get("row"))
                name = _value(values, headers, "name")
                if not name:
                    continue
                self._add_alarm(
                    record,
                    name=name,
                    message=_value(values, headers, "message"),
                    severity=_value(values, headers, "severity"),
                    group=_value(values, headers, "group"),
                    source_tag=_value(values, headers, "source_tag"),
                    anchor_key=f"row:{row}",
                    display_location=f"{filename} / row {row}",
                )
            return

        # Generic XML (an HMI or alarm-server export): take elements whose tag
        # looks like an alarm and read their attributes.
        for element in _as_list(content.get("elements")):
            tag_name = normalise_tag_name(str(element.get("tag", "")))
            if tag_name != "alarm":
                continue
            attributes = _as_dict(element.get("attributes"))
            name = str(attributes.get("Name") or attributes.get("name") or "").strip()
            if not name:
                continue
            xpath = str(element.get("xpath", ""))
            # ``descendant_text`` is present only when the element's own text is
            # interrupted by a child, and it is the complete message; ``text`` is
            # just the parent's own runs and would silently truncate such a
            # message mid-sentence.
            message = str(element.get("descendant_text") or element.get("text") or "").strip()
            self._add_alarm(
                record,
                name=name,
                message=message,
                severity=str(attributes.get("Severity") or attributes.get("severity") or ""),
                group=str(attributes.get("Group") or attributes.get("group") or ""),
                source_tag="",
                expression=str(attributes.get("Expression") or ""),
                anchor_key=f"element:{xpath}",
                display_location=f"{filename} / {xpath}",
            )

    def _add_alarm(
        self,
        record: UploadedFile,
        *,
        name: str,
        message: str,
        severity: str,
        group: str,
        source_tag: str,
        anchor_key: str,
        display_location: str,
        expression: str = "",
    ) -> None:
        self.graph.alarms.append(
            {
                "name": name,
                "message": message,
                "severity": severity,
                "group": group,
                "source_tag": source_tag,
                "expression": expression,
                "source": {
                    "file_id": str(record.id),
                    "filename": record.original_filename,
                    "anchor_key": anchor_key,
                    "display_location": display_location,
                },
            }
        )
        provenance = Provenance(
            file_id=record.id,
            filename=record.original_filename,
            anchor_key=anchor_key,
            display_location=display_location,
        )
        for candidate in (name, source_tag):
            if not candidate:
                continue
            fact = self._tag(candidate)
            if fact is not None:
                fact.in_alarm_list = True
                fact.sources.append(provenance)

    def add_document(self, record: UploadedFile, artifact: ParsedArtifact) -> None:
        """Index a narrative document's statements so a requirement can cite one."""
        content = artifact.content
        document = DocumentFact(
            file_id=record.id,
            filename=record.original_filename,
            document_role=record.document_role.value,
        )

        if artifact.artifact_kind == FileKind.DOCX.value:
            document.headings = [
                {
                    "level": _as_int(heading.get("level")),
                    "text": str(heading.get("text", "")),
                    "anchor_key": f"heading:{_as_int(heading.get('index'))}",
                }
                for heading in _as_list(content.get("headings"))
            ]
            for paragraph in _as_list(content.get("paragraphs")):
                text = str(paragraph.get("text", "")).strip()
                if not text:
                    continue
                index = _as_int(paragraph.get("index"))
                # A heading is anchored as ``heading:<index>``, not
                # ``paragraph:<index>`` — composing the wrong one here gives a
                # statement a citation that resolves to nothing.
                is_heading = paragraph.get("heading_level") is not None
                document.statements.append(
                    {
                        "anchor_key": f"heading:{index}" if is_heading else f"paragraph:{index}",
                        "section": " / ".join(_as_list_of_str(paragraph.get("section_path"))),
                        "text": text,
                    }
                )
        elif artifact.artifact_kind == FileKind.PDF.value:
            for page in _as_list(content.get("pages")):
                number = _as_int(page.get("page"))
                for line in _as_list(page.get("lines")):
                    text = str(line.get("text", "")).strip()
                    if not text:
                        continue
                    document.statements.append(
                        {
                            "anchor_key": f"page:{number}",
                            "section": f"page {number}",
                            "text": text,
                        }
                    )
                # Annotation and form-field text is real content of the document
                # that is not in the page's text layer. A filled-in test record
                # ("FAIL - punch item PL3-014") lives here, so leaving it out
                # would mean the comparison agents never see the very statement
                # the engineer wrote. Labelled so it is never mistaken for body
                # text; the page anchor is the citation, as annotations have no
                # anchor of their own.
                for annotation in _as_list(page.get("annotations")):
                    text = str(annotation.get("text", "")).strip()
                    if not text:
                        continue
                    document.statements.append(
                        {
                            "anchor_key": f"page:{number}",
                            "section": f"page {number} annotation",
                            "text": text,
                        }
                    )
        elif artifact.artifact_kind == FileKind.TEXT.value:
            for line in _as_list(content.get("lines")):
                text = str(line.get("text", "")).strip()
                if not text:
                    continue
                number = _as_int(line.get("line"))
                document.statements.append(
                    {
                        "anchor_key": f"line:{number}",
                        "section": f"line {number}",
                        "text": text,
                    }
                )

        if document.statements or document.headings:
            self.graph.documents.append(document)

    def add_limitations(self, record: UploadedFile, artifact: ParsedArtifact) -> None:
        for warning in _as_list(artifact.warnings):
            self.graph.limitations.append(
                ParserLimitation(
                    file_id=record.id,
                    filename=record.original_filename,
                    code=str(warning.get("code", "")),
                    message=str(warning.get("message", "")),
                    location=str(warning.get("location", "")),
                )
            )
        for item in _as_list(artifact.unsupported):
            self.graph.limitations.append(
                ParserLimitation(
                    file_id=record.id,
                    filename=record.original_filename,
                    code="unsupported_content",
                    message=f"{item.get('kind', '')}: {item.get('detail', '')}",
                    location=str(item.get("location", "")),
                )
            )

    # -- cross-linking -------------------------------------------------
    def link_interlocks_to_logic(self) -> None:
        """Attach each requirement's matching and near-matching tags.

        Exact normalised equality gives ``matched_tag``. Everything else becomes a
        ranked ``candidate_tags`` list. The graph stops there: whether a candidate
        represents the same signal under a different name is an engineering
        judgement, and it is the Interlock Review Agent's to make with evidence.
        """
        declared = {key: fact for key, fact in self.graph.tags.items() if fact.declared_in_logic}
        declared_keys = sorted(declared)

        for requirement in self.graph.interlocks:
            if not requirement.trip_tag or not requirement.is_current:
                continue
            key = normalise_tag_name(requirement.trip_tag)

            if key in declared:
                requirement.matched_tag = declared[key].name
                requirement.related_rung_keys = list(declared[key].rung_keys)
                continue

            requirement.candidate_tags = _candidates(key, declared_keys, declared)
            for candidate in requirement.candidate_tags:
                fact = declared.get(normalise_tag_name(str(candidate["name"])))
                if fact is None:
                    continue
                for rung_key in fact.rung_keys:
                    if rung_key not in requirement.related_rung_keys:
                        requirement.related_rung_keys.append(rung_key)

    def note_missing_inputs(self, files: list[UploadedFile]) -> None:
        """Record which expected engineering inputs were never supplied.

        Absence is a fact worth stating: a review with no interlock schedule is a
        different thing from a review that found no discrepancies.
        """
        present = {record.document_role for record in files}
        kinds = {record.file_kind for record in files}

        expected: list[tuple[str, bool]] = [
            (
                "Studio 5000 L5X export (control logic)",
                FileKind.L5X in kinds or DocumentRole.CONTROL_LOGIC in present,
            ),
            ("Interlock schedule", DocumentRole.INTERLOCK_SCHEDULE in present),
            ("I/O list", DocumentRole.IO_LIST in present),
            ("Functional description", DocumentRole.FUNCTIONAL_DESCRIPTION in present),
        ]
        self.graph.missing_inputs = [label for label, found in expected if not found]

    def note_unreadable(self, files: list[UploadedFile]) -> None:
        for record in files:
            if record.file_kind is FileKind.ZIP:
                continue
            if record.status in {FileStatus.PARSED, FileStatus.DELETED}:
                continue
            self.graph.unreadable_files.append(
                {
                    "file_id": str(record.id),
                    "filename": record.original_filename,
                    "file_kind": record.file_kind.value,
                    "status": record.status.value,
                    "detail": record.status_detail,
                }
            )


def _candidates(
    key: str, declared_keys: list[str], declared: dict[str, TagFact]
) -> list[dict[str, Any]]:
    """Ranked near-matches for a tag name that has no exact match.

    Two complementary measures, because the interesting real-world case is a
    contraction (``GUARD_DOOR_CLOSED`` vs ``GRD_DR_CLSD``) which sequence
    similarity alone scores poorly:

    * ``difflib`` ratio on the whole normalised string;
    * a subsequence check on the initials of each word.
    """
    scored: list[tuple[float, str, str]] = []
    initials = _initials(key)

    for other in declared_keys:
        ratio = difflib.SequenceMatcher(None, key, other).ratio()
        reason = "sequence similarity"
        if _initials(other) and initials and _initials(other) == initials:
            ratio = max(ratio, 0.75)
            reason = "same word initials"
        elif _is_contraction(key, other) or _is_contraction(other, key):
            ratio = max(ratio, 0.7)
            reason = "one name is a contraction of the other"
        if ratio >= SIMILARITY_THRESHOLD:
            scored.append((ratio, other, reason))

    # Sort by score then name so the list is deterministic.
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [
        {
            "name": declared[other].name,
            "normalised": other,
            "score": round(score, 3),
            "reason": reason,
        }
        for score, other, reason in scored[:5]
    ]


def _initials(normalised: str) -> str:
    return "".join(word[0] for word in normalised.split() if word)


def _is_contraction(short: str, long: str) -> bool:
    """Whether ``short`` is ``long`` with letters removed, in order.

    ``grd dr clsd`` is a contraction of ``guard door closed``; used only to
    propose a candidate, never to assert a match.
    """
    a = short.replace(" ", "")
    b = long.replace(" ", "")
    if not a or not b or len(a) >= len(b):
        return False
    iterator = iter(b)
    return all(char in iterator for char in a)


def _looks_like_literal(name: str) -> bool:
    """Whether an operand is a constant rather than a tag reference.

    ``GRT(AIR_PRESS_PSI, 40.0)`` has one tag and one literal; treating ``40.0``
    as an undeclared tag would manufacture a finding out of nothing.
    """
    text = name.strip()
    if not text:
        return True
    if text[0].isdigit() or text[0] in "+-.":
        try:
            float(text)
        except ValueError:
            return False
        return True
    return text.startswith(("'", '"'))


_OUTPUT_MNEMONICS = frozenset({"OTE", "OTL", "OTU", "MOV", "CTU", "CTD", "TON", "TOF", "RTO"})


def _output_operands(instructions: list[dict[str, Any]]) -> list[str]:
    """Operands written by output instructions, as recorded by the parser.

    A structural reading of the instruction list, not an evaluation: it says
    which operands appear in an output position, nothing about what the rung does.
    """
    outputs: list[str] = []
    for instruction in instructions:
        mnemonic = str(instruction.get("mnemonic", "")).upper()
        operands = _as_list_of_str(instruction.get("operands"))
        if mnemonic in _OUTPUT_MNEMONICS and operands:
            outputs.append(operands[0])
    return outputs


# -- defensive coercion ----------------------------------------------------
# Artifact content comes from JSON written by a parser. The graph must not fall
# over because one parser recorded a field slightly differently; it should ignore
# what it cannot use and keep going.
def _as_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _as_list_of_str(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if item is not None]


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def _file_order(record: UploadedFile) -> tuple[int, datetime, str, uuid.UUID]:
    """A total order over a project's files, with no way for two rows to tie.

    L5X first: the tag declarations it provides are what everything else is
    matched against.

    Then ``created_at`` — and then a tiebreaker, which is the load-bearing part.
    ``created_at`` is a client-side ``utcnow()`` and every member of an uploaded
    ZIP is written in one tight loop, so a whole project's files routinely share
    a timestamp to the microsecond. A tie used to fall through to whatever row
    order the database happened to return, which no engine guarantees, and this
    builder is order-sensitive: :meth:`_Builder._tag` keeps the *first* spelling
    of a name and :meth:`_Builder.add_l5x` keeps the first non-empty data type,
    scope and description. Two runs over the same upload could hand the review
    agents a different data type and a different rung order, so a review was not
    reproducible.
    """
    return (
        0 if record.file_kind is FileKind.L5X else 1,
        record.created_at,
        record.original_filename,
        record.id,
    )


def build_graph(db: Session, project_id: uuid.UUID) -> EvidenceGraph:
    """Join every parsed artifact in a project into one evidence graph."""
    files = list(
        db.execute(
            select(UploadedFile)
            .where(
                UploadedFile.project_id == project_id,
                UploadedFile.deleted_at.is_(None),
            )
            .order_by(
                UploadedFile.created_at,
                UploadedFile.original_filename,
                UploadedFile.id,
            )
        ).scalars()
    )
    artifacts_by_file: dict[uuid.UUID, ParsedArtifact] = {}
    for artifact in db.execute(
        select(ParsedArtifact)
        .where(ParsedArtifact.project_id == project_id)
        # Later artifacts overwrite earlier ones, so a re-parse wins. ``id``
        # breaks a timestamp tie: without it, which parse of a file the graph is
        # built from would be the database's choice rather than ours.
        .order_by(ParsedArtifact.created_at, ParsedArtifact.id)
    ).scalars():
        artifacts_by_file[artifact.file_id] = artifact

    builder = _Builder(project_id)

    ordered = sorted(
        (record for record in files if record.id in artifacts_by_file),
        key=_file_order,
    )

    for record in ordered:
        artifact = artifacts_by_file[record.id]
        builder.add_limitations(record, artifact)

        if record.file_kind is FileKind.L5X:
            builder.add_l5x(record, artifact)
            continue

        role = record.document_role
        if role is DocumentRole.INTERLOCK_SCHEDULE or artifact.artifact_kind == FileKind.XLSX.value:
            builder.add_interlock_schedule(record, artifact)
        if role is DocumentRole.IO_LIST:
            builder.add_io_list(record, artifact)
        if role is DocumentRole.ALARM_LIST or role is DocumentRole.HMI_EXPORT:
            builder.add_alarm_list(record, artifact)
        if role in {
            DocumentRole.FUNCTIONAL_DESCRIPTION,
            DocumentRole.TESTING_NOTES,
            DocumentRole.OTHER,
            DocumentRole.UNCLASSIFIED,
        } or artifact.artifact_kind in {
            FileKind.DOCX.value,
            FileKind.PDF.value,
            FileKind.TEXT.value,
        }:
            builder.add_document(record, artifact)

    builder._resolve_member_declarations()
    builder.link_interlocks_to_logic()
    builder.note_missing_inputs(files)
    builder.note_unreadable(files)

    graph = builder.graph
    logger.info(
        "evidence graph built",
        extra={"project_id": str(project_id), **graph.counts()},
    )
    return graph


def tag_usage_summary(graph: EvidenceGraph) -> dict[str, list[str]]:
    """Group tag names by how they appear across the project.

    A convenience for agent prompts. Every bucket is a statement of fact about
    where a name occurs; none of them is a conclusion about whether that is right.
    """
    buckets: dict[str, list[str]] = defaultdict(list)
    for fact in graph.tags.values():
        if fact.referenced_in_logic and not fact.declared_in_logic:
            # A member of a declared UDT has no declaration of its own and never
            # will; listing it here would be a false lead, not a finding.
            if fact.base_declared:
                buckets["udt_members"].append(fact.name)
            else:
                buckets["referenced_but_not_declared"].append(fact.name)
        if fact.declared_in_logic and not fact.referenced_in_logic:
            buckets["declared_but_not_referenced"].append(fact.name)
        if fact.in_io_list and not fact.referenced_in_logic:
            buckets["in_io_list_but_not_in_logic"].append(fact.name)
        if fact.in_interlock_schedule and not fact.declared_in_logic:
            buckets["documented_but_not_declared"].append(fact.name)
        if fact.in_alarm_list and not fact.declared_in_logic:
            buckets["alarm_without_declaration"].append(fact.name)
    return {key: sorted(values) for key, values in sorted(buckets.items())}
