"""Which provider and model each agent uses.

Administrator-only, because changing it is a data-residency decision as much as a
quality one: pointing an agent at OpenAI or Anthropic sends project content to a
third party.

Configuration is resolved most-specific-first — per-project-per-agent, then
per-project, then global, then the configured default — and a row naming a
provider the administrator has since disabled is ignored rather than honoured, so
a stale row can never re-enable a provider that was turned off.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Path, Query
from sqlalchemy import select

from app.agents.base.providers import provider_status
from app.api.deps import Administrator, AppSettings, DbSession, Provenance
from app.api.schemas.agent import ModelConfigurationRead, UpdateModelConfigurationRequest
from app.api.schemas.common import CapabilityStatus, OkResponse
from app.core.enums import AuditAction, ProviderKind
from app.core.errors import NotFoundError, ValidationError
from app.models.agent import ModelConfiguration
from app.security import audit

router = APIRouter(prefix="/model-configurations", tags=["model configuration"])

#: ``agent_kind=""`` means "every agent in this scope".
ANY_AGENT = ""


@router.get(
    "",
    response_model=list[ModelConfigurationRead],
    summary="Model configuration (administrator only)",
)
def list_configurations(
    admin: Administrator,
    db: DbSession,
    project_id: Annotated[uuid.UUID | None, Query()] = None,
) -> list[ModelConfigurationRead]:
    """Configured rows, most specific first.

    Omitting ``project_id`` returns the global rows; supplying one returns that
    project's rows *and* the globals it falls back to, so the effective
    resolution order is visible rather than having to be inferred.
    """
    statement = select(ModelConfiguration)
    if project_id is not None:
        statement = statement.where(
            (ModelConfiguration.project_id == project_id)
            | (ModelConfiguration.project_id.is_(None))
        )
    else:
        statement = statement.where(ModelConfiguration.project_id.is_(None))

    rows = sorted(
        db.execute(statement).scalars(),
        key=lambda row: (row.project_id is None, row.agent_kind == ANY_AGENT, row.agent_kind),
    )
    return [ModelConfigurationRead.model_validate(row) for row in rows]


@router.get(
    "/providers",
    response_model=list[CapabilityStatus],
    summary="Which providers this installation may use",
)
def list_providers(admin: Administrator, settings: AppSettings) -> list[CapabilityStatus]:
    """Enabled providers, and which of them send content off this machine.

    That last part is the fact an administrator actually needs before choosing
    one, so it is stated rather than left to be inferred from the name.
    """
    return [CapabilityStatus(**row) for row in provider_status(settings)]


@router.put(
    "/{agent_kind}",
    response_model=ModelConfigurationRead,
    summary="Set the provider and model for an agent (administrator only)",
)
def upsert_configuration(
    agent_kind: Annotated[str, Path(description="Agent kind, or '_default' for all agents.")],
    payload: UpdateModelConfigurationRequest,
    admin: Administrator,
    db: DbSession,
    settings: AppSettings,
    provenance: Provenance,
    project_id: Annotated[uuid.UUID | None, Query()] = None,
) -> ModelConfigurationRead:
    """Create or replace one configuration row.

    The provider must already be in ``ESCO_ENABLED_PROVIDERS``. Refusing here as
    well as at resolution time means an administrator finds out immediately,
    rather than discovering mid-review that the row they saved is being ignored.
    """
    scope = ANY_AGENT if agent_kind == "_default" else agent_kind

    try:
        provider = ProviderKind(payload.provider)
    except ValueError as exc:
        raise ValidationError(
            f"'{payload.provider}' is not a known provider. Known providers: "
            + ", ".join(item.value for item in ProviderKind),
            details={"provider": payload.provider},
        ) from exc

    if not settings.provider_enabled(provider):
        raise ValidationError(
            f"The '{provider.value}' provider is not enabled on this installation, "
            f"so this configuration would be ignored. An administrator must add it "
            f"to ESCO_ENABLED_PROVIDERS first. Currently enabled: "
            + ", ".join(item.value for item in settings.enabled_providers),
            details={"provider": provider.value},
        )

    row = db.execute(
        select(ModelConfiguration).where(
            ModelConfiguration.project_id.is_(None)
            if project_id is None
            else ModelConfiguration.project_id == project_id,
            ModelConfiguration.agent_kind == scope,
        )
    ).scalar_one_or_none()

    before = {"provider": row.provider, "model": row.model} if row is not None else None
    if row is None:
        row = ModelConfiguration(project_id=project_id, agent_kind=scope)
        db.add(row)

    row.provider = provider.value
    row.model = payload.model
    row.temperature = payload.temperature
    row.max_output_tokens = payload.max_output_tokens
    row.updated_by_id = admin.id
    db.flush()

    audit.record(
        db,
        AuditAction.MODEL_CONFIG_CHANGED,
        actor_user_id=admin.id,
        project_id=project_id,
        target_type="model_configuration",
        target_id=row.id,
        summary=(
            f"Set {scope or 'every agent'} to {provider.value}/{payload.model}"
            + (f" for project {project_id}" if project_id else " globally")
        ),
        details={
            "before": before,
            "after": {"provider": row.provider, "model": row.model},
            "sends_data_off_machine": provider in {ProviderKind.OPENAI, ProviderKind.ANTHROPIC},
        },
        source_ip=provenance.source_ip,
        user_agent=provenance.user_agent,
    )
    db.commit()
    return ModelConfigurationRead.model_validate(row)


@router.delete(
    "/{agent_kind}",
    response_model=OkResponse,
    summary="Remove a configuration row and fall back to the default",
)
def delete_configuration(
    agent_kind: Annotated[str, Path()],
    admin: Administrator,
    db: DbSession,
    provenance: Provenance,
    project_id: Annotated[uuid.UUID | None, Query()] = None,
) -> OkResponse:
    scope = ANY_AGENT if agent_kind == "_default" else agent_kind
    row = db.execute(
        select(ModelConfiguration).where(
            ModelConfiguration.project_id.is_(None)
            if project_id is None
            else ModelConfiguration.project_id == project_id,
            ModelConfiguration.agent_kind == scope,
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFoundError("Model configuration", agent_kind)

    audit.record(
        db,
        AuditAction.MODEL_CONFIG_CHANGED,
        actor_user_id=admin.id,
        project_id=project_id,
        target_type="model_configuration",
        target_id=row.id,
        summary=f"Removed the configuration for {scope or 'every agent'}",
        details={"removed": {"provider": row.provider, "model": row.model}},
        source_ip=provenance.source_ip,
        user_agent=provenance.user_agent,
    )
    db.delete(row)
    db.commit()
    return OkResponse(message="Configuration removed; the next-broadest scope now applies.")
