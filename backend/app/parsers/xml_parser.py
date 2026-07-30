"""Generic XML.

Anything that is well-formed XML but is not a format the platform models
specifically — an HMI alarm export, a device configuration dump — lands here.
This parser records what XML actually is: a tree of named elements carrying
attributes and text, each addressed by an unambiguous path. It does not know
what an ``<Alarm>`` means and deliberately says nothing about it. The HMI
export's alarm for a conveyor jam the logic never implements is a finding for an
agent to make by comparing files, and it can only make it because the string
``ALM_CV102_JAM`` and its exact location were recorded.

Four things are worth knowing about the recorded shape:

* **Tags are namespace-stripped** so an xpath stays readable and matchable
  across documents; the namespaces in use are recorded separately. Attribute
  names are kept exactly as parsed (``{uri}local`` when qualified), because two
  attributes on one element can strip to the same name.
* **Every run of character data is recorded exactly once, and nothing is
  inserted between the runs.** An element's ``text`` is its own leading text
  followed by the trailing text of each direct child, concatenated in document
  order — a separator the file does not contain is never added, because
  ``GUARD`` and ``_DOOR_CLOSED`` either side of a child element are one
  identifier in the source, not two words. When an element's own text is
  interrupted by a child element (mixed content) the joined string is not one
  contiguous run of the file and the character data inside the child is not part
  of it, so such an element additionally records ``text_segments`` (each run and
  where it came from) and ``descendant_text`` (the whole subtree's character
  data in document order), and the result carries a ``mixed_content`` unsupported
  item naming those elements.
* **Comments, processing instructions and DTD content are discarded** by the
  hardened parser and cannot be recovered here. That boundary is recorded as an
  unsupported item rather than left looking like an absence of content.
* **An anchor excerpt is a normalised reconstruction, not a quotation.** The
  element is re-rendered from what was parsed — attributes in parse order with
  their values re-escaped, whitespace collapsed, comments already gone — so
  searching the source file for an excerpt may not find it. That is recorded as
  an unsupported item rather than left for a reviewer to discover.

All parsing goes through :mod:`app.security.xml_safety`; this module never
touches ``ElementTree`` directly. The document walk lives here so that each
element's xpath is built once, as the walk descends, instead of being recomputed
from that element's ancestors — recomputing it rescans every sibling of every
ancestor, which is quadratic on the flat vendor exports this parser exists for.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any
from xml.etree.ElementTree import Element

from app.core.enums import AnchorType, EvidenceKind, FileKind
from app.core.errors import ParserError
from app.parsers.base import AnchorDraft, ParseContext, ParseResult, WarningCode, truncate
from app.security.xml_safety import parse_xml_file, strip_namespace

__all__ = ["MAX_ANCHORS", "XmlParser"]

#: Most anchors one XML document may contribute. A vendor HMI export can hold
#: hundreds of thousands of elements; one anchor each would bury the evidence
#: store in citations no reviewer will ever open. Hitting the cap is reported as
#: a :attr:`~app.parsers.base.WarningCode.TRUNCATED` warning — the elements
#: themselves stay in ``content``, so nothing is silently dropped.
MAX_ANCHORS = 5_000

#: Longest run of element text kept in an anchor excerpt, so that a long text
#: node cannot push the attributes — usually the identifying part — out of view.
MAX_EXCERPT_TEXT_CHARS = 500

#: How many mixed-content xpaths the boundary item names before it stops listing
#: and gives a count instead. Every such element is flagged in ``content`` too.
MAX_NAMED_MIXED_CONTENT = 10

#: How much of the source is re-read to say what a document that recorded as
#: empty actually carries. Bounded: the file itself may be large.
EMPTY_DOCUMENT_SCAN_BYTES = 64 * 1024


def _collapse(text: str) -> str:
    """Collapse runs of whitespace, the only normalisation applied to text."""
    return " ".join(text.split())


def _collapse_run(text: str) -> str:
    """Collapse one run of character data, keeping whether it had edge spaces.

    Used when several runs are re-rendered next to each other: dropping a
    trailing space would fuse two words that the source keeps apart, and this is
    the only thing that separates ``Hello <B>x</B> again`` from ``GUARD``
    ``<Sub/>`` ``_DOOR_CLOSED``.
    """
    if not text:
        return ""
    stripped = _collapse(text)
    if not stripped:
        return " "
    lead = " " if text[:1].isspace() else ""
    trail = " " if text[-1:].isspace() else ""
    return f"{lead}{stripped}{trail}"


def _own_runs(element: Element) -> list[tuple[str, str]]:
    """The character-data runs belonging to this element, in document order.

    ``element.text`` plus the ``tail`` of each direct child, each paired with
    where it came from. Tails are included because in mixed content
    (``<p>Hello <b>x</b> again</p>``) the word "again" is stored on the child,
    and recording only ``element.text`` would drop it. Empty runs are omitted;
    nothing else is added or removed, so the recorded ``text`` is exactly
    ``_collapse("".join(run for _, run in runs))``.
    """
    runs: list[tuple[str, str]] = []
    if element.text:
        runs.append(("own_text", element.text))
    for index, child in enumerate(element, start=1):
        if child.tail:
            runs.append((f"tail_of_child_{index}", child.tail))
    return runs


def _descendant_text(element: Element) -> str:
    """Every character-data run in the subtree, in document order."""
    return _collapse("".join(element.itertext()))


def _walk(root: Element) -> Iterator[tuple[Element, str, int]]:
    """Depth-first walk yielding ``(element, xpath, depth)`` for every node.

    The xpath of a child is derived from its parent's — already-built — xpath and
    the parent's child-tag counts, so the whole walk costs one pass over the
    tree. A positional predicate is added exactly when a tag is repeated among
    its siblings, which is what makes every recorded xpath unambiguous.
    """
    stack: list[tuple[Element, str, int]] = [(root, f"/{strip_namespace(root.tag)}", 0)]
    while stack:
        element, xpath, depth = stack.pop()
        yield element, xpath, depth

        children = list(element)
        if not children:
            continue
        names = [strip_namespace(child.tag) for child in children]
        totals = Counter(names)
        seen: Counter[str] = Counter()
        frames: list[tuple[Element, str, int]] = []
        for child, name in zip(children, names, strict=True):
            if totals[name] > 1:
                seen[name] += 1
                child_xpath = f"{xpath}/{name}[{seen[name]}]"
            else:
                child_xpath = f"{xpath}/{name}"
            frames.append((child, child_xpath, depth + 1))
        stack.extend(reversed(frames))


def _namespace_uris(element: Element) -> list[str]:
    """URIs used by one element's tag and attribute names, in document order."""
    uris: list[str] = []
    for name in (element.tag, *element.attrib):
        if isinstance(name, str) and name.startswith("{"):
            uris.append(name[1:].split("}", 1)[0])
    return uris


