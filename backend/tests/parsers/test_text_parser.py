"""Tests for the plain-text fallback parser.

The happy path runs against ``fixtures/demo_project/Site_Notes_Ambiguous.txt``,
whose whole purpose is to be too vague to support a finding. What matters here is
that the traces of the planted air-pressure contradiction stay locatable by line
number and that the file's own warning against fabrication is preserved word for
word — the parser is what makes both citable, and a Senior Review Agent that
cannot quote them has no way to refuse a fabricated finding.

Degenerate and hostile inputs are built inline, never committed.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from app.core.config import Settings
from app.core.enums import AnchorType, DocumentRole, EvidenceKind, FileKind
from app.core.errors import ParserError
from app.models.evidence import EvidenceAnchor
from app.parsers.base import (
    MAX_EXCERPT_CHARS,
    AnchorDraft,
    ParseContext,
    Parser,
    ParseResult,
    WarningCode,
)
from app.parsers.registry import parser_for
from app.parsers.text_parser import MAX_LINE_ANCHORS, MAX_SECTION_ANCHORS, TextParser

REPO_ROOT = Path(__file__).resolve().parents[3]
NOTES_PATH = REPO_ROOT / "fixtures" / "demo_project" / "Site_Notes_Ambiguous.txt"

#: Read from the columns an anchor is eventually stored in, so the bounds the
#: parser has to respect are asserted rather than restated.
DISPLAY_LOCATION_LIMIT = EvidenceAnchor.display_location.type.length
SOURCE_LABEL_LIMIT = EvidenceAnchor.source_label.type.length
ANCHOR_KEY_LIMIT = EvidenceAnchor.anchor_key.type.length

FILE_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
PROJECT_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")


def make_context(path: Path, settings: Settings) -> ParseContext:
    return ParseContext(
        file_id=FILE_ID,
        project_id=PROJECT_ID,
        path=path,
        filename=path.name,
        file_kind=FileKind.TEXT,
        document_role=DocumentRole.OTHER,
        settings=settings,
    )


def parse_bytes(
    tmp_path: Path, settings: Settings, data: bytes, name: str = "notes.txt"
) -> ParseResult:
    path = tmp_path / name
    path.write_bytes(data)
    return TextParser().parse(make_context(path, settings))


def codes(result: ParseResult) -> list[str]:
    return [warning.code for warning in result.warnings]


def lines_containing(result: ParseResult, needle: str) -> list[int]:
    return [int(row["line"]) for row in result.content["lines"] if needle in row["text"]]


def anchor_signature(anchors: list[AnchorDraft]) -> list[tuple[str, str, str, str]]:
    return [
        (anchor.key, anchor.anchor_type.value, anchor.display_location, anchor.excerpt)
        for anchor in anchors
    ]


@pytest.fixture
def notes(settings: Settings) -> ParseResult:
    return TextParser().parse(make_context(NOTES_PATH, settings))


# ---------------------------------------------------------------------------
# Declared interface
# ---------------------------------------------------------------------------


def test_the_parser_declares_itself_for_text_files() -> None:
    parser = TextParser()
    assert parser.name == "text"
    assert parser.handles == (FileKind.TEXT,)
    assert parser.version
    assert isinstance(parser, Parser)


def test_the_registry_resolves_text_files_to_this_parser() -> None:
    assert isinstance(parser_for(FileKind.TEXT), TextParser)


def test_the_artifact_kind_is_the_text_file_kind(notes: ParseResult) -> None:
    assert notes.artifact_kind == FileKind.TEXT.value
    assert notes.parser_name == "text"
    assert notes.parser_version == TextParser.version


# ---------------------------------------------------------------------------
# The committed fixture: ground truth from fixtures/README.md
# ---------------------------------------------------------------------------


def test_the_first_section_is_the_underlined_heading(notes: ParseResult) -> None:
    sections = notes.content["sections"]
    assert len(sections) == 1
    assert sections[0]["title"] == "SYNTHETIC FIXTURE - loose site notes, Conveyor Line 3"
    assert sections[0]["start_line"] == 1
    assert sections[0]["end_line"] == notes.content["line_count"]
    assert WarningCode.STRUCTURE_UNRECOGNISED not in codes(notes)


def test_four_bullet_lines_are_detected(notes: ParseResult) -> None:
    bullets = notes.content["bullets"]
    assert [bullet["line"] for bullet in bullets] == [6, 8, 10, 12]
    assert {bullet["marker"] for bullet in bullets} == {"-"}
    assert bullets[3]["text"] == "CV103 drive fault behaviour is not written down anywhere."


def test_a_wrapped_bullet_continuation_is_not_folded_into_the_bullet(notes: ParseResult) -> None:
    """Joining the wrap would turn two recorded lines into one sentence the file never had."""
    assert notes.content["bullets"][0]["text"].endswith("could say at what")
    assert lines_containing(notes, "level. No tag was identified.") == [7]


def test_the_air_pressure_contradiction_is_locatable_by_line(notes: ParseResult) -> None:
    """These three strings are this file's only trace of the planted ILK-005 conflict."""
    assert lines_containing(notes, "60 psi") == [10]
    assert lines_containing(notes, "39 psi") == [11]
    assert lines_containing(notes, "Unresolved") == [11]

    anchors = notes.anchors_by_key
    assert "60 psi" in anchors["line:10"].excerpt
    assert "39 psi" in anchors["line:11"].excerpt
    assert "Unresolved" in anchors["line:11"].excerpt
    assert anchors["line:10"].display_location == "Site_Notes_Ambiguous.txt / line 10"


