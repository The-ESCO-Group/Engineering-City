"""Excel workbook (``.xlsx``) parsing.

A spreadsheet is read by an engineer as records: an interlock schedule is one
row per interlock, and a finding about it has to be able to say *cell C7 of sheet
Interlocks*. So this parser does three things.

* It records every populated cell with its A1 reference, its Excel type, and —
  when the cell holds a formula — the formula text.
* It finds the tabular regions (``candidate_tables``), preferring the range a
  real Excel table declares over anything inferred, and exposes each data row as
  a header -> value record keyed by its true spreadsheet row number.
* It reports what someone opening the file would *not* see: hidden sheets,
  hidden rows and columns, cell comments, merged regions where only the top-left
  cell holds the value, and formulas whose results are not available.

Formulas are never evaluated. The workbook is opened with ``data_only=False`` so
that the formula text is visible, and that choice means the values Excel cached
for those cells are not loaded: a cell holding ``=A1*2`` therefore has
``value: null``, a ``formula``, and a
:data:`~app.parsers.base.WarningCode.FORMULA_NOT_EVALUATED` warning saying the
cached result was not read. Reporting an unknown as unknown is the point — a
guessed number in an interlock threshold is far worse than no number. What makes
a cell a formula is Excel's own type for it, never the look of its text: a
string cell that happens to begin with ``=`` is a string, and recording it as an
unevaluated formula would turn a documented value into a null.

A cell Excel marks as an error (``data_type "e"``: ``#REF!``, ``#DIV/0!``,
``#N/A``) is Excel's own report that a calculation failed. Its marker text is
kept as the cell's value — it is what the file stores, and blanking it would
make a broken cell read like a field the schedule deliberately left empty — but
the same text typed into a cell by hand is an ordinary string, and a consumer
reading ``records`` cannot tell those two apart from the value alone. So each
record carries an ``errors`` map, keyed like ``values``, naming the columns
whose cell Excel flagged, and every such cell is named in a warning. Which
error it is, and what the failed formula was going to produce, are not
inferred: the marker is recorded, not read.

Nothing here interprets the sheet. A header called "Trip Condition" is recorded
as a string; whether ``AIR_PRESS_PSI < 60.0`` agrees with the logic is a
question for an agent, not for a parser.

Content shape (``ParseResult.content``)::

    workbook          {"properties": {"title", "creator", "created", "modified"},
                       "sheet_names": [str],
                       "hidden_sheet_names": [str],
                       "unread_sheet_names": [str],
                       "defined_names": [{"name", "scope", "value"}]}
    worksheets        [{"name", "state", "used_range", "max_row", "max_column",
                        "populated_cell_count",
                        "merged_ranges": [str],
                        "hidden_rows": [int], "hidden_columns": [str],
                        "comments": [{"ref", "author", "text"}],
                        "tables": [{"name", "ref"}],
                        "rows": [{"row": int, "cells": [cell]}]}]
    candidate_tables  [{"sheet", "table_name", "detection", "ref",
                        "header_row", "header_cells": [str], "headers": [str],
                        "record_keys": [str], "first_data_row", "last_data_row",
                        "column_count",
                        "records": [{"row", "ref", "values", "formulas",
                                     "errors"}]}]

    cell              {"ref", "column", "column_index", "value", "formula",
                       "data_type"}

Only populated cells and non-empty rows appear in ``worksheets[].rows``; an
absent cell is an empty cell. ``state`` is Excel's own sheet state
(``visible``, ``hidden``, ``veryHidden``). ``detection`` is ``named_table`` when
the range came from the workbook and ``heuristic`` when this parser inferred the
header row, so a consumer can tell a declared structure from a guessed one.
``first_data_row`` and ``last_data_row`` bound the declared data region; rows
inside it that hold nothing produce no record, so record row numbers — not the
region — say where data actually is. ``values`` is keyed by ``record_keys``,
which equals ``headers`` unless a header was blank or repeated.

A ``heuristic`` header row is a guess and is disclosed as one every time it is
made: the file gives no signal that separates a row of column names from a row
of text values, so the row this parser chose is named in a warning, its own
values are recorded as cells but not as a record, and every anchor that depends
on that choice carries ``detection: "heuristic"``. Populated rows that no table
covers are named in a warning too — a block of data that produced no records is
exactly the omission a reader would otherwise mistake for an empty region.

``unread_sheet_names`` lists tabs that exist in the workbook but are not
worksheets (chart sheets, dialog sheets); their content is not read, and each is
also recorded as an :class:`~app.parsers.base.UnsupportedItem`.
"""

from __future__ import annotations

import datetime as dt
import math
import zipfile
from dataclasses import dataclass
from typing import Any

import openpyxl
from openpyxl import load_workbook
from openpyxl.utils import get_column_letter, range_boundaries
from openpyxl.worksheet.formula import ArrayFormula, DataTableFormula

from app.core.enums import AnchorType, EvidenceKind, FileKind
from app.core.errors import ParserError
from app.parsers.base import AnchorDraft, ParseContext, ParseResult, WarningCode, truncate

#: A worksheet Excel shows. Anything else is hidden from the engineer and is
#: reported as such.
_STATE_VISIBLE = "visible"

#: How a candidate table's extent was arrived at.
_DETECTION_NAMED = "named_table"
_DETECTION_HEURISTIC = "heuristic"

#: Bounds. A legal sheet is 1,048,576 rows by 16,384 columns, and the parsed
#: document has to stay small enough to store and to hand to a model. Every
#: bound that actually bites is disclosed as a ``TRUNCATED`` warning rather than
#: quietly shortening the record.
MAX_ROWS_PER_SHEET = 5_000
#: Rows looked at, populated or not, before reading stops.
MAX_SCANNED_ROWS = 100_000
MAX_COLUMNS_PER_ROW = 256
MAX_CELL_CHARS = 2_000
MAX_ANCHORS = 20_000
#: Per warning code. One pathological workbook must not be able to bury the
#: other warnings under a million identical ones.
MAX_WARNINGS_PER_CODE = 50
#: Refs listed inline in a single warning message.
MAX_REFS_IN_MESSAGE = 10
#: Characters of one header cell quoted in a warning message. A warning has to
#: stay readable next to the other warnings, and the cells themselves are
#: recorded in full.
MAX_HEADER_CHARS_IN_MESSAGE = 60

#: Data rows rendered into a table anchor's excerpt, and rows rendered into a
#: worksheet anchor's excerpt. Both are read by a human checking a finding.
TABLE_EXCERPT_ROWS = 3
SHEET_EXCERPT_ROWS = 5

#: A header row needs at least this many populated cells...
MIN_HEADER_CELLS = 2
#: ...and at least this proportion of them must be text rather than numbers.
MIN_HEADER_TEXT_RATIO = 0.6

#: Name given to a column whose header cell is empty. Prefixed rather than
#: borrowed from the data: naming a column after one of its values would be an
#: invention, and the column still has to be addressable.
SYNTHETIC_HEADER_PREFIX = "Column "

