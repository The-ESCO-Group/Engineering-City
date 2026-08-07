"""The cross-file evidence graph, over the whole synthetic project.

The parser tests prove each file is read correctly. This proves the *join* is
right, which is where the product's value actually is: a discrepancy only exists
between two documents.

Every assertion here traces to a deliberately planted case in
``fixtures/README.md``, or — for the Line 4 matrix further down — in
``fixtures/line4_packaging_conveyor/README.md``. The negative assertions matter as
much as the positive ones: a graph that manufactures leads is as harmful as one
that misses them, because an agent will faithfully turn a false lead into a
finding an engineer has to disprove.
"""

from __future__ import annotations

import io
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.enums import DocumentRole, FileKind, FileStatus, ProjectRole
from app.core.services import evidence_graph, parsing
from app.core.services import files as file_service
from app.core.services import projects as project_service
from app.core.services.evidence_graph import (
    EvidenceGraph,
    InterlockRequirement,
    RequirementScope,
    build_graph,
)
from app.models.file import ParsedArtifact, UploadedFile
from app.models.project import Project
from app.models.user import User
from app.parsers.base import normalise_tag_name


def _graph(db: Session, project: Project) -> EvidenceGraph:
    return build_graph(db, project.id)


def _by_id(graph: EvidenceGraph, identifier: str) -> InterlockRequirement:
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
    assert estop.matched_tag == "ESTOP_OK"
    assert estop.candidate_tags == []
    assert estop.related_rung_keys, "the matched tag should point at the rungs using it"

    overload = _by_id(graph, "ILK-002")
    assert overload.matched_tag == "CV101_OL_TRIP"
    assert overload.candidate_tags == []


# ---------------------------------------------------------------------------
# ILK-003 — documented and wired, but never implemented in logic.
# ---------------------------------------------------------------------------
def test_documented_interlock_absent_from_logic_is_visible(
    db: Session, ingested_project: Project
) -> None:
    graph = _graph(db, ingested_project)
    jam = _by_id(graph, "ILK-003")

    assert jam.trip_tag == "CV102_JAM"
    assert jam.is_specified is True, "the schedule specifies it fully"
    assert jam.matched_tag is None, "no declared tag of that name exists"

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

    assert guard.trip_tag == "GUARD_DOOR_CLOSED"
    assert guard.matched_tag is None, "the graph must not silently equate the two names"

    candidates = {item["name"] for item in guard.candidate_tags}
    assert "GRD_DR_CLSD" in candidates, f"contraction not offered; got {candidates}"

    top = guard.candidate_tags[0]
    assert top["name"] == "GRD_DR_CLSD", "the real match should rank first"
    assert top["score"] >= evidence_graph.SIMILARITY_THRESHOLD
    assert top["reason"]

    # The candidate carries the rungs that use it, so the agent can cite logic.
    assert guard.related_rung_keys
    assert graph.rungs_referencing("GRD_DR_CLSD"), "the logic does implement it"


def test_unrelated_tags_are_not_offered_as_candidates(
    db: Session, ingested_project: Project
) -> None:
    """A permissive candidate list would bury the real match in noise."""
    graph = _graph(db, ingested_project)
    guard = _by_id(graph, "ILK-004")

    candidates = {item["name"] for item in guard.candidate_tags}
    for unrelated in ("ESTOP_OK", "AIR_PRESS_PSI", "HOPPER_LEVEL_PCT", "LINE_PERMISSIVE"):
        assert unrelated not in candidates
    assert len(guard.candidate_tags) <= 5


# ---------------------------------------------------------------------------
# ILK-005 — the tag matches; the threshold does not.
# ---------------------------------------------------------------------------
def test_both_sides_of_the_threshold_disagreement_are_recorded_verbatim(
    db: Session, ingested_project: Project
) -> None:
    """The graph records 60.0 and 40.0 and does not decide they contradict."""
    graph = _graph(db, ingested_project)
    air = _by_id(graph, "ILK-005")

    assert air.matched_tag == "AIR_PRESS_PSI"
    assert "60" in air.trip_condition, air.trip_condition

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

    assert hopper.trip_tag == ""
    assert hopper.trip_condition == ""
    assert hopper.is_specified is False
    assert "trip_tag" in hopper.missing_fields
    assert "trip_condition" in hopper.missing_fields
    assert hopper.matched_tag is None
    assert hopper.candidate_tags == [], "an unspecified requirement has nothing to match"


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

    assert withdrawn.from_hidden_sheet is True
    assert withdrawn.is_current is False
    assert withdrawn.sheet == "Superseded"

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


