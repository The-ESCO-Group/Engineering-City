"""The approval gates: what they block, what they say, and what they refuse to claim.

Every test here is a product rule rather than a code path. Four of them are the
ones that matter, and each asserts the *negative* case explicitly rather than
trusting a status enum:

* a gate that is not ready names what is missing;
* a decision does not survive a merge that changed what it was about;
* a decision that needs a written reason is refused without one;
* gate 5 does not report a pass for an import that never happened.

The validation runs below are written directly rather than produced by
:func:`~app.validation.run_validation`. A gate reads a run; what makes a run
correct is the validation package's own business, and driving twenty-five
validators over a three-row fixture would test those instead of these.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.agents.base.actions import (
    ChangeOperationProposal,
    ObjectBasis,
    ProposeChangesetPayload,
)
from app.api.schemas.events import GateStatusChangedEventPayload
from app.core.clock import utcnow
from app.core.enums import (
    AlarmSeverity,
    ApprovalDecision,
    ApprovalGateKey,
    ApprovalKind,
    ApprovalStatus,
    AuditAction,
    BaseDataType,
    ChangeOperationType,
    ControllerFamily,
    EventType,
    EvidenceKind,
    ExportArtifactKind,
    FailState,
    FaultClass,
    FindingSeverity,
    FindingStatus,
    GateStatus,
    HmiProduct,
    ImportVerificationStatus,
    ModuleRole,
    ObjectType,
    ProfileFieldStatus,
    ProjectType,
    RecommendationStatus,
    ReleaseCandidateStatus,
    ReleaseMode,
    RequirementStatus,
    RestrictedDomain,
    RestrictedDomainMode,
    RoutineLanguage,
    SignalDirection,
    SignalType,
    UnitSystem,
    ValidationDiscipline,
    ValidationLevel,
    ValidationRunStatus,
    ValidationSeverity,
)
from app.core.errors import ConflictError, PermissionDeniedError, ValidationError
from app.database.session import get_session_factory
from app.engineering import changesets, gates, merge
from app.models.approval import Approval
from app.models.audit import AuditEntry
from app.models.delivery import AlarmDefinition
from app.models.event import Event
from app.models.finding import Finding, Recommendation
from app.models.interlocks import FaultDefinition
from app.models.plant import Device, HardwareModule, IOPoint, Signal
from app.models.plc import LogicRung, Program, Routine
from app.models.profile import (
    REQUIRED_PROFILE_FIELDS,
    ProjectProfile,
    ProjectQuestion,
    TargetPlatform,
    TemplateSet,
)
from app.models.project import Project
from app.models.release import (
    ExportArtifact,
    ReleaseCandidate,
    ReleaseManifest,
    ValidationResult,
    ValidationRun,
    WorkstationVerificationJob,
)
from app.models.requirements import Requirement
from app.models.revision import ApprovalGate
from app.models.tags import Tag
from app.models.user import User
from app.validation import ALL_DISCIPLINES

CATALOG_NUMBER = "1756-L83E"
FIRMWARE = "35.011"
STUDIO_VERSION = "35.00"
TEMPLATE_ID = "esco.motor.standard@1.0.0"


# ---------------------------------------------------------------------------
# Builders. Each one satisfies exactly the criteria named in its docstring, so a
# test that wants a gate to fail can leave one out and nothing else moves.
# ---------------------------------------------------------------------------
def _template_set(db: Session, project: Project) -> TemplateSet:
    template_set = TemplateSet(
        project_id=project.id,
        key="esco.templates.discrete",
        display_name="ESCO discrete templates",
        version="1.0.0",
        template_ids=[TEMPLATE_ID],
        checksum="0" * 64,
        supported_controller_families=[ControllerFamily.CONTROLLOGIX.value],
    )
    db.add(template_set)
    db.flush()
    return template_set


def _profile(db: Session, project: Project, **overrides: object) -> ProjectProfile:
    """A complete profile: gate 1's field, scope and template criteria."""
    template_set = _template_set(db, project)
    body: dict[str, object] = {
        "project_name": "Line 4 conveyor upgrade",
        "project_description": "Synthetic fixture description.",
        "project_type": ProjectType.UPGRADE,
        "naming_standard_version": "3.2",
        "alarm_philosophy_version": "1.1",
        "template_set_id": template_set.id,
        "units": UnitSystem.METRIC,
        "locale": "en-GB",
        "release_mode": ReleaseMode.IMPORT_PACKAGE_ONLY,
        # ``engineering_standard_set_id`` is deliberately left null: the profile
        # criteria read ``completeness``, which is where "somebody has actually
        # chosen a standard set" is recorded, not the foreign key.
        "completeness": dict.fromkeys(REQUIRED_PROFILE_FIELDS, ProfileFieldStatus.PRESENT.value),
        "missing_required_fields": [],
        "is_complete_for_generation": True,
    }
    body.update(overrides)
    profile = ProjectProfile(project_id=project.id, **body)  # type: ignore[arg-type]
    db.add(profile)
    db.flush()
    return profile


def _platform(db: Session, project: Project, **overrides: object) -> TargetPlatform:
    """A complete target platform with no HMI in scope."""
    body: dict[str, object] = {
        "controller_family": ControllerFamily.CONTROLLOGIX,
        "controller_catalog_number": CATALOG_NUMBER,
        "firmware_revision": FIRMWARE,
        "studio_5000_version": STUDIO_VERSION,
        "hmi_product": HmiProduct.NONE,
    }
    body.update(overrides)
    platform = TargetPlatform(project_id=project.id, **body)  # type: ignore[arg-type]
    db.add(platform)
    db.flush()
    return platform


def _requirement(
    db: Session,
    project: Project,
    *,
    status: RequirementStatus = RequirementStatus.ACCEPTED,
    reference: str = "REQ-001",
) -> Requirement:
    requirement = Requirement(
        project_id=project.id,
        reference=reference,
        title="Conveyor stops when the guard opens",
        statement="The conveyor shall stop when the guard door opens.",
        source_text="The conveyor stops when the guard door opens.",
        status=status,
    )
    db.add(requirement)
    db.flush()
    return requirement


def _plant(db: Session, project: Project) -> Device:
    """One wired device, its signal, its card and its terminal: gate 2 and gate 3."""
    device = Device(
        project_id=project.id,
        tag_prefix="MTR_101",
        name="Conveyor drive motor",
        fail_state=FailState.DE_ENERGIZE,
    )
    db.add(device)
    db.flush()
    db.add(
        Signal(
            project_id=project.id,
            device_id=device.id,
            name="Run_Cmd",
            signal_type=SignalType.DISCRETE_OUTPUT,
            direction=SignalDirection.OUTPUT,
            ordinal=0,
        )
    )
    module = HardwareModule(
        project_id=project.id, name="Local_2", catalog_number="1756-OB16E", role=ModuleRole.LOCAL_IO
    )
    db.add(module)
    db.flush()
    db.add(
        IOPoint(
            project_id=project.id,
            hardware_module_id=module.id,
            address="Local:2:O.Data.3",
            signal_type=SignalType.DISCRETE_OUTPUT,
            direction=SignalDirection.OUTPUT,
        )
    )
    db.flush()
    return device


def _tag(db: Session, project: Project, name: str = "MTR_101_Cmd_Run") -> None:
    db.add(
        Tag(
            project_id=project.id,
            name=name,
            base_data_type=BaseDataType.BOOL,
            description="Run command from the HMI.",
        )
    )
    db.flush()


def _logic(db: Session, project: Project) -> Routine:
    """A program holding one ladder routine with one rung: gate 3."""
    program = Program(project_id=project.id, name="MainProgram", ordinal=0)
    db.add(program)
    db.flush()
    routine = Routine(
        project_id=project.id,
        program_id=program.id,
        name="Conveyor_Control",
        language=RoutineLanguage.LADDER,
        ordinal=0,
        is_main=True,
    )
    db.add(routine)
    db.flush()
    db.add(LogicRung(project_id=project.id, routine_id=routine.id, ordinal=0, text="XIC(A)OTE(B);"))
    db.flush()
    return routine


def _validation_run(
    db: Session,
    project: Project,
    *,
    revision: int | None = None,
    disciplines: tuple[ValidationDiscipline, ...] = ALL_DISCIPLINES,
    errors: int = 0,
) -> ValidationRun:
    revision_number = project.current_revision_number if revision is None else revision
    run = ValidationRun(
        project_id=project.id,
        revision_number=revision_number,
        status=ValidationRunStatus.COMPLETED,
        disciplines=[discipline.value for discipline in disciplines],
        checks_run=len(disciplines),
        error_count=errors,
        finished_at=utcnow(),
    )
    db.add(run)
    db.flush()
    for index in range(errors):
        db.add(
            ValidationResult(
                project_id=project.id,
                validation_run_id=run.id,
                code="tags.duplicate_name",
                discipline=ValidationDiscipline.TAGS,
                severity=ValidationSeverity.ERROR,
                message=f"Synthetic error {index}.",
                object_label=f"Tag SYNTH_{index}",
            )
        )
    db.flush()
    return run


