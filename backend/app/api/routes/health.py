"""Liveness and capability reporting."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from app import CONTRACT_VERSION, __version__
from app.agents.base.plugins import get_plugin_registry
from app.api.schemas.common import CapabilityStatus, HealthResponse
from app.core.config import Settings, get_settings
from app.database.session import get_db

router = APIRouter(tags=["system"])


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Liveness, contract version and installed capabilities",
)
def health(
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> HealthResponse:
    """Report whether the backend is usable, and precisely what it can do.

    Returns ``degraded`` rather than failing when the database is unreachable,
    so an operator can still read the diagnosis from a running process.
    """
    try:
        db.execute(text("SELECT 1"))
        database = "ok"
    except Exception as exc:
        database = f"{type(exc).__name__}: {exc}"

    registry = get_plugin_registry()
    capabilities = [CapabilityStatus(**row) for row in registry.capability_rows()]

    return HealthResponse(
        status="ok" if database == "ok" else "degraded",
        app_name=settings.app_name,
        version=__version__,
        contract_version=CONTRACT_VERSION,
        environment=settings.environment,
        database=database,
        enabled_providers=[provider.value for provider in settings.enabled_providers],
        capabilities=capabilities,
    )


@router.get(
    "/system/capabilities",
    response_model=list[CapabilityStatus],
    summary="Optional plugin capabilities and why any are unavailable",
)
def capabilities() -> list[CapabilityStatus]:
    return [CapabilityStatus(**row) for row in get_plugin_registry().capability_rows()]
