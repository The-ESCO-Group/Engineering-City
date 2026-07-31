"""PDF parser behaviour.

The committed fixtures carry the ground truth these tests exist to protect:
``Testing_Notes.pdf`` records the two planted test failures (ILK-003 / PL3-014
and the 39 psi versus 60 psi threshold / PL3-015), and ``Scanned_Drawing.pdf``
has no text layer at all. The single most important assertion in this file is
that the scanned drawing produces a ``no_text_layer`` warning: without it the
product would be free to report a drawing it never read as reviewed and clean.

Degenerate and hostile inputs are assembled inline in ``tmp_path`` — a minimal
PDF is little more than a byte-offset table, so no fixture file is needed and
none is added.
"""

from __future__ import annotations

import io
import uuid
from collections.abc import Sequence
from pathlib import Path

import pytest
from pypdf import PageObject, PdfReader, PdfWriter
from pypdf.errors import PdfReadError

from app.core.config import Settings
from app.core.enums import AnchorType, DocumentRole, EvidenceKind, FileKind
from app.core.errors import ParserError
from app.models.evidence import MAX_EXCERPT_CHARS
from app.parsers.base import (
    MAX_ANCHOR_KEY_CHARS,
    MAX_DISPLAY_LOCATION_CHARS,
    MAX_SOURCE_LABEL_CHARS,
    ParseContext,
    Parser,
    ParseResult,
    WarningCode,
)
from app.parsers.pdf_parser import (
    MAX_TEXT_RANGE_ANCHORS,
    MIN_PAGE_CHARACTERS,
    PAGE_EXCERPT_CHARS,
    PdfParser,
)

DEMO_PROJECT = Path(__file__).resolve().parents[3] / "fixtures" / "demo_project"
TESTING_NOTES = DEMO_PROJECT / "Testing_Notes.pdf"
SCANNED_DRAWING = DEMO_PROJECT / "Scanned_Drawing.pdf"

#: Measured from the committed ``Testing_Notes.pdf``, not recomputed from the
#: parser's own output: a regression that drops a line has to fail a test rather
#: than quietly change both sides of a comparison.
NOTES_CHARACTERS = 919
NOTES_LINES = 20
NOTES_LAST_LINE = "   Not tested. Interlock ILK-007 is undefined."


def parse(path: Path, settings: Settings, *, archive_member_path: str = "") -> ParseResult:
    context = ParseContext(
        file_id=uuid.UUID(int=1),
        project_id=uuid.UUID(int=2),
        path=path,
        filename=path.name,
        file_kind=FileKind.PDF,
        document_role=DocumentRole.TESTING_NOTES,
        settings=settings,
        archive_member_path=archive_member_path,
    )
    return PdfParser().parse(context)


def codes(result: ParseResult) -> list[str]:
    return [warning.code for warning in result.warnings]


def warning_with(result: ParseResult, code: str) -> str:
    matching = [warning.message for warning in result.warnings if warning.code == code]
    assert matching, f"expected a {code} warning, got {codes(result)}"
    return matching[0]


def anchor_signature(result: ParseResult) -> list[tuple[object, ...]]:
    return [
        (
            anchor.key,
            anchor.anchor_type,
            anchor.display_location,
            tuple(sorted(anchor.structured_location.items())),
            anchor.excerpt,
            anchor.content_hash,
            anchor.evidence_kind,
            anchor.expectation,
        )
        for anchor in result.anchors
    ]


# ---------------------------------------------------------------------------
# Minimal PDF assembly, so degenerate inputs need no committed fixture
# ---------------------------------------------------------------------------

#: Marks on the page but no text operators: what a scanned page looks like.
GRAPHICS_ONLY = (
    b"q 0.85 0.85 0.85 rg 56 520 500 220 re f Q\n"
    b"q 0.4 0.4 0.4 rg 96 700 180 8 re f 96 676 320 8 re f Q\n"
)

FONT_OBJECT = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"


def assemble_pdf(objects: Sequence[bytes], trailer_extra: str = "") -> bytes:
    """A PDF with a correct cross-reference table, built the way the fixtures are."""
    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for index, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{index} 0 obj\n".encode("ascii") + body + b"\nendobj\n"

    xref_at = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode("ascii")
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode("ascii")
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R{trailer_extra} >>\n"
        f"startxref\n{xref_at}\n%%EOF\n"
    ).encode("ascii")
    return bytes(out)


def page_content(lines: Sequence[str]) -> bytes:
    if not lines:
        return GRAPHICS_ONLY
    parts = [b"BT\n"]
    for index, text in enumerate(lines):
        escaped = text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
        parts.append(
            f"/F1 10 Tf 1 0 0 1 56 {760 - index * 12} Tm ({escaped}) Tj\n".encode("latin-1")
        )
    parts.append(b"ET")
    return b"".join(parts)


def pdf_bytes(pages: Sequence[Sequence[str]], info: bytes = b"") -> bytes:
    """A PDF of ``pages``; a page given no lines gets graphics and no text layer."""
    kids = " ".join(f"{4 + 2 * index} 0 R" for index in range(len(pages)))
    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode("ascii"),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
    ]
    for index, lines in enumerate(pages):
        body = page_content(lines)
        objects.append(
            (
                "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                f"/Resources << /Font << /F1 3 0 R >> >> /Contents {5 + 2 * index} 0 R >>"
            ).encode("ascii")
        )
        objects.append(
            f"<< /Length {len(body)} >>\nstream\n".encode("ascii") + body + b"\nendstream"
        )
    if not info:
        return assemble_pdf(objects)
    objects.append(info)
    return assemble_pdf(objects, f" /Info {len(objects)} 0 R")


