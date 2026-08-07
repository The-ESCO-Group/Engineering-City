"""The production workflow: the shape it plans, and the places it stops.

Six behaviours, and five of them are about stopping. A pipeline that designs,
generates and packages is only worth having if it refuses in the right places, so
the assertions here are mostly about a run that has parked and about what it says
while it is parked.

The one that carries the most weight is the last: **the review path is
unaffected**. A project with no profile, or one whose profile forbids
generation, runs exactly the chain it ran before this pipeline existed and never
reaches a generation step. That is not a nicety — it is what every other test in
this repository is written against.

Everything runs on the deterministic MockProvider: no network, no model, no cost.
The reviewer's decisions go through :func:`app.engineering.gates.record_decision`
and the approvals are settled the way the API settles them, because a fixture
that set a gate's status directly would prove nothing about a gate that is
recomputed from the model on every read.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

# Importing these is what registers the handlers. The application does it in
# ``app.orchestration``; the tests do it explicitly so a test that drives the
# queue directly does not depend on import order elsewhere.
import app.agents.review
import app.orchestration.steps  # noqa: F401
from app.core.clock import utcnow
from app.core.config import Settings
from app.core.enums import (
    ActionType,
    AddressAssignmentStatus,
    ApprovalDecision,
    ApprovalGateKey,
    ApprovalKind,
    ApprovalStatus,
    BaseDataType,
    ClarificationStatus,
    ControllerFamily,
    ControllerTaskType,
    DocumentRole,
    EvidenceKind,
    FailState,
    FileKind,
    FindingStatus,
    GateStatus,
    HmiProduct,
    ImportVerificationStatus,
    InstructionCategory,
    LogicElementKind,
    LogicOrigin,
    ModuleRole,
    ObjectType,
    OperandKind,
    ProfileFieldStatus,
    ProjectStatus,
    ProjectType,
    QuestionCategory,
    ReleaseCandidateStatus,
    ReleaseMode,
    RequirementStatus,
    RestrictedDomain,
    RestrictedDomainMode,
    RoutineLanguage,
    SignalDirection,
    SignalType,
    TagScope,
    TaskKind,
    TaskStatus,
    UnitSystem,
    ValidationLevel,
)
from app.models.approval import Approval
from app.models.delivery import HMIBinding, HMIScreen
from app.models.file import UploadedFile
from app.models.finding import Finding
from app.models.plant import AddressAssignment, Device, HardwareModule, IOPoint, Signal
from app.models.plc import (
    ControllerModel,
    ControllerTask,
    LogicInstruction,
    LogicNetwork,
    LogicOperand,
    LogicRung,
    Program,
    Routine,
)
from app.models.profile import (
    REQUIRED_PROFILE_FIELDS,
    ProjectProfile,
    ProjectQuestion,
    TargetPlatform,
    TemplateSet,
)
from app.models.project import Project
from app.models.release import ExportProfile, ReleaseCandidate
from app.models.release_archive import ReleaseArchive
from app.models.requirements import Requirement
from app.models.revision import ApprovalGate, ChangeSet, ProjectRevision
from app.models.tags import Tag
from app.models.task import Task, TaskDependency
from app.models.user import User
from app.orchestration import engine, worker
from app.orchestration.handlers import HandlerResult

CATALOG_NUMBER = "1756-L83E"
FIRMWARE = "35.011"
STUDIO_VERSION = "35.00"
HMI_VERSION = "14.00"
TEMPLATE_ID = "esco.motor.standard@1.0.0"
HMI_TEMPLATE_ID = "esco.hmi.overview"

#: The device the fixture's requirement names, so the Tag Architecture Agent has
#: an accepted requirement to cite as the basis for the tags it proposes.
DEVICE_PREFIX = "MTR_101"


# ---------------------------------------------------------------------------
# The engineering model a production run needs
# ---------------------------------------------------------------------------
def _profile(db: Session, project: Project, **overrides: object) -> ProjectProfile:
    """A complete profile that permits generation."""
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
        "project_name": "Synthetic Conveyor Line 3",
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
    """A controller *and* a PanelView, because ``HmiProduct.NONE`` tested nothing.

    Gate 3 asks whether the HMI design exists, and with no HMI in scope that
    criterion returns early. A fixture declaring ``NONE`` therefore never
    exercised it — which is how the pipeline came to hold a deadlock nothing
    here noticed: ``DELIVERY`` used to sit behind the very gate whose HMI
    criterion it was expected to satisfy, so any project naming an HMI parked at
    gate 3 for good. Declaring one keeps that criterion on the path every run in
    this file takes.
    """
    platform = TargetPlatform(
        project_id=project.id,
        controller_family=ControllerFamily.CONTROLLOGIX,
        controller_catalog_number=CATALOG_NUMBER,
        firmware_revision=FIRMWARE,
        studio_5000_version=STUDIO_VERSION,
        hmi_product=HmiProduct.FACTORYTALK_VIEW_ME,
        hmi_version=HMI_VERSION,
        hmi_terminal_model="2711P-T10C22D9P",
        hmi_resolution_width=1024,
        hmi_resolution_height=768,
    )
    db.add(platform)
    db.flush()
    return platform


def _plant(db: Session, project: Project) -> Device:
    """One wired device with two signals, one of which has no tag yet.

    The untagged signal is what gives the Tag Architecture Agent something to
    propose. Without it the fixture would exercise the change-set path only when
    it happened to fail.
    """
    device = Device(
        project_id=project.id,
        tag_prefix=DEVICE_PREFIX,
        name="Conveyor drive motor",
        fail_state=FailState.DE_ENERGIZE,
        origin=LogicOrigin.PARSED_FROM_SOURCE,
    )
    db.add(device)
    db.flush()
    command = Signal(
        project_id=project.id,
        device_id=device.id,
        name="Run_Cmd",
        signal_type=SignalType.DISCRETE_OUTPUT,
        direction=SignalDirection.OUTPUT,
        ordinal=0,
    )
    db.add(command)
    db.add(
        Signal(
            project_id=project.id,
            device_id=device.id,
            name="Sts_Running",
            signal_type=SignalType.DISCRETE_INPUT,
            direction=SignalDirection.INPUT,
            ordinal=1,
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
    point = IOPoint(
        project_id=project.id,
        hardware_module_id=module.id,
        address="Local:2:O.Data.3",
        signal_type=SignalType.DISCRETE_OUTPUT,
        direction=SignalDirection.OUTPUT,
        origin=LogicOrigin.PARSED_FROM_SOURCE,
    )
    db.add(point)
    db.flush()
    # The command signal is wired to a real terminal. Without it the device is a
    # device nobody landed, which the I/O validators report as an error — and
    # correctly so, which is why the fixture models the wiring rather than
    # waiving the check.
    db.add(
        AddressAssignment(
            project_id=project.id,
            signal_id=command.id,
            io_point_id=point.id,
            status=AddressAssignmentStatus.ASSIGNED,
            origin=LogicOrigin.PARSED_FROM_SOURCE,
        )
    )
    db.flush()
    return device


def _controller(db: Session, project: Project) -> ControllerModel:
    controller = ControllerModel(
        project_id=project.id,
        name="Line3_PLC",
        catalog_number=CATALOG_NUMBER,
        major_revision=35,
        minor_revision=11,
        description="Synthetic conveyor line 3.",
    )
    db.add(controller)
    db.flush()
    return controller


def _tags(db: Session, project: Project) -> None:
    for name in ("ESTOP_OK", f"{DEVICE_PREFIX}_Run_Cmd"):
        db.add(
            Tag(
                project_id=project.id,
                name=name,
                scope=TagScope.CONTROLLER,
                base_data_type=BaseDataType.BOOL,
                description=f"{name} carried over from the existing controller.",
                source_name=name,
                origin=LogicOrigin.PARSED_FROM_SOURCE,
            )
        )
    db.flush()


def _logic(db: Session, project: Project) -> Routine:
    """One task, one program, one ladder routine holding one rung."""
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
        origin=LogicOrigin.PARSED_FROM_SOURCE,
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
        origin=LogicOrigin.PARSED_FROM_SOURCE,
    )
    db.add(routine)
    db.flush()

    # The block that implements REQ-001, named as such. Without it the PLC Logic
    # Agent has an accepted requirement no logic claims, and it asks which
    # approved pattern implements it rather than choosing one — which is the
    # right behaviour and is asserted elsewhere.
    network = LogicNetwork(
        project_id=project.id,
        routine_id=routine.id,
        name="Conveyor run",
        purpose="Run the conveyor while the E-stop string is healthy.",
        ordinal=0,
        requirement_reference="REQ-001",
        rung_count=1,
        origin=LogicOrigin.PARSED_FROM_SOURCE,
    )
    db.add(network)
    db.flush()

    rung = LogicRung(
        project_id=project.id,
        routine_id=routine.id,
        logic_network_id=network.id,
        ordinal=0,
        comment="Run the conveyor while the E-stop string is healthy.",
        text=f"XIC(ESTOP_OK)OTE({DEVICE_PREFIX}_Run_Cmd);",
        origin=LogicOrigin.PARSED_FROM_SOURCE,
    )
    db.add(rung)
    db.flush()
    for position, (mnemonic, operand, destructive) in enumerate(
        (("XIC", "ESTOP_OK", False), ("OTE", f"{DEVICE_PREFIX}_Run_Cmd", True))
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
    return routine


def _hmi(db: Session, project: Project) -> HMIScreen:
    """The overview display the PanelView carries, and one binding on it.

    Read from the existing terminal application rather than generated, which is
    what an upgrade project actually has: nothing in this backend creates screens
    — the HMI delivery agent audits them — so a project naming an HMI has to
    bring its display design with it or gate 3 has nothing to approve.

    ``access='read'`` is load-bearing. An unstated direction is treated as a
    command, and a command binding on a tag no rung reads is a release-blocking
    error; this binding displays a status the ladder writes.
    """
    screen = HMIScreen(
        project_id=project.id,
        key="line3_overview",
        name="Line 3 overview",
        title="Line 3 overview",
        ordinal=0,
        template_id=HMI_TEMPLATE_ID,
        template_version="1.0.0",
        origin=LogicOrigin.PARSED_FROM_SOURCE,
    )
    db.add(screen)
    db.flush()
    command_tag = db.execute(
        select(Tag).where(Tag.project_id == project.id, Tag.name == f"{DEVICE_PREFIX}_Run_Cmd")
    ).scalar_one()
    db.add(
        HMIBinding(
            project_id=project.id,
            screen_id=screen.id,
            property_name="Value",
            tag_name=command_tag.name,
            tag_id=command_tag.id,
            is_resolved=True,
            access="read",
            origin=LogicOrigin.PARSED_FROM_SOURCE,
        )
    )
    db.flush()
    return screen


def _accepted_requirement(db: Session, project: Project) -> Requirement:
    """A requirement a named engineer already accepted, naming the device.

    Written directly rather than proposed: the point of the fixture is what the
    tag agent does *with* an accepted requirement, and an agent may not accept
    its own work, so somebody has to have done it first.
    """
    requirement = Requirement(
        project_id=project.id,
        reference="REQ-001",
        title=f"{DEVICE_PREFIX} conveyor drive",
        statement=(
            f"The conveyor drive motor {DEVICE_PREFIX} shall run only while the E-stop "
            f"string is healthy."
        ),
        source_text="The conveyor shall run only while the E-stop string is healthy.",
        status=RequirementStatus.ACCEPTED,
    )
    db.add(requirement)
    db.flush()
    return requirement


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


@pytest.fixture
def production_project(db: Session, ingested_project: Project) -> Project:
    """The demo project, plus everything a production run needs to reach a package.

    Built on the ingested fixture rather than a bare project because the
    Requirements and Planning Agent transcribes documented interlocks, and a
    project with no parsed documents has none to cite.
    """
    _profile(db, ingested_project)
    _platform(db, ingested_project)
    _plant(db, ingested_project)
    _controller(db, ingested_project)
    _tags(db, ingested_project)
    _logic(db, ingested_project)
    _hmi(db, ingested_project)
    _accepted_requirement(db, ingested_project)
    _export_profile(db, ingested_project)
    # This fixture supplies a clean canonical controller, plant, logic and HMI
    # model explicitly above. Every demo document that describes the *other*,
    # deliberately defective controller is kept as review evidence but its parsed
    # payload is marked a legacy fixture, so the canonical source importer does
    # not mix a second controller into this happy-path release model. That covers
    # the L5X, the alarm register — whose triggers name Conveyor_Line3 tags no
    # rung here writes — and the HMI export, which binds to the same tags. The
    # conflicting I/O schedule is retired outright for the same reason. Defective
    # source is covered by the blocking workflow tests; this fixture exists to
    # prove a valid project can finish.
    superseded_roles = frozenset({DocumentRole.ALARM_LIST, DocumentRole.HMI_EXPORT})
    for uploaded in db.execute(
        select(UploadedFile).where(UploadedFile.project_id == ingested_project.id)
    ).scalars():
        if uploaded.file_kind is FileKind.L5X or uploaded.document_role in superseded_roles:
            for artifact in uploaded.artifacts:
                artifact.artifact_kind = "legacy_fixture_evidence"
        elif uploaded.document_role is DocumentRole.IO_LIST:
            uploaded.deleted_at = utcnow()
    db.commit()
    return ingested_project


# ---------------------------------------------------------------------------
# Driving a run
# ---------------------------------------------------------------------------
def _start(db: Session, project: Project, settings: Settings, actor: User) -> engine.RunSummary:
    summary = engine.start_run(db, project, settings, actor=actor)
    db.commit()
    return summary


def _drain(db: Session, settings: Settings) -> worker.TickResult:
    db.commit()
    result = worker.drain(settings)
    db.expire_all()
    return result


def _tasks(db: Session, project: Project) -> dict[TaskKind, list[Task]]:
    grouped: dict[TaskKind, list[Task]] = {}
    for task in db.execute(select(Task).where(Task.project_id == project.id)).scalars():
        grouped.setdefault(task.kind, []).append(task)
    return grouped


def _one(db: Session, project: Project, kind: TaskKind) -> Task:
    tasks = _tasks(db, project).get(kind, [])
    assert len(tasks) == 1, f"expected exactly one {kind.value} task, got {len(tasks)}"
    return tasks[0]


def _gate_task(db: Session, project: Project, gate_key: ApprovalGateKey) -> Task:
    for task in _tasks(db, project).get(TaskKind.EVALUATE_APPROVAL_GATE, []):
        if task.payload.get("gate_key") == gate_key.value:
            return task
    raise AssertionError(f"no gate task for {gate_key.value}")


def _pending(db: Session, project: Project) -> list[Approval]:
    return list(
        db.execute(
            select(Approval).where(
                Approval.project_id == project.id,
                Approval.status == ApprovalStatus.PENDING,
            )
        ).scalars()
    )


def _settle_all_approvals(db: Session, project: Project, settings: Settings, reviewer: User) -> int:
    """Decide everything in front of a human, the way the product decides it.

    A gate decision goes through :func:`app.engineering.gates.record_decision`,
    which is the only thing that settles a gate. Everything else — the findings
    and the release candidate — is settled the way the approvals endpoint settles
    it, since that is what a reviewer clicking approve actually does.
    """
    from app.engineering import gates

    decided = 0
    for approval in _pending(db, project):
        finding = db.get(Finding, approval.finding_id) if approval.finding_id else None
        if approval.finding_id is not None and (finding is None or not finding.is_reviewed):
            # Matches the approvals endpoint: the pending row is visible, but a
            # reviewer cannot decide it before independent senior review.
            continue
        decided += 1
        if approval.kind is ApprovalKind.GATE_DECISION and approval.gate_key is not None:
            gate = db.execute(
                select(ApprovalGate).where(
                    ApprovalGate.project_id == project.id,
                    ApprovalGate.gate_key == approval.gate_key,
                )
            ).scalar_one()
            gates.record_decision(
                db,
                gate=gate,
                approval=approval,
                decision=ApprovalDecision.APPROVE,
                user=reviewer,
                comment="Checked against the functional description.",
            )
            continue

        approval.status = ApprovalStatus.APPROVED
        approval.decided_by_id = reviewer.id
        approval.decided_at = utcnow()
        approval.decision_notes = "Reviewed."
        if finding is not None:
            finding.status = FindingStatus.ACCEPTED
            finding.decided_by_id = reviewer.id
            finding.decided_at = utcnow()
    db.flush()

    engine.unblock_approval_tasks(db, project, settings)
    engine.refresh_project_status(db, project)
    db.commit()
    return decided


def _answer_open_questions(db: Session, project: Project, settings: Settings) -> int:
    """Answer every generation-blocking clarification, the way a human would.

    Clearing ``blocks_generation`` is the recorded act of somebody deciding the
    work can proceed either way — the question and its answer stay on the row.
    Returning the checkpoint task to the queue is what an endpoint would do once
    the answer landed; nothing in this backend does that yet, so the test does it
    explicitly rather than pretending the question was never asked.
    """
    from app.models.requirements import Clarification
    from app.orchestration.queue import DatabaseTaskQueue

    answered = 0
    for item in db.execute(
        select(Clarification).where(
            Clarification.project_id == project.id,
            Clarification.blocks_generation.is_(True),
        )
    ).scalars():
        item.blocks_generation = False
        item.resolution = "Answered by the line engineer during the fixture run."
        answered += 1
    if not answered:
        return 0

    queue = DatabaseTaskQueue(db, settings)
    for task in db.execute(
        select(Task).where(
            Task.project_id == project.id,
            Task.kind == TaskKind.RESOLVE_CLARIFICATIONS,
            Task.status == TaskStatus.BLOCKED,
        )
    ).scalars():
        queue.unblock(task)
    db.commit()
    return answered


def _settle_proposed_requirements(db: Session, project: Project, _reviewer: User) -> int:
    """Model the named human decision endpoint used by the real workflow."""
    proposed = list(
        db.execute(
            select(Requirement).where(
                Requirement.project_id == project.id,
                Requirement.status == RequirementStatus.PROPOSED,
                Requirement.deleted_at.is_(None),
            )
        ).scalars()
    )
    for requirement in proposed:
        # The fixture already carries REQ-001 as the accepted design basis. The
        # deterministic extractor's schedule transcriptions are reviewed here
        # and rejected rather than silently promoted into new control-pattern
        # selections the mock provider is deliberately forbidden to make.
        requirement.status = RequirementStatus.REJECTED
        requirement.updated_in_revision = project.current_revision_number
    if proposed:
        db.commit()
    return len(proposed)


def _round(db: Session, project: Project, settings: Settings, reviewer: User) -> int:
    """One turn of the loop a person drives a production run with.

    Work until nothing is runnable, answer whatever was asked, decide whatever
    was raised. Returns how much a human had to do, so a caller can tell a run
    that is progressing from one that has settled.
    """
    _drain(db, settings)
    db.expire_all()
    moved = _answer_open_questions(db, project, settings)
    moved += _settle_proposed_requirements(db, project, reviewer)
    if _pending(db, project):
        moved += _settle_all_approvals(db, project, settings, reviewer)
    db.expire_all()
    return moved


def _run_to_completion(
    db: Session, project: Project, settings: Settings, reviewer: User, *, rounds: int = 15
) -> None:
    """Drive the run until nothing is waiting on a human any more.

    ``rounds`` is a backstop — a run needing more than a dozen turns is going
    round in circles, and the assertions that follow should say so rather than
    hang.
    """
    for _ in range(rounds):
        if not _round(db, project, settings, reviewer):
            return
    _drain(db, settings)


# ---------------------------------------------------------------------------
# Which chain gets planned
# ---------------------------------------------------------------------------
def test_a_complete_profile_plans_the_production_sequence(
    db: Session, production_project: Project, seeded: dict[str, User], settings: Settings
) -> None:
    summary = _start(db, production_project, settings, seeded["engineer"])
    planned = {task.kind for task in summary.tasks}

    assert summary.workflow.is_production is True
    assert summary.workflow.key == "production"
    assert summary.workflow.reason, "a choice with no stated reason cannot be explained later"
    for kind in (
        TaskKind.EXTRACT_REQUIREMENTS,
        TaskKind.RESOLVE_CLARIFICATIONS,
        TaskKind.DESIGN_TAGS,
        TaskKind.DESIGN_PLC_ARCHITECTURE,
        TaskKind.GENERATE_LOGIC,
        TaskKind.VALIDATE_CROSS_DISCIPLINE,
        TaskKind.EVALUATE_APPROVAL_GATE,
        TaskKind.GENERATE_RELEASE_CANDIDATE,
        TaskKind.VERIFY_IMPORT,
        TaskKind.ASSEMBLE_PACKAGE,
    ):
        assert kind in planned, f"the production plan is missing {kind.value}"

    gate_keys = {
        task.payload["gate_key"]
        for task in summary.tasks
        if task.kind is TaskKind.EVALUATE_APPROVAL_GATE
    }
    assert gate_keys == {key.value for key in ApprovalGateKey}, (
        "every gate has to be a task, or the run walks past the ones that are missing"
    )

    seniors = [task for task in summary.tasks if task.kind is TaskKind.SENIOR_REVIEW]
    assert {task.payload.get("review_pass") for task in seniors} == {"initial", "final"}
    initial = next(task for task in seniors if task.payload.get("review_pass") == "initial")
    final = next(task for task in seniors if task.payload.get("review_pass") == "final")
    plan = next(task for task in summary.tasks if task.kind is TaskKind.PLAN_PROJECT)
    validation = next(
        task for task in summary.tasks if task.idempotency_key.endswith(":validation")
    )
    qa_gate = next(
        task
        for task in summary.tasks
        if task.kind is TaskKind.EVALUATE_APPROVAL_GATE
        and task.payload.get("gate_key") == ApprovalGateKey.QA_AND_RISK.value
    )
    assert plan.payload["senior_review_task_id"] == str(initial.id)
    assert {edge.depends_on_id for edge in final.dependencies} == {validation.id}
    assert {edge.depends_on_id for edge in qa_gate.dependencies} == {final.id}

    logic = next(task for task in summary.tasks if task.kind is TaskKind.GENERATE_LOGIC)
    delivery = next(task for task in summary.tasks if task.kind is TaskKind.DELIVERY)
    logic_validation = next(
        task for task in summary.tasks if task.idempotency_key.endswith(":validate-logic")
    )
    logic_gate = next(
        task
        for task in summary.tasks
        if task.kind is TaskKind.EVALUATE_APPROVAL_GATE
        and task.payload.get("gate_key") == ApprovalGateKey.LOGIC_AND_DELIVERY.value
    )
    assert {edge.depends_on_id for edge in delivery.dependencies} == {logic.id}
    assert {edge.depends_on_id for edge in logic_validation.dependencies} == {delivery.id}
    assert {edge.depends_on_id for edge in logic_gate.dependencies} == {logic_validation.id}


def test_planned_specialists_are_stable_members_of_the_parent_run_and_block_senior_review(
    db: Session, production_project: Project, seeded: dict[str, User], settings: Settings
) -> None:
    """A retried plan converges, and senior review cannot outrun its specialists."""
    from app.agents.review.handlers import plan_project
    from app.orchestration.handlers import TaskContext

    summary = _start(db, production_project, settings, seeded["engineer"])
    _drain(db, settings)
    db.expire_all()

    plan = _one(db, production_project, TaskKind.PLAN_PROJECT)
    initial = next(
        task
        for task in _tasks(db, production_project)[TaskKind.SENIOR_REVIEW]
        if task.payload.get("review_pass") == "initial"
    )
    planned_ids = {uuid.UUID(value) for value in plan.result["planned_task_ids"]}
    planned = list(db.execute(select(Task).where(Task.id.in_(planned_ids))).scalars())

    assert planned
    assert {task.run_id for task in planned} == {summary.run_id}
    assert planned_ids <= {edge.depends_on_id for edge in initial.dependencies}
    assert all(edge.hard for edge in initial.dependencies if edge.depends_on_id in planned_ids)

    before = {task.kind: (task.id, task.idempotency_key) for task in planned}
    retried = plan_project(
        TaskContext(db=db, task=plan, project=production_project, settings=settings)
    )
    db.flush()
    after = {
        task.kind: (task.id, task.idempotency_key)
        for task in db.execute(select(Task).where(Task.id.in_(planned_ids))).scalars()
    }
    dependencies = list(
        db.execute(select(TaskDependency).where(TaskDependency.task_id == initial.id)).scalars()
    )

    assert retried.status == "completed"
    assert after == before
    assert len({edge.depends_on_id for edge in dependencies}) == len(dependencies)


def test_an_incomplete_profile_plans_the_review_sequence(
    db: Session, ingested_project: Project, seeded: dict[str, User], settings: Settings
) -> None:
    """An incomplete profile is a review project, and the reason names the fields."""
    _profile(
        db,
        ingested_project,
        is_complete_for_generation=False,
        missing_required_fields=["naming_standard_version", "template_set_id"],
    )
    db.commit()

    summary = _start(db, ingested_project, settings, seeded["engineer"])
    planned = {task.kind for task in summary.tasks}

    assert summary.workflow.is_production is False
    assert "naming_standard_version" in summary.workflow.reason
    assert planned.isdisjoint(engine.PRODUCTION_TASK_KINDS), (
        "a project that may not generate must not be handed a generation step at all"
    )
    assert planned == {
        TaskKind.INVENTORY_FILES,
        TaskKind.PARSE_FILE,
        TaskKind.BUILD_EVIDENCE_GRAPH,
        TaskKind.PLAN_PROJECT,
        TaskKind.SENIOR_REVIEW,
        TaskKind.REQUEST_APPROVALS,
        TaskKind.DELIVERY,
        TaskKind.FINALIZE_PROJECT,
    }


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"release_mode": ReleaseMode.REVIEW_ONLY}, "read and report"),
        (
            {
                "restricted_domains": [RestrictedDomain.SAFETY_LOGIC.value],
                "restricted_domain_mode": RestrictedDomainMode.MANUAL_REVIEW_ONLY,
            },
            "restricted domain",
        ),
    ],
)
def test_each_reason_a_profile_forbids_generation_is_named(
    db: Session,
    ingested_project: Project,
    settings: Settings,
    overrides: dict[str, object],
    expected: str,
) -> None:
    """ "The profile blocks generation" is not something an engineer can act on."""
    _profile(db, ingested_project, **overrides)
    db.commit()

    choice = engine.choose_workflow(db, ingested_project)

    assert choice.is_production is False
    assert expected in choice.reason


def test_a_project_with_no_profile_runs_the_review_workflow(
    db: Session, ingested_project: Project
) -> None:
    choice = engine.choose_workflow(db, ingested_project)

    assert choice.is_production is False
    assert "no engineering profile" in choice.reason


# ---------------------------------------------------------------------------
# What an agent's proposal does to the canonical model
# ---------------------------------------------------------------------------
def test_an_agents_change_set_is_merged_and_creates_a_revision(
    db: Session, production_project: Project, seeded: dict[str, User], settings: Settings
) -> None:
    """The whole point of the production pipeline, in one assertion.

    An agent proposed, deterministic code merged, and the result is a numbered
    revision carrying objects that cite where they came from.
    """
    before = production_project.current_revision_number
    _start(db, production_project, settings, seeded["engineer"])
    _drain(db, settings)
    db.refresh(production_project)

    task = _one(db, production_project, TaskKind.EXTRACT_REQUIREMENTS)
    assert task.status is TaskStatus.COMPLETED, task.blocked_reason or task.error_message
    handoffs = task.result["action_handoffs"]
    assert len(handoffs) == 1
    assert handoffs[0]["action_type"] == ActionType.PROPOSE_CHANGESET.value
    assert handoffs[0]["summary"]
    assert handoffs[0]["handler"] == "production design task handler"
    assert all(
        item["action_type"] != ActionType.PROPOSE_CHANGESET.value
        for item in task.result["unexecuted"]
    )

    merged = [
        item
        for item in db.execute(
            select(ChangeSet).where(ChangeSet.project_id == production_project.id)
        ).scalars()
        if item.merged_revision_number is not None
    ]
    assert merged, "the requirements agent proposed nothing that reached the model"
    assert merged[0].author_agent == "requirements_planning"
    assert production_project.current_revision_number > before

    revisions = list(
        db.execute(
            select(ProjectRevision).where(ProjectRevision.project_id == production_project.id)
        ).scalars()
    )
    assert any(item.number == merged[0].merged_revision_number for item in revisions)

    requirements = list(
        db.execute(
            select(Requirement).where(
                Requirement.project_id == production_project.id,
                Requirement.reference != "REQ-001",
            )
        ).scalars()
    )
    assert requirements, "no requirement was recorded"
    assert all(item.status is RequirementStatus.PROPOSED for item in requirements), (
        "an agent may not accept its own reading of the brief"
    )
    assert all(item.source_text for item in requirements), (
        "a requirement with no source text cannot be checked against what was written"
    )


def _rename_builder(context: dict[str, object]) -> dict[str, object]:
    """A tag-architecture turn that proposes a rename.

    Substituted for the deterministic provider's own rule, which does not
    rename: a rename is the archetypal change nobody may make without a human,
    and the deterministic provider is deliberately incapable of proposing one.
    """
    tags = context["existing_tags"]
    assert isinstance(tags, list)
    tag = next(item for item in tags if item["name"] == f"{DEVICE_PREFIX}_Run_Cmd")
    return {
        "rationale": "The command tag does not follow the standard's verb-last form.",
        "actions": [
            {
                "action_type": "propose_changeset",
                "summary": "Rename the drive command tag to the standard form.",
                "confidence": 0.4,
                "payload": {
                    "title": "Rename the drive command tag",
                    "summary": "Align the command tag with the naming standard.",
                    "base_revision_number": context["base_revision_number"],
                    "requires_human_approval": True,
                    "operations": [
                        {
                            "operation_type": "rename_tag",
                            "object_type": "tag",
                            "object_id": tag["id"],
                            "based_on_object_revision": tag["object_revision"],
                            "values": {
                                "from_name": tag["name"],
                                "to_name": f"{DEVICE_PREFIX}_Cmd_Run",
                            },
                        }
                    ],
                },
            }
        ],
    }


def test_a_change_set_needing_approval_parks_its_task_and_the_project(
    db: Session,
    production_project: Project,
    seeded: dict[str, User],
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A merge that returns ``approval_required`` must never look like success.

    The design task holds at ``awaiting_approval``, the project is not complete,
    and — the assertion that matters — the tag has not moved. A rename reaches
    into logic, HMI bindings, alarms, tests and documentation, and it may not
    happen on an agent's own authority.
    """
    from app.agents.base.providers import mock

    monkeypatch.setitem(mock._BUILDERS, "tag_architecture", _rename_builder)

    _start(db, production_project, settings, seeded["engineer"])
    # Four turns: past the clarification checkpoint, past gate 1, into the tag
    # design, and one more so a task that was going to move has moved.
    for _ in range(4):
        _round(db, production_project, settings, seeded["reviewer"])
    db.refresh(production_project)

    task = _one(db, production_project, TaskKind.DESIGN_TAGS)
    assert task.status is TaskStatus.AWAITING_APPROVAL, (
        f"{task.status.value}: {task.blocked_reason or task.error_message}"
    )
    assert task.blocked_reason, "a task parked without a reason looks like a stalled one"
    assert production_project.status is ProjectStatus.AWAITING_REVIEW
    assert production_project.completed_at is None

    changeset = db.execute(
        select(ChangeSet).where(
            ChangeSet.project_id == production_project.id,
            ChangeSet.author_agent == "tag_architecture",
        )
    ).scalar_one()
    assert changeset.merged_revision_number is None
    assert changeset.requires_human_approval is True

    unchanged = db.execute(
        select(Tag).where(
            Tag.project_id == production_project.id,
            Tag.name == f"{DEVICE_PREFIX}_Run_Cmd",
        )
    ).scalar_one_or_none()
    assert unchanged is not None, "the tag was renamed without a human decision"


