"""Bounded agent execution and action validation.

Everything a model proposes passes through here, and nothing else in the system
executes a model-proposed action. The runner is therefore where the product's
central promise is actually enforced rather than merely stated:

* an action outside the closed :class:`~app.core.enums.ActionType` set is refused;
* a payload that fails its model is refused;
* a technical claim with no evidence anchor is refused;
* an anchor id that does not belong to this project is refused — *every* anchor
  id the action can reach, including the ones nested in a payload, not only the
  list at the top of the action;
* a basis citing a template that is not loaded is refused, because
  ``template_expansion`` is the strongest provenance the system records;
* a safety- or damage-related recommendation is forced to require approval;
* a review may lower a finding's or a change set's confidence but never raise
  it, compared against the confidence the subject actually holds;
* a finding a named human has already decided is not re-verdicted or rewritten
  by any agent.

Refusals are recorded on the run, not discarded. What a model *tried* to do is
part of the record an engineer may need to see.

Bounds are enforced per run: wall-clock timeout, maximum iterations, maximum
actions, and token/cost budgets checked against the project's totals before each
call rather than after — a budget you only notice after exceeding it is not a
budget.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.base.actions import (
    ACTION_PAYLOAD_MODELS,
    AgentAction,
    CreateFindingPayload,
    ProposeChangesetPayload,
    ReviewChangesetPayload,
    UpdateFindingReviewPayload,
    finding_dedupe_key,
)
from app.agents.base.provider import ChatMessage, ModelProvider, ProviderResponse
from app.core.clock import utcnow
from app.core.config import Settings
from app.core.enums import (
    ActionType,
    AgentRunStatus,
    AuditAction,
    FindingStatus,
    ObjectType,
    ProviderKind,
)
from app.core.errors import AgentActionRejectedError, BudgetExceededError, ProviderError
from app.core.logging import get_logger
from app.generators.plc.template_registry import TemplateRegistry, get_registry, load_templates
from app.models.agent import AgentRun, ModelConfiguration, UsageRecord
from app.models.evidence import EvidenceAnchor
from app.models.finding import Finding
from app.models.project import Project
from app.models.revision import ChangeSet
from app.security import audit

logger = get_logger(__name__)

#: JSON Schema the provider is asked to conform to. Kept small on purpose: the
#: authoritative validation is the Pydantic pass below, and a large schema tends
#: to make local models produce worse JSON rather than better.
RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["rationale", "actions"],
    "properties": {
        "rationale": {
            "type": "string",
            "description": (
                "A concise, human-readable justification. Not a reasoning trace: "
                "it is stored and shown to an engineer."
            ),
        },
        "actions": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["action_type", "summary"],
                "properties": {
                    "action_type": {
                        "type": "string",
                        "enum": [action.value for action in ActionType],
                    },
                    "summary": {"type": "string"},
                    "target_type": {"type": "string"},
                    "target_id": {"type": "string"},
                    "evidence_anchor_ids": {"type": "array", "items": {"type": "string"}},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "requires_approval": {"type": "boolean"},
                    "payload": {"type": "object"},
                    "warnings": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
        "warnings": {"type": "array", "items": {"type": "string"}},
    },
}


@dataclass(slots=True)
class ValidatedAction:
    """An action that survived validation, with its parsed payload."""

    action: AgentAction
    payload: Any

    @property
    def action_type(self) -> ActionType:
        return self.action.action_type


@dataclass(slots=True)
class RejectedAction:
    """An action the backend refused, kept for the record."""

    action_type: str
    summary: str
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "action_type": self.action_type,
            "summary": self.summary,
            "accepted": False,
            "rejection_reason": self.reason,
        }


@dataclass(slots=True)
class RunOutcome:
    """The result of one bounded agent run."""

    run: AgentRun
    accepted: list[ValidatedAction] = field(default_factory=list)
    rejected: list[RejectedAction] = field(default_factory=list)
    rationale: str = ""
    warnings: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def succeeded(self) -> bool:
        return self.run.status is AgentRunStatus.COMPLETED


def decided_by_a_human(finding: Finding) -> bool:
    """Whether a named person has already ruled on this finding.

    ``decided_at`` and ``decided_by_id`` are written only by the approvals
    endpoint on behalf of a signed-in user, and ``FindingStatus.ACCEPTED`` is
    reachable from nowhere else. An agent verdict sets none of them, so an agent
    may still revisit a conclusion an agent reached.

    Declared once and imported by the executor rather than written out twice:
    two copies of a rule this load-bearing would eventually disagree, and the
    disagreement would be silent.
    """
    return (
        finding.decided_at is not None
        or finding.decided_by_id is not None
        or finding.status is FindingStatus.ACCEPTED
    )


def resolve_model_configuration(
    db: Session, project_id: uuid.UUID, agent_kind: str, settings: Settings
) -> tuple[ProviderKind, str, float, int]:
    """Which provider and model this agent should use.

    Most specific wins: a per-project, per-agent row, then per-project, then
    global, then the configured default. A row naming a provider the
    administrator has since disabled is ignored rather than honoured — a stale
    row must not be able to re-enable a provider that was turned off.
    """
    rows = list(
        db.execute(
            select(ModelConfiguration).where(
                (ModelConfiguration.project_id == project_id)
                | (ModelConfiguration.project_id.is_(None))
            )
        ).scalars()
    )

    def rank(row: ModelConfiguration) -> int:
        if row.project_id is not None and row.agent_kind == agent_kind:
            return 0
        if row.project_id is not None and not row.agent_kind:
            return 1
        if row.project_id is None and row.agent_kind == agent_kind:
            return 2
        return 3

    for row in sorted(rows, key=rank):
        try:
            kind = ProviderKind(row.provider)
        except ValueError:
            continue
        if settings.provider_enabled(kind):
            return kind, row.model, row.temperature, row.max_output_tokens
        logger.warning(
            "ignoring a model configuration naming a disabled provider",
            extra={"provider": row.provider, "agent_kind": agent_kind},
        )

    return settings.default_provider, "", 0.0, 4096


class AgentRunner:
    """Executes one agent turn under explicit bounds."""

    def __init__(
        self,
        db: Session,
        settings: Settings,
        *,
        project: Project,
        agent_kind: str,
        provider: ModelProvider,
        task_id: uuid.UUID | None = None,
        agent_instance_id: uuid.UUID | None = None,
        temperature: float = 0.0,
        max_output_tokens: int = 4096,
    ) -> None:
        self.db = db
        self.settings = settings
        self.project = project
        self.agent_kind = agent_kind
        self.provider = provider
        self.task_id = task_id
        self.agent_instance_id = agent_instance_id
        # Carried rather than hard-coded at the call site: these are the values
        # an administrator configured per project and per agent, and a setting
        # the admin UI shows as applied must actually be applied.
        self.temperature = temperature
        self.max_output_tokens = max_output_tokens

    # -- budgets ---------------------------------------------------------
    def check_budget(self) -> None:
        """Refuse to start a call that the project's budget cannot cover.

        Checked before the call, not after. A budget noticed only once exceeded
        is not a budget, and the point here is that a runaway agent cannot spend
        a project's allowance without anyone deciding to.
        """
        if self.project.tokens_used >= self.settings.project_token_budget:
            raise BudgetExceededError(
                f"This project has used its token budget "
                f"({self.project.tokens_used:,} of "
                f"{self.settings.project_token_budget:,}). An administrator must "
                f"raise it before the review can continue.",
                details={
                    "tokens_used": self.project.tokens_used,
                    "budget": self.settings.project_token_budget,
                },
            )
        cost_budget_micros = int(self.settings.project_cost_budget_usd * 1_000_000)
        if cost_budget_micros and self.project.cost_used_micros >= cost_budget_micros:
            raise BudgetExceededError(
                "This project has used its cost budget.",
                details={
                    "cost_used_usd": round(self.project.cost_used_micros / 1_000_000, 2),
                    "budget_usd": self.settings.project_cost_budget_usd,
                },
            )

    def _record_usage(self, run: AgentRun, response: ProviderResponse, *, ok: bool) -> None:
        self.db.add(
            UsageRecord(
                project_id=self.project.id,
                agent_run_id=run.id,
                agent_kind=self.agent_kind,
                provider=response.provider,
                model=response.model,
                prompt_tokens=response.prompt_tokens,
                completion_tokens=response.completion_tokens,
                latency_ms=response.latency_ms,
                succeeded=ok,
            )
        )
        run.prompt_tokens += response.prompt_tokens
        run.completion_tokens += response.completion_tokens
        # A failed call still consumed the provider's budget, so it counts.
        self.project.tokens_used += response.total_tokens

    # -- execution -------------------------------------------------------
    def run(
        self,
        *,
        system_prompt: str,
        context: dict[str, Any],
        allowed_actions: set[ActionType],
        anchor_ids: set[uuid.UUID] | None = None,
        allowed_object_types: Sequence[ObjectType] | None = None,
    ) -> RunOutcome:
        """Call the model once and validate everything it proposed.

        One call, not a loop: every agent in this product is a single-turn
        reviewer over a prepared context. A conversational loop would multiply
        cost and nondeterminism for no gain, and the iteration limit exists for
        agents that later need one.

        ``allowed_object_types`` is the change-set scope the agent was granted.
        ``None`` or an empty sequence means this layer does not constrain it and
        the merge's own scope check is the only gate — which is what it was
        before this parameter existed, so an agent that declares a scope gets it
        enforced here, at the boundary, and one that declares none is no worse
        off than it was.
        """
        run = AgentRun(
            project_id=self.project.id,
            agent_instance_id=self.agent_instance_id,
            task_id=self.task_id,
            agent_kind=self.agent_kind,
            status=AgentRunStatus.RUNNING,
            provider=str(getattr(self.provider.kind, "value", self.provider.kind)),
            model=self.provider.model,
            started_at=utcnow(),
        )
        self.db.add(run)
        self.db.flush()

        outcome = RunOutcome(run=run)
        started = time.perf_counter()

        try:
            self.check_budget()
        except BudgetExceededError as exc:
            return self._finish(
                outcome, AgentRunStatus.BUDGET_EXCEEDED, exc.code, exc.message, started
            )

        try:
            response = self.provider.complete_structured(
                system=system_prompt,
                messages=[ChatMessage(role="user", content=_encode(context))],
                schema=RESPONSE_SCHEMA,
                max_output_tokens=self.max_output_tokens,
                temperature=self.temperature,
            )
        except ProviderError as exc:
            return self._finish(outcome, AgentRunStatus.FAILED, exc.code, exc.message, started)

        self._record_usage(run, response, ok=True)

        elapsed = time.perf_counter() - started
        if elapsed > self.settings.agent_run_timeout_seconds:
            return self._finish(
                outcome,
                AgentRunStatus.TIMED_OUT,
                "agent_timeout",
                f"The run exceeded its {self.settings.agent_run_timeout_seconds:.0f}s limit.",
                started,
            )

        outcome.rationale = str(response.data.get("rationale") or "")[:4000]
        outcome.warnings = [str(item) for item in response.data.get("warnings") or []]
        outcome.warnings.extend(response.warnings)
        outcome.extra = {
            key: value
            for key, value in response.data.items()
            if key not in {"rationale", "actions", "warnings"}
        }

        proposed = response.data.get("actions")
        if not isinstance(proposed, list):
            return self._finish(
                outcome,
                AgentRunStatus.FAILED,
                "invalid_response",
                "The model's response did not contain an actions list.",
                started,
            )

        if len(proposed) > self.settings.agent_max_actions_per_run:
            outcome.warnings.append(
                f"The agent proposed {len(proposed)} actions; only the first "
                f"{self.settings.agent_max_actions_per_run} were considered."
            )
            proposed = proposed[: self.settings.agent_max_actions_per_run]

        valid_anchors = anchor_ids if anchor_ids is not None else self._project_anchor_ids()
        scope = frozenset(allowed_object_types or ())
        for raw in proposed:
            try:
                outcome.accepted.append(self._validate(raw, allowed_actions, valid_anchors, scope))
            except AgentActionRejectedError as exc:
                rejection = RejectedAction(
                    action_type=str(exc.action_type or ""),
                    summary=str(raw.get("summary", ""))[:500] if isinstance(raw, dict) else "",
                    reason=exc.reason,
                )
                outcome.rejected.append(rejection)
                audit.record(
                    self.db,
                    AuditAction.AGENT_ACTION_REJECTED,
                    project_id=self.project.id,
                    actor_agent_kind=self.agent_kind,
                    target_type="agent_run",
                    target_id=run.id,
                    summary=f"Refused a proposed {rejection.action_type or 'action'}",
                    details={"reason": rejection.reason},
                )
                logger.warning(
                    "refused a model-proposed action",
                    extra={
                        "agent_kind": self.agent_kind,
                        "action_type": rejection.action_type,
                        "reason": rejection.reason,
                    },
                )

        run.actions = [_action_record(item) for item in outcome.accepted]
        run.rejected_actions = [item.as_dict() for item in outcome.rejected]
        return self._finish(outcome, AgentRunStatus.COMPLETED, "", "", started)

    def _finish(
        self,
        outcome: RunOutcome,
        status: AgentRunStatus,
        error_code: str,
        error_message: str,
        started: float,
    ) -> RunOutcome:
        run = outcome.run
        run.status = status
        run.finished_at = utcnow()
        run.duration_ms = int((time.perf_counter() - started) * 1000)
        run.rationale = outcome.rationale
        run.warnings = outcome.warnings
        run.error_code = error_code
        run.error_message = error_message
        run.iterations = 1
        self.db.flush()

        logger.info(
            "agent run finished",
            extra={
                "agent_kind": self.agent_kind,
                "status": status.value,
                "accepted": len(outcome.accepted),
                "rejected": len(outcome.rejected),
                "tokens": run.total_tokens,
                "duration_ms": run.duration_ms,
            },
        )
        return outcome

    # -- validation ------------------------------------------------------
    def _project_anchor_ids(self) -> set[uuid.UUID]:
        return set(
            self.db.execute(
                select(EvidenceAnchor.id).where(EvidenceAnchor.project_id == self.project.id)
            ).scalars()
        )

    def _registry(self) -> TemplateRegistry:
        """The template registry, honouring a configured root.

        An administrator who points the platform at a different template tree
        must not be silently served the shipped one; the cached registry is only
        correct when no override is set.
        """
        if self.settings.template_root is not None:
            return load_templates(self.settings.template_root)
        return get_registry()

    def _validate(
        self,
        raw: Any,
        allowed_actions: set[ActionType],
        valid_anchors: set[uuid.UUID],
        allowed_object_types: frozenset[ObjectType] = frozenset(),
    ) -> ValidatedAction:
        """Validate one proposed action, or refuse it with a reason."""
        if not isinstance(raw, dict):
            raise AgentActionRejectedError("The action was not a JSON object.")

        try:
            action = AgentAction.model_validate(raw)
        except ValidationError as exc:
            errors = exc.errors()
            message = str(errors[0]["msg"]) if errors else "invalid"
            raise AgentActionRejectedError(
                f"The action failed validation: {message}",
                action_type=str(raw.get("action_type", "")),
            ) from exc

        if action.action_type not in allowed_actions:
            raise AgentActionRejectedError(
                f"The {self.agent_kind} agent is not permitted to propose "
                f"'{action.action_type.value}'.",
                action_type=action.action_type.value,
                allowed=sorted(item.value for item in allowed_actions),
            )

        self._refuse_foreign_anchors(action.action_type, action.evidence_anchor_ids, valid_anchors)

        model = ACTION_PAYLOAD_MODELS[action.action_type]
        try:
            payload = model.model_validate(action.payload)
        except ValidationError as exc:
            errors = exc.errors()
            location = ".".join(str(part) for part in errors[0]["loc"]) if errors else ""
            message = str(errors[0]["msg"]) if errors else "invalid"
            raise AgentActionRejectedError(
                f"The {action.action_type.value} payload is invalid"
                + (f" at '{location}'" if location else "")
                + f": {message}",
                action_type=action.action_type.value,
            ) from exc

        # The action-level list is not the only way an anchor id reaches the
        # database: a finding's ``evidence`` entries and a change operation's
        # ``basis`` each carry their own. Checking only the first list meant a
        # single in-project citation at the top of an action bought passage for
        # any number of foreign ones underneath it.
        self._refuse_foreign_anchors(
            action.action_type, _payload_anchor_ids(payload), valid_anchors
        )
        self._enforce_approval_rules(action, payload)
        self._enforce_scope(action, payload, allowed_object_types)
        self._enforce_project_state_rules(action, payload)
        return ValidatedAction(action=action, payload=payload)

    def _refuse_foreign_anchors(
        self,
        action_type: ActionType,
        anchor_ids: Sequence[uuid.UUID],
        valid_anchors: set[uuid.UUID],
    ) -> None:
        """A citation to an anchor outside this project is never evidence.

        It is either a hallucinated id or a cross-project leak: a foreign
        anchor's source label, file path and quoted excerpt render straight into
        the delivered report of whichever project cited it.
        """
        unknown = [str(anchor_id) for anchor_id in anchor_ids if anchor_id not in valid_anchors]
        if unknown:
            raise AgentActionRejectedError(
                "The action cites evidence anchors that do not belong to this "
                f"project: {', '.join(unknown[:5])}.",
                action_type=action_type.value,
            )

    def _enforce_approval_rules(self, action: AgentAction, payload: Any) -> None:
        """The safety gate, enforced in code rather than requested in a prompt."""
        if action.action_type is ActionType.CREATE_RECOMMENDATION:
            safety = bool(getattr(payload, "safety_related", False))
            damage = bool(getattr(payload, "equipment_damage_related", False))
            if (safety or damage) and not action.requires_approval:
                # Coerced, not refused. Refusing discarded the recommendation
                # while the byte-identical action declaring `safety_related:
                # false` was accepted — and the executor marks every
                # recommendation as requiring approval either way, so the
                # refusal changed nothing except which proposals survived. That
                # gradient pointed at under-declaring safety relevance, which is
                # the one direction this rule exists to prevent.
                action.requires_approval = True
                action.warnings = [
                    *action.warnings,
                    "Marked as requiring approval: the recommendation declares itself "
                    "safety- or equipment-damage-related.",
                ]

    def _enforce_scope(
        self, action: AgentAction, payload: Any, allowed_object_types: frozenset[ObjectType]
    ) -> None:
        """Refuse an operation on an object type this agent was not granted."""
        if not allowed_object_types or not isinstance(payload, ProposeChangesetPayload):
            return
        outside = sorted(
            {
                operation.object_type.value
                for operation in payload.operations
                if operation.object_type not in allowed_object_types
            }
        )
        if outside:
            raise AgentActionRejectedError(
                f"The {self.agent_kind} agent may propose changes to "
                f"{', '.join(sorted(item.value for item in allowed_object_types))} "
                f"and nothing else; this change set also touches {', '.join(outside)}.",
                action_type=action.action_type.value,
            )

    def _enforce_project_state_rules(self, action: AgentAction, payload: Any) -> None:
        """The rules that need a row in front of them rather than a schema.

        Each of these compares what the model proposed against what the project
        actually holds, which is why none of them can live in the payload model.
        """
        if isinstance(payload, UpdateFindingReviewPayload):
            self._check_finding_review(action, payload)
        elif isinstance(payload, CreateFindingPayload):
            self._check_finding_create(action, payload)
        elif isinstance(payload, ReviewChangesetPayload):
            self._check_changeset_review(action, payload)
        elif isinstance(payload, ProposeChangesetPayload):
            self._check_template_bases(action, payload)

    def _finding_in_project(self, action: AgentAction, finding_id: uuid.UUID) -> Finding:
        finding = self.db.get(Finding, finding_id)
        if finding is None or finding.project_id != self.project.id:
            raise AgentActionRejectedError(
                f"Finding {finding_id} is not a finding in this project.",
                action_type=action.action_type.value,
            )
        return finding

    def _check_finding_review(
        self, action: AgentAction, payload: UpdateFindingReviewPayload
    ) -> None:
        finding = self._finding_in_project(action, payload.finding_id)

        if decided_by_a_human(finding):
            # A human decision is the end of the argument. Re-verdicting one in
            # place flipped an accepted critical finding to rejected — removing
            # it from the delivered report — while leaving the engineer's name,
            # timestamp and decision notes attached to it.
            raise AgentActionRejectedError(
                f"Finding {finding.id} was decided by a named engineer"
                + (f" on {finding.decided_at:%Y-%m-%d}" if finding.decided_at else "")
                + ". A decided finding is superseded by a new finding and a fresh "
                "approval, never re-verdicted in place.",
                action_type=action.action_type.value,
            )

        adjusted = payload.adjusted_confidence
        if adjusted is not None and adjusted > finding.confidence:
            # Compared against the finding, which is the number being changed.
            # Comparing it against the reviewer's confidence in its own review
            # compared unrelated quantities: an action that omitted `confidence`
            # (default 0.0) disabled the check entirely, and a reviewer honest
            # enough to state low confidence in its own review could not lower a
            # finding at all.
            raise AgentActionRejectedError(
                f"Senior review may lower a finding's confidence but not raise it: "
                f"finding {finding.id} stands at {finding.confidence:.2f} and this "
                f"review proposes {adjusted:.2f}.",
                action_type=action.action_type.value,
            )

    def _check_finding_create(self, action: AgentAction, payload: CreateFindingPayload) -> None:
        """Refuse to overwrite a finding a human has decided on.

        ``dedupe_key`` addresses a row. Re-running an agent is meant to converge
        on the finding it wrote last time; rewriting one that a named engineer
        has already accepted is a different act, and it published text nobody
        had read under that engineer's decision.
        """
        key = finding_dedupe_key(self.agent_kind, payload.dedupe_key, payload.title)
        existing = self.db.execute(
            select(Finding).where(Finding.project_id == self.project.id, Finding.dedupe_key == key)
        ).scalar_one_or_none()
        if existing is not None and decided_by_a_human(existing):
            raise AgentActionRejectedError(
                f"Finding {existing.id} ({existing.title!r}) was decided by a named "
                f"engineer and cannot be rewritten. Raise the new observation under a "
                f"different dedupe_key so both the decision and the new finding survive.",
                action_type=action.action_type.value,
            )

    def _check_changeset_review(self, action: AgentAction, payload: ReviewChangesetPayload) -> None:
        """The same confidence rule the payload has always claimed to be under.

        ``ReviewChangesetPayload.adjusted_confidence`` documented itself as
        "enforced by the runner" while the runner inspected only
        ``update_finding_review``.
        """
        changeset = self.db.get(ChangeSet, payload.changeset_id)
        if changeset is None or changeset.project_id != self.project.id:
            raise AgentActionRejectedError(
                f"Change set {payload.changeset_id} is not a change set in this project.",
                action_type=action.action_type.value,
            )
        adjusted = payload.adjusted_confidence
        if adjusted is not None and adjusted > changeset.confidence:
            raise AgentActionRejectedError(
                f"Review may lower a change set's confidence but not raise it: change "
                f"set {changeset.id} stands at {changeset.confidence:.2f} and this "
                f"review proposes {adjusted:.2f}.",
                action_type=action.action_type.value,
            )

    def _check_template_bases(self, action: AgentAction, payload: ProposeChangesetPayload) -> None:
        """A template basis must name a template this installation has loaded.

        ``kind=template_rule`` is what stamps a created object
        ``LogicOrigin.TEMPLATE_EXPANSION`` — the strongest provenance the system
        records, meaning "produced by an approved, checksummed, once-reviewed
        pattern". An invented id turns that into a citation a reviewer cannot
        open, which reads as provenance and is not.
        """
        registry = self._registry()
        unknown: list[str] = []
        for operation in payload.operations:
            for basis in operation.basis:
                if not basis.template_id:
                    continue
                template_id, _, version = basis.template_id.partition("@")
                if registry.get(template_id, version or None) is None:
                    unknown.append(basis.template_id)
        if unknown:
            available = ", ".join(registry.ids[:10]) or "none"
            raise AgentActionRejectedError(
                f"A basis cites templates this installation has not loaded: "
                f"{', '.join(sorted(set(unknown))[:5])}. Available template ids: "
                f"{available}.",
                action_type=action.action_type.value,
            )


def _payload_anchor_ids(payload: Any) -> list[uuid.UUID]:
    """Every anchor id reachable from a payload, whatever shape it arrived in."""
    anchor_ids: list[uuid.UUID] = []
    if isinstance(payload, CreateFindingPayload):
        anchor_ids.extend(reference.anchor_id for reference in payload.evidence)
    elif isinstance(payload, ProposeChangesetPayload):
        anchor_ids.extend(
            basis.anchor_id
            for operation in payload.operations
            for basis in operation.basis
            if basis.anchor_id is not None
        )
    return anchor_ids


def _encode(context: dict[str, Any]) -> str:
    import json

    return json.dumps(context, ensure_ascii=False, sort_keys=True, default=str)


def _action_record(item: ValidatedAction) -> dict[str, Any]:
    action = item.action
    return {
        "action_type": action.action_type.value,
        "summary": action.summary,
        "target_type": action.target_type,
        "target_id": str(action.target_id) if action.target_id else None,
        "evidence_anchor_ids": [str(value) for value in action.evidence_anchor_ids],
        "confidence": action.confidence,
        "requires_approval": action.requires_approval,
        "warnings": action.warnings,
        "accepted": True,
        "rejection_reason": "",
    }
