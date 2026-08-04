"""Delimited text (CSV) parsing.

A CSV file declares neither its encoding nor its delimiter, so both must be
inferred and both can be inferred wrongly. Read ``Alarm_List.csv`` with a comma
instead of a semicolon and every row collapses into one column of nonsense that
still looks like a perfectly successful parse. This parser therefore records
*how* it decided — encoding, confidence, delimiter, and the basis for each — as
warnings, so a downstream agent can see the inference rather than inherit it.

Nothing is dropped quietly either: comment and blank lines are listed in
``skipped_lines`` with their real line numbers, and a row whose field count
disagrees with the header is recorded exactly as found and named in a
``ragged_rows`` warning instead of being padded or truncated.

Row and column positions are preserved as they appear in the file — 1-based
physical line numbers, 1-based column indices — so a finding can say
"row 14, column 'Trip Tag'" and a reviewer can open the file and look at it.

Nothing here interprets a value. A tag name is text in a cell and a threshold is
text in a cell; deciding what either *means* is the agents' job.

Conventions this parser applies, stated because they are assumptions rather than
facts the format supplies:

* the first line that is neither blank nor a ``#`` comment is the header row;
* a line whose first non-space character is ``#`` is a comment, but only when the
  reader is between records — inside a quoted field it is data;
* every skipped, unread or re-labelled line is visible in ``skipped_lines``,
  ``warnings`` or both.
"""

from __future__ import annotations

import codecs
import csv
from dataclasses import dataclass
from itertools import islice
from pathlib import Path
from typing import Any

from charset_normalizer import from_bytes

from app.core.enums import AnchorType, EvidenceKind, FileKind
from app.core.errors import ParserError
from app.parsers.base import (
    AnchorDraft,
    ParseContext,
    ParseResult,
    WarningCode,
    truncate,
)

#: Delimiters the sniffer may choose between. Restricted so that a sniff over an
#: awkward sample cannot settle on something like a space or a digit and turn
#: every value into a fragment.
CANDIDATE_DELIMITERS = ",;\t|"
DEFAULT_DELIMITER = ","
DEFAULT_QUOTECHAR = '"'
COMMENT_PREFIX = "#"

#: Lines fed to :class:`csv.Sniffer`. A sample, not the file: sniffing scales
#: with its input and a header plus a few rows is what actually decides.
MAX_SNIFF_LINES = 20
MAX_SNIFF_CHARS = 64 * 1024

#: Bounds on output size. Both are honest limits: hitting either produces a
#: ``truncated`` warning naming where reading stopped, never a short result that
#: looks complete.
MAX_ROWS = 50_000
MAX_CELL_ANCHORS = 50_000

#: ``skipped_lines`` reasons. Stable tokens; reports branch on these.
REASON_BLANK = "blank"
REASON_COMMENT = "comment"
REASON_PARSE_STOPPED = "parse_stopped"
REASON_ROW_LIMIT = "row_limit"

#: The decoded form of a byte order mark, and the character a failed decode
#: leaves behind. Named because both are invisible in source.
_BOM_CHAR = "﻿"
_REPLACEMENT_CHAR = "�"

#: The one character this parser rewrites. PostgreSQL cannot hold ``\x00`` in a
#: text column and rejects the ``\u0000`` escape in ``jsonb``, so a NUL reaching
#: ``ParsedArtifact.content`` or ``EvidenceAnchor.excerpt`` aborts the parse
#: transaction with a driver-level error and the upload lands in ``parse_failed``
#: saying nothing an engineer can act on. SQLite stores it without complaint,
#: which is why demo mode never sees this. Every substitution is reported; see
#: :func:`_decode`.
_NUL_CHAR = "\x00"

_BOM_SIGNATURES = (
    codecs.BOM_UTF8,
    codecs.BOM_UTF32_LE,
    codecs.BOM_UTF32_BE,
    codecs.BOM_UTF16_LE,
    codecs.BOM_UTF16_BE,
)


