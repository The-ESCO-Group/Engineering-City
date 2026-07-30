"""PDF text extraction.

A PDF describes marks on a page, not a document: the text layer is an optional
convenience the producer may simply never have written. A scanned drawing is
therefore *valid, readable PDF that contains no text at all*.

That makes one failure mode more dangerous here than anywhere else in the
platform: returning an empty page as though it were an empty page. "The parser
found no text" and "there is nothing of concern on the drawing" are
indistinguishable downstream unless the parser says which one happened. A
review that reported a scanned interlock drawing as examined and clean would be
a lie the product told with a straight face.

So every page that yields no usable text produces three things — a
``no_text_layer`` (or ``low_text_yield``) warning, an
:class:`~app.parsers.base.UnsupportedItem`, and a page anchor whose evidence
kind is ``unsupported_content`` and whose ``expectation`` says the page needs
OCR or manual review. The gap is citable rather than invisible.

The same rule applies to the three other ways a PDF hides text from a
naive read: pages the page tree declares but the library could not resolve,
text that lives in annotations (form field values, review markup) rather than
in the page content stream, and a text layer that decodes to unmapped glyph
codes. Each is reported rather than dropped.

No OCR is attempted and no OCR dependency exists. This parser reports what the
text layer contains, where the text sits on which page, and nothing else: it
does not classify pages, judge content or infer what an unreadable page holds.
In particular it states what it observed about a page that came back empty and
names the possible causes, rather than asserting one of them.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any, Final

from pypdf import PdfReader
from pypdf.errors import DependencyError, PyPdfError

from app.core.enums import AnchorType, EvidenceKind, FileKind
from app.core.errors import ParserError
from app.models.evidence import MAX_EXCERPT_CHARS
from app.parsers.base import AnchorDraft, ParseContext, ParseResult, WarningCode, truncate

#: ``(content key, PDF /Info key)`` for the properties recorded verbatim. Dates
#: are deliberately excluded: they are metadata about the export, not about the
#: engineering content, and nothing downstream may treat them as either.
_PROPERTIES: Final[tuple[tuple[str, str], ...]] = (
    ("title", "/Title"),
    ("author", "/Author"),
    ("subject", "/Subject"),
    ("creator", "/Creator"),
    ("producer", "/Producer"),
)

#: A document property is a label, not content, so it is bounded: a hostile
#: ``/Info`` dictionary must not be able to push a megabyte into the artifact.
MAX_PROPERTY_CHARS: Final = 300

#: Characters of a page quoted on its page anchor. Short enough that a reviewer
#: reads it at a glance; the whole page text lives in ``content``.
PAGE_EXCERPT_CHARS: Final = 400

#: Below this many non-whitespace characters, a page is reported as a low text
#: yield: enough came back to prove a text layer exists, too little to be
#: trusted as the content of the page.
MIN_PAGE_CHARACTERS: Final = 20

#: Cap on line-level citations for one document. A 400-page report would
#: otherwise mint tens of thousands of anchors, none of which a human will ever
#: follow. Page text in ``content`` is never truncated by this — only the set of
#: pre-built character-range citations is.
MAX_TEXT_RANGE_ANCHORS: Final = 1_000

#: How many page groups a warning names before it summarises the rest.
MAX_LISTED_PAGE_GROUPS: Final = 12

#: Characters kept from one annotation. An annotation is a note or a field
#: value, not a document: a hostile file must not be able to push a megabyte of
#: ``/Contents`` into the artifact.
MAX_ANNOTATION_CHARS: Final = 500

#: Annotations whose text is recorded for one page, and how many entries of a
#: page's ``/Annots`` array are examined at all. Both are bounded so a page
#: carrying a hundred thousand annotations degrades loudly instead of quietly
#: dominating the artifact.
MAX_ANNOTATIONS_PER_PAGE: Final = 50
MAX_ANNOTATIONS_SCANNED: Final = 1_000

#: How far a widget's ``/Parent`` chain is followed to find the field value it
#: displays; a form field may be split across a parent field and its widgets.
MAX_FIELD_PARENTS: Final = 8

#: Above this share of a page's non-whitespace characters decoding to unmapped
#: glyph codes, the page text is reported as not legible rather than as content.
#: A subset font with no ``/ToUnicode`` map yields ``/uniXXXX`` or ``(cid:N)``
#: tokens that are the right *quantity* of text and none of its meaning.
MAX_ILLEGIBLE_SHARE: Final = 0.5

#: Glyph names a text extractor emits when it has no character mapping.
_GLYPH_CODE_RE: Final = re.compile(r"/uni[0-9A-Fa-f]{4}|\(cid:\d+\)|/[gG]\d+")

#: Control characters are mapped to a space rather than dropped: NUL cannot be
#: stored in a text column, and dropping characters would make
#: ``character_count`` disagree with the glyphs the page actually yielded.
_CONTROL_CHARS: Final[dict[int, str]] = dict.fromkeys((*range(9), 11, 12, *range(14, 32), 127), " ")


def _normalise_page_text(raw: object) -> str:
    """Line endings unified and control characters neutralised.

    Done once, before any offset is computed, so the character ranges an anchor
    records index into exactly the string stored in ``content``.
    """
    if not raw or not isinstance(raw, str):
        return ""
    return raw.replace("\r\n", "\n").replace("\r", "\n").translate(_CONTROL_CHARS)


def _property_text(value: object, limit: int = MAX_PROPERTY_CHARS) -> str:
    if value is None:
        return ""
    text = value.decode("utf-8", errors="replace") if isinstance(value, bytes) else str(value)
    collapsed = " ".join(text.translate(_CONTROL_CHARS).split())
    return truncate(collapsed, limit)


def _resolve(value: object) -> Any:
    """Follow an indirect reference; anything else is returned unchanged."""
    getter = getattr(value, "get_object", None)
    return getter() if callable(getter) else value


def _illegible_share(text: str) -> tuple[int, int]:
    """``(unmapped characters, non-whitespace characters)`` in extracted text.

    A character counts as unmapped when the extractor gave back a glyph name or
    code point that carries no meaning: ``/uni0041``, ``(cid:12)``, U+FFFD, or a
    private-use code point. This measures *what came back*, not what the page
    says; the caller decides what to do about it.
    """
    total = sum(1 for char in text if not char.isspace())
    if not total:
        return 0, 0
    unmapped = 0
    for match in _GLYPH_CODE_RE.finditer(text):
        unmapped += sum(1 for char in match.group() if not char.isspace())
    for char in text:
        code = ord(char)
        if char == "�" or 0xE000 <= code <= 0xF8FF or 0xF0000 <= code <= 0x10FFFD:
            unmapped += 1
    return min(unmapped, total), total


def _page_has_image(page: Any) -> bool | None:
    """Whether the page's resources name an image; ``None`` when unreadable.

    Only used to describe what was observed about a page that yielded no text —
    never to conclude what the page contains.
    """
    try:
        resources = _resolve(page.get("/Resources"))
        xobjects: Any = _resolve(resources.get("/XObject")) if hasattr(resources, "get") else None
        values = getattr(xobjects, "values", None)
        if not callable(values):
            return False
        for entry in values():
            candidate = _resolve(entry)
            if hasattr(candidate, "get") and str(_resolve(candidate.get("/Subtype"))) == "/Image":
                return True
    except Exception:
        return None
    return False


def _field_value(annotation: Any) -> str:
    """The ``/V`` a widget displays, following ``/Parent`` when it inherits it."""
    node = annotation
    for _ in range(MAX_FIELD_PARENTS):
        if not hasattr(node, "get"):
            return ""
        value = _resolve(node.get("/V"))
        if isinstance(value, (str, bytes)):
            return _property_text(value, MAX_ANNOTATION_CHARS)
        node = _resolve(node.get("/Parent"))
    return ""


def _annotation_texts(entry: object, index: int) -> list[dict[str, Any]]:
    """The text one annotation carries: the value it displays and/or its note."""
    try:
        annotation = _resolve(entry)
        if not hasattr(annotation, "get"):
            return []
        subtype = str(_resolve(annotation.get("/Subtype")) or "")
        field = _property_text(_resolve(annotation.get("/T")), MAX_ANNOTATION_CHARS)
        candidates = (
            ("field_value", _field_value(annotation)),
            ("note", _property_text(_resolve(annotation.get("/Contents")), MAX_ANNOTATION_CHARS)),
        )
    # A malformed annotation costs that annotation, not the page.
    except Exception:
        return []

    found: list[dict[str, Any]] = []
    for source, text in candidates:
        if not text or any(text == existing["text"] for existing in found):
            continue
        found.append(
            {
                "annotation": index,
                "subtype": subtype,
                "field": field,
                "source": source,
                "text": text,
            }
        )
    return found


def _page_annotations(page: Any, page_text: str) -> tuple[list[dict[str, Any]], int]:
    """Annotation text on a page that its extracted text does not contain.

    Returns ``(recorded, further)`` — the annotation text kept for the record and
    how many additional annotations carried text that was not kept, so the
    caller can say so rather than let the overflow disappear.
    """
    try:
        annots = _resolve(page.get("/Annots"))
        entries = list(annots) if isinstance(annots, (list, tuple)) else []
    except Exception:
        return [], 0

    recorded: list[dict[str, Any]] = []
    further = max(0, len(entries) - MAX_ANNOTATIONS_SCANNED)
    for index, entry in enumerate(entries[:MAX_ANNOTATIONS_SCANNED], start=1):
        for found in _annotation_texts(entry, index):
            if found["text"] in page_text:
                continue
            if len(recorded) >= MAX_ANNOTATIONS_PER_PAGE:
                further += 1
                continue
            recorded.append(found)
    return recorded, further


def _declared_page_count(reader: Any) -> int | None:
    """The ``/Count`` the page tree declares, or ``None`` when it cannot be read."""
    try:
        root = _resolve(reader.trailer["/Root"])
        pages = _resolve(root.get("/Pages"))
        count = _resolve(pages.get("/Count")) if hasattr(pages, "get") else None
        return (
            int(count) if isinstance(count, (int, float)) and not isinstance(count, bool) else None
        )
    except Exception:
        return None


def _declared_form_fields(reader: Any) -> int:
    """How many top-level ``/AcroForm`` fields the catalog declares."""
    try:
        root = _resolve(reader.trailer["/Root"])
        form = _resolve(root.get("/AcroForm"))
        fields = _resolve(form.get("/Fields")) if hasattr(form, "get") else None
        return len(fields) if isinstance(fields, (list, tuple)) else 0
    except Exception:
        return 0


def _document_properties(reader: Any) -> dict[str, str]:
    """The five recorded ``/Info`` fields, empty when the PDF carries none."""
    metadata = reader.metadata
    if metadata is None:
        return {key: "" for key, _ in _PROPERTIES}
    return {key: _property_text(metadata.get(pdf_key)) for key, pdf_key in _PROPERTIES}


def _format_pages(numbers: Sequence[int]) -> str:
    """``page 3`` / ``pages 3, 5`` / ``pages 1-4, 9``, bounded in length."""
    if not numbers:
        return ""
    groups: list[str] = []
    start = previous = numbers[0]
    for number in numbers[1:]:
        if number == previous + 1:
            previous = number
            continue
        groups.append(str(start) if start == previous else f"{start}-{previous}")
        start = previous = number
    groups.append(str(start) if start == previous else f"{start}-{previous}")

    label = "page" if len(numbers) == 1 else "pages"
    if len(groups) <= MAX_LISTED_PAGE_GROUPS:
        return f"{label} " + ", ".join(groups)
    shown = ", ".join(groups[:MAX_LISTED_PAGE_GROUPS])
    return f"{label} {shown} and {len(groups) - MAX_LISTED_PAGE_GROUPS} further group(s)"


def _leading_excerpt(text: str) -> str:
    """The opening of a page, cut at a word boundary."""
    head = text.strip()
    if len(head) <= PAGE_EXCERPT_CHARS:
        return head
    cut = head[:PAGE_EXCERPT_CHARS]
    boundary = max(cut.rfind("\n"), cut.rfind(" "))
    if boundary > PAGE_EXCERPT_CHARS // 2:
        cut = cut[:boundary]
    return cut.rstrip() + "..."


def _empty_page_expectation(number: int, page: Any) -> str:
    """What is known about a page that yielded no text — and no more than that.

    The parser observed an empty extraction. It did *not* observe a scan, so it
    does not say the page is one: a page whose content stream could not be read
    looks identical here, and sending a reviewer to OCR a page that carries no
    image at all wastes the one action the warning exists to prompt.
    """
    has_image = _page_has_image(page)
    if has_image is True:
        observed = " Its resources include image data, which is consistent with a scanned page."
    elif has_image is False:
        observed = (
            " No image was found in its resources, so a scan is only one possible cause; "
            "a content stream that could not be read looks the same."
        )
    else:
        observed = ""
    return (
        f"Page {number} yielded no extractable text and nothing on it has been read."
        f"{observed} It requires OCR or manual review before any conclusion may be drawn "
        "about it."
    )


def _open_reader(handle: Any, filename: str) -> Any:
    try:
        return PdfReader(handle)
    # A library exception must never escape as itself: the parse service would
    # report it as an internal failure rather than as an unreadable file.
    except Exception as exc:
        raise ParserError(
            f"'{filename}' could not be opened as a PDF ({type(exc).__name__}: {exc}). "
            "It may be truncated, damaged, or not a PDF at all.",
            details={"reason": "unreadable_pdf", "error": type(exc).__name__},
        ) from exc


def _unlock(reader: Any, result: ParseResult, filename: str) -> bool:
    """Open an encrypted document if it needs no password, else refuse it.

    Many exports are encrypted only to set permissions and open with an empty
    user password; those are parsed, with a warning, because the encryption says
    nothing about the engineering content. A document that genuinely needs a
    password is a :class:`ParserError`: there is no content to record, and a
    result with zero pages would read as "examined, nothing found".
    """
    if not reader.is_encrypted:
        return False

    try:
        unlocked = reader.decrypt("")
    except (PyPdfError, DependencyError, NotImplementedError) as exc:
        raise ParserError(
            f"'{filename}' is encrypted with a scheme that could not be opened "
            f"({type(exc).__name__}: {exc}). Nothing in it has been read.",
            details={"reason": "password_protected", "error": type(exc).__name__},
        ) from exc

    if not unlocked:
        raise ParserError(
            f"'{filename}' is password-protected. Nothing in it has been read: "
            "supply a decrypted export before it can be reviewed.",
            details={"reason": "password_protected"},
        )

    result.warn(
        WarningCode.PASSWORD_PROTECTED,
        "The PDF is encrypted but opened with an empty user password. Permissions "
        "set by the producer may hide or restrict content that is therefore absent "
        "from the extracted text.",
    )
    return True


def _page_list(reader: Any, filename: str) -> list[Any]:
    try:
        return list(reader.pages)
    # A library exception must never escape as itself: the parse service would
    # report it as an internal failure rather than as an unreadable file.
    except Exception as exc:
        raise ParserError(
            f"The page tree of '{filename}' could not be read "
            f"({type(exc).__name__}: {exc}). Nothing in it has been read.",
            details={"reason": "unreadable_page_tree", "error": type(exc).__name__},
        ) from exc


def _page_text(page: Any) -> tuple[str, str]:
    """``(text, failure_reason)`` for one page; the reason is empty on success."""
    try:
        raw = page.extract_text()
    # One damaged page must not cost the document: the failure is returned so
    # the caller can record it as an unread page.
    except Exception as exc:
        return "", truncate(f"{type(exc).__name__}: {exc}", 200)
    return _normalise_page_text(raw), ""


class PdfParser:
    """Records the text layer of a PDF, and where there is none.

    ``content`` shape::

        {
          "document": {"page_count": int,
                       "properties": {"title", "author", "subject",
                                      "creator", "producer"},
                       "is_encrypted": bool,
                       "has_text_layer": bool,
                       "total_characters": int},
          "pages": [{"page": int,            # 1-based, as printed on the page
                     "character_count": int,
                     "text": str,
                     "lines": [{"line": int, "text": str}],
                     "has_text_layer": bool,
                     "annotations": [{"annotation": int,   # position in /Annots
                                      "subtype": str,      # e.g. "/Widget"
                                      "field": str,        # /T, for a form field
                                      "source": str,       # field_value | note
                                      "text": str}]}],
          "page_count": int,
        }

    ``annotations`` is a separate channel, not part of the page text: it holds
    text a reader sees on the page that the content stream does not contain
    (filled form fields, review markup). It is deliberately kept out of ``text``
    and ``lines`` — it has no character offset and therefore no range citation —
    and its presence is reported as a warning and an unsupported item.

    Anchors:

    ``page:<n>``
        :attr:`~app.core.enums.AnchorType.PDF_PAGE`, one per page. Direct
        evidence when the page yielded text; otherwise
        ``unsupported_content`` with an ``expectation`` naming what was not read.

    ``textrange:<n>:<start>-<end>``
        :attr:`~app.core.enums.AnchorType.PDF_TEXT_RANGE`, one per non-blank
        line. ``start``/``end`` are half-open character offsets into *that*
        page's ``text`` and the excerpt is exactly ``text[start:end]``, so a
        finding can quote one sentence rather than a whole page. A line longer
        than ``MAX_EXCERPT_CHARS`` becomes several consecutive ranges rather
        than one range whose excerpt covers only part of what it cites.
    """

    name = "pdf"
    #: 1.1.0 added the ``annotations`` channel and split over-long lines into
    #: several text ranges, so anchor keys stored by 1.0.0 may not be reproduced.
    version = "1.1.0"
    handles = (FileKind.PDF,)

    def parse(self, context: ParseContext) -> ParseResult:
        result = ParseResult(
            artifact_kind=FileKind.PDF.value,
            parser_name=self.name,
            parser_version=self.version,
        )

        try:
            size = context.path.stat().st_size
        except OSError as exc:
            raise ParserError(
                f"'{context.filename}' could not be read from storage: {exc}",
                details={"reason": "unreadable_file"},
            ) from exc
        if size == 0:
            raise ParserError(
                f"'{context.filename}' is empty (0 bytes) and contains no PDF at all.",
                details={"reason": "empty_file", "size_bytes": 0},
            )

        with context.path.open("rb") as handle:
            reader = _open_reader(handle, context.filename)
            encrypted = _unlock(reader, result, context.filename)
            declared = _declared_page_count(reader)
            pages = _page_list(reader, context.filename)
            if not pages:
                shortfall = (
                    f"Its page tree declares {declared} page(s), none of which could be read."
                    if declared
                    else "It declares no pages at all."
                )
                raise ParserError(
                    f"No page of '{context.filename}' could be read, so there is nothing to "
                    f"record. {shortfall}",
                    details={"reason": "no_pages", "declared_page_count": declared},
                )
            self._report_page_shortfall(result, declared=declared, read=len(pages))
            properties = _document_properties(reader)
            form_fields = _declared_form_fields(reader)
            payloads = self._read_pages(pages, context, result)
            self._report_unread_form(result, payloads, form_fields)

        characters = sum(int(page["character_count"]) for page in payloads)
        with_text = sum(1 for page in payloads if page["has_text_layer"])

        result.content = {
            "document": {
                "page_count": len(payloads),
                "properties": properties,
                "is_encrypted": encrypted,
                "has_text_layer": with_text > 0,
                "total_characters": characters,
            },
            "pages": payloads,
            "page_count": len(payloads),
        }
        result.summary = {
            "page_count": len(payloads),
            "pages_with_text_layer": with_text,
            "pages_without_text_layer": len(payloads) - with_text,
            "has_text_layer": with_text > 0,
            "total_characters": characters,
            "text_line_count": sum(
                1 for page in payloads for line in page["lines"] if line["text"].strip()
            ),
            "text_range_anchor_count": sum(
                1 for anchor in result.anchors if anchor.anchor_type is AnchorType.PDF_TEXT_RANGE
            ),
            "is_encrypted": encrypted,
        }
        return result

    def _read_pages(
        self,
        pages: list[Any],
        context: ParseContext,
        result: ParseResult,
    ) -> list[dict[str, Any]]:
        """Extract every page, anchor it, and collect what could not be read."""
        payloads: list[dict[str, Any]] = []
        empty: list[int] = []
        sparse: list[int] = []
        failed: list[int] = []
        illegible: list[int] = []
        annotated: list[int] = []
        overflowed: list[int] = []
        first_failure = ""
        ranges = 0

        for number, page in enumerate(pages, start=1):
            text, failure = _page_text(page)
            lines = text.split("\n") if text else []
            stripped = text.strip()
            annotations, further = _page_annotations(page, text)
            if annotations:
                annotated.append(number)
            if further:
                overflowed.append(number)
            payloads.append(
                {
                    "page": number,
                    "character_count": len(text),
                    "text": text,
                    "lines": [
                        {"line": index, "text": line} for index, line in enumerate(lines, start=1)
                    ],
                    "has_text_layer": bool(stripped),
                    "annotations": annotations,
                }
            )

            unmapped, total = _illegible_share(stripped)
            readable = True
            if failure:
                failed.append(number)
                first_failure = first_failure or failure
                expectation = (
                    f"Page {number} could not be decoded ({failure}), so none of it "
                    "has been read. It requires manual review."
                )
                evidence_kind = EvidenceKind.UNSUPPORTED_CONTENT
                readable = False
            elif not stripped:
                empty.append(number)
                expectation = _empty_page_expectation(number, page)
                evidence_kind = EvidenceKind.UNSUPPORTED_CONTENT
                readable = False
            elif total and unmapped / total > MAX_ILLEGIBLE_SHARE:
                illegible.append(number)
                expectation = (
                    f"{unmapped} of the {total} characters extracted from page {number} came "
                    "back as unmapped glyph codes rather than readable characters, so what the "
                    "page says has not been read. The extracted text is recorded as it came "
                    f"back; page {number} requires manual review."
                )
                evidence_kind = EvidenceKind.UNSUPPORTED_CONTENT
                readable = False
            elif len(stripped) < MIN_PAGE_CHARACTERS:
                sparse.append(number)
                expectation = (
                    f"Page {number} yielded only {len(stripped)} characters of text. The "
                    "remainder may be image-only and requires OCR or manual review."
                )
                evidence_kind = EvidenceKind.PARSER_WARNING
            else:
                expectation = ""
                evidence_kind = EvidenceKind.DIRECT

            result.add_anchor(
                AnchorDraft(
                    key=f"page:{number}",
                    anchor_type=AnchorType.PDF_PAGE,
                    display_location=f"{context.source_label} / page {number}",
                    structured_location={"page": number},
                    excerpt=_leading_excerpt(text),
                    evidence_kind=evidence_kind,
                    source_label=context.source_label,
                    expectation=expectation,
                )
            )
            if readable:
                ranges = self._add_line_anchors(number, text, context, result, ranges)

        self._report_unread(
            result,
            empty=empty,
            sparse=sparse,
            failed=failed,
            illegible=illegible,
            reason=first_failure,
        )
        self._report_annotations(result, annotated=annotated, overflowed=overflowed)
        return payloads

    def _add_line_anchors(
        self,
        number: int,
        text: str,
        context: ParseContext,
        result: ParseResult,
        emitted: int,
    ) -> int:
        """One citable character range per non-blank line; returns the new total.

        A line longer than ``MAX_EXCERPT_CHARS`` becomes consecutive ranges of
        that size. The alternative — one range spanning the whole line — would
        publish a character span wider than the excerpt quoting it, so the
        excerpt, its ``content_hash`` and the cited span would disagree and the
        citation could not be checked against the source.
        """
        offset = 0
        for line in text.split("\n"):
            line_start = offset + (len(line) - len(line.lstrip()))
            content = line.strip()
            offset += len(line) + 1
            if not content:
                continue

            for cut in range(0, len(content), MAX_EXCERPT_CHARS):
                excerpt = content[cut : cut + MAX_EXCERPT_CHARS]
                start = line_start + cut
                end = start + len(excerpt)

                if emitted >= MAX_TEXT_RANGE_ANCHORS:
                    if not any(
                        warning.code == WarningCode.TRUNCATED for warning in result.warnings
                    ):
                        result.warn(
                            WarningCode.TRUNCATED,
                            f"Line-level citations stop after {MAX_TEXT_RANGE_ANCHORS} character "
                            "ranges. The page text in the artifact is complete, but a finding "
                            "past this point must cite its page anchor instead of a range.",
                            f"page {number}",
                        )
                    return emitted

                emitted += 1
                result.add_anchor(
                    AnchorDraft(
                        key=f"textrange:{number}:{start}-{end}",
                        anchor_type=AnchorType.PDF_TEXT_RANGE,
                        display_location=(
                            f"{context.source_label} / page {number}, characters {start}-{end}"
                        ),
                        structured_location={"page": number, "start": start, "end": end},
                        excerpt=excerpt,
                        source_label=context.source_label,
                    )
                )
        return emitted

    @staticmethod
    def _report_page_shortfall(result: ParseResult, *, declared: int | None, read: int) -> None:
        """Report pages the document declares that are not in this record.

        A page tree that declares more pages than the library could resolve is
        the most dangerous silent failure in this parser: ``page_count`` simply
        shrinks, and a partially read document is indistinguishable from a fully
        examined one.
        """
        if declared is None or declared == read:
            return
        if declared > read:
            detail = (
                f"The page tree declares {declared} page(s) but only {read} could be read, so "
                f"{declared - read} page(s) of this document are absent from the record. "
                "Nothing on them has been read, their absence from the extracted text proves "
                "nothing, and they require manual review of the source file."
            )
        else:
            detail = (
                f"The page tree declares {declared} page(s) but {read} were read, so the page "
                "numbers in this record may not be the page numbers of the document. Page "
                "citations from this file should be checked against the source."
            )
        result.warn(WarningCode.STRUCTURE_UNRECOGNISED, detail, "page tree")
        result.mark_unsupported("unreadable_page", detail, "page tree")

    @staticmethod
    def _report_unread_form(
        result: ParseResult, payloads: list[dict[str, Any]], form_fields: int
    ) -> None:
        """Report a declared form whose values were found on no page."""
        if not form_fields:
            return
        if any(
            entry["source"] == "field_value" for page in payloads for entry in page["annotations"]
        ):
            return
        detail = (
            f"The document declares an interactive form with {form_fields} top-level field(s), "
            "and no field value was found on any page. Anything filled into the form is "
            "therefore not in this record: the form must be reviewed in the source file."
        )
        result.warn(WarningCode.HIDDEN_CONTENT, detail, "form")
        result.mark_unsupported("form_fields_not_read", detail, "form")

    @staticmethod
    def _report_annotations(
        result: ParseResult, *, annotated: list[int], overflowed: list[int]
    ) -> None:
        """Report annotation text that the page text does not contain."""
        if annotated:
            label = _format_pages(annotated)
            detail = (
                f"Text was found in annotations — form field values or review markup — on "
                f"{label} that the extracted page text does not contain. It is recorded "
                "separately under pages[].annotations, has no character offset and therefore "
                "no line-level citation: a finding that relies on it must cite the page."
            )
            result.warn(WarningCode.HIDDEN_CONTENT, detail, label)
            result.mark_unsupported("page_with_annotation_text", detail, label)

        if overflowed:
            label = _format_pages(overflowed)
            detail = (
                f"More than {MAX_ANNOTATIONS_PER_PAGE} annotations carrying text were found on "
                f"{label}; only the first {MAX_ANNOTATIONS_PER_PAGE} of each page are recorded. "
                "The remainder have not been read."
            )
            result.warn(WarningCode.TRUNCATED, detail, label)
            result.mark_unsupported("page_with_annotation_text", detail, label)

    @staticmethod
    def _report_unread(
        result: ParseResult,
        *,
        empty: list[int],
        sparse: list[int],
        failed: list[int],
        illegible: list[int],
        reason: str,
    ) -> None:
        """Warn, once per condition, about pages whose content was not read.

        Aggregated by condition rather than per page so a 300-page scan produces
        a usable report; the per-page record lives on the page anchors, which are
        emitted for every page either way.
        """
        if failed:
            label = _format_pages(failed)
            detail = (
                f"Text extraction failed for {label} (first error: {reason}), so the page "
                "content may be damaged. No OCR is attempted: nothing there has been read, "
                f"absence from the extracted text proves nothing, and {label} requires "
                "manual review."
            )
            result.warn(WarningCode.STRUCTURE_UNRECOGNISED, detail, label)
            result.mark_unsupported("undecodable_page", detail, label)

        if empty:
            label = _format_pages(empty)
            detail = (
                f"{label.capitalize()} yielded no extractable text, so nothing there has been "
                "read. The cause was not established: that is what an image-only or scanned "
                "page looks like, and equally what a page whose content stream could not be "
                "read looks like. No OCR is attempted and no cause is inferred, so OCR or "
                f"manual review of {label} is required before any conclusion may be drawn "
                "about it."
            )
            result.warn(WarningCode.NO_TEXT_LAYER, detail, label)
            result.mark_unsupported("page_without_text_layer", detail, label)

        if illegible:
            label = _format_pages(illegible)
            detail = (
                f"The text extracted from {label} is mostly unmapped glyph codes (such as "
                "/uni0041 or (cid:12)) rather than readable characters, which is what a font "
                "with no usable Unicode mapping yields. The extracted characters are recorded "
                f"as they came back, but what {label} says has not been read and no line-level "
                "citation is offered for it: it requires manual review."
            )
            result.warn(WarningCode.STRUCTURE_UNRECOGNISED, detail, label)
            result.mark_unsupported("page_text_not_legible", detail, label)

        if sparse:
            label = _format_pages(sparse)
            detail = (
                f"{label.capitalize()} yielded fewer than {MIN_PAGE_CHARACTERS} characters "
                "of text. Enough came back to show a text layer exists, but too little to "
                "treat as the content of the page: the remainder may be image-only, so OCR "
                f"or manual review of {label} is required."
            )
            result.warn(WarningCode.LOW_TEXT_YIELD, detail, label)
            result.mark_unsupported("page_with_low_text_yield", detail, label)
