"""Tests for the Word (``.docx``) parser.

The happy path runs against ``fixtures/demo_project/Functional_Description.docx``,
the document that states the interlock thresholds the L5X export contradicts. The
values asserted here are the planted ground truth from ``fixtures/README.md``: the
air-pressure trip documented as "below 60 psi" against the ``GRT(..., 40.0)`` in
logic, the CV102 jam that "shall stop CV101 and CV102" and was never implemented,
and ILK-007, whose threshold is explicitly "to be confirmed" and must therefore
never harden into a technical claim.

Because the Interlock Review Agent cites this file, a paragraph is only useful if
a human can find it again, so the display location of every anchor is asserted to
name its containing section rather than a bare index.

Degenerate and hostile inputs — an empty file, a truncated package, a legacy
``.doc``, a spreadsheet wearing a ``.docx`` extension, a document with no headings
— are built inline in ``tmp_path`` and never committed.
"""

from __future__ import annotations

import re
import uuid
import zipfile
from pathlib import Path
from typing import Any

import pytest
from docx import Document
from docx.document import Document as DocumentClass
from docx.enum.style import WD_STYLE_TYPE
from docx.oxml.ns import qn
from docx.table import Table

from app.core.config import Settings
from app.core.enums import AnchorType, DocumentRole, EvidenceKind, FileKind
from app.core.errors import ParserError
from app.parsers import docx_parser as docx_parser_module
from app.parsers.base import AnchorDraft, ParseContext, Parser, ParseResult, WarningCode
from app.parsers.docx_parser import DocxParser
from app.parsers.registry import parser_for

REPO_ROOT = Path(__file__).resolve().parents[3]
DESCRIPTION_PATH = REPO_ROOT / "fixtures" / "demo_project" / "Functional_Description.docx"

FILE_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
PROJECT_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")

TITLE = "Conveyor Line 3 - Functional Description"
SECTION_3 = "3. Interlocks and Permissives"
SECTION_4 = "4. Interlock Summary"
SECTION_6 = "6. Open Items"

#: Smallest valid PNG, so an inline fixture can hold a real image.
ONE_PIXEL_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06"
    b"\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05"
    b"\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


def make_context(path: Path, settings: Settings) -> ParseContext:
    return ParseContext(
        file_id=FILE_ID,
        project_id=PROJECT_ID,
        path=path,
        filename=path.name,
        file_kind=FileKind.DOCX,
        document_role=DocumentRole.FUNCTIONAL_DESCRIPTION,
        settings=settings,
    )


def parse_path(path: Path, settings: Settings) -> ParseResult:
    return DocxParser().parse(make_context(path, settings))


def parse_bytes(
    tmp_path: Path, settings: Settings, data: bytes, name: str = "input.docx"
) -> ParseResult:
    path = tmp_path / name
    path.write_bytes(data)
    return parse_path(path, settings)


def codes(result: ParseResult) -> list[str]:
    return [warning.code for warning in result.warnings]


def unsupported_kinds(result: ParseResult) -> list[str]:
    return [item.kind for item in result.unsupported]


def paragraphs_containing(result: ParseResult, needle: str) -> list[dict[str, Any]]:
    return [row for row in result.content["paragraphs"] if needle in row["text"]]


def only_paragraph_containing(result: ParseResult, needle: str) -> dict[str, Any]:
    matches = paragraphs_containing(result, needle)
    assert len(matches) == 1, f"expected exactly one paragraph containing {needle!r}"
    return matches[0]


def anchor_signature(anchors: list[AnchorDraft]) -> list[tuple[str, str, str, str, str]]:
    """Everything about an anchor a stored citation depends on.

    ``structured_location`` is included because ``section_path`` lives there: a
    re-parse that reparented a paragraph under a different heading would otherwise
    compare equal.
    """
    return [
        (
            anchor.key,
            anchor.anchor_type.value,
            anchor.display_location,
            anchor.excerpt,
            repr(anchor.structured_location),
        )
        for anchor in anchors
    ]


def save(document: Any, path: Path) -> Path:
    document.save(str(path))
    return path


def wrap_run(paragraph: Any, run: Any, tag: str) -> Any:
    """Move ``run`` inside a new ``tag`` element in its place. Returns the wrapper."""
    wrapper = paragraph._p.makeelement(tag, {qn("w:id"): "1"})
    paragraph._p.replace(run._r, wrapper)
    wrapper.append(run._r)
    return wrapper


def append_child(element: Any, tag: str, attributes: dict[str, str] | None = None) -> Any:
    child = element.makeelement(tag, attributes or {})
    element.append(child)
    return child


def warnings_with(result: ParseResult, code: str) -> list[Any]:
    return [warning for warning in result.warnings if warning.code == code]


def unsupported_with(result: ParseResult, kind: str) -> list[Any]:
    return [item for item in result.unsupported if item.kind == kind]


@pytest.fixture
def description(settings: Settings) -> ParseResult:
    return parse_path(DESCRIPTION_PATH, settings)


# ---------------------------------------------------------------------------
# Declared interface
# ---------------------------------------------------------------------------


def test_the_parser_declares_itself_for_docx_files() -> None:
    parser = DocxParser()
    assert parser.name == "docx"
    assert parser.version == "1.1.0"
    assert parser.handles == (FileKind.DOCX,)
    assert isinstance(parser, Parser)


def test_the_registry_resolves_docx_to_this_parser() -> None:
    assert isinstance(parser_for(FileKind.DOCX), DocxParser)


def test_the_result_is_labelled_with_the_docx_artifact_kind(description: ParseResult) -> None:
    assert description.artifact_kind == FileKind.DOCX.value
    assert description.parser_name == "docx"
    assert description.parser_version == "1.1.0"


def test_content_exposes_the_documented_top_level_shape(description: ParseResult) -> None:
    assert set(description.content) == {
        "properties",
        "headings",
        "paragraphs",
        "tables",
        "outline",
    }


# ---------------------------------------------------------------------------
# Document properties
# ---------------------------------------------------------------------------


def test_document_properties_are_read_from_the_core_properties_part(
    description: ParseResult,
) -> None:
    properties = description.content["properties"]
    assert set(properties) == {"title", "author", "created", "modified", "comments", "category"}
    assert "Conveyor Line 3" in properties["title"]
    assert properties["author"] == "SYNTHETIC FIXTURE"
    assert "Invented content" in properties["comments"]
    assert properties["category"] == ""


def test_property_timestamps_are_serialised_as_iso_strings(description: ParseResult) -> None:
    """A datetime object would not survive being stored as JSON content."""
    properties = description.content["properties"]
    assert properties["created"] == "2013-12-23T23:15:00+00:00"
    assert properties["modified"] == "2013-12-23T23:15:00+00:00"


