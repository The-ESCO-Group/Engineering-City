"""Parser for Word documents (Office Open XML ``.docx``).

A functional description is prose, and prose is the side of a review that is
easiest to over-read. This parser therefore records *where text is* and nothing
about what it means: headings, paragraphs in document order, list membership and
tables as rows of cells. Whether "below 60 psi" contradicts a ``GRT`` comparison
in an L5X export is a question for an agent holding both documents; here it is
just the text of paragraph 12 in section "3. Interlocks and Permissives".

Three things it is deliberate about.

*Location.* A paragraph index on its own ("paragraph 12") is useless to a
reviewer opening the document, so every anchor's display location names the
section it sits under. Each paragraph also carries the full ``section_path``,
which is what makes a citation checkable. Every number in a display location is a
body-paragraph index and nothing else: a heading reads "heading at paragraph 17",
not "heading 17", because 17 is its position among the body's paragraphs and not
its position among the headings — a document with seven headings really does have
a "heading at paragraph 17". A heading is located under its *parent* section
rather than under itself, so the segment is a place to navigate from instead of a
repeat of the excerpt.

*Silence.* Word files carry a lot that plain-text extraction drops: images,
charts, embedded objects, footnotes, text boxes, headers and footers, tracked
insertions and deletions, content controls. Every one of those is recorded — as
an :class:`~app.parsers.base.UnsupportedItem` where it is a capability boundary,
as a warning where it means text is missing from what we did extract. "We did not
read this" is never indistinguishable from "there was nothing there".

*Guessing.* List level comes from the paragraph's own ``w:numPr/w:ilvl`` or it is
recorded as ``None``. A style called "List Bullet" tells us the paragraph is a
list item; it does not tell us its depth, so no depth is invented. Word's
built-in "List Paragraph" is deliberately **not** read as a list style: Word
applies it to any indented block, including block quotes and plain indented
prose, so on its own it says nothing about list membership. A "List Paragraph"
counts as a list item only when it also carries ``w:numPr``, which is the
paragraph stating it itself.

XML note: python-docx reads the package's OOXML parts with lxml configured with
``resolve_entities=False``, and this module never parses XML itself — it walks the
element tree python-docx has already built. ``app.security.xml_safety`` guards
documents the platform parses directly (L5X, generic XML); a ``.docx`` cannot be
routed through it, because its XML lives inside a ZIP container that only the OPC
reader knows how to open. What replaces it here is a central-directory check on
the package before python-docx opens it — declared uncompressed size, compression
ratio and the size of ``word/document.xml`` — plus hard caps on how much of a body
is walked, so an over-large or amplifying package is refused or truncated loudly
instead of pinning a worker.
"""

from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from docx import Document
from docx.opc.exceptions import OpcError, PackageNotFoundError
from docx.table import Table
from docx.text.paragraph import Paragraph

from app.core.config import Settings
from app.core.enums import AnchorType, FileKind
from app.core.errors import ParserError
from app.parsers.base import AnchorDraft, ParseContext, ParseResult, WarningCode, truncate

__all__ = ["DocxParser"]

# ---------------------------------------------------------------------------
# Package sniffing
# ---------------------------------------------------------------------------

#: A ``.docx`` is a ZIP container; anything else cannot be an OOXML package.
_ZIP_MAGIC = b"PK\x03\x04"
#: OLE2 compound file — a legacy ``.doc``, or an OOXML package encrypted by Word.
_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

# ---------------------------------------------------------------------------
# OOXML namespaces, spelled out so tag matching does not depend on python-docx
# internals.
# ---------------------------------------------------------------------------

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_M = "{http://schemas.openxmlformats.org/officeDocument/2006/math}"
_MC = "{http://schemas.openxmlformats.org/markup-compatibility/2006}"

#: Namespace URI -> the prefix a reader will recognise, so a warning can name a
#: tag the way the OOXML specification does.
_NAMESPACE_PREFIXES: tuple[tuple[str, str], ...] = (
    (_W, "w:"),
    (_A, "a:"),
    (_M, "m:"),
    (_MC, "mc:"),
)

_URI_PICTURE = "http://schemas.openxmlformats.org/drawingml/2006/picture"
_URI_CHART = "http://schemas.openxmlformats.org/drawingml/2006/chart"
_URI_DIAGRAM = "http://schemas.openxmlformats.org/drawingml/2006/diagram"

#: Where run text can live. ``w:delText`` holds the text of a tracked deletion.
_TEXT_TAGS = (_W + "t", _W + "delText")

# ---------------------------------------------------------------------------
# Style-name classification
# ---------------------------------------------------------------------------

#: "Heading 3" and the style-id form "Heading3". Word allows levels 1-9.
_HEADING_STYLE_RE = re.compile(r"^heading\s*([1-9])$", re.IGNORECASE)
#: The document title behaves as the outline root, so it is level 0.
_TITLE_STYLE_RE = re.compile(r"^title$", re.IGNORECASE)
#: "List Bullet", "List Number", "List Continue 2", ... Styles whose *name* is
#: evidence of list membership. "List Paragraph" is excluded on purpose: it is
#: what Word applies to any indented block — a block quote, an indented note —
#: and reading it as a list would invent membership the document never states.
#: Such a paragraph is still a list item when it carries ``w:numPr``, which
#: ``_has_numbering`` picks up.
_LIST_STYLE_RE = re.compile(r"^list\s*(bullet|number|continue)(\s*[1-9])?$", re.IGNORECASE)

