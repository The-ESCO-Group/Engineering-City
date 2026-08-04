"""What the columns are allowed to say when nobody said anything.

``test_project_core_schema`` asserts the *shape* of the model. This module
asserts its *meaning*, and it exists because the two ways a schema lies are both
silent:

* a default that fills an unstated engineering decision with a plausible one, so
  that a validator reads a real value, finds nothing to report, and the
  fabrication ships as though somebody had made it;
* a delete cascade that removes the evidence a conclusion rested on, or the
  record that a named person accepted responsibility for it, in response to a
  routine event like a re-parse.

Both are worse than a crash. A crash gets fixed; these produce a document that
reads correctly and is wrong. Every test below is written against the concrete
event that triggers it rather than against the DDL alone, so it fails for the
behaviour and not merely for the declaration.
"""

from __future__ import annotations

from enum import Enum

import pytest
from sqlalchemy import ForeignKeyConstraint, delete, inspect
from sqlalchemy.orm import Session

from app.core.enums import (
    AlarmSeverity,
    AnchorType,
    ApprovalDecision,
    ApprovalKind,
    ApprovalStatus,
    EvidenceKind,
    FaultClass,
    InterlockKind,
    ObjectApprovalState,
    ObjectType,
)
from app.database.types import StrEnumType
from app.models import Base
from app.models.approval import Approval
from app.models.core import ObjectEvidenceLink
from app.models.delivery import AlarmDefinition, HMIBinding, HMIScreen
from app.models.evidence import EvidenceAnchor
from app.models.file import UploadedFile
from app.models.finding import Finding, FindingEvidence
from app.models.interlocks import (
    CauseEffectEntry,
    FaultDefinition,
    Interlock,
    Permissive,
    SequenceStep,
)
from app.models.project import Project
from app.models.tags import Tag
from app.models.user import User

# ---------------------------------------------------------------------------
# Arrangement helpers
# ---------------------------------------------------------------------------


def _anchor(db: Session, project: Project) -> EvidenceAnchor:
    """An anchor and the file it points into, as a parse would leave them."""
    record = UploadedFile(
        project_id=project.id,
        original_filename="Line3.L5X",
        stored_filename="line3.l5x",
        relative_path="uploads/line3.l5x",
    )
    db.add(record)
    db.flush()
    anchor = EvidenceAnchor(
        project_id=project.id,
        file_id=record.id,
        anchor_key="rung:MainProgram/Conveyor_Interlocks/0",
        anchor_type=AnchorType.L5X_RUNG,
        source_label="Line3.L5X",
        display_location="MainProgram / Conveyor_Interlocks / Rung 0",
        excerpt="XIC(Guard_Closed) OTE(Conveyor_Run)",
    )
    db.add(anchor)
    db.flush()
    return anchor


def _reparse_removes(db: Session, anchor: EvidenceAnchor) -> None:
    """Delete the anchor exactly the way a re-parse does.

    ``app.core.services.parsing._persist_anchors`` issues a Core ``DELETE`` for
    every anchor key the current parse no longer produces, so the ORM's own
    cascade rules never see it and only the database's ``ON DELETE`` action
    applies. Using the same statement here is the point of the test.
    """
    db.execute(delete(EvidenceAnchor).where(EvidenceAnchor.id == anchor.id))
    db.flush()
    db.expire_all()


def _finding(db: Session, project: Project) -> Finding:
    finding = Finding(
        project_id=project.id,
        title="Guard-door interlock is documented and not implemented",
        dedupe_key="interlock_review:ILK-003",
        requires_approval=True,
    )
    db.add(finding)
    db.flush()
    return finding


# ---------------------------------------------------------------------------
# Cascades that would destroy evidence
# ---------------------------------------------------------------------------


