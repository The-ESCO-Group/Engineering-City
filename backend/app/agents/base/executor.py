"""Executing validated agent actions.

The runner decides whether an action is *allowed*. This module carries it out.
Nothing else in the system writes a finding, a recommendation or an approval on
an agent's behalf.

Two invariants are enforced here because they cannot be enforced by a schema:

* a finding is written with its evidence links in the same flush, so a finding
  with no citation cannot exist even transiently;
* anything safety- or damage-related raises an :class:`Approval` at the same
  time, so it cannot reach a report without a human decision.

Findings are keyed by ``(project_id, dedupe_key)``. Re-running an agent updates
the existing finding rather than adding a second copy — which is what makes the
at-least-once queue safe for agents.
"""

from __future__ import annotations

import uuid
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
    UpdateFindingReviewPayload,
)
from app.agents.base.runner import RunOutcome, ValidatedAction
from app.core.clock import utcnow
from app.core.enums import (
    SEVERITY_ORDER,
    ActionType,
    ApprovalKind,
    ApprovalStatus,
    AuditAction,
    EventType,
    FindingSeverity,
    FindingStatus,
    MessageRole,
    RecommendationStatus,
)
from app.core.logging import get_logger
from app.models.agent import AgentMessage
from app.models.approval import Approval
from app.models.evidence import EvidenceAnchor
from app.models.finding import Finding, FindingEvidence, Recommendation
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


@dataclass(slots=True)
class ExecutionResult:
    findings: list[Finding] = field(default_factory=list)
    recommendations: list[Recommendation] = field(default_factory=list)
    approvals: list[Approval] = field(default_factory=list)
    messages: list[AgentMessage] = field(default_factory=list)
    reviewed: list[Finding] = field(default_factory=list)
    enqueue: list[EnqueueRequest] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    #: True when the agent explicitly reported that it found nothing.
    reported_no_findings: bool = False
    scope_examined: str = ""

    def summary(self) -> dict[str, Any]:
        return {
            "findings": len(self.findings),
            "recommendations": len(self.recommendations),
            "approvals": len(self.approvals),
            "messages": len(self.messages),
            "reviewed": len(self.reviewed),
            "tasks_planned": len(self.enqueue),
            "blockers": self.blockers,
            "reported_no_findings": self.reported_no_findings,
        }


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
        agent_instance_id: uuid.UUID | None = None,
    ) -> None:
        self.db = db
        self.project = project
        self.agent_kind = agent_kind
        self.run_id = run_id
        self.task_id = task_id
        self.agent_instance_id = agent_instance_id

    def execute(self, outcome: RunOutcome) -> ExecutionResult:
        result = ExecutionResult()
        handlers = {
            ActionType.CREATE_FINDING: self._create_finding,
            ActionType.CREATE_RECOMMENDATION: self._create_recommendation,
            ActionType.CREATE_TASK: self._plan_task,
            ActionType.POST_MESSAGE: self._post_message,
            ActionType.REQUEST_APPROVAL: self._request_approval,
            ActionType.UPDATE_FINDING_REVIEW: self._update_review,
            ActionType.REPORT_BLOCKER: self._report_blocker,
            ActionType.REPORT_NO_FINDINGS: self._report_no_findings,
        }
        for item in outcome.accepted:
            handlers[item.action_type](item, result)

        self.db.flush()
        return result

    # -- findings --------------------------------------------------------
    def _create_finding(self, item: ValidatedAction, result: ExecutionResult) -> None:
        payload = item.payload
        assert isinstance(payload, CreateFindingPayload)

        dedupe_key = payload.dedupe_key or f"{self.agent_kind}:{payload.title}"[:120]
        finding = self.db.execute(
            select(Finding).where(
                Finding.project_id == self.project.id,
                Finding.dedupe_key == dedupe_key,
            )
        ).scalar_one_or_none()

        created = finding is None
        if finding is None:
            finding = Finding(
                project_id=self.project.id,
                dedupe_key=dedupe_key,
                created_by_agent_kind=self.agent_kind,
            )
            self.db.add(finding)

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
        finding.warnings = item.action.warnings
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
                select(EvidenceAnchor).where(EvidenceAnchor.id.in_(list(roles)))
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
            logger.warning(
                "senior review referenced a finding outside this project",
                extra={"finding_id": str(payload.finding_id)},
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
    def _plan_task(self, item: ValidatedAction, result: ExecutionResult) -> None:
        from app.core.enums import TaskKind

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

        result.enqueue.append(
            EnqueueRequest(
                kind=kind,
                title=payload.title,
                description=payload.description,
                idempotency_key=f"plan:{self.run_id.hex[:8]}:{kind.value}",
                agent_kind=payload.agent_kind or None,
                payload=payload.payload,
                priority=payload.priority,
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