#: An OLE2 container: a legacy ``.xls``, or an OOXML file encrypted with a
#: password. Detected up front so the failure names the real cause.
_OLE2_SIGNATURE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
#: Every ``.xlsx`` is a ZIP archive.
_ZIP_SIGNATURE = b"PK"

#: Excel's own type for a formula cell. Nothing else makes a cell a formula.
_TYPE_FORMULA = "f"
#: Excel's own type for an error cell (``#REF!``, ``#DIV/0!``, ``#N/A``, ...).
#: As with formulas, the type is what decides: a hand-typed "#REF!" is a string.
_TYPE_ERROR = "e"
#: openpyxl hands back a wrapper rather than a string for an array or data-table
#: formula; both are still formula cells and carry ``data_type == "f"``.
_FORMULA_WRAPPERS = (ArrayFormula, DataTableFormula)


# ---------------------------------------------------------------------------
# Scanned representation
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Cell:
    """One populated cell, already normalised to JSON-safe values."""

    ref: str
    row: int
    column: int
    column_letter: str
    value: Any
    formula: str | None
    data_type: str

    @property
    def text(self) -> str:
        """What a reader would see quoted: the formula, or the value as text."""
        if self.formula is not None:
            return self.formula
        return "" if self.value is None else str(self.value)

    @property
    def is_error(self) -> bool:
        """Whether Excel typed this cell as an error, rather than it looking like one."""
        return self.data_type == _TYPE_ERROR

    def as_dict(self) -> dict[str, Any]:
        return {
            "ref": self.ref,
            "column": self.column_letter,
            "column_index": self.column,
            "value": self.value,
            "formula": self.formula,
            "data_type": self.data_type,
        }


@dataclass(frozen=True, slots=True)
class _Row:
    row: int
    cells: tuple[_Cell, ...]

    @property
    def by_column(self) -> dict[int, _Cell]:
        return {cell.column: cell for cell in self.cells}

    def as_dict(self) -> dict[str, Any]:
        return {"row": self.row, "cells": [cell.as_dict() for cell in self.cells]}


@dataclass(frozen=True, slots=True)
class _SheetScan:
    """Everything read from one worksheet, so the workbook can then be closed."""

    name: str
    state: str
    used_range: str
    max_row: int
    max_column: int
    merged_ranges: tuple[str, ...]
    #: Rows and columns Excel does not draw. Only ones holding content are listed.
    hidden_rows: tuple[int, ...]
    hidden_columns: tuple[str, ...]
    #: ``(ref, author, text)`` for every cell comment, in cell order.
    comments: tuple[tuple[str, str, str], ...]
    #: ``(table name, ref)`` sorted by name.
    tables: tuple[tuple[str, str], ...]
    #: Table name -> ``(header row count, totals row count)``.
    table_shape: dict[str, tuple[int, int]]
    rows: tuple[_Row, ...]
    formula_refs: tuple[str, ...]
    #: ``(ref, marker text)`` for every cell Excel typed as an error.
    error_refs: tuple[tuple[str, str], ...]
    shortened_refs: tuple[str, ...]
    #: ``(ref, python type name)`` for values whose type was not recognised.
    unrecognised_values: tuple[tuple[str, str], ...]
    row_limit_reached: bool
    column_limit_reached: bool

    @property
    def location(self) -> str:
        return f"Sheet '{self.name}'"

    @property
    def rows_by_index(self) -> dict[int, _Row]:
        return {row.row: row for row in self.rows}

    @property
    def populated_cell_count(self) -> int:
        return sum(len(row.cells) for row in self.rows)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "state": self.state,
            "used_range": self.used_range,
            "max_row": self.max_row,
            "max_column": self.max_column,
            "populated_cell_count": self.populated_cell_count,
            "merged_ranges": list(self.merged_ranges),
            "hidden_rows": list(self.hidden_rows),
            "hidden_columns": list(self.hidden_columns),
            "comments": [
                {"ref": ref, "author": author, "text": text} for ref, author, text in self.comments
            ],
            "tables": [{"name": name, "ref": ref} for name, ref in self.tables],
            "rows": [row.as_dict() for row in self.rows],
        }


# ---------------------------------------------------------------------------
# Reading the workbook
# ---------------------------------------------------------------------------


def _iso_or_none(value: Any) -> str | None:
    if isinstance(value, dt.datetime | dt.date | dt.time):
        return value.isoformat()
    return None if value is None else str(value)


def _text_or_none(value: Any) -> str | None:
    return None if value is None else str(value)


def _read_properties(workbook: Any) -> dict[str, Any]:
    properties = workbook.properties
    return {
        "title": _text_or_none(getattr(properties, "title", None)),
        "creator": _text_or_none(getattr(properties, "creator", None)),
        "created": _iso_or_none(getattr(properties, "created", None)),
        "modified": _iso_or_none(getattr(properties, "modified", None)),
    }


def _defined_name(name: str, entry: Any, scope: str) -> dict[str, Any]:
    value = getattr(entry, "attr_text", None)
    return {"name": str(name), "scope": scope, "value": "" if value is None else str(value)}


def _read_defined_names(workbook: Any) -> list[dict[str, Any]]:
    """Workbook-scoped names first, then each sheet's own, each sorted by name."""
    names = [
        _defined_name(name, workbook.defined_names[name], "")
        for name in sorted(workbook.defined_names)
    ]
    for sheet in workbook.worksheets:
        local = getattr(sheet, "defined_names", None) or {}
        names.extend(_defined_name(name, local[name], str(sheet.title)) for name in sorted(local))
    return names


def _unread_sheets(workbook: Any) -> tuple[tuple[str, str, str], ...]:
    """``(name, kind, state)`` for every tab that is not a worksheet.

    ``workbook.worksheets`` excludes chart sheets and dialog sheets, so iterating
    it alone drops those tabs from the record entirely — a workbook whose only
    tab is a chart sheet would otherwise be reported as having no sheets at all.
    They are named here in the order Excel shows them.
    """
    read = {str(sheet.title) for sheet in workbook.worksheets}
    unread: list[tuple[str, str, str]] = []
    for name in workbook.sheetnames:
        if str(name) in read:
            continue
        sheet = workbook[name]
        unread.append(
            (str(name), type(sheet).__name__, str(getattr(sheet, "sheet_state", "unknown")))
        )
    return tuple(unread)


def _boundaries(ref: str) -> tuple[int, int, int, int] | None:
    """``"A4:H10"`` -> ``(min_col, min_row, max_col, max_row)``, or ``None``."""
    try:
        bounds = range_boundaries(ref)
    except (ValueError, TypeError):
        return None
    if bounds is None or any(bound is None for bound in bounds):
        return None
    min_col, min_row, max_col, max_row = (int(bound) for bound in bounds)
    return min_col, min_row, max_col, max_row


def _range_sort_key(ref: str) -> tuple[int, int, str]:
    bounds = _boundaries(ref)
    if bounds is None:
        return (2**31, 2**31, ref)
    min_col, min_row, _, _ = bounds
    return (min_row, min_col, ref)