# ---------------------------------------------------------------------------
# Determinism: a ZIP upload writes every member in one transaction, so a whole
# project's files routinely share a created_at to the microsecond. The graph
# builder is order-sensitive — the first spelling of a tag name wins, and so do
# the first non-empty data type, scope and description — so a tie that fell
# through to whatever row order the database returned made a review
# irreproducible over byte-identical uploads.
# ---------------------------------------------------------------------------
#: Two L5X exports declaring the same signal under two spellings, two data
#: types, two scopes and two descriptions. Whichever the builder reaches first
#: is the one the review agents are told about.
_TIED_DECLARATIONS = {
    "A": ("LineA.L5X", "CV101_Status", "DINT", "controller", "from A"),
    "B": ("LineB.L5X", "cv101_status", "INT", "program", "from B"),
}

#: One timestamp for both files, exactly as ``files.py`` writes ZIP members.
_TIED_AT = datetime(2026, 2, 3, 9, 15, 0, tzinfo=UTC)


def _add_l5x_row(db: Session, project: Project, key: str) -> None:
    filename, tag_name, data_type, scope, description = _TIED_DECLARATIONS[key]
    record = UploadedFile(
        project_id=project.id,
        original_filename=filename,
        stored_filename=filename,
        relative_path=f"raw/{filename}",
        file_kind=FileKind.L5X,
        status=FileStatus.PARSED,
        created_at=_TIED_AT,
    )
    db.add(record)
    db.flush()
    db.add(
        ParsedArtifact(
            project_id=project.id,
            file_id=record.id,
            artifact_kind=FileKind.L5X.value,
            parser_name="l5x_parser",
            parser_version="1.0.0",
            created_at=_TIED_AT,
            content={
                "tags": [
                    {
                        "name": tag_name,
                        "data_type": data_type,
                        "scope": scope,
                        "description": description,
                        "anchor_key": f"tag:{scope}/{tag_name}",
                    }
                ],
                "rungs": [
                    {
                        "program": "MainProgram",
                        "routine": "R_Main",
                        "number": 0,
                        "comment": "",
                        "text": "",
                        "referenced_tags": [tag_name],
                        "instructions": [],
                        "anchor_key": f"rung:{key}/0",
                    }
                ],
            },
        )
    )
    db.flush()


def _without_identifiers(value: Any) -> Any:
    """The graph with the two ids that legitimately differ between projects removed."""
    if isinstance(value, dict):
        return {
            key: _without_identifiers(item)
            for key, item in value.items()
            if key not in {"file_id", "project_id"}
        }
    if isinstance(value, list):
        return [_without_identifiers(item) for item in value]
    return value


def test_two_files_sharing_a_timestamp_build_the_same_graph_either_way_round(
    db: Session,
    project: Project,
    seeded: dict[str, User],
    settings: Settings,
) -> None:
    """The same two uploads, written in the opposite order, are the same evidence.

    The two projects hold byte-identical parsed content and differ only in the
    order the rows were inserted, which is the order an ``ORDER BY created_at``
    with no tiebreaker hands back. If that leaks into the graph then the tag data
    type, the tag spelling and the rung order the review agents are given depend
    on it, and no review over this project is reproducible.
    """
    mirror = project_service.create_project(
        db,
        name="Synthetic Conveyor Line 3 (mirror)",
        description="Same two uploads, inserted the other way round.",
        client_reference="TEST-002",
        attributes={},
        creator=seeded["engineer"],
        settings=settings,
    )
    project_service.add_member(
        db,
        mirror,
        user_id=seeded["reviewer"].id,
        role=ProjectRole.REVIEWER,
        actor=seeded["engineer"],
    )

    for key in ("A", "B"):
        _add_l5x_row(db, project, key)
    for key in ("B", "A"):
        _add_l5x_row(db, mirror, key)
    db.flush()

    first = _without_identifiers(build_graph(db, project.id).as_dict())
    second = _without_identifiers(build_graph(db, mirror.id).as_dict())

    assert first == second


