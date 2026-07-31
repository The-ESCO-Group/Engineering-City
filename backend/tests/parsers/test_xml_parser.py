"""Generic XML parsing.

The HMI export is the counterweight to the L5X in the fixture set: it names an
alarm for a jam the logic never implements and spells a guard tag the way the
documentation does, not the way the logic does. Neither discrepancy can be found
later unless this parser records those strings verbatim and gives each one an
address a reviewer can follow, so the assertions below are mostly about exact
strings and unambiguous xpaths.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from app.core.config import Settings
from app.core.enums import AnchorType, DocumentRole, EvidenceKind, FileKind
from app.core.errors import ParserError
from app.parsers import xml_parser as xml_parser_module
from app.parsers.base import (
    MAX_DISPLAY_LOCATION_CHARS,
    MAX_EXCERPT_CHARS,
    MAX_SOURCE_LABEL_CHARS,
    ParseContext,
    Parser,
    ParseResult,
    WarningCode,
)
from app.parsers.xml_parser import MAX_ANCHORS, XmlParser
from app.security import xml_safety
from app.security.xml_safety import XmlSafetyError, iter_with_paths, parse_xml_bytes

FIXTURE_DIR = Path(__file__).resolve().parents[3] / "fixtures" / "demo_project"
HMI_EXPORT = FIXTURE_DIR / "HMI_Alarm_Export.xml"

ALARMS = "/AlarmConfiguration/Alarms"

#: Every xpath the committed export produces, in document order, written out
#: rather than derived from the parse: a list checked against the parser's own
#: output cannot fail, and these are the addresses a reviewer follows.
HMI_XPATHS = [
    "/AlarmConfiguration",
    "/AlarmConfiguration/Comment",
    "/AlarmConfiguration/AlarmGroups",
    "/AlarmConfiguration/AlarmGroups/AlarmGroup[1]",
    "/AlarmConfiguration/AlarmGroups/AlarmGroup[2]",
    "/AlarmConfiguration/AlarmGroups/AlarmGroup[3]",
    "/AlarmConfiguration/Alarms",
    "/AlarmConfiguration/Alarms/Alarm[1]",
    "/AlarmConfiguration/Alarms/Alarm[1]/Message",
    "/AlarmConfiguration/Alarms/Alarm[2]",
    "/AlarmConfiguration/Alarms/Alarm[2]/Message",
    "/AlarmConfiguration/Alarms/Alarm[3]",
    "/AlarmConfiguration/Alarms/Alarm[3]/Message",
    "/AlarmConfiguration/Alarms/Alarm[4]",
    "/AlarmConfiguration/Alarms/Alarm[4]/Message",
    "/AlarmConfiguration/Alarms/Alarm[5]",
    "/AlarmConfiguration/Alarms/Alarm[5]/Message",
    "/AlarmConfiguration/Alarms/Alarm[6]",
    "/AlarmConfiguration/Alarms/Alarm[6]/Message",
]

#: The six alarms as the export writes them: name, expression and message text.
#: A parser that swapped two messages would still satisfy a count.
HMI_ALARMS = [
    (
        "ALM_ESTOP",
        "ESTOP_OK == 0",
        "Emergency stop actuated - all line 3 conveyors stopped",
    ),
    (
        "ALM_CV101_OL",
        "CV101_OL_TRIP == 1",
        "CV101 motor overload - reset required in Manual",
    ),
    (
        "ALM_CV102_OL",
        "CV102_OL_TRIP == 1",
        "CV102 motor overload - reset required in Manual",
    ),
    (
        "ALM_HOPPER_HIGH",
        "HOPPER_LEVEL_PCT > 90.0",
        "Surge hopper level high",
    ),
    (
        "ALM_CV102_JAM",
        "CV102_JAM == 1",
        "CV102 jam detected - clear blockage before restart",
    ),
    (
        "ALM_GUARD_OPEN",
        "GUARD_DOOR_CLOSED == 0",
        "Guard door open - line permissive dropped",
    ),
]


def _parse(path: Path, settings: Settings) -> ParseResult:
    context = ParseContext(
        file_id=uuid.UUID(int=1),
        project_id=uuid.UUID(int=2),
        path=path,
        filename=path.name,
        file_kind=FileKind.XML,
        document_role=DocumentRole.HMI_EXPORT,
        settings=settings,
    )
    return XmlParser().parse(context)


def _write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def _element(result: ParseResult, xpath: str) -> dict[str, object]:
    matches = [item for item in result.content["elements"] if item["xpath"] == xpath]
    assert len(matches) == 1, f"expected exactly one element at {xpath}"
    return dict(matches[0])


def _alarm(result: ParseResult, name: str) -> dict[str, object]:
    matches = [
        item
        for item in result.content["elements"]
        if item["tag"] == "Alarm" and item["attributes"].get("Name") == name
    ]
    assert len(matches) == 1, f"expected exactly one Alarm named {name}"
    return dict(matches[0])


@pytest.fixture
def hmi(settings: Settings) -> ParseResult:
    return _parse(HMI_EXPORT, settings)


# ---------------------------------------------------------------------------
# The committed HMI export
# ---------------------------------------------------------------------------


def test_parser_declares_the_xml_contract(hmi: ParseResult) -> None:
    parser = XmlParser()

    assert isinstance(parser, Parser)
    assert parser.name == "xml"
    assert parser.handles == (FileKind.XML,)
    assert hmi.artifact_kind == FileKind.XML.value
    assert hmi.parser_name == "xml"
    assert hmi.parser_version == parser.version


def test_root_and_element_counts_match_the_hmi_export(hmi: ParseResult) -> None:
    assert hmi.content["root"] == {
        "tag": "AlarmConfiguration",
        "attributes": {"Version": "12.0", "Project": "SYNTHETIC_Line3_HMI"},
        "xpath": "/AlarmConfiguration",
    }
    assert hmi.content["element_counts"] == {
        "Alarm": 6,
        "AlarmConfiguration": 1,
        "AlarmGroup": 3,
        "AlarmGroups": 1,
        "Alarms": 1,
        "Comment": 1,
        "Message": 6,
    }
    assert hmi.content["max_depth"] == 3
    assert hmi.content["namespaces"] == {}
    assert hmi.content["attribute_names"] == [
        "AckRequired",
        "DisplayOrder",
        "Expression",
        "Group",
        "Latched",
        "Name",
        "Project",
        "Severity",
        "Version",
    ]
    # 19 elements, counted off the committed file, not re-derived from the parse.
    assert len(hmi.content["elements"]) == 19


def test_every_xpath_is_unique(hmi: ParseResult) -> None:
    """An xpath two elements share is not a citation, so uniqueness is the contract."""
    xpaths = [item["xpath"] for item in hmi.content["elements"]]

    # Pinned against the file: comparing the parse to itself would hold even if
    # the parser lost half the document.
    assert xpaths == HMI_XPATHS
    assert len(xpaths) == len(set(xpaths))
    assert len({anchor.key for anchor in hmi.anchors}) == len(hmi.anchors)


def test_repeated_siblings_get_positional_predicates(hmi: ParseResult) -> None:
    third = _element(hmi, f"{ALARMS}/Alarm[3]")
    assert third["attributes"] == {
        "Name": "ALM_CV102_OL",
        "Group": "Conveyors",
        "Severity": "2",
        "Latched": "true",
        "AckRequired": "true",
        "Expression": "CV102_OL_TRIP == 1",
    }
    assert third["depth"] == 2
    assert third["child_count"] == 1

    # A sole child carries no predicate; a repeated one always does.
    assert _element(hmi, ALARMS)["tag"] == "Alarms"
    assert [item["xpath"] for item in hmi.content["elements"] if item["tag"] == "AlarmGroup"] == [
        "/AlarmConfiguration/AlarmGroups/AlarmGroup[1]",
        "/AlarmConfiguration/AlarmGroups/AlarmGroup[2]",
        "/AlarmConfiguration/AlarmGroups/AlarmGroup[3]",
    ]


def test_the_jam_alarm_the_logic_never_implements_is_recorded_verbatim(hmi: ParseResult) -> None:
    """The HMI knows about the CV102 jam even though no rung implements it."""
    jam = _alarm(hmi, "ALM_CV102_JAM")

    assert jam["xpath"] == f"{ALARMS}/Alarm[5]"
    assert jam["attributes"]["Expression"] == "CV102_JAM == 1"
    assert jam["attributes"]["Group"] == "Conveyors"
    assert _element(hmi, f"{ALARMS}/Alarm[5]/Message")["text"] == (
        "CV102 jam detected - clear blockage before restart"
    )


def test_hopper_high_expression_keeps_its_tag_and_threshold(hmi: ParseResult) -> None:
    expression = _alarm(hmi, "ALM_HOPPER_HIGH")["attributes"]["Expression"]

    assert "HOPPER_LEVEL_PCT" in expression
    assert "90.0" in expression
    assert expression == "HOPPER_LEVEL_PCT > 90.0"
    assert "&gt;" not in expression


def test_guard_alarm_keeps_the_documented_spelling(hmi: ParseResult) -> None:
    """The HMI spelling differs from the logic's GRD_DR_CLSD; that is the finding."""
    expression = _alarm(hmi, "ALM_GUARD_OPEN")["attributes"]["Expression"]

    assert expression == "GUARD_DOOR_CLOSED == 0"
    assert "GRD_DR_CLSD" not in json.dumps(hmi.content)