def _namespace_labels(root_tag: str, uris: Iterable[str]) -> dict[str, str]:
    """Label each namespace URI found in the document.

    The hardened parser resolves and discards ``xmlns`` declarations, so the
    prefixes the author actually wrote are not recoverable. The labels here are
    the parser's own: ``""`` for the namespace the root element's name resolves
    in — the one the stripped tags in every recorded xpath belong to — then
    ``ns1``, ``ns2`` … in order of first appearance.
    """
    root_uri = root_tag[1:].split("}", 1)[0] if root_tag.startswith("{") else ""
    labels: dict[str, str] = {}
    counter = 0
    for uri in uris:
        if uri == root_uri:
            labels[""] = uri
            continue
        counter += 1
        labels[f"ns{counter}"] = uri
    return labels


def _escape_text(value: str) -> str:
    """Re-escape character data so the reconstruction is unambiguous XML."""
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _escape_attribute(value: str) -> str:
    return _escape_text(value).replace('"', "&quot;")


def _opening_tag(tag: str, attributes: dict[str, str]) -> str:
    rendered = "".join(
        f' {name}="{_escape_attribute(value)}"' for name, value in attributes.items()
    )
    return f"<{tag}{rendered}>"


def _append_run(parts: list[str], run: str) -> int:
    """Append a rendered run, without doubling a space at a run boundary."""
    if run.startswith(" ") and parts and parts[-1].endswith(" "):
        run = run[1:]
    if not run:
        return 0
    parts.append(run)
    return len(run)


def _render_body(element: Element, budget: int) -> str:
    """Render the character data of an element, showing the children in it.

    Only used for mixed content, where the element's own runs alone would read
    as one uninterrupted string and the text inside the child elements — an
    ``<Expression>``'s ``<Threshold>``, say — would be missing from a citation
    that looks complete. Rendering the child inline keeps the citation honest
    about both what is there and where it sits.
    """
    parts: list[str] = []
    length = 0
    if element.text:
        length += _append_run(parts, _escape_text(_collapse_run(element.text)))
    for child in element:
        if length >= budget:
            break
        tag = strip_namespace(child.tag)
        opening = _opening_tag(tag, dict(child.attrib))
        child_body = _render_body(child, budget - length)
        if child_body:
            length += _append_run(parts, f"{opening}{child_body}</{tag}>")
        else:
            length += _append_run(parts, f"{opening[:-1]}/>")
        if child.tail:
            length += _append_run(parts, _escape_text(_collapse_run(child.tail)))
    return "".join(parts)