def _formula_text(value: Any) -> str:
    """Formula text, including from the array-formula wrapper openpyxl returns."""
    if isinstance(value, str):
        return value
    text = getattr(value, "text", None)
    return text if isinstance(text, str) else str(value)


def _read_cell(raw: Any) -> tuple[_Cell, str | None]:
    """Normalise one cell. The second element names an unrecognised value type.

    A cell is a formula when Excel says it is — ``data_type == "f"``, or one of
    openpyxl's formula wrappers. Text that merely *looks* like a formula is text:
    sniffing for a leading ``=`` would record ``"=60 psi (per P&ID)"`` as an
    unevaluated formula with ``value: null``, making a documented threshold
    indistinguishable from a blank field and asserting a formula the file does
    not contain.
    """
    data_type = str(raw.data_type)
    raw_value = raw.value
    formula: str | None = None
    value: Any = None
    unrecognised: str | None = None

    if data_type == _TYPE_FORMULA or isinstance(raw_value, _FORMULA_WRAPPERS):
        formula = _formula_text(raw_value)
    elif isinstance(raw_value, bool | int | str):
        value = raw_value
    elif isinstance(raw_value, float):
        # A non-finite float has no JSON representation; keep it as the text it
        # printed as rather than dropping the cell.
        value = raw_value if math.isfinite(raw_value) else str(raw_value)
    elif isinstance(raw_value, dt.datetime | dt.date | dt.time):
        value = raw_value.isoformat()
    elif isinstance(raw_value, dt.timedelta):
        value = str(raw_value)
    else:
        # Recorded as text so the content is never lost, and reported so that
        # "we rendered this with str()" is visible instead of implicit.
        value = str(raw_value)
        unrecognised = type(raw_value).__name__

    shortened = False
    if formula is not None and len(formula) > MAX_CELL_CHARS:
        formula = formula[: MAX_CELL_CHARS - 3] + "..."
        shortened = True
    elif isinstance(value, str) and len(value) > MAX_CELL_CHARS:
        value = value[: MAX_CELL_CHARS - 3] + "..."
        shortened = True

    cell = _Cell(
        ref=str(raw.coordinate),
        row=int(raw.row),
        column=int(raw.column),
        column_letter=str(raw.column_letter),
        value=value,
        formula=formula,
        data_type=data_type,
    )
    return cell, ("__shortened__" if shortened else unrecognised)


def _comment_of(raw: Any) -> tuple[str, str] | None:
    """``(author, text)`` of a cell's note, shortened like any other long value."""
    comment = getattr(raw, "comment", None)
    if comment is None:
        return None
    text = "" if comment.text is None else str(comment.text)
    if len(text) > MAX_CELL_CHARS:
        text = text[: MAX_CELL_CHARS - 3] + "..."
    author = "" if comment.author is None else str(comment.author)
    return author, text


def _hidden_columns(sheet: Any, populated: set[int]) -> tuple[str, ...]:
    """Letters of hidden columns that hold content, expanding grouped ranges.

    A hidden column with nothing in it is a layout detail; a hidden column with
    values in it is content the engineer opening the workbook does not see.
    """
    columns: set[int] = set()
    dimensions = getattr(sheet, "column_dimensions", None) or {}
    for dimension in dimensions.values():
        if not getattr(dimension, "hidden", False):
            continue
        first = getattr(dimension, "min", None)
        last = getattr(dimension, "max", None)
        if first is None or last is None:
            continue
        columns.update(column for column in range(int(first), int(last) + 1) if column in populated)
    return tuple(get_column_letter(column) for column in sorted(columns))


def _hidden_rows(sheet: Any, populated: set[int]) -> tuple[int, ...]:
    dimensions = getattr(sheet, "row_dimensions", None) or {}
    return tuple(
        sorted(
            int(number)
            for number, dimension in dimensions.items()
            if getattr(dimension, "hidden", False) and int(number) in populated
        )
    )


def _scan_sheet(sheet: Any) -> _SheetScan:
    """Read one worksheet into plain data, applying the row and column bounds."""
    rows: list[_Row] = []
    formula_refs: list[str] = []
    error_refs: list[tuple[str, str]] = []
    shortened_refs: list[str] = []
    unrecognised: list[tuple[str, str]] = []
    comments: list[tuple[str, str, str]] = []

    # ``max_row``/``max_column`` are computed from the cells that are actually
    # stored, so the window below cannot be widened by a lying dimension record.
    # Iterating a window rather than filtering afterwards matters: asking
    # openpyxl for a million rows by sixteen thousand columns materialises every
    # empty cell in between.
    max_row = int(sheet.max_row)
    max_column = int(sheet.max_column)
    scan_rows = min(max_row, MAX_SCANNED_ROWS)
    scan_columns = min(max_column, MAX_COLUMNS_PER_ROW)
    row_limit_reached = max_row > scan_rows
    column_limit_reached = max_column > scan_columns

    for raw_row in sheet.iter_rows(min_row=1, max_row=scan_rows, min_col=1, max_col=scan_columns):
        if len(rows) >= MAX_ROWS_PER_SHEET:
            row_limit_reached = True
            break
        cells: list[_Cell] = []
        for raw_cell in raw_row:
            comment = _comment_of(raw_cell)
            if comment is not None:
                # Read before the emptiness test: a comment on an otherwise
                # blank cell is still content someone wrote into the file.
                comments.append((str(raw_cell.coordinate), comment[0], comment[1]))
            if raw_cell.value is None:
                continue
            cell, note = _read_cell(raw_cell)
            cells.append(cell)
            if cell.formula is not None:
                formula_refs.append(cell.ref)
            if cell.is_error:
                error_refs.append((cell.ref, cell.text))
            if note == "__shortened__":
                shortened_refs.append(cell.ref)
            elif note is not None:
                unrecognised.append((cell.ref, note))
        if cells:
            rows.append(_Row(row=cells[0].row, cells=tuple(cells)))

    tables: list[tuple[str, str]] = []
    table_shape: dict[str, tuple[int, int]] = {}
    for name in sorted(sheet.tables):
        table = sheet.tables[name]
        tables.append((str(name), str(table.ref)))
        header_rows = 1 if table.headerRowCount is None else int(table.headerRowCount)
        totals_rows = 0 if table.totalsRowCount is None else int(table.totalsRowCount)
        table_shape[str(name)] = (header_rows, totals_rows)

    return _SheetScan(
        name=str(sheet.title),
        state=str(sheet.sheet_state),
        used_range=str(sheet.dimensions),
        max_row=int(sheet.max_row),
        max_column=int(sheet.max_column),
        merged_ranges=tuple(
            sorted((str(rng) for rng in sheet.merged_cells.ranges), key=_range_sort_key)
        ),
        hidden_rows=_hidden_rows(sheet, {row.row for row in rows}),
        hidden_columns=_hidden_columns(sheet, {cell.column for row in rows for cell in row.cells}),
        comments=tuple(comments),
        tables=tuple(tables),
        table_shape=table_shape,
        rows=tuple(rows),
        formula_refs=tuple(formula_refs),
        error_refs=tuple(error_refs),
        shortened_refs=tuple(shortened_refs),
        unrecognised_values=tuple(unrecognised),
        row_limit_reached=row_limit_reached,
        column_limit_reached=column_limit_reached,
    )