def test_a_reparse_leaves_the_basis_of_an_approved_object_readable(
    db: Session, project: Project
) -> None:
    """The traceability table is the product's central promise; a re-parse is routine.

    Anchor keys are positional. A renamed routine, moved rungs, or simply a
    parser-version bump makes the old keys disappear and the anchors are hard
    deleted. Under a cascade the citation went with them, so an interlock an
    engineer had already approved was left with no basis and nothing recording
    that it ever had one — indistinguishable, at the next validation run, from
    an object generated for no reason at all.
    """
    anchor = _anchor(db, project)
    interlock = Interlock(
        project_id=project.id,
        reference="ILK-003",
        name="Guard door open stops conveyor",
        kind=InterlockKind.INTERLOCK,
        approval_state=ObjectApprovalState.APPROVED,
    )
    db.add(interlock)
    db.flush()
    link = ObjectEvidenceLink(
        project_id=project.id,
        object_type=ObjectType.INTERLOCK,
        object_id=interlock.id,
        anchor_id=anchor.id,
        basis_kind=EvidenceKind.DIRECT,
        note="Rung 0 of Conveyor_Interlocks drops the run bit on Guard_Closed.",
    )
    db.add(link)
    db.flush()
    link_id = link.id

    _reparse_removes(db, anchor)

    survivor = db.get(ObjectEvidenceLink, link_id)
    assert survivor is not None, "the citation was deleted with the anchor"
    assert survivor.anchor_id is None
    assert survivor.note.startswith("Rung 0 of Conveyor_Interlocks")
    assert survivor.basis_kind is EvidenceKind.DIRECT
    # The object is now untraceable, which is a state to report rather than a
    # row to lose: the difference is that this one can still be reported.
    assert survivor.satisfies_traceability is False


def test_a_reparse_leaves_a_delivered_findings_citation_readable(
    db: Session, project: Project
) -> None:
    """``Finding`` promises at least one evidence row; a cascade broke it silently.

    The executor enforces the invariant when the finding is written and nothing
    re-checks it afterwards. A CRITICAL finding that had already been reviewed,
    approved and delivered would come back from a later re-parse presented as a
    claim with no evidence at all.
    """
    anchor = _anchor(db, project)
    finding = _finding(db, project)
    link = FindingEvidence(
        finding_id=finding.id,
        anchor_id=anchor.id,
        evidence_kind=EvidenceKind.DIRECT,
        role="logic",
        note="No rung reads Guard_Closed anywhere in the controller.",
    )
    db.add(link)
    db.flush()

    _reparse_removes(db, anchor)

    db.refresh(finding)
    assert len(finding.evidence_links) == 1, "the finding lost its only evidence row"
    survivor = finding.evidence_links[0]
    assert survivor.anchor_id is None
    assert survivor.anchor is None
    assert survivor.role == "logic"
    assert survivor.note.startswith("No rung reads Guard_Closed")


def test_deleting_a_finding_keeps_the_risk_a_named_engineer_accepted(
    db: Session, project: Project, seeded: dict[str, User]
) -> None:
    """Findings are hard-deleted in normal operation, approvals must outlive them.

    A re-run of an at-least-once task re-uses the existing finding row by
    dedupe key and discards it when its evidence no longer resolves. Cascading
    the approval away at that moment erases the decision, its justification and
    the name attached to it, and the release record then looks like a release
    with no accepted risk at all — which is exactly the distinction
    ``ApprovalStatus.RISK_ACCEPTED`` exists to keep.
    """
    finding = _finding(db, project)
    reviewer = seeded["reviewer"]
    approval = Approval(
        project_id=project.id,
        finding_id=finding.id,
        kind=ApprovalKind.FINDING_RELEASE,
        status=ApprovalStatus.RISK_ACCEPTED,
        decision=ApprovalDecision.ACCEPT_RISK,
        title="Accept the unimplemented guard-door interlock for this delivery",
        question="Do you accept shipping with the documented guard interlock unimplemented?",
        decided_by_id=reviewer.id,
        decision_notes="Hardwired guard relay verified on site; PLC interlock is scheduled.",
    )
    db.add(approval)
    db.flush()
    approval_id = approval.id

    db.delete(finding)
    db.flush()
    db.expire_all()

    survivor = db.get(Approval, approval_id)
    assert survivor is not None, "the human decision was deleted with its subject"
    assert survivor.finding_id is None
    assert survivor.status is ApprovalStatus.RISK_ACCEPTED
    assert survivor.decision is ApprovalDecision.ACCEPT_RISK
    assert survivor.decided_by_id == reviewer.id
    assert "Hardwired guard relay" in survivor.decision_notes