def test_an_absent_timestamp_is_none_rather_than_a_placeholder_date(
    tmp_path: Path, settings: Settings
) -> None:
    document = Document()
    element = document.core_properties._element
    for child in list(element):
        if child.tag.endswith("}created") or child.tag.endswith("}modified"):
            element.remove(child)
    document.add_heading("1. Scope", level=1)
    result = parse_path(save(document, tmp_path / "undated.docx"), settings)

    assert result.content["properties"]["created"] is None
    assert result.content["properties"]["modified"] is None


# ---------------------------------------------------------------------------
# Headings and outline
# ---------------------------------------------------------------------------


def test_the_numbered_sections_are_recorded_as_headings_in_document_order(
    description: ParseResult,
) -> None:
    headings = description.content["headings"]
    assert [heading["text"] for heading in headings] == [
        TITLE,
        "1. Scope",
        "2. Operating Modes",
        SECTION_3,
        SECTION_4,
        "5. Alarms",
        SECTION_6,
    ]
    assert [heading["level"] for heading in headings] == [0, 1, 1, 1, 1, 1, 1]
    assert [heading["index"] for heading in headings] == [0, 2, 4, 9, 14, 15, 17]


def test_each_heading_carries_the_titles_of_its_ancestors(description: ParseResult) -> None:
    by_text = {heading["text"]: heading for heading in description.content["headings"]}
    assert by_text[TITLE]["path"] == []
    assert by_text[SECTION_3]["path"] == [TITLE]


def test_the_outline_nests_the_numbered_sections_under_the_document_title(
    description: ParseResult,
) -> None:
    outline = description.content["outline"]
    assert len(outline) == 1
    root = outline[0]
    assert root["text"] == TITLE
    assert root["level"] == 0
    assert root["index"] == 0
    assert [child["text"] for child in root["children"]] == [
        "1. Scope",
        "2. Operating Modes",
        SECTION_3,
        SECTION_4,
        "5. Alarms",
        SECTION_6,
    ]
    assert all(child["children"] == [] for child in root["children"])


def test_a_deeper_heading_nests_under_the_section_above_it(
    tmp_path: Path, settings: Settings
) -> None:
    document = Document()
    document.add_heading("1. Scope", level=1)
    document.add_heading("1.1 Equipment", level=2)
    document.add_paragraph("CV101 is the feed conveyor.")
    document.add_heading("2. Modes", level=1)
    result = parse_path(save(document, tmp_path / "nested.docx"), settings)

    outline = result.content["outline"]
    assert [node["text"] for node in outline] == ["1. Scope", "2. Modes"]
    assert [child["text"] for child in outline[0]["children"]] == ["1.1 Equipment"]

    body = only_paragraph_containing(result, "CV101")
    assert body["section_path"] == ["1. Scope", "1.1 Equipment"]


# ---------------------------------------------------------------------------
# Paragraphs
# ---------------------------------------------------------------------------


def test_paragraph_indices_are_zero_based_and_gapless(description: ParseResult) -> None:
    """The literal 19 is the fixture's body-paragraph count; deriving it from the

    parser's own output would let a lost paragraph renumber itself into agreement.
    """
    paragraphs = description.content["paragraphs"]
    assert [row["index"] for row in paragraphs] == list(range(19))
    assert description.summary["paragraph_count"] == 19


def test_table_cell_text_is_not_folded_into_the_body_paragraphs(
    description: ParseResult,
) -> None:
    """Body paragraph indices are what anchors address; cells are addressed separately."""
    assert paragraphs_containing(description, "ILK-001") == []
    assert any("ILK-001" == entry["text"] for entry in description.content["tables"][0]["cells"])


def test_every_paragraph_records_the_documented_field_set(description: ParseResult) -> None:
    for row in description.content["paragraphs"]:
        assert set(row) == {
            "index",
            "text",
            "style",
            "heading_level",
            "list_level",
            "is_list_item",
            "section_path",
        }


def test_the_documented_air_pressure_threshold_is_locatable_under_section_three(
    description: ParseResult,
) -> None:
    """The Interlock Review Agent cites exactly this paragraph against GRT(..., 40.0)."""
    paragraph = only_paragraph_containing(description, "below 60 psi")

    assert paragraph["index"] == 12
    assert paragraph["heading_level"] is None
    assert paragraph["section_path"] == [TITLE, SECTION_3]

    anchor = description.anchors_by_key["paragraph:12"]
    assert anchor.anchor_type is AnchorType.DOCX_PARAGRAPH
    assert anchor.display_location == f"Functional_Description.docx / {SECTION_3} / paragraph 12"
    assert "below 60 psi" in anchor.excerpt
    assert anchor.structured_location["section_path"] == [TITLE, SECTION_3]


def test_the_jam_interlock_action_is_recorded_verbatim(description: ParseResult) -> None:
    """ILK-003 is documented here and implemented nowhere; the wording is the evidence."""
    paragraph = only_paragraph_containing(description, "shall stop CV101 and CV102")

    assert paragraph["section_path"] == [TITLE, SECTION_3]
    anchor = description.anchors_by_key[f"paragraph:{paragraph['index']}"]
    assert "shall stop CV101 and CV102" in anchor.excerpt
    assert SECTION_3 in anchor.display_location


def test_the_open_item_for_ilk_007_keeps_its_unconfirmed_threshold(
    description: ParseResult,
) -> None:
    """Recording "to be confirmed" is what stops ILK-007 becoming a technical claim."""
    paragraph = only_paragraph_containing(description, "ILK-007")

    assert paragraph["section_path"] == [TITLE, SECTION_6]
    assert "to be confirmed" in paragraph["text"]
    assert "not yet defined" in paragraph["text"]
    assert not re.search(r"\d+\s*psi", paragraph["text"])

    anchor = description.anchors_by_key[f"paragraph:{paragraph['index']}"]
    assert SECTION_6 in anchor.display_location
    assert "to be confirmed" in anchor.excerpt


def test_the_bullet_list_under_operating_modes_is_flagged_without_inventing_a_level(
    description: ParseResult,
) -> None:
    items = [row for row in description.content["paragraphs"] if row["is_list_item"]]

    assert [row["text"].split(" -")[0] for row in items] == ["Off", "Manual", "Auto"]
    assert [row["style"] for row in items] == ["List Bullet"] * 3
    assert [row["section_path"] for row in items] == [[TITLE, "2. Operating Modes"]] * 3
    assert [row["list_level"] for row in items] == [None, None, None]
    assert description.summary["list_item_count"] == 3


def test_an_explicit_numbering_level_is_recorded_when_the_paragraph_states_one(
    tmp_path: Path, settings: Settings
) -> None:
    document = Document()
    document.add_heading("1. Scope", level=1)
    paragraph = document.add_paragraph("Second-level bullet.", style="List Bullet")
    numPr = paragraph._p.get_or_add_pPr().get_or_add_numPr()
    numPr.get_or_add_ilvl().val = 2
    result = parse_path(save(document, tmp_path / "numbered.docx"), settings)

    row = only_paragraph_containing(result, "Second-level bullet")
    assert row["is_list_item"] is True
    assert row["list_level"] == 2