def _release(db: Session, project: Project, **overrides: object) -> ReleaseCandidate:
    """A generated candidate with a frozen, hashed manifest: gates 5 and 6."""
    body: dict[str, object] = {
        "revision_number": project.current_revision_number,
        "candidate_number": 1,
        "name": "RC 1",
        "status": ReleaseCandidateStatus.GENERATED,
        "validation_level": ValidationLevel.ROUND_TRIP_PARSED,
        "target_profile_snapshot": {
            "controller_catalog_number": CATALOG_NUMBER,
            "firmware_revision": FIRMWARE,
            "studio_5000_version": STUDIO_VERSION,
        },
    }
    body.update(overrides)
    candidate = ReleaseCandidate(project_id=project.id, **body)  # type: ignore[arg-type]
    db.add(candidate)
    db.flush()
    db.add(
        ReleaseManifest(
            project_id=project.id,
            release_candidate_id=candidate.id,
            manifest_version="1",
            content={"artifacts": []},
            content_sha256="a" * 64,
            checksum_file_sha256="b" * 64,
            frozen_at=utcnow(),
        )
    )
    db.flush()
    return candidate


def _ready_project(db: Session, project: Project) -> None:
    """Everything the five reachable gates need, and nothing they do not."""
    _profile(db, project)
    _platform(db, project)
    _requirement(db, project)
    _plant(db, project)
    _tag(db, project)
    _logic(db, project)
    _validation_run(db, project)
    _release(db, project)


def _gate(db: Session, project: Project, key: ApprovalGateKey) -> ApprovalGate:
    return next(gate for gate in gates.evaluate_all(db, project=project) if gate.gate_key is key)


def _details(gate: ApprovalGate) -> str:
    return " ".join(str(item["detail"]) for item in gate.unmet_criteria)


def _keys(gate: ApprovalGate) -> set[str]:
    return {str(item["key"]) for item in gate.unmet_criteria}


def _pass(
    db: Session,
    project: Project,
    key: ApprovalGateKey,
    reviewer: User,
    *,
    decision: ApprovalDecision = ApprovalDecision.APPROVE,
    comment: str = "Reviewed against the requirements.",
) -> ApprovalGate:
    gate = _gate(db, project, key)
    approval = gates.request_decision(
        db, project=project, gate=gate, requested_by_agent="project_manager"
    )
    return gates.record_decision(
        db, gate=gate, approval=approval, decision=decision, user=reviewer, comment=comment
    )


# ---------------------------------------------------------------------------
# The rows themselves
# ---------------------------------------------------------------------------
def test_ensure_gates_creates_the_six_in_order_and_is_idempotent(
    db: Session, project: Project
) -> None:
    """Called from the workflow, the API and the evaluator; none may go second."""
    first = gates.ensure_gates(db, project=project)
    second = gates.ensure_gates(db, project=project)

    assert [gate.gate_key for gate in first] == list(ApprovalGateKey)
    assert [gate.ordinal for gate in first] == [0, 1, 2, 3, 4, 5]
    assert [gate.id for gate in second] == [gate.id for gate in first]
    rows = db.execute(select(ApprovalGate).where(ApprovalGate.project_id == project.id)).scalars()
    assert len(rows.all()) == 6
    assert [gate.title for gate in second] == [spec.title for spec in gates.GATE_SPECS]


def test_a_legacy_pass_without_a_bound_approval_fails_closed(
    db: Session, project: Project, seeded: dict[str, User]
) -> None:
    _ready_project(db, project)
    gate = _gate(db, project, ApprovalGateKey.PROJECT_PROFILE)
    gate.status = GateStatus.PASSED
    gate.decided_revision_number = project.current_revision_number
    gate.decided_by_id = seeded["reviewer"].id
    gate.decided_at = utcnow()
    gate.approval_id = None
    db.flush()

    repaired = _gate(db, project, ApprovalGateKey.PROJECT_PROFILE)

    assert repaired.status is GateStatus.NOT_READY
    assert "recorded_decision_valid" in _keys(repaired)
    assert "not bound" in _details(repaired)


def test_a_settled_gate_whose_approval_names_another_decider_fails_closed(
    db: Session, project: Project, seeded: dict[str, User]
) -> None:
    _ready_project(db, project)
    gate = _pass(db, project, ApprovalGateKey.PROJECT_PROFILE, seeded["reviewer"])
    approval = db.get(Approval, gate.approval_id)
    assert approval is not None
    approval.decided_by_id = seeded["engineer"].id
    db.flush()

    repaired = _gate(db, project, ApprovalGateKey.PROJECT_PROFILE)

    assert repaired.status is GateStatus.NOT_READY
    assert "recorded_decision_valid" in _keys(repaired)
    assert "reviewer" in _details(repaired)


def test_a_settled_gate_with_a_mismatched_decision_timestamp_fails_closed(
    db: Session, project: Project, seeded: dict[str, User]
) -> None:
    _ready_project(db, project)
    gate = _pass(db, project, ApprovalGateKey.PROJECT_PROFILE, seeded["reviewer"])
    assert gate.decided_at is not None
    gate.decided_at = gate.decided_at + timedelta(seconds=1)
    db.flush()

    repaired = _gate(db, project, ApprovalGateKey.PROJECT_PROFILE)

    assert repaired.status is GateStatus.NOT_READY
    assert "recorded_decision_valid" in _keys(repaired)
    assert "decision time" in _details(repaired)


def test_a_gate_that_is_not_ready_names_what_is_missing(db: Session, project: Project) -> None:
    """The whole point of the feature.

    A gate reporting "not ready" with an empty reason gives a UI nothing to
    render and an engineer nothing to do, and is how a gate feature ends up
    ignored.
    """
    gate = _gate(db, project, ApprovalGateKey.PROJECT_PROFILE)

    assert gate.status is GateStatus.NOT_READY
    assert gate.unmet_criteria, "a not-ready gate with no unmet criteria is unusable"
    assert "profile_recorded" in _keys(gate)
    assert "No project profile has been recorded" in _details(gate)
    assert all(item["statement"] for item in gate.criteria)


# ---------------------------------------------------------------------------
# The lifecycle, gate by gate
# ---------------------------------------------------------------------------
def test_every_gate_walks_not_ready_to_ready_to_awaiting_to_passed(
    db: Session, project: Project, seeded: dict[str, User]
) -> None:
    """One pass through the whole mechanism, in the order a project lives it."""
    reviewer = seeded["reviewer"]
    initial = gates.evaluate_all(db, project=project)
    assert {gate.status for gate in initial} == {GateStatus.NOT_READY}

    _ready_project(db, project)

    for key in (
        ApprovalGateKey.PROJECT_PROFILE,
        ApprovalGateKey.DEVICES_IO_TAGS,
        ApprovalGateKey.LOGIC_AND_DELIVERY,
        ApprovalGateKey.QA_AND_RISK,
    ):
        gate = _gate(db, project, key)
        assert gate.status is GateStatus.READY, f"{key.value}: {_details(gate)}"

        approval = gates.request_decision(
            db, project=project, gate=gate, requested_by_agent="project_manager"
        )
        assert approval.status is ApprovalStatus.PENDING
        assert _gate(db, project, key).status is GateStatus.AWAITING_DECISION

        passed = gates.record_decision(
            db,
            gate=gate,
            approval=approval,
            decision=ApprovalDecision.APPROVE,
            user=reviewer,
            comment="Checked against the functional description.",
        )
        assert passed.status is GateStatus.PASSED
        assert passed.decided_by_id == reviewer.id
        assert passed.decided_revision_number == project.current_revision_number

    # Gate 5 is skipped rather than passed; see its own test.
    assert _gate(db, project, ApprovalGateKey.IMPORT_VERIFICATION).status is GateStatus.SKIPPED

    release = _gate(db, project, ApprovalGateKey.RELEASE)
    assert release.status is GateStatus.READY, _details(release)
    released = _pass(
        db,
        project,
        ApprovalGateKey.RELEASE,
        reviewer,
        comment="Released to site as an import-ready candidate; import not verified.",
    )
    assert released.status is GateStatus.PASSED
    assert gates.gate_blocking(db, project=project, gate_key=ApprovalGateKey.RELEASE) is None


