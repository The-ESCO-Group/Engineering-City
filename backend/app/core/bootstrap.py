"""Startup sequence.

Runs once per process, before the API serves traffic:

1. create and probe the data directory;
2. bring the database schema to head;
3. seed roles, the administrator account and (in demo mode) one user per role;
4. register agent definitions, including any contributed by plugins.

Every step is idempotent, because it runs on every boot and because the worker
process runs it too.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import inspect, select
from sqlalchemy.orm import Session

from app.agents.base.plugins import PluginRegistry, get_plugin_registry
from app.agents.base.registry import CORE_AGENTS
from app.core.config import Settings
from app.core.logging import get_logger
from app.core.services import users as user_service
from app.core.storage import ensure_data_root
from app.database.session import get_engine, session_scope
from app.models import Base
from app.models.agent import AgentDefinition

logger = get_logger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[3]


def upgrade_database(settings: Settings) -> None:
    """Bring the schema to head.

    Uses Alembic so a real deployment gets versioned, reviewable migrations. If
    the database predates Alembic (created by an earlier ``create_all``) it is
    stamped rather than re-created, so an existing local demo is not destroyed.
    """
    from alembic import command
    from alembic.config import Config

    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "backend/app/database/migrations"))
    config.set_main_option("sqlalchemy.url", settings.database_url)

    engine = get_engine()
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())

    if tables and "alembic_version" not in tables:
        logger.warning(
            "database has tables but no Alembic version; stamping to head instead of migrating",
            extra={"table_count": len(tables)},
        )
        command.stamp(config, "head")
        return

    command.upgrade(config, "head")
    logger.info("database schema is at head")


def create_schema_directly() -> None:
    """Create tables from the ORM metadata, bypassing Alembic.

    For tests and throwaway in-memory databases only. Production and demo paths
    go through :func:`upgrade_database` so the migration history is exercised.
    """
    Base.metadata.create_all(get_engine())


def sync_agent_definitions(
    db: Session, registry: PluginRegistry | None = None
) -> list[AgentDefinition]:
    """Reconcile the ``agent_definitions`` table with the code-side catalogue.

    A definition whose plugin has been uninstalled is marked unavailable with a
    reason rather than deleted, so tasks and runs that reference it still make
    sense in the history.
    """
    registry = registry or get_plugin_registry()
    existing = {row.kind: row for row in db.execute(select(AgentDefinition)).scalars()}
    seen: set[str] = set()
    results: list[AgentDefinition] = []

    for spec in CORE_AGENTS:
        row = existing.get(spec.kind.value)
        if row is None:
            row = AgentDefinition(kind=spec.kind.value)
            db.add(row)
        row.display_name = spec.display_name
        row.description = spec.description
        row.building = spec.building
        row.handles_task_kinds = [kind.value for kind in spec.handles_task_kinds]
        row.is_plugin = False
        row.is_available = True
        row.unavailable_reason = ""
        seen.add(spec.kind.value)
        results.append(row)

    for kind, plugin in sorted(registry.delivery_agents.items()):
        row = existing.get(kind)
        if row is None:
            row = AgentDefinition(kind=kind)
            db.add(row)
        row.display_name = getattr(plugin, "display_name", kind)
        row.description = getattr(plugin, "__doc__", "") or ""
        row.building = "Delivery Yard"
        row.handles_task_kinds = list(getattr(plugin, "handles_task_kinds", []))
        row.is_plugin = True
        row.is_available = True
        row.unavailable_reason = ""
        seen.add(kind)
        results.append(row)

    for kind, row in existing.items():
        if kind in seen:
            continue
        # Was registered by a plugin on an earlier boot; the plugin is gone now.
        row.is_available = False
        row.unavailable_reason = (
            "The plugin that provided this agent is no longer installed. "
            "Existing tasks and runs that reference it are retained."
        )
        results.append(row)

    db.flush()
    return results


def bootstrap(settings: Settings, *, run_migrations: bool = True) -> None:
    """Run the whole startup sequence."""
    ensure_data_root(settings)

    if run_migrations:
        upgrade_database(settings)

    registry = get_plugin_registry()
    with session_scope() as db:
        user_service.bootstrap(db, settings)
        definitions = sync_agent_definitions(db, registry)

    logger.info(
        "bootstrap complete",
        extra={
            "data_dir": str(settings.data_dir),
            "agent_definitions": len(definitions),
            "delivery_plugins": sorted(registry.delivery_agents),
            "report_providers": sorted(registry.report_providers),
        },
    )
