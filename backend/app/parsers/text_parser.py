"""Plain-text fallback parser.

Loose notes are the most hazardous material in a review set. They are
unstructured, they are usually second-hand, and they are exactly the kind of
text a reviewer — human or model — can be tempted to read a conclusion into. So
this parser interprets as little as possible: it records which encoding it used,
the lines exactly as they appear, and only the shape it could actually see. It
does not join wrapped lines into sentences, does not expand abbreviations and
does not classify a note as an issue.

It rewrites exactly one character it read, and says so when it does: a NUL
becomes a space, because no PostgreSQL text column or ``jsonb`` document can
hold one and keeping it would fail the parse rather than record the file. See
:func:`_replace_nuls`.

Content shape (``ParseResult.content``)::

    encoding        {"name": str, "confidence": float, "had_bom": bool}
    line_count      int
    character_count int, over the newline-normalised text
    lines           [{"line": int, "text": str}]
    sections        [{"title": str, "title_source": str, "start_line": int,
                      "end_line": int, "lines": [{"line": int, "text": str}]}]
    paragraphs      [{"index": int, "start_line": int, "end_line": int, "text": str}]
    bullets         [{"line": int, "text": str, "marker": str}]

Every ``line``, ``start_line``, ``end_line`` and paragraph ``index`` is 1-based.
Line endings are normalised to ``\\n`` before anything else is computed, so the
same notes saved from Windows and from Linux produce byte-identical output, and
``character_count`` counts the normalised text rather than the raw bytes.

Sections partition the lines: every line belongs to exactly one section, and
text ahead of the first heading forms a leading section with an empty title.
When no heading is detectable the whole file is returned as one untitled section
and a :data:`~app.parsers.base.WarningCode.STRUCTURE_UNRECOGNISED` warning says
so — an unstructured note must not be given a structure it does not have.

``title_source`` records where a section title came from: ``"underlined"`` when
the file marks the line up as a heading, ``"caps"`` when nothing marks it up and
the title was inferred from the line being entirely upper case, and ``""`` for
the untitled leading section. That distinction is reported both ways round:
finding *too little* structure is a warning, and inferring *any* structure the
file did not mark up is also a warning, because a title the author never wrote
groups lines the author never grouped. A section whose title was inferred is
anchored as :data:`~app.core.enums.EvidenceKind.INFERENCE` and its label says
so, so the grouping is never offered as something the file states.
"""

from __future__ import annotations

import codecs
import re
from typing import Any, NamedTuple

from app.core.enums import AnchorType, EvidenceKind, FileKind
from app.core.errors import ParserError
from app.parsers.base import AnchorDraft, ParseContext, ParseResult, WarningCode, truncate

#: Byte-order marks, checked in this order because the UTF-32-LE mark begins
#: with the UTF-16-LE one. The codec named alongside each mark consumes it, and
#: when the file does *not* decode as that codec the mark bytes are removed
#: before the fallback ladder runs, so the BOM never survives into ``content``.
_BOMS: tuple[tuple[bytes, str], ...] = (
    (codecs.BOM_UTF32_LE, "utf-32"),
    (codecs.BOM_UTF32_BE, "utf-32"),
    (codecs.BOM_UTF8, "utf-8-sig"),
    (codecs.BOM_UTF16_LE, "utf-16"),
    (codecs.BOM_UTF16_BE, "utf-16"),
)

#: Single-byte codecs tried, in order, once UTF-8 has been ruled out. ``cp1252``
#: is strict about five unassigned bytes, so it fails loudly on data it cannot
#: represent; ``latin-1`` cannot fail at all and is therefore last.
_FALLBACK_CODECS: tuple[str, ...] = ("cp1252", "latin-1")