@pytest.mark.parametrize(
    ("key", "criterion", "detail_fragment"),
    [
        (ApprovalGateKey.PROJECT_PROFILE, "target_platform_complete", "firmware revision"),
        (ApprovalGateKey.DEVICES_IO_TAGS, "structured_types_resolve", "ESCO_Motor"),
        (ApprovalGateKey.LOGIC_AND_DELIVERY, "ladder_routines_hold_logic", "no rungs"),
        (ApprovalGateKey.QA_AND_RISK, "serious_findings_decided", "critical or high"),
    ],
)
def test_each_gate_reports_its_own_failure_precisely(
    db: Session,
    project: Project,
    key: ApprovalGateKey,
    criterion: str,
    detail_fragment: str,
) -> None:
    """A criterion says which thing is wrong, not merely that something is."""
    _profile(db, project)
    _platform(db, project, firmware_revision="")
    _plant(db, project)
    db.add(
        Tag(
            project_id=project.id,
            name="MTR_101_State",
            base_data_type=BaseDataType.UDT,
            data_type_name="ESCO_Motor",
        )
    )
    program = Program(project_id=project.id, name="MainProgram", ordinal=0)
    db.add(program)
    db.flush()
    db.add(
        Routine(
            project_id=project.id,
            program_id=program.id,
            name="Empty_Ladder",
            language=RoutineLanguage.LADDER,
            ordinal=0,
        )
    )
    db.add(
        Finding(
            project_id=project.id,
            title="Interlock has no implementing rung",
            severity=FindingSeverity.CRITICAL,
            status=FindingStatus.OPEN,
            dedupe_key="synthetic-1",
        )
    )
    _validation_run(db, project)
    db.flush()

    gate = _gate(db, project, key)
    assert criterion in _keys(gate)
    assert detail_fragment in _details(gate)


def test_qa_gate_refuses_a_human_decision_over_an_unreviewed_serious_finding(
    db: Session, project: Project
) -> None:
    """A disposition cannot substitute for the independent review that had to precede it."""
    _ready_project(db, project)
    db.add(
        Finding(
            project_id=project.id,
            title="Guard interlock is accepted but not independently reviewed",
            severity=FindingSeverity.HIGH,
            status=FindingStatus.ACCEPTED,
            is_reviewed=False,
            dedupe_key="synthetic-unreviewed-high",
        )
    )
    db.flush()

    gate = _gate(db, project, ApprovalGateKey.QA_AND_RISK)

    assert "serious_findings_senior_reviewed" in _keys(gate)
    assert "independent senior review" in _details(gate)


# ---------------------------------------------------------------------------
# Ordering
# ---------------------------------------------------------------------------
def test_a_gate_cannot_be_decided_while_an_earlier_one_is_undecided(
    db: Session, project: Project, seeded: dict[str, User]
) -> None:
    """Ordering is a product rule, not a UI convention.

    Approving the tag model before anyone has agreed which controller it is for
    is approving a name against a standard nobody chose.
    """
    _ready_project(db, project)

    devices = _gate(db, project, ApprovalGateKey.DEVICES_IO_TAGS)
    assert devices.status is GateStatus.NOT_READY
    assert "earlier_gates_settled" in _keys(devices)
    assert "Project profile and architecture" in _details(devices)

    with pytest.raises(ConflictError):
        gates.request_decision(
            db, project=project, gate=devices, requested_by_agent="tag_architecture"
        )

    blocking = gates.gate_blocking(db, project=project, gate_key=ApprovalGateKey.DEVICES_IO_TAGS)
    assert blocking is not None
    assert blocking.gate_key is ApprovalGateKey.PROJECT_PROFILE

    _pass(db, project, ApprovalGateKey.PROJECT_PROFILE, seeded["reviewer"])
    assert _gate(db, project, ApprovalGateKey.DEVICES_IO_TAGS).status is GateStatus.READY


def test_a_rejected_gate_still_blocks_the_ones_behind_it(
    db: Session, project: Project, seeded: dict[str, User]
) -> None:
    """``FAILED`` is not settled. A gate a reviewer refused is the strongest block there is."""
    _ready_project(db, project)
    rejected = _pass(
        db,
        project,
        ApprovalGateKey.PROJECT_PROFILE,
        seeded["reviewer"],
        decision=ApprovalDecision.REJECT,
        comment="The firmware revision is not the one the customer runs.",
    )

    assert rejected.status is GateStatus.FAILED
    assert _gate(db, project, ApprovalGateKey.DEVICES_IO_TAGS).status is GateStatus.NOT_READY
    blocking = gates.gate_blocking(db, project=project, gate_key=ApprovalGateKey.RELEASE)
    assert blocking is not None
    assert blocking.gate_key is ApprovalGateKey.PROJECT_PROFILE


# ---------------------------------------------------------------------------
# A decision does not survive the thing it was about changing
# ---------------------------------------------------------------------------
def test_a_merge_touching_covered_objects_re_opens_the_gate(
    db: Session, project: Project, seeded: dict[str, User]
) -> None:
    """The rule that stops an approval quietly covering work nobody reviewed.

    Gate 1 is decided in the same breath and must *not* re-open: the merge
    created a tag, which gate 2 covers and gate 1 does not. Re-opening every
    gate on every merge would train reviewers to click through them, which is a
    slower version of the same failure.
    """
    _ready_project(db, project)
    profile_gate = _pass(db, project, ApprovalGateKey.PROJECT_PROFILE, seeded["reviewer"])
    devices_gate = _pass(db, project, ApprovalGateKey.DEVICES_IO_TAGS, seeded["reviewer"])
    decided_at_revision = devices_gate.decided_revision_number
    assert devices_gate.status is GateStatus.PASSED

    _merge_a_tag(db, project, "MTR_102_Cmd_Run")
    # The model moved, so the earlier run no longer describes it.
    _validation_run(db, project)

    reopened = _gate(db, project, ApprovalGateKey.DEVICES_IO_TAGS)
    assert reopened.status is GateStatus.READY
    assert "decision_is_current" in _keys(reopened)
    assert f"decided at revision {decided_at_revision}" in _details(reopened)
    assert "tag" in _details(reopened)
    # The record of what was decided survives; only its currency changed.
    assert reopened.decided_by_id == seeded["reviewer"].id

    assert _gate(db, project, ApprovalGateKey.PROJECT_PROFILE).status is GateStatus.PASSED
    assert profile_gate.decided_revision_number == decided_at_revision


def test_a_merge_touching_nothing_the_gate_covers_leaves_it_passed(
    db: Session, project: Project, seeded: dict[str, User]
) -> None:
    _ready_project(db, project)
    _pass(db, project, ApprovalGateKey.PROJECT_PROFILE, seeded["reviewer"])
    _merge_a_tag(db, project, "MTR_103_Cmd_Run")
    _validation_run(db, project)

    profile_gate = _gate(db, project, ApprovalGateKey.PROJECT_PROFILE)
    assert profile_gate.status is GateStatus.PASSED
    assert "decision_is_current" not in _keys(profile_gate)


def _merge_a_tag(db: Session, project: Project, name: str) -> None:
    """Move the project forward through the real merge, not by editing the row.

    A hand-written revision bump would not write the change operations the
    re-open rule reads, so it would prove nothing about the rule.
    """
    changeset = changesets.build_changeset(
        db,
        project=project,
        payload=ProposeChangesetPayload(
            title="Motor 102 tags",
            summary="Add the run command tag.",
            base_revision_number=project.current_revision_number,
            operations=[
                ChangeOperationProposal(
                    operation_type=ChangeOperationType.CREATE_OBJECT,
                    object_type=ObjectType.TAG,
                    temp_key=name.lower(),
                    values={"name": name, "description": "Run command."},
                    basis=[
                        ObjectBasis(
                            kind=EvidenceKind.TEMPLATE_RULE,
                            template_id=TEMPLATE_ID,
                            note="Standard motor start/stop pattern.",
                        )
                    ],
                    description=f"Create {name}.",
                )
            ],
        ),
        author_agent="tag_architecture",
        allowed_object_types=[ObjectType.TAG],
    )
    changesets.submit(db, changeset)
    result = merge.merge_changeset(db, changeset=changeset)
    assert result.succeeded, result.explanation
    db.flush()


# ---------------------------------------------------------------------------
# Comments
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "decision",
    [ApprovalDecision.REJECT, ApprovalDecision.REQUEST_CHANGES, ApprovalDecision.ACCEPT_RISK],
)
def test_a_decision_that_needs_a_reason_is_refused_without_one(
    db: Session, project: Project, seeded: dict[str, User], decision: ApprovalDecision
) -> None:
    """A rejection nobody explained sends the engineers back with nothing to act on."""
    _ready_project(db, project)
    gate = _gate(db, project, ApprovalGateKey.PROJECT_PROFILE)
    approval = gates.request_decision(
        db, project=project, gate=gate, requested_by_agent="project_manager"
    )

    with pytest.raises(ValidationError, match="requires a comment"):
        gates.record_decision(
            db,
            gate=gate,
            approval=approval,
            decision=decision,
            user=seeded["reviewer"],
            comment="   ",
        )

    assert approval.is_pending
    assert (
        _gate(db, project, ApprovalGateKey.PROJECT_PROFILE).status is GateStatus.AWAITING_DECISION
    )