def test_the_closing_warning_against_fabrication_is_captured_verbatim(notes: ParseResult) -> None:
    expected = (
        "These notes are deliberately vague. Nothing here is sufficient on its own to\n"
        "support a technical finding, and a review that produced one from this alone\n"
        "would be fabricating."
    )
    closing = notes.content["paragraphs"][-1]

    assert closing["text"] == expected
    assert (closing["start_line"], closing["end_line"]) == (14, 16)
    assert expected in notes.anchors_by_key["file"].excerpt


def test_paragraphs_are_split_on_blank_lines(notes: ParseResult) -> None:
    spans = [(p["index"], p["start_line"], p["end_line"]) for p in notes.content["paragraphs"]]
    assert spans == [(1, 1, 2), (2, 4, 4), (3, 6, 12), (4, 14, 16)]


def test_the_fixture_is_recorded_line_for_line(notes: ParseResult) -> None:
    raw = NOTES_PATH.read_text(encoding="utf-8")
    content = notes.content

    assert content["line_count"] == 16
    assert content["character_count"] == len(raw)
    assert [row["text"] for row in content["lines"]] == raw.split("\n")[:-1]
    assert content["encoding"] == {"name": "utf-8", "confidence": 1.0, "had_bom": False}
    assert codes(notes) == []


def test_the_summary_reports_shape_without_carrying_the_text(notes: ParseResult) -> None:
    summary = notes.summary

    assert summary["line_count"] == 16
    assert summary["blank_line_count"] == 3
    assert summary["section_count"] == 1
    assert summary["headings_detected"] == 1
    assert summary["paragraph_count"] == 4
    assert summary["bullet_count"] == 4
    assert summary["line_anchor_count"] == 13
    assert summary["section_anchor_count"] == 1
    # Measured from the committed fixture, not restated from the parser's own
    # output: one whole-file anchor, one section anchor for the single
    # underlined heading, and one line anchor per non-blank line (16 - 3).
    assert summary["anchor_count"] == 15
    assert "60 psi" not in json.dumps(summary)


def test_every_anchor_carries_the_source_label_and_a_bounded_excerpt(notes: ParseResult) -> None:
    assert notes.anchors
    for anchor in notes.anchors:
        assert anchor.source_label == "Site_Notes_Ambiguous.txt"
        assert anchor.evidence_kind is EvidenceKind.DIRECT
        assert len(anchor.excerpt) <= MAX_EXCERPT_CHARS
        assert anchor.display_location.startswith("Site_Notes_Ambiguous.txt")
        assert len(anchor.display_location) <= DISPLAY_LOCATION_LIMIT

    types = [anchor.anchor_type for anchor in notes.anchors]
    assert types[0] is AnchorType.FILE_WHOLE
    assert types[1] is AnchorType.FILE_SECTION
    assert set(types[2:]) == {AnchorType.FILE_LINE}


def test_no_two_anchors_share_a_key(notes: ParseResult, tmp_path: Path, settings: Settings) -> None:
    """A duplicate key silently costs a citation, so uniqueness is asserted on the list.

    ``anchors_by_key`` is a dict, and persistence keeps the first draft per key
    and only logs the rest, so a colliding key fails nowhere: the second anchor
    is dropped and a line quietly stops being citable. Counting the raw list
    against the deduplicated mapping is the only place that shows it.
    """
    keys = [anchor.key for anchor in notes.anchors]
    assert len(notes.anchors_by_key) == len(notes.anchors)
    assert len(set(keys)) == len(keys)

    # Sections and lines are keyed in different namespaces; a file with several
    # of each is where a collision would first appear.
    body = (
        "preamble before any heading\n"
        "\n"
        "Commissioning\n"
        "=============\n"
        "notes under the underlined heading\n"
        "\n"
        "OPEN ITEMS\n"
        "- hopper level unknown\n"
    )
    multi = parse_bytes(tmp_path, settings, body.encode("utf-8"))
    multi_keys = [anchor.key for anchor in multi.anchors]

    assert len(multi.anchors_by_key) == len(multi.anchors)
    assert len(set(multi_keys)) == len(multi_keys)
    assert multi_keys == [
        "file",
        "section:1-2",
        "section:3-6",
        "section:7-8",
        "line:1",
        "line:3",
        "line:4",
        "line:5",
        "line:7",
        "line:8",
    ]


