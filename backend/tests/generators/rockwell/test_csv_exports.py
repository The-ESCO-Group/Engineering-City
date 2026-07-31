"""CSV exports: the Logix shape, explicit ordering, and quoting that holds.

A description containing a comma, a double quote and a newline is not a
hypothetical — it is what an engineer types into a description field, and each
of the three breaks a different naive writer. The round-trip through
:mod:`csv` in these tests is the check that the file a spreadsheet opens is the
file we meant to write.
"""

from __future__ import annotations

import csv
import io

from app.core.enums import BaseDataType, TagScope
from app.generators.rockwell.csv_exports import (
    COMMENT_CSV_HEADER,
    CROSS_REFERENCE_CSV_HEADER,
    TAG_CSV_HEADER,
    emit_comment_csv,
    emit_cross_reference_csv,
    emit_tag_csv,
)
from tests.generators.rockwell.conftest import (
    make_instruction,
    make_reference,
    make_rung,
    make_tag,
    oid,
)

AWKWARD = 'Trips at 40 psi, "wet" guard open\nSecond line of the note.'


def read_back(data: bytes) -> list[list[str]]:
    return list(csv.reader(io.StringIO(data.decode("utf-8"), newline="")))


class TestTagCsv:
    def test_header_is_the_logix_import_shape(self) -> None:
        data = emit_tag_csv([make_tag("A")], {})
        assert read_back(data)[0] == list(TAG_CSV_HEADER)

    def test_no_dated_remark_preamble_is_written(self) -> None:
        # Studio writes a dated ``remark`` block above the header. A clock
        # reading in the file would make two generations of one revision differ.
        assert not emit_tag_csv([make_tag("A")], {}).startswith(b"remark")

    def test_a_row_carries_type_scope_datatype_and_attributes(self) -> None:
        program_id = oid(300)
        tags = [
            make_tag("ESTOP_OK", description="Emergency stop healthy."),
            make_tag(
                "Run_Cmd",
                scope=TagScope.PROGRAM,
                scope_program_id=program_id,
                external_access="Read Only",
                is_constant=True,
            ),
            make_tag(
                "Recipe",
                base_data_type=BaseDataType.DINT,
                dimensions="[16]",
            ),
        ]
        rows = read_back(emit_tag_csv(tags, {program_id: "MainProgram"}))[1:]
        assert rows[0] == [
            "TAG",
            "",
            "ESTOP_OK",
            "Emergency stop healthy.",
            "BOOL",
            "",
            "(Constant := false, ExternalAccess := Read/Write)",
        ]
        assert rows[1][4] == "DINT[16]"
        assert rows[2] == [
            "TAG",
            "MainProgram",
            "Run_Cmd",
            "",
            "BOOL",
            "",
            "(Constant := true, ExternalAccess := Read Only)",
        ]

    def test_an_alias_carries_its_target_in_the_specifier_column(self) -> None:
        tag = make_tag("ESTOP_In", is_alias=True, alias_for="DI_01:I.Data[0]")
        row = read_back(emit_tag_csv([tag], {}))[1]
        assert row[0] == "ALIAS"
        assert row[4] == ""
        assert row[5] == "DI_01:I.Data[0]"

    def test_ordering_is_explicit_and_independent_of_input_order(self) -> None:
        program_id = oid(300)
        forwards = [
            make_tag("Zebra"),
            make_tag("Alpha"),
            make_tag("Scoped", scope=TagScope.PROGRAM, scope_program_id=program_id),
        ]
        names = {program_id: "MainProgram"}
        assert emit_tag_csv(forwards, names) == emit_tag_csv(list(reversed(forwards)), names)
        rows = read_back(emit_tag_csv(forwards, names))[1:]
        # Controller scope first, then by program, then by name.
        assert [(row[1], row[2]) for row in rows] == [
            ("", "Alpha"),
            ("", "Zebra"),
            ("MainProgram", "Scoped"),
        ]

    def test_a_description_with_a_comma_a_quote_and_a_newline_round_trips(self) -> None:
        data = emit_tag_csv([make_tag("Guard", description=AWKWARD)], {})
        text = data.decode("utf-8")
        assert '"Trips at 40 psi, ""wet"" guard open' in text
        assert read_back(data)[1][3] == AWKWARD

    def test_line_endings_are_crlf_on_every_platform(self) -> None:
        data = emit_tag_csv([make_tag("A"), make_tag("B")], {})
        assert data.count(b"\r\n") == 3
        assert b"\n" not in data.replace(b"\r\n", b"")


class TestCommentCsv:
    def test_rung_and_operand_comments_are_located_and_ordered(self) -> None:
        rungs = [
            ("MainProgram", "R2", make_rung(1, [], comment="Second routine, first rung.")),
            ("MainProgram", "R1", make_rung(10, [], comment="Tenth rung.")),
            ("MainProgram", "R1", make_rung(2, [], comment="Second rung.")),
            ("MainProgram", "R1", make_rung(3, [], comment="")),
        ]
        rows = read_back(emit_comment_csv(rungs, []))
        assert rows[0] == list(COMMENT_CSV_HEADER)
        # Rung 2 before rung 10: sorting the rendered string would not.
        assert [(row[2], row[3]) for row in rows[1:]] == [
            ("R1", "2"),
            ("R1", "10"),
            ("R2", "1"),
        ]

    def test_a_comment_with_a_comma_and_a_newline_round_trips(self) -> None:
        rung = make_rung(0, [], comment=AWKWARD)
        rows = read_back(emit_comment_csv([("P", "R", rung)], []))
        assert rows[1][6] == AWKWARD


class TestCrossReferenceCsv:
    def test_rows_are_sorted_by_tag_and_carry_the_destructive_flag(self) -> None:
        rung_id = oid(701)
        references = [
            make_reference("Run_Cmd", rung_id=rung_id, is_destructive=True, reference_kind="write"),
            make_reference("ESTOP_OK", rung_id=rung_id),
        ]
        rows = read_back(
            emit_cross_reference_csv(
                references,
                rung_numbers={rung_id: 4},
                instruction_mnemonics={},
            )
        )
        assert rows[0] == list(CROSS_REFERENCE_CSV_HEADER)
        assert [row[0] for row in rows[1:]] == ["ESTOP_OK", "Run_Cmd"]
        assert rows[1][3] == "4"
        assert rows[1][6] == "false"
        assert rows[2][6] == "true"

    def test_a_reference_whose_rung_is_unknown_is_still_written(self) -> None:
        # Dropping it would lose the fact that the tag is referenced at all,
        # which is the whole point of the cross-reference table.
        rows = read_back(emit_cross_reference_csv([make_reference("Orphan")]))
        assert rows[1][0] == "Orphan"
        assert rows[1][3] == ""

    def test_the_instruction_column_is_filled_from_the_supplied_map(self) -> None:
        instruction = make_instruction(0, "OTE", ["Run_Cmd"], identifier=814)
        reference = make_reference("Run_Cmd", is_destructive=True)
        reference.instruction_id = instruction.id
        rows = read_back(
            emit_cross_reference_csv([reference], instruction_mnemonics={instruction.id: "OTE"})
        )
        assert rows[1][4] == "OTE"