def write_pdf(tmp_path: Path, name: str, pages: Sequence[Sequence[str]], info: bytes = b"") -> Path:
    path = tmp_path / name
    path.write_bytes(pdf_bytes(pages, info))
    return path


def write_bytes(tmp_path: Path, name: str, data: bytes) -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


# ---------------------------------------------------------------------------
# Testing_Notes.pdf — the fixture with a real text layer
# ---------------------------------------------------------------------------


def test_parser_declares_the_pdf_contract() -> None:
    parser = PdfParser()

    assert isinstance(parser, Parser)
    assert parser.name == "pdf"
    assert parser.handles == (FileKind.PDF,)


def test_testing_notes_reports_one_page_with_a_text_layer(settings: Settings) -> None:
    result = parse(TESTING_NOTES, settings)
    document = result.content["document"]
    page = result.content["pages"][0]

    assert result.artifact_kind == FileKind.PDF.value
    assert result.content["page_count"] == 1
    assert document["page_count"] == 1
    assert document["has_text_layer"] is True
    assert document["is_encrypted"] is False
    assert document["total_characters"] == NOTES_CHARACTERS
    assert page["page"] == 1
    assert page["has_text_layer"] is True
    assert page["character_count"] == NOTES_CHARACTERS
    assert len(page["text"]) == NOTES_CHARACTERS
    assert page["annotations"] == []
    assert len(page["lines"]) == NOTES_LINES
    assert page["lines"][0]["line"] == 1
    assert (
        page["lines"][0]["text"]
        == "SYNTHETIC FIXTURE - Conveyor Line 3 Pre-Commissioning Test Notes"
    )
    assert page["lines"][-1] == {"line": NOTES_LINES, "text": NOTES_LAST_LINE}


def test_testing_notes_extracts_the_two_planted_test_failures(settings: Settings) -> None:
    """These five strings are the fixture's ground truth about ILK-003 and ILK-005."""
    text = parse(TESTING_NOTES, settings).content["pages"][0]["text"]

    assert "ILK-003" in text
    assert "PL3-014" in text
    assert "PL3-015" in text
    assert "39 psi" in text
    assert "60 psi" in text


def test_testing_notes_keeps_every_planted_result_including_the_last_line(
    settings: Settings,
) -> None:
    """The whole page is ground truth, not just the two failures.

    ILK-007 is recorded only on the final line of the fixture, so a regression
    that loses the tail of a page would otherwise pass every other assertion
    here: the counts below are measured from the file, not from the parser.
    """
    result = parse(TESTING_NOTES, settings)
    page = result.content["pages"][0]
    text = page["text"]

    assert text.count("PASS") == 3
    assert text.count("FAIL") == 1
    assert "6. Surge hopper level" in text
    assert "ILK-007" in text
    assert text.endswith(NOTES_LAST_LINE)

    quoted = [
        anchor.excerpt
        for anchor in result.anchors
        if anchor.anchor_type is AnchorType.PDF_TEXT_RANGE
    ]
    assert len(quoted) == NOTES_LINES
    assert quoted[-1] == "Not tested. Interlock ILK-007 is undefined."


def test_testing_notes_summary_counts_the_shape_without_the_content(settings: Settings) -> None:
    result = parse(TESTING_NOTES, settings)
    page = result.content["pages"][0]

    assert result.summary["page_count"] == 1
    assert result.summary["pages_with_text_layer"] == 1
    assert result.summary["pages_without_text_layer"] == 0
    assert result.summary["has_text_layer"] is True
    assert result.summary["total_characters"] == NOTES_CHARACTERS
    assert result.summary["text_line_count"] == NOTES_LINES
    assert result.summary["is_encrypted"] is False
    assert result.summary["text_range_anchor_count"] == NOTES_LINES
    assert len(page["lines"]) == NOTES_LINES


def test_a_readable_page_produces_no_warning_and_no_unsupported_item(settings: Settings) -> None:
    result = parse(TESTING_NOTES, settings)

    assert result.warnings == []
    assert result.unsupported == []


def test_page_anchor_quotes_the_start_of_the_page(settings: Settings) -> None:
    result = parse(TESTING_NOTES, settings)
    text = result.content["pages"][0]["text"].strip()
    anchor = result.anchors_by_key["page:1"]

    assert anchor.anchor_type is AnchorType.PDF_PAGE
    assert anchor.evidence_kind is EvidenceKind.DIRECT
    assert anchor.display_location == "Testing_Notes.pdf / page 1"
    assert anchor.structured_location == {"page": 1}
    assert anchor.source_label == "Testing_Notes.pdf"
    assert anchor.expectation == ""
    assert anchor.excerpt.startswith("SYNTHETIC FIXTURE - Conveyor Line 3")
    assert len(anchor.excerpt) <= PAGE_EXCERPT_CHARS + 3

    quoted = anchor.excerpt.removesuffix("...")
    assert anchor.excerpt.endswith("...")
    assert text.startswith(quoted)
    assert text[len(quoted)].isspace()