def test_approving_a_gate_over_serious_findings_requires_a_comment(
    db: Session, project: Project, seeded: dict[str, User]
) -> None:
    """Even a plain approval. What was decided about a critical finding is the record."""
    _ready_project(db, project)
    db.add(
        Finding(
            project_id=project.id,
            title="Guard interlock is not implemented in logic",
            severity=FindingSeverity.HIGH,
            status=FindingStatus.ACCEPTED,
            is_reviewed=True,
            review_verdict="supported",
            dedupe_key="synthetic-high",
        )
    )
    db.flush()
    for key in (
        ApprovalGateKey.PROJECT_PROFILE,
        ApprovalGateKey.DEVICES_IO_TAGS,
        ApprovalGateKey.LOGIC_AND_DELIVERY,
    ):
        _pass(db, project, key, seeded["reviewer"])

    gate = _gate(db, project, ApprovalGateKey.QA_AND_RISK)
    approval = gates.request_decision(
        db, project=project, gate=gate, requested_by_agent="senior_review"
    )
    with pytest.raises(ValidationError, match="high severity"):
        gates.record_decision(
            db,
            gate=gate,
            approval=approval,
            decision=ApprovalDecision.APPROVE,
            user=seeded["reviewer"],
            comment="",
        )

    passed = gates.record_decision(
        db,
        gate=gate,
        approval=approval,
        decision=ApprovalDecision.APPROVE,
        user=seeded["reviewer"],
        comment="The guard interlock is hardwired; the finding is accepted and closed.",
    )
    assert passed.status is GateStatus.PASSED


def test_the_release_gate_always_requires_a_comment(
    db: Session, project: Project, seeded: dict[str, User]
) -> None:
    _ready_project(db, project)
    for key in (
        ApprovalGateKey.PROJECT_PROFILE,
        ApprovalGateKey.DEVICES_IO_TAGS,
        ApprovalGateKey.LOGIC_AND_DELIVERY,
        ApprovalGateKey.QA_AND_RISK,
    ):
        _pass(db, project, key, seeded["reviewer"])

    gate = _gate(db, project, ApprovalGateKey.RELEASE)
    approval = gates.request_decision(db, project=project, gate=gate, requested_by_agent="delivery")
    with pytest.raises(ValidationError, match="covering statement"):
        gates.record_decision(
            db,
            gate=gate,
            approval=approval,
            decision=ApprovalDecision.APPROVE,
            user=seeded["reviewer"],
            comment="",
        )


# ---------------------------------------------------------------------------
# Accepted risk
# ---------------------------------------------------------------------------
def test_accept_risk_passes_the_gate_but_stays_distinguishable_from_approve(
    db: Session, project: Project, seeded: dict[str, User]
) -> None:
    """A release that proceeded on knowingly carried risk must never read as a clean one.

    Both decisions pass the gate, because both let the work continue. Everything
    that records *why* keeps them apart: the approval status, the decision, the
    audit entry and :func:`gates.decision_of`.
    """
    _ready_project(db, project)
    gate = _pass(
        db,
        project,
        ApprovalGateKey.PROJECT_PROFILE,
        seeded["reviewer"],
        decision=ApprovalDecision.ACCEPT_RISK,
        comment="Firmware 35.011 is unconfirmed on site; carried deliberately.",
    )

    assert gate.status is GateStatus.PASSED
    approval = db.get(Approval, gate.approval_id)
    assert approval is not None
    assert approval.status is ApprovalStatus.RISK_ACCEPTED
    assert approval.status is not ApprovalStatus.APPROVED
    assert approval.decision is ApprovalDecision.ACCEPT_RISK
    assert gates.decision_of(db, gate) is ApprovalDecision.ACCEPT_RISK
    assert "carried deliberately" in gate.decision_comment

    entry = db.execute(
        select(AuditEntry).where(
            AuditEntry.action == AuditAction.GATE_DECIDED, AuditEntry.target_id == gate.id
        )
    ).scalar_one()
    assert entry.details["decision"] == ApprovalDecision.ACCEPT_RISK.value
    assert entry.details["approval_status"] == ApprovalStatus.RISK_ACCEPTED.value


def test_request_changes_holds_the_gate_until_a_covered_change_is_merged(
    db: Session, project: Project, seeded: dict[str, User]
) -> None:
    """The worker must not immediately ask the reviewer the identical question again."""
    _ready_project(db, project)
    _pass(db, project, ApprovalGateKey.PROJECT_PROFILE, seeded["reviewer"])
    gate = _pass(
        db,
        project,
        ApprovalGateKey.DEVICES_IO_TAGS,
        seeded["reviewer"],
        decision=ApprovalDecision.REQUEST_CHANGES,
        comment="Add the missing motor command tag before I sign this.",
    )

    assert gate.status is GateStatus.NOT_READY
    assert "requested_changes_not_submitted" in _keys(gate)
    assert gate.decided_revision_number is None
    assert gate.decided_at is None
    approval = db.get(Approval, gate.approval_id)
    assert approval is not None
    assert approval.status is ApprovalStatus.CHANGES_REQUESTED
    assert approval.decided_by_id == seeded["reviewer"].id

    with pytest.raises(ConflictError):
        gates.request_decision(db, project=project, gate=gate, requested_by_agent="project_manager")

    _merge_a_tag(db, project, "MTR_104_Cmd_Run")
    _validation_run(db, project)
    gate = _gate(db, project, ApprovalGateKey.DEVICES_IO_TAGS)
    assert gate.status is GateStatus.READY

    # A covered revision is a real resubmission, so it receives a new request.
    again = gates.request_decision(
        db, project=project, gate=gate, requested_by_agent="project_manager"
    )
    assert again.id != approval.id


def test_defer_holds_the_gate_without_reprompting(
    db: Session, project: Project, seeded: dict[str, User]
) -> None:
    _ready_project(db, project)
    gate = _pass(
        db,
        project,
        ApprovalGateKey.PROJECT_PROFILE,
        seeded["reviewer"],
        decision=ApprovalDecision.DEFER,
        comment="Resume after the customer design review.",
    )

    assert gate.status is GateStatus.NOT_READY
    assert "decision_deferred" in _keys(gate)
    with pytest.raises(ConflictError):
        gates.request_decision(db, project=project, gate=gate, requested_by_agent="project_manager")


# ---------------------------------------------------------------------------
# Gate 5 — the place a product like this lies to itself
# ---------------------------------------------------------------------------
def test_import_verification_is_skipped_with_a_reason_not_passed(
    db: Session, project: Project, seeded: dict[str, User]
) -> None:
    """Without a licensed workstation there is nothing to verify, and saying so is the product.

    ``SKIPPED`` and a stated reason; never ``PASSED``. A gate that reported a
    pass here would put "import verified" in a manifest on the strength of our
    own validators liking our own output.
    """
    _ready_project(db, project)
    for key in (
        ApprovalGateKey.PROJECT_PROFILE,
        ApprovalGateKey.DEVICES_IO_TAGS,
        ApprovalGateKey.LOGIC_AND_DELIVERY,
        ApprovalGateKey.QA_AND_RISK,
    ):
        _pass(db, project, key, seeded["reviewer"])

    gate = _gate(db, project, ApprovalGateKey.IMPORT_VERIFICATION)
    assert gate.status is GateStatus.SKIPPED
    assert gate.status is not GateStatus.PASSED
    assert "gate_unreachable" in _keys(gate)
    assert "import_package_only" in _details(gate)
    assert "import-ready candidate" in _details(gate)
    # Skipped is settled, so it does not hold the release gate up.
    assert not gate.blocks_progress

    with pytest.raises(ConflictError):
        gates.request_decision(db, project=project, gate=gate, requested_by_agent="delivery")


