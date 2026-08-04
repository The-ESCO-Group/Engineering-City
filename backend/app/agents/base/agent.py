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
from app.core.enums import ActionType, AgentKind, AgentStatus, ObjectType
from app.core.logging import get_logger
from app.core.services.evidence_graph import EvidenceGraph, build_graph
from app.models.evidence import EvidenceAnchor
from app.models.project import Project

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

Reply with a single JSON object: {"rationale": str, "actions": [...],
"warnings": [str]}.
""".strip()


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
            context=self.build_context(context),
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
