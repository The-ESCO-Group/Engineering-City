"""Task handlers for the agent steps.

Each is a thin wrapper: build the evidence graph, run the agent, translate what
it produced into a task outcome. The agent decides what to say; the handler
decides what that means for the workflow.
"""

from __future__ import annotations

from typing import Any

from app.agents.base.agent import AgentContext, AgentResult, BaseAgent
from app.agents.review.interlock_review import InterlockReviewAgent
from app.agents.review.plc_review import PLCReviewAgent
from app.agents.review.project_manager import ProjectManagerAgent
from app.agents.review.senior_review import SeniorReviewAgent
from app.core.enums import TaskKind
from app.core.errors import BudgetExceededError, ProviderError
from app.core.logging import get_logger
from app.core.services.evidence_graph import build_graph
from app.orchestration.handlers import HandlerResult, TaskContext, register

logger = get_logger(__name__)


def _run(agent: BaseAgent, context: TaskContext) -> tuple[AgentResult, dict[str, Any]]:
    graph = build_graph(context.db, context.project.id)
    agent_context = AgentContext(
        db=context.db,
        project=context.project,
        settings=context.settings,
        graph=graph,
        task_id=context.task.id,
        payload=context.payload,
    )
    result = agent.run(agent_context)
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

    outcome.enqueue = list(planned)
    outcome.result = {**summary, "planned": [request.kind.value for request in planned]}
    return outcome


@register(TaskKind.REVIEW_PLC)
def review_plc(context: TaskContext) -> HandlerResult:
    return _guarded(PLCReviewAgent(), context)


@register(TaskKind.REVIEW_INTERLOCKS)
def review_interlocks(context: TaskContext) -> HandlerResult:
    return _guarded(InterlockReviewAgent(), context)


@register(TaskKind.SENIOR_REVIEW)
def senior_review(context: TaskContext) -> HandlerResult:
    """Independent review of every finding before any of them reaches a human."""
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

    return HandlerResult.completed(
        delivery_available=True,
        installed=sorted(registry.delivery_agents),
        detail=(
            "Delivery plugins are installed but the core backend does not execute "
            "them directly; see docs/handoffs/CLAUDE.md."
        ),
    )
