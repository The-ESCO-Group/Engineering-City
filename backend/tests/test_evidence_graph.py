"""The cross-file evidence graph, over the whole synthetic project.

The parser tests prove each file is read correctly. This proves the *join* is
right, which is where the product's value actually is: a discrepancy only exists
between two documents.

Every assertion here traces to a deliberately planted case in
``fixtures/README.md``. The negative assertions matter as much as the positive
ones — a graph that manufactures leads is as harmful as one that misses them,
because an agent will faithfully turn a false lead into a finding an engineer has
to disprove.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.services import evidence_graph
from app.core.services.evidence_graph import EvidenceGraph, build_graph
from app.models.project import Project
from app.parsers.base import normalise_tag_name


def _graph(db: Session, project: Project) -> EvidenceGraph:
    return build_graph(db, project.id)


def _by_id(graph: EvidenceGraph, identifier: str) -> object:
    matches = [item for item in graph.interlocks if item.identifier == identifier]
    assert len(matches) == 1, f"expected exactly one {identifier}, got {len(matches)}"
    return matches[0]


# ---------------------------------------------------------------------------
# Shape
# ---------------------------------------------------------------------------
def test_graph_joins_every_document_kind(db: Session, ingested_project: Project) -> None:
    graph = _graph(db, ingested_project)
    counts = graph.counts()

    assert counts["rungs"] > 0, "no logic was joined"
    assert counts["interlocks"] > 0, "no interlock schedule was joined"
    assert counts["io_points"] > 0, "no I/O list was joined"
    assert counts["alarms"] > 0, "no alarms were joined"
    assert counts["documents"] > 0, "no narrative documents were joined"
    assert graph.missing_inputs == [], f"unexpectedly missing: {graph.missing_inputs}"
    assert graph.unreadable_files == []


def test_every_fact_carries_provenance(db: Session, ingested_project: Project) -> None:
    """A fact with no source cannot become a citable finding."""
    graph = _graph(db, ingested_project)

    for requirement in graph.interlocks:
        assert requirement.sources, f"{requirement.identifier} has no provenance"
        for source in requirement.sources:
            assert source.anchor_key
            assert source.display_location

    for fact in graph.logic:
        assert fact.anchor_key.startswith("rung:")
        assert fact.file_id is not None

    for point in graph.io_points:
        assert point["source"]["anchor_key"]


# ---------------------------------------------------------------------------
# ILK-001 / ILK-002 — correctly matched. A finding here would be a false positive.
# ---------------------------------------------------------------------------
def test_correctly_implemented_interlocks_match_exactly(
    db: Session, ingested_project: Project
) -> None:
    graph = _graph(db, ingested_project)

    estop = _by_id(graph, "ILK-001")
    assert estop.matched_tag == "ESTOP_OK"  # type: ignore[attr-defined]
    assert estop.candidate_tags == []  # type: ignore[attr-defined]
    assert estop.related_rung_keys, "the matched tag should point at the rungs using it"  # type: ignore[attr-defined]

    overload = _by_id(graph, "ILK-002")
    assert overload.matched_tag == "CV101_OL_TRIP"  # type: ignore[attr-defined]
    assert overload.candidate_tags == []  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# ILK-003 — documented and wired, but never implemented in logic.
# ---------------------------------------------------------------------------
def test_documented_interlock_absent_from_logic_is_visible(
    db: Session, ingested_project: Project
) -> None:
    graph = _graph(db, ingested_project)
    jam = _by_id(graph, "ILK-003")

    assert jam.trip_tag == "CV102_JAM"  # type: ignore[attr-defined]
    assert jam.is_specified is True, "the schedule specifies it fully"  # type: ignore[attr-defined]
    assert jam.matched_tag is None, "no declared tag of that name exists"  # type: ignore[attr-defined]

    # It is in the I/O list — the signal was wired — which is what makes this a
    # missing implementation rather than a documentation error.
    assert any(point["tag"] == "CV102_JAM" for point in graph.io_points)

    # And no rung anywhere references it.
    assert graph.rungs_referencing("CV102_JAM") == []

    buckets = evidence_graph.tag_usage_summary(graph)
    assert "CV102_JAM" in buckets["documented_but_not_declared"]
    assert "CV102_JAM" in buckets["in_io_list_but_not_in_logic"]


# ---------------------------------------------------------------------------
# ILK-004 — the same interlock under two spellings.
# ---------------------------------------------------------------------------
def test_naming_mismatch_surfaces_as_a_candidate_not_a_conclusion(
    db: Session, ingested_project: Project
) -> None:
    """The graph offers ``GRD_DR_CLSD`` for consideration and stops there.

    Deciding that two names denote the same signal is an engineering judgement,
    so the graph must not make it — but it must make the judgement *available*,
    or the agent has nothing to reason from.
    """
    graph = _graph(db, ingested_project)
    guard = _by_id(graph, "ILK-004")

    assert guard.trip_tag == "GUARD_DOOR_CLOSED"  # type: ignore[attr-defined]
    assert guard.matched_tag is None, "the graph must not silently equate the two names"  # type: ignore[attr-defined]

    candidates = {item["name"] for item in guard.candidate_tags}  # type: ignore[attr-defined]
    assert "GRD_DR_CLSD" in candidates, f"contraction not offered; got {candidates}"

    top = guard.candidate_tags[0]  # type: ignore[attr-defined]
    assert top["name"] == "GRD_DR_CLSD", "the real match should rank first"
    assert top["score"] >= evidence_graph.SIMILARITY_THRESHOLD
    assert top["reason"]

    # The candidate carries the rungs that use it, so the agent can cite logic.
    assert guard.related_rung_keys  # type: ignore[attr-defined]
    assert graph.rungs_referencing("GRD_DR_CLSD"), "the logic does implement it"


def test_unrelated_tags_are_not_offered_as_candidates(
    db: Session, ingested_project: Project
) -> None:
    """A permissive candidate list would bury the real match in noise."""
    graph = _graph(db, ingested_project)
    guard = _by_id(graph, "ILK-004")

    candidates = {item["name"] for item in guard.candidate_tags}  # type: ignore[attr-defined]
    for unrelated in ("ESTOP_OK", "AIR_PRESS_PSI", "HOPPER_LEVEL_PCT", "LINE_PERMISSIVE"):
        assert unrelated not in candidates
    assert len(guard.candidate_tags) <= 5  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# ILK-005 — the tag matches; the threshold does not.
# ---------------------------------------------------------------------------
def test_both_sides_of_the_threshold_disagreement_are_recorded_verbatim(
    db: Session, ingested_project: Project
) -> None:
    """The graph records 60.0 and 40.0 and does not decide they contradict."""
    graph = _graph(db, ingested_project)
    air = _by_id(graph, "ILK-005")

    assert air.matched_tag == "AIR_PRESS_PSI"  # type: ignore[attr-defined]
    assert "60" in air.trip_condition, air.trip_condition  # type: ignore[attr-defined]

    rungs = graph.rungs_referencing("AIR_PRESS_PSI")
    assert rungs, "the permissive rung uses the tag"

    operands = [
        operand
        for rung in rungs
        for instruction in rung.instructions
        if str(instruction.get("mnemonic", "")).upper() == "GRT"
        for operand in instruction.get("operands", [])
    ]
    assert "40.0" in operands, f"the implemented threshold should be recorded; got {operands}"


# ---------------------------------------------------------------------------
# ILK-007 — underspecified. Nothing may be concluded from it.
# ---------------------------------------------------------------------------
def test_an_underspecified_requirement_is_marked_not_comparable(
    db: Session, ingested_project: Project
) -> None:
    """ILK-007 has no tag and no threshold, so no claim about logic can rest on it."""
    graph = _graph(db, ingested_project)
    hopper = _by_id(graph, "ILK-007")

    assert hopper.trip_tag == ""  # type: ignore[attr-defined]
    assert hopper.trip_condition == ""  # type: ignore[attr-defined]
    assert hopper.is_specified is False  # type: ignore[attr-defined]
    assert "trip_tag" in hopper.missing_fields  # type: ignore[attr-defined]
    assert "trip_condition" in hopper.missing_fields  # type: ignore[attr-defined]
    assert hopper.matched_tag is None  # type: ignore[attr-defined]
    assert hopper.candidate_tags == [], "an unspecified requirement has nothing to match"  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# ILK-006 — withdrawn, on a hidden sheet.
# ---------------------------------------------------------------------------
def test_a_withdrawn_interlock_is_kept_but_not_treated_as_current(
    db: Session, ingested_project: Project
) -> None:
    """Dropping it would hide content from the engineer; counting it would invent
    a requirement that was deliberately withdrawn. It is kept and flagged."""
    graph = _graph(db, ingested_project)
    withdrawn = _by_id(graph, "ILK-006")

    assert withdrawn.from_hidden_sheet is True  # type: ignore[attr-defined]
    assert withdrawn.is_current is False  # type: ignore[attr-defined]
    assert withdrawn.sheet == "Superseded"  # type: ignore[attr-defined]

    counts = graph.counts()
    assert counts["interlocks_from_hidden_sheets"] == 1
    assert counts["interlocks"] == 6, "the live schedule has six rows"

    live = {item.identifier for item in graph.interlocks if item.is_current}
    assert live == {"ILK-001", "ILK-002", "ILK-003", "ILK-004", "ILK-005", "ILK-007"}


def test_the_hidden_sheet_is_reported_as_a_parser_limitation(
    db: Session, ingested_project: Project
) -> None:
    graph = _graph(db, ingested_project)
    hidden = [item for item in graph.limitations if item.code == "hidden_content"]
    assert hidden, "a hidden sheet must reach the graph as a limitation"
    assert "Superseded" in hidden[0].message


# ---------------------------------------------------------------------------
# Undeclared references, and the UDT members that must not look like them.
# ---------------------------------------------------------------------------
def test_exactly_one_genuine_undeclared_reference(db: Session, ingested_project: Project) -> None:
    """``ALM_HOPPER_HIGH`` is the only truly undeclared operand in the fixture.

    Members of a declared UDT (``CV101_Status.Running``) have no declaration of
    their own and never will. Reporting them here would hand the PLC Review Agent
    six false leads for one real one, and an engineer would have to disprove all
    seven.
    """
    graph = _graph(db, ingested_project)
    buckets = evidence_graph.tag_usage_summary(graph)

    assert buckets["referenced_but_not_declared"] == ["ALM_HOPPER_HIGH"]

    members = buckets["udt_members"]
    assert "CV101_Status.Running" in members
    assert "CV103_Status.Interlocked" in members
    for member in members:
        fact = graph.tags[normalise_tag_name(member)]
        assert fact.base_declared is True
        assert fact.base_tag


def test_a_declared_udt_tag_is_declared(db: Session, ingested_project: Project) -> None:
    fact = graph_tag(db, ingested_project, "CV101_Status")
    assert fact.declared_in_logic is True
    assert fact.data_type == "ConveyorStatus"


def graph_tag(db: Session, project: Project, name: str):  # type: ignore[no-untyped-def]
    graph = build_graph(db, project.id)
    fact = graph.tag(name)
    assert fact is not None, f"{name} is not in the graph"
    return fact


def test_numeric_literals_are_not_mistaken_for_tags(db: Session, ingested_project: Project) -> None:
    """``GRT(AIR_PRESS_PSI, 40.0)`` has one tag and one constant.

    Treating ``40.0`` as an undeclared tag would manufacture a finding from
    nothing at all.
    """
    graph = _graph(db, ingested_project)
    for literal in ("40.0", "90.0", "1", "2", "0"):
        assert graph.tag(literal) is None, f"{literal} was recorded as a tag"


# ---------------------------------------------------------------------------
# Cross-file: the HMI knows about an alarm the logic never implements.
# ---------------------------------------------------------------------------
def test_alarm_for_an_unimplemented_interlock_is_visible(
    db: Session, ingested_project: Project
) -> None:
    graph = _graph(db, ingested_project)

    names = {alarm["name"] for alarm in graph.alarms}
    assert "ALM_CV102_JAM" in names, "the HMI declares a jam alarm"
    assert "ALM_HOPPER_HIGH" in names

    for alarm in graph.alarms:
        assert alarm["source"]["anchor_key"], f"{alarm['name']} has no citable source"


def test_narrative_documents_are_indexed_for_citation(
    db: Session, ingested_project: Project
) -> None:
    """A requirement stated in prose has to be citable, or it cannot be compared."""
    graph = _graph(db, ingested_project)
    statements = [statement for document in graph.documents for statement in document.statements]
    assert statements

    air_pressure = [item for item in statements if "60 psi" in item["text"]]
    assert air_pressure, "the documented threshold must be locatable"
    assert air_pressure[0]["anchor_key"]

    jam = [item for item in statements if "jam" in item["text"].lower()]
    assert jam, "the documented jam interlock must be locatable"


# ---------------------------------------------------------------------------
# Unreadable input must remain visible.
# ---------------------------------------------------------------------------
def test_the_unreadable_scan_reaches_the_graph_as_a_limitation(
    db: Session, ingested_project: Project
) -> None:
    """Otherwise a review could report the project clean without saying it could
    not read one of the drawings."""
    graph = _graph(db, ingested_project)

    scan = [
        item
        for item in graph.limitations
        if item.filename == "Scanned_Drawing.pdf" and item.code == "no_text_layer"
    ]
    assert scan, [item.code for item in graph.limitations]
    assert "ocr" in scan[0].message.lower() or "manual" in scan[0].message.lower()


# ---------------------------------------------------------------------------
# Determinism and serialisation
# ---------------------------------------------------------------------------
def test_building_the_graph_twice_gives_the_same_result(
    db: Session, ingested_project: Project
) -> None:
    """Agents are prompted from this. Non-determinism here is non-reproducible review."""
    first = build_graph(db, ingested_project.id).as_dict()
    second = build_graph(db, ingested_project.id).as_dict()
    assert first == second


def test_graph_serialises_to_json(db: Session, ingested_project: Project) -> None:
    import json

    payload = build_graph(db, ingested_project.id).as_dict()
    encoded = json.dumps(payload)
    assert json.loads(encoded) == payload


def test_an_empty_project_produces_an_honest_empty_graph(db: Session, project: Project) -> None:
    """A project with nothing uploaded must say which inputs are missing.

    "No findings" and "nothing to look at" are different answers and the graph
    has to distinguish them.
    """
    graph = build_graph(db, project.id)

    assert graph.counts()["rungs"] == 0
    assert graph.interlocks == []
    assert graph.has_logic is False
    assert graph.has_interlock_schedule is False
    assert "Interlock schedule" in graph.missing_inputs
    assert any("L5X" in item for item in graph.missing_inputs)


# ---------------------------------------------------------------------------
# Citation integrity: every anchor_key the graph emits must resolve.
# ---------------------------------------------------------------------------
def test_every_anchor_key_the_graph_emits_resolves_to_a_real_anchor(
    db: Session, ingested_project: Project
) -> None:
    """A fabricated anchor key fails silently, which is the dangerous part.

    The graph composed ``range:Interlocks!row5`` for interlock rows while the
    XLSX parser emitted ``range:Interlocks!A5:H5``. Nothing errored — the
    Interlock Review Agent simply had no citation for any requirement, so it
    produced no discrepancies at all and the run reported success.

    This checks every provenance key the graph hands downstream, for every file
    type, so the same class of mismatch cannot reappear in a parser nobody
    thought to re-test.
    """
    from app.models.evidence import EvidenceAnchor

    graph = build_graph(db, ingested_project.id)
    persisted = {
        (anchor.file_id, anchor.anchor_key)
        for anchor in db.execute(
            select(EvidenceAnchor).where(EvidenceAnchor.project_id == ingested_project.id)
        ).scalars()
    }

    unresolved: list[str] = []

    def check(file_id: object, key: str, where: str) -> None:
        import uuid as _uuid

        resolved = file_id if isinstance(file_id, _uuid.UUID) else _uuid.UUID(str(file_id))
        if (resolved, key) not in persisted:
            unresolved.append(f"{where}: {key}")

    for requirement in graph.interlocks:
        for source in requirement.sources:
            check(source.file_id, source.anchor_key, f"interlock {requirement.identifier}")

    for fact in graph.logic:
        if fact.file_id is not None:
            check(fact.file_id, fact.anchor_key, f"rung {fact.program}/{fact.routine}")

    for tag in graph.tags.values():
        for source in tag.sources:
            check(source.file_id, source.anchor_key, f"tag {tag.name}")

    for point in graph.io_points:
        source = point["source"]
        check(source["file_id"], source["anchor_key"], f"io point {point['tag']}")

    for alarm in graph.alarms:
        source = alarm["source"]
        check(source["file_id"], source["anchor_key"], f"alarm {alarm['name']}")

    for document in graph.documents:
        for statement in document.statements[:50]:
            check(document.file_id, statement["anchor_key"], f"statement in {document.filename}")

    assert unresolved == [], (
        f"{len(unresolved)} citation(s) point at anchors that do not exist:\n  "
        + "\n  ".join(unresolved[:15])
    )


def test_every_current_interlock_can_cite_its_schedule_row(
    db: Session, ingested_project: Project
) -> None:
    """Without a resolvable requirement anchor, no discrepancy can be reported."""
    graph = build_graph(db, ingested_project.id)
    for requirement in graph.interlocks:
        if not requirement.is_current:
            continue
        assert requirement.sources, f"{requirement.identifier} has no source"
        assert requirement.sources[0].anchor_key.startswith("range:"), (
            f"{requirement.identifier} cites {requirement.sources[0].anchor_key}"
        )