def test_all_six_alarms_keep_their_expression_and_message_verbatim(hmi: ParseResult) -> None:
    """Counts do not catch a swap: every string an agent may cite is pinned.

    ``text_node_count == 7`` holds just as well if two messages trade places, and
    a message attached to the wrong alarm is a fabricated citation, not a
    rounding error.
    """
    recorded = [
        (
            item["attributes"]["Name"],
            item["attributes"]["Expression"],
            _element(hmi, f"{item['xpath']}/Message")["text"],
        )
        for item in hmi.content["elements"]
        if item["tag"] == "Alarm"
    ]

    assert recorded == HMI_ALARMS


def test_text_nodes_hold_the_alarm_messages_and_skip_whitespace(hmi: ParseResult) -> None:
    text_nodes = hmi.content["text_nodes"]

    assert [node["xpath"] for node in text_nodes] == [
        "/AlarmConfiguration/Comment",
        f"{ALARMS}/Alarm[1]/Message",
        f"{ALARMS}/Alarm[2]/Message",
        f"{ALARMS}/Alarm[3]/Message",
        f"{ALARMS}/Alarm[4]/Message",
        f"{ALARMS}/Alarm[5]/Message",
        f"{ALARMS}/Alarm[6]/Message",
    ]
    assert text_nodes[0]["text"] == (
        "SYNTHETIC FIXTURE. Invented HMI alarm export, not a real project."
    )
    assert _element(hmi, ALARMS)["text"] == ""