# ---------------------------------------------------------------------------
# Gates
# ---------------------------------------------------------------------------
def test_a_gate_blocks_the_tasks_behind_it_and_releases_them_once_decided(
    db: Session, production_project: Project, seeded: dict[str, User], settings: Settings
) -> None:
    """The gate is where the run stops, and the stop has to be visible.

    Before the decision: the gate task is awaiting approval and the design task
    behind it has not started. After it: both have moved.
    """
    _start(db, production_project, settings, seeded["engineer"])
    # One turn to get past the clarification checkpoint, then work until the
    # gate task itself has run.
    _round(db, production_project, settings, seeded["reviewer"])
    _drain(db, settings)
    db.expire_all()

    gate_task = _gate_task(db, production_project, ApprovalGateKey.PROJECT_PROFILE)
    design = _one(db, production_project, TaskKind.DESIGN_TAGS)

    assert gate_task.status is TaskStatus.AWAITING_APPROVAL
    assert design.status is TaskStatus.QUEUED, "design started before its gate was decided"
    assert design.attempts == 0

    gate = db.execute(
        select(ApprovalGate).where(
            ApprovalGate.project_id == production_project.id,
            ApprovalGate.gate_key == ApprovalGateKey.PROJECT_PROFILE,
        )
    ).scalar_one()
    assert gate.status.value == "awaiting_decision"

    _settle_all_approvals(db, production_project, settings, seeded["reviewer"])
    _drain(db, settings)
    db.expire_all()

    gate_task = _gate_task(db, production_project, ApprovalGateKey.PROJECT_PROFILE)
    design = _one(db, production_project, TaskKind.DESIGN_TAGS)
    assert gate_task.status is TaskStatus.COMPLETED, gate_task.blocked_reason
    assert design.attempts >= 1, "the design task never ran after its gate was decided"


