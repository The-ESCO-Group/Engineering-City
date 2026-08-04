"""Task handlers for the agent steps.

Each is a thin wrapper: build the evidence graph, run the agent, translate what
it produced into a task outcome. The agent decides what to say; the handler
decides what that means for the workflow.

The production agents propose changes to the canonical model rather than
findings, and this is where those proposals are carried out — through
:mod:`app.engineering` and nowhere else. Three things follow from that and are
the reason these handlers are longer than the review ones:

* a proposal is checked by the agent's own deterministic guard *before* a change
  set exists. The merge would refuse it too, but a refusal after the proposal
  has been recorded and attributed is a worse record than one before;
* a merge that could not proceed is **blocked**, not failed. A conflict means
  the platform worked and a human has to look; reporting it as a failure would
  tell an engineer the tool is broken;
* an agent that produced nothing and asked a question is blocked with the
  question, because "waiting for you" and "finished" must never look alike.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from sqlalchemy import select

from app.agents.base.actions import ProposeChangesetPayload, RequestClarificationPayload
from app.agents.base.agent import AgentContext, AgentResult, BaseAgent
from app.agents.base.runner import ValidatedAction
from app.agents.review import plc_logic, requirements_planning
from app.agents.review.interlock_review import InterlockReviewAgent
from app.agents.review.plc_logic import PLCLogicAgent
from app.agents.review.plc_review import PLCReviewAgent
from app.agents.review.project_manager import ProjectManagerAgent
from app.agents.review.requirements_planning import RequirementsPlanningAgent
from app.agents.review.senior_review import SeniorReviewAgent
from app.agents.review.tag_architecture import TagArchitectureAgent
from app.core.enums import ActionType, ChangeSetStatus, ControllerFamily, MergeOutcome, TaskKind
from app.core.errors import BudgetExceededError, ProviderError
from app.core.logging import get_logger
from app.core.services.evidence_graph import build_graph
from app.engineering import build_changeset, merge_changeset, submit
from app.generators.plc.template_registry import TemplateRegistry, get_registry, load_templates
from app.models.finding import Finding
from app.models.profile import ProjectProfile, TargetPlatform
from app.models.revision import ChangeSet, MergeResult
from app.models.task import Task
from app.orchestration.handlers import HandlerResult, TaskContext, register
from app.orchestration.queue import DatabaseTaskQueue

logger = get_logger(__name__)

#: Merge outcomes that leave the canonical model in the state the change set
#: asked for. ``NO_OP`` is here because a change set with nothing left to apply
#: has converged, which is what an at-least-once queue needs to be safe.
_SETTLED_MERGES: frozenset[MergeOutcome] = frozenset(
    {MergeOutcome.MERGED, MergeOutcome.NO_OP, MergeOutcome.DRY_RUN_OK}
)

#: A deterministic check an agent exports over its own proposal. Returns the
#: reasons the proposal must be refused, and an empty list when there are none.
ProposalGuard = Callable[[ProposeChangesetPayload], list[str]]


def _run(
    agent: BaseAgent,
    context: TaskContext,
    *,
    handoff_handlers: Mapping[ActionType, str] | None = None,
) -> tuple[AgentResult, dict[str, Any]]:
    graph = build_graph(context.db, context.project.id)
    agent_context = AgentContext(
        db=context.db,
        project=context.project,
        settings=context.settings,
        graph=graph,
        task_id=context.task.id,
        workflow_run_id=context.task.run_id,
        payload=context.payload,
    )
    result = agent.run(agent_context, handoff_handlers=handoff_handlers)
    return result, result.summary()


def _outcome(result: AgentResult, summary: dict[str, Any]) -> HandlerResult:
    """Translate an agent run into a task outcome.

    A run that failed at the provider or ran out of budget is a task failure. A
    run that completed but reported a blocker is *blocked*, not failed: the
    platform worked correctly and is waiting for a human to supply something.
    """
    if not result.succeeded:
        run = result.outcome.run
        return HandlerResult.failed(
            run.error_code or "agent_failed",
            run.error_message or f"The agent run ended as {run.status.value}.",
            **summary,
        )
    if result.execution.blockers:
        return HandlerResult.blocked("; ".join(result.execution.blockers), **summary)
    return HandlerResult.completed(**summary)


def _guarded(agent: BaseAgent, context: TaskContext) -> HandlerResult:
    try:
        result, summary = _run(agent, context)
    except BudgetExceededError as exc:
        # Blocked rather than failed: nothing is wrong with the review, an
        # administrator simply has to raise the budget for it to continue.
        return HandlerResult.blocked(exc.message)
    except ProviderError as exc:
        return HandlerResult.failed(exc.code, exc.message)
    return _outcome(result, summary)


# ---------------------------------------------------------------------------
# Proposals: from an accepted action to a merged revision
# ---------------------------------------------------------------------------
def _design(
    agent: BaseAgent,
    context: TaskContext,
    *,
    guard: ProposalGuard | None = None,
) -> HandlerResult:
    """Run a production agent and carry out what it proposed.

    The order is the product rule: check the proposal, record it, merge it. A
    guard that fires stops the whole change set before it exists, because a
    refused proposal that has already been recorded and attributed reads, six
    months later, as work somebody did.
    """
    try:
        result, summary = _run(
            agent,
            context,
            handoff_handlers={
                ActionType.PROPOSE_CHANGESET: "production design task handler",
            },
        )
    except BudgetExceededError as exc:
        return HandlerResult.blocked(exc.message)
    except ProviderError as exc:
        return HandlerResult.failed(exc.code, exc.message)

    outcome = _outcome(result, summary)
    if outcome.status != "completed":
        return outcome

    proposals = _accepted(result, ActionType.PROPOSE_CHANGESET)
    questions = [
        item.payload
        for item in _accepted(result, ActionType.REQUEST_CLARIFICATION)
        if isinstance(item.payload, RequestClarificationPayload)
    ]

    refusals = _refusals(proposals, guard)
    if refusals:
        return HandlerResult.blocked(
            "The proposal was refused before it was recorded: " + "; ".join(refusals),
            **summary,
            refused_operations=refusals,
        )

    merges: list[dict[str, Any]] = []
    for ordinal, item in enumerate(proposals):
        assert isinstance(item.payload, ProposeChangesetPayload)
        changeset, merge = _merge_proposal(
            agent, context, payload=item.payload, ordinal=ordinal, run_id=result.outcome.run.id
        )
        merges.append(_merge_view(changeset, merge))
        if merge.outcome is MergeOutcome.APPROVAL_REQUIRED:
            return HandlerResult.awaiting_approval(
                merge.explanation, **summary, changesets=merges, questions=_questions(questions)
            )
        if merge.outcome not in _SETTLED_MERGES:
            return HandlerResult.blocked(
                merge.explanation,
                **summary,
                changesets=merges,
                conflicts=[
                    f"{conflict.object_label}: {conflict.message}" for conflict in merge.conflicts
                ],
            )

    if questions and not merges:
        # Nothing was produced and something was asked. Completing here would
        # let the run walk past a question nobody has answered.
        return HandlerResult.blocked(
            "; ".join(item.question for item in questions),
            **summary,
            questions=_questions(questions),
        )

    return HandlerResult.completed(**summary, changesets=merges, questions=_questions(questions))


def _accepted(result: AgentResult, action_type: ActionType) -> list[ValidatedAction]:
    return [item for item in result.outcome.accepted if item.action_type is action_type]


def _refusals(proposals: Sequence[ValidatedAction], guard: ProposalGuard | None) -> list[str]:
    if guard is None:
        return []
    reasons: list[str] = []
    for item in proposals:
        if isinstance(item.payload, ProposeChangesetPayload):
            reasons.extend(guard(item.payload))
    return reasons


def _merge_proposal(
    agent: BaseAgent,
    context: TaskContext,
    *,
    payload: ProposeChangesetPayload,
    ordinal: int,
    run_id: uuid.UUID,
) -> tuple[ChangeSet, MergeResult]:
    """Record one proposal and merge it, or record why it could not be merged.

    The idempotency key is the task and the action's position within the run, so
    a task the at-least-once queue runs twice converges on one change set rather
    than proposing the same batch again.
    """
    changeset = build_changeset(
        context.db,
        project=context.project,
        payload=payload,
        author_agent=agent.kind.value,
        task_id=context.task.id,
        agent_run_id=run_id,
        idempotency_key=f"{context.task.id}:changeset:{ordinal}",
        # The scope the agent was *granted*, not one it declared. An agent that
        # states none is not thereby unrestricted: the merge still enforces
        # protected fields, the traceability rule and the approval gate.
        allowed_object_types=agent.allowed_object_types,
    )
    if changeset.status is not ChangeSetStatus.MERGED:
        submit(context.db, changeset)
    return changeset, merge_changeset(context.db, changeset=changeset)


def _merge_view(changeset: ChangeSet, merge: MergeResult) -> dict[str, Any]:
    return {
        "changeset_id": str(changeset.id),
        "title": changeset.title,
        "outcome": merge.outcome.value,
        "operations_applied": merge.operations_applied,
        "revision_number": merge.resulting_revision_number,
        "explanation": merge.explanation,
    }


def _questions(questions: Sequence[RequestClarificationPayload]) -> list[dict[str, str]]:
    """Clarifications the agent raised, kept where a human will see them.

    The action executor persists each accepted action as a ``ProjectQuestion``.
    This compact copy remains on the task result so an operator can understand
    why this task stopped without making a second request.
    """
    return [
        {
            "category": item.category.value,
            "question": item.question,
            "why_it_matters": item.why_it_matters,
        }
        for item in questions
    ]


@register(TaskKind.PLAN_PROJECT)
def plan_project(context: TaskContext) -> HandlerResult:
    """The Project Manager Agent plans which specialist reviews to run.

    The tasks it plans become dependencies of the senior review, so that step
    waits for work that did not exist when the run was created.
    """
    agent = ProjectManagerAgent()
    try:
        result, summary = _run(agent, context)
    except BudgetExceededError as exc:
        return HandlerResult.blocked(exc.message)
    except ProviderError as exc:
        return HandlerResult.failed(exc.code, exc.message)

    outcome = _outcome(result, summary)
    if outcome.status != "completed":
        return outcome

    planned = result.execution.enqueue
    if not planned:
        return HandlerResult.blocked(
            "The planner scheduled no specialist review, because the supplied "
            "evidence does not support one. "
            + "; ".join(result.execution.blockers or ["No reason was given."]),
            **summary,
        )

    senior_id = _planned_senior_id(context)
    if senior_id is None:
        return HandlerResult.failed(
            "invalid_workflow_plan",
            "The workflow did not name the senior-review task that must wait for its "
            "specialist reviews.",
            **summary,
        )

    queue = DatabaseTaskQueue(context.db, context.settings)
    senior = context.db.get(Task, senior_id)
    if (
        senior is None
        or senior.project_id != context.project.id
        or senior.run_id != context.task.run_id
        or senior.kind is not TaskKind.SENIOR_REVIEW
    ):
        return HandlerResult.failed(
            "invalid_workflow_plan",
            "The workflow's senior-review dependency target is missing or belongs to a "
            "different project or run.",
            **summary,
        )

    created: list[Task] = []
    for request in planned:
        request.run_id = context.task.run_id
        task = queue.enqueue(context.project.id, request)
        queue.add_dependency(senior, task.id, hard=True)
        created.append(task)

    # Enqueueing here is intentional: the dependency edge and its specialist
    # task land in the handler's transaction together. Returning the requests to
    # the generic worker would create the tasks only after the handler had lost
    # the chance to make senior review wait for them.
    outcome.enqueue = []
    outcome.result = {
        **summary,
        "planned": [task.kind.value for task in created],
        "planned_task_ids": [str(task.id) for task in created],
        "senior_review_task_id": str(senior.id),
    }
    return outcome


def _planned_senior_id(context: TaskContext) -> uuid.UUID | None:
    raw = context.payload.get("senior_review_task_id")
    if not raw:
        return None
    try:
        return uuid.UUID(str(raw))
    except (TypeError, ValueError, AttributeError):
        return None


@register(TaskKind.REVIEW_PLC)
def review_plc(context: TaskContext) -> HandlerResult:
    return _guarded(PLCReviewAgent(), context)


@register(TaskKind.REVIEW_INTERLOCKS)
def review_interlocks(context: TaskContext) -> HandlerResult:
    return _guarded(InterlockReviewAgent(), context)


@register(TaskKind.EXTRACT_REQUIREMENTS)
def extract_requirements(context: TaskContext) -> HandlerResult:
    """The Requirements and Planning Agent records what was asked for.

    :func:`~app.agents.review.requirements_planning.refusal_reasons` is the gate
    here rather than the merge's. None of the five object kinds this agent
    creates is traceability-checked below it, so nothing else in the stack knows
    that an assumption without ``impact_if_wrong`` is a guess with a reference
    number, or that an agent setting a requirement to ``accepted`` has approved
    its own reading of the brief.
    """
    return _design(
        RequirementsPlanningAgent(),
        context,
        guard=requirements_planning.refusal_reasons,
    )


@register(TaskKind.DESIGN_TAGS)
def design_tags(context: TaskContext) -> HandlerResult:
    """The Tag Architecture Agent proposes the vocabulary everything else binds to.

    No handler-side guard: the rules that matter here — a rename may not be
    disguised as a field update, a change set containing a rename must require
    human approval, a created tag must state a basis — are enforced by the action
    schema and by the merge, both of which see every proposal whatever proposed
    it.
    """
    return _design(TagArchitectureAgent(), context)


@register(TaskKind.DESIGN_PLC_ARCHITECTURE)
def design_plc_architecture(context: TaskContext) -> HandlerResult:
    """Controller organisation: tasks, programs and routines.

    Shares an agent with :func:`generate_logic` and differs in what it is asked
    to produce, so the stage travels in the task payload. The agent's context is
    the same either way — an organisation proposed without knowing which
    routines will hold which patterns is an organisation proposed blind.
    """
    return _generation_stage(context)


@register(TaskKind.GENERATE_LOGIC)
def generate_logic(context: TaskContext) -> HandlerResult:
    """Networks, each naming the approved pattern that expands into its rungs."""
    return _generation_stage(context)


def _generation_stage(context: TaskContext) -> HandlerResult:
    """One of the two PLC Logic stages, refused up front where it may not run.

    :func:`~app.agents.review.plc_logic.generation_block` is deterministic and is
    asked before the provider is, for two reasons: the answer cannot change
    because a model was consulted, and a blocked project would spend a run
    producing the refusal the block already states in words written to be put to
    a human unedited.
    """
    profile = _profile_of(context)
    platform = _platform_of(context)

    block = plc_logic.generation_block(profile, platform, context.settings)
    if block is not None:
        return HandlerResult.blocked(
            f"{block.reason} Answer this before generation is attempted again: {block.question}",
            **block.as_dict(),
        )

    family = platform.controller_family if platform is not None else ControllerFamily.OTHER
    return _design(PLCLogicAgent(), context, guard=_selection_guard(context, family))


def _selection_guard(context: TaskContext, family: ControllerFamily) -> ProposalGuard:
    """Refuse a template selection before anything is expanded from it.

    :func:`~app.agents.review.plc_logic.check_selection` is what stands between a
    plausible-looking selection and a half-expanded routine: an unloaded
    template, a pattern approved for another family, a mnemonic this platform
    never emits, an unresolved operand with no stated reason, or protective logic
    claiming to need no approval.
    """
    registry = _template_registry(context)
    enabled = tuple(context.settings.enabled_restricted_domains)

    def guard(payload: ProposeChangesetPayload) -> list[str]:
        problems: list[str] = []
        for operation in payload.operations:
            selection = plc_logic.selection_of(operation)
            if selection is None:
                continue
            problems.extend(
                f"{selection.qualified_id}: {problem}"
                for problem in plc_logic.check_selection(
                    selection, registry=registry, family=family, enabled_domains=enabled
                )
            )
        return problems

    return guard


def _template_registry(context: TaskContext) -> TemplateRegistry:
    """The template registry these settings point at.

    Same rule the runner applies: an administrator who points the platform at a
    different template tree must not be silently served the cached shipped one,
    so the cache is only correct when no override is set.
    """
    if context.settings.template_root is not None:
        return load_templates(context.settings.template_root)
    return get_registry()


def _profile_of(context: TaskContext) -> ProjectProfile | None:
    return context.db.execute(
        select(ProjectProfile).where(
            ProjectProfile.project_id == context.project.id,
            ProjectProfile.deleted_at.is_(None),
        )
    ).scalar_one_or_none()


def _platform_of(context: TaskContext) -> TargetPlatform | None:
    return context.db.execute(
        select(TargetPlatform).where(
            TargetPlatform.project_id == context.project.id,
            TargetPlatform.deleted_at.is_(None),
        )
    ).scalar_one_or_none()


@register(TaskKind.SENIOR_REVIEW)
def senior_review(context: TaskContext) -> HandlerResult:
    """Independent review of every finding before any of them reaches a human."""
    if context.payload.get("only_unreviewed"):
        unreviewed = context.db.execute(
            select(Finding.id).where(
                Finding.project_id == context.project.id,
                Finding.is_reviewed.is_(False),
            )
        ).first()
        if unreviewed is None:
            return HandlerResult.completed(
                reviewed=0,
                already_current=True,
                review_pass=str(context.payload.get("review_pass", "final")),
            )
    return _guarded(SeniorReviewAgent(), context)


@register(TaskKind.REQUEST_APPROVALS)
def request_approvals(context: TaskContext) -> HandlerResult:
    """Hold the run until every raised approval has been decided.

    The approvals themselves were raised as the findings were written, so this
    step only gates. It reports how many are outstanding rather than completing
    silently, because "waiting for you" and "finished" must never look alike.
    """
    from sqlalchemy import select

    from app.models.approval import Approval

    pending = list(
        context.db.execute(
            select(Approval).where(
                Approval.project_id == context.project.id,
                Approval.status == "pending",
            )
        ).scalars()
    )
    if pending:
        return HandlerResult.awaiting_approval(
            f"{len(pending)} finding(s) need an engineering decision before this "
            f"review can be reported.",
            pending_approvals=len(pending),
            approval_ids=[str(item.id) for item in pending],
        )
    return HandlerResult.completed(pending_approvals=0)


@register(TaskKind.DELIVERY)
def delivery(context: TaskContext) -> HandlerResult:
    """Run installed delivery agents over the approved findings.

    Delivery agents are GPT-owned plugins under ``app.agents.delivery``. When
    none is installed the run is not failed: the core review is complete and only
    the deliverables are missing, so this reports the missing capability and
    lets the project finish.
    """
    from app.agents.base.plugins import get_plugin_registry

    registry = get_plugin_registry()
    if not registry.has_delivery:
        return HandlerResult.completed(
            delivery_available=False,
            detail=(
                "No delivery agent plugin is installed, so no reports or testing "
                "documentation were produced. The review itself is unaffected."
            ),
        )

    summaries: dict[str, Any] = {}
    for kind, plugin in sorted(registry.delivery_agents.items()):
        if TaskKind.DELIVERY.value not in plugin.handles_task_kinds:
            continue
        result = plugin.run(context)
        summary = result.summary() if hasattr(result, "summary") else {"result": str(result)}
        summaries[kind] = summary

    if not summaries:
        return HandlerResult.completed(
            delivery_available=False,
            installed=sorted(registry.delivery_agents),
            detail="Installed delivery plugins do not handle the delivery task kind.",
        )

    return HandlerResult.completed(
        delivery_available=True,
        installed=sorted(summaries),
        deliveries=summaries,
        detail="Accepted-finding deliverables were generated by the installed delivery plugins.",
    )