def test_every_element_is_anchored_by_its_xpath(hmi: ParseResult) -> None:
    assert len(hmi.anchors) == 19
    # Against the pinned addresses, not against the parse's own element list —
    # keys derived from the same output they are checked against agree by
    # construction even when both are wrong.
    assert [anchor.key for anchor in hmi.anchors] == [f"element:{xpath}" for xpath in HMI_XPATHS]
    assert {anchor.anchor_type for anchor in hmi.anchors} == {AnchorType.XML_ELEMENT}
    assert {anchor.evidence_kind for anchor in hmi.anchors} == {EvidenceKind.DIRECT}
    assert {anchor.source_label for anchor in hmi.anchors} == {"HMI_Alarm_Export.xml"}

    jam = hmi.anchors_by_key[f"element:{ALARMS}/Alarm[5]"]
    assert jam.structured_location == {
        "xpath": f"{ALARMS}/Alarm[5]",
        "tag": "Alarm",
        "attributes": {
            "Name": "ALM_CV102_JAM",
            "Group": "Conveyors",
            "Severity": "2",
            "Latched": "true",
            "AckRequired": "true",
            "Expression": "CV102_JAM == 1",
        },
    }


def test_anchor_excerpts_render_the_element_as_parsed(hmi: ParseResult) -> None:
    """A rendering, not a quotation: this export writes its start tags over two lines."""
    jam = hmi.anchors_by_key[f"element:{ALARMS}/Alarm[5]"]
    message = hmi.anchors_by_key[f"element:{ALARMS}/Alarm[5]/Message"]
    hopper = hmi.anchors_by_key[f"element:{ALARMS}/Alarm[4]"]

    assert jam.excerpt == (
        '<Alarm Name="ALM_CV102_JAM" Group="Conveyors" Severity="2" Latched="true" '
        'AckRequired="true" Expression="CV102_JAM == 1">'
    )
    assert message.excerpt == (
        "<Message>CV102 jam detected - clear blockage before restart</Message>"
    )
    # The parsed value is `>`; re-emitting it raw would make the rendering
    # something that is neither the file nor valid XML.
    assert _alarm(hmi, "ALM_HOPPER_HIGH")["attributes"]["Expression"] == "HOPPER_LEVEL_PCT > 90.0"
    assert 'Expression="HOPPER_LEVEL_PCT &gt; 90.0">' in hopper.excerpt
    assert all(len(anchor.excerpt) <= MAX_EXCERPT_CHARS for anchor in hmi.anchors)


def test_anchors_cite_the_file_by_name_not_by_path(hmi: ParseResult) -> None:
    """An absolute path in an anchor would leak the server layout and break re-parse equality."""
    for anchor in hmi.anchors:
        assert anchor.display_location.startswith("HMI_Alarm_Export.xml /AlarmConfiguration")

    rendered = json.dumps([anchor.display_location for anchor in hmi.anchors])
    assert str(FIXTURE_DIR) not in rendered
    assert str(FIXTURE_DIR) not in json.dumps(hmi.content)