def test_a_heading_is_not_also_reported_as_a_list_item(tmp_path: Path, settings: Settings) -> None:
    """A numbered heading is structure to navigate by, not a bullet."""
    document = Document()
    heading = document.add_heading("1. Scope", level=1)
    heading._p.get_or_add_pPr().get_or_add_numPr().get_or_add_ilvl().val = 0
    result = parse_path(save(document, tmp_path / "numbered_heading.docx"), settings)

    row = only_paragraph_containing(result, "1. Scope")
    assert row["heading_level"] == 1
    assert row["is_list_item"] is False


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------


def test_the_interlock_summary_table_has_four_columns_and_five_data_rows(
    description: ParseResult,
) -> None:
    tables = description.content["tables"]
    assert len(tables) == 1

    table = tables[0]
    assert table["index"] == 0
    assert table["columns"] == 4
    assert table["rows"] == 6
    assert table["header"] == ["Interlock", "Condition", "Action", "Fail-safe"]
    assert len(table["records"]) == 5


def test_table_records_are_keyed_by_the_header_row(description: ParseResult) -> None:
    records = description.content["tables"][0]["records"]

    assert [record["row"] for record in records] == [1, 2, 3, 4, 5]
    assert [record["values"]["Interlock"] for record in records] == [
        "ILK-001",
        "ILK-002",
        "ILK-003",
        "ILK-004",
        "ILK-005",
    ]
    assert records[2]["values"] == {
        "Interlock": "ILK-003",
        "Condition": "CV102 jam",
        "Action": "Stop CV101 and CV102",
        "Fail-safe": "Energise to trip",
    }
    assert records[4]["values"]["Condition"] == "Air pressure below 60 psi"


def test_the_table_is_located_in_the_section_that_introduces_it(
    description: ParseResult,
) -> None:
    assert description.content["tables"][0]["section_path"] == [TITLE, SECTION_4]


def test_every_grid_position_is_addressable_by_row_and_column(
    description: ParseResult,
) -> None:
    cells = description.content["tables"][0]["cells"]

    assert len(cells) == 24
    assert [(cell["row"], cell["column"]) for cell in cells[:5]] == [
        (0, 0),
        (0, 1),
        (0, 2),
        (0, 3),
        (1, 0),
    ]
    assert description.summary["table_cell_count"] == 24


def test_a_table_cell_anchor_names_its_column_and_its_section(
    description: ParseResult,
) -> None:
    anchor = description.anchors_by_key["tablecell:0:3:2"]

    assert anchor.anchor_type is AnchorType.DOCX_TABLE_CELL
    assert anchor.excerpt == "Stop CV101 and CV102"
    assert anchor.display_location == (
        f"Functional_Description.docx / {SECTION_4} / table 0 row 3 column 2 (Action)"
    )
    assert anchor.structured_location == {
        "table_index": 0,
        "row": 3,
        "column": 2,
        "header": "Action",
        "section_path": [TITLE, SECTION_4],
    }


def test_a_merged_cell_is_reported_and_anchored_only_once(
    tmp_path: Path, settings: Settings
) -> None:
    document = Document()
    document.add_heading("1. Scope", level=1)
    table = document.add_table(rows=2, cols=2)
    table.rows[0].cells[0].text = "Interlock"
    table.rows[0].cells[1].text = "Condition"
    row = table.rows[1]
    row.cells[0].merge(row.cells[1]).text = "spans both columns"
    result = parse_path(save(document, tmp_path / "merged.docx"), settings)

    assert WarningCode.MERGED_CELLS in codes(result)
    cells = result.content["tables"][0]["cells"]
    assert [cell["text"] for cell in cells if cell["row"] == 1] == [
        "spans both columns",
        "spans both columns",
    ]
    keys = [anchor.key for anchor in result.anchors if anchor.key.startswith("tablecell:")]
    assert "tablecell:0:1:0" in keys
    assert "tablecell:0:1:1" not in keys


def test_a_blank_header_cell_gets_a_positional_key_and_is_reported(
    tmp_path: Path, settings: Settings
) -> None:
    """Two records must never collapse into one because a header label was missing."""
    document = Document()
    document.add_heading("1. Scope", level=1)
    table = document.add_table(rows=2, cols=3)
    table.rows[0].cells[0].text = "Interlock"
    table.rows[0].cells[2].text = "Interlock"
    table.rows[1].cells[0].text = "ILK-001"
    table.rows[1].cells[1].text = "unlabelled"
    table.rows[1].cells[2].text = "duplicate label"
    result = parse_path(save(document, tmp_path / "bad_header.docx"), settings)

    assert WarningCode.STRUCTURE_UNRECOGNISED in codes(result)
    assert result.content["tables"][0]["records"][0]["values"] == {
        "Interlock": "ILK-001",
        "column 1": "unlabelled",
        "Interlock (2)": "duplicate label",
    }


# ---------------------------------------------------------------------------
# Anchors
# ---------------------------------------------------------------------------


def test_every_anchor_carries_the_source_label_and_a_docx_anchor_type(
    description: ParseResult,
) -> None:
    assert description.anchors
    for anchor in description.anchors:
        assert anchor.source_label == "Functional_Description.docx"
        assert anchor.anchor_type in {
            AnchorType.DOCX_HEADING,
            AnchorType.DOCX_PARAGRAPH,
            AnchorType.DOCX_TABLE_CELL,
        }
        assert anchor.evidence_kind is EvidenceKind.DIRECT
        assert anchor.excerpt.strip()
        assert anchor.display_location.startswith("Functional_Description.docx / ")


def test_anchor_keys_follow_the_documented_scheme_and_are_unique(
    description: ParseResult,
) -> None:
    keys = [anchor.key for anchor in description.anchors]
    pattern = re.compile(r"^(heading:\d+|paragraph:\d+|tablecell:\d+:\d+:\d+)$")

    assert len(set(keys)) == len(keys)
    assert all(pattern.match(key) for key in keys)
    # 7 headings + 12 non-empty body paragraphs + 24 table cells.
    assert len(keys) == 43
    assert description.summary["anchor_count"] == 43


def test_no_anchor_leaks_an_absolute_path(description: ParseResult) -> None:
    """An anchor holding a server path would be neither portable nor deterministic."""
    root = str(REPO_ROOT)
    for anchor in description.anchors:
        assert root not in anchor.display_location
        assert root not in repr(anchor.structured_location)


def test_an_archive_member_label_reaches_every_anchor(settings: Settings) -> None:
    context = ParseContext(
        file_id=FILE_ID,
        project_id=PROJECT_ID,
        path=DESCRIPTION_PATH,
        filename=DESCRIPTION_PATH.name,
        file_kind=FileKind.DOCX,
        document_role=DocumentRole.FUNCTIONAL_DESCRIPTION,
        settings=settings,
        archive_member_path="docs/Functional_Description.docx",
    )
    result = DocxParser().parse(context)

    label = "Functional_Description.docx (docs/Functional_Description.docx)"
    assert {anchor.source_label for anchor in result.anchors} == {label}
    assert result.anchors_by_key["paragraph:12"].display_location.startswith(label)


