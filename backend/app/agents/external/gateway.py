"""A capability-scoped ingress for OpenAI and Claude SDK workers.

External workers never receive a database handle and never write engineering
rows directly.  They submit the same structured action envelope used by native
agents; :class:`AgentRunner` validates it and :class:`ActionExecutor` applies
only the allowed, evidence-backed operations.  Approval and release decisions
are intentionally absent from this surface.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agents.base.agent import SHARED_RULES
from app.agents.base.executor import ActionExecutor, ExecutionResult
from app.agents.base.presence import announce_agent_presence, move_agent, set_agent_status
from app.agents.base.provider import ChatMessage, ProviderResponse
from app.agents.base.runner import RESPONSE_SCHEMA, AgentRunner, RunOutcome
from app.api.schemas.agent import AgentActionRead, AgentInstanceRead, AgentRunDetail
from app.api.schemas.external_agent import (
    ExternalAgentContextRead,
    ExternalAgentDeployRequest,
    ExternalAgentDeployResponse,
    ExternalAgentEndpoints,
    ExternalAgentProgressRequest,
    ExternalAgentSubmissionRequest,
    ExternalAgentSubmissionResponse,
)
from app.core.config import Settings
from app.core.enums import (
    ActionType,
    AgentRunStatus,
    AgentStatus,
    AuditAction,
    EventType,
    MessageRole,
    ProviderKind,
)
from app.core.errors import AuthenticationError, ConflictError, NotFoundError, ValidationError
from app.database.locking import lock_project_root
from app.models.agent import AgentDefinition, AgentInstance, AgentMessage, AgentRun
from app.models.evidence import EvidenceAnchor
from app.models.file import UploadedFile
from app.models.project import Project
from app.models.requirements import Requirement
from app.models.user import User
from app.orchestration.events import record_event
from app.security import audit, permissions
from app.security.external_agent_tokens import (
    ExternalAgentClaims,
    create_external_agent_token,
)

EXTERNAL_ALLOWED_ACTIONS: frozenset[ActionType] = frozenset(
    {
        ActionType.CREATE_FINDING,
        ActionType.CREATE_RECOMMENDATION,
        ActionType.POST_MESSAGE,
        ActionType.REQUEST_APPROVAL,
        ActionType.REQUEST_CLARIFICATION,
        ActionType.REPORT_BLOCKER,
        ActionType.REPORT_NO_FINDINGS,
    }
)


@dataclass(frozen=True, slots=True)
class ExternalAgentSession:
    claims: ExternalAgentClaims
    project: Project
    instance: AgentInstance
    deployed_by: User


@dataclass(slots=True)
class SubmittedSDKProvider:
    """One already-produced SDK result presented through the provider protocol."""

    kind: ProviderKind
    model: str
    data: dict[str, Any]
    prompt_tokens: int
    completion_tokens: int
    latency_ms: int

    def complete_structured(
        self,
        *,
        system: str,
        messages: list[ChatMessage],
        schema: dict[str, Any],
        max_output_tokens: int = 4096,
        temperature: float = 0.0,
    ) -> ProviderResponse:
        del system, messages, schema, max_output_tokens, temperature
        return ProviderResponse(
            data=self.data,
            provider=f"{self.kind.value}_sdk",
            model=self.model,
            prompt_tokens=self.prompt_tokens,
            completion_tokens=self.completion_tokens,
            latency_ms=self.latency_ms,
        )


def _require_current_write_authority(db: Session, claims: ExternalAgentClaims) -> User:
    user = db.get(User, claims.deployed_by_id)
    if user is None or not user.is_active:
        raise AuthenticationError("The account that deployed this agent is no longer active.")
    role = permissions.project_role_for(db, user, claims.project_id)
    if not permissions.can_write_project(role):
        raise AuthenticationError(
            "The account that deployed this agent no longer has project write access."
        )
    return user


def resolve_external_session(db: Session, claims: ExternalAgentClaims) -> ExternalAgentSession:
    """Resolve a token to live rows and revoke it when any authority changed."""

    project = db.get(Project, claims.project_id)
    instance = db.get(AgentInstance, claims.instance_id)
    if (
        project is None
        or instance is None
        or instance.project_id != claims.project_id
        or not instance.kind.startswith("external_")
    ):
        raise AuthenticationError("External agent mission is no longer available.")
    if instance.status is AgentStatus.OFFLINE:
        raise AuthenticationError("External agent mission was revoked.")
    deployed_by = _require_current_write_authority(db, claims)
    return ExternalAgentSession(
        claims=claims,
        project=project,
        instance=instance,
        deployed_by=deployed_by,
    )


def deploy_agent(
    db: Session,
    *,
    project_id: uuid.UUID,
    actor: User,
    payload: ExternalAgentDeployRequest,
    settings: Settings,
) -> ExternalAgentDeployResponse:
    project = lock_project_root(db, project_id)
    if project is None:
        raise NotFoundError("Project", project_id)
    role = permissions.project_role_for(db, actor, project.id)
    if not permissions.can_write_project(role):
        raise AuthenticationError("Project write access is required to deploy an agent.")
    if not project.accepts_work:
        raise ConflictError("This project is paused or no longer accepts agent work.")

    provider = ProviderKind(payload.provider)
    suffix = uuid.uuid4().hex[:12]
    kind = f"external_{provider.value}_{suffix}"
    definition = AgentDefinition(
        kind=kind,
        display_name=payload.specialty,
        description=(
            f"External {provider.value} SDK worker. "
            "Its submissions pass through ESCO evidence, action and approval gates."
        ),
        building="External Agent Gateway",
        handles_task_kinds=[],
        is_plugin=True,
        is_available=True,
    )
    db.add(definition)
    db.flush()
    instance = AgentInstance(
        project_id=project.id,
        definition_id=definition.id,
        kind=kind,
        display_name=f"{payload.display_name} · {provider.value.title()} SDK"[:120],
        status=AgentStatus.WALKING,
        location=payload.initial_location.value,
        current_activity=f"Connecting: {payload.mission}"[:255],
    )
    db.add(instance)
    db.flush()
    announce_agent_presence(db, instance)

    token, claims = create_external_agent_token(
        instance_id=instance.id,
        project_id=project.id,
        deployed_by_id=actor.id,
        provider=provider,
        model=payload.model,
        specialty=payload.specialty,
        mission=payload.mission,
        ttl_seconds=payload.ttl_seconds,
        settings=settings,
    )
    audit.record(
        db,
        AuditAction.AGENT_ACTION,
        actor_user_id=actor.id,
        project_id=project.id,
        target_type="external_agent_instance",
        target_id=instance.id,
        summary=f"Deployed external {provider.value} SDK agent {payload.display_name}",
        details={
            "provider": provider.value,
            "model": payload.model,
            "specialty": payload.specialty,
            "expires_at": claims.expires_at.isoformat(),
        },
    )
    return ExternalAgentDeployResponse(
        instance=AgentInstanceRead.model_validate(instance),
        agent_token=token,
        token_expires_at=claims.expires_at,
        provider=provider,
        model=payload.model,
        mission=payload.mission,
        allowed_actions=sorted(action.value for action in EXTERNAL_ALLOWED_ACTIONS),
        endpoints=ExternalAgentEndpoints(
            context="/api/external-agents/session/context",
            progress="/api/external-agents/session/progress",
            submit="/api/external-agents/session/submit",
        ),
    )


def build_context(
    db: Session,
    session: ExternalAgentSession,
    *,
    evidence_offset: int,
    evidence_limit: int,
) -> ExternalAgentContextRead:
    project = session.project
    files = list(
        db.execute(
            select(UploadedFile)
            .where(UploadedFile.project_id == project.id, UploadedFile.deleted_at.is_(None))
            .order_by(UploadedFile.created_at, UploadedFile.id)
            .limit(500)
        ).scalars()
    )
    requirements = list(
        db.execute(
            select(Requirement)
            .where(Requirement.project_id == project.id)
            .order_by(Requirement.ordinal, Requirement.reference)
            .limit(1000)
        ).scalars()
    )
    evidence_total = int(
        db.execute(
            select(func.count(EvidenceAnchor.id)).where(EvidenceAnchor.project_id == project.id)
        ).scalar_one()
    )
    anchors = list(
        db.execute(
            select(EvidenceAnchor)
            .where(EvidenceAnchor.project_id == project.id)
            .order_by(EvidenceAnchor.created_at, EvidenceAnchor.id)
            .offset(evidence_offset)
            .limit(evidence_limit)
        ).scalars()
    )
    return ExternalAgentContextRead(
        instance=AgentInstanceRead.model_validate(session.instance),
        provider=session.claims.provider,
        model=session.claims.model,
        mission=session.claims.mission,
        project={
            "id": str(project.id),
            "name": project.name,
            "description": project.description,
            "client_reference": project.client_reference,
            "status": project.status.value,
            "current_revision_number": project.current_revision_number,
            "attributes": project.attributes,
        },
        files=[
            {
                "id": str(item.id),
                "filename": item.original_filename,
                "file_kind": item.file_kind.value,
                "document_role": item.document_role.value,
                "status": item.status.value,
                "sha256": item.sha256,
                "size_bytes": item.size_bytes,
                "status_detail": item.status_detail,
            }
            for item in files
        ],
        requirements=[
            {
                "id": str(item.id),
                "reference": item.reference,
                "title": item.title,
                "statement": item.statement[:8000],
                "source_text": item.source_text[:8000],
                "kind": item.kind.value,
                "priority": item.priority.value,
                "status": item.status.value,
                "is_inferred": item.is_inferred,
                "confidence": item.confidence,
                "verification_method": item.verification_method,
            }
            for item in requirements
        ],
        evidence_anchors=[
            {
                "id": str(item.id),
                "file_id": str(item.file_id),
                "anchor_key": item.anchor_key,
                "anchor_type": item.anchor_type.value,
                "evidence_kind": item.evidence_kind.value,
                "source_label": item.source_label,
                "display_location": item.display_location,
                "structured_location": item.structured_location,
                "excerpt": item.excerpt,
                "content_hash": item.content_hash,
                "expectation": item.expectation,
            }
            for item in anchors
        ],
        evidence_total=evidence_total,
        evidence_offset=evidence_offset,
        evidence_limit=evidence_limit,
        allowed_actions=sorted(action.value for action in EXTERNAL_ALLOWED_ACTIONS),
        response_schema=RESPONSE_SCHEMA,
        rules=SHARED_RULES,
    )


def _lock_active_instance(db: Session, session: ExternalAgentSession) -> AgentInstance:
    project = lock_project_root(db, session.project.id)
    if project is None:
        raise AuthenticationError("External agent project is no longer available.")
    if not project.accepts_work:
        raise ConflictError("This project is paused or no longer accepts agent work.")
    _require_current_write_authority(db, session.claims)
    instance = db.execute(
        select(AgentInstance)
        .where(
            AgentInstance.id == session.instance.id,
            AgentInstance.project_id == project.id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if instance is None or instance.status is AgentStatus.OFFLINE:
        raise AuthenticationError("External agent mission was revoked.")
    return instance


def record_progress(
    db: Session,
    *,
    session: ExternalAgentSession,
    payload: ExternalAgentProgressRequest,
) -> AgentInstance:
    instance = _lock_active_instance(db, session)
    submitted_run_id = db.execute(
        select(AgentRun.id)
        .where(AgentRun.agent_instance_id == instance.id)
        .limit(1)
    ).scalar_one_or_none()
    if submitted_run_id is not None:
        raise ConflictError(
            "This one-mission agent has already submitted its result.",
            details={"agent_run_id": str(submitted_run_id)},
        )
    set_agent_status(db, instance, payload.status, activity=payload.activity)
    move_agent(db, instance, payload.location)
    if payload.message:
        message = AgentMessage(
            project_id=instance.project_id,
            agent_instance_id=instance.id,
            role=MessageRole.AGENT,
            agent_kind=instance.kind,
            body=payload.message,
            context={"provider": session.claims.provider.value, "external_sdk": True},
        )
        db.add(message)
        db.flush()
        record_event(
            db,
            project_id=instance.project_id,
            event_type=EventType.AGENT_MESSAGE_CREATED,
            agent_id=instance.id,
            payload={
                "message_id": str(message.id),
                "agent_id": str(instance.id),
                "agent_kind": instance.kind,
                "role": MessageRole.AGENT.value,
                "body": message.body,
                "task_id": None,
            },
        )
    return instance


def _submission_digest(payload: ExternalAgentSubmissionRequest) -> str:
    encoded = json.dumps(payload.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _submission_metadata(run: AgentRun) -> dict[str, Any] | None:
    for item in run.tool_results:
        if isinstance(item, dict) and item.get("kind") == "external_sdk_submission":
            return item
    return None


def _render_submission(
    run: AgentRun,
    *,
    execution: dict[str, Any],
    replayed: bool,
) -> ExternalAgentSubmissionResponse:
    detail = AgentRunDetail.model_validate(run)
    detail.actions = [AgentActionRead.model_validate(item) for item in run.actions]
    detail.rejected_actions = [
        AgentActionRead.model_validate(item) for item in run.rejected_actions
    ]
    return ExternalAgentSubmissionResponse(
        run=detail,
        accepted_actions=len(run.actions),
        rejected_actions=len(run.rejected_actions),
        execution=execution,
        replayed=replayed,
    )


def submit_result(
    db: Session,
    *,
    session: ExternalAgentSession,
    payload: ExternalAgentSubmissionRequest,
    settings: Settings,
) -> ExternalAgentSubmissionResponse:
    instance = _lock_active_instance(db, session)
    digest = _submission_digest(payload)
    existing = db.execute(
        select(AgentRun)
        .where(AgentRun.agent_instance_id == instance.id)
        .order_by(AgentRun.created_at.desc())
        .limit(1)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if existing is not None:
        metadata = _submission_metadata(existing) or {}
        if metadata.get("submission_id") != str(payload.submission_id) or metadata.get(
            "sha256"
        ) != digest:
            raise ConflictError(
                "This one-mission agent already submitted a different result.",
                details={"agent_run_id": str(existing.id)},
            )
        return _render_submission(
            existing,
            execution=dict(metadata.get("execution") or {}),
            replayed=True,
        )

    reported_tokens = payload.prompt_tokens + payload.completion_tokens
    if reported_tokens > settings.agent_max_tokens_per_run:
        raise ValidationError(
            "Reported token usage exceeds the per-run limit.",
            details={
                "reported": reported_tokens,
                "limit": settings.agent_max_tokens_per_run,
            },
        )
    provider = SubmittedSDKProvider(
        kind=session.claims.provider,
        model=session.claims.model,
        data={
            "rationale": payload.rationale,
            "actions": payload.actions,
            "warnings": payload.warnings,
        },
        prompt_tokens=payload.prompt_tokens,
        completion_tokens=payload.completion_tokens,
        latency_ms=payload.latency_ms,
    )
    anchor_ids = set(
        db.execute(
            select(EvidenceAnchor.id).where(EvidenceAnchor.project_id == session.project.id)
        ).scalars()
    )
    runner = AgentRunner(
        db,
        settings,
        project=session.project,
        agent_kind=instance.kind,
        provider=provider,
        agent_instance_id=instance.id,
    )
    outcome: RunOutcome = runner.run(
        system_prompt=SHARED_RULES,
        context={"mission": session.claims.mission},
        allowed_actions=set(EXTERNAL_ALLOWED_ACTIONS),
        anchor_ids=anchor_ids,
    )
    outcome.run.provider = f"{session.claims.provider.value}_sdk"
    executor = ActionExecutor(
        db,
        session.project,
        agent_kind=instance.kind,
        run_id=outcome.run.id,
        agent_instance_id=instance.id,
    )
    execution: ExecutionResult = executor.execute(outcome)
    execution_summary = execution.summary()
    outcome.run.tool_results = [
        *outcome.run.tool_results,
        {
            "kind": "external_sdk_submission",
            "submission_id": str(payload.submission_id),
            "sha256": digest,
            "provider": session.claims.provider.value,
            "execution": execution_summary,
        },
    ]
    final_status = (
        AgentStatus.IDLE
        if outcome.run.status is AgentRunStatus.COMPLETED
        else AgentStatus.BLOCKED
    )
    final_activity = "" if final_status is AgentStatus.IDLE else outcome.run.error_message
    set_agent_status(db, instance, final_status, activity=final_activity)
    audit.record(
        db,
        AuditAction.AGENT_ACTION,
        actor_agent_kind=instance.kind,
        project_id=session.project.id,
        target_type="agent_run",
        target_id=outcome.run.id,
        summary=f"External {session.claims.provider.value} SDK agent submitted its mission",
        details={
            "submission_id": str(payload.submission_id),
            "accepted_actions": len(outcome.accepted),
            "rejected_actions": len(outcome.rejected),
        },
    )
    db.flush()
    return _render_submission(
        outcome.run,
        execution=execution_summary,
        replayed=False,
    )


def revoke_agent(
    db: Session,
    *,
    project_id: uuid.UUID,
    instance_id: uuid.UUID,
    actor: User,
) -> AgentInstance:
    project = lock_project_root(db, project_id)
    if project is None:
        raise NotFoundError("Project", project_id)
    role = permissions.project_role_for(db, actor, project.id)
    if not permissions.can_write_project(role):
        raise AuthenticationError("Project write access is required to revoke an agent.")
    instance = db.execute(
        select(AgentInstance)
        .where(
            AgentInstance.id == instance_id,
            AgentInstance.project_id == project.id,
            AgentInstance.kind.like("external_%"),
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if instance is None:
        raise NotFoundError("External agent", instance_id)
    set_agent_status(db, instance, AgentStatus.OFFLINE, activity="Mission revoked")
    audit.record(
        db,
        AuditAction.AGENT_ACTION,
        actor_user_id=actor.id,
        project_id=project.id,
        target_type="external_agent_instance",
        target_id=instance.id,
        summary=f"Revoked external SDK agent {instance.display_name}",
    )
    return instance