def test_summary_is_counts_only(hmi: ParseResult) -> None:
    assert hmi.summary == {
        "root_tag": "AlarmConfiguration",
        "element_count": 19,
        "distinct_tag_count": 7,
        "max_depth": 3,
        "attribute_name_count": 9,
        "text_node_count": 7,
        "mixed_content_element_count": 0,
        "namespace_count": 0,
        "anchor_count": 19,
        "anchors_truncated": False,
    }
    assert all(isinstance(value, int | str) for value in hmi.summary.values())


def test_a_clean_export_reports_no_warnings_but_declares_its_boundary(hmi: ParseResult) -> None:
    assert hmi.warnings == []
    # Both boundaries hold for every document: markup this parser cannot see, and
    # excerpts that are re-rendered rather than quoted.
    assert [item.kind for item in hmi.unsupported] == ["xml_comment_or_pi", "excerpt_not_verbatim"]


def test_reparsing_the_same_bytes_produces_identical_output(settings: Settings) -> None:
    first = _parse(HMI_EXPORT, settings)
    second = _parse(HMI_EXPORT, settings)

    assert first.content == second.content
    assert json.dumps(first.content) == json.dumps(second.content)
    assert json.dumps(first.summary) == json.dumps(second.summary)
    assert [item.as_dict() for item in first.warnings] == [
        item.as_dict() for item in second.warnings
    ]
    assert [item.as_dict() for item in first.unsupported] == [
        item.as_dict() for item in second.unsupported
    ]

    def fingerprint(result: ParseResult) -> list[tuple[str, str, str, str]]:
        return [
            (
                anchor.key,
                anchor.display_location,
                anchor.excerpt,
                json.dumps(anchor.structured_location),
            )
            for anchor in result.anchors
        ]

    assert fingerprint(first) == fingerprint(second)
    assert [anchor.content_hash for anchor in first.anchors] == [
        anchor.content_hash for anchor in second.anchors
    ]


# ---------------------------------------------------------------------------
# Shapes the committed fixture does not have
# ---------------------------------------------------------------------------


def test_namespaces_are_recorded_and_tags_are_stripped(tmp_path: Path, settings: Settings) -> None:
    document = _write(
        tmp_path,
        "namespaced.xml",
        '<Root xmlns="urn:esco:default" xmlns:m="urn:esco:meta">'
        '<Item m:lang="en" Name="A">text</Item>'
        "<m:Note>meta</m:Note>"
        "</Root>",
    )

    result = _parse(document, settings)

    assert result.content["namespaces"] == {"": "urn:esco:default", "ns1": "urn:esco:meta"}
    assert [item["xpath"] for item in result.content["elements"]] == [
        "/Root",
        "/Root/Item",
        "/Root/Note",
    ]
    assert all("{" not in item["tag"] for item in result.content["elements"])
    # A qualified attribute keeps its namespace: stripping it could collide with
    # another attribute on the same element.
    assert result.content["attribute_names"] == ["Name", "{urn:esco:meta}lang"]
    assert "namespace_prefix" in [item.kind for item in result.unsupported]
    # An element records the URI its own name resolved in; an unqualified one
    # carries no such key at all, so the field is never a claim about a document
    # that has no namespaces.
    assert _element(result, "/Root/Note")["namespace"] == "urn:esco:meta"
    assert _element(result, "/Root/Item")["namespace"] == "urn:esco:default"


def test_two_tags_that_strip_to_the_same_name_keep_their_namespaces(
    tmp_path: Path, settings: Settings
) -> None:
    """Stripping the tag collapses two different names onto one xpath.

    ``/Root/Item[1]`` and ``/Root/Item[2]`` are the same tag in the record but
    different element names in the file. Without the URI on each element the
    collision is unrecoverable, and a reviewer reading the record cannot tell
    which vendor namespace a value came from.
    """
    document = _write(
        tmp_path,
        "nscollide.xml",
        '<Root xmlns="urn:a" xmlns:b="urn:b"><Item Name="from-a"/><b:Item Name="from-b"/></Root>',
    )

    result = _parse(document, settings)

    assert [item["xpath"] for item in result.content["elements"]] == [
        "/Root",
        "/Root/Item[1]",
        "/Root/Item[2]",
    ]
    assert _element(result, "/Root/Item[1]")["namespace"] == "urn:a"
    assert _element(result, "/Root/Item[2]")["namespace"] == "urn:b"
    assert result.anchors_by_key["element:/Root/Item[2]"].structured_location == {
        "xpath": "/Root/Item[2]",
        "tag": "Item",
        "attributes": {"Name": "from-b"},
        "namespace": "urn:b",
    }