def test_blank_lines_get_no_anchor(notes: ParseResult) -> None:
    """An empty line has nothing for a reviewer to read, so it is not offered as evidence."""
    keys = notes.anchors_by_key
    assert "line:4" in keys
    assert "line:3" not in keys
    assert "line:5" not in keys


def test_the_output_contains_no_absolute_path(notes: ParseResult) -> None:
    serialised = json.dumps(
        {
            "content": notes.content,
            "summary": notes.summary,
            "anchors": anchor_signature(notes.anchors),
            "warnings": [warning.as_dict() for warning in notes.warnings],
        }
    )
    assert str(REPO_ROOT) not in serialised
    assert NOTES_PATH.as_posix() not in serialised
    assert str(NOTES_PATH.parent) not in serialised


def test_reparsing_the_same_fixture_produces_identical_output(settings: Settings) -> None:
    first = TextParser().parse(make_context(NOTES_PATH, settings))
    second = TextParser().parse(make_context(NOTES_PATH, settings))

    assert first.content == second.content
    assert first.summary == second.summary
    assert anchor_signature(first.anchors) == anchor_signature(second.anchors)
    assert [w.as_dict() for w in first.warnings] == [w.as_dict() for w in second.warnings]
    assert [u.as_dict() for u in first.unsupported] == [u.as_dict() for u in second.unsupported]
    assert [a.structured_location for a in first.anchors] == [
        a.structured_location for a in second.anchors
    ]


# ---------------------------------------------------------------------------
# Structure detection
# ---------------------------------------------------------------------------


def test_a_file_with_no_headings_returns_one_whole_file_section(
    tmp_path: Path, settings: Settings
) -> None:
    body = "just some loose prose about the line.\n\nand a second thought.\n"
    result = parse_bytes(tmp_path, settings, body.encode("utf-8"))
    sections = result.content["sections"]

    assert len(sections) == 1
    assert sections[0]["title"] == ""
    assert (sections[0]["start_line"], sections[0]["end_line"]) == (1, 3)
    assert WarningCode.STRUCTURE_UNRECOGNISED in codes(result)


def test_underlined_and_all_caps_headings_partition_the_file(
    tmp_path: Path, settings: Settings
) -> None:
    body = (
        "preamble before any heading\n"
        "\n"
        "Commissioning\n"
        "=============\n"
        "notes under the underlined heading\n"
        "\n"
        "OPEN ITEMS\n"
        "- hopper level unknown\n"
    )
    result = parse_bytes(tmp_path, settings, body.encode("utf-8"))
    sections = result.content["sections"]

    assert [(s["title"], s["start_line"], s["end_line"]) for s in sections] == [
        ("", 1, 2),
        ("Commissioning", 3, 6),
        ("OPEN ITEMS", 7, 8),
    ]
    covered = [row["line"] for section in sections for row in section["lines"]]
    assert covered == [1, 2, 3, 4, 5, 6, 7, 8]
    assert result.content["line_count"] == 8
    assert result.summary["headings_detected"] == 2


def test_an_underlined_heading_is_marked_up_by_the_file_and_cited_as_such(
    tmp_path: Path, settings: Settings
) -> None:
    """The underline is in the file, so the title and the grouping are the file's."""
    body = "Commissioning\n=============\nnotes under the heading\n"
    result = parse_bytes(tmp_path, settings, body.encode("utf-8"))
    section = result.content["sections"][0]
    anchor = result.anchors_by_key["section:1-3"]

    assert (section["title"], section["title_source"]) == ("Commissioning", "underlined")
    assert anchor.evidence_kind is EvidenceKind.DIRECT
    assert anchor.structured_location["title_source"] == "underlined"
    assert anchor.display_location.endswith("Commissioning (lines 1-3)")
    assert codes(result) == []
    assert result.summary["headings_inferred"] == 0