def test_no_reference_to_an_evidence_anchor_cascades() -> None:
    """Stated over the metadata so a new citation table inherits the rule.

    Anchors are the one kind of row this system deletes as a matter of course.
    Any table that points at one and cascades is a table that will lose its own
    content to an infrastructure event.
    """
    offenders: list[str] = []
    for table in Base.metadata.tables.values():
        for constraint in table.constraints:
            if not isinstance(constraint, ForeignKeyConstraint):
                continue
            if constraint.referred_table.name != "evidence_anchors":
                continue
            if (constraint.ondelete or "").upper() != "SET NULL":
                columns = ", ".join(element.parent.key for element in constraint.elements)
                offenders.append(f"{table.name}.{columns} -> ON DELETE {constraint.ondelete}")
    assert not offenders, offenders


def test_nothing_can_delete_an_approval_except_deleting_the_project() -> None:
    """An approval is the durable record of a human accepting responsibility.

    Every subject an approval can be about — task, finding, recommendation,
    report — is deletable, so every reference to one has to be ``SET NULL``.
    The single exception is the project: deleting an engagement is a deliberate
    act that removes the whole record, not an event that quietly rewrites it.
    """
    approvals = Base.metadata.tables["approvals"]
    offenders: list[str] = []
    for constraint in approvals.constraints:
        if not isinstance(constraint, ForeignKeyConstraint):
            continue
        columns = [element.parent.key for element in constraint.elements]
        if columns == ["project_id"]:
            continue
        if (constraint.ondelete or "").upper() != "SET NULL":
            offenders.append(f"approvals.{', '.join(columns)} -> ON DELETE {constraint.ondelete}")
    assert not offenders, offenders


# ---------------------------------------------------------------------------
# Defaults that would state a decision nobody made
# ---------------------------------------------------------------------------

#: Enum member names the vocabulary uses to mean "nothing is being claimed here".
_ASSERTS_NOTHING = frozenset(
    {
        "UNDEFINED",
        "UNKNOWN",
        "OTHER",
        "UNCLASSIFIED",
        "UNSPECIFIED",
        "NOT_STATED",
        "NOT_APPLICABLE",
        "NOT_ATTEMPTED",
        "NONE",
    }
)

#: ``origin``, ``approval_state`` and ``status`` describe where a row is in a
#: workflow this platform owns. Their defaults are the starting state of that
#: workflow, not a claim about the customer's plant, so they are outside the
#: rule below.
_LIFECYCLE_COLUMNS = frozenset({"origin", "approval_state", "status"})

#: Columns whose default is a real member of an enum that also offers a neutral
#: one, reviewed and kept. Each entry needs a reason that is about this column,
#: not about convenience.
_REVIEWED_EXCEPTIONS: dict[tuple[str, str], str] = {
    (
        "DesignConstraint",
        "kind",
    ): "A DesignConstraint is a constraint by construction; OTHER would be less true, not more.",
}

#: Columns this lens established are still stating an engineering decision by
#: default, and which could not be fixed here because the fix is not confined to
#: the model layer. Asserted as an exact set: fixing one of these fails this
#: test until it is removed from the list, and a new one fails it immediately.
_STILL_FABRICATING: dict[tuple[str, str], str] = {
    (
        "Program",
        "program_type",
    ): (
        "NORMAL asserts a program is not safety logic. ProgramType has no neutral member and "
        "app/agents/review/plc_logic.py reads .value, so the column can be neither defaulted "
        "honestly nor made nullable without a coordinated change there and in the ~8 test "
        "fixtures that build a Program without stating a type."
    ),
    (
        "Routine",
        "language",
    ): (
        "LADDER asserts what kind of logic a routine contains. Same shape as program_type: "
        "app/validation/logic.py and app/agents/review/plc_logic.py both read .value."
    ),
    (
        "Requirement",
        "priority",
    ): (
        "EXPECTED invents a customer priority; app/validation/requirements.py reads it to decide "
        "whether uncovered work blocks. RequirementPriority has no neutral member and three "
        "call sites plus app/api/schemas/events.py read .value."
    ),
}


