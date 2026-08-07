"""The base agent: build a context, run it, execute what survives validation.

Agents are deliberately thin. Everything that matters — action validation, the
evidence requirement, the approval gate, budgets — is enforced below them in
:mod:`app.agents.base.runner` and :mod:`app.agents.base.executor`. An agent
chooses *what to look at* and *what it is allowed to propose*; it cannot widen
its own permissions, and a badly written prompt cannot get an unsourced claim
into the record.

Every system prompt inherits :data:`SHARED_RULES`, which states the boundaries
the whole product is built on.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.base.executor import ActionExecutor, ExecutionResult
from app.agents.base.presence import (
    instance_for_task,
    set_agent_status,
)
from app.agents.base.provider import ModelProvider
from app.agents.base.providers import build_provider
from app.agents.base.registry import AgentSpec, spec_for
from app.agents.base.runner import AgentRunner, RunOutcome, resolve_model_configuration
from app.core.config import Settings
from app.core.enums import (
    ActionType,
    AgentKind,
    AgentStatus,
    ClarificationStatus,
    EvidenceKind,
    ObjectType,
)
from app.core.logging import get_logger
from app.core.services.evidence_graph import EvidenceGraph, build_graph
from app.models.evidence import EvidenceAnchor
from app.models.profile import ProjectDecision, ProjectQuestion
from app.models.project import Project
from app.models.user import User

logger = get_logger(__name__)

#: Prepended to every agent's system prompt. These are not suggestions to the
#: model — most are also enforced in code — but stating them shapes the output
#: toward what will actually be accepted, and the wording is what an engineer
#: eventually reads in a report.
SHARED_RULES = """
You are one reviewer inside ESCO Engineering City, an evidence-based engineering
review platform. Your output is a proposal for a human engineer, never a
conclusion the platform acts on.

Absolute rules:

1. EVERY technical claim must cite evidence_anchor_ids taken from the context you
   were given. An id you did not see in the context will be rejected. Never
   invent one.
2. Separate fact from inference. `fact` states only what the cited evidence
   literally shows. `inference` states what you conclude from it. Never write a
   conclusion in the fact field.
3. State what limits you. Partial parses, ambiguous naming, documents that could
   not be read — put them in `limitations`. Uncertainty is information, not
   weakness, and hiding it is the worst thing you can do here.
4. You are reviewing exported documents, not a plant. An L5X export describes
   offline logic at export time; it establishes nothing about what a controller
   is running, its firmware, or whether the program was ever downloaded. Never
   claim otherwise.
5. Absence in a document is not absence in the plant. A tag missing from one
   export may exist in another program, another controller, or in hard-wired
   circuitry that never appears in software.
6. If you find nothing, say so explicitly with `report_no_findings`, naming what
   you examined. Silence and a clean result must never look the same.
7. Never fabricate. If the evidence does not support a finding, do not make one.
   A missed issue is a failure; an invented one destroys trust in every other
   finding you made.
8. Do not include reasoning traces. `rationale` is a short, human-readable
   justification that will be stored and shown to an engineer.
9. `answered_questions` holds questions a named engineer has already answered.
   Each answer is a fact you may state and cite by its `decision_reference`; it
   is not a fact you established. Never ask again what one of them answers — the
   same question a second time is not a question, it is a run that cannot move —
   and never treat a question as answered because you would have answered it
   that way. Where no answer covers it, it stays a question.