def test_the_namespace_boundary_says_the_recorded_xpath_is_a_local_name_path(
    tmp_path: Path, settings: Settings
) -> None:
    """The xpath is not a namespace-aware expression; a reviewer must be told so."""
    document = _write(tmp_path, "nspath.xml", '<Root xmlns="urn:a"><Item Name="A"/></Root>')

    result = _parse(document, settings)
    boundary = [item for item in result.unsupported if item.kind == "namespace_prefix"]

    assert len(boundary) == 1
    detail = boundary[0].detail.lower()
    assert "local-name" in detail
    assert "not a namespace-aware xpath" in detail


def test_a_declared_prefix_that_nothing_uses_is_disclosed_not_reported_as_absent(
    tmp_path: Path, settings: Settings
) -> None:
    """'namespaces' is built from the names that were recorded, not from the file.

    A prefix declared on the root and used by no tag or attribute is resolved
    away before this parser sees the tree, so the parse alone shows an empty
    'namespaces' and namespace_count 0 — which reads as "this document declares
    none". That is a claim about the file the parse did not establish.
    """
    document = _write(
        tmp_path,
        "unused_ns.xml",
        '<Root xmlns:m="urn:esco:meta"><Item Name="A"/></Root>',
    )

    result = _parse(document, settings)
    boundary = [item for item in result.unsupported if item.kind == "namespace_prefix"]

    assert result.content["namespaces"] == {}
    assert result.summary["namespace_count"] == 0
    assert len(boundary) == 1
    assert "xmlns:m" in boundary[0].detail
    # It states what was seen and what was recorded, and claims nothing else.
    assert "start tag in the source declares" in boundary[0].detail
    assert "namespace_count is 0" in boundary[0].detail
    assert boundary[0].location == "/Root"


def test_no_namespace_declaration_is_claimed_from_an_attribute_value_that_mentions_it(
    tmp_path: Path, settings: Settings
) -> None:
    """Reading the start tag back must not turn a value into a declaration."""
    plain = _write(tmp_path, "plain_root.xml", '<Root Note="see xmlns=urn:x"><Item/></Root>')

    result = _parse(plain, settings)

    assert [item.kind for item in result.unsupported] == [
        "xml_comment_or_pi",
        "excerpt_not_verbatim",
    ]


def test_a_default_declaration_the_document_uses_is_reported_once_not_twice(
    tmp_path: Path, settings: Settings
) -> None:
    """The used-namespace boundary and the unused-declaration one are exclusive."""
    document = _write(tmp_path, "used_ns.xml", '<Root xmlns="urn:a"><Item/></Root>')

    result = _parse(document, settings)

    assert [item.kind for item in result.unsupported].count("namespace_prefix") == 1


def test_text_in_mixed_content_is_not_dropped(tmp_path: Path, settings: Settings) -> None:
    """The word "again" lives on the child's tail, not in the parent's own text."""
    document = _write(tmp_path, "mixed.xml", "<Doc><P>Hello <B>world</B> again</P></Doc>")

    result = _parse(document, settings)

    assert _element(result, "/Doc/P")["text"] == "Hello again"
    assert _element(result, "/Doc/P/B")["text"] == "world"
    assert result.content["text_nodes"] == [
        {"xpath": "/Doc/P", "text": "Hello again"},
        {"xpath": "/Doc/P/B", "text": "world"},
    ]


def test_text_interrupted_by_a_child_element_says_so_and_keeps_the_child_text(
    tmp_path: Path, settings: Settings
) -> None:
    """ILK-005's threshold lives in a child element; the joined string alone loses it.

    ``text`` is only the element's own runs, so on its own it reads as a
    complete expression that is missing its number. The element therefore has to
    record that its text is interrupted, where each run came from, and what the
    subtree actually says.
    """
    document = _write(
        tmp_path,
        "interlock.xml",
        "<Interlocks><Expression>AIR_PRESS_PSI &lt; "
        "<Threshold>60</Threshold> psi</Expression></Interlocks>",
    )

    result = _parse(document, settings)
    expression = _element(result, "/Interlocks/Expression")

    assert expression["text"] == "AIR_PRESS_PSI < psi"
    assert expression["text_segments"] == [
        {"origin": "own_text", "text": "AIR_PRESS_PSI < "},
        {"origin": "tail_of_child_1", "text": " psi"},
    ]
    assert expression["descendant_text"] == "AIR_PRESS_PSI < 60 psi"
    assert result.summary["mixed_content_element_count"] == 1

    boundary = [item for item in result.unsupported if item.kind == "mixed_content"]
    assert len(boundary) == 1
    assert "/Interlocks/Expression" in boundary[0].detail

    # The citation an agent would quote must not hide the threshold either.
    assert result.anchors_by_key["element:/Interlocks/Expression"].excerpt == (
        "<Expression>AIR_PRESS_PSI &lt; <Threshold>60</Threshold> psi</Expression>"
    )


