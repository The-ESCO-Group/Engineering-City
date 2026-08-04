"""The workflow engine.

Owns the project state machine and the shape of a run.

Every run has the same fixed prelude — inventory, one parse task per file, the
evidence graph — and then takes one of two shapes.

**The review workflow**, which is what a project gets when it may not generate
anything:

1. **plan** — the Project Manager Agent decides which specialists to run
2. *(planned)* PLC review, interlock review — enqueued by the plan
3. **senior review** — independently check every finding
4. **approvals** — route what needs a human to a human
5. *(optional)* delivery — GPT-owned plugins, if installed
6. **finalize** — only after the required approvals are decided

**The production workflow**, which adds the engineering pipeline between the
graph and the report: extract requirements, resolve clarifications *(gate 1)*,
design tags and I/O *(gate 2)*, PLC architecture and logic *(gate 3)*, delivery,
senior review, cross-discipline validation *(gate 4)*, approvals, and the
release candidate *(gates 5 and 6)* before it finalises.

Which one a project gets is decided once, at :func:`start_run`, by
:func:`choose_workflow`, from :attr:`ProjectProfile.blocks_generation` — the one
rule that already answers "may this project produce anything". The choice is
recorded rather than inferred later: the profile is editable, and a run has to
stay explicable after somebody changes it.

The prelude and the two chains are fixed because they are mechanical. The
specialist reviews are planned because which are worth running depends on what
was actually uploaded — and the Project Manager Agent is not permitted to draw
engineering conclusions while deciding it.

Project status is owned here and nowhere else. There is no API to set it
directly: a status a client could assign would make the state machine a
suggestion. On a production run it also names the *phase*, which is derived from
the tasks rather than stored, for the same reason the evidence graph is: a phase
column would be a second account of the run that could disagree with the queue.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.clock import utcnow
from app.core.config import Settings
from app.core.enums import (
    AgentKind,
    ApprovalGateKey,
    AuditAction,
    EventType,
    ProjectStatus,
    TaskKind,
    TaskStatus,
)
from app.core.errors import ConflictError
from app.core.logging import get_logger
from app.core.services import parsing
from app.engineering import gates
from app.models.approval import Approval
from app.models.profile import ProjectProfile
from app.models.project import Project
from app.models.task import Task
from app.models.user import User
from app.orchestration.events import record_event
from app.orchestration.queue import DatabaseTaskQueue, EnqueueRequest
from app.security import audit

logger = get_logger(__name__)

#: Project statuses from which a new run may be started.
STARTABLE: frozenset[ProjectStatus] = frozenset(
    {
        ProjectStatus.DRAFT,
        ProjectStatus.PLANNED,
        ProjectStatus.COMPLETED,
        ProjectStatus.FAILED,
        ProjectStatus.AWAITING_REVIEW,
    }
)

#: Task kinds only the production workflow creates. Their presence on a project
#: is what tells the status rule which shape of run it is looking at, so the
#: review path's reported status is untouched by anything below.
PRODUCTION_TASK_KINDS: frozenset[TaskKind] = frozenset(
    {
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
    }
)

#: The engineering phase each task kind works in, for a production run.
#:
#: ``EVALUATE_APPROVAL_GATE`` is deliberately absent. A gate waiting on a
#: reviewer already puts the project in ``AWAITING_REVIEW`` through the approval
#: rule, and a gate that is merely queued has not moved the project on from the
#: phase whose work it closes — so the lookup skips it and reads the next
#: unfinished task instead.
PHASE_BY_TASK_KIND: dict[TaskKind, ProjectStatus] = {
    TaskKind.INVENTORY_FILES: ProjectStatus.INGESTING,
    TaskKind.PARSE_FILE: ProjectStatus.INGESTING,
    TaskKind.BUILD_EVIDENCE_GRAPH: ProjectStatus.INGESTING,
    TaskKind.EXTRACT_REQUIREMENTS: ProjectStatus.INGESTING,
    TaskKind.PLAN_PROJECT: ProjectStatus.PLANNED,
    TaskKind.RESOLVE_CLARIFICATIONS: ProjectStatus.CLARIFICATION_REQUIRED,
    TaskKind.DESIGN_TAGS: ProjectStatus.TAG_AND_IO_DESIGN,
    TaskKind.DESIGN_PLC_ARCHITECTURE: ProjectStatus.PLC_ARCHITECTURE,
    TaskKind.GENERATE_LOGIC: ProjectStatus.IMPLEMENTATION,
    TaskKind.DELIVERY: ProjectStatus.TEST_DEVELOPMENT,
    # Senior review sits between planning and the first gate on a production
    # run — it is part of establishing what the inputs say, before any design
    # begins — so it reports the planning phase rather than the cross-discipline
    # one, which is where the validators run.
    TaskKind.SENIOR_REVIEW: ProjectStatus.PLANNED,
    TaskKind.VALIDATE_CROSS_DISCIPLINE: ProjectStatus.CROSS_DISCIPLINE_REVIEW,
    TaskKind.REQUEST_APPROVALS: ProjectStatus.AWAITING_REVIEW,
    TaskKind.GENERATE_RELEASE_CANDIDATE: ProjectStatus.RELEASE_VALIDATION,
    TaskKind.VERIFY_IMPORT: ProjectStatus.RELEASE_VALIDATION,
    TaskKind.ASSEMBLE_PACKAGE: ProjectStatus.RELEASE_VALIDATION,
    TaskKind.FINALIZE_PROJECT: ProjectStatus.READY_FOR_EXPORT,
}


@dataclass(frozen=True, slots=True)
class WorkflowChoice:
    """Which shape of run a project gets, and the reason it was chosen.

    The reason is not decoration. "This project ran review-only" is a question
    somebody asks months later, usually because a customer expected files, and
    the answer has to name which of the profile's conditions applied rather than
    leaving them to re-derive it from a profile that has since been edited.
    """

    is_production: bool
    reason: str

    @property
    def key(self) -> str:
        return "production" if self.is_production else "review"


def choose_workflow(db: Session, project: Project) -> WorkflowChoice:
    """Whether this project's run may generate, read from its profile.

    :attr:`~app.models.profile.ProjectProfile.blocks_generation` is the single
    rule, and it is asked rather than re-derived. The branches below only name
    *which* of its three conditions applied, because "the profile blocks
    generation" is not something an engineer can act on and "the naming standard
    and template set are not chosen yet" is.
    """
    profile = db.execute(
        select(ProjectProfile).where(
            ProjectProfile.project_id == project.id,
            ProjectProfile.deleted_at.is_(None),
        )
    ).scalar_one_or_none()

    if profile is None:
        return WorkflowChoice(
            is_production=False,
            reason=(
                "This project has no engineering profile, so nothing establishes the "
                "naming standard, template set or standards that generated work would "
                "be governed by. It is reviewed and reported on."
            ),
        )
    if not profile.blocks_generation:
        return WorkflowChoice(
            is_production=True,
            reason=(
                f"The project profile is complete and scoped to "
                f"'{profile.release_mode.value}', so this run designs, generates and "
                f"packages as well as reviewing."
            ),
        )

    if profile.release_mode.value == "review_only":
        detail = "the project is scoped to read and report."
    elif profile.missing_required_fields:
        missing = ", ".join(sorted(str(name) for name in profile.missing_required_fields))
        detail = f"the profile is missing {missing}."
    else:
        domains = ", ".join(sorted(str(name) for name in profile.restricted_domains))
        detail = (
            f"the project is in a restricted domain ({domains}) for which no qualified "
            f"workflow is enabled."
        )
    return WorkflowChoice(
        is_production=False,
        reason=f"This run reviews and reports without generating: {detail}",
    )


@dataclass(slots=True)
class RunSummary:
    run_id: uuid.UUID
    project_id: uuid.UUID
    task_count: int
    tasks: list[Task]
    #: Which workflow was planned, and why. Carried out of ``start_run`` so a
    #: caller can say what it started without asking the profile again.
    workflow: WorkflowChoice = WorkflowChoice(is_production=False, reason="")


def start_run(
    db: Session,
    project: Project,
    settings: Settings,
    *,
    actor: User,
    force_reparse: bool = False,
    file_ids: list[uuid.UUID] | None = None,
    note: str = "",
) -> RunSummary:
    """Create the task graph for a new run.

    Refuses rather than silently restarting when a run is already in flight —
    two concurrent runs over one project would interleave findings and neither
    would be reproducible.

    Which chain gets built is :func:`choose_workflow`'s answer, taken once here
    and recorded on the project, in the audit trail and on the run summary. A
    project that may not generate gets the review chain and never reaches a
    generation step at all: the refusal is structural rather than a check inside
    a task that would otherwise have run.
    """
    if project.is_archived:
        raise ConflictError("Archived projects are read-only.")
    if project.status is ProjectStatus.RUNNING:
        raise ConflictError(
            "A run is already in progress for this project. Cancel it before starting another."
        )
    if project.status not in STARTABLE:
        raise ConflictError(
            f"A run cannot be started while the project is '{project.status.value}'."
        )

    files = parsing.parseable_files(db, project.id)
    if file_ids:
        wanted = set(file_ids)
        files = [record for record in files if record.id in wanted]
    if not files:
        raise ConflictError(
            "This project has no files that can be analysed. Upload engineering "
            "documents before starting a review."
        )

    workflow = choose_workflow(db, project)
    run_id = uuid.uuid4()
    queue = DatabaseTaskQueue(db, settings)
    created: list[Task] = []

    def enqueue(request: EnqueueRequest) -> Task:
        task = queue.enqueue(project.id, request)
        created.append(task)
        return task

    # The run id is part of every idempotency key so a second run over the same
    # files creates a fresh graph instead of colliding with the first.
    prefix = f"{run_id.hex[:8]}"

    intake = enqueue(
        EnqueueRequest(
            kind=TaskKind.INVENTORY_FILES,
            title="Inventory uploaded files",
            description="Record what was supplied and name any expected input that was not.",
            idempotency_key=f"{prefix}:intake",
            agent_kind=AgentKind.PROJECT_MANAGER.value,
            run_id=run_id,
            priority=10,
        )
    )

    parse_tasks: list[Task] = []
    for record in files:
        parse_tasks.append(
            enqueue(
                EnqueueRequest(
                    kind=TaskKind.PARSE_FILE,
                    title=f"Parse {record.original_filename}",
                    description=f"Extract normalised content and evidence anchors from "
                    f"{record.original_filename}.",
                    idempotency_key=f"{prefix}:parse:{record.id}",
                    payload={"file_id": str(record.id), "force": force_reparse},
                    run_id=run_id,
                    priority=20,
                    depends_on=[intake.id],
                )
            )
        )

    graph = enqueue(
        EnqueueRequest(
            kind=TaskKind.BUILD_EVIDENCE_GRAPH,
            title="Build the project evidence graph",
            description="Join every parsed artifact into one cross-file structure.",
            idempotency_key=f"{prefix}:graph",
            run_id=run_id,
            priority=30,
            # Soft: one unreadable document must not stop the review of the rest.
            # The graph records that the file was unreadable instead.
            depends_on=[],
        )
    )
    for task in parse_tasks:
        queue.add_dependency(graph, task.id, hard=False)

    if workflow.is_production:
        _plan_production(enqueue, run_id=run_id, prefix=prefix, graph_id=graph.id)
    else:
        _plan_review(enqueue, run_id=run_id, prefix=prefix, graph_id=graph.id)

    # The phase the run starts in, not a bare "running": on a production run the
    # first unfinished task is intake, and reporting that is what makes the phase
    # sequence continuous from the first tick rather than starting one step late.
    project.status = current_phase(created) or ProjectStatus.RUNNING
    project.is_paused = False
    project.paused_at = None
    project.failure_reason = ""
    project.completed_at = None
    # Written to the project because "which workflow is this project on" is a
    # question the UI, a later run and a post-mortem all ask, and re-deriving it
    # from the profile would answer with what the profile says *now* rather than
    # with what this run was planned from. Non-authoritative by construction:
    # ``blocks_generation`` remains the rule, and this is the record of when it
    # was last asked.
    project.attributes = {
        **project.attributes,
        "workflow": {
            "kind": workflow.key,
            "reason": workflow.reason,
            "run_id": str(run_id),
            "chosen_at": utcnow().isoformat(),
        },
    }
    db.flush()

    audit.record(
        db,
        AuditAction.ANALYSIS_STARTED,
        actor_user_id=actor.id,
        project_id=project.id,
        target_type="project",
        target_id=project.id,
        summary=f"Started a {workflow.key} run over {len(files)} file(s)",
        details={
            "run_id": str(run_id),
            "workflow": workflow.key,
            "workflow_reason": workflow.reason,
            "file_count": len(files),
            "task_count": len(created),
            "force_reparse": force_reparse,
            "note": note,
        },
    )
    record_event(
        db,
        project_id=project.id,
        event_type=EventType.WORKFLOW_STARTED,
        actor_user_id=actor.id,
        payload={
            "run_id": str(run_id),
            "task_count": len(created),
            # The note when the caller gave one, and otherwise the reason this
            # run has the shape it does. An empty reason on the one event that
            # announces a run would leave a review-only run looking like a
            # production run that failed to generate.
            "reason": note or workflow.reason,
        },
    )
    for task in created:
        record_event(
            db,
            project_id=project.id,
            event_type=EventType.TASK_CREATED,
            task_id=task.id,
            payload={
                "task_id": str(task.id),
                "kind": task.kind.value,
                "title": task.title,
                "status": task.status.value,
                "agent_kind": task.agent_kind or "",
            },
        )

    logger.info(
        "workflow run started",
        extra={
            "project_id": str(project.id),
            "run_id": str(run_id),
            "workflow": workflow.key,
            "workflow_reason": workflow.reason,
            "tasks": len(created),
            "files": len(files),
        },
    )
    return RunSummary(
        run_id=run_id,
        project_id=project.id,
        task_count=len(created),
        tasks=created,
        workflow=workflow,
    )


# ---------------------------------------------------------------------------
# The two chains
# ---------------------------------------------------------------------------
#: What ``_plan_review`` and ``_plan_production`` are handed: the enqueue that
#: records each task on the run, so neither has to know about the queue.
Enqueue = Callable[[EnqueueRequest], Task]


def _plan_review(enqueue: Enqueue, *, run_id: uuid.UUID, prefix: str, graph_id: uuid.UUID) -> None:
    """Plan, review, check, route, deliver, finalise.

    Unchanged since before the production pipeline existed, and deliberately so:
    a project that may not generate must run exactly what it ran before.
    """
    plan = enqueue(
        EnqueueRequest(
            kind=TaskKind.PLAN_PROJECT,
            title="Plan the review",
            description="Decide which specialist reviews the supplied evidence supports.",
            idempotency_key=f"{prefix}:plan",
            agent_kind=AgentKind.PROJECT_MANAGER.value,
            run_id=run_id,
            priority=40,
            depends_on=[graph_id],
        )
    )

    # The specialist tasks are enqueued by the plan handler, which is why senior
    # review depends on the plan rather than on tasks that do not exist yet. The
    # plan adds the specialists as dependencies of this task as it creates them.
    senior = enqueue(
        EnqueueRequest(
            kind=TaskKind.SENIOR_REVIEW,
            title="Senior review of all findings",
            description="Independently check every finding before it reaches a human.",
            idempotency_key=f"{prefix}:senior",
            agent_kind=AgentKind.SENIOR_REVIEW.value,
            run_id=run_id,
            priority=70,
            depends_on=[plan.id],
        )
    )
    plan.payload = {
        **plan.payload,
        "senior_review_task_id": str(senior.id),
    }

    approvals = enqueue(
        EnqueueRequest(
            kind=TaskKind.REQUEST_APPROVALS,
            title="Route findings for human approval",
            description="Raise an approval for everything that needs an engineer's decision.",
            idempotency_key=f"{prefix}:approvals",
            agent_kind=AgentKind.SENIOR_REVIEW.value,
            run_id=run_id,
            priority=80,
            depends_on=[senior.id],
        )
    )

    delivery = enqueue(
        EnqueueRequest(
            kind=TaskKind.DELIVERY,
            title="Produce reports and testing documentation",
            description="Run installed delivery agents over the approved findings.",
            idempotency_key=f"{prefix}:delivery",
            agent_kind=AgentKind.DELIVERY.value,
            run_id=run_id,
            priority=90,
            depends_on=[approvals.id],
        )
    )

    enqueue(
        EnqueueRequest(
            kind=TaskKind.FINALIZE_PROJECT,
            title="Finalise the review",
            description="Complete the project once the required approvals are decided.",
            idempotency_key=f"{prefix}:finalize",
            run_id=run_id,
            priority=100,
            depends_on=[delivery.id],
        )
    )


def _plan_production(
    enqueue: Enqueue, *, run_id: uuid.UUID, prefix: str, graph_id: uuid.UUID
) -> None:
    """The engineering pipeline, with a gate task between each stage and the next.

    A gate is a task rather than a check inside the stage that follows it,
    because a gate is where a run *stops*: the tasks behind it depend on it, so
    an undecided gate leaves them queued and visible instead of letting the run
    walk into work nobody has authorised. It also means the reason the run
    stopped is on a row a UI can render, with the gate's unmet criteria on it.

    Three orderings are worth stating.

    The requirements extraction runs beside the planner rather than after it —
    they read the same evidence and neither needs the other's output — and the
    clarification checkpoint waits for both, because a question raised by either
    has to be answered before design starts.

    **Senior review runs before the first gate**, which is the one place this
    chain departs from a straight reading of the pipeline order. Senior review
    exists to check every finding *before it reaches a human*, and the specialist
    reviews the planner enqueues raise their approvals the moment they write a
    finding. Put senior review after the gates and a reviewer is asked to decide
    findings nobody has independently checked — and worse, the executor then
    refuses to re-verdict a finding a named engineer has already ruled on, so
    those findings stay unreviewed for the rest of the run and the finalise step
    blocks on them forever. It waits on the plan, which is what creates the
    specialists; they outrank it on priority, so they run first.

    Delivery therefore follows senior review rather than preceding it. It cannot
    move earlier: it is gated on the logic being approved, and that gate is after
    the profile gate senior review has to precede.
    """
    requirements = enqueue(
        EnqueueRequest(
            kind=TaskKind.EXTRACT_REQUIREMENTS,
            title="Extract the requirements",
            description="Turn the brief and the supporting documents into a traceable "
            "requirements model, citing where each item was read from.",
            idempotency_key=f"{prefix}:requirements",
            agent_kind=AgentKind.REQUIREMENTS_PLANNING.value,
            run_id=run_id,
            priority=35,
            depends_on=[graph_id],
        )
    )

    plan = enqueue(
        EnqueueRequest(
            kind=TaskKind.PLAN_PROJECT,
            title="Plan the review",
            description="Decide which specialist reviews the supplied evidence supports.",
            idempotency_key=f"{prefix}:plan",
            agent_kind=AgentKind.PROJECT_MANAGER.value,
            run_id=run_id,
            priority=40,
            depends_on=[graph_id],
        )
    )

    clarifications = enqueue(
        EnqueueRequest(
            kind=TaskKind.RESOLVE_CLARIFICATIONS,
            title="Check that the blocking questions are answered",
            description="Hold the run until every mandatory question and every "
            "generation-blocking clarification has an answer.",
            idempotency_key=f"{prefix}:clarifications",
            run_id=run_id,
            priority=45,
            depends_on=[requirements.id, plan.id],
        )
    )
    senior = enqueue(
        EnqueueRequest(
            kind=TaskKind.SENIOR_REVIEW,
            title="Senior review of all findings",
            description="Independently check every finding before it reaches a human.",
            idempotency_key=f"{prefix}:senior",
            agent_kind=AgentKind.SENIOR_REVIEW.value,
            payload={"review_pass": "initial"},
            run_id=run_id,
            priority=56,
            depends_on=[plan.id, clarifications.id],
        )
    )
    plan.payload = {
        **plan.payload,
        "senior_review_task_id": str(senior.id),
    }

    profile_gate = _gate_task(
        enqueue,
        ApprovalGateKey.PROJECT_PROFILE,
        run_id=run_id,
        prefix=prefix,
        priority=58,
        depends_on=[senior.id],
    )

    tags = enqueue(
        EnqueueRequest(
            kind=TaskKind.DESIGN_TAGS,
            title="Design the tag and I/O model",
            description="Propose the canonical tag vocabulary every other discipline "
            "binds to, under the project's naming standard.",
            idempotency_key=f"{prefix}:tags",
            agent_kind=AgentKind.TAG_ARCHITECTURE.value,
            run_id=run_id,
            priority=60,
            depends_on=[profile_gate.id],
        )
    )
    tag_validation = _validation_task(
        enqueue,
        title="Validate the tag and I/O model",
        key="validate-tags",
        run_id=run_id,
        prefix=prefix,
        priority=62,
        depends_on=[tags.id],
    )
    tag_gate = _gate_task(
        enqueue,
        ApprovalGateKey.DEVICES_IO_TAGS,
        run_id=run_id,
        prefix=prefix,
        priority=64,
        depends_on=[tag_validation.id],
    )

    architecture = enqueue(
        EnqueueRequest(
            kind=TaskKind.DESIGN_PLC_ARCHITECTURE,
            title="Design the controller organisation",
            description="Propose the tasks, programs and routines the logic will live in.",
            idempotency_key=f"{prefix}:architecture",
            agent_kind=AgentKind.PLC_LOGIC.value,
            payload={"stage": TaskKind.DESIGN_PLC_ARCHITECTURE.value},
            run_id=run_id,
            priority=66,
            depends_on=[tag_gate.id],
        )
    )
    logic = enqueue(
        EnqueueRequest(
            kind=TaskKind.GENERATE_LOGIC,
            title="Generate the ladder logic",
            description="Select an approved control pattern per network and supply its "
            "parameters; deterministic code expands them into rungs.",
            idempotency_key=f"{prefix}:logic",
            agent_kind=AgentKind.PLC_LOGIC.value,
            payload={"stage": TaskKind.GENERATE_LOGIC.value},
            run_id=run_id,
            priority=68,
            depends_on=[architecture.id],
        )
    )
    logic_validation = _validation_task(
        enqueue,
        title="Validate the logic and delivery model",
        key="validate-logic",
        run_id=run_id,
        prefix=prefix,
        priority=70,
        depends_on=[logic.id],
    )
    logic_gate = _gate_task(
        enqueue,
        ApprovalGateKey.LOGIC_AND_DELIVERY,
        run_id=run_id,
        prefix=prefix,
        priority=72,
        depends_on=[logic_validation.id],
    )

    delivery = enqueue(
        EnqueueRequest(
            kind=TaskKind.DELIVERY,
            title="Produce reports and testing documentation",
            description="Run installed delivery agents over the approved model.",
            idempotency_key=f"{prefix}:delivery",
            agent_kind=AgentKind.DELIVERY.value,
            run_id=run_id,
            priority=74,
            depends_on=[logic_gate.id],
        )
    )

    validation = _validation_task(
        enqueue,
        title="Validate across every discipline",
        key="validation",
        run_id=run_id,
        prefix=prefix,
        priority=76,
        depends_on=[delivery.id],
    )
    final_senior = enqueue(
        EnqueueRequest(
            kind=TaskKind.SENIOR_REVIEW,
            title="Final senior review of new findings",
            description="Independently check findings created or changed during design, "
            "generation, delivery, and final validation before the QA gate opens.",
            idempotency_key=f"{prefix}:senior:final",
            agent_kind=AgentKind.SENIOR_REVIEW.value,
            payload={"review_pass": "final", "only_unreviewed": True},
            run_id=run_id,
            priority=77,
            depends_on=[validation.id],
        )
    )
    qa_gate = _gate_task(
        enqueue,
        ApprovalGateKey.QA_AND_RISK,
        run_id=run_id,
        prefix=prefix,
        priority=78,
        depends_on=[final_senior.id],
    )

    approvals = enqueue(
        EnqueueRequest(
            kind=TaskKind.REQUEST_APPROVALS,
            title="Route findings for human approval",
            description="Raise an approval for everything that needs an engineer's decision.",
            idempotency_key=f"{prefix}:approvals",
            agent_kind=AgentKind.SENIOR_REVIEW.value,
            run_id=run_id,
            priority=85,
            depends_on=[qa_gate.id],
        )
    )

    candidate = enqueue(
        EnqueueRequest(
            kind=TaskKind.GENERATE_RELEASE_CANDIDATE,
            title="Generate the release candidate",
            description="Build the import-ready package from the approved revision.",
            idempotency_key=f"{prefix}:candidate",
            run_id=run_id,
            priority=90,
            depends_on=[approvals.id],
        )
    )
    verification = enqueue(
        EnqueueRequest(
            kind=TaskKind.VERIFY_IMPORT,
            title="Attempt import verification",
            description="Ask the workstation adapter to verify the package, and record "
            "that it could not when there is no licensed workstation.",
            idempotency_key=f"{prefix}:verify",
            run_id=run_id,
            priority=92,
            depends_on=[candidate.id],
        )
    )
    import_gate = _gate_task(
        enqueue,
        ApprovalGateKey.IMPORT_VERIFICATION,
        run_id=run_id,
        prefix=prefix,
        priority=94,
        depends_on=[verification.id],
    )

    package = enqueue(
        EnqueueRequest(
            kind=TaskKind.ASSEMBLE_PACKAGE,
            title="Seal the release manifest",
            description="Freeze the manifest against a named engineer's approval.",
            idempotency_key=f"{prefix}:package",
            run_id=run_id,
            priority=96,
            depends_on=[import_gate.id],
        )
    )
    release_gate = _gate_task(
        enqueue,
        ApprovalGateKey.RELEASE,
        run_id=run_id,
        prefix=prefix,
        priority=98,
        depends_on=[package.id],
    )

    enqueue(
        EnqueueRequest(
            kind=TaskKind.FINALIZE_PROJECT,
            title="Finalise the project",
            description="Complete the project once the required approvals are decided.",
            idempotency_key=f"{prefix}:finalize",
            run_id=run_id,
            priority=100,
            depends_on=[release_gate.id],
        )
    )


def _validation_task(
    enqueue: Enqueue,
    *,
    title: str,
    key: str,
    run_id: uuid.UUID,
    prefix: str,
    priority: int,
    depends_on: list[uuid.UUID],
) -> Task:
    """A validation run immediately before a gate that cannot be ready without one.

    Three of them, not one, and that is the gates' requirement rather than a
    preference: gate 2's criteria include "the newest validation run examined
    this revision and covered tags and I/O", and gate 3's say the same of logic
    and delivery. A single run after senior review would leave both gates
    permanently not-ready, with a message about validation currency that no
    amount of design work could clear.

    Every run covers every discipline. Scoping one to the stage that asked for it
    would record a clean result narrower than it reads — and a run that examined
    three disciplines must never be mistaken for one that examined all of them.
    """
    return enqueue(
        EnqueueRequest(
            kind=TaskKind.VALIDATE_CROSS_DISCIPLINE,
            title=title,
            description="Run every validator over the revision as it stands; only "
            "unwaived errors stop the run.",
            idempotency_key=f"{prefix}:{key}",
            run_id=run_id,
            priority=priority,
            depends_on=depends_on,
        )
    )


def _gate_task(
    enqueue: Enqueue,
    gate_key: ApprovalGateKey,
    *,
    run_id: uuid.UUID,
    prefix: str,
    priority: int,
    depends_on: list[uuid.UUID],
) -> Task:
    """One gate as a task, titled and described from the gate's own definition.

    Taken from :mod:`app.engineering.gates` rather than written again here, so a
    reviewer reads the same sentence on the task that they read on the gate.
    """
    spec = gates.spec_for(gate_key)
    return enqueue(
        EnqueueRequest(
            kind=TaskKind.EVALUATE_APPROVAL_GATE,
            title=f"Approval gate: {spec.title}",
            description=spec.description,
            idempotency_key=f"{prefix}:gate:{gate_key.value}",
            payload={"gate_key": gate_key.value},
            run_id=run_id,
            priority=priority,
            depends_on=depends_on,
        )
    )


# ---------------------------------------------------------------------------
# Control
# ---------------------------------------------------------------------------
def pause(db: Session, project: Project, *, actor: User, reason: str = "") -> Project:
    """Stop the worker picking up further tasks for this project.

    A task already running is allowed to finish; killing it mid-write would leave
    partial state that the restart-recovery path then has to reason about.
    """
    if project.is_paused:
        raise ConflictError("Project is already paused.")
    if project.status is not ProjectStatus.RUNNING:
        raise ConflictError(
            f"Only a running project can be paused; this one is '{project.status.value}'."
        )

    project.is_paused = True
    project.paused_at = utcnow()
    db.flush()

    audit.record(
        db,
        AuditAction.ANALYSIS_PAUSED,
        actor_user_id=actor.id,
        project_id=project.id,
        target_type="project",
        target_id=project.id,
        summary="Paused the review",
        details={"reason": reason},
    )
    record_event(
        db,
        project_id=project.id,
        event_type=EventType.WORKFLOW_PAUSED,
        actor_user_id=actor.id,
        payload={"reason": reason},
    )
    return project


def resume(db: Session, project: Project, *, actor: User, reason: str = "") -> Project:
    if not project.is_paused:
        raise ConflictError("Project is not paused.")

    project.is_paused = False
    project.paused_at = None
    db.flush()

    audit.record(
        db,
        AuditAction.ANALYSIS_RESUMED,
        actor_user_id=actor.id,
        project_id=project.id,
        target_type="project",
        target_id=project.id,
        summary="Resumed the review",
        details={"reason": reason},
    )
    record_event(
        db,
        project_id=project.id,
        event_type=EventType.WORKFLOW_RESUMED,
        actor_user_id=actor.id,
        payload={"reason": reason},
    )
    return project


def cancel(
    db: Session, project: Project, settings: Settings, *, actor: User, reason: str = ""
) -> int:
    """Cancel every unfinished task and stop the run.

    Completed work is kept: findings already produced and approved remain valid,
    because cancelling a run is not the same as retracting what it found.
    """
    queue = DatabaseTaskQueue(db, settings)
    unfinished = list(
        db.execute(
            select(Task).where(
                Task.project_id == project.id,
                Task.status.notin_([TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED]),
            )
        ).scalars()
    )
    for task in unfinished:
        queue.cancel(task, reason or "The run was cancelled.")

    project.status = ProjectStatus.PLANNED if unfinished else project.status
    project.is_paused = False
    project.paused_at = None
    db.flush()

    audit.record(
        db,
        AuditAction.ANALYSIS_CANCELLED,
        actor_user_id=actor.id,
        project_id=project.id,
        target_type="project",
        target_id=project.id,
        summary=f"Cancelled the review, stopping {len(unfinished)} task(s)",
        details={"reason": reason, "cancelled_tasks": len(unfinished)},
    )
    record_event(
        db,
        project_id=project.id,
        event_type=EventType.WORKFLOW_CANCELLED,
        actor_user_id=actor.id,
        payload={"reason": reason, "task_count": len(unfinished)},
    )
    return len(unfinished)


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------
def refresh_project_status(db: Session, project: Project) -> ProjectStatus:
    """Recompute project status from its tasks.

    Called after every task transition. Deliberately conservative about
    ``completed``: a project is only complete when every task is terminal AND no
    approval is still pending. Marking it complete with an outstanding safety
    approval would be the single worst thing this product could do.

    A production run reports the *phase* it is in rather than a bare ``running``
    — see :func:`current_phase`. A review run reports ``running`` exactly as it
    always has: the phase members describe design and generation work a review
    run never does, and labelling one ``tag_and_io_design`` would be a claim
    about work nobody performed.
    """
    if project.status in {ProjectStatus.ARCHIVED, ProjectStatus.DRAFT}:
        return project.status

    tasks = list(db.execute(select(Task).where(Task.project_id == project.id)).scalars())
    if not tasks:
        return project.status

    statuses = [task.status for task in tasks]
    pending_approvals = int(
        db.execute(
            select(Approval.id).where(
                Approval.project_id == project.id,
                Approval.status == "pending",
            )
        )
        .scalars()
        .first()
        is not None
    )

    active = any(
        status in {TaskStatus.QUEUED, TaskStatus.RUNNING, TaskStatus.BLOCKED} for status in statuses
    )
    awaiting = any(status is TaskStatus.AWAITING_APPROVAL for status in statuses)

    previous = project.status
    if awaiting or pending_approvals:
        project.status = ProjectStatus.AWAITING_REVIEW
    elif active:
        project.status = current_phase(tasks) or ProjectStatus.RUNNING
    elif all(status is TaskStatus.CANCELLED for status in statuses):
        project.status = ProjectStatus.PLANNED
    elif any(status is TaskStatus.FAILED for status in statuses):
        # A run that could not finish is failed, and the reason names the tasks
        # rather than leaving an engineer to guess.
        failed = [task for task in tasks if task.status is TaskStatus.FAILED]
        project.status = ProjectStatus.FAILED
        project.failure_reason = "; ".join(
            f"{task.title}: {task.error_message or task.error_code}" for task in failed[:5]
        )
    else:
        project.status = ProjectStatus.COMPLETED
        project.completed_at = utcnow()

    if project.status is not previous:
        db.flush()
        _emit_status_change(db, project, previous)
    return project.status


def current_phase(tasks: list[Task]) -> ProjectStatus | None:
    """Which engineering phase a production run is in, or ``None`` for a review run.

    Derived from the tasks rather than stored, for the reason the evidence graph
    is: the phase is a pure function of the queue, and a column holding it would
    be a second account that could disagree with the first.

    The phase is the one belonging to the earliest unfinished task in plan order
    — priority, which is how the queue itself orders work — so it is where the
    run has actually got to rather than the furthest thing that has happened. A
    task blocked in a phase reports ``blocked`` instead, except at the
    clarification checkpoint, whose own phase already says what is wrong and who
    has to act.
    """
    if not any(task.kind in PRODUCTION_TASK_KINDS for task in tasks):
        return None

    unfinished = sorted(
        (task for task in tasks if not task.is_terminal and task.kind in PHASE_BY_TASK_KIND),
        key=lambda task: (task.priority, task.created_at),
    )
    if not unfinished:
        return None

    task = unfinished[0]
    phase = PHASE_BY_TASK_KIND[task.kind]
    if task.status is TaskStatus.BLOCKED and phase is not ProjectStatus.CLARIFICATION_REQUIRED:
        return ProjectStatus.BLOCKED
    return phase


def _emit_status_change(db: Session, project: Project, previous: ProjectStatus) -> None:
    payload = {
        "project_id": str(project.id),
        "name": project.name,
        "status": project.status.value,
        "failure_reason": project.failure_reason,
    }
    if project.status is ProjectStatus.COMPLETED:
        event_type = EventType.PROJECT_COMPLETED
    elif project.status is ProjectStatus.FAILED:
        event_type = EventType.PROJECT_FAILED
    else:
        event_type = EventType.PROJECT_UPDATED

    record_event(db, project_id=project.id, event_type=event_type, payload=payload)
    logger.info(
        "project status changed",
        extra={
            "project_id": str(project.id),
            "from": previous.value,
            "to": project.status.value,
        },
    )


def unblock_approval_tasks(db: Session, project: Project, settings: Settings) -> int:
    """Return approval-gated tasks to the queue once nothing is pending.

    Called after an approval decision. A task waiting on approval resumes only
    when every approval for the project has been decided, so a partially-approved
    set cannot slip through.
    """
    still_pending = db.execute(
        select(Approval.id).where(Approval.project_id == project.id, Approval.status == "pending")
    ).first()
    if still_pending is not None:
        return 0

    queue = DatabaseTaskQueue(db, settings)
    waiting = list(
        db.execute(
            select(Task).where(
                Task.project_id == project.id,
                Task.status == TaskStatus.AWAITING_APPROVAL,
            )
        ).scalars()
    )
    for task in waiting:
        queue.unblock(task)
    if waiting:
        logger.info(
            "approval gate cleared",
            extra={"project_id": str(project.id), "resumed_tasks": len(waiting)},
        )
    return len(waiting)


def active_run_id(db: Session, project_id: uuid.UUID) -> uuid.UUID | None:
    """The run id of the most recently created task, if any."""
    task = db.execute(
        select(Task)
        .where(Task.project_id == project_id, Task.run_id.isnot(None))
        .order_by(Task.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    return task.run_id if task else None