def test_import_verification_refuses_approval_when_no_workstation_ran_it(
    db: Session, project: Project, seeded: dict[str, User]
) -> None:
    """Even with the gate deliberately put in front of a reviewer, approval is refused.

    The project is scoped to workstation assembly, so the gate is reachable in
    principle — but no job ever ran, and the criterion reads the evidence rather
    than a configuration flag. Approving here would be the platform claiming a
    Studio 5000 import that did not happen.
    """
    _ready_project(db, project)
    profile = db.execute(
        select(ProjectProfile).where(ProjectProfile.project_id == project.id)
    ).scalar_one()
    profile.release_mode = ReleaseMode.ROCKWELL_WORKSTATION_ASSEMBLY
    db.flush()

    for key in (
        ApprovalGateKey.PROJECT_PROFILE,
        ApprovalGateKey.DEVICES_IO_TAGS,
        ApprovalGateKey.LOGIC_AND_DELIVERY,
        ApprovalGateKey.QA_AND_RISK,
    ):
        _pass(db, project, key, seeded["reviewer"])

    gate = _gate(db, project, ApprovalGateKey.IMPORT_VERIFICATION)
    assert gate.status is GateStatus.SKIPPED
    assert "licensed Rockwell workstation adapter" in _details(gate)

    # Force the gate in front of a reviewer anyway, the way a wired-up UI or a
    # future adapter integration might, and check the refusal is in the recorder
    # rather than only in the evaluator.
    approval = Approval(
        project_id=project.id,
        gate_key=gate.gate_key,
        kind=ApprovalKind.GATE_DECISION,
        status=ApprovalStatus.PENDING,
        revision_number=project.current_revision_number,
        title="Approval gate: Import verification",
        question="Forced for the test.",
    )
    db.add(approval)
    db.flush()
    gate.approval_id = approval.id
    gate.status = GateStatus.AWAITING_DECISION
    db.flush()

    with pytest.raises(ConflictError, match="no longer current"):
        gates.record_decision(
            db,
            gate=gate,
            approval=approval,
            decision=ApprovalDecision.APPROVE,
            user=seeded["reviewer"],
            comment="Looks fine to me.",
        )
    assert approval.status is ApprovalStatus.CANCELLED
    assert "criteria no longer hold" in approval.decision_notes
    assert gate.approval_id is None
    assert gate.status is GateStatus.SKIPPED


def test_import_verification_passes_only_on_a_real_verified_import(
    db: Session, project: Project, seeded: dict[str, User]
) -> None:
    """The one route to a pass: a job that ran, passed, and raised the candidate's level."""
    _ready_project(db, project)
    profile = db.execute(
        select(ProjectProfile).where(ProjectProfile.project_id == project.id)
    ).scalar_one()
    profile.release_mode = ReleaseMode.ROCKWELL_WORKSTATION_ASSEMBLY
    candidate = db.execute(
        select(ReleaseCandidate).where(ReleaseCandidate.project_id == project.id)
    ).scalar_one()
    candidate.import_verification_status = ImportVerificationStatus.PASSED
    candidate.validation_level = ValidationLevel.ROCKWELL_IMPORT_VERIFIED
    db.add(
        ExportArtifact(
            project_id=project.id,
            release_candidate_id=candidate.id,
            kind=ExportArtifactKind.CONTROLLER_PROJECT_L5X,
            relative_path="01_Controller/Line4.L5X",
            filename="Line4.L5X",
            sha256="c" * 64,
        )
    )
    db.add(
        WorkstationVerificationJob(
            project_id=project.id,
            release_candidate_id=candidate.id,
            adapter_key="bench-1",
            status=ImportVerificationStatus.PASSED,
        )
    )
    db.flush()

    for key in (
        ApprovalGateKey.PROJECT_PROFILE,
        ApprovalGateKey.DEVICES_IO_TAGS,
        ApprovalGateKey.LOGIC_AND_DELIVERY,
        ApprovalGateKey.QA_AND_RISK,
    ):
        _pass(db, project, key, seeded["reviewer"])

    gate = _gate(db, project, ApprovalGateKey.IMPORT_VERIFICATION)
    assert gate.status is GateStatus.READY, _details(gate)
    passed = _pass(db, project, ApprovalGateKey.IMPORT_VERIFICATION, seeded["reviewer"])
    assert passed.status is GateStatus.PASSED


def test_import_verification_reports_a_package_built_for_a_different_version(
    db: Session, project: Project, seeded: dict[str, User]
) -> None:
    """A candidate built against one firmware and released against another will not import."""
    _ready_project(db, project)
    candidate = db.execute(
        select(ReleaseCandidate).where(ReleaseCandidate.project_id == project.id)
    ).scalar_one()
    candidate.target_profile_snapshot = {
        "controller_catalog_number": CATALOG_NUMBER,
        "firmware_revision": "33.011",
        "studio_5000_version": STUDIO_VERSION,
    }
    db.flush()

    gate = _gate(db, project, ApprovalGateKey.IMPORT_VERIFICATION)
    assert "target_version_verified" in _keys(gate)
    assert "firmware revision" in _details(gate)


# ---------------------------------------------------------------------------
# Disabling
# ---------------------------------------------------------------------------
def test_a_disabled_gate_is_recorded_with_its_reason_and_stops_blocking(
    db: Session, project: Project, seeded: dict[str, User]
) -> None:
    """ "Nobody reviewed the tag model" has to stay visible in the release record."""
    _ready_project(db, project)
    _pass(db, project, ApprovalGateKey.PROJECT_PROFILE, seeded["reviewer"])

    gate = _gate(db, project, ApprovalGateKey.DEVICES_IO_TAGS)
    disabled = gates.set_gate_enabled(
        db,
        project=project,
        gate=gate,
        is_enabled=False,
        reason="The customer's own tag standard applies and their engineer owns it.",
        user=seeded["admin"],
    )

    assert disabled.status is GateStatus.SKIPPED
    assert not disabled.is_enabled
    assert "customer's own tag standard" in disabled.disabled_reason
    assert "customer's own tag standard" in _details(disabled)
    assert not disabled.blocks_progress
    assert _gate(db, project, ApprovalGateKey.LOGIC_AND_DELIVERY).status is GateStatus.READY

    entry = db.execute(
        select(AuditEntry).where(
            AuditEntry.action == AuditAction.GATE_DECIDED, AuditEntry.target_id == gate.id
        )
    ).scalar_one()
    assert entry.details["reason"].startswith("The customer's own tag standard")
    status_events = db.execute(
        select(Event)
        .where(
            Event.project_id == project.id,
            Event.event_type == EventType.GATE_STATUS_CHANGED,
        )
        .order_by(Event.seq.desc())
    ).scalars()
    status_event = next(
        (
            event
            for event in status_events
            if event.payload.get("gate_key") == ApprovalGateKey.DEVICES_IO_TAGS.value
        ),
        None,
    )
    assert status_event is not None
    assert status_event.payload["gate_key"] == ApprovalGateKey.DEVICES_IO_TAGS.value
    assert status_event.payload["status"] == GateStatus.SKIPPED.value
    assert status_event.actor_user_id == seeded["admin"].id


def test_gate_enablement_rejects_a_gate_bound_to_another_project(
    db: Session, project: Project, seeded: dict[str, User]
) -> None:
    gate = _gate(db, project, ApprovalGateKey.DEVICES_IO_TAGS)
    other = Project(
        name="Another project",
        description="Must not borrow another project's gate.",
        client_reference="OTHER",
        created_by_id=seeded["engineer"].id,
    )
    db.add(other)
    db.commit()

    with pytest.raises(ConflictError, match="does not belong"):
        gates.set_gate_enabled(
            db,
            project=other,
            gate=gate,
            is_enabled=False,
            reason="This reason must never land.",
            user=seeded["admin"],
        )
    db.rollback()

    db.expire_all()
    assert db.get(ApprovalGate, gate.id).is_enabled is True  # type: ignore[union-attr]


def test_disabling_a_gate_without_a_reason_is_refused(
    db: Session, project: Project, seeded: dict[str, User]
) -> None:
    """The reason is the whole feature. An unexplained skip cannot be defended later."""
    gate = _gate(db, project, ApprovalGateKey.DEVICES_IO_TAGS)
    with pytest.raises(ValidationError, match="requires a stated reason"):
        gates.set_gate_enabled(
            db,
            project=project,
            gate=gate,
            is_enabled=False,
            reason="  ",
            user=seeded["admin"],
        )
    assert _gate(db, project, ApprovalGateKey.DEVICES_IO_TAGS).is_enabled


def test_final_release_gate_cannot_be_disabled(
    db: Session, project: Project, seeded: dict[str, User]
) -> None:
    gate = _gate(db, project, ApprovalGateKey.RELEASE)

    with pytest.raises(ValidationError, match="cannot be disabled"):
        gates.set_gate_enabled(
            db,
            project=project,
            gate=gate,
            is_enabled=False,
            reason="Site integrator will sign later.",
            user=seeded["admin"],
        )

    assert gate.is_enabled


def test_only_an_administrator_may_disable_a_gate(
    db: Session, project: Project, seeded: dict[str, User]
) -> None:
    """Removing a review step is not for the people whose work it reviews."""
    gate = _gate(db, project, ApprovalGateKey.DEVICES_IO_TAGS)
    for role in ("engineer", "reviewer"):
        with pytest.raises(PermissionDeniedError):
            gates.set_gate_enabled(
                db,
                project=project,
                gate=gate,
                is_enabled=False,
                reason="It is inconvenient.",
                user=seeded[role],
            )
    assert _gate(db, project, ApprovalGateKey.DEVICES_IO_TAGS).is_enabled