def test_every_page_gets_an_anchor_and_every_key_is_unique(settings: Settings) -> None:
    result = parse(TESTING_NOTES, settings)
    keys = [anchor.key for anchor in result.anchors]

    assert [key for key in keys if key.startswith("page:")] == ["page:1"]
    assert len(set(keys)) == len(keys)
    assert all(anchor.source_label == "Testing_Notes.pdf" for anchor in result.anchors)


def test_text_range_offsets_index_into_that_pages_text(settings: Settings) -> None:
    result = parse(TESTING_NOTES, settings)
    text = result.content["pages"][0]["text"]
    ranges = [
        anchor for anchor in result.anchors if anchor.anchor_type is AnchorType.PDF_TEXT_RANGE
    ]

    assert ranges
    for anchor in ranges:
        start = anchor.structured_location["start"]
        end = anchor.structured_location["end"]
        assert anchor.structured_location == {"page": 1, "start": start, "end": end}
        assert text[start:end] == anchor.excerpt
        assert anchor.key == f"textrange:1:{start}-{end}"
        assert anchor.display_location == f"Testing_Notes.pdf / page 1, characters {start}-{end}"
        assert anchor.evidence_kind is EvidenceKind.DIRECT


def test_a_single_statement_is_citable_rather_than_the_whole_page(settings: Settings) -> None:
    """A finding about ILK-003 must be able to quote the line that records the failure."""
    result = parse(TESTING_NOTES, settings)
    ranges = [
        anchor for anchor in result.anchors if anchor.anchor_type is AnchorType.PDF_TEXT_RANGE
    ]

    jam = [anchor for anchor in ranges if "ILK-003" in anchor.excerpt]
    assert len(jam) == 1
    assert jam[0].excerpt == (
        "Expected CV101 and CV102 to stop per ILK-003. FAIL - raised as punch item"
    )

    pressure = [anchor for anchor in ranges if "39 psi" in anchor.excerpt]
    assert len(pressure) == 1
    assert pressure[0].excerpt == "at 39 psi. Threshold does not match the 60 psi in the functional"


def test_archive_provenance_reaches_every_anchor_label(settings: Settings) -> None:
    result = parse(
        TESTING_NOTES, settings, archive_member_path="Line3_Project_Package/Testing_Notes.pdf"
    )
    anchor = result.anchors_by_key["page:1"]

    label = "Testing_Notes.pdf (Line3_Project_Package/Testing_Notes.pdf)"
    assert anchor.source_label == label
    assert anchor.display_location == f"{label} / page 1"


# ---------------------------------------------------------------------------
# Scanned_Drawing.pdf — the fixture with no text layer
# ---------------------------------------------------------------------------


def test_scanned_drawing_reports_a_missing_text_layer_instead_of_an_empty_page(
    settings: Settings,
) -> None:
    """The assertion that stops the product claiming it read a drawing it could not."""
    result = parse(SCANNED_DRAWING, settings)
    page = result.content["pages"][0]

    assert result.content["page_count"] == 1
    assert result.content["document"]["has_text_layer"] is False
    assert page["has_text_layer"] is False
    assert page["text"].strip() == ""
    assert page["character_count"] == 0

    message = warning_with(result, WarningCode.NO_TEXT_LAYER)
    assert "ocr" in message.lower()
    assert "manual review" in message.lower()

    assert [item.kind for item in result.unsupported] == ["page_without_text_layer"]
    assert result.unsupported[0].location == "page 1"


def test_an_unreadable_page_is_citable_as_a_gap(settings: Settings) -> None:
    result = parse(SCANNED_DRAWING, settings)
    anchor = result.anchors_by_key["page:1"]

    assert anchor.anchor_type is AnchorType.PDF_PAGE
    assert anchor.evidence_kind is EvidenceKind.UNSUPPORTED_CONTENT
    assert anchor.display_location == "Scanned_Drawing.pdf / page 1"
    assert "OCR or manual review" in anchor.expectation
    assert anchor.excerpt == ""
    assert not [
        anchor for anchor in result.anchors if anchor.anchor_type is AnchorType.PDF_TEXT_RANGE
    ]


def test_the_two_fixtures_disagree_about_having_a_text_layer(settings: Settings) -> None:
    notes = parse(TESTING_NOTES, settings)
    scan = parse(SCANNED_DRAWING, settings)

    assert notes.summary["has_text_layer"] is True
    assert scan.summary["has_text_layer"] is False
    assert notes.summary["pages_without_text_layer"] == 0
    assert scan.summary["pages_without_text_layer"] == 1
    assert WarningCode.NO_TEXT_LAYER not in codes(notes)
    assert WarningCode.NO_TEXT_LAYER in codes(scan)


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_reparsing_the_same_bytes_produces_identical_output(settings: Settings) -> None:
    for path in (TESTING_NOTES, SCANNED_DRAWING):
        first = parse(path, settings)
        second = parse(path, settings)

        assert first.content == second.content
        assert first.summary == second.summary
        assert [warning.as_dict() for warning in first.warnings] == [
            warning.as_dict() for warning in second.warnings
        ]
        assert [item.as_dict() for item in first.unsupported] == [
            item.as_dict() for item in second.unsupported
        ]
        assert anchor_signature(first) == anchor_signature(second)