def test_a_bare_all_caps_line_is_titled_as_an_inference_not_as_the_files_own_heading(
    tmp_path: Path, settings: Settings
) -> None:
    """Nothing in the file associates these lines; the parser's reading must say so.

    A titled, scoped bundle over vague notes reads to an agent as an association
    the source made. It is the parser's inference, and both the warning and the
    anchor have to carry that, or "too little structure" is reported while
    "structure that is not there" is not.
    """
    body = (
        "Site walkdown 2026-07-14, Conveyor Line 3.\n"
        "\n"
        "HOPPER LEVEL\n"
        "Ops mentioned it trips out sometimes but nobody could say at what level.\n"
        "\n"
        "The panel drawing says 60 psi; commissioning says it dropped out at 39 psi.\n"
        "These notes are vague and nothing here supports a technical finding.\n"
    )
    result = parse_bytes(tmp_path, settings, body.encode("utf-8"), name="walkdown.txt")

    sections = result.content["sections"]
    assert [(s["title"], s["title_source"]) for s in sections] == [
        ("", ""),
        ("HOPPER LEVEL", "caps"),
    ]
    assert result.summary["headings_inferred"] == 1

    inferred = next(w for w in result.warnings if w.code == WarningCode.STRUCTURE_UNRECOGNISED)
    assert inferred.severity == "info"
    assert "line 3 'HOPPER LEVEL'" in inferred.message
    assert "inferred" in inferred.message

    anchor = result.anchors_by_key["section:3-7"]
    assert anchor.evidence_kind is EvidenceKind.INFERENCE
    assert anchor.structured_location["title_source"] == "caps"
    assert anchor.display_location == "walkdown.txt / HOPPER LEVEL (inferred heading, lines 3-7)"


def test_a_run_of_all_caps_lines_is_a_block_of_text_not_a_stack_of_headings(
    tmp_path: Path, settings: Settings
) -> None:
    """An upper-case line followed by another one heads nothing — it is a log or a dump."""
    body = "CV103 DRIVE FAULT LATCHED 1\nCV103 DRIVE FAULT LATCHED 2\nCV103 DRIVE FAULT LATCHED 3\n"
    result = parse_bytes(tmp_path, settings, body.encode("utf-8"), name="alarm.log")
    sections = result.content["sections"]

    assert [(s["title"], s["start_line"], s["end_line"]) for s in sections] == [("", 1, 3)]
    assert result.summary["headings_detected"] == 0
    assert len([a for a in result.anchors if a.anchor_type is AnchorType.FILE_SECTION]) == 1
    unrecognised = next(w for w in result.warnings if w.code == WarningCode.STRUCTURE_UNRECOGNISED)
    assert "No heading could be detected" in unrecognised.message


def test_an_all_caps_last_line_is_not_a_heading_over_nothing(
    tmp_path: Path, settings: Settings
) -> None:
    body = "loose notes about the line\n\nOPEN ITEMS\n"
    result = parse_bytes(tmp_path, settings, body.encode("utf-8"))

    assert [s["title"] for s in result.content["sections"]] == [""]
    assert result.summary["headings_detected"] == 0


def test_a_long_upper_case_sentence_is_prose_in_caps_not_a_heading(
    tmp_path: Path, settings: Settings
) -> None:
    """The length bound is the only thing separating a shouted sentence from a title.

    Both lines below sit in the same position — upper case, with lower-case prose
    under them — so the short one is titled and the long one must not be. A title
    invented over a shouted sentence would group the lines under a heading the
    author never wrote, which is the failure the caps inference exists to bound.
    """
    shouted = "THE PANEL DRAWING SAYS 60 PSI AND COMMISSIONING SAYS 39 PSI, UNRESOLVED"
    short = "AIR PRESSURE"
    assert len(shouted) == 71  # over the 60-character bound
    assert len(short) == 12  # under it

    def sections(caps: str, name: str) -> list[tuple[str, str]]:
        body = f"site walkdown notes.\n\n{caps}\nfollow-up needed with the panel builder.\n"
        result = parse_bytes(tmp_path, settings, body.encode("utf-8"), name=name)
        return [(s["title"], s["title_source"]) for s in result.content["sections"]]

    assert sections(shouted, "shouted.txt") == [("", "")]
    assert sections(short, "short.txt") == [("", ""), ("AIR PRESSURE", "caps")]


def test_more_sections_than_the_cap_is_reported_as_truncated(
    tmp_path: Path, settings: Settings
) -> None:
    """Section anchors are bounded too, or a heading every other line routes around the line cap."""
    over_cap = MAX_SECTION_ANCHORS + 2
    body = "".join(f"SECTION {number}\nbody line {number}\n" for number in range(over_cap))
    result = parse_bytes(tmp_path, settings, body.encode("utf-8"))

    assert result.summary["section_count"] == over_cap
    assert result.summary["section_anchor_count"] == MAX_SECTION_ANCHORS
    first_dropped = MAX_SECTION_ANCHORS * 2 + 1
    assert f"section:{first_dropped - 2}-{first_dropped - 1}" in result.anchors_by_key
    assert f"section:{first_dropped}-{first_dropped + 1}" not in result.anchors_by_key

    truncated = next(w for w in result.warnings if w.code == WarningCode.TRUNCATED)
    assert truncated.location.endswith(f"lines {first_dropped}-{first_dropped + 1}")
    assert str(MAX_SECTION_ANCHORS) in truncated.message