# ---------------------------------------------------------------------------
# Table detection helpers
# ---------------------------------------------------------------------------


def _is_header_row(row: _Row) -> bool:
    """Whether this row looks like column names rather than data.

    A heuristic, and labelled as one wherever its result is used: a header row
    is mostly text, and a table's data rows usually are not. It cannot tell a
    header from the first row of a headerless list of names, which is why a
    declared Excel table always wins.
    """
    if len(row.cells) < MIN_HEADER_CELLS:
        return False
    text_cells = [
        cell
        for cell in row.cells
        if cell.formula is None and isinstance(cell.value, str) and cell.value.strip()
    ]
    if len(text_cells) < MIN_HEADER_CELLS:
        return False
    return len(text_cells) / len(row.cells) >= MIN_HEADER_TEXT_RATIO


def _record_keys(headers: list[str], columns: list[int]) -> list[str]:
    """Unique per-column keys for the record dicts, preserving header order."""
    seen: dict[str, int] = {}
    keys: list[str] = []
    for header in headers:
        count = seen.get(header, 0) + 1
        seen[header] = count
        keys.append(header if count == 1 else f"{header} ({count})")
    if len(set(keys)) == len(keys):
        return keys
    # Suffixing collided too (a sheet already contains "Notes (2)"); column
    # letters are unique by construction.
    return [
        f"{header} [{get_column_letter(column)}]"
        for header, column in zip(headers, columns, strict=True)
    ]


def _row_line(row: _Row | None, min_col: int, max_col: int) -> str:
    """One row rendered across a fixed column span, so values stay aligned."""
    by_column: dict[int, _Cell] = {} if row is None else row.by_column
    parts: list[str] = []
    for column in range(min_col, max_col + 1):
        cell = by_column.get(column)
        parts.append("" if cell is None else cell.text)
    return " | ".join(parts)


def _listed_refs(refs: list[str]) -> str:
    if len(refs) <= MAX_REFS_IN_MESSAGE:
        return ", ".join(refs)
    shown = ", ".join(refs[:MAX_REFS_IN_MESSAGE])
    return f"{shown} and {len(refs) - MAX_REFS_IN_MESSAGE} more"


# ---------------------------------------------------------------------------
# Bounded warnings and anchors
# ---------------------------------------------------------------------------


class _WarningBudget:
    """Caps repeated warnings, and reports how many it suppressed.

    Without a cap, a workbook of a million formula cells produces a million
    warnings and the review record becomes unreadable — which hides the other
    warnings just as effectively as omitting them would.
    """

    def __init__(self, result: ParseResult, limit: int = MAX_WARNINGS_PER_CODE) -> None:
        self._result = result
        self._limit = limit
        self._emitted: dict[str, int] = {}
        self._suppressed: dict[str, int] = {}

    def warn(self, code: str, message: str, location: str = "", severity: str = "warning") -> None:
        emitted = self._emitted.get(code, 0)
        if emitted >= self._limit:
            self._suppressed[code] = self._suppressed.get(code, 0) + 1
            return
        self._emitted[code] = emitted + 1
        self._result.warn(code, message, location=location, severity=severity)

    def flush(self) -> None:
        for code in sorted(self._suppressed):
            self._result.warn(
                code,
                f"A further {self._suppressed[code]} '{code}' warning(s) were suppressed "
                f"after the first {self._limit}. The content they refer to is still "
                f"recorded in full.",
                location="",
            )


class _AnchorSink:
    """Collects anchors: deduplicates keys, stamps the label, bounds the total."""

    def __init__(self, result: ParseResult, label: str) -> None:
        self._result = result
        self._label = label
        self._keys: set[str] = set()
        self._reported_limit = False

    def add(
        self,
        *,
        key: str,
        anchor_type: AnchorType,
        location: str,
        structured: dict[str, Any],
        excerpt: str,
        evidence_kind: EvidenceKind = EvidenceKind.DIRECT,
    ) -> None:
        if key in self._keys:
            return
        if len(self._result.anchors) >= MAX_ANCHORS:
            if not self._reported_limit:
                self._reported_limit = True
                self._result.warn(
                    WarningCode.TRUNCATED,
                    f"Only the first {MAX_ANCHORS} anchors were created, so cells from "
                    f"here on cannot be cited individually. Every cell is still recorded "
                    f"in the parsed content.",
                    location=location,
                )
            return
        self._keys.add(key)
        self._result.add_anchor(
            AnchorDraft(
                key=key,
                anchor_type=anchor_type,
                display_location=f"{self._label} / {location}" if location else self._label,
                structured_location=structured,
                excerpt=excerpt,
                evidence_kind=evidence_kind,
                source_label=self._label,
            )
        )


# ---------------------------------------------------------------------------
# The parser
# ---------------------------------------------------------------------------