#: Tag -> (unsupported kind, detail). One entry per thing knowingly skipped.
_TAG_FEATURES: dict[str, tuple[str, str]] = {
    _W + "object": (
        "embedded_object",
        "Embedded OLE object. Its content is a separate file format and was not read.",
    ),
    _W + "pict": (
        "drawing",
        "Legacy VML drawing. Shapes are not converted to text.",
    ),
    _W + "footnoteReference": (
        "footnote",
        "Footnote reference. Footnote text lives in a separate part and was not extracted.",
    ),
    _W + "endnoteReference": (
        "endnote",
        "Endnote reference. Endnote text lives in a separate part and was not extracted.",
    ),
    _W + "commentReference": (
        "comment",
        "Review comment. Comment text was not extracted.",
    ),
    _W + "txbxContent": (
        "text_box",
        "Text box. Text inside shapes is not part of the document body and was not extracted.",
    ),
    _W + "fldSimple": (
        "field_code",
        "Field. The field code was not evaluated, and the result Word cached for it "
        "is not part of the extracted paragraph text either.",
    ),
    _W + "instrText": (
        "field_code",
        "Field code instruction. Fields are not evaluated.",
    ),
    _M + "oMath": ("equation", "OMML equation. Not converted to text."),
    _M + "oMathPara": ("equation", "OMML equation. Not converted to text."),
}

#: Wrappers whose runs python-docx does not include in ``Paragraph.text`` (nor,
#: therefore, in ``_Cell.text``), so text inside them is missing from what was
#: extracted. Scanned in paragraphs *and* in table cells: a threshold edited under
#: track-changes inside a schedule table is exactly the text a review must not
#: lose silently.
_DROPPED_TEXT_WRAPPERS: tuple[tuple[str, str], ...] = (
    (_W + "ins", "tracked insertion"),
    (_W + "del", "tracked deletion"),
    (_W + "smartTag", "smart tag"),
    (_W + "sdt", "content control"),
    (_W + "fldSimple", "field result"),
)

# ---------------------------------------------------------------------------
# Body-level containers
# ---------------------------------------------------------------------------

#: Direct ``w:body`` children ``iter_inner_content()`` yields, i.e. those actually
#: read into ``paragraphs`` and ``tables``.
_BODY_CONSUMED_TAGS = frozenset({_W + "p", _W + "tbl"})

#: Direct ``w:body`` children that hold no paragraph, table or run text of their
#: own — section properties and range markers. Skipping these is not a loss.
_BODY_INERT_TAGS = frozenset(
    {
        _W + "sectPr",
        _W + "bookmarkStart",
        _W + "bookmarkEnd",
        _W + "commentRangeStart",
        _W + "commentRangeEnd",
        _W + "proofErr",
        _W + "permStart",
        _W + "permEnd",
    }
)

#: Names for the body containers that are known to hold content. Anything not
#: listed here is still reported — by tag name — rather than assumed harmless.
_BODY_CONTAINER_LABELS: dict[str, str] = {
    _W + "sdt": "block-level content control",
    _W + "customXml": "custom XML container",
    _W + "altChunk": "imported content chunk",
    _W + "ins": "block-level tracked insertion",
    _W + "del": "block-level tracked deletion",
    _W + "moveFrom": "block-level tracked move (source)",
    _W + "moveTo": "block-level tracked move (destination)",
    _MC + "AlternateContent": "markup-compatibility alternate content",
}

#: How many locations one aggregated report names before it says "and N more".
_MAX_REPORTED_LOCATIONS = 5
#: Bound on ``summary`` so it stays cheap to show without loading ``content``.
_MAX_SUMMARY_HEADINGS = 24

# ---------------------------------------------------------------------------
# Bounds. A document larger than these is read as far as the cap and the
# shortfall is reported, rather than being read in full at unbounded cost.
# ---------------------------------------------------------------------------

#: Paragraphs plus tables taken from the body before the walk stops.
_MAX_BODY_ITEMS = 20_000
#: Anchors emitted before citations stop being minted.
_MAX_ANCHORS = 5_000

#: Which page header/footer variants are inspected, and what to call each one.
#: ``(attribute, unsupported kind, human label)``.
_HEADER_FOOTER_PARTS: tuple[tuple[str, str, str], ...] = (
    ("header", "header", "header"),
    ("footer", "footer", "footer"),
    ("first_page_header", "header", "first-page header"),
    ("first_page_footer", "footer", "first-page footer"),
    ("even_page_header", "header", "even-page header"),
    ("even_page_footer", "footer", "even-page footer"),
)


@dataclass(slots=True)
class _FeatureTally:
    """Running count of one kind of content that was not interpreted."""

    kind: str
    detail: str
    count: int = 0
    locations: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Opening the file
# ---------------------------------------------------------------------------


def _redact(text: str, path: Path) -> str:
    """Strip the on-disk path out of a library message.

    python-docx puts the full path in its errors; a stored parser error must not
    leak the server's filesystem layout, and an absolute path would make the
    output machine-dependent.
    """
    for variant in (str(path), path.as_posix()):
        text = text.replace(variant, path.name)
    return text


def _guard_package(path: Path) -> None:
    """Reject non-packages before python-docx, so the error names the real cause."""
    try:
        with path.open("rb") as handle:
            head = handle.read(len(_OLE_MAGIC))
    except OSError as exc:
        raise ParserError(
            f"Cannot read the DOCX file: {type(exc).__name__}.",
            details={"reason": "unreadable"},
        ) from exc

    if not head:
        raise ParserError(
            "The file is empty (0 bytes), so it contains no Word package.",
            details={"reason": "empty_file"},
        )
    if head.startswith(_OLE_MAGIC):
        raise ParserError(
            "The file is an OLE2 compound document, not an Office Open XML package. "
            "That is either a legacy Word 97-2003 .doc or a password-protected .docx; "
            "re-save it as an unprotected .docx.",
            details={"reason": "ole2_container"},
        )
    if not head.startswith(_ZIP_MAGIC):
        raise ParserError(
            "The file is not a ZIP container, so it cannot be a .docx package. "
            "Its extension does not match its content.",
            details={"reason": "not_a_zip"},
        )


#: The part every Word package's body lives in.
_MAIN_PART = "word/document.xml"