@dataclass(frozen=True, slots=True)
class _Line:
    """One physical line of the file.

    ``raw`` keeps the line terminator because :class:`csv.reader` needs it to
    reproduce a newline embedded in a quoted field; ``text`` is the line as a
    human sees it and is what excerpts and ``skipped_lines`` carry.
    """

    number: int
    text: str
    raw: str


def _split_lines(text: str) -> list[_Line]:
    """Split on CR, LF and CRLF only.

    ``str.splitlines`` also breaks on vertical tab, form feed and U+2028, which
    would silently split a field that legitimately contains one.
    """
    lines: list[_Line] = []
    start = 0
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char == "\r":
            end = index + 2 if text[index + 1 : index + 2] == "\n" else index + 1
        elif char == "\n":
            end = index + 1
        else:
            index += 1
            continue
        lines.append(_Line(len(lines) + 1, text[start:index], text[start:end]))
        index = end
        start = end
    if start < length:
        lines.append(_Line(len(lines) + 1, text[start:], text[start:]))
    return lines


def _skip_reason(line: _Line) -> str:
    """Why this line is not a record, or ``""`` if it is one."""
    stripped = line.text.strip()
    if not stripped:
        return REASON_BLANK
    if stripped.startswith(COMMENT_PREFIX):
        return REASON_COMMENT
    return ""


class _RecordFeeder:
    """Feeds physical lines to :class:`csv.reader`, remembering what it fed.

    Two jobs the stdlib reader cannot do for us:

    * a line is only eligible to be skipped as blank or as a comment when the
      reader sits *between* records — inside a quoted field such a line is data;
    * the reader's own ``line_num`` counts the lines it was handed, which is not
      the line number in the file once anything has been skipped. Recording each
      consumed line lets every record keep its true position.
    """

    def __init__(self, lines: list[_Line]) -> None:
        self._lines = lines
        self.index = 0
        self.at_boundary = True
        self.consumed: list[_Line] = []
        self.skipped: list[tuple[_Line, str]] = []

    def __iter__(self) -> _RecordFeeder:
        return self

    def __next__(self) -> str:
        while self.index < len(self._lines):
            line = self._lines[self.index]
            self.index += 1
            if self.at_boundary:
                reason = _skip_reason(line)
                if reason:
                    self.skipped.append((line, reason))
                    continue
            self.at_boundary = False
            self.consumed.append(line)
            return line.raw
        raise StopIteration

    @property
    def remaining(self) -> list[_Line]:
        return self._lines[self.index :]


def _codes(result: ParseResult) -> set[str]:
    return {warning.code for warning in result.warnings}


def _read_bytes(path: Path, label: str) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        # Deliberately not ``str(exc)``: that embeds the absolute path, which
        # must never reach parser output.
        raise ParserError(
            f"Cannot read the CSV file: {exc.strerror or type(exc).__name__}.",
            details={"source": label},
        ) from exc