# Confidence is a coarse, fixed value per detection route rather than a
# statistical score. A statistical detector was considered and rejected here:
# on short notes it happily picks between codecs that decode without error and
# yet disagree on which characters they produce, which is silent guessing of
# the worst kind — the mangling lands in the review record looking like
# verbatim text. A fixed ladder is auditable: the number tells a reviewer which
# route was taken, and the route is what they need in order to judge the text.
_CONFIDENCE_BOM = 1.0
#: UTF-8 is self-validating: bytes that decode cleanly are almost never
#: something else, and pure ASCII is a subset of it — *unless* the decoded text
#: carries NUL characters, which plain text does not, and which BOM-less UTF-16
#: does for every ASCII character it holds.
_CONFIDENCE_UTF8 = 1.0
_CONFIDENCE_SINGLE_BYTE = 0.6
#: A clean UTF-8 decode that nevertheless contains NULs, and a wide encoding
#: inferred from the NUL pattern rather than declared by a mark: both are
#: guesses and are reported as such.
_CONFIDENCE_UTF8_WITH_NULS = 0.6
_CONFIDENCE_WIDE_GUESS = 0.6
_CONFIDENCE_LAST_RESORT = 0.3

#: Share of byte pairs that must carry a NUL on one fixed side before BOM-less
#: UTF-16 is inferred. Text is not required to be pure ASCII, so this is well
#: below 1.0, but it is high enough that ordinary bytes cannot reach it.
_WIDE_NUL_SHARE = 0.5

#: A run of these under a line of text marks that line as a heading.
_UNDERLINE_RE = re.compile(r"^(?:=+|-+|~+)$")
_MIN_UNDERLINE_LENGTH = 3

#: ``-``, ``*`` or ``12.`` followed by whitespace or end of line.
_BULLET_RE = re.compile(r"^(-|\*|\d+\.)(?:\s|$)")

#: An ALL-CAPS line longer than this is prose in caps, not a heading.
_MAX_CAPS_HEADING_CHARS = 60

#: How a section title was arrived at. ``_HEADING_UNDERLINED`` is markup the
#: file contains; ``_HEADING_CAPS`` is this parser's inference.
_HEADING_UNDERLINED = "underlined"
_HEADING_CAPS = "caps"

#: Inferred headings named individually in the warning before it stops listing
#: them; the count is always reported in full.
_MAX_INFERRED_HEADINGS_NAMED = 5

#: A heading is a line, so an underlined one can be arbitrarily long, while
#: ``EvidenceAnchor.display_location`` is a bounded column. Only the label is
#: shortened: the full title stays in ``content`` and in ``structured_location``.
_MAX_TITLE_IN_DISPLAY = 80

#: Line anchors are bounded so one large log cannot dominate the evidence
#: table. Exceeding it is reported, never silently absorbed.
MAX_LINE_ANCHORS = 5000

#: Sections are bounded too, and separately: a file with a heading every other
#: line would otherwise route around :data:`MAX_LINE_ANCHORS` and put one anchor
#: per line back into the evidence table under a different anchor type. Coarser
#: than the line bound because a document with more than this many headings is
#: not a document with headings.
MAX_SECTION_ANCHORS = 1000


class _Decoded(NamedTuple):
    """The outcome of decoding, including how it was arrived at."""

    text: str
    encoding: str
    confidence: float
    had_bom: bool
    #: Set when the route taken needs explaining beyond its name.
    note: str


def _decode(data: bytes) -> _Decoded:
    """Decode bytes to text, recording which route succeeded."""
    for mark, encoding in _BOMS:
        if not data.startswith(mark):
            continue
        try:
            return _Decoded(data.decode(encoding), encoding, _CONFIDENCE_BOM, True, "")
        except UnicodeDecodeError:
            note = (
                f"A {encoding} byte-order mark is present but the file does not "
                f"decode as {encoding}. The mark itself was removed before the "
                f"remaining bytes were decoded, so it is not part of the recorded text."
            )
            # Without dropping the mark its bytes decode as mojibake and land at
            # the front of line 1, where an anchor would then quote them as if
            # the author had typed them.
            return _fallback(data[len(mark) :], had_bom=True, note=note)

    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return _fallback(data, had_bom=False, note="")
    if "\x00" not in text:
        return _Decoded(text, "utf-8", _CONFIDENCE_UTF8, False, "")
    return _decode_with_nuls(data, text)