class XlsxParser:
    """Records the sheets, cells and tabular regions of a workbook."""

    name: str = "xlsx"
    #: 1.1.0 — anchors carry the table's ``detection``, heuristic headers and
    #: uncovered row blocks are disclosed, and a string beginning with ``=`` is
    #: no longer recorded as a formula.
    #: 1.2.0 — a cell Excel typed as an error is named in a warning and listed in
    #: the record's ``errors``, so it is no longer indistinguishable from text.
    version: str = "1.2.0"
    handles: tuple[FileKind, ...] = (FileKind.XLSX,)

    def parse(self, context: ParseContext) -> ParseResult:
        result = ParseResult(
            artifact_kind=FileKind.XLSX.value,
            parser_name=self.name,
            parser_version=self.version,
        )
        budget = _WarningBudget(result)

        workbook = self._open(context)
        try:
            properties = _read_properties(workbook)
            defined_names = _read_defined_names(workbook)
            scans = tuple(_scan_sheet(sheet) for sheet in workbook.worksheets)
            unread = _unread_sheets(workbook)
        finally:
            # Releases the underlying archive handle; nothing below touches the
            # workbook object again.
            workbook.close()

        worksheets: list[dict[str, Any]] = []
        candidate_tables: list[dict[str, Any]] = []
        result.content = {
            "workbook": {
                "properties": properties,
                "sheet_names": [scan.name for scan in scans],
                "hidden_sheet_names": [scan.name for scan in scans if scan.state != _STATE_VISIBLE],
                "unread_sheet_names": [name for name, _, _ in unread],
                "defined_names": defined_names,
            },
            "worksheets": worksheets,
            "candidate_tables": candidate_tables,
        }

        self._report_workbook(result, context, scans, unread)
        for scan in scans:
            self._report_sheet(result, budget, scan)
            worksheets.append(scan.as_dict())
            candidate_tables.extend(self._candidate_tables(result, budget, scan))

        self._add_anchors(result, context, scans, candidate_tables)
        budget.flush()
        result.summary = self._summarise(result, scans, candidate_tables)
        return result

    # -- reading ------------------------------------------------------------

    def _open(self, context: ParseContext) -> Any:
        """Open the workbook, or fail with a ParserError that says why.

        openpyxl signals a file that is not a workbook by raising whatever the
        zip layer raised — ``BadZipFile``, ``KeyError`` — which is not something
        a caller can report to an engineer, so every failure is translated here.
        Only the stored filename is ever interpolated into the message: an
        openpyxl error can carry the absolute path on this server, and that
        message goes back to the client verbatim.
        """
        self._require_xml_defences(context)
        self._check_package_bounds(context)
        try:
            with context.path.open("rb") as handle:
                head = handle.read(len(_OLE2_SIGNATURE))
        except OSError as exc:
            # Only the stored filename travels out: the path on disk is an
            # internal detail.
            raise ParserError(
                f"Cannot read workbook '{context.filename}': {exc.strerror or type(exc).__name__}",
                details={"filename": context.filename},
            ) from exc

        if not head:
            raise ParserError(
                f"Workbook '{context.filename}' is empty (0 bytes), so it holds no "
                f"worksheets to review.",
                details={"filename": context.filename},
            )
        if head.startswith(_OLE2_SIGNATURE):
            raise ParserError(
                f"Workbook '{context.filename}' is an OLE2 container, not an .xlsx "
                f"package. That is either a legacy .xls file or a password-protected "
                f"workbook; re-save it as .xlsx without protection.",
                details={"filename": context.filename, "signature": "ole2"},
            )
        if not head.startswith(_ZIP_SIGNATURE):
            raise ParserError(
                f"Workbook '{context.filename}' is not an .xlsx package: it does not "
                f"begin with a ZIP header, so it is a different format or is corrupt.",
                details={"filename": context.filename},
            )

        try:
            return load_workbook(context.path, read_only=False, data_only=False)
        except Exception as exc:
            # Deliberately broad: openpyxl raises whatever the zip and XML
            # layers raise, and none of those are reportable as they stand. The
            # exception's own text is deliberately not interpolated — openpyxl's
            # "could not read worksheets from <path>" would put the server's
            # absolute path in a client-visible message.
            raise ParserError(
                f"Workbook '{context.filename}' could not be opened as an .xlsx "
                f"package; the failure was a {type(exc).__name__} from the zip or XML "
                f"layer, so the file is corrupt or is not a workbook.",
                details={"filename": context.filename, "error": type(exc).__name__},
            ) from exc

    def _require_xml_defences(self, context: ParseContext) -> None:
        """Refuse to parse when openpyxl's XML hardening is not in force.

        Every other XML path in this codebase goes through
        :mod:`app.security.xml_safety`; ``load_workbook`` does not, so an .xlsx
        with a nested-entity DOCTYPE is refused only because openpyxl found
        ``defusedxml`` installed and the ``OPENPYXL_DEFUSEDXML`` environment
        variable did not turn it off. That guarantee lives in a dependency and
        an environment variable, so it is asserted here rather than assumed:
        without it an entity bomb expands inside the worker.
        """
        if getattr(openpyxl, "DEFUSEDXML", False):
            return
        raise ParserError(
            f"Workbook '{context.filename}' was not parsed: openpyxl's XML entity "
            f"defences are not active in this environment, and an .xlsx is parsed as "
            f"XML. Install 'defusedxml' and leave OPENPYXL_DEFUSEDXML unset.",
            details={"filename": context.filename, "error": "xml_defences_disabled"},
        )

    def _check_package_bounds(self, context: ParseContext) -> None:
        """Reject a package that expands far beyond its size on disk.

        openpyxl materialises the whole workbook before any bound in this module
        applies, so the amplification limits have to be read off the ZIP central
        directory first — the same properties :mod:`app.security.archive`
        enforces on an uploaded .zip, using the same settings, applied to the ZIP
        that every .xlsx is.
        """
        settings = context.settings
        try:
            with zipfile.ZipFile(context.path) as package:
                infos = package.infolist()
        except (zipfile.BadZipFile, OSError):
            # Not readable as a ZIP at all: load_workbook below reports that in
            # the one message this parser has for it.
            return

        uncompressed = sum(info.file_size for info in infos)
        compressed = sum(info.compress_size for info in infos)
        largest = max((info.file_size for info in infos), default=0)

        if uncompressed > settings.max_archive_uncompressed_bytes:
            raise ParserError(
                f"Workbook '{context.filename}' expands to {uncompressed:,} bytes, above "
                f"the {settings.max_archive_uncompressed_bytes:,} limit, so it was not "
                f"opened.",
                details={"filename": context.filename, "uncompressed_bytes": uncompressed},
            )
        if largest > settings.max_xml_bytes:
            raise ParserError(
                f"Workbook '{context.filename}' contains a part of {largest:,} bytes, "
                f"above the {settings.max_xml_bytes:,} limit for a single XML document, "
                f"so it was not opened.",
                details={"filename": context.filename, "part_bytes": largest},
            )
        if compressed > 0:
            ratio = uncompressed / compressed
            if ratio > settings.max_archive_compression_ratio:
                raise ParserError(
                    f"Workbook '{context.filename}' expands {ratio:.0f}:1, above the "
                    f"{settings.max_archive_compression_ratio:.0f}:1 limit. That is "
                    f"characteristic of a zip bomb, so it was not opened.",
                    details={"filename": context.filename, "ratio": round(ratio, 1)},
                )

    # -- warnings -----------------------------------------------------------

    def _report_workbook(
        self,
        result: ParseResult,
        context: ParseContext,
        scans: tuple[_SheetScan, ...],
        unread: tuple[tuple[str, str, str], ...],
    ) -> None:
        for name, kind, state in unread:
            result.mark_unsupported(
                "non_worksheet_tab",
                f"Tab '{name}' is a {kind}, not a worksheet, and this parser reads only "
                f"worksheets; nothing on it was read. Its sheet state is '{state}'.",
                location=f"Sheet '{name}'",
            )
        if any(scan.rows for scan in scans):
            return
        sheets = len(scans)
        if unread:
            # "Nothing to review" would be a conclusion about content this parser
            # never looked at.
            result.warn(
                WarningCode.EMPTY_DOCUMENT,
                f"None of the workbook's {sheets} worksheet(s) hold a populated cell. It "
                f"also has {len(unread)} tab(s) that are not worksheets "
                f"({', '.join(name for name, _, _ in unread)}), which were not read, so "
                f"whether there is anything to review on those is not established here.",
                location=context.source_label,
            )
            return
        result.warn(
            WarningCode.EMPTY_DOCUMENT,
            f"The workbook has {sheets} worksheet(s) but no populated cells, so there "
            f"is nothing in it to review.",
            location=context.source_label,
        )

    def _report_sheet(self, result: ParseResult, budget: _WarningBudget, scan: _SheetScan) -> None:
        if scan.state != _STATE_VISIBLE:
            result.warn(
                WarningCode.HIDDEN_CONTENT,
                f"Sheet '{scan.name}' is {scan.state} and is not visible when the "
                f"workbook is opened. Its {scan.populated_cell_count} populated cell(s) "
                f"were read and are included below.",
                location=scan.location,
            )
        if scan.hidden_rows or scan.hidden_columns:
            parts: list[str] = []
            if scan.hidden_rows:
                parts.append(f"row(s) {_listed_refs([str(row) for row in scan.hidden_rows])}")
            if scan.hidden_columns:
                parts.append(f"column(s) {_listed_refs(list(scan.hidden_columns))}")
            result.warn(
                WarningCode.HIDDEN_CONTENT,
                f"Sheet '{scan.name}' hides populated {' and '.join(parts)}; that content "
                f"is not visible when the workbook is opened. It was read and is included "
                f"below, so a hidden row reads like a live one unless this warning is "
                f"taken into account.",
                location=scan.location,
            )
        if scan.comments:
            result.warn(
                WarningCode.HIDDEN_CONTENT,
                f"Sheet '{scan.name}' carries {len(scan.comments)} cell comment(s) "
                f"({_listed_refs([ref for ref, _, _ in scan.comments])}). A comment is not "
                f"part of the cell's value and is not visible without hovering; the text "
                f"is recorded under this sheet's 'comments'.",
                location=scan.location,
            )
        if scan.merged_ranges:
            result.warn(
                WarningCode.MERGED_CELLS,
                f"Sheet '{scan.name}' has {len(scan.merged_ranges)} merged region(s) "
                f"({_listed_refs(list(scan.merged_ranges))}). Only the top-left cell of a "
                f"merged region holds the value, so a value shown across several columns "
                f"is recorded against that one cell.",
                location=scan.location,
            )
        if scan.row_limit_reached:
            budget.warn(
                WarningCode.TRUNCATED,
                f"Not every row of sheet '{scan.name}' was read: reading stops at "
                f"{MAX_ROWS_PER_SHEET} populated rows or row {MAX_SCANNED_ROWS}, "
                f"whichever comes first, and this sheet reaches row {scan.max_row}. "
                f"Later rows are not in this record at all.",
                location=scan.location,
            )
        if scan.column_limit_reached:
            budget.warn(
                WarningCode.TRUNCATED,
                f"Sheet '{scan.name}' has cells beyond column "
                f"{get_column_letter(MAX_COLUMNS_PER_ROW)} (out to column "
                f"{get_column_letter(scan.max_column)}); those columns were not read.",
                location=scan.location,
            )
        for ref in scan.shortened_refs:
            budget.warn(
                WarningCode.TRUNCATED,
                f"Cell {ref} holds more than {MAX_CELL_CHARS} characters; the recorded "
                f"value is shortened and ends with an ellipsis.",
                location=f"{scan.location}, cell {ref}",
            )
        for ref, type_name in scan.unrecognised_values:
            result.mark_unsupported(
                "cell_value_type",
                f"Cell {ref} holds a value of type '{type_name}', which this parser does "
                f"not model; it was recorded as text.",
                location=f"{scan.location}, cell {ref}",
            )
        for ref, marker in scan.error_refs:
            budget.warn(
                WarningCode.STRUCTURE_UNRECOGNISED,
                f"Cell {ref} is typed by Excel as an error cell and holds the marker "
                f"'{truncate(marker, MAX_HEADER_CHARS_IN_MESSAGE)}'. That is Excel "
                f"reporting a calculation that failed, not a value the workbook states, "
                f"so the marker is recorded as the cell's value and the column is named "
                f"under the record's 'errors'. What the cell was meant to hold is not "
                f"established here.",
                location=f"{scan.location}, cell {ref}",
            )
        for ref in scan.formula_refs:
            budget.warn(
                WarningCode.FORMULA_NOT_EVALUATED,
                f"Cell {ref} holds a formula. The workbook was read with the formula "
                f"text rather than the value Excel cached for it, so the result of this "
                f"cell is unknown and is recorded as null.",
                location=f"{scan.location}, cell {ref}",
            )

    # -- candidate tables ---------------------------------------------------

    def _candidate_tables(
        self, result: ParseResult, budget: _WarningBudget, scan: _SheetScan
    ) -> list[dict[str, Any]]:
        tables: list[dict[str, Any]] = []
        claimed: set[int] = set()

        for name, ref in scan.tables:
            bounds = _boundaries(ref)
            if bounds is None:
                budget.warn(
                    WarningCode.STRUCTURE_UNRECOGNISED,
                    f"Table '{name}' on sheet '{scan.name}' declares the range '{ref}', "
                    f"which could not be read, so its rows were not turned into records.",
                    location=scan.location,
                )
                continue
            min_col, min_row, max_col, max_row = bounds
            claimed.update(range(min_row, max_row + 1))
            header_rows, totals_rows = scan.table_shape.get(name, (1, 0))
            where = f"{scan.location}, table '{name}' ({ref})"

            if header_rows < 1:
                budget.warn(
                    WarningCode.STRUCTURE_UNRECOGNISED,
                    f"Table '{name}' on sheet '{scan.name}' declares no header row, so "
                    f"its columns have no names and its rows were not turned into "
                    f"records. The cells are still recorded.",
                    location=where,
                )
                continue
            if header_rows > 1:
                result.mark_unsupported(
                    "multi_row_table_header",
                    f"Table '{name}' declares {header_rows} header rows; only row "
                    f"{min_row} was used to name the columns.",
                    location=where,
                )
            if totals_rows:
                result.mark_unsupported(
                    "table_totals_row",
                    f"Table '{name}' declares {totals_rows} totals row(s) at the bottom "
                    f"of {ref}; they are recorded as cells but not as records.",
                    location=where,
                )

            tables.append(
                self._build_table(
                    budget,
                    scan,
                    table_name=name,
                    detection=_DETECTION_NAMED,
                    header_row=min_row,
                    first_data_row=min_row + header_rows,
                    last_data_row=max_row - totals_rows,
                    min_col=min_col,
                    max_col=max_col,
                )
            )

        covered = set(claimed)
        for table in self._heuristic_tables(budget, scan, claimed):
            tables.append(table)
            bounds = _boundaries(str(table["ref"]))
            if bounds is not None:
                _, first_row, _, last_row = bounds
                covered.update(range(first_row, last_row + 1))

        self._report_uncovered_rows(budget, scan, covered)
        return tables

    def _heuristic_tables(
        self, budget: _WarningBudget, scan: _SheetScan, claimed: set[int]
    ) -> list[dict[str, Any]]:
        """Find header-plus-data blocks in the rows no declared table covers.

        A block ends at the first blank row: a gap is the one signal a
        spreadsheet reliably gives that the next thing is a different thing.

        Which row of such a block is the header is a guess and cannot be
        anything else: a row of column names and a row of text values look the
        same. So every table found here is disclosed — the chosen row is named,
        and the fact that its own values became column names rather than a
        record is stated, because a reader enumerating records would otherwise
        never learn that row is missing from them.
        """
        rows = [row for row in scan.rows if row.row not in claimed]
        tables: list[dict[str, Any]] = []
        index = 0
        while index < len(rows):
            header = rows[index]
            if not _is_header_row(header):
                index += 1
                continue
            cursor = index + 1
            expected = header.row + 1
            while cursor < len(rows) and rows[cursor].row == expected:
                cursor += 1
                expected += 1
            if cursor == index + 1:
                index += 1
                continue
            columns = [cell.column for cell in header.cells]
            tables.append(
                self._build_table(
                    budget,
                    scan,
                    table_name="",
                    detection=_DETECTION_HEURISTIC,
                    header_row=header.row,
                    first_data_row=header.row + 1,
                    last_data_row=rows[cursor - 1].row,
                    min_col=min(columns),
                    max_col=max(columns),
                )
            )
            values = _listed_refs(
                [truncate(cell.text.strip(), MAX_HEADER_CHARS_IN_MESSAGE) for cell in header.cells]
            )
            budget.warn(
                WarningCode.STRUCTURE_UNRECOGNISED,
                f"Row {header.row} on sheet '{scan.name}' was read as the header row of "
                f"rows {header.row}-{rows[cursor - 1].row}; the workbook declares no table "
                f"there, so this is an inference from the row being mostly text. Its own "
                f"values ({values}) name the columns and are not a record of their own. "
                f"The table is marked detection 'heuristic'.",
                location=f"{scan.location}, row {header.row}",
                severity="info",
            )
            index = cursor
        return tables

    def _report_uncovered_rows(
        self, budget: _WarningBudget, scan: _SheetScan, covered: set[int]
    ) -> None:
        """Name every populated row block that no table on this sheet covers.

        A block that yielded no records is invisible to anything reading
        ``records``, whether the sheet produced no tables at all or produced one
        and left this block out — so both are reported the same way.
        """
        numbers = [row.row for row in scan.rows if row.row not in covered]
        if not numbers:
            return
        #: ``(first row, last row, populated rows in between)``.
        blocks: list[tuple[int, int, int]] = []
        for number in numbers:
            if blocks and number == blocks[-1][1] + 1:
                blocks[-1] = (blocks[-1][0], number, blocks[-1][2] + 1)
            else:
                blocks.append((number, number, 1))
        for first, last, count in blocks:
            span = f"row {first}" if first == last else f"rows {first}-{last}"
            budget.warn(
                WarningCode.STRUCTURE_UNRECOGNISED,
                f"No header row could be identified for {span} on sheet '{scan.name}', "
                f"so those {count} populated row(s) are recorded as cells only and no "
                f"records were derived from them.",
                location=scan.location,
            )

    def _build_table(
        self,
        budget: _WarningBudget,
        scan: _SheetScan,
        *,
        table_name: str,
        detection: str,
        header_row: int,
        first_data_row: int,
        last_data_row: int,
        min_col: int,
        max_col: int,
    ) -> dict[str, Any]:
        by_row = scan.rows_by_index
        header = by_row.get(header_row)
        header_by_column = {} if header is None else header.by_column
        columns = list(range(min_col, max_col + 1))
        first_letter = get_column_letter(min_col)
        last_letter = get_column_letter(max_col)

        headers: list[str] = []
        for column in columns:
            letter = get_column_letter(column)
            cell = header_by_column.get(column)
            if cell is not None and cell.text.strip():
                headers.append(cell.text.strip())
                continue
            headers.append(f"{SYNTHETIC_HEADER_PREFIX}{letter}")
            budget.warn(
                WarningCode.STRUCTURE_UNRECOGNISED,
                f"Column {letter} of the table starting at row {header_row} on sheet "
                f"'{scan.name}' has no header text; it is recorded as "
                f"'{headers[-1]}' rather than being named from its data.",
                location=f"{scan.location}, cell {letter}{header_row}",
            )

        keys = _record_keys(headers, columns)
        if keys != headers:
            budget.warn(
                WarningCode.STRUCTURE_UNRECOGNISED,
                f"Row {header_row} on sheet '{scan.name}' repeats a column name, so "
                f"record keys were made unique ({', '.join(keys)}). The header text is "
                f"recorded unchanged in 'headers'.",
                location=f"{scan.location}, row {header_row}",
            )

        records: list[dict[str, Any]] = []
        for number in range(first_data_row, last_data_row + 1):
            row = by_row.get(number)
            if row is None:
                continue
            by_column = row.by_column
            values: dict[str, Any] = {}
            formulas: dict[str, str] = {}
            errors: dict[str, str] = {}
            for key, column in zip(keys, columns, strict=True):
                cell = by_column.get(column)
                values[key] = None if cell is None else cell.value
                if cell is not None and cell.formula is not None:
                    formulas[key] = cell.formula
                if cell is not None and cell.is_error:
                    # The marker stays in ``values`` — it is what the file holds —
                    # and is repeated here so a reader of the record can tell it
                    # from the same characters typed in as text.
                    errors[key] = cell.text
            outside = [
                cell.ref for cell in row.cells if cell.column < min_col or cell.column > max_col
            ]
            if outside:
                budget.warn(
                    WarningCode.RAGGED_ROWS,
                    f"Row {number} on sheet '{scan.name}' has populated cells outside "
                    f"the table's columns {first_letter}-{last_letter} "
                    f"({_listed_refs(outside)}); they are recorded as cells but are not "
                    f"fields of the record.",
                    location=f"{scan.location}, row {number}",
                )
            records.append(
                {
                    "row": number,
                    "ref": f"{first_letter}{number}:{last_letter}{number}",
                    "values": values,
                    "formulas": formulas,
                    "errors": errors,
                }
            )

        has_data_region = first_data_row <= last_data_row
        region_end = last_data_row if has_data_region else header_row
        return {
            "sheet": scan.name,
            "table_name": table_name,
            "detection": detection,
            "ref": f"{first_letter}{header_row}:{last_letter}{region_end}",
            "header_row": header_row,
            "header_cells": [f"{get_column_letter(column)}{header_row}" for column in columns],
            "headers": headers,
            "record_keys": keys,
            "first_data_row": first_data_row if has_data_region else None,
            "last_data_row": last_data_row if has_data_region else None,
            "column_count": len(columns),
            "records": records,
        }

    # -- anchors ------------------------------------------------------------

    def _add_anchors(
        self,
        result: ParseResult,
        context: ParseContext,
        scans: tuple[_SheetScan, ...],
        tables: list[dict[str, Any]],
    ) -> None:
        sink = _AnchorSink(result, context.source_label)
        sheet_names = [scan.name for scan in scans]
        hidden = [scan.name for scan in scans if scan.state != _STATE_VISIBLE]
        unread = [str(name) for name in result.content["workbook"]["unread_sheet_names"]]

        overview = [f"Sheets ({len(scans)}): {', '.join(sheet_names)}"]
        if hidden:
            overview.append(f"Hidden sheets: {', '.join(hidden)}")
        if unread:
            overview.append(f"Tabs that are not worksheets and were not read: {', '.join(unread)}")
        sink.add(
            key="workbook",
            anchor_type=AnchorType.XLSX_WORKBOOK,
            location="",
            structured={
                "sheet_count": len(scans),
                "sheet_names": sheet_names,
                "hidden_sheet_names": hidden,
                "unread_sheet_names": unread,
            },
            excerpt="\n".join(overview),
        )

        for scan in scans:
            preview = [
                f"{row.row}: " + " | ".join(cell.text for cell in row.cells)
                for row in scan.rows[:SHEET_EXCERPT_ROWS]
            ]
            sink.add(
                key=f"sheet:{scan.name}",
                anchor_type=AnchorType.XLSX_WORKSHEET,
                location=scan.name,
                structured={
                    "sheet": scan.name,
                    "state": scan.state,
                    "used_range": scan.used_range,
                    "max_row": scan.max_row,
                    "max_column": scan.max_column,
                },
                excerpt="\n".join(preview),
            )

        by_name = {scan.name: scan for scan in scans}
        for table in tables:
            scan = by_name[str(table["sheet"])]
            self._add_table_anchors(sink, scan, table)

    def _add_table_anchors(
        self, sink: _AnchorSink, scan: _SheetScan, table: dict[str, Any]
    ) -> None:
        ref = str(table["ref"])
        bounds = _boundaries(ref)
        if bounds is None:  # pragma: no cover - the ref is built from integers
            return
        min_col, _, max_col, _ = bounds
        sheet = scan.name
        by_row = scan.rows_by_index
        header_row = int(table["header_row"])
        headers = [str(value) for value in table["headers"]]
        keys = [str(value) for value in table["record_keys"]]
        header_line = " | ".join(headers)
        detection = str(table["detection"])
        # A declared table's extent is a fact about the file; an inferred one is
        # this parser's reading of it, and evidence must not blur the two.
        table_kind = (
            EvidenceKind.DIRECT if detection == _DETECTION_NAMED else EvidenceKind.INFERENCE
        )
        records: list[dict[str, Any]] = table["records"]

        def add_cells(row_number: int) -> None:
            row = by_row.get(row_number)
            if row is None:
                return
            for cell in row.cells:
                if cell.column < min_col or cell.column > max_col:
                    continue
                sink.add(
                    key=f"cell:{sheet}!{cell.ref}",
                    anchor_type=AnchorType.XLSX_CELL,
                    location=f"{sheet} / {cell.ref}",
                    structured={
                        "sheet": sheet,
                        "ref": cell.ref,
                        "row": cell.row,
                        "column": cell.column_letter,
                        "column_index": cell.column,
                        "header": keys[cell.column - min_col],
                        "table_ref": ref,
                        # The value quoted here is the file's; which column name
                        # it sits under is only as sound as the table it belongs
                        # to, so the reading is carried on the citation itself.
                        "detection": detection,
                    },
                    excerpt=cell.text,
                )

        preview = [header_line]
        preview.extend(
            _row_line(by_row.get(int(record["row"])), min_col, max_col)
            for record in records[:TABLE_EXCERPT_ROWS]
        )
        sink.add(
            key=f"range:{sheet}!{ref}",
            anchor_type=AnchorType.XLSX_RANGE,
            location=f"{sheet} / {ref}",
            structured={
                "sheet": sheet,
                "ref": ref,
                "table_name": str(table["table_name"]),
                "detection": detection,
                "header_row": header_row,
                "first_data_row": table["first_data_row"],
                "last_data_row": table["last_data_row"],
                "record_count": len(records),
            },
            excerpt="\n".join(preview),
            evidence_kind=table_kind,
        )

        header_ref = (
            f"{get_column_letter(min_col)}{header_row}:{get_column_letter(max_col)}{header_row}"
        )
        sink.add(
            key=f"range:{sheet}!{header_ref}",
            anchor_type=AnchorType.XLSX_RANGE,
            location=f"{sheet} / {header_ref}",
            structured={
                "sheet": sheet,
                "ref": header_ref,
                "row": header_row,
                "table_ref": ref,
                "is_header_row": True,
                "detection": detection,
            },
            excerpt=header_line,
            # "This row is the header" is the whole of the claim, and for a
            # heuristic table that claim is this parser's, not the file's.
            evidence_kind=table_kind,
        )
        add_cells(header_row)

        for record in records:
            row_number = int(record["row"])
            row_ref = str(record["ref"])
            sink.add(
                key=f"range:{sheet}!{row_ref}",
                anchor_type=AnchorType.XLSX_RANGE,
                location=f"{sheet} / {row_ref}",
                structured={
                    "sheet": sheet,
                    "ref": row_ref,
                    "row": row_number,
                    "table_ref": ref,
                    "is_header_row": False,
                    "detection": detection,
                },
                excerpt=f"{header_line}\n{_row_line(by_row.get(row_number), min_col, max_col)}",
            )
            add_cells(row_number)

    # -- summary ------------------------------------------------------------

    def _summarise(
        self,
        result: ParseResult,
        scans: tuple[_SheetScan, ...],
        tables: list[dict[str, Any]],
    ) -> dict[str, Any]:
        records_per_sheet: dict[str, int] = {scan.name: 0 for scan in scans}
        for table in tables:
            sheet = str(table["sheet"])
            records_per_sheet[sheet] = records_per_sheet.get(sheet, 0) + len(table["records"])
        workbook: dict[str, Any] = result.content["workbook"]
        return {
            "sheet_count": len(scans),
            "visible_sheet_count": sum(1 for scan in scans if scan.state == _STATE_VISIBLE),
            "hidden_sheet_count": sum(1 for scan in scans if scan.state != _STATE_VISIBLE),
            "hidden_sheet_names": list(workbook["hidden_sheet_names"]),
            "unread_sheet_count": len(workbook["unread_sheet_names"]),
            "unread_sheet_names": list(workbook["unread_sheet_names"]),
            "populated_cell_count": sum(scan.populated_cell_count for scan in scans),
            "formula_cell_count": sum(len(scan.formula_refs) for scan in scans),
            "error_cell_count": sum(len(scan.error_refs) for scan in scans),
            "merged_range_count": sum(len(scan.merged_ranges) for scan in scans),
            "hidden_row_count": sum(len(scan.hidden_rows) for scan in scans),
            "hidden_column_count": sum(len(scan.hidden_columns) for scan in scans),
            "comment_count": sum(len(scan.comments) for scan in scans),
            "named_table_count": sum(len(scan.tables) for scan in scans),
            "defined_name_count": len(workbook["defined_names"]),
            "candidate_table_count": len(tables),
            "record_count": sum(len(table["records"]) for table in tables),
            "sheets": [
                {
                    "name": scan.name,
                    "state": scan.state,
                    "max_row": scan.max_row,
                    "max_column": scan.max_column,
                    "populated_cell_count": scan.populated_cell_count,
                    "record_count": records_per_sheet[scan.name],
                }
                for scan in scans
            ],
            "anchor_count": len(result.anchors),
            "warning_count": len(result.warnings),
            "unsupported_count": len(result.unsupported),
        }