def test_a_tied_timestamp_resolves_by_filename_not_by_insertion_order(
    db: Session, project: Project
) -> None:
    """And the winner is stated, not incidental.

    ``LineA.L5X`` sorts before ``LineB.L5X``, so its declaration is the one the
    graph keeps — whichever row the database happened to return first.
    """
    for key in ("B", "A"):
        _add_l5x_row(db, project, key)
    db.flush()

    graph = build_graph(db, project.id)
    fact = graph.tag("CV101_Status")

    assert fact is not None
    assert fact.name == "CV101_Status"
    assert fact.data_type == "DINT"
    assert fact.scope == "controller"
    assert fact.description == "from A"
    assert fact.rung_keys == ["rung:A/0", "rung:B/0"]
    assert [item.anchor_key for item in graph.logic] == ["rung:A/0", "rung:B/0"]


def test_a_schedule_with_no_scope_column_leaves_scope_unstated(
    db: Session, ingested_project: Project
) -> None:
    """The demo schedule never says, and the graph must not answer for it.

    "Nobody stated the scope" is a different fact from "stated as existing", and
    only the first one licenses an agent to say so. Unstated still compares
    against the logic: a schedule with no scope column is documenting the plant
    it describes, and reading silence as "not built yet" would quietly stop the
    comparison this product exists to make.
    """
    graph = _graph(db, ingested_project)
    estop = _by_id(graph, "ILK-001")

    assert estop.scope_text == ""
    assert estop.scope is RequirementScope.UNSTATED
    assert estop.is_current is True
    assert graph.counts()["interlocks_scope_unstated"] == len(graph.interlocks)
    assert graph.counts()["interlocks_new_in_upgrade"] == 0


# ---------------------------------------------------------------------------
# The Line 4 interlock matrix: two worksheets, only one of them a schedule.
#
# Ground truth is ``fixtures/line4_packaging_conveyor/README.md``, which names
# the seven planted issues and the eleven things a correct review must not
# report. Six of those eleven were reported because of this join, and the
# highest-severity planted issue was misreported by it, so each is asserted here
# as well — where a failure names the cause, rather than only through the
# acceptance run, where it names a symptom.
# ---------------------------------------------------------------------------
LINE4_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "line4_packaging_conveyor"

#: The base controller and the matrix. Enough for every claim below: the matrix
#: is the document under test and the export is what its rows are matched
#: against.
LINE4_FIXTURES: tuple[tuple[str, DocumentRole], ...] = (
    ("Conveyor_Line4_Base.L5X", DocumentRole.CONTROL_LOGIC),
    ("Interlock_Matrix.xlsx", DocumentRole.INTERLOCK_SCHEDULE),
)


@pytest.fixture
def line4_project(
    db: Session,
    project: Project,
    seeded: dict[str, User],
    settings: Settings,
) -> Project:
    """The Line 4 export and interlock matrix, ingested and parsed as an upload."""
    if not LINE4_DIR.is_dir():  # pragma: no cover - developer aid
        pytest.fail(f"{LINE4_DIR} is missing. Run: python scripts/backend/make_line4_fixture.py")

    for filename, role in LINE4_FIXTURES:
        data = (LINE4_DIR / filename).read_bytes()
        outcome = file_service.ingest_upload(
            db,
            project=project,
            filename=filename,
            stream=io.BytesIO(data),
            size_bytes=len(data),
            declared_role=role,
            uploader=seeded["engineer"],
            settings=settings,
        )
        assert outcome.accepted, f"{filename} was rejected: {outcome.rejection_reason}"
    db.commit()

    for record in parsing.parseable_files(db, project.id):
        outcome = parsing.parse_file(db, record, settings)
        assert outcome.succeeded, f"{record.original_filename}: {outcome.error_message}"
    db.commit()
    return project