def _guard_package_size(path: Path, settings: Settings) -> None:
    """Reject a package that would cost more to expand than it costs to send.

    Only the ZIP central directory is read, so a bomb is refused before a single
    byte is inflated. The thresholds are the platform's existing ones: a ``.docx``
    is an archive of XML, and both of those already have limits — this parser had
    simply been outside them, because python-docx opens the container itself.
    """
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
    except (zipfile.BadZipFile, OSError, RuntimeError):
        # Not readable as a ZIP central directory. python-docx will say so with a
        # better-targeted error than anything invented here.
        return

    if len(infos) > settings.max_archive_entries:
        raise ParserError(
            f"The Word package holds {len(infos):,} parts, above the "
            f"{settings.max_archive_entries:,} limit.",
            details={"reason": "too_many_parts", "parts": len(infos)},
        )

    total_uncompressed = sum(info.file_size for info in infos)
    total_compressed = sum(info.compress_size for info in infos)

    if total_uncompressed > settings.max_archive_uncompressed_bytes:
        raise ParserError(
            f"The Word package expands to {total_uncompressed:,} bytes, above the "
            f"{settings.max_archive_uncompressed_bytes:,} limit.",
            details={"reason": "package_too_large", "uncompressed_bytes": total_uncompressed},
        )

    if total_compressed > 0:
        ratio = total_uncompressed / total_compressed
        if ratio > settings.max_archive_compression_ratio:
            raise ParserError(
                f"The Word package expands {ratio:.0f}:1, above the "
                f"{settings.max_archive_compression_ratio:.0f}:1 limit. That is "
                "characteristic of a decompression bomb rather than a document.",
                details={"reason": "compression_ratio", "ratio": round(ratio, 1)},
            )

    for info in infos:
        if info.filename == _MAIN_PART and info.file_size > settings.max_xml_bytes:
            raise ParserError(
                f"The document body part is {info.file_size:,} bytes, above the "
                f"{settings.max_xml_bytes:,} XML limit.",
                details={"reason": "body_part_too_large", "uncompressed_bytes": info.file_size},
            )


def _open_document(path: Path, settings: Settings) -> Any:
    _guard_package(path)
    _guard_package_size(path, settings)
    try:
        return Document(str(path))
    except PackageNotFoundError as exc:
        raise ParserError(
            "The ZIP container is not a readable Office Open XML package.",
            details={"reason": "not_an_opc_package", "error": _redact(str(exc), path)},
        ) from exc
    except (OpcError, ValueError, KeyError) as exc:
        # A valid OPC package of the wrong type — an .xlsx renamed .docx — fails
        # python-docx's content-type check with a ValueError.
        raise ParserError(
            f"The package could not be opened as a Word document ({type(exc).__name__}).",
            details={"reason": "not_a_word_package", "error": _redact(str(exc), path)},
        ) from exc
    except Exception as exc:
        raise ParserError(
            f"Unexpected failure opening the Word document ({type(exc).__name__}).",
            details={"reason": "open_failed", "error": _redact(str(exc), path)},
        ) from exc


# ---------------------------------------------------------------------------
# Classification helpers
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _StyleFacts:
    """What a paragraph's style contributes, resolved once per distinct style."""

    name: str = ""
    style_id: str = ""
    #: Heading level reached through ``w:basedOn``, if the style itself is not one.
    inherited_level: int | None = None
    #: Why the style could not be resolved at all, or ``""`` when it was. A
    #: paragraph whose style is unknown is recorded with no style name, and that
    #: has to be visible rather than look like a paragraph in the default style.
    failure: str = ""


def _paragraph_style_id(paragraph: Any) -> str:
    """The raw ``w:pStyle`` value, or ``""`` for the default paragraph style.

    Used as a cache key: resolving a style walks the styles part, and doing that
    once per paragraph is the difference between a long document parsing in
    seconds and in minutes.
    """
    values = paragraph._p.xpath("./w:pPr/w:pStyle/@w:val")
    return str(values[0]) if values else ""


def _style_facts(paragraph: Any) -> _StyleFacts:
    """Resolve the paragraph's style. Callers cache this by ``_paragraph_style_id``.

    A failure is carried back on the result rather than swallowed: the caller
    reports it once per distinct style, so "this paragraph has no style name"
    never has to be read as "this paragraph is in the default style".
    """
    try:
        style = paragraph.style
    except (KeyError, ValueError) as exc:
        return _StyleFacts(failure=f"the styles part raised {type(exc).__name__}")
    if style is None:
        return _StyleFacts(failure="python-docx resolved it to no style object")
    return _StyleFacts(
        name=str(getattr(style, "name", "") or ""),
        style_id=str(getattr(style, "style_id", "") or ""),
        inherited_level=_inherited_heading_level(style),
    )


def _tag_name(tag: str) -> str:
    """``{ns}p`` -> ``w:p``, so a message names a tag the way the schema does."""
    for uri, prefix in _NAMESPACE_PREFIXES:
        if tag.startswith(uri):
            return prefix + tag[len(uri) :]
    return tag


def _style_heading_level(style_name: str, style_id: str) -> int | None:
    """Outline level from a style's own name or id, or ``None``."""
    for candidate in (style_name.strip(), style_id.strip()):
        if not candidate:
            continue
        if _TITLE_STYLE_RE.match(candidate):
            return 0
        match = _HEADING_STYLE_RE.match(candidate)
        if match:
            return int(match.group(1))
    return None


def _explicit_outline_level(paragraph: Any) -> int | None:
    """Level from the paragraph's own ``w:pPr/w:outlineLvl``, or ``None``.

    ``w:outlineLvl`` is the element that *defines* outline level in OOXML — it is
    what Word's navigation pane reads — so a heading written in a house style
    ("ESCO Heading 1") is still a heading. Values run 0-8 for levels 1-9; 9 means
    body text and is not a heading claim.
    """
    values = paragraph._p.xpath("./w:pPr/w:outlineLvl/@w:val")
    if not values:
        return None
    try:
        value = int(str(values[0]))
    except ValueError:
        return None
    return value + 1 if 0 <= value <= 8 else None