def test_a_disabled_gate_with_no_reason_holds_the_release_gate(
    db: Session, project: Project, seeded: dict[str, User]
) -> None:
    """Belt and braces: even a row disabled by hand cannot slip past release."""
    _ready_project(db, project)
    for key in (
        ApprovalGateKey.PROJECT_PROFILE,
        ApprovalGateKey.LOGIC_AND_DELIVERY,
        ApprovalGateKey.QA_AND_RISK,
    ):
        gates.set_gate_enabled(
            db,
            project=project,
            gate=_gate(db, project, key),
            is_enabled=False,
            reason="Out of scope for this engagement.",
            user=seeded["admin"],
        )
    silent = _gate(db, project, ApprovalGateKey.DEVICES_IO_TAGS)
    silent.is_enabled = False
    silent.disabled_reason = ""
    db.flush()

    release = _gate(db, project, ApprovalGateKey.RELEASE)
    assert "skipped_gates_state_a_reason" in _keys(release)
    assert release.status is GateStatus.NOT_READY


# ---------------------------------------------------------------------------
# Who may decide
# ---------------------------------------------------------------------------
def test_only_a_reviewer_may_record_a_decision(
    db: Session, project: Project, seeded: dict[str, User]
) -> None:
    """An engineer signing off their own project's work is the separation the role exists for."""
    _ready_project(db, project)
    gate = _gate(db, project, ApprovalGateKey.PROJECT_PROFILE)
    approval = gates.request_decision(
        db, project=project, gate=gate, requested_by_agent="project_manager"
    )

    with pytest.raises(PermissionDeniedError, match="reviewer role"):
        gates.record_decision(
            db,
            gate=gate,
            approval=approval,
            decision=ApprovalDecision.APPROVE,
            user=seeded["engineer"],
            comment="",
        )
    with pytest.raises(PermissionDeniedError):
        gates.record_decision(
            db,
            gate=gate,
            approval=approval,
            decision=ApprovalDecision.APPROVE,
            user=seeded["outsider"],
            comment="",
        )
    assert approval.is_pending


def test_requesting_a_decision_twice_yields_one_question(db: Session, project: Project) -> None:
    """The task queue is at-least-once; a re-run must not queue a reviewer twice."""
    _ready_project(db, project)
    gate = _gate(db, project, ApprovalGateKey.PROJECT_PROFILE)
    first = gates.request_decision(
        db, project=project, gate=gate, requested_by_agent="project_manager"
    )
    second = gates.request_decision(
        db, project=project, gate=gate, requested_by_agent="project_manager"
    )

    assert first.id == second.id
    assert (
        len(
            db.execute(
                select(Approval).where(
                    Approval.project_id == project.id,
                    Approval.kind == ApprovalKind.GATE_DECISION,
                )
            )
            .scalars()
            .all()
        )
        == 1
    )


def test_a_preloaded_session_cannot_overwrite_a_concurrent_gate_decision(
    db: Session, project: Project, seeded: dict[str, User]
) -> None:
    _ready_project(db, project)
    gate = _gate(db, project, ApprovalGateKey.PROJECT_PROFILE)
    approval = gates.request_decision(
        db, project=project, gate=gate, requested_by_agent="project_manager"
    )
    db.commit()

    factory = get_session_factory()
    stale = factory()
    winner = factory()
    try:
        stale_gate = stale.get(ApprovalGate, gate.id)
        stale_approval = stale.get(Approval, approval.id)
        stale_reviewer = stale.get(User, seeded["reviewer"].id)
        winner_gate = winner.get(ApprovalGate, gate.id)
        winner_approval = winner.get(Approval, approval.id)
        winner_reviewer = winner.get(User, seeded["reviewer"].id)
        assert stale_gate is not None and stale_approval is not None and stale_reviewer is not None
        assert winner_gate is not None and winner_approval is not None
        assert winner_reviewer is not None

        gates.record_decision(
            winner,
            gate=winner_gate,
            approval=winner_approval,
            decision=ApprovalDecision.APPROVE,
            user=winner_reviewer,
            comment="The first committed reviewer decision is authoritative.",
        )
        winner.commit()

        with pytest.raises(ConflictError, match="already approved"):
            gates.record_decision(
                stale,
                gate=stale_gate,
                approval=stale_approval,
                decision=ApprovalDecision.REJECT,
                user=stale_reviewer,
                comment="A stale session must not replace the first decision.",
            )
        stale.rollback()
    finally:
        stale.close()
        winner.close()

    db.expire_all()
    stored = db.get(Approval, approval.id)
    assert stored is not None
    assert stored.status is ApprovalStatus.APPROVED
    assert stored.decision is ApprovalDecision.APPROVE
    assert stored.decision_notes == "The first committed reviewer decision is authoritative."


def test_request_decision_refreshes_the_project_after_a_concurrent_revision_advance(
    db: Session, project: Project
) -> None:
    _ready_project(db, project)
    gate = _gate(db, project, ApprovalGateKey.PROJECT_PROFILE)
    db.commit()

    factory = get_session_factory()
    stale = factory()
    revision_writer = factory()
    try:
        stale_project = stale.get(Project, project.id)
        stale_gate = stale.get(ApprovalGate, gate.id)
        assert stale_project is not None and stale_gate is not None
        old_revision = stale_project.current_revision_number

        revision_writer.execute(
            update(Project)
            .where(Project.id == project.id)
            .values(current_revision_number=Project.current_revision_number + 1)
        )
        revision_writer.commit()

        approval = gates.request_decision(
            stale,
            project=stale_project,
            gate=stale_gate,
            requested_by_agent="project_manager",
        )
        assert stale_project.current_revision_number == old_revision + 1
        assert approval.revision_number == old_revision + 1
        stale.commit()
    finally:
        stale.close()
        revision_writer.close()

    db.expire_all()
    stored = db.scalar(
        select(Approval).where(
            Approval.project_id == project.id,
            Approval.kind == ApprovalKind.GATE_DECISION,
            Approval.status == ApprovalStatus.PENDING,
        )
    )
    assert stored is not None
    assert stored.revision_number == old_revision + 1


def test_record_decision_rechecks_a_reviewer_deactivated_after_page_load(
    db: Session, project: Project, seeded: dict[str, User]
) -> None:
    _ready_project(db, project)
    gate = _gate(db, project, ApprovalGateKey.PROJECT_PROFILE)
    approval = gates.request_decision(
        db, project=project, gate=gate, requested_by_agent="project_manager"
    )
    db.commit()

    factory = get_session_factory()
    stale = factory()
    authority_writer = factory()
    try:
        stale_gate = stale.get(ApprovalGate, gate.id)
        stale_approval = stale.get(Approval, approval.id)
        stale_reviewer = stale.get(User, seeded["reviewer"].id)
        assert stale_gate is not None and stale_approval is not None and stale_reviewer is not None

        authority_writer.execute(
            update(User).where(User.id == seeded["reviewer"].id).values(is_active=False)
        )
        authority_writer.commit()

        with pytest.raises(PermissionDeniedError, match="deactivated"):
            gates.record_decision(
                stale,
                gate=stale_gate,
                approval=stale_approval,
                decision=ApprovalDecision.APPROVE,
                user=stale_reviewer,
                comment="This stale authority must be refused.",
            )
        stale.rollback()
    finally:
        stale.close()
        authority_writer.close()

    db.expire_all()
    stored = db.get(Approval, approval.id)
    assert stored is not None and stored.status is ApprovalStatus.PENDING


def test_duplicate_pending_gate_requests_converge_to_one(db: Session, project: Project) -> None:
    _ready_project(db, project)
    gate = _gate(db, project, ApprovalGateKey.PROJECT_PROFILE)
    first = gates.request_decision(
        db, project=project, gate=gate, requested_by_agent="project_manager"
    )
    duplicate = Approval(
        project_id=project.id,
        gate_key=gate.gate_key,
        kind=ApprovalKind.GATE_DECISION,
        status=ApprovalStatus.PENDING,
        revision_number=project.current_revision_number,
        title=first.title,
        question=first.question,
    )
    db.add(duplicate)
    db.flush()

    converged = gates.request_decision(
        db, project=project, gate=gate, requested_by_agent="project_manager"
    )

    assert converged.id == first.id
    assert duplicate.status is ApprovalStatus.CANCELLED
    assert "duplicate pending request" in duplicate.decision_notes
    pending = list(
        db.execute(
            select(Approval).where(
                Approval.project_id == project.id,
                Approval.gate_key == gate.gate_key,
                Approval.status == ApprovalStatus.PENDING,
            )
        ).scalars()
    )
    assert [row.id for row in pending] == [first.id]