Reply with a single JSON object: {"rationale": str, "actions": [...],
"warnings": [str]}.
""".strip()


@dataclass(frozen=True, slots=True)
class AnsweredQuestion:
    """A question a named human has already answered.

    An answer is *evidence*, not conversation: resolving a question records a
    :class:`~app.models.profile.ProjectDecision`, which is what
    :data:`~app.core.enums.EvidenceKind.HUMAN_DECISION` resolves to and what an
    object built on the answer cites. That is why the decision's reference and
    the decider's name travel with the text — an answer nobody is named for is
    not a human decision, and an agent that cannot cite one cannot build on it.

    Carried into every agent's turn because an agent that cannot see the answers
    asks again. The question is idempotent by reference, so the second asking
    returns the answered row, the step blocks on the same sentence, and a human
    who answered correctly watches the run refuse to move.
    """

    reference: str
    category: str
    question: str
    answer: str
    answered_by: str
    #: The ``ProjectDecision`` that records this answer. Empty only for a row
    #: answered by something that did not record one, and an agent must then
    #: treat the answer as read rather than as citable.
    decision_reference: str

    def as_dict(self) -> dict[str, str]:
        return {
            "reference": self.reference,
            "category": self.category,
            "question": self.question,
            "answer": self.answer,
            "answered_by": self.answered_by,
            "decision_reference": self.decision_reference,
            # Stated rather than left to be worked out: this is the basis kind an
            # object built on this answer has to cite.
            "basis_kind": EvidenceKind.HUMAN_DECISION.value,
        }


@dataclass(slots=True)
class AgentContext:
    """Everything an agent needs to do one turn."""

    db: Session
    project: Project
    settings: Settings
    graph: EvidenceGraph
    task_id: uuid.UUID | None = None
    workflow_run_id: uuid.UUID | None = None
    payload: dict[str, Any] | None = None
    #: Questions on this project a human has answered, built by the caller with
    #: :func:`answered_questions_for`. Handed over rather than queried per agent,
    #: for the same reason the evidence graph is: what an agent reasons over is
    #: assembled once, by the caller, and is the same view for every agent in the
    #: turn. Defaults to empty so a caller with no session-level view — a unit
    #: test exercising one agent — is unchanged.
    answered_questions: tuple[AnsweredQuestion, ...] = ()


@dataclass(slots=True)
class AgentResult:
    """What an agent produced."""

    outcome: RunOutcome
    execution: ExecutionResult

    @property
    def succeeded(self) -> bool:
        return self.outcome.succeeded

    def summary(self) -> dict[str, Any]:
        return {
            "agent_run_id": str(self.outcome.run.id),
            "status": self.outcome.run.status.value,
            "provider": self.outcome.run.provider,
            "model": self.outcome.run.model,
            "accepted_actions": len(self.outcome.accepted),
            "rejected_actions": len(self.outcome.rejected),
            "tokens": self.outcome.run.total_tokens,
            **self.execution.summary(),
        }


class BaseAgent(ABC):
    """One review agent."""

    kind: AgentKind

    @property
    def spec(self) -> AgentSpec:
        found = spec_for(self.kind)
        if found is None:  # pragma: no cover - every core agent is registered
            raise ValueError(f"no registered spec for agent kind {self.kind!r}")
        return found

    @property
    @abstractmethod
    def allowed_actions(self) -> set[ActionType]:
        """The only action types this agent may propose.

        Narrow deliberately: the Project Manager Agent cannot create a finding,
        so it cannot draw a technical conclusion even if a prompt injection
        persuades it to try.
        """

    @property
    def allowed_object_types(self) -> tuple[ObjectType, ...]:
        """The canonical object types this agent may propose changes to.

        Empty — the default — means this agent states no scope, and the action
        layer does not constrain one: the merge's own check against
        ``ChangeSet.allowed_object_types`` remains the gate. An agent that does
        state a scope gets it refused at the boundary as well, which is where a
        requirements extractor proposing ladder logic is cheapest to stop.
        """
        return ()

    @abstractmethod
    def system_prompt(self, context: AgentContext) -> str: ...

    @abstractmethod
    def build_context(self, context: AgentContext) -> dict[str, Any]:
        """The structured view of the project this agent reasons over.

        Only what the agent needs. A model handed the whole project is more
        likely to reach for something irrelevant, and the context is what the
        token budget is spent on.
        """

    # -- execution -------------------------------------------------------
    def run(
        self,
        context: AgentContext,
        provider: ModelProvider | None = None,
        *,
        handoff_handlers: Mapping[ActionType, str] | None = None,
    ) -> AgentResult:
        instance = instance_for_task(
            context.db,
            context.project,
            self.kind,
            context.task_id,
        )
        set_agent_status(
            context.db,
            instance,
            AgentStatus.WORKING,
            activity=self.spec.display_name,
            task_id=context.task_id,
        )

        kind, model, temperature, max_output_tokens = resolve_model_configuration(
            context.db, context.project.id, self.kind.value, context.settings
        )
        if provider is None:
            provider = build_provider(kind, context.settings, model=model or None)

        anchor_ids = set(
            context.db.execute(
                select(EvidenceAnchor.id).where(EvidenceAnchor.project_id == context.project.id)
            ).scalars()
        )

        runner = AgentRunner(
            context.db,
            context.settings,
            project=context.project,
            agent_kind=self.kind.value,
            provider=provider,
            task_id=context.task_id,
            agent_instance_id=instance.id,
            # The administrator's configured values, not a literal. Both are
            # writable through the model-configuration endpoint and the admin
            # UI shows them as applied; the run used to be cut at a hard-coded
            # 4096 output tokens regardless, so a change set too large for that
            # arrived as truncated JSON and failed as `invalid_response`.
            temperature=temperature,
            max_output_tokens=max_output_tokens,
        )
        outcome = runner.run(
            system_prompt=self.system_prompt(context),
            context=self._context_view(context),
            allowed_actions=self.allowed_actions,
            anchor_ids=anchor_ids,
            allowed_object_types=self.allowed_object_types,
        )

        executor = ActionExecutor(
            context.db,
            context.project,
            agent_kind=self.kind.value,
            run_id=outcome.run.id,
            task_id=context.task_id,
            workflow_run_id=context.workflow_run_id,
            agent_instance_id=instance.id,
            handoff_handlers=handoff_handlers,
        )
        execution = executor.execute(outcome)

        set_agent_status(
            context.db,
            instance,
            AgentStatus.IDLE if outcome.succeeded else AgentStatus.BLOCKED,
            activity="",
            task_id=None,
        )
        return AgentResult(outcome=outcome, execution=execution)

    # -- shared context helpers ------------------------------------------
    def _context_view(self, context: AgentContext) -> dict[str, Any]:
        """What the agent chose to look at, plus the answers it may not re-ask.

        The answers are added here rather than inside each ``build_context``
        because they are not one agent's concern: a question a human has answered
        must not be asked again by *any* of them, and a rule that depended on each
        agent remembering to include it would be a rule that held for some.
        ``setdefault`` so an agent that assembles its own view of the answers —
        one that needs them grouped by requirement, say — keeps it.
        """
        view = self.build_context(context)
        view.setdefault(
            "answered_questions", [item.as_dict() for item in context.answered_questions]
        )
        return view

    @staticmethod
    def anchor_index(db: Session, project_id: uuid.UUID) -> dict[str, str]:
        """Map parser-assigned anchor keys to anchor ids.

        Agents see stable keys like ``rung:MainProgram/Conveyor_Interlocks/0`` in
        the graph, and cite the ids this resolves them to. Handing the model a
        key-to-id map is what lets it cite precisely without being able to invent
        an id — anything not in this map is refused by the runner.
        """
        rows = db.execute(
            select(EvidenceAnchor.anchor_key, EvidenceAnchor.id).where(
                EvidenceAnchor.project_id == project_id
            )
        ).all()
        return {key: str(anchor_id) for key, anchor_id in rows if key}


def graph_for(context_db: Session, project: Project) -> EvidenceGraph:
    return build_graph(context_db, project.id)


def answered_questions_for(db: Session, project_id: uuid.UUID) -> tuple[AnsweredQuestion, ...]:
    """Every project question a named human has answered, oldest first.

    Only ``ANSWERED``, and only with text on the row. ``DEFERRED`` records that
    somebody looked at the question and chose to come back to it, and
    ``DISMISSED`` that the answer is no longer needed; neither states what the
    answer *is*, and an agent that read one as an answer would be acting on words
    nobody offered as one.
    """
    decisions = {
        decision.question_id: decision
        for decision in db.execute(
            select(ProjectDecision).where(
                ProjectDecision.project_id == project_id,
                ProjectDecision.question_id.is_not(None),
                ProjectDecision.deleted_at.is_(None),
            )
        ).scalars()
    }
    answered: list[AnsweredQuestion] = []
    for question in db.execute(
        select(ProjectQuestion)
        .where(
            ProjectQuestion.project_id == project_id,
            ProjectQuestion.status == ClarificationStatus.ANSWERED,
            ProjectQuestion.deleted_at.is_(None),
        )
        .order_by(ProjectQuestion.created_at, ProjectQuestion.reference)
    ).scalars():
        if not question.answer.strip():
            continue
        decision = decisions.get(question.id)
        answered.append(
            AnsweredQuestion(
                reference=question.reference,
                category=question.category.value,
                question=question.question,
                answer=question.answer,
                answered_by=_answered_by(db, question, decision),
                decision_reference=decision.reference if decision is not None else "",
            )
        )
    return tuple(answered)


def _answered_by(db: Session, question: ProjectQuestion, decision: ProjectDecision | None) -> str:
    """Who answered, by name.

    The decision's own denormalised name first: it was captured when the decision
    was recorded, so it survives a rename or a deactivated account, which is the
    point of storing it there.
    """
    if decision is not None and decision.decided_by_name:
        return decision.decided_by_name
    if question.answered_by_id is None:
        return ""
    user = db.get(User, question.answered_by_id)
    return user.display_name if user is not None else ""