def test_the_cause_and_effect_worksheet_is_not_a_second_set_of_interlocks(
    db: Session, line4_project: Project
) -> None:
    """Seven rows in the matrix are seven requirements, not fourteen.

    The workbook's second sheet is a cause-and-effect grid. Read as a schedule it
    yields one requirement per row with the trip tag, the trip condition, the
    action and the fail-safe column all absent — which reads as seven incompletely
    specified interlocks, including against the one device this fixture implements
    correctly from end to end.
    """
    graph = build_graph(db, line4_project.id)

    identifiers = [item.identifier for item in graph.interlocks]
    assert identifiers == [f"ILK-40{number}" for number in range(1, 8)], identifiers
    assert {item.sheet for item in graph.interlocks} == {"Interlock Matrix"}

    underspecified = [item.identifier for item in graph.interlocks if not item.is_specified]
    assert underspecified == [], (
        f"the matrix fills every column of every row; these were read as gaps: {underspecified}"
    )


def test_the_cause_and_effect_worksheet_is_kept_as_cause_and_effect(
    db: Session, line4_project: Project
) -> None:
    """Dropping the grid would lose what it is for: which cause drives which effect.

    The schedule row states the trip and the grid states the actions the trip is
    meant to produce. Neither document holds both, so a review that has only one
    of them cannot check either against the logic.
    """
    graph = build_graph(db, line4_project.id)

    assert graph.cause_effects, "the grid reached the graph as nothing at all"
    assert {item.sheet for item in graph.cause_effects} == {"Cause and Effect"}

    diverter = graph.cause_effects_for("ILK-403")
    effects = {item.effect: item.detail for item in diverter}
    assert "Stop CV403" in effects, sorted(effects)
    assert "Inhibit CYL401" in effects, sorted(effects)
    assert "Stop CV401" not in effects, "a blank cell says the cause does not drive that effect"
    assert effects["Stop CV403"] == "", "a crossing mark carries no value of its own"
    assert effects["Alarm Tag"] == "ALM_CYL401_FAIL"
    assert {item.cause for item in diverter} == {"Diverter fails to extend within 1.5 s"}


def test_a_grid_cell_that_is_prose_is_not_resolved_into_a_tag(
    db: Session, line4_project: Project
) -> None:
    """``no alarm required`` is kept as what the engineer wrote and nothing more.

    The alarm column of a grid carries tag names and sentences about scope in the
    same column. Recording either as a signal would put a tag in the graph that no
    plant has, and every agent downstream would then be entitled to report it as
    an alarm nobody implemented.
    """
    graph = build_graph(db, line4_project.id)

    accumulation = {item.effect: item.detail for item in graph.cause_effects_for("ILK-404")}
    assert accumulation["Alarm Tag"] == "no alarm required"

    for prose in ("no alarm required", "new alarm to be added"):
        assert graph.tag(prose) is None, f"the grid cell '{prose}' was recorded as a tag"

    # And a cell that does name a real alarm is still only a cell. The tag is in
    # the graph because the export declares it, and the grid adds no evidence
    # about it: reading one column as tag references means reading them all.
    alarm = graph.tag("ALM_CYL401_FAIL")
    assert alarm is not None and alarm.declared_in_logic
    assert not any("Cause and Effect" in source.display_location for source in alarm.sources), (
        f"the grid was joined onto the tag: {[item.display_location for item in alarm.sources]}"
    )


def test_every_crossing_cites_the_row_it_came_from(db: Session, line4_project: Project) -> None:
    """A crossing nobody can cite cannot support a claim about the plant."""
    from app.models.evidence import EvidenceAnchor

    graph = build_graph(db, line4_project.id)
    persisted = {
        (anchor.file_id, anchor.anchor_key)
        for anchor in db.execute(
            select(EvidenceAnchor).where(EvidenceAnchor.project_id == line4_project.id)
        ).scalars()
    }

    unresolved = [
        f"{item.identifier} / {item.effect}: {source.anchor_key}"
        for item in graph.cause_effects
        for source in item.sources
        if (source.file_id, source.anchor_key) not in persisted
    ]
    assert unresolved == [], unresolved