def test_a_project_naming_an_hmi_reaches_the_logic_gate_with_its_hmi_criterion_met(
    db: Session, production_project: Project, seeded: dict[str, User], settings: Settings
) -> None:
    """The deadlock, asserted from the side that used to hold.

    Gate 3 asks whether the HMI design exists. Nothing in this backend creates
    screens, so the work that can satisfy that question must run *before* the
    gate is evaluated — delivery inspects and reports on the display model, and
    the validation snapshot the gate reads is taken after it. With delivery
    behind the gate instead, every project whose target platform named an HMI
    parked here permanently and the gate's own message said only that no screens
    were recorded.
    """
    from app.engineering import gates

    _start(db, production_project, settings, seeded["engineer"])
    _run_to_completion(db, production_project, settings, seeded["reviewer"])
    db.expire_all()

    platform = db.execute(
        select(TargetPlatform).where(TargetPlatform.project_id == production_project.id)
    ).scalar_one()
    delivery = _one(db, production_project, TaskKind.DELIVERY)
    gate_task = _gate_task(db, production_project, ApprovalGateKey.LOGIC_AND_DELIVERY)
    gate = next(
        item
        for item in gates.evaluate_all(db, project=production_project)
        if item.gate_key is ApprovalGateKey.LOGIC_AND_DELIVERY
    )
    unmet = {str(item.get("key")) for item in gate.unmet_criteria}

    assert platform.hmi_product is not HmiProduct.NONE, (
        "the fixture stopped declaring an HMI, so gate 3's HMI criterion is untested again"
    )
    assert delivery.status is TaskStatus.COMPLETED, (
        delivery.blocked_reason or delivery.error_message
    )
    assert delivery.finished_at is not None
    assert gate_task.finished_at is not None
    assert delivery.finished_at <= gate_task.finished_at, (
        "the gate that asks whether the HMI design exists was decided before the "
        "delivery step that inspects it"
    )
    assert "hmi_screens_recorded" not in unmet, sorted(unmet)
    assert gate.status is GateStatus.PASSED, f"{gate.status.value}: {gate.unmet_criteria}"