def _wide_encoding(data: bytes) -> str | None:
    """``utf-16-le``/``utf-16-be`` if the bytes look wide-encoded without a mark.

    Mostly-ASCII UTF-16 is a NUL on the same side of every byte pair. That is
    also valid UTF-8, so a clean UTF-8 decode does not rule it out; the NUL
    pattern is the only signal available once the byte-order mark is missing.
    """
    if len(data) < 4 or len(data) % 2:
        return None
    even_nuls = data[0::2].count(0)
    odd_nuls = data[1::2].count(0)
    pairs = len(data) // 2
    if odd_nuls and not even_nuls and odd_nuls >= pairs * _WIDE_NUL_SHARE:
        return "utf-16-le"
    if even_nuls and not odd_nuls and even_nuls >= pairs * _WIDE_NUL_SHARE:
        return "utf-16-be"
    return None


def _decode_with_nuls(data: bytes, utf8_text: str) -> _Decoded:
    """Handle bytes that decode as UTF-8 yet yield NUL characters.

    Either route is a guess and neither is reported at UTF-8's confidence: what
    the parser observed is a NUL pattern, not a declared encoding.
    """
    wide = _wide_encoding(data)
    if wide is not None:
        try:
            wide_text = data.decode(wide)
        except UnicodeDecodeError:
            wide_text = None
        if wide_text is not None and "\x00" not in wide_text:
            note = (
                f"No byte-order mark is present and the bytes do decode as UTF-8, but a "
                f"NUL sits on the same side of nearly every byte pair, which is what "
                f"ASCII text encoded as {wide} looks like. The file was decoded as {wide} "
                f"on that inference rather than as UTF-8."
            )
            return _Decoded(wide_text, wide, _CONFIDENCE_WIDE_GUESS, False, note)
    note = (
        "The bytes decode as UTF-8, but the decoded text contains NUL characters, "
        "which plain text does not normally contain, and no byte-order mark or "
        "wide-encoding pattern identified another encoding."
    )
    return _Decoded(utf8_text, "utf-8", _CONFIDENCE_UTF8_WITH_NULS, False, note)


def _fallback(data: bytes, *, had_bom: bool, note: str) -> _Decoded:
    for index, encoding in enumerate(_FALLBACK_CODECS):
        confidence = (
            _CONFIDENCE_LAST_RESORT
            if index == len(_FALLBACK_CODECS) - 1
            else _CONFIDENCE_SINGLE_BYTE
        )
        try:
            return _Decoded(data.decode(encoding), encoding, confidence, had_bom, note)
        except UnicodeDecodeError:
            continue
    # Unreachable while latin-1 is last: it maps every byte. Kept so a future
    # change to the ladder fails loudly instead of returning nothing.
    raise ParserError(
        "The file could not be decoded as text by any supported encoding.",
        details={"encodings_tried": ["utf-8", *_FALLBACK_CODECS]},
    )


def _normalise_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _replace_nuls(text: str) -> tuple[str, int]:
    """NUL characters mapped to a space, and how many there were.

    The one place this parser rewrites a character it read, and it does so
    because the alternative is not recording the text at all. PostgreSQL cannot
    hold ``\\x00`` in a text column and rejects the ``\\u0000`` escape in
    ``jsonb``, so a NUL reaching either ``ParsedArtifact.content`` or
    ``EvidenceAnchor.excerpt`` aborts the whole parse transaction with a driver
    message no engineer can act on -- and the file lands in ``parse_failed``
    with nothing said about why. SQLite stores NUL happily, which is exactly
    why demo mode never sees it.

    A space rather than a deletion, matching :mod:`app.parsers.pdf_parser`:
    dropping characters would put ``character_count`` and every line and
    section offset out of step with the text they index into. Only NUL is
    substituted; the other control characters are storable and stay as they
    were read.

    The count is returned rather than the substitution being made quietly,
    because :meth:`TextParser._report_binary_content` has to say it happened.
    """
    count = text.count("\x00")
    return (text.replace("\x00", " "), count) if count else (text, 0)


