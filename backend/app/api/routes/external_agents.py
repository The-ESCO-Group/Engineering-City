"""Deploy and operate capability-scoped OpenAI/Claude SDK workers."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, Request, status

from app.agents.external import gateway
from app.api.deps import AppSettings, DbSession, ProjectWrite
from app.api.schemas.agent import AgentInstanceRead
from app.api.schemas.external_agent import (
    ExternalAgentContextRead,
    ExternalAgentDeployRequest,
    ExternalAgentDeployResponse,
    ExternalAgentProgressRequest,
    ExternalAgentRevokeResponse,
    ExternalAgentSubmissionRequest,
    ExternalAgentSubmissionResponse,
)
from app.core.errors import AuthenticationError
from app.security.external_agent_tokens import decode_external_agent_token

router = APIRouter(tags=["external agents"])


def _external_session(
    request: Request,
    db: DbSession,
    settings: AppSettings,
) -> gateway.ExternalAgentSession:
    """Authenticate only an external mission bearer token; cookies are refused."""

    header = request.headers.get("Authorization", "")
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        raise AuthenticationError(
            "External agent endpoints require 'Authorization: Bearer <agent_token>'."
        )
    claims = decode_external_agent_token(value.strip(), settings)
    return gateway.resolve_external_session(db, claims)


ExternalSession = Annotated[gateway.ExternalAgentSession, Depends(_external_session)]


@router.post(
    "/projects/{project_id}/external-agents",
    response_model=ExternalAgentDeployResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Deploy one short-lived OpenAI or Claude SDK agent",
)
def deploy_external_agent(
    payload: ExternalAgentDeployRequest,
    access: ProjectWrite,
    db: DbSession,
    settings: AppSettings,
) -> ExternalAgentDeployResponse:
    response = gateway.deploy_agent(
        db,
        project_id=access.project.id,
        actor=access.user,
        payload=payload,
        settings=settings,
    )
    db.commit()
    return response


@router.get(
    "/external-agents/session/context",
    response_model=ExternalAgentContextRead,
    summary="Read the bounded project context granted to this SDK mission",
)
def external_agent_context(
    session: ExternalSession,
    db: DbSession,
    evidence_offset: Annotated[int, Query(ge=0)] = 0,
    evidence_limit: Annotated[int, Query(ge=1, le=500)] = 200,
) -> ExternalAgentContextRead:
    return gateway.build_context(
        db,
        session,
        evidence_offset=evidence_offset,
        evidence_limit=evidence_limit,
    )


@router.post(
    "/external-agents/session/progress",
    response_model=AgentInstanceRead,
    summary="Publish SDK agent progress and its semantic city location",
)
def external_agent_progress(
    payload: ExternalAgentProgressRequest,
    session: ExternalSession,
    db: DbSession,
) -> AgentInstanceRead:
    instance = gateway.record_progress(db, session=session, payload=payload)
    db.commit()
    return AgentInstanceRead.model_validate(instance)


@router.post(
    "/external-agents/session/submit",
    response_model=ExternalAgentSubmissionResponse,
    summary="Submit one idempotent SDK result through native ESCO action gates",
)
def external_agent_submit(
    payload: ExternalAgentSubmissionRequest,
    session: ExternalSession,
    db: DbSession,
    settings: AppSettings,
) -> ExternalAgentSubmissionResponse:
    response = gateway.submit_result(
        db,
        session=session,
        payload=payload,
        settings=settings,
    )
    db.commit()
    return response


@router.delete(
    "/projects/{project_id}/external-agents/{instance_id}",
    response_model=ExternalAgentRevokeResponse,
    summary="Revoke an external SDK mission immediately",
)
def revoke_external_agent(
    access: ProjectWrite,
    db: DbSession,
    instance_id: Annotated[uuid.UUID, Path()],
) -> ExternalAgentRevokeResponse:
    instance = gateway.revoke_agent(
        db,
        project_id=access.project.id,
        instance_id=instance_id,
        actor=access.user,
    )
    db.commit()
    return ExternalAgentRevokeResponse(
        instance_id=instance.id,
        status=instance.status,
        message="External agent mission revoked.",
    )
