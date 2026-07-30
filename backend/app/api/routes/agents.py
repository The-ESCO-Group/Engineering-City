"""Agents, their runs and the activity feed.

Everything here is read-only. There is no endpoint that runs an agent directly:
agents run as workflow tasks, so a run is always attached to a project, a task
and an audit trail rather than being invokable on its own.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Path, Query
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession, ProjectRead, authorise_project
from app.api.schemas.agent import (
    AgentActionRead,
    AgentDefinitionRead,
    AgentInstanceRead,
    AgentMessageRead,
    AgentRunDetail,
    AgentRunRead,
)
from app.core.errors import NotFoundError
from app.models.agent import AgentDefinition, AgentInstance, AgentMessage, AgentRun
from app.security import permissions

router = APIRouter(tags=["agents"])


@router.get(
    "/agents",
    response_model=list[AgentDefinitionRead],
    summary="Every registered agent, and why any is unavailable",
)
def list_agents(db: DbSession, user: CurrentUser) -> list[AgentDefinitionRead]:
    """The agent catalogue.

    Includes agents contributed by plugins, and reports one as unavailable with a
    reason rather than omitting it — a missing agent that simply is not listed
    looks identical to one that never existed.
    """
    rows = db.execute(select(AgentDefinition).order_by(AgentDefinition.kind)).scalars()
    return [AgentDefinitionRead.model_validate(row) for row in rows]


@router.get(
    "/projects/{project_id}/agents",
    response_model=list[AgentInstanceRead],
    summary="Agent presence for the city view",
)
def list_project_agents(access: ProjectRead, db: DbSession) -> list[AgentInstanceRead]:
    rows = db.execute(
        select(AgentInstance)
        .where(AgentInstance.project_id == access.project.id)
        .order_by(AgentInstance.kind)
    ).scalars()
    return [AgentInstanceRead.model_validate(row) for row in rows]


@router.get(
    "/projects/{project_id}/agent-runs",
    response_model=list[AgentRunRead],
    summary="Agent runs for a project",
)
def list_runs(
    access: ProjectRead,
    db: DbSession,
    agent_kind: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[AgentRunRead]:
    statement = select(AgentRun).where(AgentRun.project_id == access.project.id)
    if agent_kind:
        statement = statement.where(AgentRun.agent_kind == agent_kind)
    rows = db.execute(statement.order_by(AgentRun.created_at.desc()).limit(limit)).scalars()
    return [AgentRunRead.model_validate(row) for row in rows]


@router.get(
    "/agent-runs/{run_id}",
    response_model=AgentRunDetail,
    summary="One agent run, including the actions the backend refused",
)
def get_run(
    run_id: Annotated[uuid.UUID, Path()], db: DbSession, user: CurrentUser
) -> AgentRunDetail:
    """Everything persisted about one run.

    Contains no chain-of-thought by construction: ``rationale`` is a concise,
    human-facing justification, and there is no field for a reasoning trace.

    ``rejected_actions`` lists what the backend refused and why. Those are kept
    deliberately — what a model *tried* to do is part of the record an engineer
    may need to see.
    """
    run = db.get(AgentRun, run_id)
    if run is None:
        raise NotFoundError("Agent run", run_id)
    authorise_project(db, user, run.project_id, permissions.READ_ROLES, "view")

    detail = AgentRunDetail.model_validate(run)
    detail.actions = [AgentActionRead.model_validate(item) for item in run.actions]
    detail.rejected_actions = [
        AgentActionRead.model_validate(item) for item in run.rejected_actions
    ]
    return detail


@router.get(
    "/projects/{project_id}/messages",
    response_model=list[AgentMessageRead],
    summary="The project activity feed",
)
def list_messages(
    access: ProjectRead,
    db: DbSession,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
    since: Annotated[uuid.UUID | None, Query()] = None,
) -> list[AgentMessageRead]:
    """Short, human-readable statements from the agents.

    A communication surface, not a log of model internals.
    """
    statement = select(AgentMessage).where(AgentMessage.project_id == access.project.id)
    if since is not None:
        marker = db.get(AgentMessage, since)
        if marker is not None:
            statement = statement.where(AgentMessage.created_at > marker.created_at)

    rows = db.execute(statement.order_by(AgentMessage.created_at).limit(limit)).scalars()
    return [AgentMessageRead.model_validate(row) for row in rows]