def test_an_unanswered_blocking_clarification_stops_the_run_before_design(
    db: Session, production_project: Project, seeded: dict[str, User], settings: Settings
) -> None:
    """The refusal to guess, expressed as a stopped run.

    The Requirements agent raises a clarification for every incomplete interlock
    row in the fixture, so the checkpoint behind it must not pass.
    """
    from app.models.requirements import Clarification

    _start(db, production_project, settings, seeded["engineer"])
    _drain(db, settings)
    db.expire_all()

    open_clarifications = list(
        db.execute(
            select(Clarification).where(
                Clarification.project_id == production_project.id,
                Clarification.blocks_generation.is_(True),
            )
        ).scalars()
    )
    assert open_clarifications, "the fixture's incomplete interlock row raised no question"

    task = _one(db, production_project, TaskKind.RESOLVE_CLARIFICATIONS)
    assert task.status is TaskStatus.BLOCKED
    assert task.blocked_reason
    assert task.result["blocking_clarifications"] == len(open_clarifications)
    assert _one(db, production_project, TaskKind.DESIGN_TAGS).attempts == 0


# ---------------------------------------------------------------------------
# Answering a question, and the two ways a design step produces nothing
# ---------------------------------------------------------------------------
#: An accepted requirement no logic network claims, which is the state every real
#: project starts in. The fixture's ``REQ-001`` is deliberately claimed by a
#: seeded network so the PLC Logic Agent has nothing to ask about; these tests are
#: about the other case.
UNCLAIMED_REFERENCE = "REQ-500"

