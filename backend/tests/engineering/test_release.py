"""The release service: what it produces, and what it refuses to produce.

Every test here is a product rule rather than a code path, and the refusals are
the point. A release pipeline that generates whenever it is asked is not a
pipeline, it is a button — the value is in the six things it declines to do and
in the sentence each refusal gives the person holding it.

Four assertions carry more weight than the rest:

* a package regenerated from the same revision is byte-identical to the first,
  because that is what makes a checksum in a manifest worth recording;
* the candidate's level is the weakest of its artifacts, never the strongest;
* with no licensed workstation the import status is ``UNAVAILABLE`` with a
  reason, never ``NOT_ATTEMPTED`` dressed up as fine;
* a refused generation leaves nothing on disk and names the offending object.

The validation runs are written directly rather than produced by
:func:`~app.validation.run_validation`. What makes a run correct is the
validation package's business; driving twenty-five validators over this fixture
would test those instead of these.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.clock import utcnow
from app.core.config import Settings
from app.core.enums import (
    ApprovalDecision,
    ApprovalGateKey,
    AuditAction,
    BaseDataType,
    ControllerFamily,
    ControllerTaskType,
    EventType,
    EvidenceKind,
    FailState,
    HmiProduct,
    ImportVerificationStatus,
    InstructionCategory,
    LogicElementKind,
    ModuleRole,
    ObjectApprovalState,
    ObjectType,
    OperandKind,
    ProfileFieldStatus,
    ProjectType,
    ReleaseCandidateStatus,
    ReleaseMode,
    RequirementStatus,
    RestrictedDomain,
    RestrictedDomainMode,
    RoutineLanguage,
    SignalDirection,
    SignalType,
    TagScope,
    UnitSystem,
    ValidationDiscipline,
    ValidationLevel,
    ValidationRunStatus,
    ValidationSeverity,
)
from app.core.storage import ProjectStorage
from app.engineering import gates, release
from app.engineering.revisions import allocate_revision_number, create_revision, get_revision
from app.models.audit import AuditEntry
from app.models.core import ObjectEvidenceLink
from app.models.event import Event
from app.models.plant import Device, HardwareModule, IOPoint, Signal
from app.models.plc import (
    ControllerModel,
    ControllerTask,
    LogicComment,
    LogicInstruction,
    LogicOperand,
    LogicRung,
    Program,
    Routine,
)
from app.models.profile import (
    REQUIRED_PROFILE_FIELDS,
    ProjectProfile,
    TargetPlatform,
    TemplateSet,
)
from app.models.project import Project
from app.models.release import (
    ExportArtifact,
    ExportProfile,
    ImportDependency,
    ReleaseCandidate,
    ValidationResult,
    ValidationRun,
)
from app.models.requirements import Requirement
from app.models.tags import Tag
from app.models.user import User

CATALOG_NUMBER = "1756-L83E"
FIRMWARE = "35.011"
STUDIO_VERSION = "35.00"
TEMPLATE_ID = "esco.motor.standard@1.0.0"

#: Tags the fixture's one rung references. Named here so a test that wants the
#: generator to refuse can add a rung pointing at something absent from it.
CONTROLLER_TAGS: tuple[str, ...] = ("ESTOP_OK", "MTR_101_Run_Cmd")


# ---------------------------------------------------------------------------
# Builders. Each satisfies exactly the criteria named in its docstring, so a
# test that wants one thing to fail can leave that one out and nothing else
# moves.
# ---------------------------------------------------------------------------
def _profile(db: Session, project: Project, **overrides: object) -> ProjectProfile:
    """A complete profile that permits generation: gate 1, and the profile rule."""
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

    body: dict[str, object] = {
        "project_name": "Line 4 packaging conveyor",
        "project_description": "Synthetic fixture description.",
        "project_type": ProjectType.UPGRADE,
        "naming_standard_version": "3.2",
        "alarm_philosophy_version": "1.1",
        "template_set_id": template_set.id,
        "units": UnitSystem.METRIC,
        "locale": "en-GB",
        "release_mode": ReleaseMode.IMPORT_PACKAGE_ONLY,
        "completeness": dict.fromkeys(REQUIRED_PROFILE_FIELDS, ProfileFieldStatus.PRESENT.value),
        "missing_required_fields": [],
        "is_complete_for_generation": True,
    }
    body.update(overrides)
    profile = ProjectProfile(project_id=project.id, **body)  # type: ignore[arg-type]
    db.add(profile)
    db.flush()
    return profile


def _platform(db: Session, project: Project) -> TargetPlatform:
    platform = TargetPlatform(
        project_id=project.id,
        controller_family=ControllerFamily.CONTROLLOGIX,
        controller_catalog_number=CATALOG_NUMBER,
        firmware_revision=FIRMWARE,
        studio_5000_version=STUDIO_VERSION,
        hmi_product=HmiProduct.NONE,
    )
    db.add(platform)
    db.flush()
    return platform


def _plant(db: Session, project: Project) -> HardwareModule:
    """One wired device, its signal, its card and its terminal: gates 2 and 3."""
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
        project_id=project.id,
        name="Local_2",
        catalog_number="1756-OB16E",
        role=ModuleRole.LOCAL_IO,
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
    return module


def _controller(db: Session, project: Project) -> ControllerModel:
    controller = ControllerModel(
        project_id=project.id,
        name="Line4_PLC",
        catalog_number=CATALOG_NUMBER,
        major_revision=35,
        minor_revision=11,
        description="Line 4 packaging conveyor.",
    )
    db.add(controller)
    db.flush()
    return controller


def _tags(db: Session, project: Project) -> list[Tag]:
    tags = [
        Tag(
            project_id=project.id,
            name=name,
            scope=TagScope.CONTROLLER,
            base_data_type=BaseDataType.BOOL,
            description=f"{name} from the interlock schedule.",
        )
        for name in CONTROLLER_TAGS
    ]
    for tag in tags:
        db.add(tag)
    db.flush()
    return tags


def _rung(
    db: Session,
    project: Project,
    routine: Routine,
    *,
    ordinal: int,
    condition: str,
    coil: str,
    comment: str = "",
) -> LogicRung:
    """One ``XIC(condition) OTE(coil);`` rung, structured rather than as text.

    Written out longhand because the generator renders from the instruction and
    operand rows, never from ``LogicRung.text``: a fixture that only set the
    text would prove nothing about what gets emitted.
    """
    rung = LogicRung(
        project_id=project.id,
        routine_id=routine.id,
        ordinal=ordinal,
        comment=comment,
        text=f"XIC({condition})OTE({coil});",
    )
    db.add(rung)
    db.flush()

    for position, (mnemonic, operand, destructive) in enumerate(
        (("XIC", condition, False), ("OTE", coil, True))
    ):
        instruction = LogicInstruction(
            project_id=project.id,
            rung_id=rung.id,
            ordinal=position,
            mnemonic=mnemonic,
            category=InstructionCategory.BIT,
            element_kind=(LogicElementKind.OUTPUT if destructive else LogicElementKind.CONDITION),
            is_destructive=destructive,
            text=f"{mnemonic}({operand})",
        )
        db.add(instruction)
        db.flush()
        db.add(
            LogicOperand(
                project_id=project.id,
                instruction_id=instruction.id,
                ordinal=0,
                kind=OperandKind.TAG,
                text=operand,
                resolved_name=operand,
                is_destination=destructive,
            )
        )
    db.flush()
    return rung


def _logic(db: Session, project: Project) -> Routine:
    """A task, a program and one ladder routine holding one rung: gate 3."""
    task = ControllerTask(
        project_id=project.id,
        name="MainTask",
        task_type=ControllerTaskType.CONTINUOUS,
        ordinal=0,
    )
    db.add(task)
    db.flush()
    program = Program(
        project_id=project.id,
        controller_task_id=task.id,
        name="MainProgram",
        ordinal=0,
        main_routine_name="Conveyor_Control",
    )
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
    rung = _rung(
        db,
        project,
        routine,
        ordinal=0,
        condition="ESTOP_OK",
        coil="MTR_101_Run_Cmd",
        comment="Run the conveyor while the E-stop string is healthy.",
    )
    db.add(
        LogicComment(
            project_id=project.id,
            rung_id=rung.id,
            operand_text="ESTOP_OK",
            language_code="en-GB",
            text="Healthy when every E-stop in the line is released.",
        )
    )
    db.add(
        ObjectEvidenceLink(
            project_id=project.id,
            object_type=ObjectType.LOGIC_RUNG,
            object_id=rung.id,
            template_id=TEMPLATE_ID,
            basis_kind=EvidenceKind.TEMPLATE_RULE,
            note="Generated from the approved motor-start template.",
        )
    )
    db.flush()
    return routine


def _validation_run(
    db: Session, project: Project, *, errors: int = 0, revision: int | None = None
) -> ValidationRun:
    from app.validation import ALL_DISCIPLINES

    revision_number = project.current_revision_number if revision is None else revision
    run = ValidationRun(
        project_id=project.id,
        revision_number=revision_number,
        status=ValidationRunStatus.COMPLETED,
        disciplines=[discipline.value for discipline in ALL_DISCIPLINES],
        checks_run=len(ALL_DISCIPLINES),
        error_count=errors,
        finished_at=utcnow(),
        summary="Synthetic run written by the release fixture.",
    )
    db.add(run)
    db.flush()
    for index in range(errors):
        db.add(
            ValidationResult(
                project_id=project.id,
                validation_run_id=run.id,
                code="logic.unresolved_operand",
                discipline=ValidationDiscipline.PLC_LOGIC,
                severity=ValidationSeverity.ERROR,
                message=f"Synthetic error {index}.",
                object_label=f"MainProgram / Conveyor_Control / Rung {index}",
            )
        )
    db.flush()
    return run


def _export_profile(db: Session, project: Project) -> ExportProfile:
    profile = ExportProfile(
        project_id=project.id,
        key="full_controller",
        name="Full controller package",
        target_studio_version=STUDIO_VERSION,
        controller_family=ControllerFamily.CONTROLLOGIX,
        l5x_schema_revision="1.0",
        include_delivery_artifacts=False,
        is_default=True,
    )
    db.add(profile)
    db.flush()
    return profile


def _accepted_requirement(db: Session, project: Project) -> Requirement:
    requirement = Requirement(
        project_id=project.id,
        reference="REQ-001",
        title="Emergency-stop run permissive",
        statement="The motor shall run only while the emergency-stop string is healthy.",
        source_text="The motor shall run only while the emergency-stop string is healthy.",
        status=RequirementStatus.ACCEPTED,
    )
    db.add(requirement)
    db.flush()
    return requirement


def _pass_gates(db: Session, project: Project, reviewer: User) -> None:
    """Take gates 1 to 4 through request and decision, as a reviewer would.

    Not faked by setting a status: the gate rows are recomputed from the model,
    so a fixture that satisfied the criteria only in appearance would pass here
    and fail in the service.
    """
    for key in (
        ApprovalGateKey.PROJECT_PROFILE,
        ApprovalGateKey.DEVICES_IO_TAGS,
        ApprovalGateKey.LOGIC_AND_DELIVERY,
        ApprovalGateKey.QA_AND_RISK,
    ):
        gate = next(
            item for item in gates.evaluate_all(db, project=project) if item.gate_key is key
        )
        approval = gates.request_decision(
            db, project=project, gate=gate, requested_by_agent="project_manager"
        )
        gates.record_decision(
            db,
            gate=gate,
            approval=approval,
            decision=ApprovalDecision.APPROVE,
            user=reviewer,
            comment="Checked against the functional description.",
        )


def _engineering_model(db: Session, project: Project) -> None:
    """Everything a package is generated from, and nothing a gate does not need."""
    _profile(db, project)
    _platform(db, project)
    _plant(db, project)
    _controller(db, project)
    _tags(db, project)
    _logic(db, project)
    _accepted_requirement(db, project)
    _export_profile(db, project)
    _validation_run(db, project)


@pytest.fixture
def ready_project(db: Session, project: Project, seeded: dict[str, User]) -> Project:
    """A project that may generate: model, validation and gates 1 to 4 settled."""
    _engineering_model(db, project)
    _pass_gates(db, project, seeded["reviewer"])
    db.flush()
    return project


def _merge_a_revision(db: Session, project: Project) -> int:
    """Move the project's head forward the way a merge does, without one.

    The staleness rule is about the revision number and nothing else, so driving
    a real change set through the merge engine would test the merge engine.
    """
    number = allocate_revision_number(db, project)
    create_revision(db, project, summary="Synthetic merge.", number=number)
    db.flush()
    return number


def _events(db: Session, project: Project, event_type: EventType) -> list[Event]:
    return list(
        db.execute(
            select(Event)
            .where(Event.project_id == project.id, Event.event_type == event_type)
            .order_by(Event.seq)
        ).scalars()
    )


def _audit(db: Session, project: Project, action: AuditAction) -> list[AuditEntry]:
    return list(
        db.execute(
            select(AuditEntry)
            .where(AuditEntry.project_id == project.id, AuditEntry.action == action)
            .order_by(AuditEntry.created_at, AuditEntry.id)
        ).scalars()
    )


def _codes(exc: release.ReleaseRefused) -> set[release.BlockCode]:
    return {block.code for block in exc.blocks}


def _package_root(settings: Settings, candidate: ReleaseCandidate) -> Path:
    storage = ProjectStorage(candidate.project_id, settings)
    return storage.root / candidate.package_path


# ---------------------------------------------------------------------------
# Readiness, answered without generating
# ---------------------------------------------------------------------------
def test_readiness_on_a_bare_project_names_every_block_and_generates_nothing(
    db: Session, project: Project, settings: Settings
) -> None:
    """What a UI asks before it offers the button, and why it must not generate."""
    readiness = release.release_readiness(db, project=project, settings=settings)

    assert readiness.is_ready is False
    assert readiness.blocking, "a not-ready answer with no reason is unusable"
    assert _codes_of(readiness) >= {
        release.BlockCode.NO_PROFILE,
        release.BlockCode.NO_CONTROLLER,
        release.BlockCode.NO_VALIDATION_RUN,
        release.BlockCode.GATE_UNDECIDED,
    }
    assert all(block.detail for block in readiness.blocking)
    assert db.execute(select(ReleaseCandidate)).scalars().all() == []
    assert not (ProjectStorage(project.id, settings).root / release.RELEASES_DIR).exists()


def test_readiness_states_the_offline_ceiling_and_that_verification_is_impossible(
    db: Session, ready_project: Project, settings: Settings
) -> None:
    """The adapter is off by default, and the ceiling has to be said before anyone asks."""
    readiness = release.release_readiness(db, project=ready_project, settings=settings)

    assert readiness.is_ready is True
    assert readiness.blocking == ()
    assert readiness.outstanding_gates == ()
    assert readiness.validation_error_count == 0
    assert readiness.highest_attainable_validation_level is ValidationLevel.ROUND_TRIP_PARSED
    assert readiness.import_verification_possible is False
    assert "licensed Rockwell workstation" in readiness.import_verification_detail


def _codes_of(readiness: release.ReleaseReadiness) -> set[release.BlockCode]:
    return {block.code for block in readiness.blocking}


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------
def test_create_generate_freeze_produces_a_signed_package_on_disk(
    db: Session, ready_project: Project, seeded: dict[str, User], settings: Settings
) -> None:
    """One pass through the pipeline, in the order a project lives it."""
    reviewer = seeded["reviewer"]
    candidate = release.create_release_candidate(
        db, project=ready_project, requested_by=reviewer, settings=settings
    )
    assert candidate.candidate_number == 1
    assert candidate.status is ReleaseCandidateStatus.DRAFT
    assert candidate.revision_number == ready_project.current_revision_number

    generated = release.generate_artifacts(db, candidate=candidate, settings=settings)
    assert generated.status is ReleaseCandidateStatus.GENERATED
    assert generated.artifacts, "a generated candidate with no artifacts is a failed one"

    root = _package_root(settings, generated)
    assert root.is_dir()
    for artifact in generated.artifacts:
        assert (root / artifact.relative_path).is_file()
        assert artifact.sha256

    manifest = release.freeze_manifest(
        db, candidate=generated, approved_by=reviewer, settings=settings
    )
    assert manifest.frozen_at is not None
    assert manifest.frozen_by_id == reviewer.id
    assert manifest.content_sha256 and manifest.checksum_file_sha256
    assert manifest.artifact_count == len(generated.artifacts)
    assert generated.status is ReleaseCandidateStatus.APPROVED
    assert generated.approval_state is ObjectApprovalState.APPROVED

    stored = json.loads((root / "00_Manifest" / "manifest.json").read_text(encoding="utf-8"))
    assert stored == manifest.content


def test_generation_marks_the_revision_a_release_baseline(
    db: Session, ready_project: Project, seeded: dict[str, User], settings: Settings
) -> None:
    """A package with a checksum has to keep meaning what it meant.

    The baseline flag is what stops the revision it was cut from becoming the
    base of an in-place edit afterwards.
    """
    candidate = release.create_release_candidate(
        db, project=ready_project, requested_by=seeded["engineer"], settings=settings
    )
    revision = get_revision(db, ready_project.id, candidate.revision_number)

    assert revision is not None
    assert revision.is_release_baseline is True


def test_the_candidate_level_is_the_weakest_of_its_artifacts(
    db: Session, ready_project: Project, settings: Settings
) -> None:
    """One file that only reached GENERATED means the package did.

    The CSV exports are never round-tripped — there is no parser to read them
    back — so a package containing one cannot claim the level its L5X files
    reached. Rolling up to the strongest would promote a file nobody checked.
    """
    candidate = release.create_release_candidate(db, project=ready_project, settings=settings)
    release.generate_artifacts(db, candidate=candidate, settings=settings)

    levels = {artifact.relative_path: artifact.validation_level for artifact in candidate.artifacts}
    assert ValidationLevel.ROUND_TRIP_PARSED in levels.values(), (
        "the L5X files should have been parsed back; otherwise this test proves nothing"
    )
    assert candidate.validation_level is ValidationLevel.GENERATED
    assert candidate.validation_level is min(
        levels.values(), key=lambda level: list(ValidationLevel).index(level)
    )


def test_import_verification_is_unavailable_with_a_reason_not_merely_unattempted(
    db: Session, ready_project: Project, settings: Settings
) -> None:
    """The refusal this product would tell itself first, so it is asserted directly."""
    candidate = release.create_release_candidate(db, project=ready_project, settings=settings)
    release.generate_artifacts(db, candidate=candidate, settings=settings)

    assert candidate.import_verification_status is ImportVerificationStatus.UNAVAILABLE
    assert candidate.import_verification_status is not ImportVerificationStatus.NOT_ATTEMPTED
    assert "No licensed Rockwell workstation is configured" in candidate.import_verification_detail
    assert candidate.is_import_ready_candidate_only is True


def test_the_pipeline_announces_and_audits_every_step(
    db: Session, ready_project: Project, seeded: dict[str, User], settings: Settings
) -> None:
    """Four events, and an audit row for each operation that changed something."""
    reviewer = seeded["reviewer"]
    candidate = release.create_release_candidate(
        db, project=ready_project, requested_by=reviewer, settings=settings
    )
    release.generate_artifacts(db, candidate=candidate, settings=settings)
    release.freeze_manifest(db, candidate=candidate, approved_by=reviewer, settings=settings)

    created = _events(db, ready_project, EventType.RELEASE_CANDIDATE_CREATED)
    started = _events(db, ready_project, EventType.EXPORT_STARTED)
    per_artifact = _events(db, ready_project, EventType.EXPORT_ARTIFACT_CREATED)
    ready = _events(db, ready_project, EventType.RELEASE_READY)

    assert len(created) == 1
    assert len(started) == 1
    assert len(per_artifact) == len(candidate.artifacts)
    assert len(ready) == 1

    assert started[0].payload["expected_artifact_count"] == len(candidate.artifacts)
    assert {str(item.payload["relative_path"]) for item in per_artifact} == {
        artifact.relative_path for artifact in candidate.artifacts
    }
    assert ready[0].payload["is_import_ready_candidate_only"] is True
    assert ready[0].payload["import_verification_status"] == ImportVerificationStatus.UNAVAILABLE

    assert len(_audit(db, ready_project, AuditAction.RELEASE_CANDIDATE_CREATED)) == 1
    assert len(_audit(db, ready_project, AuditAction.EXPORT_GENERATED)) == 1
    assert len(_audit(db, ready_project, AuditAction.RELEASE_APPROVED)) == 1


def test_a_regenerated_candidate_from_the_same_revision_is_byte_identical(
    db: Session, ready_project: Project, settings: Settings
) -> None:
    """The property every checksum in the manifest rests on.

    Without it a hash records only that somebody hashed something once, and a
    regenerated package cannot be diffed against an approved one — which is the
    entire reason a release manifest exists.
    """
    first = release.create_release_candidate(db, project=ready_project, settings=settings)
    release.generate_artifacts(db, candidate=first, settings=settings)
    first_hashes = {artifact.relative_path: artifact.sha256 for artifact in sorted_artifacts(first)}

    second = release.create_release_candidate(db, project=ready_project, settings=settings)
    release.generate_artifacts(db, candidate=second, settings=settings)
    second_hashes = {
        artifact.relative_path: artifact.sha256 for artifact in sorted_artifacts(second)
    }

    assert second.candidate_number == first.candidate_number + 1
    assert second.revision_number == first.revision_number
    assert second_hashes == first_hashes
    assert second.package_sha256 == first.package_sha256
    assert second.package_bytes == first.package_bytes


def sorted_artifacts(candidate: ReleaseCandidate) -> list[ExportArtifact]:
    return sorted(candidate.artifacts, key=lambda artifact: artifact.relative_path)


def test_import_dependencies_are_rebuilt_rather_than_accumulated(
    db: Session, ready_project: Project, settings: Settings
) -> None:
    """Regenerating must not leave an edge pointing at an artifact that is gone."""
    candidate = release.create_release_candidate(db, project=ready_project, settings=settings)
    release.generate_artifacts(db, candidate=candidate, settings=settings)
    first = len(
        db.execute(
            select(ImportDependency).where(ImportDependency.release_candidate_id == candidate.id)
        )
        .scalars()
        .all()
    )
    release.generate_artifacts(db, candidate=candidate, settings=settings)
    second = list(
        db.execute(
            select(ImportDependency).where(ImportDependency.release_candidate_id == candidate.id)
        ).scalars()
    )
    live = {artifact.id for artifact in candidate.artifacts}

    assert first > 0
    assert len(second) == first
    assert all(edge.artifact_id in live for edge in second)


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------
def test_it_refuses_a_revision_with_blocking_validation_errors(
    db: Session, ready_project: Project, settings: Settings
) -> None:
    """Generating from a model with known errors fails at import for a known reason."""
    _validation_run(db, ready_project, errors=2)
    db.flush()

    with pytest.raises(release.ReleaseRefused) as caught:
        release.create_release_candidate(db, project=ready_project, settings=settings)

    assert release.BlockCode.VALIDATION_ERRORS in _codes(caught.value)
    assert "2 error(s)" in caught.value.message
    assert db.execute(select(ReleaseCandidate)).scalars().all() == []


def test_it_refuses_while_a_gate_covering_the_work_is_undecided(
    db: Session, project: Project, settings: Settings
) -> None:
    """A gate is where a human takes responsibility; walking past one fakes a sign-off."""
    _engineering_model(db, project)
    db.flush()

    readiness = release.release_readiness(db, project=project, settings=settings)
    with pytest.raises(release.ReleaseRefused) as caught:
        release.create_release_candidate(db, project=project, settings=settings)

    assert release.BlockCode.GATE_UNDECIDED in _codes(caught.value)
    assert ApprovalGateKey.PROJECT_PROFILE in readiness.outstanding_gates
    assert readiness.outstanding_gates[0] is ApprovalGateKey.PROJECT_PROFILE


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        pytest.param(
            {"release_mode": ReleaseMode.REVIEW_ONLY},
            release.BlockCode.REVIEW_ONLY,
            id="review_only",
        ),
        pytest.param(
            {
                "is_complete_for_generation": False,
                "completeness": {"naming_standard_version": ProfileFieldStatus.MISSING.value},
                "missing_required_fields": ["naming_standard_version"],
            },
            release.BlockCode.PROFILE_INCOMPLETE,
            id="incomplete",
        ),
        pytest.param(
            {
                "restricted_domains": [RestrictedDomain.SAFETY_LOGIC.value],
                "restricted_domain_mode": RestrictedDomainMode.MANUAL_REVIEW_ONLY,
            },
            release.BlockCode.RESTRICTED_DOMAIN,
            id="restricted_domain",
        ),
    ],
)
def test_it_refuses_a_profile_that_blocks_generation_and_says_which_condition(
    db: Session,
    project: Project,
    settings: Settings,
    overrides: dict[str, object],
    expected: release.BlockCode,
) -> None:
    """Three independent conditions, each needing a different next step.

    "The profile blocks generation" is not something anyone can act on, so the
    refusal has to name which of the three applies.
    """
    _profile(db, project, **overrides)
    _platform(db, project)
    _plant(db, project)
    _controller(db, project)
    _tags(db, project)
    _logic(db, project)
    _validation_run(db, project)
    db.flush()

    with pytest.raises(release.ReleaseRefused) as caught:
        release.create_release_candidate(db, project=project, settings=settings)

    assert expected in _codes(caught.value)


def test_it_refuses_to_generate_from_a_stale_revision(
    db: Session, ready_project: Project, settings: Settings
) -> None:
    """Never quietly ship the revision the project has already moved past."""
    candidate = release.create_release_candidate(db, project=ready_project, settings=settings)
    head = _merge_a_revision(db, ready_project)

    with pytest.raises(release.ReleaseRefused) as caught:
        release.generate_artifacts(db, candidate=candidate, settings=settings)

    assert release.BlockCode.STALE_REVISION in _codes(caught.value)
    assert f"revision {head}" in caught.value.message
    assert candidate.package_path == ""


def test_force_bypasses_only_the_staleness_check_and_records_that_it_did(
    db: Session, ready_project: Project, seeded: dict[str, User], settings: Settings
) -> None:
    """The one forceable rule, and the sentence the customer gets because of it."""
    candidate = release.create_release_candidate(
        db, project=ready_project, requested_by=seeded["engineer"], force=True, settings=settings
    )
    _merge_a_revision(db, ready_project)

    release.generate_artifacts(db, candidate=candidate, settings=settings)

    assert candidate.status is ReleaseCandidateStatus.GENERATED
    assert release.was_forced_past_stale_revision(candidate)
    codes = {str(entry["code"]) for entry in candidate.limitations}
    assert release.FORCED_STALE_REVISION_CODE in codes

    root = _package_root(settings, candidate)
    limitations = (root / "00_Manifest" / "LIMITATIONS.md").read_text(encoding="utf-8")
    assert release.FORCED_STALE_REVISION_CODE in limitations


def test_force_does_not_bypass_a_blocking_validation_error(
    db: Session, ready_project: Project, settings: Settings
) -> None:
    """Everything except staleness is a refusal, not a warning."""
    _validation_run(db, ready_project, errors=1)
    db.flush()

    with pytest.raises(release.ReleaseRefused) as caught:
        release.create_release_candidate(db, project=ready_project, force=True, settings=settings)

    assert release.BlockCode.VALIDATION_ERRORS in _codes(caught.value)


def test_a_generator_refusal_becomes_a_recorded_failure_naming_the_object(
    db: Session, ready_project: Project, settings: Settings
) -> None:
    """A refusal is an engineering statement, so it lands where a reviewer looks.

    It also leaves nothing behind: the package is assembled in memory and the
    refusal happens before any of it reaches the filesystem, so there is no
    half-written directory for somebody to find and believe.
    """
    routine = db.execute(select(Routine).where(Routine.project_id == ready_project.id)).scalar_one()
    _rung(
        db,
        ready_project,
        routine,
        ordinal=1,
        condition="GHOST_PERMISSIVE",
        coil="MTR_101_Run_Cmd",
    )
    db.flush()

    candidate = release.create_release_candidate(db, project=ready_project, settings=settings)
    outcome = release.generate_artifacts(db, candidate=candidate, settings=settings)

    assert outcome.status is ReleaseCandidateStatus.VALIDATION_FAILED
    assert "GHOST_PERMISSIVE" in outcome.notes
    assert outcome.unsupported_items[0]["subject"] == "GHOST_PERMISSIVE"
    assert "Conveyor_Control" in str(outcome.unsupported_items[0]["location"])
    assert outcome.package_path == ""
    assert outcome.artifacts == []

    storage = ProjectStorage(ready_project.id, settings)
    assert not (storage.root / release.RELEASES_DIR / "RC-0001").exists()
    assert _events(db, ready_project, EventType.EXPORT_STARTED) == []

    refusals = _audit(db, ready_project, AuditAction.EXPORT_GENERATED)
    assert len(refusals) == 1
    assert refusals[0].details["outcome"] == "refused"
    assert refusals[0].details["subject"] == "GHOST_PERMISSIVE"


def test_it_refuses_to_freeze_a_manifest_without_a_named_human_approver(
    db: Session, ready_project: Project, settings: Settings
) -> None:
    """The one thing in the pipeline that cannot be inferred, defaulted or agent-supplied."""
    candidate = release.create_release_candidate(db, project=ready_project, settings=settings)
    release.generate_artifacts(db, candidate=candidate, settings=settings)

    with pytest.raises(release.ReleaseRefused) as caught:
        release.freeze_manifest(db, candidate=candidate, approved_by=None, settings=settings)

    assert release.BlockCode.NO_NAMED_APPROVER in _codes(caught.value)
    assert candidate.manifest is None
    assert candidate.status is ReleaseCandidateStatus.GENERATED


def test_it_refuses_to_freeze_a_candidate_that_was_never_generated(
    db: Session, ready_project: Project, seeded: dict[str, User], settings: Settings
) -> None:
    candidate = release.create_release_candidate(db, project=ready_project, settings=settings)

    with pytest.raises(release.ReleaseRefused) as caught:
        release.freeze_manifest(
            db, candidate=candidate, approved_by=seeded["reviewer"], settings=settings
        )

    assert release.BlockCode.CANDIDATE_STATUS in _codes(caught.value)


def test_it_refuses_to_freeze_twice(
    db: Session, ready_project: Project, seeded: dict[str, User], settings: Settings
) -> None:
    """A manifest is written once. Any change at all produces the next candidate."""
    reviewer = seeded["reviewer"]
    candidate = release.create_release_candidate(db, project=ready_project, settings=settings)
    release.generate_artifacts(db, candidate=candidate, settings=settings)
    release.freeze_manifest(db, candidate=candidate, approved_by=reviewer, settings=settings)

    with pytest.raises(release.ReleaseRefused) as caught:
        release.freeze_manifest(db, candidate=candidate, approved_by=reviewer, settings=settings)

    assert release.BlockCode.MANIFEST_ALREADY_FROZEN in _codes(caught.value)


@pytest.mark.parametrize(
    "relative",
    [
        pytest.param("00_Manifest/manifest.json", id="manifest"),
        pytest.param("01_Controller/Controller_Tags.csv", id="a_file_the_manifest_only_lists"),
    ],
)
def test_it_refuses_to_freeze_a_package_that_changed_after_generation(
    db: Session,
    ready_project: Project,
    seeded: dict[str, User],
    settings: Settings,
    relative: str,
) -> None:
    """A manifest describing bytes nobody re-examined proves nothing.

    Every artifact is re-read, not only the manifest: hashing the checksum file
    alone would prove the *list* is intact while a changed tag CSV sailed past,
    and the freeze is the moment a named human puts their name to these bytes.
    """
    candidate = release.create_release_candidate(db, project=ready_project, settings=settings)
    release.generate_artifacts(db, candidate=candidate, settings=settings)

    tampered = _package_root(settings, candidate) / relative
    tampered.write_bytes(tampered.read_bytes() + b"\n")

    with pytest.raises(release.ReleaseRefused) as caught:
        release.freeze_manifest(
            db, candidate=candidate, approved_by=seeded["reviewer"], settings=settings
        )

    assert release.BlockCode.PACKAGE_ALTERED in _codes(caught.value)
    assert relative in caught.value.message
    assert candidate.manifest is None


def test_it_refuses_to_generate_when_no_controller_is_recorded(
    db: Session, project: Project, seeded: dict[str, User], settings: Settings
) -> None:
    """Nothing about a controller may be invented, so there is nothing to generate against."""
    _profile(db, project)
    _platform(db, project)
    _plant(db, project)
    _tags(db, project)
    _logic(db, project)
    _validation_run(db, project)
    db.flush()

    with pytest.raises(release.ReleaseRefused) as caught:
        release.create_release_candidate(db, project=project, settings=settings)

    assert release.BlockCode.NO_CONTROLLER in _codes(caught.value)


# ---------------------------------------------------------------------------
# What travels inside the package
# ---------------------------------------------------------------------------
def test_the_package_carries_its_own_provenance_and_states_its_ceiling(
    db: Session, ready_project: Project, seeded: dict[str, User], settings: Settings
) -> None:
    """A limitation the customer meets at the import dialog is a limitation we hid."""
    candidate = release.create_release_candidate(
        db, project=ready_project, requested_by=seeded["engineer"], settings=settings
    )
    release.generate_artifacts(db, candidate=candidate, settings=settings)
    manifest = release.freeze_manifest(
        db, candidate=candidate, approved_by=seeded["reviewer"], settings=settings
    )

    content = manifest.content
    assert content["verification"]["rockwell_import_verified"] is False
    assert content["verification"]["label"] == "import-ready candidate"
    assert content["validation"]["revision_number"] == candidate.revision_number
    assert content["target"]["export_profile_key"] == "full_controller"
    assert content["approvals"], "the gate decisions should travel with the package"
    assert any(record["approver"] for record in content["approvals"])

    root = _package_root(settings, candidate)
    evidence = json.loads((root / "06_Audit" / "evidence_index.json").read_text(encoding="utf-8"))
    assert any(entry["basis_kind"] == EvidenceKind.TEMPLATE_RULE for entry in evidence["entries"])


def test_the_target_snapshot_records_the_platform_the_package_was_built_for(
    db: Session, ready_project: Project, settings: Settings
) -> None:
    """A package built for firmware 33 and approved at firmware 35 imports into the wrong box."""
    candidate = release.create_release_candidate(db, project=ready_project, settings=settings)
    release.generate_artifacts(db, candidate=candidate, settings=settings)

    snapshot = candidate.target_profile_snapshot
    assert snapshot["controller_catalog_number"] == CATALOG_NUMBER
    assert snapshot["firmware_revision"] == FIRMWARE
    assert snapshot["studio_5000_version"] == STUDIO_VERSION


def test_a_soft_deleted_instruction_is_not_emitted(
    db: Session, ready_project: Project, settings: Settings
) -> None:
    """Logic somebody removed must not reappear in a generated file.

    The ORM relationship does not filter soft deletes, so this is the one place
    a deleted instruction could travel into a package unnoticed.
    """
    routine = db.execute(select(Routine).where(Routine.project_id == ready_project.id)).scalar_one()
    doomed = _rung(
        db, ready_project, routine, ordinal=1, condition="ESTOP_OK", coil="MTR_101_Run_Cmd"
    )
    for instruction in doomed.instructions:
        instruction.deleted_at = utcnow()
    doomed.comment = "This rung's instructions were deleted."
    db.flush()

    candidate = release.create_release_candidate(db, project=ready_project, settings=settings)
    release.generate_artifacts(db, candidate=candidate, settings=settings)

    root = _package_root(settings, candidate)
    routine_file = (
        root / "01_Controller" / "Routines" / "MainProgram-Conveyor_Control.L5X"
    ).read_text(encoding="utf-8")
    # The surviving rung still renders; the emptied one renders as an empty rung
    # rather than carrying instructions nobody wants.
    assert routine_file.count("XIC(ESTOP_OK)OTE(MTR_101_Run_Cmd);") == 1