# ---------------------------------------------------------------------------
# Degenerate and hostile input
# ---------------------------------------------------------------------------


def test_an_empty_file_raises_parser_error(tmp_path: Path, settings: Settings) -> None:
    path = write_bytes(tmp_path, "Empty.pdf", b"")

    with pytest.raises(ParserError) as excinfo:
        parse(path, settings)

    assert excinfo.value.code == "parser_error"
    assert excinfo.value.status_code == 422
    assert excinfo.value.details["reason"] == "empty_file"
    assert "empty" in excinfo.value.message.lower()


def test_bytes_that_are_not_a_pdf_raise_parser_error(tmp_path: Path, settings: Settings) -> None:
    """A raw pypdf exception here would surface as an internal failure, not a bad file."""
    path = write_bytes(tmp_path, "Not_A_Drawing.pdf", b"Dear engineer, please find attached.")

    with pytest.raises(ParserError) as excinfo:
        parse(path, settings)

    assert excinfo.value.details["reason"] == "unreadable_pdf"
    assert "Not_A_Drawing.pdf" in excinfo.value.message


def test_a_truncated_pdf_raises_parser_error(tmp_path: Path, settings: Settings) -> None:
    truncated = TESTING_NOTES.read_bytes()[:-200]
    path = write_bytes(tmp_path, "Truncated.pdf", truncated)

    with pytest.raises(ParserError) as excinfo:
        parse(path, settings)

    assert excinfo.value.details["reason"] == "unreadable_pdf"
    assert "truncated" in excinfo.value.message.lower()


def test_a_pdf_with_no_pages_raises_parser_error(tmp_path: Path, settings: Settings) -> None:
    path = write_bytes(
        tmp_path,
        "No_Pages.pdf",
        assemble_pdf(
            [
                b"<< /Type /Catalog /Pages 2 0 R >>",
                b"<< /Type /Pages /Kids [] /Count 0 >>",
            ]
        ),
    )

    with pytest.raises(ParserError) as excinfo:
        parse(path, settings)

    assert excinfo.value.details["reason"] == "no_pages"


def test_a_password_protected_pdf_is_refused_rather_than_reported_as_empty(
    tmp_path: Path, settings: Settings
) -> None:
    """Chosen behaviour: a PDF needing a password is a ParserError, not a warning.

    There is no content to record, and a result with zero pages would read
    downstream as "examined, nothing found".
    """
    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(pdf_bytes([["CV102 jam detection"]]))))
    writer.encrypt("secret")
    buffer = io.BytesIO()
    writer.write(buffer)
    path = write_bytes(tmp_path, "Locked.pdf", buffer.getvalue())

    with pytest.raises(ParserError) as excinfo:
        parse(path, settings)

    assert excinfo.value.details["reason"] == "password_protected"
    assert "password" in excinfo.value.message.lower()


def test_encryption_that_needs_no_password_is_parsed_with_a_warning(
    tmp_path: Path, settings: Settings
) -> None:
    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(pdf_bytes([["CV102 jam detection"]]))))
    writer.encrypt(user_password="", owner_password="owner")
    buffer = io.BytesIO()
    writer.write(buffer)
    path = write_bytes(tmp_path, "Permissioned.pdf", buffer.getvalue())

    result = parse(path, settings)

    assert result.content["document"]["is_encrypted"] is True
    assert result.summary["is_encrypted"] is True
    assert "CV102 jam detection" in result.content["pages"][0]["text"]
    assert "empty user password" in warning_with(result, WarningCode.PASSWORD_PROTECTED)


def test_a_page_yielding_almost_no_text_is_reported_as_a_low_yield(
    tmp_path: Path, settings: Settings
) -> None:
    path = write_pdf(tmp_path, "Mostly_Image.pdf", [["Sheet 3"]])

    result = parse(path, settings)
    anchor = result.anchors_by_key["page:1"]

    assert result.content["pages"][0]["has_text_layer"] is True
    assert result.content["pages"][0]["text"] == "Sheet 3"
    assert str(MIN_PAGE_CHARACTERS) in warning_with(result, WarningCode.LOW_TEXT_YIELD)
    assert [item.kind for item in result.unsupported] == ["page_with_low_text_yield"]
    assert anchor.evidence_kind is EvidenceKind.PARSER_WARNING
    assert "OCR or manual review" in anchor.expectation
    assert anchor.excerpt == "Sheet 3"


def test_pages_without_text_are_reported_as_one_group_in_a_mixed_document(
    tmp_path: Path, settings: Settings
) -> None:
    path = write_pdf(
        tmp_path,
        "Mixed.pdf",
        [["Conveyor Line 3 general arrangement", "Sheet 1 of 4"], [], [], []],
    )

    result = parse(path, settings)
    warnings = [w for w in result.warnings if w.code == WarningCode.NO_TEXT_LAYER]

    assert result.content["page_count"] == 4
    assert result.content["document"]["has_text_layer"] is True
    assert result.summary["pages_without_text_layer"] == 3
    assert len(warnings) == 1
    assert warnings[0].location == "pages 2-4"
    assert warnings[0].message.startswith("Pages 2-4 yielded no extractable text")
    assert [item.location for item in result.unsupported] == ["pages 2-4"]

    assert result.anchors_by_key["page:1"].evidence_kind is EvidenceKind.DIRECT
    for number in (2, 3, 4):
        anchor = result.anchors_by_key[f"page:{number}"]
        assert anchor.evidence_kind is EvidenceKind.UNSUPPORTED_CONTENT
        assert f"Page {number}" in anchor.expectation