ANSWER = "Confirmed with the line engineer: the existing Zone 2 rung implements it."


def _unclaimed_requirement(db: Session, project: Project) -> Requirement:
    """A requirement a named human accepted and no rung implements yet."""
    requirement = Requirement(
        project_id=project.id,
        reference=UNCLAIMED_REFERENCE,
        title="Accumulation zone release",
        statement=(
            "Zone 2 shall release accumulated cases once the downstream zone reports clear."
        ),
        source_text="Zone 2 releases accumulated cases when the downstream zone is clear.",
        status=RequirementStatus.ACCEPTED,
    )
    db.add(requirement)
    db.flush()
    return requirement


def _design_stage(db: Session, project: Project, settings: Settings) -> HandlerResult:
    """Run the PLC architecture stage the way the worker runs it.

    Called directly rather than drained, because what is being examined is the
    handler's outcome — the reason and the result it hands the worker — and a
    drained run would only leave the reason on the task row after the fact.
    """
    from app.agents.review.handlers import design_plc_architecture
    from app.orchestration.handlers import TaskContext

    task = _one(db, project, TaskKind.DESIGN_PLC_ARCHITECTURE)
    outcome = design_plc_architecture(
        TaskContext(db=db, task=task, project=project, settings=settings)
    )
    db.flush()
    return outcome


