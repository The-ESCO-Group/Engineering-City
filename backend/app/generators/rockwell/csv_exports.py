"""Tag, comment and cross-reference CSV exports.

Three files, one writer. Every one of them is sorted explicitly before it is
written: a CSV whose row order came from whatever the database returned would
hash differently on two runs over the same data, and a checksum that moves for
no reason is a checksum nobody checks.

Quoting is delegated to :mod:`csv` with an explicit dialect rather than done by
hand, because a description containing a comma, a double quote and a newline is
not a hypothetical — it is what an engineer types into a description field, and
each of the three breaks a different naive implementation. Line endings are
``\\r\\n`` on every platform, which is what RFC 4180 and Logix's own exports
use, and the result is ``bytes`` so nothing downstream can translate them again.

What the shapes are, and how much of that is known
--------------------------------------------------
The tag CSV column set — ``TYPE,SCOPE,NAME,DESCRIPTION,DATATYPE,SPECIFIER,
ATTRIBUTES`` — is the Logix tag import/export shape. Studio 5000 also writes a
handful of ``remark`` lines above the header carrying an export date and the
software version; those are **omitted** here, because a date read from the clock
would make two generations of one revision differ, which is the property the
whole release pipeline rests on.

The comment CSV and the cross-reference CSV have no import shape to match:
Logix's own cross-reference export is a report rather than something you import.
Their columns are therefore defined here, documented here, and chosen so a human
can read them in a spreadsheet.
"""

from __future__ import annotations

import csv
import io
import uuid
from collections.abc import Iterable, Mapping, Sequence

from app.models.plc import LogicComment, LogicReference, LogicRung
from app.models.tags import Tag

__all__ = [
    "COMMENT_CSV_HEADER",
    "CROSS_REFERENCE_CSV_HEADER",
    "LINE_TERMINATOR",
    "TAG_CSV_HEADER",
    "emit_comment_csv",
    "emit_cross_reference_csv",
    "emit_tag_csv",
    "render_csv",
]

#: The Logix tag import/export column set.
TAG_CSV_HEADER: tuple[str, ...] = (
    "TYPE",
    "SCOPE",
    "NAME",
    "DESCRIPTION",
    "DATATYPE",
    "SPECIFIER",
    "ATTRIBUTES",
)

#: Defined here rather than matched: ``TYPE`` is ``RUNG`` for a rung comment and
#: ``OPERAND`` for a comment attached to an address within a rung, which Logix
#: keeps separately and which survive different edits.
COMMENT_CSV_HEADER: tuple[str, ...] = (
    "TYPE",
    "PROGRAM",
    "ROUTINE",
    "RUNG",
    "OPERAND",
    "LANGUAGE",
    "COMMENT",
)

#: One row per :class:`~app.models.plc.LogicReference`. ``DESTRUCTIVE`` is the
#: column the file exists for: sorting on ``TAG`` then ``DESTRUCTIVE`` puts every
#: writer of a tag next to every other writer of it.
CROSS_REFERENCE_CSV_HEADER: tuple[str, ...] = (
    "TAG",
    "PROGRAM",
    "ROUTINE",
    "RUNG",
    "INSTRUCTION",
    "REFERENCE_KIND",
    "DESTRUCTIVE",
)

#: RFC 4180, and what Logix writes. Fixed rather than platform-dependent.
LINE_TERMINATOR = "\r\n"

ENCODING = "utf-8"


def render_csv(header: Sequence[str], rows: Iterable[Sequence[str]]) -> bytes:
    """Serialise ``rows`` under ``header`` with fixed quoting and line endings.

    ``QUOTE_MINIMAL`` with an explicit ``\\r\\n`` terminator means a field is
    quoted exactly when it contains a comma, a double quote, a carriage return
    or a newline — and a contained double quote is doubled, which is the one
    piece of CSV escaping hand-rolled writers reliably get wrong.
    """
    buffer = io.StringIO(newline="")
    writer = csv.writer(
        buffer,
        delimiter=",",
        quotechar='"',
        doublequote=True,
        quoting=csv.QUOTE_MINIMAL,
        lineterminator=LINE_TERMINATOR,
    )
    writer.writerow(list(header))
    for row in rows:
        writer.writerow(list(row))
    return buffer.getvalue().encode(ENCODING)


def _boolean(value: bool) -> str:
    return "true" if value else "false"


def _data_type(tag: Tag) -> str:
    concrete = (tag.data_type_name or "").strip()
    base = concrete or str(tag.base_data_type)
    return f"{base}{(tag.dimensions or '').strip()}"