def test_line_citations_are_capped_without_truncating_the_page_text(
    tmp_path: Path, settings: Settings
) -> None:
    lines = [
        f"Line {index:04d} of the generated page" for index in range(MAX_TEXT_RANGE_ANCHORS + 200)
    ]
    path = write_pdf(tmp_path, "Very_Long.pdf", [lines])

    result = parse(path, settings)
    truncated = [w for w in result.warnings if w.code == WarningCode.TRUNCATED]

    assert len(result.content["pages"][0]["lines"]) == len(lines)
    assert result.summary["text_range_anchor_count"] == MAX_TEXT_RANGE_ANCHORS
    assert "page:1" in result.anchors_by_key
    assert str(MAX_TEXT_RANGE_ANCHORS) in warning_with(result, WarningCode.TRUNCATED)
    assert [w.location for w in truncated] == ["page 1"]
    assert "stop part-way down the page" in truncated[0].message
    assert [item.kind for item in result.unsupported] == ["line_citations_truncated"]
    assert result.unsupported[0].location == "page 1"


def test_every_page_the_citation_cap_reaches_is_named_not_only_the_first(
    tmp_path: Path, settings: Settings
) -> None:
    """A whole page with no line-level citation must not hide behind "page 3".

    Five pages of 400 lines exhaust the 1,000-range cap part-way down page 3,
    leaving pages 4 and 5 with no character-range citation whatsoever. A warning
    naming only the page the cap fell on would tell a reader that page 3 was the
    only page affected.
    """
    per_page = 400
    pages = [
        [f"Page {number} line {index:03d} of the generated record" for index in range(per_page)]
        for number in range(1, 6)
    ]
    path = write_pdf(tmp_path, "Five_Long_Pages.pdf", pages)

    result = parse(path, settings)
    ranges_per_page = {
        number: sum(
            1
            for anchor in result.anchors
            if anchor.anchor_type is AnchorType.PDF_TEXT_RANGE
            and anchor.structured_location["page"] == number
        )
        for number in range(1, 6)
    }
    truncated = [w for w in result.warnings if w.code == WarningCode.TRUNCATED]

    assert result.summary["text_range_anchor_count"] == MAX_TEXT_RANGE_ANCHORS
    assert ranges_per_page == {1: 400, 2: 400, 3: 200, 4: 0, 5: 0}
    assert [len(page["lines"]) for page in result.content["pages"]] == [per_page] * 5
    assert all(f"page:{number}" in result.anchors_by_key for number in range(1, 6))

    assert len(truncated) == 1
    assert truncated[0].location == "pages 3-5"
    assert "Citations for page 3 stop part-way down the page." in truncated[0].message
    assert "No line on pages 4-5 has a character-range citation at all." in truncated[0].message
    assert [item.kind for item in result.unsupported] == ["line_citations_truncated"]
    assert result.unsupported[0].location == "pages 3-5"


def test_a_page_of_only_whitespace_is_recorded_as_a_page_that_yielded_nothing(
    tmp_path: Path, settings: Settings
) -> None:
    """Whitespace is not content: counting it contradicts ``has_text_layer``.

    The page below really does extract as ``'   \\n   '`` (asserted directly
    against pypdf, so this test cannot pass vacuously). Recorded as-is it gave a
    document with ``has_text_layer`` False and ``total_characters`` 7, and two
    blank ``lines`` entries on a page the contract says yielded nothing.
    """
    path = write_pdf(tmp_path, "Whitespace_Only.pdf", [["   ", "   "]])

    assert PdfReader(str(path)).pages[0].extract_text() == "   \n   "

    result = parse(path, settings)
    page = result.content["pages"][0]

    assert page["text"] == ""
    assert page["character_count"] == 0
    assert page["lines"] == []
    assert page["has_text_layer"] is False
    assert result.content["document"]["total_characters"] == 0
    assert result.content["document"]["has_text_layer"] is False
    assert result.summary["total_characters"] == 0
    assert result.summary["text_line_count"] == 0
    assert result.summary["pages_without_text_layer"] == 1

    assert WarningCode.NO_TEXT_LAYER in codes(result)
    assert [item.kind for item in result.unsupported] == ["page_without_text_layer"]
    assert result.anchors_by_key["page:1"].evidence_kind is EvidenceKind.UNSUPPORTED_CONTENT
    assert result.anchors_by_key["page:1"].excerpt == ""


def test_a_long_source_label_stays_within_the_columns_it_is_stored_in(
    settings: Settings,
) -> None:
    """Bounded in ``AnchorDraft.__post_init__``; asserted here on a PDF anchor."""
    member = "Line3_Project_Package/" + "deeply_nested_folder/" * 20 + "Testing_Notes.pdf"

    result = parse(TESTING_NOTES, settings, archive_member_path=member)

    assert len(member) > 400
    for anchor in result.anchors:
        assert len(anchor.source_label) <= MAX_SOURCE_LABEL_CHARS
        assert len(anchor.display_location) <= MAX_DISPLAY_LOCATION_CHARS
        assert len(anchor.key) <= MAX_ANCHOR_KEY_CHARS
    assert result.anchors_by_key["page:1"].source_label.startswith("Testing_Notes.pdf (")


