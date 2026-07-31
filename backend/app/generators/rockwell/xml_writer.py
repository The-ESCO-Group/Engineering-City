"""Deterministic XML serialisation, and the one place that knows how to escape.

Everything in this package that writes markup builds an :class:`XmlElement` tree
and hands it to :func:`serialise`. Nothing concatenates strings and escapes by
hand, because escaping scattered across a generator is escaping that is wrong in
one of the places nobody tested.

Two properties are load-bearing, and both are the reason ``xml.dom.minidom`` is
not used here:

* **The same tree always serialises to the same bytes.** ``minidom``'s pretty
  printer has changed its whitespace handling between CPython releases, so a
  package regenerated on a different interpreter would differ from an approved
  one for no engineering reason at all — and a checksum that moves when the
  interpreter moves proves nothing. Indentation, attribute order, line endings
  and the trailing newline are all fixed here.
* **Attribute order is the order the caller wrote.** Attributes are stored as a
  tuple of pairs rather than a mapping, so no iteration order is inherited from
  anywhere.

Line endings are always ``\\n``, including on Windows, and the result is
``bytes`` so no text-mode translation can reintroduce ``\\r\\n``.

Rung text and descriptions go in CDATA, which is what Studio 5000 writes and
what :mod:`app.parsers.l5x_parser` reads. A CDATA section cannot contain the
sequence ``]]>``; :func:`cdata_section` splits the run across two sections
rather than corrupting the document or silently dropping the text. A rung
comment that happens to contain ``]]>`` is unlikely and entirely possible, and
"the export was fine until somebody pasted a snippet into a comment" is not a
failure this generator is willing to have.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

__all__ = [
    "CDATA_CLOSE",
    "CDATA_OPEN",
    "ENCODING",
    "INDENT",
    "NEWLINE",
    "XML_DECLARATION",
    "XmlElement",
    "cdata_section",
    "element",
    "escape_attribute",
    "escape_text",
    "normalise_text",
    "serialise",
]

#: Written verbatim as the first line of every document.
XML_DECLARATION = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
INDENT = "  "
NEWLINE = "\n"
ENCODING = "utf-8"

CDATA_OPEN = "<![CDATA["
CDATA_CLOSE = "]]>"

#: The escape that keeps ``]]>`` inside a CDATA section: close the section after
#: the two brackets and open a new one before the ``>``. Both halves are CDATA,
#: so a parser concatenates them back into the original text.
_CDATA_SPLIT = "]]" + CDATA_CLOSE + CDATA_OPEN + ">"

#: Characters XML 1.0 cannot represent at all — not as a literal, not as a
#: character reference, not inside CDATA. They are removed rather than escaped
#: because there is nothing to escape them *to*; a document that carried them
#: would be rejected by every parser including our own. Carriage returns are
#: absent from this class because :func:`normalise_text` has already folded
#: them into newlines.
_ILLEGAL_XML_CHARS = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")

#: Deliberately narrower than the XML production for a name. Every element and
#: attribute name in this package is a literal from the Logix vocabulary, so an
#: exotic name means a bug in the caller rather than an unusual export.
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")


def normalise_text(value: str) -> str:
    """Fold line endings to ``\\n`` and drop characters XML cannot carry.

    An XML parser normalises ``\\r\\n`` and ``\\r`` to ``\\n`` while reading, so
    writing a carriage return means writing bytes that do not survive a round
    trip. Folding on the way out is what makes "generate, parse, compare"
    a check of the generator rather than a check of line-ending luck.
    """
    folded = value.replace("\r\n", "\n").replace("\r", "\n")
    return _ILLEGAL_XML_CHARS.sub("", folded)


def escape_text(value: str) -> str:
    """Escape ``value`` for use as element character data.

    ``>`` is escaped as well as ``<`` and ``&``. It does not have to be, but
    escaping it means the sequence ``]]>`` can never appear in text content by
    accident, which is one fewer way to produce a document that parses
    differently from the way it reads.
    """
    return normalise_text(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def escape_attribute(value: str) -> str:
    """Escape ``value`` for use inside a double-quoted attribute.

    Tab and newline become character references rather than being left literal.
    XML attribute-value normalisation turns a literal tab or newline into a
    space while parsing, so leaving them literal would quietly rewrite the
    value; the reference survives.
    """
    escaped = (
        normalise_text(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )
    return escaped.replace("\t", "&#9;").replace("\n", "&#10;")


def cdata_section(value: str) -> str:
    """Wrap ``value`` in one or more CDATA sections.

    A ``]]>`` inside the text is split across two sections. The result is still
    exactly one logical run of character data, so a parser reading it back gets
    the original string.
    """
    return CDATA_OPEN + normalise_text(value).replace(CDATA_CLOSE, _CDATA_SPLIT) + CDATA_CLOSE


@dataclass(frozen=True, slots=True)
class XmlElement:
    """One element in a document that is about to be serialised.

    An element carries *either* children, *or* text, *or* CDATA, or nothing at
    all. Mixed content — text alongside child elements — is representable in XML
    and is not representable here, because nothing in a Logix export needs it and
    supporting it would make the serialiser's whitespace handling a judgement
    call rather than a rule.
    """

    tag: str
    #: Pairs rather than a mapping: the emitted order is the order the caller
    #: wrote, and it cannot be inherited from a dictionary built elsewhere.
    attributes: tuple[tuple[str, str], ...] = ()
    children: tuple[XmlElement, ...] = ()
    text: str | None = None
    cdata: str | None = None

    def __post_init__(self) -> None:
        if not _NAME.match(self.tag):
            raise ValueError(f"Not a usable XML element name: {self.tag!r}")
        for name, _ in self.attributes:
            if not _NAME.match(name):
                raise ValueError(f"Not a usable XML attribute name on <{self.tag}>: {name!r}")
        occupied = sum(
            (1 if self.children else 0, 1 if self.text is not None else 0, 1 if self.cdata else 0)
        )
        if occupied > 1:
            raise ValueError(
                f"<{self.tag}> was given more than one kind of content. An element carries "
                "children, or text, or CDATA — mixed content is not supported here."
            )
        seen = {name for name, _ in self.attributes}
        if len(seen) != len(self.attributes):
            raise ValueError(f"<{self.tag}> repeats an attribute name.")


def element(
    tag: str,
    attributes: Mapping[str, str | None] | None = None,
    *,
    children: Sequence[XmlElement | None] = (),
    text: str | None = None,
    cdata: str | None = None,
) -> XmlElement:
    """Build an :class:`XmlElement`, dropping absent attributes and children.

    An attribute whose value is ``None`` is **omitted**, which is not the same
    as writing it empty: ``MinorRev=""`` claims the export stated a revision and
    it was blank, while omitting it says the source never stated one. The same
    applies to a ``None`` child, so an optional ``<Description>`` can be built
    inline without a branch at every call site.
    """
    pairs = tuple((name, value) for name, value in (attributes or {}).items() if value is not None)
    kept = tuple(child for child in children if child is not None)
    return XmlElement(tag=tag, attributes=pairs, children=kept, text=text, cdata=cdata)


def _render(node: XmlElement, depth: int, out: list[str]) -> None:
    pad = INDENT * depth
    attributes = "".join(f' {name}="{escape_attribute(value)}"' for name, value in node.attributes)
    if node.children:
        out.append(f"{pad}<{node.tag}{attributes}>")
        for child in node.children:
            _render(child, depth + 1, out)
        out.append(f"{pad}</{node.tag}>")
        return
    if node.cdata is not None:
        out.append(f"{pad}<{node.tag}{attributes}>{cdata_section(node.cdata)}</{node.tag}>")
        return
    if node.text is not None:
        out.append(f"{pad}<{node.tag}{attributes}>{escape_text(node.text)}</{node.tag}>")
        return
    out.append(f"{pad}<{node.tag}{attributes}/>")


def serialise(root: XmlElement, *, declaration: bool = True) -> bytes:
    """Serialise a tree to UTF-8 bytes, with fixed indentation and line endings.

    Bytes rather than ``str`` so that nothing downstream can open the file in
    text mode on Windows and turn every ``\\n`` into ``\\r\\n``, which would make
    the same tree hash differently on two machines.
    """
    lines: list[str] = [XML_DECLARATION] if declaration else []
    _render(root, 0, lines)
    return (NEWLINE.join(lines) + NEWLINE).encode(ENCODING)