def _defaulted_enum_columns() -> list[tuple[str, str, Enum]]:
    found: list[tuple[str, str, Enum]] = []
    for mapper in Base.registry.mappers:
        for column in mapper.local_table.columns:
            if not isinstance(column.type, StrEnumType):
                continue
            value = getattr(column.default, "arg", None)
            if isinstance(value, Enum):
                found.append((mapper.class_.__name__, column.key, value))
    return found


def test_a_defaulted_enum_column_uses_the_member_that_asserts_nothing() -> None:
    """Where the vocabulary offers a neutral member, a default must be it.

    This is the convention ``app/models/interlocks.py`` states in its own
    docstring, applied to every enum column in the model. It is mechanical
    rather than curated on purpose: it catches the *next* column somebody
    defaults to a real classification, which is the failure mode this rule
    exists for.
    """
    offenders: list[str] = []
    for model, column, default in _defaulted_enum_columns():
        if column in _LIFECYCLE_COLUMNS:
            continue
        if default.name in _ASSERTS_NOTHING:
            continue
        if any(member.name in _ASSERTS_NOTHING for member in type(default)):
            if (model, column) in _REVIEWED_EXCEPTIONS:
                continue
            neutral = sorted(m.name for m in type(default) if m.name in _ASSERTS_NOTHING)
            offenders.append(
                f"{model}.{column} defaults to {type(default).__name__}.{default.name} "
                f"when {neutral} says nothing"
            )
    assert not offenders, offenders


def test_the_defaults_that_still_state_a_decision_are_exactly_the_known_ones() -> None:
    """A ledger, not an amnesty.

    Three columns default to a real member of an enum with no neutral one, and
    each needs a change outside ``app/models`` to fix. Listing them here keeps
    them visible and stops a fourth being added quietly; removing one from the
    list is part of fixing it.
    """
    outstanding: set[tuple[str, str]] = set()
    for model, column, default in _defaulted_enum_columns():
        if column in _LIFECYCLE_COLUMNS:
            continue
        if default.name in _ASSERTS_NOTHING:
            continue
        if any(member.name in _ASSERTS_NOTHING for member in type(default)):
            continue
        outstanding.add((model, column))

    # Severity, validation level and rule severity are report-side gradings this
    # platform assigns to its own output, and each defaults to the weakest claim
    # available, so an unset one under-claims rather than over-claims.
    outstanding -= {
        ("Finding", "severity"),
        ("ExportArtifact", "validation_level"),
        ("ReleaseCandidate", "validation_level"),
        ("TagNamingRule", "severity"),
        ("TagValidationResult", "severity"),
    }
    # Release mode and restricted-domain mode default to the most restrictive
    # setting either enum offers, so an unset one withholds capability rather
    # than granting it.
    outstanding -= {
        ("ProjectProfile", "release_mode"),
        ("ProjectProfile", "restricted_domain_mode"),
    }
    # Presentation and structural conventions, not engineering judgements:
    # ``units`` selects how a number is displayed and nothing converts on it,
    # ``scope`` is what Logix itself means by a tag declared outside a program,
    # and ``element_kind`` is a position within a rung that the IR builder
    # always states.
    outstanding -= {
        ("ProjectProfile", "units"),
        ("Tag", "scope"),
        ("LogicInstruction", "element_kind"),
    }
    # Both ``evidence_kind`` defaults are DIRECT, which is the strongest claim
    # the enum can make, and EvidenceKind has no member that makes none. They
    # are out of scope here rather than acceptable: every construction site
    # assigns the field explicitly, so no live path reaches the default, and
    # the honest fix is a new vocabulary member — which belongs to whoever owns
    # app/core/enums.py, not to this module.
    outstanding -= {
        ("EvidenceAnchor", "evidence_kind"),
        ("FindingEvidence", "evidence_kind"),
    }

    assert outstanding == set(_STILL_FABRICATING), {
        "newly fabricating": sorted(outstanding - set(_STILL_FABRICATING)),
        "fixed, remove from the ledger": sorted(set(_STILL_FABRICATING) - outstanding),
    }