def _split_lines(text: str) -> list[str]:
    """Split normalised text into lines without inventing a trailing one."""
    if not text:
        return []
    body = text[:-1] if text.endswith("\n") else text
    return body.split("\n")


def _is_underline(stripped: str) -> bool:
    return len(stripped) >= _MIN_UNDERLINE_LENGTH and _UNDERLINE_RE.match(stripped) is not None


def _looks_like_caps_heading(stripped: str) -> bool:
    """Whether a line *looks* like an ALL-CAPS heading, before corroboration."""
    if not stripped or len(stripped) > _MAX_CAPS_HEADING_CHARS:
        return False
    if _is_underline(stripped) or _BULLET_RE.match(stripped):
        return False
    if any(char.islower() for char in stripped):
        return False
    return any(char.isupper() for char in stripped)


def _next_non_blank(line_texts: list[str], index: int) -> str | None:
    """The next non-blank line after ``index``, or ``None`` at end of file."""
    for candidate in line_texts[index + 1 :]:
        stripped = candidate.strip()
        if stripped:
            return stripped
    return None


def _is_caps_heading(line_texts: list[str], index: int, stripped: str) -> bool:
    """An upper-case line only counts as a heading if it actually heads something.

    Nothing in the file marks these lines up, so the only corroboration
    available is what follows them. Two cases are rejected because they are what
    a misfire looks like rather than what a heading looks like:

    * nothing but blank lines follows — a title over no body is not a title; and
    * the next line is another upper-case candidate — a run of upper-case lines
      is a log, a tag dump or a table, and calling its first line a heading
      invents a grouping for all the rest.

    The remaining case is still an inference and is reported as one; see
    :meth:`TextParser._report_shape`.
    """
    if not _looks_like_caps_heading(stripped):
        return False
    following = _next_non_blank(line_texts, index)
    if following is None:
        return False
    return not _looks_like_caps_heading(following)


class _Heading(NamedTuple):
    """A detected heading and how it was detected."""

    line: int
    title: str
    source: str


def _detect_headings(line_texts: list[str]) -> list[_Heading]:
    """Each heading, in document order, with the route that found it."""
    headings: list[_Heading] = []
    underline_of_previous = -1
    for index, raw in enumerate(line_texts):
        number = index + 1
        stripped = raw.strip()
        if number == underline_of_previous or not stripped:
            continue
        if _is_underline(stripped) or _BULLET_RE.match(stripped):
            continue
        following = line_texts[index + 1].strip() if index + 1 < len(line_texts) else ""
        if _is_underline(following):
            headings.append(_Heading(number, stripped, _HEADING_UNDERLINED))
            underline_of_previous = number + 1
        elif _is_caps_heading(line_texts, index, stripped):
            headings.append(_Heading(number, stripped, _HEADING_CAPS))
    return headings


def _line_rows(line_texts: list[str], start: int, end: int) -> list[dict[str, Any]]:
    """1-based ``[start, end]`` slice as ``{"line", "text"}`` rows."""
    return [{"line": number, "text": line_texts[number - 1]} for number in range(start, end + 1)]


def _build_sections(line_texts: list[str], headings: list[_Heading]) -> list[dict[str, Any]]:
    if not line_texts:
        return []

    starts: list[_Heading] = []
    if not headings or headings[0].line != 1:
        starts.append(_Heading(1, "", ""))
    starts.extend(headings)

    sections: list[dict[str, Any]] = []
    for position, heading in enumerate(starts):
        start = heading.line
        end = starts[position + 1].line - 1 if position + 1 < len(starts) else len(line_texts)
        sections.append(
            {
                "title": heading.title,
                "title_source": heading.source,
                "start_line": start,
                "end_line": end,
                "lines": _line_rows(line_texts, start, end),
            }
        )
    return sections