def _inherited_heading_level(style: Any) -> int | None:
    """Level from the ``w:basedOn`` chain of a style, or ``None``.

    A style derived from "Heading 2" is a Heading 2 wearing a local name. The walk
    is depth-bounded so a cyclic ``basedOn`` chain cannot hang the parse.
    """
    seen: set[int] = set()
    for _ in range(10):
        if style is None or id(style) in seen:
            return None
        seen.add(id(style))
        try:
            style = style.base_style
        except (KeyError, ValueError, AttributeError):
            return None
        if style is None:
            return None
        name = str(getattr(style, "name", "") or "")
        style_id = str(getattr(style, "style_id", "") or "")
        level = _style_heading_level(name, style_id)
        if level is not None:
            return level
    return None


def _heading_level(paragraph: Any, facts: _StyleFacts) -> int | None:
    """Outline level for a paragraph, or ``None`` for body text.

    Three sources, most specific first: the style's own name or id, the
    paragraph's explicit ``w:outlineLvl``, then the style's ``basedOn`` ancestry.
    The style name leads because it is what a reviewer sees in Word's style box;
    the other two exist so a heading under a corporate style name is not demoted
    to body text — which would also give every paragraph after it the wrong
    section path.
    """
    level = _style_heading_level(facts.name, facts.style_id)
    if level is not None:
        return level
    level = _explicit_outline_level(paragraph)
    if level is not None:
        return level
    return facts.inherited_level


def _numbering_level(paragraph: Any) -> tuple[int | None, str]:
    """``(level, unreadable_value)`` from the paragraph's own ``w:numPr/w:ilvl``.

    A style-defined level is not chased: ``None`` is honest, whereas 0 would
    invent a depth the paragraph never states.

    The second element is the raw ``w:val`` when the paragraph *does* state a
    level and it is not a number. That case is returned rather than folded into
    ``None`` so the caller can say a stated level was discarded — otherwise it is
    indistinguishable from a paragraph that stated no level at all.
    """
    values = paragraph._p.xpath("./w:pPr/w:numPr/w:ilvl/@w:val")
    if not values:
        return None, ""
    raw = str(values[0])
    try:
        return int(raw), ""
    except ValueError:
        return None, raw


def _has_numbering(paragraph: Any) -> bool:
    return bool(paragraph._p.xpath("./w:pPr/w:numPr"))


def _is_list_style(style_name: str, style_id: str) -> bool:
    return any(_LIST_STYLE_RE.match(name.strip()) for name in (style_name, style_id) if name)


def _classify_drawing(node: Any) -> tuple[str, str]:
    """Work out what a ``w:drawing`` holds from its ``graphicData`` URI."""
    for child in node.iter(_A + "graphicData"):
        uri = str(child.get("uri") or "")
        if uri == _URI_CHART:
            return ("chart", "Embedded chart. Its underlying data was not read.")
        if uri == _URI_PICTURE:
            return ("image", "Inline or floating image. No text was extracted from it.")
        if uri == _URI_DIAGRAM:
            return ("diagram", "SmartArt diagram. Its text was not extracted.")
        return ("drawing", f"DrawingML content of type '{uri}'. Not interpreted.")
    return ("drawing", "DrawingML content with no graphic data. Not interpreted.")


def _scan_features(element: Any) -> list[tuple[str, str]]:
    """``(kind, detail)`` for everything in this element that was not interpreted.

    Returned in document order, which is what keeps the aggregated report stable.
    """
    found: list[tuple[str, str]] = []
    for node in element.iter():
        tag = node.tag
        if not isinstance(tag, str):
            continue  # XML comment or processing instruction
        if tag == _W + "drawing":
            found.append(_classify_drawing(node))
            continue
        feature = _TAG_FEATURES.get(tag)
        if feature is not None:
            found.append(feature)
    return found


def _dropped_text_wrappers(element: Any) -> list[str]:
    """Wrappers under this element whose text is absent from the extracted text.

    Takes a bare lxml element so a paragraph (``w:p``) and a table cell (``w:tc``)
    are scanned by the same rule: python-docx builds a cell's text out of its
    paragraphs' runs, so every wrapper that hides text in a paragraph hides it in
    a cell too.
    """
    names: list[str] = []
    for tag, label in _DROPPED_TEXT_WRAPPERS:
        for node in element.iter(tag):
            if any(next(node.iter(text_tag), None) is not None for text_tag in _TEXT_TAGS):
                names.append(label)
                break
    return names


def _section_display(section_path: list[str]) -> str:
    """The nearest named ancestor heading, for a human-readable location."""
    for title in reversed(section_path):
        if title.strip():
            return title.strip()
    return ""


def _location_summary(tally: _FeatureTally) -> str:
    shown = tally.locations[:_MAX_REPORTED_LOCATIONS]
    remainder = tally.count - len(shown)
    text = "; ".join(shown)
    if remainder > 0:
        return f"{text}; and {remainder} more" if text else f"{remainder} locations"
    return text


def _record_keys(header: list[str], columns: int) -> tuple[list[str], bool, list[int]]:
    """Unique record keys wide enough for the widest row, and what was invented.

    Two different gaps, reported separately by the caller: a blank or repeated
    *label* inside the header row, and a column the header row does not reach at
    all. Both get a positional key — the alternatives are one record quietly
    overwriting another, and a column of data missing from every record.
    """
    keys: list[str] = []
    synthesised = False
    unheaded: list[int] = []
    for index in range(max(columns, len(header))):
        candidate = header[index].strip() if index < len(header) else ""
        if not candidate:
            if index < len(header):
                synthesised = True
            else:
                unheaded.append(index)
            candidate = f"column {index}"
        if candidate in keys:
            candidate = f"{candidate} ({index})"
            synthesised = True
        keys.append(candidate)
    return keys, synthesised, unheaded


