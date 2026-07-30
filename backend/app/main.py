"""FastAPI application factory.

The app is assembled here and nowhere else, so tests can build an isolated
instance and the entrypoint stays a one-liner.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# Imported for the side effect: this package registers every task handler.
# Bound to an underscore name so the module-level `app` symbol is not shadowed
# by the package of the same name.
import app.orchestration as _orchestration  # noqa: F401
from app import CONTRACT_VERSION, __version__
from app.agents.base.plugins import get_plugin_registry
from app.api.errors import install_exception_handlers
from app.api.middleware import RequestContextMiddleware, SecurityHeadersMiddleware
from app.api.routes import auth as auth_routes
from app.api.routes import events as event_routes
from app.api.routes import files as file_routes
from app.api.routes import health as health_routes
from app.api.routes import projects as project_routes
from app.api.routes import workflow as workflow_routes
from app.core.bootstrap import bootstrap
from app.core.config import Settings, get_settings
from app.core.logging import configure_logging, get_logger
from app.orchestration.broadcast import get_broker
from app.orchestration.worker import get_worker, stop_worker

logger = get_logger(__name__)

DESCRIPTION = """
Local-first, evidence-based engineering review platform for ESCO Group.

The platform analyses uploaded engineering documents (Studio 5000 L5X exports,
interlock schedules, I/O lists, functional descriptions) and produces findings
that always cite exact evidence.

**Boundaries this API enforces by design**

* It never connects to an industrial controller and never uploads, downloads or
  modifies controller programs.
* It does not parse proprietary `.ACD` files and does not claim to.
* It never applies a PLC or electrical design change; recommendations are
  proposals for a human engineer.
* Safety- and equipment-damage-related recommendations cannot be released
  without a recorded human approval.
* Model output is never presented as approved engineering, and raw model
  reasoning is never stored or returned.
"""


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start-up and shut-down hooks."""
    settings: Settings = app.state.settings
    registry = get_plugin_registry()
    logger.info(
        "backend starting",
        extra={
            "version": __version__,
            "contract_version": CONTRACT_VERSION,
            "environment": settings.environment,
            "database": "sqlite" if settings.is_sqlite else "postgresql",
            "enabled_providers": [p.value for p in settings.enabled_providers],
            "delivery_plugins": sorted(registry.delivery_agents),
            "report_providers": sorted(registry.report_providers),
        },
    )

    # Bind the running loop so events recorded on worker threads and in sync
    # request handlers can still reach WebSocket subscribers.
    get_broker().bind_loop(asyncio.get_running_loop())

    if app.state.run_bootstrap:
        bootstrap(settings)

    worker = None
    if settings.worker_enabled and app.state.run_worker:
        worker = get_worker(settings)
        worker.start()

    yield

    if worker is not None:
        stop_worker()
    logger.info("backend stopped")


def create_app(
    settings: Settings | None = None,
    *,
    run_bootstrap: bool = True,
    run_worker: bool = True,
) -> FastAPI:
    """Build the ASGI application.

    ``run_bootstrap=False`` skips migrations and seeding, and ``run_worker=False``
    leaves the background worker stopped, for tests that arrange their own schema
    and drive the queue deterministically.
    """
    settings = settings or get_settings()
    configure_logging(level=settings.log_level, json_output=settings.log_json)

    app = FastAPI(
        title=settings.app_name,
        description=DESCRIPTION,
        version=__version__,
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url=None,
        openapi_url="/openapi.json",
    )
    app.state.settings = settings
    app.state.run_bootstrap = run_bootstrap
    app.state.run_worker = run_worker

    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["*"],
        expose_headers=["X-Request-ID"],
    )

    install_exception_handlers(app)

    prefix = settings.api_prefix
    app.include_router(health_routes.router, prefix=prefix)
    app.include_router(auth_routes.router, prefix=prefix)
    app.include_router(project_routes.router, prefix=prefix)
    app.include_router(file_routes.router, prefix=prefix)
    app.include_router(workflow_routes.router, prefix=prefix)
    app.include_router(event_routes.router, prefix=prefix)
    # The WebSocket lives at /ws/projects/{id}, outside the /api prefix.
    app.include_router(event_routes.ws_router)

    return app


app = create_app()