def test_an_underline_run_is_not_itself_treated_as_a_heading_or_a_bullet(
    tmp_path: Path, settings: Settings
) -> None:
    result = parse_bytes(tmp_path, settings, b"Air pressure\n------------\n40 psi seen\n")
    sections = result.content["sections"]

    assert [s["title"] for s in sections] == ["Air pressure"]
    assert result.content["bullets"] == []


def test_a_decimal_number_at_the_start_of_a_line_is_not_a_bullet(
    tmp_path: Path, settings: Settings
) -> None:
    body = "1. check the gauge\n39.5 psi recorded\n* and a star\n"
    result = parse_bytes(tmp_path, settings, body.encode("utf-8"))

    assert [(b["line"], b["marker"]) for b in result.content["bullets"]] == [(1, "1."), (3, "*")]


# ---------------------------------------------------------------------------
# Line endings and encodings
# ---------------------------------------------------------------------------


def test_crlf_line_endings_produce_the_same_output_as_lf(
    tmp_path: Path, settings: Settings
) -> None:
    body = "Site notes\n==========\n\n- gauge reads 60 psi\n- dropout at 39 psi\n"
    lf_dir = tmp_path / "lf"
    crlf_dir = tmp_path / "crlf"
    lf_dir.mkdir()
    crlf_dir.mkdir()

    lf = parse_bytes(lf_dir, settings, body.encode("utf-8"))
    crlf = parse_bytes(crlf_dir, settings, body.replace("\n", "\r\n").encode("utf-8"))

    assert lf.content == crlf.content
    assert lf.summary == crlf.summary
    assert anchor_signature(lf.anchors) == anchor_signature(crlf.anchors)
    assert crlf.content["line_count"] == 5
    assert "\r" not in json.dumps(crlf.content)


def test_a_lone_carriage_return_is_normalised_to_a_line_break(
    tmp_path: Path, settings: Settings
) -> None:
    result = parse_bytes(tmp_path, settings, b"first line\rsecond line\r")

    assert [row["text"] for row in result.content["lines"]] == ["first line", "second line"]


def test_a_file_with_no_trailing_newline_keeps_its_last_line(
    tmp_path: Path, settings: Settings
) -> None:
    result = parse_bytes(tmp_path, settings, b"first\nlast line, no newline")
    content = result.content

    assert content["line_count"] == 2
    assert content["lines"][-1]["text"] == "last line, no newline"
    assert content["paragraphs"][-1]["end_line"] == 2
    assert "line:2" in result.anchors_by_key


def test_a_latin_1_file_parses_with_an_encoding_guessed_warning(
    tmp_path: Path, settings: Settings
) -> None:
    body = "Technicien Rene Dupre a note une reduction: 39 psi\nvalve trouvee ouverte\n"
    body = body.replace("Rene", "Ren\xe9").replace("note", "not\xe9")
    result = parse_bytes(tmp_path, settings, body.encode("latin-1"))

    assert result.content["encoding"] == {"name": "cp1252", "confidence": 0.6, "had_bom": False}
    assert result.content["lines"][0]["text"] == body.split("\n")[0]
    assert WarningCode.ENCODING_GUESSED in codes(result)
    guessed = next(w for w in result.warnings if w.code == WarningCode.ENCODING_GUESSED)
    assert "cp1252" in guessed.message
    assert "0.6" in guessed.message


def test_a_utf8_bom_is_recorded_and_stripped_from_the_text(
    tmp_path: Path, settings: Settings
) -> None:
    result = parse_bytes(tmp_path, settings, "notes\n".encode("utf-8-sig"))

    assert result.content["encoding"] == {"name": "utf-8-sig", "confidence": 1.0, "had_bom": True}
    assert result.content["lines"][0]["text"] == "notes"
    assert WarningCode.ENCODING_GUESSED not in codes(result)


def test_a_utf16_file_is_not_mistaken_for_binary(tmp_path: Path, settings: Settings) -> None:
    """Every ASCII character in UTF-16 carries a NUL byte, which is not evidence of binary."""
    result = parse_bytes(tmp_path, settings, "SITE NOTES\ndropout\n".encode("utf-16"))

    assert result.content["encoding"] == {"name": "utf-16", "confidence": 1.0, "had_bom": True}
    assert [row["text"] for row in result.content["lines"]] == ["SITE NOTES", "dropout"]
    assert result.unsupported == []
    # "SITE NOTES" is read as an inferred heading, which is reported separately;
    # nothing about the decode is in doubt.
    assert WarningCode.ENCODING_GUESSED not in codes(result)


# ---------------------------------------------------------------------------
# Degenerate and hostile input
# ---------------------------------------------------------------------------