def _attributes(tag: Tag) -> str:
    """The Logix ``(Name := Value, ...)`` attribute blob.

    Only attributes the canonical model actually holds are written. ``Radix`` is
    absent because no column carries it, and inventing ``Decimal`` for every tag
    would be this generator asserting a display format nobody chose.
    """
    parts = [f"Constant := {_boolean(bool(tag.is_constant))}"]
    access = (tag.external_access or "").strip()
    if access:
        parts.append(f"ExternalAccess := {access}")
    return f"({', '.join(parts)})"


def emit_tag_csv(tags: Sequence[Tag], program_names: Mapping[uuid.UUID, str]) -> bytes:
    """The tag CSV, controller scope first and alphabetical within each scope.

    ``program_names`` resolves ``Tag.scope_program_id`` to the name Logix uses in
    the ``SCOPE`` column. It is a caller-supplied map rather than a relationship
    walk because :attr:`~app.models.tags.Tag.qualified_name` deliberately does
    not load the program — doing it per tag would be a query per row.
    """
    rows: list[tuple[str, ...]] = []
    for tag in tags:
        program_id = tag.scope_program_id
        scope = program_names.get(program_id, "") if program_id is not None else ""
        rows.append(
            (
                "ALIAS" if tag.is_alias else "TAG",
                scope,
                tag.name,
                (tag.description or "").strip(),
                "" if tag.is_alias else _data_type(tag),
                (tag.alias_for or "").strip() if tag.is_alias else "",
                _attributes(tag),
            )
        )
    # Controller-scope rows first (empty SCOPE sorts before any program name),
    # then by program, then by name. Stated here rather than left to the query.
    rows.sort(key=lambda row: (row[1], row[2], row[0]))
    return render_csv(TAG_CSV_HEADER, rows)


def emit_comment_csv(
    rung_comments: Sequence[tuple[str, str, LogicRung]],
    operand_comments: Sequence[tuple[str, str, int, LogicComment]],
) -> bytes:
    """Rung comments and operand comments, in one file.

    Both are passed with their program and routine names already resolved: a
    comment's location is denormalised into the row because a comment nobody can
    locate is a comment nobody acts on, and the row has to keep working after
    the rung it came from has been renumbered.
    """
    rows: list[tuple[str, ...]] = []
    for program_name, routine_name, rung in rung_comments:
        comment = (rung.comment or "").strip()
        if not comment:
            continue
        rows.append(("RUNG", program_name, routine_name, str(rung.ordinal), "", "", comment))
    for program_name, routine_name, rung_ordinal, item in operand_comments:
        text = (item.text or "").strip()
        if not text:
            continue
        rows.append(
            (
                "OPERAND",
                program_name,
                routine_name,
                str(rung_ordinal),
                (item.operand_text or "").strip(),
                (item.language_code or "").strip(),
                text,
            )
        )
    rows.sort(key=lambda row: (row[1], row[2], _numeric(row[3]), row[0], row[4], row[6]))
    return render_csv(COMMENT_CSV_HEADER, rows)


def emit_cross_reference_csv(
    references: Sequence[LogicReference],
    *,
    rung_numbers: Mapping[uuid.UUID, int] | None = None,
    instruction_mnemonics: Mapping[uuid.UUID, str] | None = None,
) -> bytes:
    """The cross-reference CSV, built from :class:`~app.models.plc.LogicReference`.

    ``LogicReference`` holds the rung and instruction by id rather than by
    number, so the two maps translate them into something a reader can find.
    A reference whose rung is not in the map is still written, with an empty
    ``RUNG``: dropping the row would lose the fact that the tag is referenced at
    all, which is the whole point of the table.
    """
    numbers = rung_numbers or {}
    mnemonics = instruction_mnemonics or {}
    rows: list[tuple[str, ...]] = []
    for reference in references:
        rung_number = numbers.get(reference.rung_id)
        instruction_id = reference.instruction_id
        rows.append(
            (
                reference.tag_name,
                (reference.program_name or "").strip(),
                (reference.routine_name or "").strip(),
                "" if rung_number is None else str(rung_number),
                mnemonics.get(instruction_id, "") if instruction_id is not None else "",
                (reference.reference_kind or "").strip(),
                _boolean(bool(reference.is_destructive)),
            )
        )
    rows.sort(key=lambda row: (row[0], row[1], row[2], _numeric(row[3]), row[4], row[5], row[6]))
    return render_csv(CROSS_REFERENCE_CSV_HEADER, rows)


def _numeric(value: str) -> tuple[int, int]:
    """Sort key that orders rung 2 before rung 10 and puts blanks last.

    Sorting the rendered string would put rung 10 between rung 1 and rung 2,
    which reads as a mistake in a file an engineer is scanning by eye.
    """
    try:
        return (0, int(value))
    except ValueError:
        return (1, 0)