def _decode(raw: bytes, result: ParseResult, label: str) -> tuple[str, str, float]:
    """Decode ``raw``, reporting the encoding chosen and why.

    Always warns: a CSV carries no in-band encoding declaration, so the choice is
    always an inference and is always worth showing, even when it is certain.
    """
    if not raw:
        result.warn(
            WarningCode.ENCODING_GUESSED,
            "The file is empty, so no encoding could be detected; assumed utf-8.",
            location=label,
        )
        return "", "utf-8", 0.0

    match = from_bytes(raw).best()
    if match is None:
        encoding = "utf-8"
        confidence = 0.0
        basis = "charset_normalizer identified no candidate encoding"
    else:
        encoding = match.encoding
        confidence = round(1.0 - float(match.chaos), 4)
        basis = f"charset_normalizer, confidence {confidence:.2f}"

    try:
        text = raw.decode(encoding, errors="replace")
    except LookupError:
        result.warn(
            WarningCode.ENCODING_GUESSED,
            f"Python has no codec named '{encoding}', so the file was decoded as "
            "utf-8 with unreadable bytes replaced.",
            location=label,
        )
        encoding = "utf-8"
        confidence = 0.0
        text = raw.decode(encoding, errors="replace")
        basis = "no codec for the detected encoding"

    result.warn(
        WarningCode.ENCODING_GUESSED,
        f"Decoded as '{encoding}' ({basis}). A CSV file declares no encoding, so "
        "this was inferred from the bytes.",
        location=label,
    )

    replacements = text.count(_REPLACEMENT_CHAR)
    if replacements:
        result.warn(
            WarningCode.ENCODING_GUESSED,
            f"{replacements} byte sequence(s) could not be decoded as '{encoding}' "
            "and were replaced with U+FFFD, so those values are not the original text.",
            location=label,
            severity="error",
        )

    nuls = text.count(_NUL_CHAR)
    if nuls:
        # ``errors="replace"`` does not touch these: a NUL byte decodes cleanly
        # in every codec, so it arrives in the text intact. See _NUL_CHAR.
        text = text.replace(_NUL_CHAR, " ")
        result.warn(
            WarningCode.ENCODING_GUESSED,
            f"{nuls} NUL character(s) were replaced with a space, so those values are "
            "not the original text. A space rather than a deletion, so the character "
            "positions an anchor records still line up. The substitution is not "
            "cosmetic: a NUL cannot be stored in a PostgreSQL text column or a jsonb "
            "document, and keeping it would fail the whole parse with a database error "
            "instead of recording the file.",
            location=label,
            severity="error",
        )
    return text, encoding, confidence


def _strip_bom(text: str, raw: bytes) -> tuple[str, bool]:
    had_bom = any(raw.startswith(signature) for signature in _BOM_SIGNATURES)
    if text.startswith(_BOM_CHAR):
        # utf-16/32 codecs consume the mark; utf-8 leaves it on the first value,
        # where it would corrupt the first header name.
        return text[1:], True
    return text, had_bom


def _line_terminator(lines: list[_Line]) -> str:
    """The first terminator observed, or ``""`` for a file with none."""
    for line in lines:
        for candidate in ("\r\n", "\n", "\r"):
            if line.raw.endswith(candidate):
                return candidate
    return ""


def _sniff_dialect(lines: list[_Line], result: ParseResult, label: str) -> tuple[str, str]:
    """Choose a delimiter and quote character, and say how.

    The sample excludes comment and blank lines: the ``#`` banner on
    ``IO_List.csv`` contains no commas and is enough on its own to make
    :class:`csv.Sniffer` give up on an otherwise obvious comma-delimited file.
    """
    sample_lines = list(
        islice((line.text for line in lines if not _skip_reason(line)), MAX_SNIFF_LINES)
    )
    delimiter = DEFAULT_DELIMITER
    quotechar = DEFAULT_QUOTECHAR
    sniffed = False

    if not sample_lines:
        basis = "there was no data line to sample"
    else:
        sample = "\n".join(sample_lines)[:MAX_SNIFF_CHARS]
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=CANDIDATE_DELIMITERS)
        except csv.Error as exc:
            basis = f"csv.Sniffer could not decide ({exc})"
        else:
            delimiter = dialect.delimiter
            if dialect.quotechar and len(dialect.quotechar) == 1:
                quotechar = dialect.quotechar
            basis = f"csv.Sniffer over {len(sample_lines)} sample line(s)"
            sniffed = True

    result.warn(
        WarningCode.DELIMITER_GUESSED,
        f"Using delimiter {delimiter!r} and quote character {quotechar!r} "
        f"({basis}). A CSV file declares no delimiter, so this was inferred"
        + (
            "."
            if sniffed
            else f"; {DEFAULT_DELIMITER!r} is the fallback, and a wrong delimiter "
            "would collapse every row into a single column."
        ),
        location=label,
    )
    return delimiter, quotechar