def test_a_utf16_file_without_a_byte_order_mark_is_not_read_as_certain_utf8(
    tmp_path: Path, settings: Settings
) -> None:
    """NUL-interleaved UTF-16 is valid UTF-8, so a clean UTF-8 decode proves nothing.

    Decoded as UTF-8 the recorded lines are NUL-interleaved mojibake, which the
    old route published at confidence 1.0 with no warning at all.
    """
    result = parse_bytes(
        tmp_path, settings, "SITE NOTES\nAir pressure dropped to 39 psi\n".encode("utf-16-le")
    )

    assert result.content["encoding"] == {
        "name": "utf-16-le",
        "confidence": 0.6,
        "had_bom": False,
    }
    assert [row["text"] for row in result.content["lines"]] == [
        "SITE NOTES",
        "Air pressure dropped to 39 psi",
    ]
    assert "\x00" not in json.dumps(result.content)
    assert result.unsupported == []

    guessed = next(w for w in result.warnings if w.code == WarningCode.ENCODING_GUESSED)
    assert "utf-16-le" in guessed.message
    assert "No byte-order mark is present" in guessed.message


def test_a_big_endian_utf16_file_without_a_mark_is_detected_the_same_way(
    tmp_path: Path, settings: Settings
) -> None:
    result = parse_bytes(tmp_path, settings, "SITE NOTES\ndropout at 39 psi\n".encode("utf-16-be"))

    assert result.content["encoding"]["name"] == "utf-16-be"
    assert result.content["encoding"]["confidence"] < 1.0
    assert [row["text"] for row in result.content["lines"]] == ["SITE NOTES", "dropout at 39 psi"]
    assert WarningCode.ENCODING_GUESSED in codes(result)


def test_nul_characters_in_a_utf8_decode_are_reported_as_an_observation(
    tmp_path: Path, settings: Settings
) -> None:
    """The parser saw NULs; it did not establish that the file is not text."""
    result = parse_bytes(tmp_path, settings, b"notes\x00 39 psi\n")

    assert result.content["encoding"]["name"] == "utf-8"
    assert result.content["encoding"]["confidence"] < 1.0
    assert WarningCode.ENCODING_GUESSED in codes(result)

    detail = result.unsupported[0].detail
    assert result.unsupported[0].kind == "binary_content"
    assert "probably not a plain-text file" not in detail
    assert "may be a binary format" in detail


def test_an_empty_file_reports_an_empty_document_instead_of_nothing(
    tmp_path: Path, settings: Settings
) -> None:
    result = parse_bytes(tmp_path, settings, b"")
    content = result.content

    assert codes(result) == [WarningCode.EMPTY_DOCUMENT]
    assert content["line_count"] == 0
    assert content["character_count"] == 0
    assert content["lines"] == []
    assert content["sections"] == []
    assert content["paragraphs"] == []
    assert content["bullets"] == []
    assert [anchor.key for anchor in result.anchors] == ["file"]


def test_a_whitespace_only_file_reports_an_empty_document(
    tmp_path: Path, settings: Settings
) -> None:
    result = parse_bytes(tmp_path, settings, b"   \n\t\n \n")

    assert codes(result) == [WarningCode.EMPTY_DOCUMENT]
    assert result.content["line_count"] == 3
    assert result.summary["blank_line_count"] == 3
    assert result.summary["line_anchor_count"] == 0
    assert result.content["sections"][0]["end_line"] == 3


def test_undecodable_bytes_degrade_to_latin_1_rather_than_raising(
    tmp_path: Path, settings: Settings
) -> None:
    """0x81 is unassigned in cp1252, so the ladder has to fall through to latin-1."""
    result = parse_bytes(tmp_path, settings, b"gauge \x81 reading \xff\n39 psi\n")

    assert result.content["encoding"] == {"name": "latin-1", "confidence": 0.3, "had_bom": False}
    assert WarningCode.ENCODING_GUESSED in codes(result)
    assert result.content["line_count"] == 2
    assert lines_containing(result, "39 psi") == [2]


def test_nul_bytes_are_recorded_as_unsupported_binary_content(
    tmp_path: Path, settings: Settings
) -> None:
    result = parse_bytes(tmp_path, settings, b"\x00\x01notes\x00\x81\xff more\n")

    assert [item.kind for item in result.unsupported] == ["binary_content"]
    assert result.unsupported[0].detail.startswith("2 NUL character(s)")
    assert result.content["line_count"] == 1
    assert result.anchors_by_key["line:1"].excerpt


def test_a_byte_order_mark_that_does_not_decode_is_reported_not_trusted(
    tmp_path: Path, settings: Settings
) -> None:
    """An odd-length UTF-16 body cannot be decoded; the BOM's claim must not be repeated as fact."""
    result = parse_bytes(tmp_path, settings, b"\xff\xfeodd body\n")

    assert result.content["encoding"]["had_bom"] is True
    assert result.content["encoding"]["confidence"] < 1.0
    guessed = next(w for w in result.warnings if w.code == WarningCode.ENCODING_GUESSED)
    assert "byte-order mark" in guessed.message