def _questions(db: Session, project: Project) -> list[ProjectQuestion]:
    return list(
        db.execute(
            select(ProjectQuestion)
            .where(ProjectQuestion.project_id == project.id)
            .order_by(ProjectQuestion.reference)
        ).scalars()
    )


def _resolve(
    db: Session,
    project: Project,
    question: ProjectQuestion,
    actor: User,
    settings: Settings,
    *,
    resolution: str = "answered",
) -> None:
    """Answer a question through the door a person answers it through.

    :func:`app.core.services.engineering_intake.resolve_project_question` is what
    the endpoint calls: it records the answer, the decision, who decided it, and
    returns the blocked tasks to the queue. Writing the answer onto the row here
    would prove nothing about whether the run can see it.
    """
    from app.core.services import engineering_intake

    engineering_intake.resolve_project_question(
        db,
        project=project,
        question=question,
        answer=ANSWER,
        resolution=resolution,
        actor=actor,
        settings=settings,
    )
    db.flush()


def test_a_design_step_that_asks_blocks_on_the_question_rather_than_choosing(
    db: Session, production_project: Project, seeded: dict[str, User], settings: Settings
) -> None:
    """The refusal to guess, at the one step whose central act is a judgement.

    The deterministic provider will not decide which approved pattern implements
    a requirement, so the step asks and blocks. This is the *legitimate* block —
    a human owes an answer — and it is asserted separately from the dead end
    below because the whole point is that the two must not read alike.
    """
    _start(db, production_project, settings, seeded["engineer"])
    _unclaimed_requirement(db, production_project)

    outcome = _design_stage(db, production_project, settings)

    assert outcome.status == "blocked", outcome.reason
    assert UNCLAIMED_REFERENCE in outcome.reason, outcome.reason
    assert outcome.result.get("dead_end") is not True, (
        "a question nobody has answered was reported as a dead end"
    )
    asked = _questions(db, production_project)
    assert len(asked) == 1, [item.reference for item in asked]
    assert asked[0].status is ClarificationStatus.OPEN
    assert asked[0].is_blocking, "a question that blocks nothing cannot have stopped this step"