def test_an_element_whose_text_is_one_run_is_not_reported_as_interrupted(
    tmp_path: Path, settings: Settings
) -> None:
    """The disclosure has to be specific, or it is noise on every document."""
    document = _write(tmp_path, "plain.xml", "<Doc><Message>Line stopped</Message></Doc>")

    result = _parse(document, settings)

    assert "text_segments" not in _element(result, "/Doc/Message")
    assert "descendant_text" not in _element(result, "/Doc/Message")
    assert result.summary["mixed_content_element_count"] == 0
    assert [item.kind for item in result.unsupported] == [
        "xml_comment_or_pi",
        "excerpt_not_verbatim",
    ]


def test_no_separator_is_invented_where_a_child_interrupts_an_identifier(
    tmp_path: Path, settings: Settings
) -> None:
    """A space the file does not contain would rename ILK-004's tag."""
    document = _write(tmp_path, "tag.xml", "<Tag>GUARD<Sub/>_DOOR_CLOSED</Tag>")

    result = _parse(document, settings)

    assert _element(result, "/Tag")["text"] == "GUARD_DOOR_CLOSED"
    assert result.content["text_nodes"][0]["text"] == "GUARD_DOOR_CLOSED"
    assert result.anchors_by_key["element:/Tag"].excerpt == ("<Tag>GUARD<Sub/>_DOOR_CLOSED</Tag>")


def test_a_child_between_two_runs_does_not_fuse_words_that_the_file_separates(
    tmp_path: Path, settings: Settings
) -> None:
    """The other direction: a space that *is* in the file must survive."""
    document = _write(tmp_path, "msg.xml", "<Msg>Hello <B>x</B>, world</Msg>")

    result = _parse(document, settings)

    assert _element(result, "/Msg")["text"] == "Hello , world"
    assert _element(result, "/Msg")["descendant_text"] == "Hello x, world"


def test_an_excerpt_is_declared_as_a_reconstruction_rather_than_a_quotation(
    tmp_path: Path, settings: Settings
) -> None:
    """A reviewer who cannot find the excerpt in the file must be told why."""
    document = _write(tmp_path, "note.xml", "<Multi>line one\nline two\nline three</Multi>")

    result = _parse(document, settings)
    boundary = [item for item in result.unsupported if item.kind == "excerpt_not_verbatim"]

    assert len(boundary) == 1
    assert "not" in boundary[0].detail.lower()
    assert "reconstruction" in boundary[0].detail.lower()
    # Whitespace is collapsed, so the excerpt genuinely is not in the file.
    excerpt = result.anchors_by_key["element:/Multi"].excerpt
    assert excerpt == "<Multi>line one line two line three</Multi>"
    assert excerpt not in document.read_text(encoding="utf-8")


def test_an_excerpt_stays_unambiguous_when_a_value_contains_markup_characters(
    tmp_path: Path, settings: Settings
) -> None:
    """Re-emitting a quote or an ampersand raw makes the attribute boundaries a guess."""
    document = _write(
        tmp_path,
        "quoted.xml",
        '<Alarm Note="he said &quot;stop&quot;" Amp="A &amp; B" Lt="x &lt; y" '
        'Expression="HOPPER_LEVEL_PCT &gt; 90.0">CV102 &amp; CV103 &lt; limit</Alarm>',
    )

    result = _parse(document, settings)
    excerpt = result.anchors_by_key["element:/Alarm"].excerpt

    assert excerpt == (
        '<Alarm Note="he said &quot;stop&quot;" Amp="A &amp; B" Lt="x &lt; y" '
        'Expression="HOPPER_LEVEL_PCT &gt; 90.0">CV102 &amp; CV103 &lt; limit</Alarm>'
    )
    # The proof that the boundaries are unambiguous: it parses back to the values
    # the element actually has.
    reparsed = parse_xml_bytes(excerpt.encode("utf-8"), settings)
    assert dict(reparsed.attrib) == _element(result, "/Alarm")["attributes"]
    assert reparsed.text == _element(result, "/Alarm")["text"]


def test_discarded_comments_are_declared_not_silently_omitted(
    tmp_path: Path, settings: Settings
) -> None:
    document = _write(
        tmp_path,
        "commented.xml",
        "<Root><!-- HIDDEN-NOTE interlock disabled --><?target data?><Child>ok</Child></Root>",
    )

    result = _parse(document, settings)

    assert "HIDDEN-NOTE" not in json.dumps(result.content)
    assert result.summary["element_count"] == 2
    boundary = [item for item in result.unsupported if item.kind == "xml_comment_or_pi"]
    assert len(boundary) == 1
    assert "comment" in boundary[0].detail.lower()