def test_a_page_that_cannot_be_decoded_is_recorded_rather_than_dropped(
    tmp_path: Path, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A damaged page must cost that page's text, not the whole document."""
    calls: list[int] = []

    def exploding_extract(self: PageObject, *args: object, **kwargs: object) -> str:
        calls.append(1)
        if len(calls) == 1:
            raise PdfReadError("simulated damaged content stream")
        return "CV103 discharge conveyor detail"

    monkeypatch.setattr(PageObject, "extract_text", exploding_extract)
    path = write_pdf(tmp_path, "Damaged.pdf", [["first page"], ["second page"]])

    result = parse(path, settings)

    assert result.content["pages"][0]["has_text_layer"] is False
    assert result.content["pages"][1]["text"] == "CV103 discharge conveyor detail"
    assert "simulated damaged content stream" in warning_with(
        result, WarningCode.STRUCTURE_UNRECOGNISED
    )
    assert [item.kind for item in result.unsupported] == ["undecodable_page"]
    assert result.anchors_by_key["page:1"].evidence_kind is EvidenceKind.UNSUPPORTED_CONTENT
    assert "manual review" in result.anchors_by_key["page:1"].expectation


def test_document_properties_are_recorded_verbatim(tmp_path: Path, settings: Settings) -> None:
    info = (
        b"<< /Title (  Conveyor   Line 3 Test Notes ) /Author (Synthetic Engineer) "
        b"/Subject (Pre-commissioning) /Creator (Fixture) /Producer (Hand-assembled) >>"
    )
    path = write_pdf(tmp_path, "With_Properties.pdf", [["A readable line of page text"]], info)

    properties = parse(path, settings).content["document"]["properties"]

    assert properties == {
        "title": "Conveyor Line 3 Test Notes",
        "author": "Synthetic Engineer",
        "subject": "Pre-commissioning",
        "creator": "Fixture",
        "producer": "Hand-assembled",
    }


def test_missing_document_properties_are_empty_strings(settings: Settings) -> None:
    """The fixture carries no /Info dictionary, and the shape must not change."""
    properties = parse(TESTING_NOTES, settings).content["document"]["properties"]

    assert properties == {
        "title": "",
        "author": "",
        "subject": "",
        "creator": "",
        "producer": "",
    }


# ---------------------------------------------------------------------------
# Pages the document declares but that could not be read
# ---------------------------------------------------------------------------


def one_page_with_missing_siblings(kids: str, count: int) -> bytes:
    """A page tree that declares ``count`` pages but resolves only the first."""
    body = page_content(["CONVEYOR LINE 3 TEST RECORD sheet 1", "1. Emergency stop  PASS"])
    return assemble_pdf(
        [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            f"<< /Type /Pages /Kids [{kids}] /Count {count} >>".encode("ascii"),
            FONT_OBJECT,
            (
                b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                b"/Resources << /Font << /F1 3 0 R >> >> /Contents 5 0 R >>"
            ),
            f"<< /Length {len(body)} >>\nstream\n".encode("ascii") + body + b"\nendstream",
        ]
    )


def test_pages_the_document_declares_but_that_are_unreadable_are_reported(
    tmp_path: Path, settings: Settings
) -> None:
    """A shrinking page_count must never read as "examined, nothing found"."""
    path = write_bytes(
        tmp_path, "Dangling_Kids.pdf", one_page_with_missing_siblings("4 0 R 90 0 R 91 0 R", 3)
    )

    result = parse(path, settings)

    assert result.content["page_count"] == 1
    message = warning_with(result, WarningCode.STRUCTURE_UNRECOGNISED)
    assert "declares 3 page(s) but only 1 could be read" in message
    assert "manual review" in message
    assert [item.kind for item in result.unsupported] == ["unreadable_page"]
    assert result.unsupported[0].location == "page tree"


def test_a_page_count_that_matches_the_pages_read_is_not_reported(
    tmp_path: Path, settings: Settings
) -> None:
    """The guard must stay quiet on a healthy file, including the fixtures."""
    path = write_pdf(
        tmp_path,
        "Two_Pages.pdf",
        [["CV101 feed conveyor arrangement"], ["CV102 transfer conveyor arrangement"]],
    )

    result = parse(path, settings)

    assert result.content["page_count"] == 2
    assert result.warnings == []
    assert result.unsupported == []


def test_a_page_tree_declaring_fewer_pages_than_were_read_is_reported(
    tmp_path: Path, settings: Settings
) -> None:
    path = write_pdf(
        tmp_path,
        "Undercount.pdf",
        [["CV101 feed conveyor arrangement"], ["CV102 transfer conveyor arrangement"]],
    )
    path.write_bytes(path.read_bytes().replace(b"/Count 2", b"/Count 1"))

    result = parse(path, settings)

    assert result.content["page_count"] == 2
    assert "declares 1 page(s) but 2 were read" in warning_with(
        result, WarningCode.STRUCTURE_UNRECOGNISED
    )


# ---------------------------------------------------------------------------
# Text that does not live in the page content stream
# ---------------------------------------------------------------------------

FILLED_FIELD = "FAIL - no conveyor stopped, punch item PL3-014"
MARKUP_NOTE = "REVIEW COMMENT: GRD_DR_CLSD is not wired to the CV103 drive."


def filled_form_pdf() -> bytes:
    """Static labels in the content stream, the answers in widget /V values."""
    body = page_content(["CONVEYOR LINE 3 - TEST RECORD", "3. CV102 jam detection result:"])
    return assemble_pdf(
        [
            b"<< /Type /Catalog /Pages 2 0 R /AcroForm << /Fields [6 0 R 7 0 R] >> >>",
            b"<< /Type /Pages /Kids [4 0 R] /Count 1 >>",
            FONT_OBJECT,
            (
                b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                b"/Resources << /Font << /F1 3 0 R >> >> /Contents 5 0 R /Annots [6 0 R 7 0 R] >>"
            ),
            f"<< /Length {len(body)} >>\nstream\n".encode("ascii") + body + b"\nendstream",
            (
                b"<< /Type /Annot /Subtype /Widget /FT /Tx /T (jam_result) /P 4 0 R "
                b"/Rect [300 700 560 716] /V (" + FILLED_FIELD.encode("latin-1") + b") >>"
            ),
            (
                b"<< /Type /Annot /Subtype /FreeText /Rect [80 600 520 660] /Contents ("
                + MARKUP_NOTE.encode("latin-1")
                + b") >>"
            ),
        ]
    )


def test_form_field_values_and_markup_notes_are_recorded_rather_than_dropped(
    tmp_path: Path, settings: Settings
) -> None:
    """A filled test record must not parse as a blank one with no warning."""
    path = write_bytes(tmp_path, "Filled_Test_Record.pdf", filled_form_pdf())

    result = parse(path, settings)
    page = result.content["pages"][0]

    assert FILLED_FIELD not in page["text"]
    assert page["annotations"] == [
        {
            "annotation": 1,
            "subtype": "/Widget",
            "field": "jam_result",
            "source": "field_value",
            "text": FILLED_FIELD,
        },
        {
            "annotation": 2,
            "subtype": "/FreeText",
            "field": "",
            "source": "note",
            "text": MARKUP_NOTE,
        },
    ]

    message = warning_with(result, WarningCode.HIDDEN_CONTENT)
    assert "annotations" in message
    assert "page 1" in message
    assert [item.kind for item in result.unsupported] == ["page_with_annotation_text"]
    assert result.unsupported[0].location == "page 1"


def test_annotation_text_the_page_already_shows_is_not_reported_twice(
    tmp_path: Path, settings: Settings
) -> None:
    """A flattened form is not hidden content: the page text already holds it."""
    body = page_content(["Low air pressure trip point 39 psi"])
    path = write_bytes(
        tmp_path,
        "Flattened.pdf",
        assemble_pdf(
            [
                b"<< /Type /Catalog /Pages 2 0 R >>",
                b"<< /Type /Pages /Kids [4 0 R] /Count 1 >>",
                FONT_OBJECT,
                (
                    b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                    b"/Resources << /Font << /F1 3 0 R >> >> /Contents 5 0 R /Annots [6 0 R] >>"
                ),
                f"<< /Length {len(body)} >>\nstream\n".encode("ascii") + body + b"\nendstream",
                b"<< /Type /Annot /Subtype /Widget /FT /Tx /T (trip) /V (39 psi) >>",
            ]
        ),
    )

    result = parse(path, settings)

    assert result.content["pages"][0]["annotations"] == []
    assert codes(result) == []


def test_a_form_whose_values_reach_no_page_is_reported_as_unread(
    tmp_path: Path, settings: Settings
) -> None:
    """The field is declared on the catalog and referenced by no page /Annots."""
    body = page_content(["CONVEYOR LINE 3 - TEST RECORD", "3. CV102 jam detection result:"])
    path = write_bytes(
        tmp_path,
        "Orphan_Form.pdf",
        assemble_pdf(
            [
                b"<< /Type /Catalog /Pages 2 0 R /AcroForm << /Fields [6 0 R] >> >>",
                b"<< /Type /Pages /Kids [4 0 R] /Count 1 >>",
                FONT_OBJECT,
                (
                    b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                    b"/Resources << /Font << /F1 3 0 R >> >> /Contents 5 0 R >>"
                ),
                f"<< /Length {len(body)} >>\nstream\n".encode("ascii") + body + b"\nendstream",
                b"<< /FT /Tx /T (jam_result) /V (" + FILLED_FIELD.encode("latin-1") + b") >>",
            ]
        ),
    )

    result = parse(path, settings)

    message = warning_with(result, WarningCode.HIDDEN_CONTENT)
    assert "interactive form with 1 top-level field(s)" in message
    assert [item.kind for item in result.unsupported] == ["form_fields_not_read"]
    assert result.unsupported[0].location == "form"


# ---------------------------------------------------------------------------
# A citation must quote exactly the span it claims
# ---------------------------------------------------------------------------


def test_a_line_longer_than_one_excerpt_is_split_into_verifiable_ranges(
    tmp_path: Path, settings: Settings
) -> None:
    """``text[start:end] == excerpt`` is what makes a citation checkable."""
    tail = "TRIPS AT 39 PSI"
    line = "AIR PRESSURE INTERLOCK ILK-005 " + "x" * (MAX_EXCERPT_CHARS + 500) + " " + tail
    path = write_pdf(tmp_path, "Long_Line.pdf", [[line]])

    result = parse(path, settings)
    text = result.content["pages"][0]["text"]
    ranges = [
        anchor for anchor in result.anchors if anchor.anchor_type is AnchorType.PDF_TEXT_RANGE
    ]

    assert len(ranges) == 2
    for anchor in ranges:
        start = anchor.structured_location["start"]
        end = anchor.structured_location["end"]
        assert text[start:end] == anchor.excerpt
        assert anchor.key == f"textrange:1:{start}-{end}"
        assert anchor.display_location.endswith(f"characters {start}-{end}")
    assert ranges[0].structured_location["end"] == ranges[1].structured_location["start"]
    assert tail in ranges[1].excerpt
    assert "".join(anchor.excerpt for anchor in ranges) == text.strip()


# ---------------------------------------------------------------------------
# Text that came back as glyph codes rather than characters
# ---------------------------------------------------------------------------


def subset_font_pdf() -> bytes:
    """A subset font with a /Differences map and no /ToUnicode: what a CAD
    export looks like when the producer wrote no character mapping."""
    escaped = b"".join(b"\\%03o" % code for code in range(1, 21))
    body = b"BT\n/F1 10 Tf 1 0 0 1 56 700 Tm (" + escaped + b") Tj\nET"
    differences = " ".join(f"/uni{code:04X}" for code in range(1, 21))
    return assemble_pdf(
        [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [4 0 R] /Count 1 >>",
            (
                "<< /Type /Font /Subtype /Type1 /BaseFont /ABCDEF+CadSubset "
                f"/Encoding << /Type /Encoding /Differences [1 {differences}] >> >>"
            ).encode("ascii"),
            (
                b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                b"/Resources << /Font << /F1 3 0 R >> >> /Contents 5 0 R >>"
            ),
            f"<< /Length {len(body)} >>\nstream\n".encode("ascii") + body + b"\nendstream",
        ]
    )


def test_a_page_of_unmapped_glyph_codes_is_not_offered_as_readable_content(
    tmp_path: Path, settings: Settings
) -> None:
    """Enough characters to pass the yield check, none of them legible."""
    path = write_bytes(tmp_path, "Subset_Font.pdf", subset_font_pdf())

    result = parse(path, settings)
    anchor = result.anchors_by_key["page:1"]

    assert result.content["pages"][0]["text"].startswith("/uni0001")
    assert result.content["pages"][0]["character_count"] > MIN_PAGE_CHARACTERS

    message = warning_with(result, WarningCode.STRUCTURE_UNRECOGNISED)
    assert "unmapped glyph codes" in message
    assert [item.kind for item in result.unsupported] == ["page_text_not_legible"]
    assert anchor.evidence_kind is EvidenceKind.UNSUPPORTED_CONTENT
    assert "manual review" in anchor.expectation
    assert not [a for a in result.anchors if a.anchor_type is AnchorType.PDF_TEXT_RANGE]


def test_ordinary_text_is_not_mistaken_for_glyph_codes(settings: Settings) -> None:
    """The legibility check must not fire on the fixture it would ruin."""
    result = parse(TESTING_NOTES, settings)

    assert result.warnings == []
    assert result.anchors_by_key["page:1"].evidence_kind is EvidenceKind.DIRECT


# ---------------------------------------------------------------------------
# What an empty page is allowed to claim
# ---------------------------------------------------------------------------


def test_the_no_text_layer_warning_states_the_observation_not_a_cause(
    tmp_path: Path, settings: Settings
) -> None:
    """Asserting "image-only, needs OCR" is interpretation the parser cannot do.

    A page whose content stream could not be read looks identical here, and OCR
    of a page that carries no image returns nothing.
    """
    path = write_pdf(tmp_path, "Blank_Page.pdf", [[]])

    result = parse(path, settings)
    message = warning_with(result, WarningCode.NO_TEXT_LAYER)
    expectation = result.anchors_by_key["page:1"].expectation

    assert "cause was not established" in message
    assert "content stream could not be read" in message
    assert "No image was found in its resources" in expectation
    assert "OCR or manual review" in expectation


def test_an_empty_page_that_does_carry_an_image_says_so(tmp_path: Path, settings: Settings) -> None:
    path = write_bytes(
        tmp_path,
        "Scan_Like.pdf",
        assemble_pdf(
            [
                b"<< /Type /Catalog /Pages 2 0 R >>",
                b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
                (
                    b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                    b"/Resources << /XObject << /Im0 5 0 R >> >> /Contents 4 0 R >>"
                ),
                f"<< /Length {len(GRAPHICS_ONLY)} >>\nstream\n".encode("ascii")
                + GRAPHICS_ONLY
                + b"\nendstream",
                (
                    b"<< /Type /XObject /Subtype /Image /Width 1 /Height 1 "
                    b"/ColorSpace /DeviceGray /BitsPerComponent 8 /Length 1 >>\nstream\n"
                    b"\xff\nendstream"
                ),
            ]
        ),
    )

    result = parse(path, settings)
    expectation = result.anchors_by_key["page:1"].expectation

    assert "resources include image data" in expectation
    assert "consistent with a scanned page" in expectation
    assert "OCR or manual review" in expectation