def _build_paragraphs(line_texts: list[str]) -> list[dict[str, Any]]:
    paragraphs: list[dict[str, Any]] = []
    start: int | None = None

    def close(first: int, last: int) -> None:
        paragraphs.append(
            {
                "index": len(paragraphs) + 1,
                "start_line": first,
                "end_line": last,
                "text": "\n".join(line_texts[first - 1 : last]),
            }
        )

    for index, raw in enumerate(line_texts):
        number = index + 1
        if raw.strip():
            if start is None:
                start = number
            continue
        if start is not None:
            close(start, number - 1)
            start = None
    if start is not None:
        close(start, len(line_texts))
    return paragraphs


def _build_bullets(line_texts: list[str]) -> list[dict[str, Any]]:
    bullets: list[dict[str, Any]] = []
    for index, raw in enumerate(line_texts):
        stripped = raw.strip()
        match = _BULLET_RE.match(stripped)
        if match is None:
            continue
        marker = match.group(1)
        bullets.append(
            {
                "line": index + 1,
                "text": stripped[len(marker) :].strip(),
                "marker": marker,
            }
        )
    return bullets


class TextParser:
    """Records the structure and text of a plain-text file, and nothing else."""

    name: str = "text"
    version: str = "1.1.0"
    handles: tuple[FileKind, ...] = (FileKind.TEXT,)

    def parse(self, context: ParseContext) -> ParseResult:
        data = self._read(context)
        decoded = _decode(data)
        text, nul_count = _replace_nuls(_normalise_newlines(decoded.text))
        line_texts = _split_lines(text)
        headings = _detect_headings(line_texts)
        sections = _build_sections(line_texts, headings)

        result = ParseResult(
            artifact_kind=FileKind.TEXT.value,
            parser_name=self.name,
            parser_version=self.version,
            content={
                "encoding": {
                    "name": decoded.encoding,
                    "confidence": decoded.confidence,
                    "had_bom": decoded.had_bom,
                },
                "line_count": len(line_texts),
                "character_count": len(text),
                "lines": _line_rows(line_texts, 1, len(line_texts)),
                "sections": sections,
                "paragraphs": _build_paragraphs(line_texts),
                "bullets": _build_bullets(line_texts),
            },
        )

        self._report_encoding(result, context, decoded)
        self._report_binary_content(result, context, nul_count)
        self._report_shape(result, context, text, headings)
        self._add_anchors(result, context, text, line_texts, sections)
        result.summary = self._summarise(result, decoded)
        return result

    # -- reading ------------------------------------------------------------

    def _read(self, context: ParseContext) -> bytes:
        try:
            return context.path.read_bytes()
        except OSError as exc:
            # Only the stored filename is reported: the absolute path on disk is
            # an internal detail and must not travel out with the error.
            raise ParserError(
                f"Cannot read text file '{context.filename}': {exc.strerror or type(exc).__name__}",
                details={"filename": context.filename},
            ) from exc

    # -- warnings -----------------------------------------------------------

    def _report_encoding(
        self, result: ParseResult, context: ParseContext, decoded: _Decoded
    ) -> None:
        if decoded.confidence >= _CONFIDENCE_UTF8 and not decoded.note:
            return
        opening = decoded.note or ("Character encoding was not declared and is not valid UTF-8.")
        message = (
            f"{opening} Decoded as {decoded.encoding} (confidence {decoded.confidence}). "
            f"Characters outside ASCII may not be the ones the author typed."
        )
        result.warn(WarningCode.ENCODING_GUESSED, message, location=context.source_label)

    def _report_binary_content(
        self, result: ParseResult, context: ParseContext, count: int
    ) -> None:
        """Record NUL *characters* in the decoded text, without diagnosing them.

        Counted on the decoded text rather than the raw bytes: a perfectly
        ordinary UTF-16 file has a NUL byte for every ASCII character, and
        calling that binary would be a false alarm on every wide-encoded note.
        What is left after decoding is a real oddity, but "binary file" is one
        possible cause among several — a wide encoding this parser could not
        identify is another — and the parser has observed neither, so the item
        states what was counted and leaves the cause to the reader.

        Reported twice over, because it is now two different facts. The
        unsupported item is the observation, unchanged: this file held NUL and
        was not interpreted as a binary format. The warning is the *edit* --
        the one place this parser does not record what it read -- and it is a
        warning rather than an item because a reader comparing an excerpt with
        the file has to be told the recorded text differs from it.
        """
        if not count:
            return
        result.mark_unsupported(
            "binary_content",
            f"{count} NUL character(s) were present in the decoded text. Plain text does "
            f"not normally contain NUL, so this may be a binary format, or text in an "
            f"encoding this parser did not identify; that is an observation, not a "
            f"conclusion. No attempt was made to interpret the file as a binary format.",
            location=context.source_label,
        )
        result.warn(
            WarningCode.ENCODING_GUESSED,
            f"{count} NUL character(s) were replaced with a space before the text was "
            f"recorded, so the recorded text is not byte-for-byte what the file holds. "
            f"A space rather than a deletion, so line and character positions still "
            f"match. The substitution is not cosmetic: a NUL cannot be stored in a "
            f"PostgreSQL text column or a jsonb document, and keeping it would fail the "
            f"whole parse with a database error instead of recording the file.",
            location=context.source_label,
            severity="error",
        )

    def _report_shape(
        self,
        result: ParseResult,
        context: ParseContext,
        text: str,
        headings: list[_Heading],
    ) -> None:
        if not text.strip():
            result.warn(
                WarningCode.EMPTY_DOCUMENT,
                "The file contains no text (it is empty or only whitespace), so there "
                "is nothing to review in it.",
                location=context.source_label,
            )
            return
        if not headings:
            result.warn(
                WarningCode.STRUCTURE_UNRECOGNISED,
                "No heading could be detected (no underlined or ALL-CAPS heading line), "
                "so the whole file is recorded as one untitled section rather than being "
                "split into invented ones.",
                location=context.source_label,
                severity="info",
            )
            return
        self._report_inferred_headings(result, context, headings)

    def _report_inferred_headings(
        self,
        result: ParseResult,
        context: ParseContext,
        headings: list[_Heading],
    ) -> None:
        """Inventing structure is reported as loudly as failing to find any.

        Only reporting "no structure found" makes the parser silent in exactly
        the direction that costs a reviewer something: a title the file never
        wrote, over a span the author never grouped, reads to an agent as an
        association the source made.
        """
        inferred = [heading for heading in headings if heading.source == _HEADING_CAPS]
        if not inferred:
            return
        named = ", ".join(
            f"line {heading.line} {heading.title!r}"
            for heading in inferred[:_MAX_INFERRED_HEADINGS_NAMED]
        )
        if len(inferred) > _MAX_INFERRED_HEADINGS_NAMED:
            named = f"{named}, and {len(inferred) - _MAX_INFERRED_HEADINGS_NAMED} more"
        result.warn(
            WarningCode.STRUCTURE_UNRECOGNISED,
            f"{len(inferred)} of {len(headings)} section title(s) were inferred from a "
            f"line being entirely upper case, not from any markup in the file: {named}. "
            f"The lines grouped under such a title were not grouped by the author, and "
            f"the title is not a statement the file makes about them.",
            location=context.source_label,
            severity="info",
        )

    # -- anchors ------------------------------------------------------------

    def _add_anchors(
        self,
        result: ParseResult,
        context: ParseContext,
        text: str,
        line_texts: list[str],
        sections: list[dict[str, Any]],
    ) -> None:
        label = context.source_label
        result.add_anchor(
            AnchorDraft(
                key="file",
                anchor_type=AnchorType.FILE_WHOLE,
                display_location=f"{label} / whole document",
                structured_location={
                    "line_count": len(line_texts),
                    "character_count": len(text),
                },
                excerpt=truncate(text),
                evidence_kind=EvidenceKind.DIRECT,
                source_label=label,
            )
        )

        anchored_sections = 0
        for section in sections:
            start = int(section["start_line"])
            end = int(section["end_line"])
            span = f"line {start}" if start == end else f"lines {start}-{end}"
            if anchored_sections >= MAX_SECTION_ANCHORS:
                result.warn(
                    WarningCode.TRUNCATED,
                    f"Only the first {MAX_SECTION_ANCHORS} sections were given an anchor; "
                    f"the section at {span} and every section after it have no anchor of "
                    f"their own. Every line is still recorded in the parsed content, and "
                    f"the whole-file anchor still covers them.",
                    location=f"{label} / {span}",
                )
                break
            title = str(section["title"])
            inferred = str(section["title_source"]) == _HEADING_CAPS
            shown = truncate(title, _MAX_TITLE_IN_DISPLAY)
            if not title:
                display_location = f"{label} / {span}"
            elif inferred:
                # The title is this parser's reading of the line, so the label
                # says that rather than presenting it as the file's own heading.
                display_location = f"{label} / {shown} (inferred heading, {span})"
            else:
                display_location = f"{label} / {shown} ({span})"
            result.add_anchor(
                AnchorDraft(
                    key=f"section:{start}-{end}",
                    anchor_type=AnchorType.FILE_SECTION,
                    display_location=display_location,
                    structured_location={
                        "title": title,
                        "title_source": str(section["title_source"]),
                        "start_line": start,
                        "end_line": end,
                    },
                    excerpt=truncate("\n".join(line_texts[start - 1 : end])),
                    evidence_kind=(EvidenceKind.INFERENCE if inferred else EvidenceKind.DIRECT),
                    source_label=label,
                )
            )
            anchored_sections += 1

        # A blank line has nothing to quote, so it gets no anchor; the cap
        # counts the lines actually anchored.
        anchored = 0
        for index, raw in enumerate(line_texts):
            if not raw.strip():
                continue
            number = index + 1
            if anchored >= MAX_LINE_ANCHORS:
                result.warn(
                    WarningCode.TRUNCATED,
                    f"Only the first {MAX_LINE_ANCHORS} non-blank lines were given a line "
                    f"anchor; lines from {number} onward have no anchor of their own. "
                    f"Every line is still recorded in the parsed content, and such a line "
                    f"can be cited through the whole-file anchor, or through the anchor "
                    f"for the section containing it where one was emitted.",
                    location=f"{label} / line {number}",
                )
                break
            result.add_anchor(
                AnchorDraft(
                    key=f"line:{number}",
                    anchor_type=AnchorType.FILE_LINE,
                    display_location=f"{label} / line {number}",
                    structured_location={"line": number},
                    excerpt=raw,
                    evidence_kind=EvidenceKind.DIRECT,
                    source_label=label,
                )
            )
            anchored += 1

    # -- summary ------------------------------------------------------------

    def _summarise(self, result: ParseResult, decoded: _Decoded) -> dict[str, Any]:
        content = result.content
        line_rows: list[dict[str, Any]] = content["lines"]
        return {
            "encoding": decoded.encoding,
            "encoding_confidence": decoded.confidence,
            "had_bom": decoded.had_bom,
            "line_count": content["line_count"],
            "blank_line_count": sum(1 for row in line_rows if not str(row["text"]).strip()),
            "character_count": content["character_count"],
            "section_count": len(content["sections"]),
            "headings_detected": sum(1 for section in content["sections"] if section["title"]),
            # Reported next to headings_detected so the cheap view says how much
            # of the structure the file marked up and how much was inferred.
            "headings_inferred": sum(
                1 for section in content["sections"] if section["title_source"] == _HEADING_CAPS
            ),
            "paragraph_count": len(content["paragraphs"]),
            "bullet_count": len(content["bullets"]),
            "line_anchor_count": sum(
                1 for anchor in result.anchors if anchor.anchor_type is AnchorType.FILE_LINE
            ),
            "section_anchor_count": sum(
                1 for anchor in result.anchors if anchor.anchor_type is AnchorType.FILE_SECTION
            ),
            "anchor_count": len(result.anchors),
        }
