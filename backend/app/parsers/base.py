"""The parser interface.

A parser turns one stored file into two things:

* a **normalised document** (``ParseResult.content``) that agents and
  cross-file comparison can reason about; and
* a set of **evidence anchor drafts** — addressable citations back into the
  original file, which is what lets a finding be checked by a human.

Three rules every parser follows:

1. **Never guess silently.** Content that cannot be interpreted becomes a
   :class:`ParserWarning` or an :class:`UnsupportedItem`, not an omission. A
   review that reports nothing because the parser quietly failed is worse than
   one that reports it could not read the file.

2. **Never claim more than the format supports.** An L5X export describes
   offline logic; it says nothing about what a controller is running. Parsers
   record structure and text, and leave interpretation to agents.

3. **Be deterministic.** The same bytes must always yield the same content,
   summary and anchors, so ``content_hash`` is meaningful and tests are stable.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from app.core.config import Settings
from app.core.enums import AnchorType, DocumentRole, EvidenceKind, FileKind
from app.models.evidence import MAX_EXCERPT_CHARS


class WarningCode:
    """Stable warning codes. Clients and reports branch on these."""

    HIDDEN_CONTENT = "hidden_content"
    EMPTY_DOCUMENT = "empty_document"
    NO_TEXT_LAYER = "no_text_layer"
    LOW_TEXT_YIELD = "low_text_yield"
    ENCODING_GUESSED = "encoding_guessed"
    DELIMITER_GUESSED = "delimiter_guessed"
    RAGGED_ROWS = "ragged_rows"
    MERGED_CELLS = "merged_cells"
    FORMULA_NOT_EVALUATED = "formula_not_evaluated"
    UNRESOLVED_REFERENCE = "unresolved_reference"
    UNPARSED_INSTRUCTION = "unparsed_instruction"
    TRUNCATED = "truncated"
    STRUCTURE_UNRECOGNISED = "structure_unrecognised"
    PASSWORD_PROTECTED = "password_protected"  # noqa: S105 - a warning code, not a credential


@dataclass(slots=True)
class ParserWarning:
    """Something the parser could not do, or did approximately.

    ``location`` is a human-readable pointer ("Sheet 'Interlocks', row 14") so a
    warning is actionable rather than merely alarming.
    """

    code: str
    message: str
    location: str = ""
    severity: str = "warning"

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "location": self.location,
            "severity": self.severity,
        }


@dataclass(slots=True)
class UnsupportedItem:
    """Content the parser deliberately did not interpret.

    Distinct from a warning: this is a documented capability boundary, recorded
    so that "we did not look at this" is visible in the record instead of being
    indistinguishable from "there was nothing there".
    """

    kind: str
    detail: str
    location: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "detail": self.detail, "location": self.location}


@dataclass(slots=True)
class AnchorDraft:
    """A citation a parser produced, before it is persisted.

    ``key`` is a stable, parser-assigned identifier used to look an anchor up
    while building findings (e.g. ``rung:MainProgram/Conveyor_Interlocks/0``).
    Two parses of the same bytes produce the same keys, which is what makes a
    re-parse idempotent.
    """

    key: str
    anchor_type: AnchorType
    display_location: str
    structured_location: dict[str, Any] = field(default_factory=dict)
    excerpt: str = ""
    evidence_kind: EvidenceKind = EvidenceKind.DIRECT
    source_label: str = ""
    #: Populated on ``missing_expected`` anchors: what was sought and not found.
    expectation: str = ""

    def __post_init__(self) -> None:
        # Bounded so a finding can never smuggle an entire document into the
        # record via its evidence.
        if len(self.excerpt) > MAX_EXCERPT_CHARS:
            self.excerpt = self.excerpt[: MAX_EXCERPT_CHARS - 3] + "..."

    @property
    def content_hash(self) -> str:
        """Hash of the excerpt as extracted, fixing it at anchor-creation time."""
        return hashlib.sha256(self.excerpt.encode("utf-8")).hexdigest()


@dataclass(slots=True)
class ParseContext:
    """Everything a parser is given. Read-only by convention.

    A parser receives a path and settings — no database session and no way to
    write outside the artifact it returns.
    """

    file_id: uuid.UUID
    project_id: uuid.UUID
    path: Path
    filename: str
    file_kind: FileKind
    document_role: DocumentRole
    settings: Settings
    #: Provenance when the file came out of an archive.
    archive_member_path: str = ""

    @property
    def source_label(self) -> str:
        """Short label naming the source, used on every anchor from this file."""
        if self.archive_member_path:
            return f"{self.filename} ({self.archive_member_path})"
        return self.filename


@dataclass(slots=True)
class ParseResult:
    """What a parser returns."""

    artifact_kind: str
    parser_name: str
    parser_version: str
    #: Normalised document. Shape is per artifact kind; see docs/backend/parsers.md.
    content: dict[str, Any] = field(default_factory=dict)
    #: Counts and shape hints, cheap enough to show without loading ``content``.
    summary: dict[str, Any] = field(default_factory=dict)
    warnings: list[ParserWarning] = field(default_factory=list)
    unsupported: list[UnsupportedItem] = field(default_factory=list)
    anchors: list[AnchorDraft] = field(default_factory=list)

    def warn(self, code: str, message: str, location: str = "", severity: str = "warning") -> None:
        self.warnings.append(
            ParserWarning(code=code, message=message, location=location, severity=severity)
        )

    def mark_unsupported(self, kind: str, detail: str, location: str = "") -> None:
        self.unsupported.append(UnsupportedItem(kind=kind, detail=detail, location=location))

    def add_anchor(self, anchor: AnchorDraft) -> AnchorDraft:
        self.anchors.append(anchor)
        return anchor

    @property
    def anchors_by_key(self) -> dict[str, AnchorDraft]:
        return {anchor.key: anchor for anchor in self.anchors}


@runtime_checkable
class Parser(Protocol):
    """A file parser.

    Implementations are stateless: ``parse`` must be safe to call concurrently
    for different files.
    """

    #: Stable name, recorded on every artifact and anchor.
    name: str
    #: Bumped whenever output changes in a way that invalidates stored anchors.
    version: str
    #: File kinds this parser claims.
    handles: tuple[FileKind, ...]

    def parse(self, context: ParseContext) -> ParseResult: ...


def normalise_tag_name(name: str) -> str:
    """Canonical form for matching a tag across documents.

    Cross-file comparison has to survive the ways the same signal gets written
    in a spreadsheet versus in logic: ``GUARD_DOOR_CLOSED``, ``Guard-Door-Closed``
    and ``guard door closed`` all normalise together, while a genuinely different
    name such as ``GRD_DR_CLSD`` deliberately does **not** — that difference is a
    real naming mismatch and is exactly what the Interlock Review Agent must
    report rather than paper over.
    """
    collapsed = "".join(char if char.isalnum() else " " for char in name.strip().lower())
    return " ".join(collapsed.split())


def truncate(text: str, limit: int = MAX_EXCERPT_CHARS) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."
