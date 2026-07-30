"""Hardened XML parsing.

Every XML document the platform reads — L5X exports, HMI exports, alarm files —
is untrusted. Python's stdlib ``ElementTree`` is vulnerable to entity-expansion
attacks (billion laughs, quadratic blowup) and, on some parsers, to external
entity resolution that can read local files or make network requests.

``defusedxml`` closes those. This module is the *only* place XML is parsed, so
there is one implementation to audit and no path around it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from xml.etree.ElementTree import Element

from defusedxml.ElementTree import ParseError as DefusedParseError
from defusedxml.ElementTree import fromstring as _defused_fromstring
from defusedxml.ElementTree import parse as _defused_parse

from app.core.config import Settings
from app.core.errors import FileRejectedError, ParserError

__all__ = [
    "XmlSafetyError",
    "element_path",
    "iter_with_paths",
    "parse_xml_bytes",
    "parse_xml_file",
    "strip_namespace",
]


class XmlSafetyError(ParserError):
    """The document was refused on safety grounds rather than being malformed."""

    code = "xml_rejected"


def _check_size(size: int, settings: Settings) -> None:
    if size > settings.max_xml_bytes:
        raise XmlSafetyError(
            f"XML document is {size:,} bytes, above the {settings.max_xml_bytes:,} limit.",
            details={"size_bytes": size},
        )


def _check_depth(root: Element, settings: Settings) -> None:
    """Reject a pathologically deep tree.

    defusedxml already stops entity expansion; depth is a separate axis — a
    document with 100k nested elements is legal XML that will exhaust the stack
    of any recursive consumer downstream.
    """
    depth = 0
    frontier = [(root, 1)]
    while frontier:
        element, level = frontier.pop()
        depth = max(depth, level)
        if depth > settings.max_xml_depth:
            raise XmlSafetyError(
                f"XML nesting exceeds {settings.max_xml_depth} levels.",
                details={"max_depth": settings.max_xml_depth},
            )
        frontier.extend((child, level + 1) for child in element)


def parse_xml_bytes(data: bytes, settings: Settings) -> Element:
    """Parse XML from memory with entity expansion and DTDs disabled."""
    _check_size(len(data), settings)
    try:
        root = _defused_fromstring(data)
    except DefusedParseError as exc:
        raise ParserError(f"Malformed XML: {exc}") from exc
    except ValueError as exc:
        # defusedxml raises EntitiesForbidden / DTDForbidden (ValueError
        # subclasses) when a document tries to use them.
        raise XmlSafetyError(
            "XML document uses entities or a DTD, which are disabled for "
            f"safety: {type(exc).__name__}"
        ) from exc

    if root is None:
        raise ParserError("XML document has no root element.")
    _check_depth(root, settings)
    return root


def parse_xml_file(path: Path, settings: Settings) -> Element:
    """Parse XML from disk with the same protections."""
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ParserError(f"Cannot read XML file: {exc}") from exc
    _check_size(size, settings)

    try:
        tree = _defused_parse(str(path))
    except DefusedParseError as exc:
        raise ParserError(f"Malformed XML: {exc}") from exc
    except ValueError as exc:
        raise XmlSafetyError(
            "XML document uses entities or a DTD, which are disabled for "
            f"safety: {type(exc).__name__}"
        ) from exc

    root = tree.getroot()
    if root is None:
        raise ParserError("XML document has no root element.")
    _check_depth(root, settings)
    return root


def strip_namespace(tag: str) -> str:
    """``{urn:x}Rung`` -> ``Rung``."""
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def element_path(ancestors: list[Element], element: Element) -> str:
    """Build an XPath-like locator with positional predicates.

    Positions make the path unambiguous, which matters because it is what an
    evidence anchor stores and what a reviewer follows back to the source.
    """
    parts: list[str] = []
    chain = [*ancestors, element]
    for index, node in enumerate(chain):
        name = strip_namespace(node.tag)
        if index == 0:
            parts.append(f"/{name}")
            continue
        parent = chain[index - 1]
        siblings = [child for child in parent if strip_namespace(child.tag) == name]
        if len(siblings) > 1:
            position = siblings.index(node) + 1
            parts.append(f"/{name}[{position}]")
        else:
            parts.append(f"/{name}")
    return "".join(parts)


def iter_with_paths(root: Element) -> Any:
    """Depth-first walk yielding ``(element, xpath)`` for every node."""
    stack: list[tuple[Element, list[Element]]] = [(root, [])]
    while stack:
        element, ancestors = stack.pop()
        yield element, element_path(ancestors, element)
        stack.extend((child, [*ancestors, element]) for child in reversed(list(element)))


def guard_xml_upload(head: bytes) -> None:
    """Reject an XML upload whose header already shows a disabled feature.

    Catching this at intake means the user is told why the file was refused,
    rather than discovering it as a parse failure later in the workflow.
    """
    prefix = head[:4096].lstrip()
    lowered = prefix.lower()
    if b"<!doctype" in lowered:
        raise FileRejectedError(
            "XML document declares a DTD. DTDs are disabled because they enable "
            "external-entity and entity-expansion attacks. Re-export without one.",
        )
    if b"<!entity" in lowered:
        raise FileRejectedError(
            "XML document defines entities, which are disabled for safety.",
        )
