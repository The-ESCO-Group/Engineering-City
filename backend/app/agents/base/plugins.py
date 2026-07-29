"""Plugin interfaces and discovery.

Delivery agents, report providers and export formats are contributed by modules
this backend does **not** own (``app.agents.delivery`` and ``app.reporting``).
Discovery is therefore defensive by design: a missing, broken or partially
implemented plugin package must degrade the product to a clearly-reported
missing capability, never prevent the application from starting.

A plugin package opts in by exposing a module-level ``register(registry)``
function, or by declaring ``DELIVERY_AGENTS`` / ``REPORT_PROVIDERS`` /
``EXPORT_FORMATS`` sequences.
"""

from __future__ import annotations

import importlib
import traceback
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from app.core.logging import get_logger

logger = get_logger(__name__)

#: Packages searched for plugins. Reserved for GPT; absent by default.
PLUGIN_MODULES: tuple[str, ...] = ("app.agents.delivery", "app.reporting")


# ---------------------------------------------------------------------------
# Interfaces
# ---------------------------------------------------------------------------
@runtime_checkable
class DeliveryAgentPlugin(Protocol):
    """An agent that turns approved findings into deliverables.

    Delivery agents run *after* human approval. They receive a read-only
    context object and return :class:`~app.agents.base.actions.AgentAction`
    instances, exactly like core agents — they get no direct database or
    filesystem access.
    """

    #: Stable identifier, unique across plugins (e.g. ``test_plan_writer``).
    kind: str
    display_name: str
    version: str
    #: ``TaskKind`` values this agent can execute.
    handles_task_kinds: Sequence[str]

    def run(self, context: Any) -> Any:
        """Execute one bounded turn and return an ``AgentRunResult``."""
        ...


@runtime_checkable
class ReportProvider(Protocol):
    """Renders a report body from an approved, read-only project snapshot."""

    #: e.g. ``discrepancy_matrix``.
    report_type: str
    display_name: str
    version: str
    #: Export format keys this provider can emit.
    supported_formats: Sequence[str]

    def render(self, snapshot: Any, export_format: str) -> Any:
        """Return a ``RenderedReport`` (bytes plus media type and title)."""
        ...


@runtime_checkable
class ExportFormatProvider(Protocol):
    """Serialises a rendered document into a concrete file format."""

    #: e.g. ``markdown``, ``html``, ``xlsx``.
    format_key: str
    media_type: str
    file_extension: str
    version: str

    def serialize(self, document: Any) -> bytes: ...


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class PluginLoadError:
    """A plugin module that exists but could not be loaded."""

    module: str
    message: str
    traceback_text: str = ""


@dataclass(slots=True)
class PluginRegistry:
    """Everything discovered from the reserved plugin packages."""

    delivery_agents: dict[str, DeliveryAgentPlugin] = field(default_factory=dict)
    report_providers: dict[str, ReportProvider] = field(default_factory=dict)
    export_formats: dict[str, ExportFormatProvider] = field(default_factory=dict)
    #: Modules that were simply not present. Expected, not an error.
    missing_modules: list[str] = field(default_factory=list)
    #: Modules present but unusable. Surfaced to operators.
    errors: list[PluginLoadError] = field(default_factory=list)

    # -- registration API used by ``register(registry)`` -------------------
    def add_delivery_agent(self, plugin: DeliveryAgentPlugin) -> None:
        kind = getattr(plugin, "kind", None)
        if not kind:
            raise ValueError("delivery agent plugin must define a non-empty 'kind'")
        if kind in self.delivery_agents:
            raise ValueError(f"duplicate delivery agent kind: {kind!r}")
        self.delivery_agents[kind] = plugin

    def add_report_provider(self, provider: ReportProvider) -> None:
        key = getattr(provider, "report_type", None)
        if not key:
            raise ValueError("report provider must define a non-empty 'report_type'")
        if key in self.report_providers:
            raise ValueError(f"duplicate report_type: {key!r}")
        self.report_providers[key] = provider

    def add_export_format(self, provider: ExportFormatProvider) -> None:
        key = getattr(provider, "format_key", None)
        if not key:
            raise ValueError("export format provider must define a non-empty 'format_key'")
        if key in self.export_formats:
            raise ValueError(f"duplicate format_key: {key!r}")
        self.export_formats[key] = provider

    # -- queries ----------------------------------------------------------
    @property
    def has_delivery(self) -> bool:
        return bool(self.delivery_agents)

    @property
    def has_reporting(self) -> bool:
        return bool(self.report_providers)

    def capability_rows(self) -> list[dict[str, Any]]:
        """Flat capability list for ``GET /api/health`` and ``/api/system/capabilities``."""
        rows: list[dict[str, Any]] = [
            {
                "name": "delivery_agents",
                "available": self.has_delivery,
                "provider": ", ".join(sorted(self.delivery_agents)),
                "version": "",
                "detail": ""
                if self.has_delivery
                else "No delivery agent plugin installed under app.agents.delivery.",
            },
            {
                "name": "report_providers",
                "available": self.has_reporting,
                "provider": ", ".join(sorted(self.report_providers)),
                "version": "",
                "detail": ""
                if self.has_reporting
                else "No report provider installed under app.reporting.",
            },
            {
                "name": "export_formats",
                "available": bool(self.export_formats),
                "provider": ", ".join(sorted(self.export_formats)),
                "version": "",
                "detail": ""
                if self.export_formats
                else "No export format provider installed under app.reporting.",
            },
        ]
        for err in self.errors:
            rows.append(
                {
                    "name": f"plugin:{err.module}",
                    "available": False,
                    "provider": err.module,
                    "version": "",
                    "detail": err.message,
                }
            )
        return rows