def test_the_scope_column_decides_what_the_export_is_expected_to_hold(
    db: Session, line4_project: Project
) -> None:
    """Two of the seven rows belong to the upgrade, and the base export predates it.

    ILK-405 and ILK-406 are scoped ``New (this upgrade)``. Comparing them against
    an export of the controller as it runs today reports both as missing
    protections, which is a finding about work nobody has done yet — and it buries
    ILK-404, which is documented as existing and really is absent.
    """
    graph = build_graph(db, line4_project.id)

    accumulation = _by_id(graph, "ILK-404")
    assert accumulation.scope_text == "Existing"
    assert accumulation.scope is RequirementScope.EXISTING
    assert accumulation.is_current is True

    for identifier in ("ILK-405", "ILK-406"):
        planned = _by_id(graph, identifier)
        assert planned.scope_text == "New (this upgrade)"
        assert planned.scope is RequirementScope.NEW
        assert planned.is_current is False, "the base export is not expected to implement it"
        assert planned.matched_tag is None and planned.candidate_tags == [], (
            "an upgrade-scoped row was compared against the installed logic anyway"
        )

    counts = graph.counts()
    assert counts["interlocks"] == 5, "five rows describe the system as it exists"
    assert counts["interlocks_new_in_upgrade"] == 2
    assert counts["interlocks_scope_unstated"] == 0


def test_an_upgrade_scoped_trip_tag_does_not_look_documented_for_this_export(
    db: Session, line4_project: Project
) -> None:
    """``AIR_PRESS_PSI`` is a new transmitter for a new interlock.

    The 5069-IF8 in slot 3 is spare in the base export, which agrees. A tag named
    only by a row the upgrade adds must not make the installed controller look as
    though it is missing a documented signal.
    """
    graph = build_graph(db, line4_project.id)
    buckets = evidence_graph.tag_usage_summary(graph)

    assert "AIR_PRESS_PSI" not in buckets.get("documented_but_not_declared", [])


def test_two_photo_eyes_are_not_offered_as_one_signal_under_two_names(
    db: Session, line4_project: Project
) -> None:
    """ILK-404's trip tag is absent from the export, and no near-match hides that.

    ``PE402_BLOCKED`` is the discharge photo-eye and ``PE401_BLOCKED`` is the
    entry photo-eye of the same accumulation zone. Both are real, both are wired,
    and the strings are one character apart. Offering one for the other turns the
    highest-severity issue in this fixture — a documented, wired, never
    implemented accumulation interlock — into a naming mismatch that cites the
    wrong device.
    """
    graph = build_graph(db, line4_project.id)
    accumulation = _by_id(graph, "ILK-404")

    entry_eye = graph.tag("PE401_BLOCKED")
    assert entry_eye is not None and entry_eye.declared_in_logic, (
        "the near-match this test is about has to exist for the test to mean anything"
    )
    discharge_eye = graph.tag("PE402_BLOCKED")
    assert discharge_eye is not None and not discharge_eye.declared_in_logic, (
        "the README states that PE402 occurs nowhere in the export"
    )

    assert accumulation.trip_tag == "PE402_BLOCKED"
    assert accumulation.is_specified is True
    assert accumulation.matched_tag is None

    offered = [str(item["name"]) for item in accumulation.candidate_tags]
    assert "PE401_BLOCKED" not in offered, (
        f"a different device was offered as the signal: {offered}"
    )
    # Whatever else survives the threshold is raw string proximity, which is the
    # weakest thing the graph can say and is not a claim that two names mean the
    # same signal. Nothing here is offered on a rule that would make that claim.
    assert all(item["reason"] == "sequence similarity" for item in accumulation.candidate_tags), (
        accumulation.candidate_tags
    )