def test_an_answered_question_reaches_the_next_run_as_a_citable_human_decision(
    db: Session, production_project: Project, seeded: dict[str, User], settings: Settings
) -> None:
    """What an agent is handed once a person has answered.

    An answer is evidence, and the three things that make it citable travel with
    it: the words, the name of whoever gave them, and the ``ProjectDecision`` an
    object built on the answer cites as its basis. A dismissal does not travel,
    and that is the second half of this test — dismissing records that the answer
    is no longer needed, never what the answer is, and an agent that read one as
    an answer would be acting on words nobody offered as one.
    """
    from app.agents.base.agent import answered_questions_for

    _start(db, production_project, settings, seeded["engineer"])
    _unclaimed_requirement(db, production_project)
    _design_stage(db, production_project, settings)
    asked = _questions(db, production_project)
    assert len(asked) == 1
    _resolve(db, production_project, asked[0], seeded["reviewer"], settings)

    answers = answered_questions_for(db, production_project.id)

    assert [item.reference for item in answers] == [asked[0].reference]
    answer = answers[0]
    assert answer.answer == ANSWER
    assert answer.answered_by == seeded["reviewer"].display_name, (
        "an answer nobody is named for is not a human decision"
    )
    assert answer.decision_reference.startswith("DEC-"), (
        f"the answer cites no recorded decision: {answer.decision_reference!r}"
    )
    assert answer.as_dict()["basis_kind"] == EvidenceKind.HUMAN_DECISION.value
    assert UNCLAIMED_REFERENCE in answer.question, (
        "the answer arrives without the question, so nothing can tell what it is about"
    )

    dismissed = ProjectQuestion(
        project_id=production_project.id,
        reference="Q-DISMISSED",
        category=QuestionCategory.TEMPLATE_SELECTION,
        question=f"Which approved template implements {UNCLAIMED_REFERENCE}?",
        why_it_matters="Nothing is generated for it until a pattern is chosen.",
        created_in_revision=production_project.current_revision_number,
        updated_in_revision=production_project.current_revision_number,
    )
    db.add(dismissed)
    db.flush()
    _resolve(
        db,
        production_project,
        dismissed,
        seeded["reviewer"],
        settings,
        resolution="dismissed",
    )

    assert [item.reference for item in answered_questions_for(db, production_project.id)] == [
        asked[0].reference
    ], "a dismissed question was handed over as though somebody had answered it"


def test_the_answered_question_is_not_asked_again_and_the_step_says_why_it_is_stuck(
    db: Session, production_project: Project, seeded: dict[str, User], settings: Settings
) -> None:
    """The human loop, closed at the provider.

    The deterministic provider now reads the answer. It still selects nothing —
    matching a pattern to a requirement is the judgement it does not make, and the
    recorded answer names no approved template — but it says so *once*, as a
    stated fact naming who answered and what they said, instead of putting the
    same question back in front of the person who answered it.

    Two runs after the answer, not one: the second proves the report is stable.
    A step that alternated between two accounts of the same situation would be a
    loop with better manners.
    """
    _start(db, production_project, settings, seeded["engineer"])
    _unclaimed_requirement(db, production_project)
    first = _design_stage(db, production_project, settings)
    asked = _questions(db, production_project)
    _resolve(db, production_project, asked[0], seeded["reviewer"], settings)

    second = _design_stage(db, production_project, settings)
    third = _design_stage(db, production_project, settings)

    assert [item.reference for item in _questions(db, production_project)] == [
        asked[0].reference
    ], "the answered question was asked again under a new reference"
    assert second.status == "blocked", second.reason
    assert second.reason != first.reason, (
        "the step reported the identical sentence before and after a human answered it"
    )
    assert third.reason == second.reason, (
        f"the report is not stable: {second.reason!r} then {third.reason!r}"
    )
    for expected in (asked[0].reference, ANSWER, seeded["reviewer"].display_name):
        assert expected in second.reason, f"{expected!r} is missing from: {second.reason}"
    assert "no logic was proposed" in second.reason.lower(), second.reason
    assert second.result["blockers"], (
        "the step reported no blocker, so the sentence a human reads came from somewhere else"
    )


def _re_asking_builder(context: dict[str, object]) -> dict[str, object]:
    """A PLC-logic turn that asks the same question however often it is answered.

    Stands in for any provider that does not act on the answers — which is every
    provider until it does, and a model on any given turn. The handler's rule has
    to hold for that provider too: what is being tested here is not that the
    deterministic provider behaves, it is that a step cannot block for ever on a
    question a human has answered, whatever asked it.
    """
    return {
        "rationale": "Asked which approved pattern implements the requirement.",
        "actions": [
            {
                "action_type": "request_clarification",
                "summary": f"Which approved pattern implements {UNCLAIMED_REFERENCE}?",
                "confidence": 0.0,
                "payload": {
                    "category": QuestionCategory.TEMPLATE_SELECTION.value,
                    "question": f"Which approved template implements {UNCLAIMED_REFERENCE}?",
                    "why_it_matters": "No logic is proposed for it until a pattern is chosen.",
                },
            },
            {
                "action_type": "complete_task",
                "summary": "No control pattern was selected.",
                "confidence": 0.0,
                "payload": {
                    "outcome": "Asked which pattern implements the requirement.",
                    "limitations": ["No template was selected and no logic was proposed."],
                },
            },
        ],
    }