def _iso(value: Any) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else None


def _text_property(properties: Any, name: str) -> str:
    value = getattr(properties, name, "")
    return str(value) if value else ""


# ---------------------------------------------------------------------------
# The parser
# ---------------------------------------------------------------------------


class DocxParser:
    """Normalises a ``.docx`` into headings, paragraphs and tables."""

    name: str = "docx"
    #: 1.2.0 — a heading's display location names its body-paragraph index
    #: ("heading at paragraph 17") and the section above it rather than itself,
    #: and paragraphs record the ``w:pStyle`` id their XML names alongside the
    #: style that was resolved for it.
    version: str = "1.2.0"
    handles: tuple[FileKind, ...] = (FileKind.DOCX,)

    def parse(self, context: ParseContext) -> ParseResult:
        document = _open_document(context.path, context.settings)
        result = ParseResult(
            artifact_kind=FileKind.DOCX.value,
            parser_name=self.name,
            parser_version=self.version,
        )
        _DocumentBuilder(document, context, result).run()
        return result


class _DocumentBuilder:
    """One parse. Holds the walk state so :class:`DocxParser` stays stateless."""

    def __init__(self, document: Any, context: ParseContext, result: ParseResult) -> None:
        self._document = document
        self._result = result
        self._label = context.source_label
        #: Open heading stack: ``(level, text)``, innermost last.
        self._stack: list[tuple[int, str]] = []
        self._paragraphs: list[dict[str, Any]] = []
        self._headings: list[dict[str, Any]] = []
        self._tables: list[dict[str, Any]] = []
        self._features: dict[str, _FeatureTally] = {}
        self._paragraph_index = 0
        self._table_index = 0
        self._list_item_count = 0
        self._empty_paragraph_count = 0
        self._character_count = 0
        self._table_cell_count = 0
        self._body_items_skipped = 0
        self._anchors_suppressed = 0
        #: ``w:pStyle`` value -> resolved style facts.
        self._style_cache: dict[str, _StyleFacts] = {}

    # -- orchestration ----------------------------------------------------

    def run(self) -> None:
        self._collect_body()
        self._collect_headers_and_footers()
        self._check_unread_body_children()
        self._emit_features()
        self._report_truncation()
        self._finalise()

    def _collect_body(self) -> None:
        """Walk the body's own children, in document order.

        ``Document.iter_inner_content()`` selects exactly the same elements — the
        direct ``w:p`` and ``w:tbl`` children — but it does so with an XPath union
        over the body, whose cost grows with the square of the number of children:
        enumerating a 60,000-paragraph body that way takes three minutes against
        six seconds for this walk. Walking the children directly is what makes the
        cap below a real bound rather than a bound on the *reading* of a body the
        parser has already paid to enumerate.
        """
        for child in self._document.element.body:
            tag = child.tag
            if not isinstance(tag, str) or tag not in _BODY_CONSUMED_TAGS:
                continue  # reported by ``_check_unread_body_children``
            if self._paragraph_index + self._table_index >= _MAX_BODY_ITEMS:
                # Counted, not read: the shortfall is reported by
                # ``_report_truncation`` rather than left to look like the end of
                # the document.
                self._body_items_skipped += 1
                continue
            if tag == _W + "tbl":
                self._add_table(Table(child, self._document))
            else:
                self._add_paragraph(Paragraph(child, self._document))

    # -- citations --------------------------------------------------------

    def _add_anchor(self, anchor: AnchorDraft) -> None:
        """Emit a citation, or count it as suppressed once the cap is reached."""
        if len(self._result.anchors) >= _MAX_ANCHORS:
            self._anchors_suppressed += 1
            return
        self._result.add_anchor(anchor)

    def _report_truncation(self) -> None:
        if self._body_items_skipped:
            self._result.warn(
                WarningCode.TRUNCATED,
                f"The document body holds more than {_MAX_BODY_ITEMS:,} paragraphs and "
                f"tables; {self._body_items_skipped:,} further item(s) were not read and "
                "are absent from this record.",
                location=self._label,
            )
        if self._anchors_suppressed:
            self._result.warn(
                WarningCode.TRUNCATED,
                f"Citations were capped at {_MAX_ANCHORS:,}; {self._anchors_suppressed:,} "
                "further paragraph(s) or cell(s) were recorded in the content but "
                "cannot be cited by an anchor.",
                location=self._label,
            )

    # -- paragraphs -------------------------------------------------------

    def _paragraph_style(self, paragraph: Any, index: int) -> tuple[str, _StyleFacts]:
        """``(requested style id, style facts)``, resolved once per distinct style.

        Every paragraph in a document usually carries one of a handful of styles,
        and resolving one costs a scan of the styles part; caching turns a
        per-paragraph cost into a per-style one. Reporting is done on the cache
        miss, so a style that resolves oddly is disclosed once rather than once
        per paragraph carrying it.

        The requested id — the paragraph's own ``w:pStyle/@w:val`` — is returned
        alongside the resolution because the two can differ; see
        :meth:`_report_style_resolution`.
        """
        key = _paragraph_style_id(paragraph)
        facts = self._style_cache.get(key)
        if facts is None:
            facts = _style_facts(paragraph)
            self._style_cache[key] = facts
            self._report_style_resolution(key, facts, index)
        return key, facts

    def _report_style_resolution(self, requested: str, facts: _StyleFacts, index: int) -> None:
        """Disclose a style name that is not the one the paragraph's XML asks for.

        python-docx's ``get_by_id`` falls back to the default paragraph style when
        a style id is not defined in the styles part, so ``w:pStyle w:val="X"``
        with no ``X`` defined is reported as "Normal" — a positive statement about
        the document that the document does not make. Recording the substitution
        keeps ``style`` usable without letting it be read as verbatim.
        """
        location = f"{self._label} / paragraph {index}"
        if facts.failure:
            self._result.warn(
                WarningCode.STRUCTURE_UNRECOGNISED,
                f"The paragraph style {requested or '(the document default)'!r} could "
                f"not be resolved: {facts.failure}. Paragraphs carrying it are recorded "
                "with no style name, and no heading level was read from it — so a "
                "heading in that style is recorded as body text.",
                location=location,
            )
            return
        if requested and facts.style_id != requested:
            self._result.warn(
                WarningCode.STRUCTURE_UNRECOGNISED,
                f"Paragraph style id {requested!r} is not defined as a paragraph style "
                f"in this document, so the default style {facts.name!r} was substituted "
                "for it. Paragraphs carrying it record the substitute as their style, "
                "not the id their XML names, and any heading level was read from the "
                "substitute.",
                location=location,
            )

    def _add_paragraph(self, paragraph: Any) -> None:
        index = self._paragraph_index
        self._paragraph_index += 1

        text = str(paragraph.text or "").strip()
        requested_style_id, facts = self._paragraph_style(paragraph, index)
        style_name, style_id = facts.name, facts.style_id
        level = _heading_level(paragraph, facts)
        list_level, unreadable_level = _numbering_level(paragraph)
        if unreadable_level:
            self._result.warn(
                WarningCode.STRUCTURE_UNRECOGNISED,
                f"The paragraph states a numbering level of {unreadable_level!r}, which "
                "is not a number. No list level was recorded for it, so it reads as a "
                "list item whose depth the paragraph never stated.",
                location=f"{self._label} / paragraph {index}",
            )
        # A numbered heading is a heading first; calling it a list item would
        # misdescribe the structure a reviewer navigates by.
        is_list_item = level is None and (
            _is_list_style(style_name, style_id) or _has_numbering(paragraph)
        )

        ancestors: list[str] = []
        if level is None:
            section_path = [title for _, title in self._stack]
        else:
            while self._stack and self._stack[-1][0] >= level:
                self._stack.pop()
            ancestors = [title for _, title in self._stack]
            self._stack.append((level, text))
            section_path = [*ancestors, text]
            self._headings.append({"level": level, "text": text, "index": index, "path": ancestors})

        if is_list_item:
            self._list_item_count += 1
        if not text:
            self._empty_paragraph_count += 1
        self._character_count += len(text)

        self._paragraphs.append(
            {
                "index": index,
                "text": text,
                "style": style_name,
                # The id the paragraph's own XML names, which is not always the
                # style that was resolved: see ``_report_style_resolution``.
                "style_id": requested_style_id,
                "heading_level": level,
                "list_level": list_level,
                "is_list_item": is_list_item,
                "section_path": section_path,
            }
        )

        # A heading is located under the section *containing* it, not under
        # itself: repeating its own text would name no place to navigate from.
        location = self._paragraph_location(
            index,
            ancestors if level is not None else section_path,
            is_heading=level is not None,
        )
        self._note_features(paragraph._p, location)
        self._note_dropped_text(paragraph._p, location)

        if not text:
            # Nothing to quote, so nothing a finding could cite. The paragraph is
            # still recorded above, keeping the indices anchors rely on intact.
            return
        if level is not None:
            self._add_anchor(
                AnchorDraft(
                    key=f"heading:{index}",
                    anchor_type=AnchorType.DOCX_HEADING,
                    display_location=location,
                    structured_location={
                        "paragraph_index": index,
                        "level": level,
                        "style": style_name,
                        "style_id": requested_style_id,
                        "path": ancestors,
                    },
                    excerpt=truncate(text),
                    source_label=self._label,
                )
            )
            return
        self._add_anchor(
            AnchorDraft(
                key=f"paragraph:{index}",
                anchor_type=AnchorType.DOCX_PARAGRAPH,
                display_location=location,
                structured_location={
                    "paragraph_index": index,
                    "style": style_name,
                    "style_id": requested_style_id,
                    "section_path": section_path,
                    "is_list_item": is_list_item,
                    "list_level": list_level,
                },
                excerpt=truncate(text),
                source_label=self._label,
            )
        )

    def _paragraph_location(self, index: int, section_path: list[str], *, is_heading: bool) -> str:
        """Where to open the document, in one grammar.

        ``index`` is a body-paragraph index in both cases, so a heading says so
        rather than presenting the number as a count of headings — the seventh of
        seven headings in the demo fixture sits at paragraph 17, and "heading 17"
        would send a reviewer counting to something that is not there.
        """
        unit = f"heading at paragraph {index}" if is_heading else f"paragraph {index}"
        section = _section_display(section_path)
        return f"{self._label} / {section} / {unit}" if section else f"{self._label} / {unit}"

    # -- tables -----------------------------------------------------------

    def _add_table(self, table: Any) -> None:
        index = self._table_index
        self._table_index += 1
        section_path = [title for _, title in self._stack]
        section = _section_display(section_path)

        try:
            # Materialised in one go: holding the cell objects keeps their lxml
            # proxies alive, which is what makes identity-based merge detection
            # below valid.
            grid = [list(row.cells) for row in table.rows]
        except Exception as exc:
            self._result.warn(
                WarningCode.STRUCTURE_UNRECOGNISED,
                "Table layout could not be read, so its cells were not extracted "
                f"({type(exc).__name__}).",
                location=f"{self._label} / table {index}",
            )
            self._tables.append(
                {
                    "index": index,
                    "rows": 0,
                    "columns": 0,
                    "header": [],
                    "records": [],
                    "cells": [],
                    "section_path": section_path,
                }
            )
            return

        columns = max((len(row) for row in grid), default=0)
        short_rows = [number for number, row in enumerate(grid) if len(row) != columns]
        if short_rows:
            listed = ", ".join(str(number) for number in short_rows[:_MAX_REPORTED_LOCATIONS])
            if len(short_rows) > _MAX_REPORTED_LOCATIONS:
                listed = f"{listed} and {len(short_rows) - _MAX_REPORTED_LOCATIONS} more"
            self._result.warn(
                WarningCode.RAGGED_ROWS,
                f"Table rows have differing cell counts: the widest has {columns} and "
                f"row(s) {listed} have fewer. Short rows were left short rather than "
                "padded, so a record simply has no entry for the columns its row omits.",
                location=f"{self._label} / table {index}",
            )

        cells: list[dict[str, Any]] = []
        by_position: dict[tuple[int, int], str] = {}
        first_positions: list[tuple[int, int]] = []
        seen: dict[int, tuple[int, int]] = {}
        repeated = 0
        for row_number, row in enumerate(grid):
            for column_number, cell in enumerate(row):
                cell_text = str(cell.text or "").strip()
                cells.append({"row": row_number, "column": column_number, "text": cell_text})
                by_position[row_number, column_number] = cell_text
                self._table_cell_count += 1
                self._character_count += len(cell_text)

                tc_id = id(cell._tc)
                if tc_id in seen:
                    repeated += 1
                    continue
                seen[tc_id] = (row_number, column_number)
                first_positions.append((row_number, column_number))
                location = self._cell_location(index, section, row_number, column_number, "")
                self._note_features(cell._tc, location)
                # A cell's text is built from its paragraphs' runs, so the same
                # wrappers that hide text in a body paragraph hide it here — and
                # the mutilated text would otherwise be published as a verbatim
                # cell excerpt.
                self._note_dropped_text(cell._tc, location)
                if cell.tables:
                    self._tally(
                        "nested_table",
                        "Table nested inside a table cell. Nested tables are not "
                        "flattened and their cells were not extracted.",
                        location,
                    )

        if repeated:
            self._result.warn(
                WarningCode.MERGED_CELLS,
                f"{repeated} grid position(s) repeat a merged cell. The text is listed "
                "at every position the cell spans, but only the first carries an anchor.",
                location=f"{self._label} / table {index}",
            )

        header = [by_position[0, column] for column in range(len(grid[0]))] if grid else []
        keys, synthesised, unheaded = _record_keys(header, columns)
        if synthesised:
            self._result.warn(
                WarningCode.STRUCTURE_UNRECOGNISED,
                "The header row has blank or repeated labels; positional keys were "
                "substituted so that no record silently overwrites another.",
                location=f"{self._label} / table {index} header row",
            )
        if unheaded:
            listed = ", ".join(str(column) for column in unheaded)
            self._result.warn(
                WarningCode.STRUCTURE_UNRECOGNISED,
                f"The header row has {len(header)} cell(s) but the widest row has "
                f"{columns}; column(s) {listed} are named by no header label. Their "
                "values are recorded under positional keys rather than being left out "
                "of the records.",
                location=f"{self._label} / table {index} header row",
            )

        records = [
            {
                "row": row_number,
                "values": {
                    keys[column]: by_position[row_number, column]
                    for column in range(len(grid[row_number]))
                },
            }
            for row_number in range(1, len(grid))
        ]

        self._tables.append(
            {
                "index": index,
                "rows": len(grid),
                "columns": columns,
                "header": header,
                "records": records,
                "cells": cells,
                "section_path": section_path,
            }
        )

        for row_number, column_number in first_positions:
            cell_text = by_position[row_number, column_number]
            if not cell_text:
                continue
            column_header = keys[column_number] if column_number < len(keys) else ""
            self._add_anchor(
                AnchorDraft(
                    key=f"tablecell:{index}:{row_number}:{column_number}",
                    anchor_type=AnchorType.DOCX_TABLE_CELL,
                    display_location=self._cell_location(
                        index, section, row_number, column_number, column_header
                    ),
                    structured_location={
                        "table_index": index,
                        "row": row_number,
                        "column": column_number,
                        "header": column_header,
                        "section_path": section_path,
                    },
                    excerpt=truncate(cell_text),
                    source_label=self._label,
                )
            )

    def _cell_location(
        self, table_index: int, section: str, row: int, column: int, header: str
    ) -> str:
        unit = f"table {table_index} row {row} column {column}"
        if header:
            unit = f"{unit} ({header})"
        return f"{self._label} / {section} / {unit}" if section else f"{self._label} / {unit}"

    # -- content that was not read ---------------------------------------

    def _tally(self, kind: str, detail: str, location: str) -> None:
        tally = self._features.get(kind)
        if tally is None:
            tally = _FeatureTally(kind=kind, detail=detail)
            self._features[kind] = tally
        tally.count += 1
        if len(tally.locations) < _MAX_REPORTED_LOCATIONS:
            tally.locations.append(location)

    def _note_features(self, element: Any, location: str) -> None:
        for kind, detail in _scan_features(element):
            self._tally(kind, detail, location)

    def _note_dropped_text(self, element: Any, location: str) -> None:
        for label in _dropped_text_wrappers(element):
            self._result.warn(
                WarningCode.HIDDEN_CONTENT,
                f"Text inside a {label} is not part of the text extracted here, so "
                "what was recorded at this location is incomplete.",
                location=location,
            )

    def _collect_headers_and_footers(self) -> None:
        """Report every header/footer variant, not only the default one.

        A revision block ("DOC NO ... REV 4 - UNCONTROLLED COPY") usually lives in
        the *first-page* header, so inspecting only ``section.header`` would drop
        exactly the document-control text a reviewer looks for.
        """
        try:
            sections = list(self._document.sections)
        except Exception as exc:
            # Returning quietly would delete the disclosure itself: the document
            # would look like one with no page furniture at all.
            self._result.warn(
                WarningCode.STRUCTURE_UNRECOGNISED,
                "The document's sections could not be listed "
                f"({type(exc).__name__}), so no page header or footer was inspected. "
                "Document-control text such as a revision or 'UNCONTROLLED COPY' "
                "block lives there and is neither recorded nor reported as skipped.",
                location=self._label,
            )
            return
        for number, section in enumerate(sections, start=1):
            for attribute, kind, label in _HEADER_FOOTER_PARTS:
                part = getattr(section, attribute, None)
                if part is None or getattr(part, "is_linked_to_previous", False):
                    continue
                text = " ".join(
                    paragraph.text.strip()
                    for paragraph in getattr(part, "paragraphs", [])
                    if paragraph.text and paragraph.text.strip()
                )
                if not text:
                    continue
                self._tally(
                    kind,
                    f"Page {kind} text is repeated furniture rather than body "
                    "content, and was not included in the paragraphs.",
                    f"{self._label} / section {number} {label}: {truncate(text, 80)}",
                )

    def _check_unread_body_children(self) -> None:
        """Report every direct body child the body walk did not consume.

        ``iter_inner_content()`` yields ``w:p`` and ``w:tbl`` and nothing else, so
        any other container — a content control, a ``w:customXml`` wrapper, an
        ``w:altChunk`` from "Insert > Object > Text from File", a block-level
        tracked change — takes its paragraphs and tables out of the body flow with
        it. Diffing the children against what was consumed is what stops a
        container this parser has never heard of from being silent by default.
        """
        try:
            children = list(self._document.element.body)
        except Exception as exc:
            # This check is the only thing standing between an unknown container
            # and a result that looks like a complete parse, so its own failure
            # has to be louder than silence.
            self._result.warn(
                WarningCode.STRUCTURE_UNRECOGNISED,
                "The document body's children could not be enumerated "
                f"({type(exc).__name__}), so it is not known whether any container "
                "holding paragraphs or tables was left unread. Treat the paragraphs "
                "and tables recorded here as possibly incomplete.",
                location=self._label,
            )
            return
        #: Tag -> count, in order of first appearance, so the report is stable.
        unread: dict[str, int] = {}
        for child in children:
            tag = child.tag
            if not isinstance(tag, str):
                continue  # XML comment or processing instruction
            if tag in _BODY_CONSUMED_TAGS or tag in _BODY_INERT_TAGS:
                continue
            unread[tag] = unread.get(tag, 0) + 1
        for tag, count in unread.items():
            name = _tag_name(tag)
            label = _BODY_CONTAINER_LABELS.get(tag)
            described = f" ({label})" if label else ""
            self._result.warn(
                WarningCode.HIDDEN_CONTENT,
                f"{count} <{name}> element(s){described} sit directly in the document "
                "body and are not among the paragraphs and tables read from it. Any "
                "text, paragraphs or tables inside them are absent from this record.",
                location=self._label,
            )

    def _emit_features(self) -> None:
        for tally in self._features.values():
            self._result.mark_unsupported(
                kind=tally.kind,
                detail=f"{tally.detail} Occurrences: {tally.count}.",
                location=_location_summary(tally),
            )

    # -- assembly ---------------------------------------------------------

    def _outline(self) -> list[dict[str, Any]]:
        roots: list[dict[str, Any]] = []
        open_nodes: list[tuple[int, dict[str, Any]]] = []
        for heading in self._headings:
            node: dict[str, Any] = {
                "level": heading["level"],
                "text": heading["text"],
                "index": heading["index"],
                "children": [],
            }
            while open_nodes and open_nodes[-1][0] >= heading["level"]:
                open_nodes.pop()
            if open_nodes:
                open_nodes[-1][1]["children"].append(node)
            else:
                roots.append(node)
            open_nodes.append((heading["level"], node))
        return roots

    def _properties(self) -> dict[str, Any]:
        try:
            core = self._document.core_properties
        except Exception as exc:
            self._result.warn(
                WarningCode.STRUCTURE_UNRECOGNISED,
                f"Document properties could not be read ({type(exc).__name__}).",
                location=self._label,
            )
            core = None
        if core is None:
            return {
                "title": "",
                "author": "",
                "created": None,
                "modified": None,
                "comments": "",
                "category": "",
            }
        return {
            "title": _text_property(core, "title"),
            "author": _text_property(core, "author"),
            "created": _iso(getattr(core, "created", None)),
            "modified": _iso(getattr(core, "modified", None)),
            "comments": _text_property(core, "comments"),
            "category": _text_property(core, "category"),
        }

    def _finalise(self) -> None:
        result = self._result
        result.content = {
            "properties": self._properties(),
            "headings": self._headings,
            "paragraphs": self._paragraphs,
            "tables": self._tables,
            "outline": self._outline(),
        }

        if not self._paragraphs and not self._tables:
            result.warn(
                WarningCode.EMPTY_DOCUMENT,
                "The document body contains no paragraphs and no tables. Nothing in "
                "it can be cited, so it supports no finding either way.",
                location=self._label,
            )
        elif not self._headings:
            # Raised only for a document that *has* content: telling the reader
            # that an empty document also lacks headings adds noise, not
            # information, and EMPTY_DOCUMENT already says everything.
            result.warn(
                WarningCode.STRUCTURE_UNRECOGNISED,
                "No heading styles were found, so paragraphs can only be cited by "
                "index and no section structure is available.",
                location=self._label,
            )

        result.summary = {
            "paragraph_count": len(self._paragraphs),
            "empty_paragraph_count": self._empty_paragraph_count,
            "heading_count": len(self._headings),
            "list_item_count": self._list_item_count,
            "table_count": len(self._tables),
            "table_cell_count": self._table_cell_count,
            "character_count": self._character_count,
            "anchor_count": len(result.anchors),
            "warning_count": len(result.warnings),
            "unsupported_count": len(result.unsupported),
            "has_headings": bool(self._headings),
            "title": result.content["properties"]["title"],
            "top_level_headings": [
                node["text"] for node in result.content["outline"][:_MAX_SUMMARY_HEADINGS]
            ],
        }