def test_anchor_count_is_capped_and_truncation_is_reported(
    tmp_path: Path, settings: Settings
) -> None:
    groups, per_group = 51, 100
    body = "".join(
        "<Group>" + "".join(f'<Item Index="{index}"/>' for index in range(per_group)) + "</Group>"
        for _ in range(groups)
    )
    document = _write(tmp_path, "large.xml", f"<Items>{body}</Items>")

    result = _parse(document, settings)

    expected_elements = 1 + groups + groups * per_group
    assert expected_elements > MAX_ANCHORS
    assert len(result.anchors) == MAX_ANCHORS
    # Capping citations must not cap the record of what is in the document.
    assert len(result.content["elements"]) == expected_elements
    assert result.summary["anchor_count"] == MAX_ANCHORS
    assert result.summary["anchors_truncated"] is True

    truncated = [item for item in result.warnings if item.code == WarningCode.TRUNCATED]
    assert len(truncated) == 1
    assert str(MAX_ANCHORS) in truncated[0].message
    assert str(expected_elements) in truncated[0].message
    last_anchored = result.content["elements"][MAX_ANCHORS - 1]["xpath"]
    assert result.anchors[-1].key == f"element:{last_anchored}"


def test_every_xpath_is_built_once_as_the_walk_descends(
    tmp_path: Path, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rebuilding a path from an element's ancestors rescans every sibling.

    A flat vendor export is exactly the shape this parser exists for, and there
    the rescan is quadratic: the work per element must not grow with the number
    of siblings it has. Counted rather than timed so the bound is deterministic.
    """
    siblings = 2_000
    body = "".join(f'<Point Name="P{index}"/>' for index in range(siblings))
    document = _write(tmp_path, "flat.xml", f"<Export>{body}</Export>")

    real = xml_safety.strip_namespace
    calls = 0

    def counted(tag: str) -> str:
        nonlocal calls
        calls += 1
        return real(tag)

    monkeypatch.setattr(xml_safety, "strip_namespace", counted)
    monkeypatch.setattr(xml_parser_module, "strip_namespace", counted)

    result = _parse(document, settings)

    assert result.summary["element_count"] == siblings + 1
    assert result.content["elements"][1]["xpath"] == "/Export/Point[1]"
    assert result.content["elements"][-1]["xpath"] == f"/Export/Point[{siblings}]"
    # Linear in the document, not in siblings-per-element (which would be ~4,000,000).
    assert calls <= 10 * (siblings + 1)


def test_recorded_xpaths_agree_with_the_shared_path_helper(
    tmp_path: Path, settings: Settings
) -> None:
    """The paths are built here now, so they are checked against the shared rule."""
    document = _write(
        tmp_path,
        "shapes.xml",
        '<Root xmlns:m="urn:esco:meta">'
        "<Only/>"
        "<Pair><Leaf/><Leaf/></Pair>"
        "<Pair><Leaf/></Pair>"
        "<m:Leaf/>"
        "</Root>",
    )

    result = _parse(document, settings)
    root = xml_safety.parse_xml_file(document, settings)

    assert [item["xpath"] for item in result.content["elements"]] == [
        xpath for _, xpath in iter_with_paths(root)
    ]
    assert [item["xpath"] for item in result.content["elements"]] == [
        "/Root",
        "/Root/Only",
        "/Root/Pair[1]",
        "/Root/Pair[1]/Leaf[1]",
        "/Root/Pair[1]/Leaf[2]",
        "/Root/Pair[2]",
        "/Root/Pair[2]/Leaf",
        "/Root/Leaf",
    ]
    assert [item["depth"] for item in result.content["elements"]] == [0, 1, 1, 2, 2, 1, 2, 1]


def test_root_only_document_is_reported_as_empty(tmp_path: Path, settings: Settings) -> None:
    document = _write(tmp_path, "bare.xml", "<AlarmConfiguration/>")

    result = _parse(document, settings)

    assert [item.code for item in result.warnings] == [WarningCode.EMPTY_DOCUMENT]
    assert result.content["root"]["tag"] == "AlarmConfiguration"
    assert result.content["elements"] == [
        {
            "xpath": "/AlarmConfiguration",
            "tag": "AlarmConfiguration",
            "depth": 0,
            "attributes": {},
            "text": "",
            "child_count": 0,
        }
    ]
    assert len(result.anchors) == 1


def test_an_empty_looking_document_reports_what_was_recorded_not_what_the_file_holds(
    tmp_path: Path, settings: Settings
) -> None:
    """A comment is content this parser cannot see, so it cannot call the file empty."""
    document = _write(
        tmp_path,
        "commented_only.xml",
        "<Root><!-- ALARM ALM_CV102_JAM disabled 2024 --></Root>",
    )

    result = _parse(document, settings)
    empty = [item for item in result.warnings if item.code == WarningCode.EMPTY_DOCUMENT]

    assert len(empty) == 1
    message = empty[0].message
    # The old wording asserted a property of the file ("is a single empty element
    # with no attributes, text or children") that the parser had not established.
    assert "recorded" in message
    assert "comment" in message.lower()
    assert "discarded" in message.lower()
    # And it says the discarded markup was actually observed in this file.
    assert "does contain a comment" in message
    # And only what it saw: there is no processing instruction in this file.
    assert "processing instruction" not in message.split("does contain")[1]


def test_the_empty_warning_does_not_claim_markup_it_did_not_see(
    tmp_path: Path, settings: Settings
) -> None:
    document = _write(tmp_path, "bare_root.xml", "<Root>   </Root>")

    result = _parse(document, settings)
    empty = [item for item in result.warnings if item.code == WarningCode.EMPTY_DOCUMENT]

    assert len(empty) == 1
    assert "does contain" not in empty[0].message


# ---------------------------------------------------------------------------
# Hostile and degenerate input
# ---------------------------------------------------------------------------


def test_a_long_filename_still_composes_a_label_the_column_can_hold(
    tmp_path: Path, settings: Settings
) -> None:
    """An anchor from a deep archive member must not overflow its column.

    SQLite stores an over-long value happily, so an unbounded label is invisible
    until a stricter backend rejects the upload.
    """
    document = _write(tmp_path, "long.xml", '<Root><Item Name="A"/></Root>')
    context = ParseContext(
        file_id=uuid.UUID(int=1),
        project_id=uuid.UUID(int=2),
        path=document,
        filename="C" * 300 + ".xml",
        file_kind=FileKind.XML,
        document_role=DocumentRole.HMI_EXPORT,
        settings=settings,
        archive_member_path="deep/" * 60 + "member.xml",
    )

    result = XmlParser().parse(context)

    assert result.anchors
    for anchor in result.anchors:
        assert len(anchor.source_label) <= MAX_SOURCE_LABEL_CHARS
        assert len(anchor.display_location) <= MAX_DISPLAY_LOCATION_CHARS


def test_external_entity_is_refused_and_the_secret_is_never_exposed(
    tmp_path: Path, settings: Settings
) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("TOPSECRET-controller-password", encoding="utf-8")
    document = _write(
        tmp_path,
        "xxe.xml",
        '<?xml version="1.0"?>\n'
        f'<!DOCTYPE cfg [<!ENTITY leak SYSTEM "file:///{secret.as_posix()}">]>\n'
        '<cfg><Alarm Name="&leak;"/></cfg>',
    )

    with pytest.raises(XmlSafetyError) as excinfo:
        _parse(document, settings)

    assert isinstance(excinfo.value, ParserError)
    exposed = json.dumps(excinfo.value.to_payload()) + str(excinfo.value)
    assert "TOPSECRET" not in exposed
    assert secret.read_text(encoding="utf-8") not in exposed


def test_malformed_xml_raises_a_parser_error(tmp_path: Path, settings: Settings) -> None:
    document = _write(tmp_path, "broken.xml", "<AlarmConfiguration><Alarms></AlarmConfiguration>")

    with pytest.raises(ParserError) as excinfo:
        _parse(document, settings)

    assert excinfo.value.code == "parser_error"
    assert "XML" in excinfo.value.message


def test_empty_file_raises_a_parser_error(tmp_path: Path, settings: Settings) -> None:
    document = tmp_path / "empty.xml"
    document.write_bytes(b"")

    with pytest.raises(ParserError) as excinfo:
        _parse(document, settings)

    assert "empty" in excinfo.value.message.lower()


def test_whitespace_only_file_raises_a_parser_error(tmp_path: Path, settings: Settings) -> None:
    document = _write(tmp_path, "blank.xml", "   \n\n")

    with pytest.raises(ParserError):
        _parse(document, settings)


def test_nesting_beyond_the_depth_limit_is_refused(tmp_path: Path, settings: Settings) -> None:
    levels = 10
    document = _write(tmp_path, "deep.xml", "<n>" * levels + "leaf" + "</n>" * levels)

    with pytest.raises(XmlSafetyError) as excinfo:
        _parse(document, settings.model_copy(update={"max_xml_depth": 4}))

    assert excinfo.value.details["max_depth"] == 4
    assert _parse(document, settings).content["max_depth"] == levels - 1