def test_pending_decision_is_cancelled_when_the_project_revision_moves(
    db: Session, project: Project
) -> None:
    _ready_project(db, project)
    gate = _gate(db, project, ApprovalGateKey.PROJECT_PROFILE)
    approval = gates.request_decision(
        db, project=project, gate=gate, requested_by_agent="project_manager"
    )

    _merge_a_tag(db, project, "MTR_105_Cmd_Run")
    _validation_run(db, project)
    refreshed = _gate(db, project, ApprovalGateKey.PROJECT_PROFILE)

    assert approval.status is ApprovalStatus.CANCELLED
    assert "project is now at revision" in approval.decision_notes
    assert refreshed.approval_id is None
    assert refreshed.status is GateStatus.READY


def test_release_decision_is_bound_to_the_exact_current_candidate(
    db: Session, project: Project, seeded: dict[str, User]
) -> None:
    _ready_project(db, project)
    for key in (
        ApprovalGateKey.PROJECT_PROFILE,
        ApprovalGateKey.DEVICES_IO_TAGS,
        ApprovalGateKey.LOGIC_AND_DELIVERY,
        ApprovalGateKey.QA_AND_RISK,
    ):
        _pass(db, project, key, seeded["reviewer"])

    gate = _gate(db, project, ApprovalGateKey.RELEASE)
    first = gates.request_decision(db, project=project, gate=gate, requested_by_agent="delivery")
    first_candidate = (
        db.execute(
            select(ReleaseCandidate)
            .where(ReleaseCandidate.project_id == project.id)
            .order_by(ReleaseCandidate.candidate_number.desc())
        )
        .scalars()
        .first()
    )
    assert first_candidate is not None
    assert first.release_candidate_id == first_candidate.id

    second_candidate = _release(db, project, candidate_number=2, name="RC 2")
    refreshed = _gate(db, project, ApprovalGateKey.RELEASE)
    assert first.status is ApprovalStatus.CANCELLED
    assert refreshed.status is GateStatus.READY

    second = gates.request_decision(
        db, project=project, gate=refreshed, requested_by_agent="delivery"
    )
    assert second.release_candidate_id == second_candidate.id


def test_a_decision_cannot_be_recorded_against_another_gates_approval(
    db: Session, project: Project, seeded: dict[str, User]
) -> None:
    _ready_project(db, project)
    profile_gate = _gate(db, project, ApprovalGateKey.PROJECT_PROFILE)
    approval = gates.request_decision(
        db, project=project, gate=profile_gate, requested_by_agent="project_manager"
    )
    devices_gate = _gate(db, project, ApprovalGateKey.DEVICES_IO_TAGS)

    with pytest.raises(ConflictError, match="not the decision this gate is waiting for"):
        gates.record_decision(
            db,
            gate=devices_gate,
            approval=approval,
            decision=ApprovalDecision.APPROVE,
            user=seeded["reviewer"],
            comment="",
        )


# ---------------------------------------------------------------------------
# What the city is told
# ---------------------------------------------------------------------------
def test_human_gate_events_name_the_actor_but_an_automated_reopen_does_not(
    db: Session, project: Project, seeded: dict[str, User]
) -> None:
    _ready_project(db, project)
    _pass(db, project, ApprovalGateKey.PROJECT_PROFILE, seeded["reviewer"])
    _pass(db, project, ApprovalGateKey.DEVICES_IO_TAGS, seeded["reviewer"])
    db.commit()

    _merge_a_tag(db, project, "MTR_106_Cmd_Run")
    gates.evaluate_all(db, project=project)
    db.commit()

    records = list(
        db.execute(
            select(Event)
            .where(
                Event.project_id == project.id,
                Event.event_type == EventType.GATE_STATUS_CHANGED,
            )
            .order_by(Event.seq)
        ).scalars()
    )
    device_events = [
        record
        for record in records
        if record.payload["gate_key"] == ApprovalGateKey.DEVICES_IO_TAGS.value
    ]
    passed = next(
        record for record in device_events if record.payload["status"] == GateStatus.PASSED.value
    )
    reopened = device_events[-1]
    assert passed.actor_user_id == seeded["reviewer"].id
    assert reopened.payload["status"] == GateStatus.NOT_READY.value
    assert reopened.actor_user_id is None
    assert reopened.payload["decided_by_id"] == str(seeded["reviewer"].id)


def test_a_status_change_broadcasts_the_unmet_criteria_in_its_published_shape(
    db: Session, project: Project, seeded: dict[str, User]
) -> None:
    """A UI told only "not ready" has nothing to render.

    The payload is validated against the published model, because an event the
    frontend cannot type is a contract hole rather than a nicety.
    """
    _ready_project(db, project)
    approved_at_revision = project.current_revision_number
    _pass(db, project, ApprovalGateKey.PROJECT_PROFILE, seeded["reviewer"])
    # A merge with no fresh validation run behind it: gate 2 falls back to
    # not-ready, which is the transition a UI most needs explained.
    _merge_a_tag(db, project, "MTR_104_Cmd_Run")
    gates.evaluate_all(db, project=project)
    db.commit()

    records = list(
        db.execute(
            select(Event).where(
                Event.project_id == project.id,
                Event.event_type == EventType.GATE_STATUS_CHANGED,
            )
        ).scalars()
    )
    assert records, "no gate status change was broadcast"
    for record in records:
        GateStatusChangedEventPayload.model_validate(record.payload)

    by_gate: dict[str, list[Event]] = {}
    for record in records:
        by_gate.setdefault(str(record.payload["gate_key"]), []).append(record)

    unlocked = by_gate[ApprovalGateKey.DEVICES_IO_TAGS.value]
    assert [item.payload["status"] for item in unlocked] == [
        GateStatus.READY.value,
        GateStatus.NOT_READY.value,
    ]
    reverted = unlocked[-1]
    assert reverted.payload["unmet_criteria"], "a not-ready gate must say what is missing"
    assert reverted.payload["unmet_criteria_count"] == len(reverted.payload["unmet_criteria"])
    assert "validation run" in " ".join(reverted.payload["unmet_criteria"])

    passed = next(
        record for record in records if record.payload["status"] == GateStatus.PASSED.value
    )
    assert passed.payload["decided_revision_number"] == approved_at_revision
    assert passed.payload["decided_by_id"] == str(seeded["reviewer"].id)


# ---------------------------------------------------------------------------
# Criteria that read the rest of the model
# ---------------------------------------------------------------------------
def test_proposed_requirements_hold_the_profile_gate_for_a_human_decision(
    db: Session, project: Project
) -> None:
    _profile(db, project)
    _platform(db, project)
    _requirement(db, project, status=RequirementStatus.PROPOSED, reference="REQ-014")

    gate = _gate(db, project, ApprovalGateKey.PROJECT_PROFILE)

    assert "requirements_human_decided" in _keys(gate)
    assert "REQ-014" in _details(gate)
    assert "Accept or reject" in _details(gate)


def test_generation_requires_at_least_one_human_accepted_requirement(
    db: Session, project: Project
) -> None:
    _profile(db, project)
    _platform(db, project)
    _requirement(db, project, status=RequirementStatus.REJECTED)

    gate = _gate(db, project, ApprovalGateKey.PROJECT_PROFILE)

    assert "requirements_human_decided" not in _keys(gate)
    assert "accepted_requirement_for_generation" in _keys(gate)
    assert "at least one requirement accepted by a human" in _details(gate)


def test_review_only_profile_does_not_invent_an_accepted_requirement(
    db: Session, project: Project
) -> None:
    _profile(db, project, release_mode=ReleaseMode.REVIEW_ONLY)
    _platform(db, project)
    _requirement(db, project, status=RequirementStatus.REJECTED)

    gate = _gate(db, project, ApprovalGateKey.PROJECT_PROFILE)

    assert "requirements_human_decided" not in _keys(gate)
    assert "accepted_requirement_for_generation" not in _keys(gate)
    assert gate.status is GateStatus.READY


def test_a_restricted_domain_without_a_qualified_workflow_holds_the_profile_gate(
    db: Session, project: Project
) -> None:
    """Rule 8 made visible at the gate rather than only at generation time."""
    _profile(
        db,
        project,
        restricted_domains=[RestrictedDomain.SAFETY_LOGIC.value],
        restricted_domain_mode=RestrictedDomainMode.MANUAL_REVIEW_ONLY,
    )
    _platform(db, project)

    gate = _gate(db, project, ApprovalGateKey.PROJECT_PROFILE)
    assert "generation_scope_settled" in _keys(gate)
    assert "qualified workflow" in _details(gate)


def test_an_unanswered_mandatory_question_holds_the_profile_gate(
    db: Session, project: Project
) -> None:
    """The refusal-to-guess rule: an open question is a stop, not a warning."""
    _profile(db, project)
    _platform(db, project)
    db.add(
        ProjectQuestion(
            project_id=project.id,
            reference="Q-001",
            question="Which firmware revision is actually running on site?",
            is_mandatory=True,
        )
    )
    db.flush()

    gate = _gate(db, project, ApprovalGateKey.PROJECT_PROFILE)
    assert "blocking_questions_answered" in _keys(gate)
    assert "Q-001" in _details(gate)