def test_a_step_that_re_asks_an_answered_question_reports_a_dead_end_once(
    db: Session,
    production_project: Project,
    seeded: dict[str, User],
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The defect that made the human loop decorative, and where it now stops.

    The question's reference is derived from the task and the payload, so an exact
    retry returns the question already raised — which is right, or a leased task
    running twice would queue the same decision twice. What it must not do is make
    an answered row look newly asked: the answer requeued the task, the agent asked
    the identical question, and the step blocked on the same sentence again, for
    ever, however correctly the human answered.

    It is still a block, and deliberately: a design step that produced nothing is
    not finished, and completing it would mark a design task done that designed
    nothing. What changes is the sentence, which now says the answers are in and
    names what is still missing — the only thing an engineer can act on here.
    """
    from app.agents.base.providers import mock

    monkeypatch.setitem(mock._BUILDERS, "plc_logic", _re_asking_builder)

    _start(db, production_project, settings, seeded["engineer"])
    _unclaimed_requirement(db, production_project)
    first = _design_stage(db, production_project, settings)
    asked = _questions(db, production_project)
    assert len(asked) == 1, [item.reference for item in asked]
    assert first.status == "blocked"
    assert first.result.get("dead_end") is not True

    _resolve(db, production_project, asked[0], seeded["reviewer"], settings)
    second = _design_stage(db, production_project, settings)
    third = _design_stage(db, production_project, settings)

    assert second.status == "blocked", second.reason
    assert second.result["dead_end"] is True, (
        "the step blocked on a question a human had answered as though it were still open"
    )
    assert second.result["questions_already_settled"] == [asked[0].reference]
    assert second.reason != first.reason, (
        "answering the question changed nothing about what the step reported"
    )
    for expected in (asked[0].reference, ANSWER, "No template was selected"):
        assert expected in second.reason, f"{expected!r} is missing from: {second.reason}"
    assert [item["reference"] for item in second.result["settled_questions"]] == [
        asked[0].reference
    ]
    assert third.reason == second.reason, (
        f"the dead end is not reported the same way twice: {second.reason!r} then {third.reason!r}"
    )
    assert [item.reference for item in _questions(db, production_project)] == [
        asked[0].reference
    ], "a second copy of an answered question was raised"


# ---------------------------------------------------------------------------
# The whole run, offline
# ---------------------------------------------------------------------------
def test_the_whole_production_run_completes_offline_on_the_mock_provider(
    db: Session, production_project: Project, seeded: dict[str, User], settings: Settings
) -> None:
    """Intake to sealed manifest, with no network and no model.

    The end state is what matters: a package that exists, is hashed, is honest
    about the level it reached, and was sealed by a named person.
    """
    _start(db, production_project, settings, seeded["engineer"])
    _run_to_completion(db, production_project, settings, seeded["reviewer"])
    db.expire_all()
    db.refresh(production_project)

    unfinished = [
        f"{task.kind.value}: {task.status.value} — "
        f"{task.blocked_reason or task.error_message or ''}"
        for tasks in _tasks(db, production_project).values()
        for task in tasks
        if not task.is_terminal
    ]
    assert not unfinished, "; ".join(unfinished)
    assert production_project.status is ProjectStatus.COMPLETED

    candidate = db.execute(
        select(ReleaseCandidate).where(ReleaseCandidate.project_id == production_project.id)
    ).scalar_one()
    assert candidate.status is ReleaseCandidateStatus.RELEASED
    assert candidate.artifacts
    assert candidate.package_sha256
    # Never verified, because nothing here could verify it, and the candidate
    # says so rather than leaving a blank that reads as a pass.
    assert candidate.import_verification_status is ImportVerificationStatus.UNAVAILABLE
    assert candidate.import_verification_detail
    assert candidate.validation_level is not ValidationLevel.ROCKWELL_IMPORT_VERIFIED
    assert candidate.manifest is not None
    assert candidate.manifest.frozen_by_id == seeded["reviewer"].id
    archive = db.execute(
        select(ReleaseArchive).where(ReleaseArchive.release_candidate_id == candidate.id)
    ).scalar_one()
    assert archive.approved_by_id == seeded["reviewer"].id
    assert archive.sha256
    assert archive.size_bytes > 0


def test_the_run_advances_the_project_through_the_engineering_phases(
    db: Session, production_project: Project, seeded: dict[str, User], settings: Settings
) -> None:
    """A production run reports where it is, not merely that it is running."""
    _start(db, production_project, settings, seeded["engineer"])

    seen: set[ProjectStatus] = set()
    for _ in range(15):
        db.refresh(production_project)
        seen.add(production_project.status)
        moved = _round(db, production_project, settings, seeded["reviewer"])
        db.refresh(production_project)
        seen.add(production_project.status)
        if not moved:
            break

    assert ProjectStatus.RUNNING not in seen, (
        "a production run reported a bare 'running', which says nothing about where it got to"
    )
    assert seen & {
        ProjectStatus.TAG_AND_IO_DESIGN,
        ProjectStatus.PLC_ARCHITECTURE,
        ProjectStatus.IMPLEMENTATION,
        ProjectStatus.RELEASE_VALIDATION,
    }, f"no engineering phase was ever reported: {sorted(item.value for item in seen)}"


# ---------------------------------------------------------------------------
# The review path, unchanged
# ---------------------------------------------------------------------------
def test_the_existing_review_run_is_unaffected(
    db: Session, ingested_project: Project, seeded: dict[str, User], settings: Settings
) -> None:
    """The most important test in this file.

    A project with no profile runs the chain it ran before the production
    pipeline existed: the same tasks, the same statuses, and a bare ``running``
    rather than an engineering phase it never entered.
    """
    summary = _start(db, ingested_project, settings, seeded["engineer"])
    assert summary.workflow.is_production is False

    _drain(db, settings)
    db.expire_all()
    db.refresh(ingested_project)

    grouped = _tasks(db, ingested_project)
    for kind in (
        TaskKind.INVENTORY_FILES,
        TaskKind.BUILD_EVIDENCE_GRAPH,
        TaskKind.PLAN_PROJECT,
        TaskKind.REVIEW_PLC,
        TaskKind.REVIEW_INTERLOCKS,
        TaskKind.SENIOR_REVIEW,
    ):
        assert kind in grouped, f"{kind.value} was never created"
        task = grouped[kind][0]
        assert task.status is TaskStatus.COMPLETED, task.blocked_reason or task.error_message

    assert grouped[TaskKind.REQUEST_APPROVALS][0].status is TaskStatus.AWAITING_APPROVAL
    assert ingested_project.status is ProjectStatus.AWAITING_REVIEW
    assert engine.current_phase([task for tasks in grouped.values() for task in tasks]) is None, (
        "a review run was labelled with a production phase"
    )


def test_a_review_run_reports_running_rather_than_a_phase(
    db: Session, ingested_project: Project, seeded: dict[str, User], settings: Settings
) -> None:
    _start(db, ingested_project, settings, seeded["engineer"])
    db.refresh(ingested_project)

    assert ingested_project.status is ProjectStatus.RUNNING


def test_the_workflow_choice_is_recorded_on_the_project(
    db: Session, production_project: Project, seeded: dict[str, User], settings: Settings
) -> None:
    """A run has to stay explicable after somebody edits the profile."""
    summary = _start(db, production_project, settings, seeded["engineer"])
    db.refresh(production_project)

    recorded = production_project.attributes["workflow"]
    assert recorded["kind"] == "production"
    assert recorded["reason"] == summary.workflow.reason
    assert recorded["run_id"] == str(summary.run_id)


def test_import_verification_reports_unavailable_rather_than_passing(
    db: Session, production_project: Project, seeded: dict[str, User], settings: Settings
) -> None:
    """The place a product like this lies to itself first.

    With no licensed workstation the step completes — the package is still an
    import-ready candidate — but it records ``unavailable`` with the adapter's
    own sentence, never a blank that reads as a pass.
    """
    from app.orchestration.handlers import TaskContext
    from app.orchestration.steps import verify_import

    _start(db, production_project, settings, seeded["engineer"])
    task = _one(db, production_project, TaskKind.VERIFY_IMPORT)

    candidate = ReleaseCandidate(
        project_id=production_project.id,
        revision_number=production_project.current_revision_number,
        candidate_number=1,
        status=ReleaseCandidateStatus.GENERATED,
    )
    db.add(candidate)
    db.flush()

    outcome = verify_import(
        TaskContext(db=db, task=task, project=production_project, settings=settings)
    )

    assert outcome.status == "completed"
    assert outcome.result["import_verification_status"] == (
        ImportVerificationStatus.UNAVAILABLE.value
    )
    assert outcome.result["detail"], "an unavailable verification with no reason is a blank"
    db.refresh(candidate)
    assert candidate.import_verification_status is ImportVerificationStatus.UNAVAILABLE


def test_the_requirements_guard_refuses_a_self_accepted_requirement(
    db: Session, production_project: Project, seeded: dict[str, User], settings: Settings
) -> None:
    """The guard the handler calls, on the proposal it would otherwise record."""
    from app.agents.base.actions import ProposeChangesetPayload
    from app.agents.review.requirements_planning import refusal_reasons

    payload = ProposeChangesetPayload(
        title="Requirements",
        summary="Extracted from the brief.",
        base_revision_number=0,
        operations=[
            {
                "operation_type": "create_object",
                "object_type": ObjectType.REQUIREMENT,
                "temp_key": "req-1",
                "values": {
                    "reference": "REQ-900",
                    "title": "Conveyor stop",
                    "statement": "The conveyor shall stop when the guard door opens.",
                    "status": RequirementStatus.ACCEPTED,
                },
                "basis": [{"kind": "inference", "requirement_reference": "REQ-001"}],
            }
        ],  # type: ignore[arg-type]
    )

    reasons = refusal_reasons(payload)

    assert reasons, "an agent accepted its own requirement and nothing objected"
    assert "REQ-900" in reasons[0]
    assert "status" in reasons[0]
