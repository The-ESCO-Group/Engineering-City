"""The one place that knows how to escape, and the only place it has to be right.

The ``]]>`` case is the one worth stating plainly: a rung comment containing the
CDATA terminator is unlikely and entirely possible, and a generator that
corrupts the document when it meets one produces a file that fails at import for
a reason nobody would ever guess.
"""

from __future__ import annotations

import pytest
from defusedxml.ElementTree import fromstring

from app.generators.rockwell.xml_writer import (
    XML_DECLARATION,
    XmlElement,
    cdata_section,
    element,
    escape_attribute,
    escape_text,
    normalise_text,
    serialise,
)


def _parse_back(data: bytes) -> object:
    return fromstring(data.decode("utf-8"))


class TestEscaping:
    def test_text_escapes_the_three_that_matter(self) -> None:
        assert escape_text('a < b & c > d "e"') == 'a &lt; b &amp; c &gt; d "e"'

    def test_attribute_escapes_quotes_and_folds_control_whitespace(self) -> None:
        # A literal newline in an attribute is normalised to a space by any XML
        # parser, so writing it literally would silently rewrite the value.
        assert escape_attribute('a"b') == "a&quot;b"
        assert escape_attribute("a\nb") == "a&#10;b"
        assert escape_attribute("a\tb") == "a&#9;b"

    def test_normalise_folds_line_endings_and_drops_unrepresentable_characters(self) -> None:
        assert normalise_text("a\r\nb\rc") == "a\nb\nc"
        assert normalise_text("a\x00b\x08c") == "abc"

    def test_cdata_wraps_plain_text_untouched(self) -> None:
        assert cdata_section("XIC(A)OTE(B);") == "<![CDATA[XIC(A)OTE(B);]]>"

    def test_cdata_splits_a_contained_terminator(self) -> None:
        section = cdata_section("before]]>after")
        assert "]]>after]]>" not in section
        parsed = _parse_back(f"<Text>{section}</Text>".encode())
        assert "".join(parsed.itertext()) == "before]]>after"  # type: ignore[attr-defined]

    def test_a_rung_comment_containing_the_terminator_survives_a_round_trip(self) -> None:
        comment = "Guard: the sequence ]]> came from a pasted snippet."
        document = serialise(element("Comment", cdata=comment))
        root = _parse_back(document)
        assert "".join(root.itertext()) == comment  # type: ignore[attr-defined]


class TestSerialisation:
    def test_declaration_indentation_and_trailing_newline_are_fixed(self) -> None:
        tree = element(
            "Root",
            {"A": "1"},
            children=(element("Child", {"B": "2"}, text="x"), element("Empty")),
        )
        assert serialise(tree).decode("utf-8") == (
            f'{XML_DECLARATION}\n<Root A="1">\n  <Child B="2">x</Child>\n  <Empty/>\n</Root>\n'
        )

    def test_line_endings_are_lf_even_on_windows(self) -> None:
        data = serialise(element("Root", children=(element("Child"),)))
        assert b"\r" not in data

    def test_the_same_tree_always_serialises_to_the_same_bytes(self) -> None:
        def build() -> XmlElement:
            return element(
                "Root",
                {"Z": "last", "A": "first"},
                children=(element("Child", cdata="text"),),
            )

        assert serialise(build()) == serialise(build())

    def test_attribute_order_is_the_order_written(self) -> None:
        data = serialise(element("Root", {"Z": "1", "A": "2"})).decode("utf-8")
        assert data.index('Z="1"') < data.index('A="2"')

    def test_absent_attributes_and_children_are_omitted_not_blanked(self) -> None:
        # ``MinorRev=""`` would claim the source stated a revision and left it
        # blank, which is a different fact from never having stated one.
        data = serialise(element("Tag", {"Name": "X", "MinorRev": None}, children=(None,)))
        assert b"MinorRev" not in data
        assert data.decode("utf-8").endswith('<Tag Name="X"/>\n')


class TestElementRules:
    def test_mixed_content_is_refused(self) -> None:
        with pytest.raises(ValueError, match="more than one kind of content"):
            XmlElement(tag="Root", children=(element("Child"),), text="also text")

    def test_a_repeated_attribute_is_refused(self) -> None:
        with pytest.raises(ValueError, match="repeats an attribute"):
            XmlElement(tag="Root", attributes=(("Name", "a"), ("Name", "b")))

    @pytest.mark.parametrize("bad", ["", "1Name", "Na me", "Na<me"])
    def test_an_unusable_element_name_is_refused(self, bad: str) -> None:
        with pytest.raises(ValueError, match="usable XML element name"):
            XmlElement(tag=bad)