def _excerpt(element: Element, tag: str, attributes: dict[str, str], text: str) -> str:
    """Re-render an element from what was parsed.

    This is a reconstruction, not a quotation: attributes come back in parse
    order, whitespace inside text is collapsed, and anything the hardened parser
    dropped (comments, processing instructions) is not here. Values are
    re-escaped so that the boundaries of an attribute holding a quote or an
    ampersand stay unambiguous — a qualified attribute *name* is still rendered
    as it was parsed (``{uri}local``), which is how the record spells it. A
    reviewer should not expect to find the string itself in the file.
    """
    opening = _opening_tag(tag, attributes)
    if len(element) and text:
        body = _render_body(element, MAX_EXCERPT_TEXT_CHARS).strip()
    else:
        body = _escape_text(text)
    if not body:
        return opening
    return f"{opening}{truncate(body, MAX_EXCERPT_TEXT_CHARS)}</{tag}>"


def _without_comments(body: bytes) -> bytes:
    """Drop comment spans, so markup quoted inside one is not read as markup."""
    kept: list[bytes] = []
    rest = body
    while True:
        start = rest.find(b"<!--")
        if start == -1:
            kept.append(rest)
            return b"".join(kept)
        kept.append(rest[:start])
        end = rest.find(b"-->", start + 4)
        if end == -1:  # unterminated within the bytes read; treat the rest as comment
            return b"".join(kept)
        rest = rest[end + 3 :]


def _discarded_markup(path: Path) -> str:
    """What discarded markup the head of the source file actually shows.

    Only consulted when the parse recorded nothing but a bare root, so that the
    warning can report what was seen in the file instead of asserting the file is
    empty — a comment holding a decommissioned alarm is exactly the content this
    parser cannot see. Names only what is there: nothing is claimed when the
    scanned head shows neither.
    """
    try:
        with path.open("rb") as handle:
            head = handle.read(EMPTY_DOCUMENT_SCAN_BYTES)
    except OSError:  # pragma: no cover - the file was readable moments earlier
        return ""
    body = head.lstrip()
    if body.startswith(b"<?xml"):
        _, _, body = body.partition(b"?>")
    found = []
    if b"<!--" in body:
        found.append("a comment")
    if b"<?" in _without_comments(body):
        found.append("a processing instruction")
    return " and ".join(found)