def test_a_byte_order_mark_that_does_not_decode_is_kept_out_of_the_recorded_text(
    tmp_path: Path, settings: Settings
) -> None:
    """The mark is metadata. Left on the front of the bytes it is quoted as authored text."""
    result = parse_bytes(tmp_path, settings, b"\xff\xfeodd body\n")

    assert result.content["lines"][0]["text"] == "odd body"
    assert result.anchors_by_key["line:1"].excerpt == "odd body"
    assert result.anchors_by_key["file"].excerpt.startswith("odd body")
    assert result.content["character_count"] == len("odd body\n")

    guessed = next(w for w in result.warnings if w.code == WarningCode.ENCODING_GUESSED)
    assert "removed" in guessed.message


def test_a_utf8_mark_over_undecodable_bytes_is_also_kept_out_of_the_text(
    tmp_path: Path, settings: Settings
) -> None:
    result = parse_bytes(tmp_path, settings, b"\xef\xbb\xbfgauge \x81 reads 39 psi\n")

    assert result.content["encoding"]["had_bom"] is True
    assert result.content["lines"][0]["text"] == "gauge \x81 reads 39 psi"
    assert "\ufeff" not in json.dumps(result.content)
    assert lines_containing(result, "39 psi") == [1]


def test_a_very_long_heading_is_shortened_in_the_label_but_not_in_the_content(
    tmp_path: Path, settings: Settings
) -> None:
    """display_location is a bounded column, and a heading is just a line."""
    title = "Air pressure interlock threshold reconciliation " * 8
    body = f"{title}\n{'=' * 10}\nnotes\n"
    result = parse_bytes(tmp_path, settings, body.encode("utf-8"))
    anchor = result.anchors_by_key["section:1-3"]

    assert result.content["sections"][0]["title"] == title.strip()
    assert anchor.structured_location["title"] == title.strip()
    assert len(anchor.display_location) <= DISPLAY_LOCATION_LIMIT
    assert anchor.display_location.endswith("(lines 1-3)")


def test_a_long_filename_and_archive_path_still_produce_a_bounded_label(
    tmp_path: Path, settings: Settings
) -> None:
    """Every anchor label is bounded against the column it is stored in.

    ``source_label`` and ``display_location`` are ``String(255)`` and
    ``String(512)``, and both are composed here from ``ParseContext.source_label``
    — a filename plus, for a file lifted out of a zip, its member path. SQLite
    stores the overflow silently, so an over-long label works in the demo and
    raises on a stricter backend. The bound itself lives in ``AnchorDraft``; what
    is asserted here is that the labels this parser composes actually reach it.
    """
    path = tmp_path / "notes.txt"
    title = "Air pressure interlock threshold reconciliation " * 4
    path.write_text(f"{title}\n{'=' * 10}\nthe gauge reads 60 psi\n", encoding="utf-8")
    context = ParseContext(
        file_id=FILE_ID,
        project_id=PROJECT_ID,
        path=path,
        filename="site notes " * 18 + "final.txt",
        file_kind=FileKind.TEXT,
        document_role=DocumentRole.OTHER,
        settings=settings,
        archive_member_path="packages/line3/" + "deeply nested folder/" * 10 + "notes.txt",
    )
    assert len(context.source_label) > SOURCE_LABEL_LIMIT  # the input really is over-long

    result = TextParser().parse(context)

    assert result.anchors
    for anchor in result.anchors:
        assert len(anchor.source_label) <= SOURCE_LABEL_LIMIT
        assert len(anchor.display_location) <= DISPLAY_LOCATION_LIMIT
        assert len(anchor.key) <= ANCHOR_KEY_LIMIT
    # The section label is the longest composition the parser builds (label,
    # heading and span), so it is the one that has to be cut to the column.
    assert len(result.anchors_by_key["section:1-3"].display_location) == DISPLAY_LOCATION_LIMIT
    # The full heading is still recorded; only the label was shortened.
    assert result.content["sections"][0]["title"] == title.strip()


def test_a_single_very_long_line_is_recorded_in_full_but_the_excerpt_is_bounded(
    tmp_path: Path, settings: Settings
) -> None:
    body = "x" * (MAX_EXCERPT_CHARS * 3)
    result = parse_bytes(tmp_path, settings, body.encode("utf-8"))

    assert result.content["character_count"] == len(body)
    assert result.content["lines"][0]["text"] == body
    assert len(result.anchors_by_key["line:1"].excerpt) == MAX_EXCERPT_CHARS
    assert result.anchors_by_key["file"].excerpt.endswith("...")


