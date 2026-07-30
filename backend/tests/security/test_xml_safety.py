"""Hardened XML parsing.

Every XML document the platform ingests is untrusted, so this module is the only
place XML is parsed. These tests pin the three guarantees that make that safe:
entities are never expanded (no XXE, no billion laughs), size and depth are
bounded before a downstream consumer sees the tree, and an upload carrying a DTD
is refused at intake with an explanation the engineer can act on.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.config import Settings
from app.core.errors import FileRejectedError, ParserError
from app.security.xml_safety import (
    XmlSafetyError,
    element_path,
    guard_xml_upload,
    iter_with_paths,
    parse_xml_bytes,
    parse_xml_file,
    strip_namespace,
)

#: Synthetic marker written into a temp file that an XXE payload tries to read.
SECRET = "SYNTHETIC-SECRET-9137-MUST-NOT-LEAK"


def _limited(settings: Settings, **overrides: object) -> Settings:
    """A copy of the fixture settings with tighter XML limits, to keep tests fast."""
    return settings.model_copy(update=overrides)


def _nested_document(levels: int) -> bytes:
    """A legal document nested exactly ``levels`` elements deep."""
    return (("<n>" * levels) + ("</n>" * levels)).encode()


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def _external_entity_document(secret_path: Path) -> str:
    return (
        '<?xml version="1.0"?>'
        f'<!DOCTYPE root [<!ENTITY leak SYSTEM "file:///{secret_path.as_posix()}">]>'
        "<root>&leak;</root>"
    )


def _assert_secret_absent(error: XmlSafetyError) -> None:
    assert SECRET not in str(error)
    assert SECRET not in repr(error.details)


# --------------------------------------------------------------------------- happy path


def test_wellformed_document_parses_to_a_usable_tree(settings: Settings) -> None:
    document = (
        b'<Controller name="Line3">'
        b"<Programs><Program><Routine>MainRoutine</Routine></Program></Programs>"
        b"</Controller>"
    )

    root = parse_xml_bytes(document, settings)

    assert root.tag == "Controller"
    assert root.attrib == {"name": "Line3"}
    assert [child.tag for child in root] == ["Programs"]
    assert root.findtext("./Programs/Program/Routine") == "MainRoutine"


def test_wellformed_file_parses_to_the_same_tree(settings: Settings, tmp_path: Path) -> None:
    path = _write(tmp_path / "controller.xml", '<Controller name="Line3"><Rung/></Controller>')

    root = parse_xml_file(path, settings)

    assert root.tag == "Controller"
    assert root.attrib == {"name": "Line3"}
    assert [child.tag for child in root] == ["Rung"]


# --------------------------------------------------------------------------- malformed


def test_malformed_bytes_raise_a_parser_error_not_a_safety_error(settings: Settings) -> None:
    """A broken export is a parse failure, not a refusal; the codes must not be confused."""
    with pytest.raises(ParserError) as excinfo:
        parse_xml_bytes(b"<Controller><Rung></Controller>", settings)

    assert not isinstance(excinfo.value, XmlSafetyError)
    assert excinfo.value.code == "parser_error"
    assert "Malformed XML" in str(excinfo.value)


def test_malformed_file_raises_a_parser_error(settings: Settings, tmp_path: Path) -> None:
    path = _write(tmp_path / "broken.xml", "<Controller><Rung></Controller>")

    with pytest.raises(ParserError) as excinfo:
        parse_xml_file(path, settings)

    assert not isinstance(excinfo.value, XmlSafetyError)
    assert "Malformed XML" in str(excinfo.value)


def test_empty_document_raises_a_parser_error(settings: Settings) -> None:
    with pytest.raises(ParserError):
        parse_xml_bytes(b"   ", settings)


def test_unreadable_path_raises_a_parser_error(settings: Settings, tmp_path: Path) -> None:
    """A missing artifact must surface as a reportable parser error, not an OSError."""
    with pytest.raises(ParserError) as excinfo:
        parse_xml_file(tmp_path / "absent.xml", settings)

    assert "Cannot read XML file" in str(excinfo.value)


# --------------------------------------------------------------------------- size limit


def test_document_over_the_size_limit_is_refused(settings: Settings) -> None:
    oversized = b"<Controller>" + b"x" * 200 + b"</Controller>"

    with pytest.raises(XmlSafetyError) as excinfo:
        parse_xml_bytes(oversized, _limited(settings, max_xml_bytes=64))

    assert excinfo.value.code == "xml_rejected"
    assert excinfo.value.details == {"size_bytes": len(oversized)}


def test_document_exactly_at_the_size_limit_is_accepted(settings: Settings) -> None:
    """The limit is inclusive, so a document sized to the documented maximum must parse."""
    document = b"<Controller/>"

    root = parse_xml_bytes(document, _limited(settings, max_xml_bytes=len(document)))

    assert root.tag == "Controller"


def test_file_over_the_size_limit_is_refused_without_reading_it(
    settings: Settings, tmp_path: Path
) -> None:
    """The size check uses stat(), so a huge file is never pulled into memory."""
    oversized = "<Controller>" + "x" * 200 + "</Controller>"
    path = _write(tmp_path / "big.xml", oversized)

    with pytest.raises(XmlSafetyError) as excinfo:
        parse_xml_file(path, _limited(settings, max_xml_bytes=64))

    assert excinfo.value.details == {"size_bytes": len(oversized)}


# --------------------------------------------------------------------------- depth limit


def test_nesting_past_the_depth_limit_is_refused(settings: Settings) -> None:
    """Deep-but-legal XML would blow the stack of any recursive consumer downstream."""
    with pytest.raises(XmlSafetyError) as excinfo:
        parse_xml_bytes(_nested_document(6), _limited(settings, max_xml_depth=5))

    assert excinfo.value.code == "xml_rejected"
    assert excinfo.value.details == {"max_depth": 5}
    assert "exceeds 5 levels" in str(excinfo.value)


def test_nesting_exactly_at_the_depth_limit_is_accepted(settings: Settings) -> None:
    root = parse_xml_bytes(_nested_document(5), _limited(settings, max_xml_depth=5))

    assert root.tag == "n"


def test_nesting_past_the_depth_limit_is_refused_from_a_file(
    settings: Settings, tmp_path: Path
) -> None:
    path = _write(tmp_path / "deep.xml", _nested_document(6).decode())

    with pytest.raises(XmlSafetyError) as excinfo:
        parse_xml_file(path, _limited(settings, max_xml_depth=5))

    assert excinfo.value.details == {"max_depth": 5}


def test_depth_is_measured_over_every_branch(settings: Settings) -> None:
    """Depth is the maximum across the whole tree; a shallow sibling must not excuse a deep one."""
    document = b"<a><shallow/><b><c><d><e><f/></e></d></c></b></a>"

    with pytest.raises(XmlSafetyError):
        parse_xml_bytes(document, _limited(settings, max_xml_depth=4))


# --------------------------------------------------------------------------- XXE


def test_external_file_entity_is_refused_and_the_file_contents_never_appear(
    settings: Settings, tmp_path: Path
) -> None:
    """The classic XXE read: refusing it is the only thing stopping local file disclosure."""
    secret_path = _write(tmp_path / "secret.txt", SECRET)

    with pytest.raises(XmlSafetyError) as excinfo:
        parse_xml_bytes(_external_entity_document(secret_path).encode(), settings)

    _assert_secret_absent(excinfo.value)


def test_external_file_entity_in_an_uploaded_file_is_refused(
    settings: Settings, tmp_path: Path
) -> None:
    secret_path = _write(tmp_path / "secret.txt", SECRET)
    payload = _write(tmp_path / "xxe.xml", _external_entity_document(secret_path))

    with pytest.raises(XmlSafetyError) as excinfo:
        parse_xml_file(payload, settings)

    _assert_secret_absent(excinfo.value)


def test_internal_entity_is_refused_and_never_expanded(settings: Settings) -> None:
    """Even a harmless-looking internal entity is refused: the parser has one rule, not a guess."""
    document = (
        b'<?xml version="1.0"?><!DOCTYPE root [<!ENTITY greeting "hello">]><root>&greeting;</root>'
    )

    with pytest.raises(XmlSafetyError) as excinfo:
        parse_xml_bytes(document, settings)

    assert "entities or a DTD" in str(excinfo.value)
    assert "EntitiesForbidden" in str(excinfo.value)
    assert "hello" not in str(excinfo.value)


# --------------------------------------------------------------------------- billion laughs


def test_billion_laughs_is_refused_rather_than_expanded(settings: Settings) -> None:
    """Recursive entities would expand to gigabytes; the parser must stop at the declaration."""
    document = (
        b'<?xml version="1.0"?>'
        b"<!DOCTYPE lolz ["
        b'<!ENTITY lol "lol">'
        b'<!ENTITY lol1 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">'
        b'<!ENTITY lol2 "&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;">'
        b'<!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">'
        b"]>"
        b"<lolz>&lol3;</lolz>"
    )

    with pytest.raises(XmlSafetyError) as excinfo:
        parse_xml_bytes(document, settings)

    assert excinfo.value.code == "xml_rejected"
    assert "lollol" not in str(excinfo.value)


# --------------------------------------------------------------------------- upload guard


def test_guard_rejects_a_doctype_and_names_the_attacks_it_prevents() -> None:
    """The engineer has to know to re-export without a DTD, so the message must say why."""
    with pytest.raises(FileRejectedError) as excinfo:
        guard_xml_upload(b'<?xml version="1.0"?><!DOCTYPE Controller SYSTEM "rslogix.dtd"><r/>')

    message = str(excinfo.value)
    assert excinfo.value.code == "file_rejected"
    assert "DTD" in message
    assert "external-entity" in message
    assert "entity-expansion" in message
    assert "Re-export without one" in message


def test_guard_rejects_an_entity_declaration_and_says_it_is_disabled_for_safety() -> None:
    with pytest.raises(FileRejectedError) as excinfo:
        guard_xml_upload(b'<!ENTITY lol "lol">')

    message = str(excinfo.value)
    assert excinfo.value.code == "file_rejected"
    assert "entities" in message
    assert "disabled for safety" in message


def test_guard_accepts_an_ordinary_xml_head() -> None:
    guard_xml_upload(b'<?xml version="1.0" encoding="utf-8"?>\n<RSLogix5000Content>\n  <Cont')


def test_guard_accepts_a_head_with_leading_whitespace() -> None:
    guard_xml_upload(b'\r\n  <?xml version="1.0"?><Controller Use="Target"/>')


@pytest.mark.parametrize(
    "head",
    [
        b"<!doctype Controller>",
        b"<!DOCTYPE Controller>",
        b"<!DocTyPe Controller>",
    ],
)
def test_guard_matches_a_doctype_in_any_case(head: bytes) -> None:
    """XML declarations are case-sensitive but attackers are not; the guard must not be evaded."""
    with pytest.raises(FileRejectedError):
        guard_xml_upload(head)


@pytest.mark.parametrize(
    "head",
    [
        b'<!entity lol "lol">',
        b'<!ENTITY lol "lol">',
        b'<!EnTiTy lol "lol">',
    ],
)
def test_guard_matches_an_entity_declaration_in_any_case(head: bytes) -> None:
    with pytest.raises(FileRejectedError):
        guard_xml_upload(head)


# --------------------------------------------------------------------------- name helpers


@pytest.mark.parametrize(
    ("tag", "expected"),
    [
        ("{urn:esco:test}Rung", "Rung"),
        ("Rung", "Rung"),
        ("{}Rung", "Rung"),
        ("{http://www.rockwellautomation.com/x}RSLogix5000Content", "RSLogix5000Content"),
    ],
)
def test_strip_namespace_returns_the_local_name(tag: str, expected: str) -> None:
    assert strip_namespace(tag) == expected


# --------------------------------------------------------------------------- element paths


def test_element_path_adds_a_position_only_where_a_sibling_name_repeats(
    settings: Settings,
) -> None:
    """Positions everywhere would make paths churn between exports; only ambiguity needs one."""
    document = (
        b"<Controller><Programs><Program>"
        b'<Routine Name="Main"/><Routine Name="Fault"/><Tags/>'
        b"</Program></Programs></Controller>"
    )
    root = parse_xml_bytes(document, settings)
    program = root[0][0]

    routines = [child for child in program if child.tag == "Routine"]
    ancestors = [root, root[0], program]

    assert element_path([], root) == "/Controller"
    assert element_path(ancestors[:-1], program) == "/Controller/Programs/Program"
    assert element_path(ancestors, routines[0]) == "/Controller/Programs/Program/Routine[1]"
    assert element_path(ancestors, routines[1]) == "/Controller/Programs/Program/Routine[2]"
    assert element_path(ancestors, program[2]) == "/Controller/Programs/Program/Tags"


def test_element_path_uses_local_names_for_a_namespaced_tree(settings: Settings) -> None:
    """An evidence anchor must stay readable, so namespace URIs are stripped from the locator."""
    document = b'<c:Controller xmlns:c="urn:esco:test"><c:Rung/><c:Rung/></c:Controller>'
    root = parse_xml_bytes(document, settings)

    assert element_path([], root) == "/Controller"
    assert element_path([root], root[0]) == "/Controller/Rung[1]"
    assert element_path([root], root[1]) == "/Controller/Rung[2]"


def test_element_path_counts_only_same_named_siblings(settings: Settings) -> None:
    """A repeated name must be numbered by its own occurrences, not by raw child index."""
    document = b"<Program><Description/><Routine/><Comment/><Routine/></Program>"
    root = parse_xml_bytes(document, settings)

    assert element_path([root], root[1]) == "/Program/Routine[1]"
    assert element_path([root], root[3]) == "/Program/Routine[2]"
    assert element_path([root], root[2]) == "/Program/Comment"


def test_iter_with_paths_yields_a_unique_path_for_every_element(settings: Settings) -> None:
    """Anchors are stored by path, so two different elements sharing one would corrupt evidence."""
    document = (
        b"<Controller>"
        b"<Programs>"
        b"<Program><Routine><Rung/><Rung/></Routine><Routine><Rung/></Routine></Program>"
        b"<Program><Routine><Rung/></Routine></Program>"
        b"</Programs>"
        b"<Programs><Program/></Programs>"
        b"</Controller>"
    )
    root = parse_xml_bytes(document, settings)

    pairs = list(iter_with_paths(root))
    paths = [path for _, path in pairs]

    assert len(pairs) == len(list(root.iter()))
    assert len(set(paths)) == len(paths)
    assert "/Controller/Programs[1]/Program[2]/Routine/Rung" in paths
    assert "/Controller/Programs[2]/Program" in paths


def test_iter_with_paths_walks_in_document_order(settings: Settings) -> None:
    """Reviewers read findings in file order, which only holds if the walk is pre-order."""
    document = b"<a><b><c/><d/></b><e/></a>"
    root = parse_xml_bytes(document, settings)

    assert [path for _, path in iter_with_paths(root)] == [
        "/a",
        "/a/b",
        "/a/b/c",
        "/a/b/d",
        "/a/e",
    ]


def test_iter_with_paths_pairs_each_path_with_its_own_element(settings: Settings) -> None:
    document = b'<Program><Routine Name="Main"/><Routine Name="Fault"/></Program>'
    root = parse_xml_bytes(document, settings)

    named = {path: element.get("Name") for element, path in iter_with_paths(root)}

    assert named["/Program/Routine[1]"] == "Main"
    assert named["/Program/Routine[2]"] == "Fault"


def test_iter_with_paths_handles_a_single_element_document(settings: Settings) -> None:
    root = parse_xml_bytes(b"<Controller/>", settings)

    assert [path for _, path in iter_with_paths(root)] == ["/Controller"]