def _column_names(headers: list[str]) -> tuple[list[str], list[str]]:
    """Unique keys for ``record`` and for cell anchors, plus any problems found.

    ``headers`` keeps the header row verbatim. These names are derived from it:
    surrounding whitespace removed, a blank heading filled in by position and a
    repeated heading suffixed, because a duplicate key would otherwise drop a
    whole column out of every ``record`` without trace.
    """
    names: list[str] = []
    problems: list[str] = []
    used: set[str] = set()
    for index, heading in enumerate(headers, start=1):
        base = heading.strip()
        if not base:
            base = f"column_{index}"
            problems.append(f"column {index} has a blank heading, recorded as '{base}'")
        name = base
        suffix = 1
        while name in used:
            suffix += 1
            name = f"{base}_{suffix}"
        if name != base:
            problems.append(f"column {index} repeats the heading '{base}', recorded as '{name}'")
        used.add(name)
        names.append(name)
    return names, problems


def _read_records(
    lines: list[_Line],
    delimiter: str,
    quotechar: str,
    result: ParseResult,
    label: str,
) -> tuple[list[tuple[int, str, list[str]]], list[dict[str, Any]], bool]:
    """Read every record, returning ``(records, skipped_lines, truncated)``.

    A record is ``(first line number, raw text of its lines, field values)``.
    """
    feeder = _RecordFeeder(lines)
    reader = csv.reader(feeder, delimiter=delimiter, quotechar=quotechar)
    records: list[tuple[int, str, list[str]]] = []
    #: ``(first line, last line)`` of each record that spans more than one line.
    spans: list[tuple[int, int]] = []
    stop_reason = ""
    truncated = False

    while True:
        if len(records) > MAX_ROWS:
            # One header plus MAX_ROWS data rows. Reaching the limit only
            # truncates if something is actually left: a file with exactly
            # MAX_ROWS data rows was read in full and must not be reported as
            # cut short.
            if feeder.remaining:
                stop_reason = REASON_ROW_LIMIT
                truncated = True
                result.warn(
                    WarningCode.TRUNCATED,
                    f"Stopped after {MAX_ROWS} data rows; {len(feeder.remaining)} further "
                    "line(s) in the file were not read.",
                    location=f"{label} / row {feeder.remaining[0].number}",
                )
            break
        feeder.at_boundary = True
        mark = len(feeder.consumed)
        try:
            values = next(reader)
        except StopIteration:
            break
        except csv.Error as exc:
            stop_reason = REASON_PARSE_STOPPED
            truncated = True
            result.warn(
                WarningCode.STRUCTURE_UNRECOGNISED,
                f"csv could not read the file past this point ({exc}); "
                f"{len(feeder.remaining)} line(s) were not read.",
                location=f"{label} / row {feeder.remaining[0].number}"
                if feeder.remaining
                else label,
            )
            break
        consumed = feeder.consumed[mark:]
        if not consumed:
            break
        raw_text = "".join(item.raw for item in consumed).rstrip("\r\n")
        records.append((consumed[0].number, raw_text, values))
        if len(consumed) > 1:
            spans.append((consumed[0].number, consumed[-1].number))

    if spans:
        shown = "; ".join(f"row {first} spans lines {first}-{last}" for first, last in spans[:5])
        if len(spans) > 5:
            shown += f"; and {len(spans) - 5} more"
        result.warn(
            WarningCode.STRUCTURE_UNRECOGNISED,
            f"{len(spans)} record(s) contain a line break inside a quoted field, so "
            f"row numbers are the first line of each record and are not consecutive "
            f"({shown}). An unclosed quote produces the same shape, so the quoting is "
            "worth checking if this was not intended.",
            location=f"{label} / row {spans[0][0]}",
        )

    skipped = [
        {"row": line.number, "reason": reason, "text": truncate(line.text)}
        for line, reason in feeder.skipped
    ]
    if stop_reason and feeder.remaining:
        first_unread = feeder.remaining[0]
        skipped.append(
            {
                "row": first_unread.number,
                "reason": stop_reason,
                "text": truncate(first_unread.text),
            }
        )
    skipped.sort(key=lambda entry: (entry["row"], entry["reason"]))
    return records, skipped, truncated