def test_a_stale_validation_run_is_not_a_clean_one(db: Session, project: Project) -> None:
    """ "We did not check" and "we checked and it was fine" must not look alike."""
    _ready_project(db, project)
    run = db.execute(
        select(ValidationRun).where(ValidationRun.project_id == project.id)
    ).scalar_one()
    run.revision_number = project.current_revision_number - 1
    db.flush()

    gate = _gate(db, project, ApprovalGateKey.DEVICES_IO_TAGS)
    assert "tag_and_io_validation_current" in _keys(gate)
    assert "no_open_tag_or_io_errors" not in _keys(gate)


def test_equal_time_current_validation_runs_reopen_qa_and_release_fail_closed(
    db: Session, project: Project, seeded: dict[str, User]
) -> None:
    """A UUID may make query output stable, but it cannot decide which run superseded which."""
    _ready_project(db, project)
    reviewer = seeded["reviewer"]
    for key in (
        ApprovalGateKey.PROJECT_PROFILE,
        ApprovalGateKey.DEVICES_IO_TAGS,
        ApprovalGateKey.LOGIC_AND_DELIVERY,
        ApprovalGateKey.QA_AND_RISK,
    ):
        _pass(db, project, key, reviewer)
    _pass(
        db,
        project,
        ApprovalGateKey.RELEASE,
        reviewer,
        comment="Released after reviewing the complete current validation record.",
    )

    first = _validation_run(db, project)
    second = _validation_run(db, project, errors=1)
    tied_at = utcnow() + timedelta(days=1)
    first.created_at = tied_at
    second.created_at = tied_at
    db.flush()

    evaluated = {gate.gate_key: gate for gate in gates.evaluate_all(db, project=project)}
    qa = evaluated[ApprovalGateKey.QA_AND_RISK]
    release_gate = evaluated[ApprovalGateKey.RELEASE]

    assert qa.status is GateStatus.NOT_READY
    assert "full_validation_current" in _keys(qa)
    assert "same recorded creation time" in _details(qa)
    assert release_gate.status is GateStatus.NOT_READY
    assert "no_errors_at_release" in _keys(release_gate)
    assert "same recorded creation time" in _details(release_gate)
    context = gates.GateContext(db=db, project=project)
    assert context.validation_run_is_ambiguous is True
    assert context.validation_run is None


def test_equal_time_older_runs_do_not_hide_a_unique_current_run(
    db: Session, project: Project
) -> None:
    """Revision order outranks timestamps; only an equal-time current head is ambiguous."""
    project.current_revision_number = 2
    current = _validation_run(db, project, revision=2)
    older_first = _validation_run(db, project, revision=1)
    older_second = _validation_run(db, project, revision=1)
    tied_at = utcnow() + timedelta(days=1)
    older_first.created_at = tied_at
    older_second.created_at = tied_at
    db.flush()

    context = gates.GateContext(db=db, project=project)
    assert context.validation_run_is_ambiguous is False
    assert context.validation_run is current


def test_a_future_revision_validation_run_reopens_qa_and_release_fail_closed(
    db: Session, project: Project, seeded: dict[str, User]
) -> None:
    """A malformed future claim must not displace current validation by revision ordering."""
    _ready_project(db, project)
    reviewer = seeded["reviewer"]
    for key in (
        ApprovalGateKey.PROJECT_PROFILE,
        ApprovalGateKey.DEVICES_IO_TAGS,
        ApprovalGateKey.LOGIC_AND_DELIVERY,
        ApprovalGateKey.QA_AND_RISK,
    ):
        _pass(db, project, key, reviewer)
    _pass(
        db,
        project,
        ApprovalGateKey.RELEASE,
        reviewer,
        comment="Released after reviewing the current validation record.",
    )

    _validation_run(db, project, revision=project.current_revision_number + 1)
    db.flush()

    evaluated = {gate.gate_key: gate for gate in gates.evaluate_all(db, project=project)}
    qa = evaluated[ApprovalGateKey.QA_AND_RISK]
    release_gate = evaluated[ApprovalGateKey.RELEASE]
    context = gates.GateContext(db=db, project=project)

    assert context.validation_run_is_from_future_revision is True
    assert context.validation_run is None
    assert context.run_covers(ALL_DISCIPLINES) is False
    assert qa.status is GateStatus.NOT_READY
    assert "full_validation_current" in _keys(qa)
    assert "claims future revision" in _details(qa)
    assert release_gate.status is GateStatus.NOT_READY
    assert "no_errors_at_release" in _keys(release_gate)
    assert "claims future revision" in _details(release_gate)


def test_every_discipline_a_gate_asks_for_is_one_a_validator_covers(db: Session) -> None:
    """A gate that waits for a discipline nothing checks would never pass.

    It would report "the run did not cover X" forever, and no amount of work by
    the engineering team could close it. The failure is invisible until somebody
    tries to release, so it is asserted here rather than discovered there.
    """
    asked = set(gates._TAG_AND_IO_DISCIPLINES) | set(gates._LOGIC_AND_DELIVERY_DISCIPLINES)
    uncovered = sorted(
        discipline.value for discipline in asked if discipline not in set(ALL_DISCIPLINES)
    )
    assert uncovered == [], f"no validator reports against {uncovered}"


def test_an_unwaived_error_holds_the_gate_that_covers_it(db: Session, project: Project) -> None:
    _ready_project(db, project)
    run = db.execute(
        select(ValidationRun).where(ValidationRun.project_id == project.id)
    ).scalar_one()
    db.delete(run)
    _validation_run(db, project, errors=2)

    gate = _gate(db, project, ApprovalGateKey.DEVICES_IO_TAGS)
    assert "no_open_tag_or_io_errors" in _keys(gate)
    assert "2 unwaived error(s)" in _details(gate)


def test_a_proposed_safety_recommendation_holds_the_qa_gate(
    db: Session, project: Project, seeded: dict[str, User]
) -> None:
    """Rule 5: a safety recommendation needs a named human, not a gate that ignores it."""
    _ready_project(db, project)
    db.add(
        Recommendation(
            project_id=project.id,
            title="Add a hardwired guard interlock to the drive enable",
            safety_related=True,
            status=RecommendationStatus.PROPOSED,
        )
    )
    db.flush()
    for key in (
        ApprovalGateKey.PROJECT_PROFILE,
        ApprovalGateKey.DEVICES_IO_TAGS,
        ApprovalGateKey.LOGIC_AND_DELIVERY,
    ):
        _pass(db, project, key, seeded["reviewer"])

    gate = _gate(db, project, ApprovalGateKey.QA_AND_RISK)
    assert gate.status is GateStatus.NOT_READY
    assert "safety_recommendations_decided" in _keys(gate)
    assert "hardwired guard interlock" in _details(gate)


def test_faults_without_alarms_hold_the_logic_gate(db: Session, project: Project) -> None:
    """A fault nobody annunciates is a machine that stops for no stated reason."""
    _ready_project(db, project)
    db.add(
        FaultDefinition(
            project_id=project.id,
            reference="FLT-001",
            name="Drive fault",
            fault_class=FaultClass.EQUIPMENT,
            description="The VFD reported a fault.",
        )
    )
    db.flush()

    gate = _gate(db, project, ApprovalGateKey.LOGIC_AND_DELIVERY)
    assert "alarm_model_recorded" in _keys(gate)

    db.add(
        AlarmDefinition(
            project_id=project.id,
            reference="ALM-001",
            name="Drive fault",
            message_key="alm.drive_fault",
            severity=AlarmSeverity.HIGH,
            ordinal=0,
        )
    )
    db.flush()
    assert "alarm_model_recorded" not in _keys(
        _gate(db, project, ApprovalGateKey.LOGIC_AND_DELIVERY)
    )


def test_a_criterion_that_raises_is_reported_as_unmet_rather_than_swallowed(
    db: Session, project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A gate that passed because the check that would have blocked it crashed is the worst outcome.

    It looks exactly like a clean pass, so it gets believed.
    """

    def _explode(context: gates.GateContext) -> str:
        raise RuntimeError("the tag table is on fire")

    spec = gates.spec_for(ApprovalGateKey.PROJECT_PROFILE)
    broken = replace(
        spec,
        criteria=(
            gates.Criterion("profile_recorded", spec.criteria[0].statement, _explode),
            *spec.criteria[1:],
        ),
    )
    monkeypatch.setitem(gates._SPECS_BY_KEY, ApprovalGateKey.PROJECT_PROFILE, broken)

    _ready_project(db, project)
    gate = _gate(db, project, ApprovalGateKey.PROJECT_PROFILE)
    assert gate.status is GateStatus.NOT_READY
    assert "profile_recorded" in _keys(gate)
    assert "RuntimeError" in _details(gate)