def test_an_empty_paragraph_is_recorded_but_not_anchored(
    tmp_path: Path, settings: Settings
) -> None:
    """Indices must not shift, yet a blank paragraph gives a finding nothing to quote."""
    document = Document()
    document.add_heading("1. Scope", level=1)
    document.add_paragraph("")
    document.add_paragraph("CV101 is the feed conveyor.")
    result = parse_path(save(document, tmp_path / "blank.docx"), settings)

    assert [row["index"] for row in result.content["paragraphs"]] == [0, 1, 2]
    assert result.content["paragraphs"][1]["text"] == ""
    assert result.summary["empty_paragraph_count"] == 1
    assert [anchor.key for anchor in result.anchors] == ["heading:0", "paragraph:2"]


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------


def test_the_summary_reports_shape_without_needing_the_content(
    description: ParseResult,
) -> None:
    assert description.summary == {
        "paragraph_count": 19,
        "empty_paragraph_count": 0,
        "heading_count": 7,
        "list_item_count": 3,
        "table_count": 1,
        "table_cell_count": 24,
        "character_count": 2010,
        "anchor_count": 43,
        "warning_count": 0,
        "unsupported_count": 0,
        "has_headings": True,
        "title": "Conveyor Line 3 Functional Description (SYNTHETIC)",
        "top_level_headings": [TITLE],
    }


def test_the_clean_fixture_produces_no_warnings_and_no_unsupported_items(
    description: ParseResult,
) -> None:
    """It is plain prose and one table, so anything reported here would be noise."""
    assert description.warnings == []
    assert description.unsupported == []


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_reparsing_the_same_bytes_produces_identical_output(settings: Settings) -> None:
    first = parse_path(DESCRIPTION_PATH, settings)
    second = parse_path(DESCRIPTION_PATH, settings)

    assert first.content == second.content
    assert first.summary == second.summary
    assert anchor_signature(first.anchors) == anchor_signature(second.anchors)
    assert [warning.as_dict() for warning in first.warnings] == [
        warning.as_dict() for warning in second.warnings
    ]
    assert [item.as_dict() for item in first.unsupported] == [
        item.as_dict() for item in second.unsupported
    ]
    assert [anchor.content_hash for anchor in first.anchors] == [
        anchor.content_hash for anchor in second.anchors
    ]


def test_a_reparse_by_a_separate_parser_instance_agrees(settings: Settings) -> None:
    """Anchors persisted by one worker have to resolve against another's parse."""
    first = DocxParser().parse(make_context(DESCRIPTION_PATH, settings))
    second = DocxParser().parse(make_context(DESCRIPTION_PATH, settings))

    assert anchor_signature(first.anchors) == anchor_signature(second.anchors)


# ---------------------------------------------------------------------------
# Content the parser deliberately does not read
# ---------------------------------------------------------------------------


def test_an_image_is_recorded_as_unsupported_rather_than_dropped(
    tmp_path: Path, settings: Settings
) -> None:
    image = tmp_path / "diagram.png"
    image.write_bytes(ONE_PIXEL_PNG)
    document = Document()
    document.add_heading("1. Scope", level=1)
    document.add_picture(str(image))
    result = parse_path(save(document, tmp_path / "with_image.docx"), settings)

    assert unsupported_kinds(result) == ["image"]
    item = result.unsupported[0]
    assert "Occurrences: 1" in item.detail
    assert "1. Scope" in item.location


def test_a_footnote_reference_is_recorded_as_unsupported(
    tmp_path: Path, settings: Settings
) -> None:
    document = Document()
    document.add_heading("1. Scope", level=1)
    paragraph = document.add_paragraph("Air pressure is monitored.")
    run = paragraph.add_run()
    reference = run._r.makeelement(qn("w:footnoteReference"), {qn("w:id"): "2"})
    run._r.append(reference)
    result = parse_path(save(document, tmp_path / "footnote.docx"), settings)

    assert "footnote" in unsupported_kinds(result)
    assert "paragraph 1" in result.unsupported[0].location


def test_a_header_with_text_is_reported_because_it_is_not_a_body_paragraph(
    tmp_path: Path, settings: Settings
) -> None:
    document = Document()
    document.add_heading("1. Scope", level=1)
    document.sections[0].header.paragraphs[0].text = "UNCONTROLLED COPY"
    result = parse_path(save(document, tmp_path / "header.docx"), settings)

    assert "header" in unsupported_kinds(result)
    assert "UNCONTROLLED COPY" in result.unsupported[0].location
    assert paragraphs_containing(result, "UNCONTROLLED COPY") == []


def test_text_hidden_in_a_tracked_insertion_is_warned_about(
    tmp_path: Path, settings: Settings
) -> None:
    """python-docx drops revision-marked runs, so the recorded paragraph is short of text."""
    document = Document()
    document.add_heading("1. Scope", level=1)
    paragraph = document.add_paragraph("Trip at ")
    run = paragraph.add_run("40 psi")
    insertion = paragraph._p.makeelement(qn("w:ins"), {qn("w:id"): "1"})
    paragraph._p.replace(run._r, insertion)
    insertion.append(run._r)
    result = parse_path(save(document, tmp_path / "tracked.docx"), settings)

    row = only_paragraph_containing(result, "Trip at")
    assert "40 psi" not in row["text"]
    hidden = [warning for warning in result.warnings if warning.code == WarningCode.HIDDEN_CONTENT]
    assert len(hidden) == 1
    assert "tracked insertion" in hidden[0].message
    assert "paragraph 1" in hidden[0].location


# ---------------------------------------------------------------------------
# Degenerate documents
# ---------------------------------------------------------------------------


def test_a_document_with_no_body_content_is_reported_as_empty(
    tmp_path: Path, settings: Settings
) -> None:
    result = parse_path(save(Document(), tmp_path / "empty.docx"), settings)

    assert codes(result) == [WarningCode.EMPTY_DOCUMENT]
    assert result.content["paragraphs"] == []
    assert result.content["tables"] == []
    assert result.content["headings"] == []
    assert result.content["outline"] == []
    assert result.anchors == []
    assert result.summary["paragraph_count"] == 0
    assert result.summary["has_headings"] is False


def test_a_flat_document_with_no_headings_is_reported_as_unrecognised_structure(
    tmp_path: Path, settings: Settings
) -> None:
    """Without headings a paragraph can only be cited by index, which is worth saying."""
    document = Document()
    document.add_paragraph("The line trips below 60 psi.")
    document.add_paragraph("The jam detector stops CV101.")
    result = parse_path(save(document, tmp_path / "flat.docx"), settings)

    assert codes(result) == [WarningCode.STRUCTURE_UNRECOGNISED]
    assert result.content["headings"] == []
    assert result.content["paragraphs"][0]["section_path"] == []
    assert result.anchors_by_key["paragraph:0"].display_location == "flat.docx / paragraph 0"
    assert result.summary["has_headings"] is False