def test_a_row_naming_three_contacts_is_matched_against_all_three(
    db: Session, line4_project: Project
) -> None:
    """ILK-407's trip tag column names the three drive fault contacts.

    Matched as one name it matches nothing, and the graph then offers whatever is
    textually nearest — which is how an interlock that is implemented three times
    over becomes a naming mismatch against a tag from another device.
    """
    graph = build_graph(db, line4_project.id)
    drives = _by_id(graph, "ILK-407")

    assert drives.trip_tags == ["MTR401_FAULT", "MTR402_FAULT", "MTR403_FAULT"]
    assert drives.matched_tags == ["MTR401_FAULT", "MTR402_FAULT", "MTR403_FAULT"]
    assert drives.matched_tag == "MTR401_FAULT"
    assert drives.candidate_tags == []
    assert drives.related_rung_keys, "the permissive rung reads all three contacts"
    assert graph.tag("MTR401_FAULT, MTR402_FAULT, MTR403_FAULT") is None, (
        "the whole cell was recorded as though it were one signal"
    )


# ---------------------------------------------------------------------------
# Candidate scoring, on names built for the purpose.
# ---------------------------------------------------------------------------
def _add_schedule_row(
    db: Session,
    project: Project,
    *,
    sheet: str,
    headers: list[str],
    rows: list[list[str]],
) -> None:
    """One parsed worksheet, so a table's shape can be stated exactly."""
    record = UploadedFile(
        project_id=project.id,
        original_filename=f"{sheet}.xlsx",
        stored_filename=f"{sheet}.xlsx",
        relative_path=f"raw/{sheet}.xlsx",
        file_kind=FileKind.XLSX,
        document_role=DocumentRole.INTERLOCK_SCHEDULE,
        status=FileStatus.PARSED,
        created_at=_TIED_AT,
    )
    db.add(record)
    db.flush()
    db.add(
        ParsedArtifact(
            project_id=project.id,
            file_id=record.id,
            artifact_kind=FileKind.XLSX.value,
            parser_name="xlsx_parser",
            parser_version="1.0.0",
            created_at=_TIED_AT,
            content={
                "workbook": {"sheet_names": [sheet], "hidden_sheet_names": []},
                "candidate_tables": [
                    {
                        "sheet": sheet,
                        "headers": headers,
                        "records": [
                            {
                                "row": index + 2,
                                "ref": f"A{index + 2}:A{index + 2}",
                                "values": dict(zip(headers, values, strict=True)),
                            }
                            for index, values in enumerate(rows)
                        ],
                    }
                ],
            },
        )
    )
    db.flush()


def _add_declarations(db: Session, project: Project, names: list[str]) -> None:
    """An export declaring each name and referencing it on one rung."""
    record = UploadedFile(
        project_id=project.id,
        original_filename="Declared.L5X",
        stored_filename="Declared.L5X",
        relative_path="raw/Declared.L5X",
        file_kind=FileKind.L5X,
        status=FileStatus.PARSED,
        created_at=_TIED_AT,
    )
    db.add(record)
    db.flush()
    db.add(
        ParsedArtifact(
            project_id=project.id,
            file_id=record.id,
            artifact_kind=FileKind.L5X.value,
            parser_name="l5x_parser",
            parser_version="1.0.0",
            created_at=_TIED_AT,
            content={
                "tags": [
                    {
                        "name": name,
                        "data_type": "BOOL",
                        "scope": "controller",
                        "anchor_key": f"tag:controller/{name}",
                    }
                    for name in names
                ],
                "rungs": [
                    {
                        "program": "MainProgram",
                        "routine": "R_Main",
                        "number": 0,
                        "referenced_tags": names,
                        "instructions": [],
                        "anchor_key": "rung:MainProgram/R_Main/0",
                    }
                ],
            },
        )
    )
    db.flush()