class XmlParser:
    """Records the structure and text of an arbitrary XML document."""

    name = "xml"
    version = "1.1.0"
    handles: tuple[FileKind, ...] = (FileKind.XML,)

    def parse(self, context: ParseContext) -> ParseResult:
        result = ParseResult(
            artifact_kind=FileKind.XML.value,
            parser_name=self.name,
            parser_version=self.version,
        )

        # Reported before parsing so an empty upload is named for what it is
        # rather than as a syntax error somewhere on line 1.
        try:
            size = context.path.stat().st_size
        except OSError as exc:
            raise ParserError(f"Cannot read XML file: {exc}") from exc
        if size == 0:
            raise ParserError("XML document is empty: the file contains no bytes.")

        root = parse_xml_file(context.path, context.settings)

        elements: list[dict[str, Any]] = []
        text_nodes: list[dict[str, str]] = []
        mixed_content: list[str] = []
        counts: dict[str, int] = {}
        attribute_names: set[str] = set()
        max_depth = 0
        # Insertion-ordered so the namespace labels are stable; a set here would
        # make the output depend on hash order.
        uris: dict[str, None] = {}

        for element, xpath, depth in _walk(root):
            tag = strip_namespace(element.tag)
            attributes = dict(element.attrib)
            runs = _own_runs(element)
            text = _collapse("".join(run for _, run in runs))
            max_depth = max(max_depth, depth)

            record: dict[str, Any] = {
                "xpath": xpath,
                "tag": tag,
                "depth": depth,
                "attributes": attributes,
                "text": text,
                "child_count": len(element),
            }
            # Mixed content: this element's text is interrupted by a child, so
            # the joined string above is not one contiguous run of the file and
            # does not include what is inside the child. Both facts are recorded
            # on the element rather than left for a reader to infer.
            if text and len(element):
                mixed_content.append(xpath)
                record["text_segments"] = [{"origin": origin, "text": run} for origin, run in runs]
                record["descendant_text"] = _descendant_text(element)
            elements.append(record)

            counts[tag] = counts.get(tag, 0) + 1
            attribute_names.update(attributes)
            for uri in _namespace_uris(element):
                uris.setdefault(uri, None)
            if text:
                text_nodes.append({"xpath": xpath, "text": text})

            if len(result.anchors) < MAX_ANCHORS:
                result.add_anchor(
                    AnchorDraft(
                        key=f"element:{xpath}",
                        anchor_type=AnchorType.XML_ELEMENT,
                        display_location=f"{context.filename} {xpath}",
                        structured_location={
                            "xpath": xpath,
                            "tag": tag,
                            "attributes": dict(attributes),
                        },
                        excerpt=_excerpt(element, tag, attributes, text),
                        evidence_kind=EvidenceKind.DIRECT,
                        source_label=context.source_label,
                    )
                )

        root_record = elements[0]
        root_xpath = str(root_record["xpath"])
        namespaces = _namespace_labels(str(root.tag), uris)

        result.content = {
            "root": {
                "tag": root_record["tag"],
                "attributes": dict(root.attrib),
                "xpath": root_xpath,
            },
            "elements": elements,
            "element_counts": {tag: counts[tag] for tag in sorted(counts)},
            "attribute_names": sorted(attribute_names),
            "namespaces": namespaces,
            "max_depth": max_depth,
            "text_nodes": text_nodes,
        }
        result.summary = {
            "root_tag": root_record["tag"],
            "element_count": len(elements),
            "distinct_tag_count": len(counts),
            "max_depth": max_depth,
            "attribute_name_count": len(attribute_names),
            "text_node_count": len(text_nodes),
            "mixed_content_element_count": len(mixed_content),
            "namespace_count": len(namespaces),
            "anchor_count": len(result.anchors),
            "anchors_truncated": len(elements) > len(result.anchors),
        }

        if len(elements) > len(result.anchors):
            result.warn(
                WarningCode.TRUNCATED,
                f"Anchored the first {MAX_ANCHORS} of {len(elements)} elements. "
                f"The remaining elements are recorded in the parsed content but "
                f"cannot be cited directly.",
                location=root_xpath,
            )
        if len(elements) == 1 and not root.attrib and not text_nodes:
            self._report_empty(result, context.path, root_record["tag"], root_xpath)

        result.mark_unsupported(
            "xml_comment_or_pi",
            "Comments, processing instructions and DTD content are discarded by "
            "the hardened XML parser and are not recorded. An instruction left "
            "in a comment in the source file will not appear in this artifact.",
            location=root_xpath,
        )
        result.mark_unsupported(
            "excerpt_not_verbatim",
            "Anchor excerpts are a normalised reconstruction of the element, not "
            "a quotation of the source: attributes are re-rendered in parse order "
            "with their values re-escaped, a qualified attribute name is rendered "
            "as '{uri}local', runs of whitespace inside text are collapsed to a "
            "single space, and discarded markup is absent. Searching the file for "
            "an excerpt may therefore not match it; the xpath is the address to "
            "follow, and 'attributes' and 'text' on the element hold the values as "
            "parsed.",
            location=root_xpath,
        )
        if mixed_content:
            self._report_mixed_content(result, mixed_content, root_xpath)
        if namespaces:
            result.mark_unsupported(
                "namespace_prefix",
                "Namespace prefixes are resolved and discarded while parsing, so "
                "the prefixes written in the file are not recoverable. The keys of "
                "'namespaces' are parser-assigned labels ('' for the root's "
                "namespace, then ns1, ns2, ...) and element tags are recorded "
                "without their namespace.",
                location=root_xpath,
            )

        return result

    @staticmethod
    def _report_empty(result: ParseResult, path: Path, tag: str, root_xpath: str) -> None:
        """Report an empty parse as what was recorded, not as what the file holds.

        The parser cannot see a comment, so it must not say the document has
        nothing in it — only that nothing was recorded beyond the root.
        """
        message = (
            f"No elements, attributes or text were recorded beyond the root <{tag}>. "
            f"Comments, processing instructions and DTD content are discarded before "
            f"this parser sees them and are not counted here, so this is what was "
            f"recorded rather than a statement about what the file contains."
        )
        discarded = _discarded_markup(path)
        if discarded:
            message += f" The start of the source file does contain {discarded}."
        result.warn(WarningCode.EMPTY_DOCUMENT, message, location=root_xpath)

    @staticmethod
    def _report_mixed_content(result: ParseResult, xpaths: list[str], root_xpath: str) -> None:
        named = ", ".join(xpaths[:MAX_NAMED_MIXED_CONTENT])
        if len(xpaths) > MAX_NAMED_MIXED_CONTENT:
            named += f" and {len(xpaths) - MAX_NAMED_MIXED_CONTENT} more"
        result.mark_unsupported(
            "mixed_content",
            f"{len(xpaths)} element(s) carry text that is interrupted by a child "
            f"element: {named}. Their 'text' is the element's own runs concatenated "
            f"in document order, so it is not one contiguous run of the file and it "
            f"does not include the character data inside the child elements. Each "
            f"such element records 'text_segments' (the runs and where each came "
            f"from) and 'descendant_text' (the whole subtree's text in document "
            f"order); the child elements are also recorded at their own xpaths.",
            location=root_xpath,
        )
