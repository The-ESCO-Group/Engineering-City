"""Executing validated agent actions.

The runner decides whether an action is *allowed*. This module carries it out.
Nothing else in the system writes a finding, a recommendation or an approval on
an agent's behalf.

Two invariants are enforced here because they cannot be enforced by a schema:

* a finding is written with its evidence links in the same flush, so a finding
  with no citation cannot exist even transiently;
* anything safety- or damage-related raises an :class:`Approval` at the same
  time, so it cannot reach a report without a human decision.

Findings are keyed by ``(project_id, dedupe_key)``, and the key is namespaced by
the agent that produced it. Re-running an agent updates the existing finding
rather than adding a second copy — which is what makes the at-least-once queue
safe for agents — while one agent cannot address another's row.

A question is keyed the same way, by a reference derived from the task and the
payload, so an exact retry returns the question it already raised instead of
queueing a second identical decision. Whether that row is still open or has since
been answered is reported separately, in
:attr:`ExecutionResult.settled_questions`: a caller that cannot tell the two
apart reports a question a human has answered as one still waiting for them, and
the run never moves.

Two things this module refuses outright, because both leave a human decision
attached to something the human never saw:

* rewriting or re-verdicting a finding a named engineer has already decided;
* publishing a senior-review verdict about content that has since been replaced.

An action type with no handler is recorded as unexecuted with a reason. It used
to raise ``KeyError`` straight into the task worker, after the provider call had
already been billed.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.base.actions import (
    CreateFindingPayload,
    CreateRecommendationPayload,
    CreateTaskPayload,
    PostMessagePayload,
    ReportBlockerPayload,
    ReportNoFindingsPayload,
    RequestApprovalPayload,
    RequestClarificationPayload,
    UpdateFindingReviewPayload,
    finding_dedupe_key,
)
from app.agents.base.runner import RunOutcome, ValidatedAction, decided_by_a_human
from app.core.clock import utcnow
from app.core.enums import (
    SEVERITY_ORDER,
    ActionType,
    AgentKind,
    ApprovalKind,
    ApprovalStatus,
    AuditAction,
    ClarificationStatus,
    EventType,
    FindingSeverity,
    FindingStatus,
    MessageRole,
    RecommendationStatus,
    SemanticLocation,
    TaskKind,
)
from app.core.logging import get_logger
from app.models.agent import AgentMessage
from app.models.approval import Approval
from app.models.evidence import EvidenceAnchor
from app.models.finding import Finding, FindingEvidence, Recommendation
from app.models.profile import ProjectQuestion
from app.models.project import Project
from app.orchestration.events import record_event
from app.orchestration.queue import EnqueueRequest
from app.security import audit

logger = get_logger(__name__)

#: Severities that always need a human decision before they can be released.
#: A high-severity interlock claim reaching a report unreviewed is the failure
#: mode this product exists to prevent.
APPROVAL_SEVERITIES: frozenset[FindingSeverity] = frozenset(
    {FindingSeverity.HIGH, FindingSeverity.CRITICAL}
)

#: Question statuses that still leave a human owing an answer — the same pair
#: :attr:`~app.models.profile.ProjectQuestion.is_blocking` treats as outstanding.
#: Everything else has been through a person: answered, dismissed as no longer
#: needed, or superseded. Deferring is not an answer.
UNANSWERED_QUESTION_STATUSES: frozenset[ClarificationStatus] = frozenset(
    {ClarificationStatus.OPEN, ClarificationStatus.DEFERRED}
)


@dataclass(slots=True)
class UnexecutedAction:
    """An action the runner accepted that the executor did not carry out.

    Kept rather than discarded for the same reason a rejection is: what the
    system declined to do on an agent's behalf is part of the record.
    """

    action_type: str
    summary: str
    reason: str

    def as_dict(self) -> dict[str, str]:
        return {
            "action_type": self.action_type,
            "summary": self.summary,
            "reason": self.reason,
        }


@dataclass(slots=True)
class ActionHandoff:
    """An accepted action explicitly handed to a caller-owned handler.

    The executor owns database actions such as creating findings and questions.
    A production change-set proposal is different: the task handler must run its
    deterministic guard, build the change set, and merge it.  Recording that
    boundary separately keeps a deliberate handoff distinct from an action for
    which the installation has no handler at all.
    """

    action_type: str
    summary: str
    handler: str

    def as_dict(self) -> dict[str, str]:
        return {
            "action_type": self.action_type,
            "summary": self.summary,
            "handler": self.handler,
        }


@dataclass(slots=True)
class ExecutionResult:
    findings: list[Finding] = field(default_factory=list)
    recommendations: list[Recommendation] = field(default_factory=list)
    approvals: list[Approval] = field(default_factory=list)
    messages: list[AgentMessage] = field(default_factory=list)
    questions: list[ProjectQuestion] = field(default_factory=list)
    #: The subset of ``questions`` a human has already been through. An accepted
    #: clarification is idempotent by reference, so a re-run legitimately returns
    #: a row somebody answered days ago; a caller that could not tell that from a
    #: question just asked would report "waiting for you" for ever.
    settled_questions: list[ProjectQuestion] = field(default_factory=list)
    reviewed: list[Finding] = field(default_factory=list)
    enqueue: list[EnqueueRequest] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    #: Actions explicitly handed to a named caller-owned handler.
    handoffs: list[ActionHandoff] = field(default_factory=list)
    #: Actions that were validated but not carried out, each with the reason.
    unexecuted: list[UnexecutedAction] = field(default_factory=list)
    #: True when the agent explicitly reported that it found nothing.
    reported_no_findings: bool = False
    scope_examined: str = ""

    def summary(self) -> dict[str, Any]:
        return {
            "findings": len(self.findings),
            "recommendations": len(self.recommendations),
            "approvals": len(self.approvals),
            "messages": len(self.messages),
            "questions_raised": len(self.questions),
            "questions_already_settled": [item.reference for item in self.settled_questions],
            "reviewed": len(self.reviewed),
            "tasks_planned": len(self.enqueue),
            "blockers": self.blockers,
            "action_handoffs": [item.as_dict() for item in self.handoffs],
            "unexecuted": [item.as_dict() for item in self.unexecuted],
            "reported_no_findings": self.reported_no_findings,
        }


#: What every action handler looks like.
Handler = Callable[["ValidatedAction", ExecutionResult], None]


class ActionExecutor:
    """Carries out the actions a run produced."""

    def __init__(
        self,
        db: Session,
        project: Project,
        *,
        agent_kind: str,
        run_id: uuid.UUID,
        task_id: uuid.UUID | None = None,
        workflow_run_id: uuid.UUID | None = None,
        agent_instance_id: uuid.UUID | None = None,
        handoff_handlers: Mapping[ActionType, str] | None = None,
    ) -> None:
        self.db = db
        self.project = project
        self.agent_kind = agent_kind
        self.run_id = run_id
        self.task_id = task_id
        self.workflow_run_id = workflow_run_id
        self.agent_instance_id = agent_instance_id
        self.handoff_handlers = dict(handoff_handlers or {})

    def execute(self, outcome: RunOutcome) -> ExecutionResult:
        result = ExecutionResult()
        handlers: dict[ActionType, Handler] = {
            ActionType.CREATE_FINDING: self._create_finding,
            ActionType.CREATE_RECOMMENDATION: self._create_recommendation,
            ActionType.CREATE_TASK: self._plan_task,
            ActionType.POST_MESSAGE: self._post_message,
            ActionType.REQUEST_APPROVAL: self._request_approval,
            ActionType.REQUEST_CLARIFICATION: self._request_clarification,
            ActionType.UPDATE_FINDING_REVIEW: self._update_review,
            ActionType.REPORT_BLOCKER: self._report_blocker,
            ActionType.REPORT_NO_FINDINGS: self._report_no_findings,
        }
        for item in outcome.accepted:
            handler = handlers.get(item.action_type)
            if handler is None:
                handoff_handler = self.handoff_handlers.get(item.action_type)
                if handoff_handler:
                    result.handoffs.append(
                        ActionHandoff(
                            action_type=item.action_type.value,
                            summary=item.action.summary[:500],
                            handler=handoff_handler,
                        )
                    )
                    continue
                # Dispatch is total by construction rather than by hope. Six of
                # the fourteen action types had no handler, so every production
                # agent's happy path — a tag architect returning the change set
                # its prompt asks for — raised KeyError out of the executor and
                # into the task worker, after the provider call had been billed
                # and the run row written.
                self._refuse(
                    item,
                    result,
                    f"This installation has no handler for '{item.action_type.value}', "
                    f"so the action was recorded and not carried out.",
                )
                continue
            handler(item, result)

        self.db.flush()
        return result

    def _refuse(self, item: ValidatedAction, result: ExecutionResult, reason: str) -> None:
        """Record an accepted action that was not carried out, and why."""
        result.unexecuted.append(
            UnexecutedAction(
                action_type=item.action_type.value,
                summary=item.action.summary[:500],
                reason=reason,
            )
        )
        logger.warning(
            "an accepted action was not executed",
            extra={
                "agent_kind": self.agent_kind,
                "action_type": item.action_type.value,
                "reason": reason,
            },
        )
        audit.record(
            self.db,
            AuditAction.AGENT_ACTION_REJECTED,
            project_id=self.project.id,
            actor_agent_kind=self.agent_kind,
            target_type="agent_run",
            target_id=self.run_id,
            summary=f"Did not execute a {item.action_type.value} action",
            details={"reason": reason},
        )

    # -- findings --------------------------------------------------------
    def _create_finding(self, item: ValidatedAction, result: ExecutionResult) -> None:
        payload = item.payload
        assert isinstance(payload, CreateFindingPayload)

        dedupe_key = finding_dedupe_key(self.agent_kind, payload.dedupe_key, payload.title)
        finding = self.db.execute(
            select(Finding).where(
                Finding.project_id == self.project.id,
                Finding.dedupe_key == dedupe_key,
            )
        ).scalar_one_or_none()

        created = finding is None
        extra_warnings: list[str] = []
        if finding is None:
            finding = Finding(
                project_id=self.project.id,
                dedupe_key=dedupe_key,
                created_by_agent_kind=self.agent_kind,
            )
            self.db.add(finding)
        elif decided_by_a_human(finding):
            # Second gate. The runner refuses this too, but the runner is not the
            # only way here and a human decision is not something to lose on a
            # code path nobody thought about.
            self._refuse(
                item,
                result,
                f"Finding {finding.id} was decided by a named engineer; it is not "
                f"rewritten in place. Raise the new observation under a different "
                f"dedupe_key.",
            )
            return
        elif finding.is_reviewed and _materially_different(finding, payload):
            # A senior-review verdict is about the text it was made against. When
            # the text changes underneath it, keeping the verdict would publish
            # "reviewed and supported" over content no reviewer read.
            finding.is_reviewed = False
            finding.review_verdict = ""
            finding.reviewed_at = None
            extra_warnings.append(
                "The senior review of this finding was cleared because its content "
                "was replaced after the review was made."
            )

        finding.task_id = self.task_id
        finding.agent_run_id = self.run_id
        finding.title = payload.title
        finding.category = payload.category
        finding.severity = payload.severity
        finding.fact = payload.fact
        finding.inference = payload.inference
        finding.suggested_review_action = payload.suggested_review_action
        finding.limitations = payload.limitations
        finding.confidence = item.action.confidence
        finding.details = payload.details
        finding.warnings = [*item.action.warnings, *extra_warnings]
        finding.requires_approval = (
            item.action.requires_approval or payload.severity in APPROVAL_SEVERITIES
        )
        if created:
            finding.status = FindingStatus.OPEN
            finding.original_confidence = item.action.confidence
            finding.original_severity = payload.severity.value
        self.db.flush()

        self._link_evidence(finding, item, payload)

        # Enforced here rather than trusted from the schema: a finding with no
        # surviving evidence link must not exist at all.
        if not finding.evidence_links:
            logger.warning(
                "discarding a finding whose evidence links did not resolve",
                extra={"finding_id": str(finding.id), "title": finding.title},
            )
            self.db.delete(finding)
            self.db.flush()
            return

        result.findings.append(finding)
        audit.record(
            self.db,
            AuditAction.FINDING_CREATED if created else AuditAction.FINDING_UPDATED,
            project_id=self.project.id,
            actor_agent_kind=self.agent_kind,
            target_type="finding",
            target_id=finding.id,
            summary=f"{'Created' if created else 'Updated'} finding: {finding.title}",
            details={
                "category": finding.category.value,
                "severity": finding.severity.value,
                "confidence": finding.confidence,
                "evidence_count": len(finding.evidence_links),
            },
        )
        record_event(
            self.db,
            project_id=self.project.id,
            event_type=EventType.FINDING_CREATED if created else EventType.FINDING_UPDATED,
            task_id=self.task_id,
            agent_id=self.agent_instance_id,
            payload=_finding_payload(finding),
        )

        if finding.requires_approval:
            self._raise_finding_approval(finding, result)

    def _link_evidence(
        self, finding: Finding, item: ValidatedAction, payload: CreateFindingPayload
    ) -> None:
        """Attach the citations, keeping the role each plays in the comparison."""
        roles: dict[uuid.UUID, tuple[str, str]] = {
            reference.anchor_id: (reference.role, reference.note) for reference in payload.evidence
        }
        for anchor_id in item.action.evidence_anchor_ids:
            roles.setdefault(anchor_id, ("context", ""))

        existing = {(link.anchor_id, link.role) for link in finding.evidence_links}
        anchors = {
            anchor.id: anchor
            for anchor in self.db.execute(
                # Scoped to the project as the second gate behind the runner's
                # check. A link written to a foreign anchor renders that other
                # customer's file path, location and quoted excerpt into this
                # project's delivered report.
                select(EvidenceAnchor).where(
                    EvidenceAnchor.id.in_(list(roles)),
                    EvidenceAnchor.project_id == self.project.id,
                )
            ).scalars()
        }

        for anchor_id, (role, note) in roles.items():
            anchor = anchors.get(anchor_id)
            if anchor is None or (anchor_id, role) in existing:
                continue
            self.db.add(
                FindingEvidence(
                    finding_id=finding.id,
                    anchor_id=anchor_id,
                    # The anchor knows whether it is a quotation, an inference or
                    # a record of something absent; the finding inherits that
                    # rather than asserting its own.
                    evidence_kind=anchor.evidence_kind,
                    role=role,
                    note=note,
                )
            )
        self.db.flush()
        self.db.refresh(finding)

    def _update_review(self, item: ValidatedAction, result: ExecutionResult) -> None:
        payload = item.payload
        assert isinstance(payload, UpdateFindingReviewPayload)

        finding = self.db.get(Finding, payload.finding_id)
        if finding is None or finding.project_id != self.project.id:
            self._refuse(
                item,
                result,
                f"Finding {payload.finding_id} is not a finding in this project.",
            )
            return

        if decided_by_a_human(finding):
            # Second gate behind the runner. A verdict of 'unsupported' set
            # status to rejected and cleared requires_approval, which removed an
            # accepted critical safety finding from the delivered report while
            # the engineer's name, timestamp and decision notes stayed attached
            # to it. A human decision is the end of the argument.
            self._refuse(
                item,
                result,
                f"Finding {finding.id} was decided by a named engineer and is not "
                f"re-verdicted in place. Raise a new finding and a fresh approval "
                f"if the conclusion has changed.",
            )
            return

        if finding.original_confidence is None:
            finding.original_confidence = finding.confidence
        if not finding.original_severity:
            finding.original_severity = finding.severity.value

        finding.is_reviewed = True
        finding.review_verdict = payload.verdict
        finding.review_notes = payload.notes
        finding.reviewed_at = utcnow()

        if payload.adjusted_confidence is not None:
            # Only ever downward. The runner refuses an increase, and this is the
            # second gate: a review that could raise confidence would let a weak
            # claim be laundered into a strong one.
            finding.confidence = min(finding.confidence, payload.adjusted_confidence)
        if payload.adjusted_severity is not None:
            if SEVERITY_ORDER[payload.adjusted_severity] > SEVERITY_ORDER[finding.severity]:
                finding.warnings = [
                    *finding.warnings,
                    f"Senior review proposed raising severity to "
                    f"{payload.adjusted_severity.value}; recorded as a note rather "
                    f"than applied.",
                ]
            else:
                finding.severity = payload.adjusted_severity

        if payload.verdict == "unsupported":
            finding.status = FindingStatus.REJECTED
            finding.requires_approval = False
        elif payload.verdict == "duplicate":
            finding.status = FindingStatus.REJECTED
            finding.superseded_by_id = payload.duplicate_of_finding_id
            finding.requires_approval = False

        if payload.parser_limitations:
            finding.limitations = "; ".join(
                filter(None, [finding.limitations, *payload.parser_limitations])
            )[:4000]

        self.db.flush()
        result.reviewed.append(finding)

        record_event(
            self.db,
            project_id=self.project.id,
            event_type=EventType.FINDING_REVIEWED,
            task_id=self.task_id,
            agent_id=self.agent_instance_id,
            payload={**_finding_payload(finding), "review_verdict": finding.review_verdict},
        )

    # -- recommendations --------------------------------------------------
    def _create_recommendation(self, item: ValidatedAction, result: ExecutionResult) -> None:
        payload = item.payload
        assert isinstance(payload, CreateRecommendationPayload)

        recommendation = Recommendation(
            project_id=self.project.id,
            finding_id=payload.finding_id,
            agent_run_id=self.run_id,
            created_by_agent_kind=self.agent_kind,
            title=payload.title,
            body=payload.body,
            rationale=payload.rationale,
            status=RecommendationStatus.PROPOSED,
            safety_related=payload.safety_related,
            equipment_damage_related=payload.equipment_damage_related,
            # Every recommendation is a proposal for a human. Nothing in this
            # product applies one, so none of them is ever "already decided".
            requires_approval=True,
            confidence=item.action.confidence,
            effort_estimate=payload.effort_estimate,
            details=payload.details,
        )
        self.db.add(recommendation)
        self.db.flush()
        result.recommendations.append(recommendation)

        kind = ApprovalKind.DESIGN_RECOMMENDATION
        if payload.safety_related:
            kind = ApprovalKind.SAFETY_RECOMMENDATION
        elif payload.equipment_damage_related:
            kind = ApprovalKind.EQUIPMENT_DAMAGE_RECOMMENDATION

        self._create_approval(
            kind=kind,
            title=payload.title,
            question=(f"Do you accept this recommendation as engineering guidance? {payload.body}"),
            recommendation_id=recommendation.id,
            finding_id=payload.finding_id,
            context={
                "safety_related": payload.safety_related,
                "equipment_damage_related": payload.equipment_damage_related,
                "confidence": item.action.confidence,
            },
            result=result,
        )

    # -- approvals --------------------------------------------------------
    def _raise_finding_approval(self, finding: Finding, result: ExecutionResult) -> None:
        self._create_approval(
            kind=ApprovalKind.FINDING_RELEASE,
            title=f"Release finding: {finding.title}",
            question=(
                f"This finding is rated {finding.severity.value} with confidence "
                f"{finding.confidence:.2f}. Do you accept it as an engineering "
                f"observation to be reported? It was produced by an automated "
                f"review and has not been confirmed by an engineer."
            ),
            finding_id=finding.id,
            context={
                "severity": finding.severity.value,
                "category": finding.category.value,
                "confidence": finding.confidence,
                "fact": finding.fact[:1000],
                "inference": finding.inference[:1000],
                "limitations": finding.limitations[:1000],
                "evidence_count": len(finding.evidence_links),
            },
            result=result,
        )

    def _request_approval(self, item: ValidatedAction, result: ExecutionResult) -> None:
        payload = item.payload
        assert isinstance(payload, RequestApprovalPayload)
        self._create_approval(
            kind=payload.kind,
            title=payload.title,
            question=payload.question,
            finding_id=payload.finding_id,
            recommendation_id=payload.recommendation_id,
            context=payload.context,
            result=result,
        )

    def _create_approval(
        self,
        *,
        kind: ApprovalKind,
        title: str,
        question: str,
        result: ExecutionResult,
        finding_id: uuid.UUID | None = None,
        recommendation_id: uuid.UUID | None = None,
        context: dict[str, Any] | None = None,
    ) -> Approval:
        """Raise a pending approval, or return the one already open.

        Deduplicated on ``(finding, recommendation, kind)`` so a re-run does not
        pile up identical decisions in an engineer's queue.
        """
        existing = self.db.execute(
            select(Approval).where(
                Approval.project_id == self.project.id,
                Approval.kind == kind,
                Approval.finding_id == finding_id,
                Approval.recommendation_id == recommendation_id,
                Approval.status == ApprovalStatus.PENDING,
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing

        approval = Approval(
            project_id=self.project.id,
            task_id=self.task_id,
            finding_id=finding_id,
            recommendation_id=recommendation_id,
            kind=kind,
            status=ApprovalStatus.PENDING,
            title=title[:255],
            question=question,
            # Snapshotted so a later edit cannot change what was approved.
            context=context or {},
            requested_by_agent_kind=self.agent_kind,
            requested_at=utcnow(),
        )
        self.db.add(approval)
        self.db.flush()
        result.approvals.append(approval)

        audit.record(
            self.db,
            AuditAction.APPROVAL_REQUESTED,
            project_id=self.project.id,
            actor_agent_kind=self.agent_kind,
            target_type="approval",
            target_id=approval.id,
            summary=f"Requested approval: {approval.title}",
            details={"kind": kind.value},
        )
        record_event(
            self.db,
            project_id=self.project.id,
            event_type=EventType.APPROVAL_REQUESTED,
            task_id=self.task_id,
            agent_id=self.agent_instance_id,
            payload={
                "approval_id": str(approval.id),
                "kind": kind.value,
                "status": approval.status.value,
                "title": approval.title,
                "finding_id": str(finding_id) if finding_id else None,
                "recommendation_id": str(recommendation_id) if recommendation_id else None,
            },
        )
        return approval

    # -- planning and messages -------------------------------------------
    def _request_clarification(self, item: ValidatedAction, result: ExecutionResult) -> None:
        """Persist an accepted question so a human can actually answer it.

        The reference is deterministic for the task and payload. A leased task
        may run more than once; an exact retry must return the same question,
        while a materially different question remains a separate decision.

        Returning the existing row is therefore right, and returning it *silently*
        was the defect: a question somebody answered came back looking exactly
        like one just asked, so the caller reported the run as waiting for an
        answer that had already been given. The row is still returned; which of
        the two it is, is now on the record.
        """
        payload = item.payload
        assert isinstance(payload, RequestClarificationPayload)

        namespace = self.task_id or self.run_id
        signature = payload.model_dump_json(exclude_none=True)
        reference = f"Q-{uuid.uuid5(namespace, signature).hex[:16].upper()}"
        question = self.db.execute(
            select(ProjectQuestion).where(
                ProjectQuestion.project_id == self.project.id,
                ProjectQuestion.reference == reference,
            )
        ).scalar_one_or_none()
        if question is not None:
            result.questions.append(question)
            if question.status not in UNANSWERED_QUESTION_STATUSES:
                result.settled_questions.append(question)
                logger.info(
                    "an accepted clarification repeats a question a human has already settled",
                    extra={
                        "agent_kind": self.agent_kind,
                        "reference": question.reference,
                        "status": question.status.value,
                    },
                )
            return

        attributes: dict[str, Any] = {
            "competing_readings": list(payload.competing_readings),
        }
        if payload.related_object_type is not None:
            attributes["related_object_type"] = payload.related_object_type.value
        if payload.related_object_id is not None:
            attributes["related_object_id"] = str(payload.related_object_id)

        question = ProjectQuestion(
            project_id=self.project.id,
            reference=reference,
            category=payload.category,
            question=payload.question,
            why_it_matters=payload.why_it_matters,
            is_mandatory=payload.is_mandatory,
            blocks_task_kinds=list(payload.blocks_task_kinds),
            asked_by_agent=self.agent_kind,
            raised_in_task_id=self.task_id,
            created_by_agent=self.agent_kind,
            created_in_revision=self.project.current_revision_number,
            updated_in_revision=self.project.current_revision_number,
            attributes=attributes,
        )
        self.db.add(question)
        self.db.flush()
        result.questions.append(question)

        audit.record(
            self.db,
            AuditAction.QUESTION_CREATED,
            project_id=self.project.id,
            actor_agent_kind=self.agent_kind,
            target_type="project_question",
            target_id=question.id,
            summary=f"Raised {question.reference}: {question.question[:200]}",
            details={
                "category": question.category.value,
                "is_mandatory": question.is_mandatory,
                "blocks_task_kinds": list(question.blocks_task_kinds),
            },
        )
        record_event(
            self.db,
            project_id=self.project.id,
            event_type=EventType.PROJECT_QUESTION_CREATED,
            task_id=self.task_id,
            agent_id=self.agent_instance_id,
            payload={
                "question_id": str(question.id),
                "reference": question.reference,
                "category": question.category.value,
                "status": question.status.value,
                "location": _question_location(self.agent_kind).value,
                "question": question.question[:500],
                "why_it_matters": question.why_it_matters[:500],
                "is_mandatory": question.is_mandatory,
                "is_blocking": question.is_blocking,
                "blocks_task_kinds": [
                    kind.value
                    for value in question.blocks_task_kinds
                    if (kind := _task_kind(value)) is not None
                ],
            },
        )

    def _plan_task(self, item: ValidatedAction, result: ExecutionResult) -> None:
        payload = item.payload
        assert isinstance(payload, CreateTaskPayload)
        try:
            kind = TaskKind(payload.kind)
        except ValueError:
            logger.warning(
                "the planner proposed an unknown task kind",
                extra={"kind": payload.kind, "agent_kind": self.agent_kind},
            )
            return

        # The provider run id changes on every retry, while the workflow run id
        # names the logical plan. Scope the key and task to the latter so an
        # at-least-once retry converges on the same specialist task and the
        # specialist remains visible as part of its parent workflow.
        scope_id = self.workflow_run_id or self.task_id or self.run_id
        result.enqueue.append(
            EnqueueRequest(
                kind=kind,
                title=payload.title,
                description=payload.description,
                idempotency_key=f"plan:{scope_id.hex}:{kind.value}",
                agent_kind=payload.agent_kind or None,
                payload=payload.payload,
                priority=payload.priority,
                run_id=self.workflow_run_id,
            )
        )

    def _post_message(self, item: ValidatedAction, result: ExecutionResult) -> None:
        payload = item.payload
        assert isinstance(payload, PostMessagePayload)
        self._message(payload.body, payload.context, result)

    def _report_blocker(self, item: ValidatedAction, result: ExecutionResult) -> None:
        payload = item.payload
        assert isinstance(payload, ReportBlockerPayload)
        result.blockers.append(payload.reason)
        self._message(
            payload.reason,
            {
                "missing_inputs": payload.missing_inputs,
                "resolvable_by_upload": payload.resolvable_by_upload,
            },
            result,
        )

    def _report_no_findings(self, item: ValidatedAction, result: ExecutionResult) -> None:
        """Record an explicit 'nothing found', with what was examined.

        Silence and a clean result must never be indistinguishable, so an agent
        that found nothing says so, says what it looked at, and says what limited
        it.
        """
        payload = item.payload
        assert isinstance(payload, ReportNoFindingsPayload)
        result.reported_no_findings = True
        result.scope_examined = payload.scope_examined
        body = f"No findings. Examined: {payload.scope_examined}"
        if payload.limitations:
            body += f" Limitations: {payload.limitations}"
        self._message(
            body,
            {"scope_examined": payload.scope_examined, "limitations": payload.limitations},
            result,
        )

    def _message(self, body: str, context: dict[str, Any], result: ExecutionResult) -> AgentMessage:
        message = AgentMessage(
            project_id=self.project.id,
            agent_instance_id=self.agent_instance_id,
            agent_run_id=self.run_id,
            task_id=self.task_id,
            role=MessageRole.AGENT,
            agent_kind=self.agent_kind,
            body=body[:8000],
            context=context,
        )
        self.db.add(message)
        self.db.flush()
        result.messages.append(message)

        record_event(
            self.db,
            project_id=self.project.id,
            event_type=EventType.AGENT_MESSAGE_CREATED,
            task_id=self.task_id,
            agent_id=self.agent_instance_id,
            payload={
                "message_id": str(message.id),
                "agent_id": str(self.agent_instance_id) if self.agent_instance_id else None,
                "agent_kind": self.agent_kind,
                "role": message.role.value,
                "body": message.body[:2000],
                "task_id": str(self.task_id) if self.task_id else None,
            },
        )
        return message


def _materially_different(finding: Finding, payload: CreateFindingPayload) -> bool:
    """Whether an incoming finding says something other than the stored one.

    Deliberately narrow: title, severity, category and the stated fact are what a
    reviewer reads and what a verdict is about. A changed inference or a reworded
    limitation does not invalidate the review, and treating every re-run as a
    content change would clear a verdict on every idempotent retry.
    """
    return (
        finding.title != payload.title
        or finding.severity is not payload.severity
        or finding.category is not payload.category
        or finding.fact != payload.fact
    )


def _task_kind(value: Any) -> TaskKind | None:
    try:
        return TaskKind(str(value))
    except ValueError:
        return None


def _question_location(agent_kind: str) -> SemanticLocation:
    return {
        AgentKind.REQUIREMENTS_PLANNING.value: SemanticLocation.REQUIREMENTS_HALL,
        AgentKind.TAG_ARCHITECTURE.value: SemanticLocation.TAG_FOUNDRY,
        AgentKind.PLC_LOGIC.value: SemanticLocation.LADDER_LOGIC_LAB,
    }.get(agent_kind, SemanticLocation.PROJECT_CORE)


def _finding_payload(finding: Finding) -> dict[str, Any]:
    return {
        "finding_id": str(finding.id),
        "title": finding.title,
        "category": finding.category.value,
        "severity": finding.severity.value,
        "status": finding.status.value,
        "confidence": finding.confidence,
        "requires_approval": finding.requires_approval,
        "evidence_count": len(finding.evidence_links),
        "created_by_agent_kind": finding.created_by_agent_kind,
    }