def _collect_declared(registry: PluginRegistry, module: Any) -> None:
    """Pick up the declarative ``*_AGENTS`` / ``*_PROVIDERS`` sequences."""
    pairs: tuple[tuple[str, Any], ...] = (
        ("DELIVERY_AGENTS", registry.add_delivery_agent),
        ("REPORT_PROVIDERS", registry.add_report_provider),
        ("EXPORT_FORMATS", registry.add_export_format),
    )
    for attribute, adder in pairs:
        declared = getattr(module, attribute, None)
        if declared is None:
            continue
        if not isinstance(declared, Iterable):
            raise TypeError(f"{module.__name__}.{attribute} must be iterable")
        for item in declared:
            adder(item)


def discover_plugins(modules: Sequence[str] = PLUGIN_MODULES) -> PluginRegistry:
    """Import each reserved package and collect what it registers.

    Never raises. A module that is absent is recorded in ``missing_modules``; a
    module that raises during import or registration is recorded in ``errors``
    with its traceback, and the rest of the application continues.
    """
    registry = PluginRegistry()

    for module_name in modules:
        try:
            module = importlib.import_module(module_name)
        except ModuleNotFoundError as exc:
            # Distinguish "the plugin package isn't installed" (expected) from
            # "the plugin package is installed but imports something missing".
            if exc.name == module_name or (exc.name or "").startswith(f"{module_name}."):
                registry.missing_modules.append(module_name)
                logger.info("plugin package not installed", extra={"plugin_module": module_name})
            else:
                registry.errors.append(
                    PluginLoadError(
                        module=module_name,
                        message=f"missing dependency {exc.name!r}",
                        traceback_text=traceback.format_exc(),
                    )
                )
                logger.warning(
                    "plugin package failed to import",
                    extra={"plugin_module": module_name, "missing": exc.name},
                )
            continue
        except Exception as exc:
            registry.errors.append(
                PluginLoadError(
                    module=module_name,
                    message=f"{type(exc).__name__}: {exc}",
                    traceback_text=traceback.format_exc(),
                )
            )
            logger.warning(
                "plugin package raised during import",
                extra={"plugin_module": module_name, "error": str(exc)},
            )
            continue

        try:
            register = getattr(module, "register", None)
            if callable(register):
                register(registry)
            _collect_declared(registry, module)
        except Exception as exc:
            registry.errors.append(
                PluginLoadError(
                    module=module_name,
                    message=f"registration failed: {type(exc).__name__}: {exc}",
                    traceback_text=traceback.format_exc(),
                )
            )
            logger.warning(
                "plugin registration failed",
                extra={"plugin_module": module_name, "error": str(exc)},
            )

    logger.info(
        "plugin discovery complete",
        extra={
            "delivery_agents": sorted(registry.delivery_agents),
            "report_providers": sorted(registry.report_providers),
            "export_formats": sorted(registry.export_formats),
            "missing_modules": registry.missing_modules,
            "errors": [e.module for e in registry.errors],
        },
    )
    return registry


_registry: PluginRegistry | None = None


def get_plugin_registry() -> PluginRegistry:
    """Cached registry. Call :func:`reload_plugins` after installing a plugin."""
    global _registry
    if _registry is None:
        _registry = discover_plugins()
    return _registry


def reload_plugins() -> PluginRegistry:
    global _registry
    _registry = discover_plugins()
    return _registry