def test_a_document_with_only_a_table_is_neither_empty_nor_headed(
    tmp_path: Path, settings: Settings
) -> None:
    document = Document()
    table = document.add_table(rows=2, cols=1)
    table.rows[0].cells[0].text = "Interlock"
    table.rows[1].cells[0].text = "ILK-001"
    result = parse_path(save(document, tmp_path / "table_only.docx"), settings)

    assert codes(result) == [WarningCode.STRUCTURE_UNRECOGNISED]
    assert result.content["tables"][0]["records"] == [
        {"row": 1, "values": {"Interlock": "ILK-001"}}
    ]
    assert result.anchors_by_key["tablecell:0:1:0"].display_location == (
        "table_only.docx / table 0 row 1 column 0 (Interlock)"
    )


# ---------------------------------------------------------------------------
# Hostile and malformed input
# ---------------------------------------------------------------------------


def test_an_empty_file_raises_a_parser_error_naming_the_cause(
    tmp_path: Path, settings: Settings
) -> None:
    with pytest.raises(ParserError) as excinfo:
        parse_bytes(tmp_path, settings, b"", name="zero_bytes.docx")

    assert excinfo.value.details["reason"] == "empty_file"
    assert excinfo.value.code == "parser_error"
    assert excinfo.value.status_code == 422


def test_plain_text_wearing_a_docx_extension_raises_a_parser_error(
    tmp_path: Path, settings: Settings
) -> None:
    with pytest.raises(ParserError) as excinfo:
        parse_bytes(tmp_path, settings, b"Interlock schedule, not a Word document.\n")

    assert excinfo.value.details["reason"] == "not_a_zip"


def test_a_legacy_ole_document_is_identified_rather_than_reported_as_corrupt(
    tmp_path: Path, settings: Settings
) -> None:
    """A .doc and an encrypted .docx share this header; both need re-saving, not repair."""
    with pytest.raises(ParserError) as excinfo:
        parse_bytes(tmp_path, settings, b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64)

    assert excinfo.value.details["reason"] == "ole2_container"
    assert "password-protected" in excinfo.value.message