def test_an_alarm_severity_nobody_stated_is_stored_as_not_stated(
    db: Session, project: Project
) -> None:
    """Severity is how fast an operator must respond, judged against the consequence.

    A fault list that gives a trigger and a corrective action but no severity —
    the usual shape, because severity normally lives in a separate alarm
    philosophy document — has not made that judgement. ``MEDIUM`` used to say
    somebody had, and because it is a real value nothing reported the gap.
    """
    fault = FaultDefinition(
        project_id=project.id,
        reference="FLT-12",
        name="Flame failure",
        fault_class=FaultClass.SAFETY,
        corrective_action="Purge and re-attempt ignition once.",
    )
    db.add(fault)
    db.flush()
    db.refresh(fault)

    assert fault.severity is None
    assert inspect(FaultDefinition).columns["severity"].default is None
    # The identical field on AlarmDefinition set the precedent; keep them equal.
    assert inspect(AlarmDefinition).columns["severity"].default is None


def test_an_alarm_latch_nobody_stated_is_not_recorded_as_self_clearing(
    db: Session, project: Project
) -> None:
    """``latched`` is the sole gate on the reset-behaviour check.

    ``False`` is a positive claim — "this alarm clears itself when the condition
    goes away" — and it also made ``ResetBehaviourValidator`` skip the alarm, so
    a release-blocking ERROR was never raised for any project whose alarm list
    had no latch column.
    """
    alarm = AlarmDefinition(
        project_id=project.id,
        reference="ALM-101",
        name="E-stop pressed",
        severity=AlarmSeverity.URGENT,
        ordinal=0,
    )
    db.add(alarm)
    db.flush()
    db.refresh(alarm)

    assert alarm.latched is None
    assert inspect(AlarmDefinition).columns["latched"].nullable is True


@pytest.mark.parametrize(
    ("model", "column"),
    [
        (Interlock, "is_implemented"),
        (Permissive, "is_implemented"),
        (CauseEffectEntry, "is_implemented"),
        (SequenceStep, "is_implemented"),
        (FaultDefinition, "is_driven"),
    ],
    ids=lambda item: item if isinstance(item, str) else item.__name__,
)
def test_an_implementation_status_holds_three_values(model: type, column: str) -> None:
    """ "Not searched" and "searched and absent" are different facts.

    A boolean defaulting to ``False`` made them the same value, so a project
    that had been ingested and never cross-referenced asserted that every
    documented protection was missing.
    """
    mapped = inspect(model).columns[column]
    assert mapped.nullable is True, f"{model.__name__}.{column} cannot record 'not searched'"
    assert mapped.default is None, f"{model.__name__}.{column} still defaults"


def test_an_interlock_nobody_searched_for_is_not_reported_as_searched_for(
    db: Session, project: Project
) -> None:
    """The claim and the search behind it are now separable.

    ``is_documented_but_missing`` deliberately still fires while the search is
    unestablished — silence about a documented protection is the one outcome
    that must never be a default — but ``is_implementation_established`` now
    lets a caller say which of the two it is looking at.
    """
    interlock = Interlock(
        project_id=project.id,
        reference="ILK-022",
        name="Low suction pressure trips pump",
        kind=InterlockKind.TRIP,
        source_reference="Interlock schedule sheet 2, row 22",
    )
    db.add(interlock)
    db.flush()
    db.refresh(interlock)

    assert interlock.is_implemented is None
    assert interlock.is_implementation_established is False
    assert interlock.is_documented_but_missing is True
    assert interlock.has_implementation is False

    interlock.is_implemented = False
    db.flush()
    assert interlock.is_implementation_established is True
    assert interlock.is_documented_but_missing is True

    interlock.is_implemented = True
    db.flush()
    assert interlock.is_documented_but_missing is False
    # Still no rung, so the claim remains unverifiable.
    assert interlock.has_implementation is False