def test_a_digit_inside_a_device_number_is_not_a_spelling_difference(
    db: Session, project: Project
) -> None:
    """The two halves of the candidate rule, side by side.

    A contraction is the case the rule exists to catch: ``GRD_DR_CLSD`` is
    ``GUARD_DOOR_CLOSED`` with letters dropped, and an agent should weigh it. Two
    device numbers are the case it must not catch: whatever the strings score,
    PE401 and PE402 are two devices, and inviting an agent to treat them as one
    downgrades an absent protection to a spelling difference.
    """
    _add_declarations(db, project, ["PE401_BLOCKED", "GRD_DR_CLSD"])
    _add_schedule_row(
        db,
        project,
        sheet="Interlocks",
        headers=["Interlock ID", "Trip Tag", "Trip Condition"],
        rows=[
            ["ILK-100", "PE402_BLOCKED", "PE402_BLOCKED = 1 for 5 s"],
            ["ILK-101", "GUARD_DOOR_CLOSED", "GUARD_DOOR_CLOSED = 0"],
        ],
    )

    graph = build_graph(db, project.id)

    photo_eye = _by_id(graph, "ILK-100")
    assert photo_eye.matched_tag is None
    assert photo_eye.candidate_tags == [], photo_eye.candidate_tags

    guard = _by_id(graph, "ILK-101")
    assert guard.matched_tag is None
    assert [item["name"] for item in guard.candidate_tags] == ["GRD_DR_CLSD"]
    assert guard.candidate_tags[0]["score"] >= evidence_graph.SIMILARITY_THRESHOLD


def test_a_grid_is_recognised_by_its_columns_not_by_its_worksheet_name(
    db: Session, project: Project
) -> None:
    """Both halves of the discriminator, on a sheet named neither way round.

    A customer names the grid whatever they like — "Trip Matrix", "C&E", nothing
    at all — so the sheet name cannot be what decides. A table with a trip tag
    column is a schedule however it is titled, and a table without one whose
    remaining columns are crossings is a grid.
    """
    _add_schedule_row(
        db,
        project,
        sheet="Trip Matrix",
        headers=["Interlock ID", "Cause", "Stop CV901", "Alarm Tag"],
        rows=[["ILK-900", "Guard door open", "X", "ALM_GD901"]],
    )
    _add_schedule_row(
        db,
        project,
        sheet="Cause and Effect",
        headers=["Interlock ID", "Trip Tag", "Trip Condition", "Effect"],
        rows=[["ILK-901", "GD901_CLOSED", "GD901_CLOSED = 0", "Stop CV901"]],
    )

    graph = build_graph(db, project.id)

    assert [item.identifier for item in graph.interlocks] == ["ILK-901"]
    assert _by_id(graph, "ILK-901").sheet == "Cause and Effect"
    assert [(item.identifier, item.effect) for item in graph.cause_effects] == [
        ("ILK-900", "Stop CV901"),
        ("ILK-900", "Alarm Tag"),
    ]


def test_a_grid_whose_cause_column_is_headed_description_is_still_a_grid(
    db: Session, project: Project
) -> None:
    """The second signal, on its own: columns holding nothing but crossings.

    No alias list recognises every word an engineer puts above the causes, so a
    grid has to be recognisable from its shape as well. Two marked columns, not
    one: a single column of ticks is as likely to be a yes/no attribute of a
    requirement as an axis of a grid.

    The cause is then read from the description column. Detection cannot work that
    way round — a schedule with a description column and no crossings must stay a
    schedule — but once the shape says grid, that column is where the causes are.
    """
    _add_schedule_row(
        db,
        project,
        sheet="Shutdown Key",
        headers=["Interlock ID", "Description", "Stop CV901", "Stop CV902"],
        rows=[
            ["ILK-910", "Guard door open", "X", "X"],
            ["ILK-911", "Low air pressure", "", "X"],
        ],
    )

    graph = build_graph(db, project.id)

    assert graph.interlocks == [], [item.identifier for item in graph.interlocks]
    assert [(item.identifier, item.effect) for item in graph.cause_effects] == [
        ("ILK-910", "Stop CV901"),
        ("ILK-910", "Stop CV902"),
        ("ILK-911", "Stop CV902"),
    ]
    assert {item.cause for item in graph.cause_effects} == {
        "Guard door open",
        "Low air pressure",
    }


def test_one_column_of_ticks_leaves_a_schedule_a_schedule(db: Session, project: Project) -> None:
    """A safety-related flag column does not make a schedule into a grid."""
    _add_schedule_row(
        db,
        project,
        sheet="Interlocks",
        headers=["Interlock ID", "Description", "Safety Related"],
        rows=[["ILK-920", "Guard door open", "X"]],
    )

    graph = build_graph(db, project.id)

    assert [item.identifier for item in graph.interlocks] == ["ILK-920"]
    assert graph.cause_effects == []