def test_a_truncated_package_raises_a_parser_error_not_a_zip_error(
    tmp_path: Path, settings: Settings
) -> None:
    intact = DESCRIPTION_PATH.read_bytes()

    with pytest.raises(ParserError) as excinfo:
        parse_bytes(tmp_path, settings, intact[: len(intact) // 2], name="truncated.docx")

    assert excinfo.value.details["reason"] == "not_an_opc_package"


def test_a_zip_that_is_not_a_word_package_raises_a_parser_error(
    tmp_path: Path, settings: Settings
) -> None:
    path = tmp_path / "not_word.docx"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("readme.txt", "just a zip")

    with pytest.raises(ParserError) as excinfo:
        parse_path(path, settings)

    assert excinfo.value.details["reason"] in {"not_an_opc_package", "not_a_word_package"}


def test_a_missing_file_raises_a_parser_error(tmp_path: Path, settings: Settings) -> None:
    with pytest.raises(ParserError) as excinfo:
        parse_path(tmp_path / "absent.docx", settings)

    assert excinfo.value.details["reason"] == "unreadable"


def test_a_package_that_expands_far_beyond_its_size_is_refused(
    tmp_path: Path, settings: Settings
) -> None:
    """A .docx is a ZIP of XML, and both already have limits; this one is inside them.

    The body part here is a few kilobytes on disk and eight megabytes expanded. The
    check reads only the central directory, so nothing is inflated to find out.
    """
    path = tmp_path / "bomb.docx"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", "A" * 8_000_000)

    with pytest.raises(ParserError) as excinfo:
        parse_path(path, settings)

    assert excinfo.value.details["reason"] == "compression_ratio"
    assert path.stat().st_size < 100_000


def test_a_body_part_above_the_xml_limit_is_refused(tmp_path: Path, settings: Settings) -> None:
    """Stored rather than deflated, so it is the declared size and not the ratio that bites."""
    strict = settings.model_copy(update={"max_xml_bytes": 1024})
    path = tmp_path / "huge_body.docx"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", "B" * 4096)

    with pytest.raises(ParserError) as excinfo:
        parse_path(path, strict)

    assert excinfo.value.details["reason"] == "body_part_too_large"


def test_the_committed_fixture_passes_the_package_guard(settings: Settings) -> None:
    """The guard must refuse bombs without refusing a real document."""
    assert parse_path(DESCRIPTION_PATH, settings).summary["paragraph_count"] == 19


def build_long_document(path: Path, paragraphs: int) -> Path:
    """The committed fixture with ``paragraphs`` extra paragraphs in its body."""
    padding = "<w:p><w:r><w:t>x</w:t></w:r></w:p>" * paragraphs
    with (
        zipfile.ZipFile(DESCRIPTION_PATH) as source,
        zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as target,
    ):
        for item in source.infolist():
            data = source.read(item.filename)
            if item.filename == "word/document.xml":
                body = data.decode("utf-8").replace("</w:body>", padding + "</w:body>")
                data = body.encode("utf-8")
            target.writestr(item, data)
    return path


def test_the_body_is_never_enumerated_through_the_quadratic_xpath_union(
    tmp_path: Path, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bound on how much is read is worthless if enumerating the body is quadratic.

    ``Document.iter_inner_content()`` selects the body's paragraphs and tables with
    an XPath union whose cost grows with the square of the number of children — so
    it is paid in full before a single paragraph is examined, and regardless of any
    cap. The parser walks the body's children directly instead.

    Asserted by making the quadratic call fail rather than by timing the parse: a
    wall-clock ceiling is machine- and load-dependent, and a test that fails
    because the machine was busy teaches nobody anything.
    """

    def forbidden(*_args: object, **_kwargs: object) -> object:
        raise AssertionError(
            "the parser enumerated the body via iter_inner_content(), whose cost is "
            "quadratic in the number of body children"
        )

    monkeypatch.setattr(DocumentClass, "iter_inner_content", forbidden, raising=True)

    result = parse_path(build_long_document(tmp_path / "long.docx", 2_000), settings)
    assert result.summary["paragraph_count"] > 0


def test_a_body_past_the_cap_reads_exactly_the_cap_and_says_so(
    tmp_path: Path, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cap is a real bound on what is read, and the shortfall is disclosed.

    The cap is lowered for the test rather than building a 20,000-paragraph
    document: the behaviour under test is the cap, not the parser's throughput.
    """
    monkeypatch.setattr(docx_parser_module, "_MAX_BODY_ITEMS", 25)
    result = parse_path(build_long_document(tmp_path / "long.docx", 500), settings)

    read = result.summary["paragraph_count"] + result.summary["table_count"]
    assert read == 25
    assert WarningCode.TRUNCATED in codes(result)
    truncated = warnings_with(result, WarningCode.TRUNCATED)
    assert "not read" in truncated[0].message


def test_a_body_longer_than_the_cap_is_truncated_loudly(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    monkeypatch.setattr(docx_parser_module, "_MAX_BODY_ITEMS", 3)
    result = parse_path(DESCRIPTION_PATH, settings)

    assert result.summary["paragraph_count"] == 3
    truncated = warnings_with(result, WarningCode.TRUNCATED)
    assert len(truncated) == 1
    assert "17" in truncated[0].message  # 19 paragraphs + 1 table, less the 3 read
    assert "not read" in truncated[0].message


def test_citations_beyond_the_anchor_cap_are_reported_not_silently_dropped(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    monkeypatch.setattr(docx_parser_module, "_MAX_ANCHORS", 2)
    result = parse_path(DESCRIPTION_PATH, settings)

    assert len(result.anchors) == 2
    assert result.summary["anchor_count"] == 2
    truncated = warnings_with(result, WarningCode.TRUNCATED)
    assert len(truncated) == 1
    assert "41" in truncated[0].message
    # The content is still complete: only the citations were capped.
    assert result.summary["paragraph_count"] == 19


def test_a_parser_error_never_repeats_the_absolute_path(tmp_path: Path, settings: Settings) -> None:
    """Stored parse failures are shown to users; they must not describe the server."""
    path = tmp_path / "truncated.docx"
    intact = DESCRIPTION_PATH.read_bytes()
    path.write_bytes(intact[: len(intact) // 2])

    with pytest.raises(ParserError) as excinfo:
        parse_path(path, settings)

    rendered = excinfo.value.message + repr(excinfo.value.details)
    assert str(tmp_path) not in rendered
    assert path.as_posix() not in rendered


# ---------------------------------------------------------------------------
# Text the extraction drops
# ---------------------------------------------------------------------------


def build_tracked_cell_table(path: Path) -> Path:
    """A 2x2 interlock table whose condition cell was edited under track changes."""
    document = Document()
    document.add_heading("1. Scope", level=1)
    table = document.add_table(rows=2, cols=2)
    table.rows[0].cells[0].text = "Interlock"
    table.rows[0].cells[1].text = "Condition"
    table.rows[1].cells[0].text = "ILK-005"
    paragraph = table.rows[1].cells[1].paragraphs[0]
    paragraph.text = "Air pressure below "
    wrap_run(paragraph, paragraph.add_run("40"), qn("w:ins"))
    wrap_run(paragraph, paragraph.add_run("60"), qn("w:del"))
    paragraph.add_run(" psi")
    return save(document, path)


def test_a_tracked_change_inside_a_table_cell_is_warned_about(
    tmp_path: Path, settings: Settings
) -> None:
    """The same edit in a body paragraph warns; inside a cell it used to be silent.

    Both thresholds are destroyed by the extraction, and the mutilated string is
    published as a table-cell excerpt an agent would read as a verbatim quote — so
    the record has to say the text is incomplete.
    """
    result = parse_path(build_tracked_cell_table(tmp_path / "tracked_cell.docx"), settings)

    record = result.content["tables"][0]["records"][0]["values"]
    assert record["Condition"] == "Air pressure below  psi"
    assert result.anchors_by_key["tablecell:0:1:1"].excerpt == "Air pressure below  psi"

    hidden = warnings_with(result, WarningCode.HIDDEN_CONTENT)
    assert [warning.location for warning in hidden] == [
        "tracked_cell.docx / 1. Scope / table 0 row 1 column 1",
        "tracked_cell.docx / 1. Scope / table 0 row 1 column 1",
    ]
    assert "tracked insertion" in hidden[0].message
    assert "tracked deletion" in hidden[1].message


def test_a_merged_cell_reports_its_hidden_text_only_once(
    tmp_path: Path, settings: Settings
) -> None:
    """The cell is anchored once, so it must not be warned about once per position."""
    document = Document()
    document.add_heading("1. Scope", level=1)
    table = document.add_table(rows=2, cols=2)
    table.rows[0].cells[0].text = "Interlock"
    table.rows[0].cells[1].text = "Condition"
    merged = table.rows[1].cells[0].merge(table.rows[1].cells[1])
    paragraph = merged.paragraphs[0]
    paragraph.text = "Trip at "
    wrap_run(paragraph, paragraph.add_run("40 psi"), qn("w:ins"))
    result = parse_path(save(document, tmp_path / "merged_tracked.docx"), settings)

    assert len(warnings_with(result, WarningCode.HIDDEN_CONTENT)) == 1


def test_a_content_control_inside_a_table_cell_is_warned_about(
    tmp_path: Path, settings: Settings
) -> None:
    document = Document()
    document.add_heading("1. Scope", level=1)
    table = document.add_table(rows=2, cols=1)
    table.rows[0].cells[0].text = "Condition"
    paragraph = table.rows[1].cells[0].paragraphs[0]
    paragraph.text = "Air pressure below "
    wrap_run(paragraph, paragraph.add_run("60 psi"), qn("w:sdt"))
    result = parse_path(save(document, tmp_path / "sdt_cell.docx"), settings)

    hidden = warnings_with(result, WarningCode.HIDDEN_CONTENT)
    assert len(hidden) == 1
    assert "content control" in hidden[0].message
    assert "table 0 row 1 column 0" in hidden[0].location


@pytest.mark.parametrize(
    ("tag", "label"),
    [
        (qn("w:del"), "tracked deletion"),
        (qn("w:smartTag"), "smart tag"),
        (qn("w:sdt"), "content control"),
    ],
)
def test_each_paragraph_wrapper_that_hides_text_is_named_in_the_warning(
    tmp_path: Path, settings: Settings, tag: str, label: str
) -> None:
    document = Document()
    document.add_heading("1. Scope", level=1)
    paragraph = document.add_paragraph("Trip at ")
    wrap_run(paragraph, paragraph.add_run("40 psi"), tag)
    result = parse_path(save(document, tmp_path / "wrapped.docx"), settings)

    assert "40 psi" not in only_paragraph_containing(result, "Trip at")["text"]
    hidden = warnings_with(result, WarningCode.HIDDEN_CONTENT)
    assert len(hidden) == 1
    assert label in hidden[0].message
    assert "paragraph 1" in hidden[0].location


def test_a_field_result_is_reported_as_missing_rather_than_as_visible(
    tmp_path: Path, settings: Settings
) -> None:
    """The cached result of a field is exactly the text that is dropped.

    Describing it as "visible" would tell a reviewer the revision number was
    captured when it is the one thing that was not.
    """
    document = Document()
    document.add_heading("1. Scope", level=1)
    paragraph = document.add_paragraph("Revision: ")
    field = paragraph._p.makeelement(qn("w:fldSimple"), {qn("w:instr"): " DOCPROPERTY Rev "})
    run = paragraph.add_run("REV 4 - 40 psi")
    paragraph._p.replace(run._r, field)
    field.append(run._r)
    result = parse_path(save(document, tmp_path / "field.docx"), settings)

    assert only_paragraph_containing(result, "Revision")["text"] == "Revision:"
    detail = unsupported_with(result, "field_code")[0].detail
    assert "not part of the extracted paragraph text" in detail
    hidden = warnings_with(result, WarningCode.HIDDEN_CONTENT)
    assert len(hidden) == 1
    assert "field result" in hidden[0].message
    assert "paragraph 1" in hidden[0].location


# ---------------------------------------------------------------------------
# Body-level containers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tag", "rendered"),
    [
        (qn("w:customXml"), "w:customXml"),
        (qn("w:sdt"), "w:sdt"),
        (qn("w:ins"), "w:ins"),
    ],
)
def test_a_body_container_that_swallows_a_paragraph_is_reported(
    tmp_path: Path, settings: Settings, tag: str, rendered: str
) -> None:
    """These are all valid direct children of ``w:body`` and none of them is read.

    ``iter_inner_content()`` yields only ``w:p`` and ``w:tbl``, so a paragraph
    inside one of these vanishes; a result that looks byte-for-byte like a
    complete parse is the worst possible failure mode.
    """
    document = Document()
    document.add_heading("1. Scope", level=1)
    paragraph = document.add_paragraph("Air pressure trips below 60 psi.")
    body = document.element.body
    wrapper = body.makeelement(tag, {})
    body.replace(paragraph._p, wrapper)
    wrapper.append(paragraph._p)
    result = parse_path(save(document, tmp_path / "container.docx"), settings)

    assert [row["text"] for row in result.content["paragraphs"]] == ["1. Scope"]
    hidden = warnings_with(result, WarningCode.HIDDEN_CONTENT)
    assert len(hidden) == 1
    assert f"<{rendered}>" in hidden[0].message
    assert "absent from this record" in hidden[0].message


def test_an_imported_alt_chunk_is_reported_even_though_it_holds_no_word_paragraphs(
    tmp_path: Path, settings: Settings
) -> None:
    """Insert > Object > Text from File produces this, and its content is a separate part."""
    document = Document()
    document.add_heading("1. Scope", level=1)
    append_child(document.element.body, qn("w:altChunk"), {qn("r:id"): "rId99"})
    result = parse_path(save(document, tmp_path / "altchunk.docx"), settings)

    hidden = warnings_with(result, WarningCode.HIDDEN_CONTENT)
    assert len(hidden) == 1
    assert "<w:altChunk>" in hidden[0].message
    assert "imported content chunk" in hidden[0].message


def test_an_unrecognised_body_container_is_reported_by_its_tag_name(
    tmp_path: Path, settings: Settings
) -> None:
    """A container this parser has never heard of must not be silent by default."""
    document = Document()
    document.add_heading("1. Scope", level=1)
    append_child(document.element.body, "{http://example.invalid/future}block")
    result = parse_path(save(document, tmp_path / "future.docx"), settings)

    hidden = warnings_with(result, WarningCode.HIDDEN_CONTENT)
    assert len(hidden) == 1
    assert "{http://example.invalid/future}block" in hidden[0].message


def test_section_properties_and_bookmarks_are_not_reported_as_unread_content(
    tmp_path: Path, settings: Settings
) -> None:
    """``w:sectPr`` and range markers carry no text, so flagging them would be noise."""
    document = Document()
    document.add_heading("1. Scope", level=1)
    append_child(document.element.body, qn("w:bookmarkStart"), {qn("w:id"): "0", qn("w:name"): "x"})
    append_child(document.element.body, qn("w:bookmarkEnd"), {qn("w:id"): "0"})
    result = parse_path(save(document, tmp_path / "markers.docx"), settings)

    assert warnings_with(result, WarningCode.HIDDEN_CONTENT) == []


# ---------------------------------------------------------------------------
# Heading detection beyond the built-in style names
# ---------------------------------------------------------------------------


def test_a_heading_in_a_house_style_is_recognised_by_its_outline_level(
    tmp_path: Path, settings: Settings
) -> None:
    """``w:outlineLvl`` is what defines outline level in OOXML, not the style's name.

    Missing it does not merely lose one heading: every paragraph after it is
    recorded under the *previous* section, so a citation points a reviewer at the
    wrong part of the document.
    """
    document = Document()
    document.add_heading("1. Scope", level=1)
    style = document.styles.add_style("ESCO Heading 1", WD_STYLE_TYPE.PARAGRAPH)
    heading = document.add_paragraph("2. Interlocks and Permissives", style=style)
    append_child(heading._p.get_or_add_pPr(), qn("w:outlineLvl"), {qn("w:val"): "0"})
    document.add_paragraph("Low air pressure trips the line below 60 psi.")
    result = parse_path(save(document, tmp_path / "outlinelvl.docx"), settings)

    promoted = only_paragraph_containing(result, "2. Interlocks")
    assert promoted["heading_level"] == 1
    assert [node["text"] for node in result.content["outline"]] == [
        "1. Scope",
        "2. Interlocks and Permissives",
    ]

    body = only_paragraph_containing(result, "Low air pressure")
    assert body["section_path"] == ["2. Interlocks and Permissives"]
    assert result.anchors_by_key["paragraph:2"].display_location == (
        "outlinelvl.docx / 2. Interlocks and Permissives / paragraph 2"
    )


def test_a_style_derived_from_a_heading_style_is_still_a_heading(
    tmp_path: Path, settings: Settings
) -> None:
    document = Document()
    document.add_heading("1. Scope", level=1)
    style = document.styles.add_style("ESCO Section", WD_STYLE_TYPE.PARAGRAPH)
    style.base_style = document.styles["Heading 2"]
    document.add_paragraph("1.1 Equipment", style=style)
    document.add_paragraph("CV101 is the feed conveyor.")
    result = parse_path(save(document, tmp_path / "basedon.docx"), settings)

    assert only_paragraph_containing(result, "1.1 Equipment")["heading_level"] == 2
    assert only_paragraph_containing(result, "CV101")["section_path"] == [
        "1. Scope",
        "1.1 Equipment",
    ]


def test_body_text_marked_outline_level_nine_is_not_promoted_to_a_heading(
    tmp_path: Path, settings: Settings
) -> None:
    """9 is Word's "body text" value; reading it as a level would invent structure."""
    document = Document()
    document.add_heading("1. Scope", level=1)
    paragraph = document.add_paragraph("Air pressure trips below 60 psi.")
    append_child(paragraph._p.get_or_add_pPr(), qn("w:outlineLvl"), {qn("w:val"): "9"})
    result = parse_path(save(document, tmp_path / "bodylevel.docx"), settings)

    assert only_paragraph_containing(result, "Air pressure")["heading_level"] is None
    assert [heading["text"] for heading in result.content["headings"]] == ["1. Scope"]


# ---------------------------------------------------------------------------
# Headers and footers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("attribute", "kind", "label", "text"),
    [
        ("first_page_header", "header", "first-page header", "DOC NO FD-L3-001 REV 4"),
        ("first_page_footer", "footer", "first-page footer", "UNCONTROLLED COPY"),
        ("even_page_header", "header", "even-page header", "Conveyor Line 3"),
        ("even_page_footer", "footer", "even-page footer", "SUPERSEDED BY REV 5"),
    ],
)
def test_every_header_and_footer_variant_is_reported(
    tmp_path: Path, settings: Settings, attribute: str, kind: str, label: str, text: str
) -> None:
    """The revision block of a functional description lives on the first page."""
    document = Document()
    document.add_heading("1. Scope", level=1)
    section = document.sections[0]
    section.different_first_page_header_footer = True
    getattr(section, attribute).paragraphs[0].text = text
    result = parse_path(save(document, tmp_path / "furniture.docx"), settings)

    items = unsupported_with(result, kind)
    assert len(items) == 1
    assert items[0].location == f"furniture.docx / section 1 {label}: {text}"
    assert paragraphs_containing(result, text) == []


# ---------------------------------------------------------------------------
# Ragged tables
# ---------------------------------------------------------------------------


def build_short_header_table(path: Path) -> Path:
    """A table whose *header* row is the short one: three data cells, two labels."""
    document = Document()
    document.add_heading("1. Scope", level=1)
    table = document.add_table(rows=2, cols=3)
    table.rows[0].cells[0].text = "Interlock"
    table.rows[0].cells[1].text = "Condition"
    table.rows[0].cells[2].text = "dropped"
    table.rows[1].cells[0].text = "ILK-005"
    table.rows[1].cells[1].text = "Air pressure below 60 psi"
    table.rows[1].cells[2].text = "Drop line permissive"
    row = table.rows[0]._tr
    row.remove(row.tc_lst[2])
    return save(document, path)


def test_a_column_the_header_row_does_not_reach_still_reaches_the_records(
    tmp_path: Path, settings: Settings
) -> None:
    """``records`` is the view a comparison agent reads, so a value missing there is lost."""
    result = parse_path(build_short_header_table(tmp_path / "ragged.docx"), settings)

    table = result.content["tables"][0]
    assert table["header"] == ["Interlock", "Condition"]
    assert table["records"] == [
        {
            "row": 1,
            "values": {
                "Interlock": "ILK-005",
                "Condition": "Air pressure below 60 psi",
                "column 2": "Drop line permissive",
            },
        }
    ]


def test_a_column_with_no_header_label_is_reported(tmp_path: Path, settings: Settings) -> None:
    result = parse_path(build_short_header_table(tmp_path / "ragged.docx"), settings)

    unrecognised = warnings_with(result, WarningCode.STRUCTURE_UNRECOGNISED)
    assert len(unrecognised) == 1
    assert "column(s) 2 are named by no header label" in unrecognised[0].message
    assert unrecognised[0].location == "ragged.docx / table 0 header row"


def test_the_ragged_rows_warning_names_the_rows_that_are_short(
    tmp_path: Path, settings: Settings
) -> None:
    """The old wording asserted that data rows were the short ones; here it is the header."""
    result = parse_path(build_short_header_table(tmp_path / "ragged.docx"), settings)

    ragged = warnings_with(result, WarningCode.RAGGED_ROWS)
    assert len(ragged) == 1
    assert "row(s) 0 have fewer" in ragged[0].message
    assert "the widest has 3" in ragged[0].message


def test_a_table_whose_layout_cannot_be_read_is_reported_and_left_empty(
    tmp_path: Path, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    document = Document()
    document.add_heading("1. Scope", level=1)
    table = document.add_table(rows=1, cols=1)
    table.rows[0].cells[0].text = "ILK-001"
    path = save(document, tmp_path / "bad_table.docx")

    def explode(self: Any) -> Any:
        raise ValueError("grid is not rectangular")

    monkeypatch.setattr(Table, "rows", property(explode))
    result = parse_path(path, settings)

    unrecognised = warnings_with(result, WarningCode.STRUCTURE_UNRECOGNISED)
    assert any("Table layout could not be read" in warning.message for warning in unrecognised)
    assert result.content["tables"][0] == {
        "index": 0,
        "rows": 0,
        "columns": 0,
        "header": [],
        "records": [],
        "cells": [],
        "section_path": ["1. Scope"],
    }


# ---------------------------------------------------------------------------
# The rest of the capability boundary
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tag", "kind"),
    [
        (qn("w:object"), "embedded_object"),
        (qn("w:pict"), "drawing"),
        (qn("w:endnoteReference"), "endnote"),
        (qn("w:commentReference"), "comment"),
        (qn("w:txbxContent"), "text_box"),
        (qn("w:instrText"), "field_code"),
        (qn("m:oMath"), "equation"),
        (qn("m:oMathPara"), "equation"),
    ],
)
def test_each_documented_unsupported_kind_is_recorded(
    tmp_path: Path, settings: Settings, tag: str, kind: str
) -> None:
    """Every entry in the module's "not read" list has to appear in the record."""
    document = Document()
    document.add_heading("1. Scope", level=1)
    paragraph = document.add_paragraph("Air pressure is monitored.")
    append_child(paragraph.add_run()._r, tag)
    result = parse_path(save(document, tmp_path / "feature.docx"), settings)

    items = unsupported_with(result, kind)
    assert len(items) == 1
    assert "Occurrences: 1" in items[0].detail
    assert "1. Scope / paragraph 1" in items[0].location


@pytest.mark.parametrize(
    ("uri", "kind"),
    [
        ("http://schemas.openxmlformats.org/drawingml/2006/chart", "chart"),
        ("http://schemas.openxmlformats.org/drawingml/2006/diagram", "diagram"),
        ("http://example.invalid/other", "drawing"),
    ],
)
def test_a_drawing_is_classified_by_the_graphic_data_it_carries(
    tmp_path: Path, settings: Settings, uri: str, kind: str
) -> None:
    document = Document()
    document.add_heading("1. Scope", level=1)
    paragraph = document.add_paragraph("See the trend below.")
    drawing = append_child(paragraph.add_run()._r, qn("w:drawing"))
    append_child(drawing, qn("a:graphicData"), {"uri": uri})
    result = parse_path(save(document, tmp_path / "drawing.docx"), settings)

    assert unsupported_kinds(result) == [kind]


def test_a_table_nested_in_a_cell_is_recorded_as_unsupported(
    tmp_path: Path, settings: Settings
) -> None:
    document = Document()
    document.add_heading("1. Scope", level=1)
    table = document.add_table(rows=2, cols=1)
    table.rows[0].cells[0].text = "Interlock"
    inner = table.rows[1].cells[0].add_table(rows=1, cols=1)
    inner.rows[0].cells[0].text = "ILK-003"
    result = parse_path(save(document, tmp_path / "nested.docx"), settings)

    items = unsupported_with(result, "nested_table")
    assert len(items) == 1
    assert "table 0 row 1 column 0" in items[0].location