def test_an_hmi_binding_of_unstated_direction_is_not_treated_as_a_display(
    db: Session, project: Project
) -> None:
    """An unhandled write is invisible on the plant floor; an unresolved read is not.

    The two ways of guessing an unstated direction are not symmetrical, so this
    one fails closed. A binding with no stated access is reported as a command,
    which is louder than it might need to be and never quieter than it must be.
    """
    screen = HMIScreen(project_id=project.id, key="L4_OVERVIEW", name="Line 4 Overview", ordinal=0)
    db.add(screen)
    db.flush()
    binding = HMIBinding(
        project_id=project.id,
        screen_id=screen.id,
        property_name="Value",
        tag_name="MTR_101_Cmd_Start",
    )
    db.add(binding)
    db.flush()
    db.refresh(binding)

    assert binding.access == ""
    assert binding.is_access_stated is False
    assert binding.is_command is True

    binding.access = "read"
    assert binding.is_access_stated is True
    assert binding.is_command is False


def test_a_tag_whose_source_omitted_external_access_does_not_export_read_write(
    db: Session, project: Project
) -> None:
    """The value is round-tripped into the export verbatim, so it must not be invented.

    ``Read/Write`` is the most permissive setting Logix offers. Defaulted, it
    reached the ``<Tag>`` element and the tag CSV on every generated tag —
    interlock and permissive status bits included — indistinguishable from a
    value the customer chose. Both writers omit the attribute when it is empty.
    """
    tag = Tag(project_id=project.id, name="ILK_003_Tripped")
    db.add(tag)
    db.flush()
    db.refresh(tag)

    assert tag.external_access == ""
    # The rule the same module already applied to UDT members, 246 lines later.
    assert inspect(Tag).columns["external_access"].default.arg == ""


def test_an_impact_report_cannot_present_itself_as_informational_before_it_is_computed() -> None:
    """``INFORMATION`` is the "this breaks nothing" end of the scale.

    A rename is approved on the basis of an impact report, so an uncomputed one
    must not be able to arrive at the reviewer's screen by omission.
    """
    from app.models.revision import ImpactReport

    assert inspect(ImpactReport).columns["highest_severity"].default is None


def test_an_object_evidence_link_still_names_its_basis_kind_without_an_anchor() -> None:
    """A regression guard on the shape the SET NULL fix depends on.

    ``anchor_id`` going null is only survivable because ``basis_kind`` and
    ``note`` are on the link row rather than on the anchor. If either moved,
    the fix above would silently become an empty row again.
    """
    columns = inspect(ObjectEvidenceLink).columns
    assert columns["basis_kind"].nullable is False
    assert columns["note"].nullable is False
    assert columns["anchor_id"].nullable is True


def test_finding_evidence_survives_its_anchor_with_its_role_intact() -> None:
    """Same guard for the finding side: the link carries its own meaning."""
    columns = inspect(FindingEvidence).columns
    assert columns["anchor_id"].nullable is True
    assert columns["evidence_kind"].nullable is False
    assert columns["role"].nullable is False


def test_every_model_referenced_here_is_still_mapped() -> None:
    """A floor: renaming a class out from under this module must not skip its tests."""
    names = {mapper.class_.__name__ for mapper in Base.registry.mappers}
    required = {
        "Approval",
        "AlarmDefinition",
        "CauseEffectEntry",
        "FaultDefinition",
        "FindingEvidence",
        "HMIBinding",
        "ImpactReport",
        "Interlock",
        "ObjectEvidenceLink",
        "Permissive",
        "SequenceStep",
        "Tag",
    }
    assert required <= names, sorted(required - names)