class CsvParser:
    """Parses a delimited text file into rows addressed by real line number."""

    name = "csv"
    version = "1.0.0"
    handles = (FileKind.CSV,)

    def parse(self, context: ParseContext) -> ParseResult:
        label = context.source_label
        result = ParseResult(
            artifact_kind=FileKind.CSV.value,
            parser_name=self.name,
            parser_version=self.version,
        )

        raw = _read_bytes(context.path, label)
        text, encoding, confidence = _decode(raw, result, label)
        text, had_bom = _strip_bom(text, raw)
        lines = _split_lines(text)
        delimiter, quotechar = _sniff_dialect(lines, result, label)
        records, skipped, truncated = _read_records(lines, delimiter, quotechar, result, label)

        dialect = {
            "delimiter": delimiter,
            "quotechar": quotechar,
            "encoding": encoding,
            "encoding_confidence": confidence,
            "line_terminator": _line_terminator(lines),
            "had_bom": had_bom,
        }

        if not records:
            result.warn(
                WarningCode.EMPTY_DOCUMENT,
                "No header row was found: the file has no line that is neither "
                "blank nor a comment.",
                location=label,
            )
            result.content = {
                "dialect": dialect,
                "headers": [],
                "column_names": [],
                "header_row_number": 0,
                "rows": [],
                "skipped_lines": skipped,
                "column_count": 0,
            }
            result.summary = self._summary(
                result.content, populated_cells=0, ragged=0, truncated=truncated
            )
            self._report_comments(result, skipped, label)
            return result

        header_row_number, header_text, headers = records[0]
        column_names, header_problems = _column_names(headers)
        if header_problems:
            result.warn(
                WarningCode.STRUCTURE_UNRECOGNISED,
                "The header row is not usable as-is: "
                + "; ".join(header_problems)
                + ". Values are keyed by the recorded names.",
                location=f"{label} / row {header_row_number}",
            )

        rows: list[dict[str, Any]] = []
        #: The line as it appears in the file, kept out of ``content`` (it would
        #: double its size) and used as the row anchor's excerpt.
        raw_texts: list[str] = []
        ragged_rows: list[int] = []
        for row_number, row_text, values in records[1:]:
            raw_texts.append(row_text)
            record = {
                name: values[index]
                for index, name in enumerate(column_names)
                if index < len(values)
            }
            missing = column_names[len(values) :]
            extra = values[len(column_names) :]
            if missing or extra:
                ragged_rows.append(row_number)
            rows.append(
                {
                    "row": row_number,
                    "values": values,
                    "record": record,
                    "ragged": bool(missing or extra),
                    "missing_columns": missing,
                    "extra_values": extra,
                }
            )

        if not rows:
            result.warn(
                WarningCode.EMPTY_DOCUMENT,
                f"A header row was found at line {header_row_number} but the file "
                "contains no data rows.",
                location=label,
            )
        if ragged_rows:
            shown = ", ".join(str(number) for number in ragged_rows[:20])
            if len(ragged_rows) > 20:
                shown += f" and {len(ragged_rows) - 20} more"
            result.warn(
                WarningCode.RAGGED_ROWS,
                f"{len(ragged_rows)} row(s) do not have the {len(column_names)} fields "
                f"of the header row: rows {shown}. Values are recorded exactly as "
                "found; nothing was padded or truncated.",
                location=f"{label} / rows {shown}",
            )

        result.content = {
            "dialect": dialect,
            "headers": headers,
            "column_names": column_names,
            "header_row_number": header_row_number,
            "rows": rows,
            "skipped_lines": skipped,
            "column_count": len(headers),
        }
        populated_cells = self._add_anchors(
            result, label, header_row_number, header_text, column_names, rows, raw_texts
        )
        result.summary = self._summary(
            result.content,
            populated_cells=populated_cells,
            ragged=len(ragged_rows),
            # Anchors are bounded separately from rows, so the flag is derived
            # from the warnings rather than from the row loop alone: a reader of
            # the summary must not see ``truncated: false`` next to a
            # ``truncated`` warning.
            truncated=truncated or WarningCode.TRUNCATED in _codes(result),
        )
        self._report_comments(result, skipped, label)
        return result

    # -- helpers ----------------------------------------------------------
    def _add_anchors(
        self,
        result: ParseResult,
        label: str,
        header_row_number: int,
        header_text: str,
        column_names: list[str],
        rows: list[dict[str, Any]],
        raw_texts: list[str],
    ) -> int:
        """Anchor the header row, every data row and every populated cell.

        A row anchor quotes the line as it appears in the file, so a reviewer sees
        the same text they will see when they open it. A cell anchor quotes the
        one value, which is what the finding is about.

        A value beyond the header's width deliberately gets no cell anchor: it has
        no column, so there is no ``cell:<row>:<column>`` address for it. It stays
        visible in the row anchor's excerpt, in ``extra_values`` and in the
        ``ragged_rows`` warning.
        """
        result.add_anchor(
            AnchorDraft(
                key=f"row:{header_row_number}",
                anchor_type=AnchorType.CSV_ROW,
                display_location=f"{label} / row {header_row_number}",
                structured_location={"row": header_row_number, "kind": "header"},
                excerpt=header_text,
                evidence_kind=EvidenceKind.DIRECT,
                source_label=label,
            )
        )

        populated_cells = 0
        cell_anchors = 0
        cells_truncated = False
        for row, raw_text in zip(rows, raw_texts, strict=True):
            row_number = int(row["row"])
            result.add_anchor(
                AnchorDraft(
                    key=f"row:{row_number}",
                    anchor_type=AnchorType.CSV_ROW,
                    display_location=f"{label} / row {row_number}",
                    structured_location={"row": row_number, "kind": "data"},
                    excerpt=raw_text,
                    evidence_kind=EvidenceKind.DIRECT,
                    source_label=label,
                )
            )
            values: list[str] = row["values"]
            for index, name in enumerate(column_names):
                if index >= len(values) or not values[index].strip():
                    continue
                populated_cells += 1
                if cell_anchors >= MAX_CELL_ANCHORS:
                    cells_truncated = True
                    continue
                cell_anchors += 1
                result.add_anchor(
                    AnchorDraft(
                        key=f"cell:{row_number}:{name}",
                        anchor_type=AnchorType.CSV_CELL,
                        display_location=f"{label} / row {row_number}, column '{name}'",
                        structured_location={
                            "row": row_number,
                            "column": name,
                            "column_index": index + 1,
                        },
                        excerpt=values[index],
                        evidence_kind=EvidenceKind.DIRECT,
                        source_label=label,
                    )
                )
        if cells_truncated:
            result.warn(
                WarningCode.TRUNCATED,
                f"Only the first {MAX_CELL_ANCHORS} populated cells were given a cell "
                f"anchor, out of {populated_cells}. Every row still has a row anchor.",
                location=label,
            )
        return populated_cells

    @staticmethod
    def _summary(
        content: dict[str, Any], *, populated_cells: int, ragged: int, truncated: bool
    ) -> dict[str, Any]:
        dialect: dict[str, Any] = content["dialect"]
        return {
            "row_count": len(content["rows"]),
            "column_count": content["column_count"],
            "columns": list(content["column_names"]),
            "header_row_number": content["header_row_number"],
            "delimiter": dialect["delimiter"],
            "encoding": dialect["encoding"],
            "skipped_line_count": len(content["skipped_lines"]),
            "ragged_row_count": ragged,
            "populated_cell_count": populated_cells,
            "truncated": truncated,
        }

    @staticmethod
    def _report_comments(result: ParseResult, skipped: list[dict[str, Any]], label: str) -> None:
        """Record comment lines as a capability boundary, not just as data.

        They are already in ``skipped_lines``, but "we chose not to interpret
        this" belongs where a reviewer looks for it.
        """
        comments = [entry for entry in skipped if entry["reason"] == REASON_COMMENT]
        if not comments:
            return
        first = comments[0]["row"]
        result.mark_unsupported(
            kind="csv_comment_line",
            detail=(
                f"{len(comments)} line(s) beginning with '{COMMENT_PREFIX}' were treated "
                "as comments and not parsed as data. Their text is in skipped_lines."
            ),
            location=f"{label} / row {first}",
        )