def test_more_anchorable_lines_than_the_cap_is_reported_as_truncated(
    tmp_path: Path, settings: Settings
) -> None:
    over_cap = MAX_LINE_ANCHORS + 1
    body = "".join(f"observation {number}\n" for number in range(1, over_cap + 1))
    result = parse_bytes(tmp_path, settings, body.encode("utf-8"))

    assert result.content["line_count"] == over_cap
    assert result.summary["line_anchor_count"] == MAX_LINE_ANCHORS
    assert f"line:{MAX_LINE_ANCHORS}" in result.anchors_by_key
    assert f"line:{over_cap}" not in result.anchors_by_key

    truncated = next(w for w in result.warnings if w.code == WarningCode.TRUNCATED)
    assert truncated.location.endswith(f"line {over_cap}")
    assert result.content["lines"][-1]["text"] == f"observation {over_cap}"


def test_the_line_cap_warning_does_not_claim_the_later_lines_are_uncitable(
    tmp_path: Path, settings: Settings
) -> None:
    """A line past the cap is still covered by the whole-file and section anchors."""
    over_cap = MAX_LINE_ANCHORS + 1
    body = "Commissioning\n=============\n" + "".join(
        f"observation {number}\n" for number in range(1, over_cap + 1)
    )
    result = parse_bytes(tmp_path, settings, body.encode("utf-8"))

    truncated = next(w for w in result.warnings if w.code == WarningCode.TRUNCATED)
    assert "cannot be cited individually" not in truncated.message
    assert "no anchor of their own" in truncated.message
    assert "whole-file anchor" in truncated.message

    last_line = result.content["line_count"]
    covering = result.anchors_by_key[f"section:1-{last_line}"]
    assert f"line:{last_line}" not in result.anchors_by_key
    assert covering.structured_location["end_line"] == last_line


def test_a_missing_file_raises_a_parser_error_without_leaking_the_path(
    tmp_path: Path, settings: Settings
) -> None:
    missing = tmp_path / "not_uploaded.txt"

    with pytest.raises(ParserError) as excinfo:
        TextParser().parse(make_context(missing, settings))

    assert excinfo.value.details["filename"] == "not_uploaded.txt"
    assert str(tmp_path) not in excinfo.value.message
    assert "not_uploaded.txt" in excinfo.value.message


# ---------------------------------------------------------------------------
# NUL characters
# ---------------------------------------------------------------------------

#: Built rather than written as an escape in a literal, so what these tests feed
#: the parser is unambiguously the character the database cannot store.
NUL = chr(0)


def test_a_nul_in_the_decoded_text_is_replaced_with_a_space(
    tmp_path: Path, settings: Settings
) -> None:
    """PostgreSQL cannot hold one, and SQLite hides that from the whole suite.

    A truncated log, a UTF-16 fragment or a mislabelled upload decodes as valid
    UTF-8 and still carries a NUL. Recorded as-is it reaches
    ``EvidenceAnchor.excerpt`` (a text column) and ``ParsedArtifact.content``
    (jsonb), neither of which can hold a NUL: the flush raises, the parse
    transaction aborts, and the file lands in ``parse_failed`` carrying a
    driver-level message no engineer can act on.
    """
    result = parse_bytes(
        tmp_path, settings, f"Line one{NUL} with an embedded NUL\nLine two\n".encode()
    )

    assert NUL not in json.dumps(result.content)
    assert not [anchor for anchor in result.anchors if NUL in anchor.excerpt]
    assert result.content["lines"][0]["text"] == "Line one  with an embedded NUL"


def test_the_nul_substitution_is_reported_rather_than_made_quietly(
    tmp_path: Path, settings: Settings
) -> None:
    """The one edit this parser makes has to be visible in the record.

    Both halves are reported: the unsupported item says the file held NUL and
    was not interpreted as a binary format, and the warning says the recorded
    text is therefore not what the file holds.
    """
    result = parse_bytes(tmp_path, settings, f"pump{NUL} station{NUL}\n".encode())

    warning = next(w for w in result.warnings if "NUL character(s) were replaced" in w.message)
    assert warning.severity == "error"
    assert "2 NUL character(s)" in warning.message

    item = next(entry for entry in result.unsupported if entry.kind == "binary_content")
    assert "2 NUL character(s)" in item.detail


def test_replacing_a_nul_moves_no_recorded_position(tmp_path: Path, settings: Settings) -> None:
    """A space, not a deletion, so every offset still names its own character."""
    body = f"CHECKLIST\n=========\nvalve {NUL} shut\nsecond line\n"
    result = parse_bytes(tmp_path, settings, body.encode())

    assert result.content["character_count"] == len(body)
    assert result.content["line_count"] == 4
    assert result.anchors_by_key["line:3"].excerpt == "valve   shut"


def test_a_file_without_nuls_reports_no_substitution(tmp_path: Path, settings: Settings) -> None:
    """The warning is evidence about the text, so an ordinary file must not carry it."""
    result = parse_bytes(tmp_path, settings, b"valve shut\nsecond line\n")

    assert not [w for w in result.warnings if "NUL character(s) were replaced" in w.message]
    assert not [entry for entry in result.unsupported if entry.kind == "binary_content"]
