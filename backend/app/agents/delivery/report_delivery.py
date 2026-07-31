"""Checkpointed documentation and report delivery from canonical project state."""

from __future__ import annotations

from sqlalchemy import select

from app.agents.base.plugins import PluginRegistry, get_plugin_registry
from app.core.clock import ensure_utc
from app.core.enums import TaskKind
from app.models.delivery import ReportDefinition
from app.orchestration.handlers import TaskContext
from app.reporting.service import generate_report
from app.reporting.snapshot import build_report_snapshot
from app.reporting.types import DeliveryRunResult


class EngineeringReportDeliveryAgent:
    """Generate configured report artifacts after the workflow approval gate."""

    kind = "engineering_report_delivery"
    display_name = "Documentation and Release Agent"
    version = "2.0.0"
    handles_task_kinds = (TaskKind.DELIVERY.value,)

    def run(self, context: TaskContext) -> DeliveryRunResult:
        snapshot = build_report_snapshot(
            context.db,
            context.project,
            generated_at=ensure_utc(context.task.created_at),
        )
        registry = get_plugin_registry()
        completed = context.done()
        report_ids = []
        generated: list[str] = []
        skipped: list[str] = []
        warnings: list[str] = []

        outputs = self._outputs(context, registry, warnings)
        for provider_key, export_format in outputs:
            provider = registry.report_providers[provider_key]
            item_key = f"report:{provider_key}:{export_format}"
            if item_key in completed:
                skipped.append(item_key)
                continue
            report, created = generate_report(context, provider, snapshot, export_format)
            report_ids.append(report.id)
            (generated if created else skipped).append(item_key)
            context.mark_done(item_key)
            completed.add(item_key)

        if not snapshot.findings:
            warnings.append(
                "No accepted findings were available; generated reports state that limitation "
                "and do not claim the project is free of discrepancies."
            )
        return DeliveryRunResult(
            report_ids=tuple(report_ids),
            generated=tuple(generated),
            skipped=tuple(skipped),
            warnings=tuple(warnings),
        )

    @staticmethod
    def _outputs(
        context: TaskContext, registry: PluginRegistry, warnings: list[str]
    ) -> list[tuple[str, str]]:
        """Resolve requested outputs without pretending missing capabilities exist."""

        payload_types = context.payload.get("report_types")
        payload_formats = context.payload.get("report_formats")

        def string_list(value: object, label: str) -> list[str] | None:
            if value is None:
                return None
            if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                warnings.append(f"Ignored invalid {label}; expected a list of string keys.")
                return []
            return sorted(set(value))

        selected_types = string_list(payload_types, "report_types")
        selected_formats = string_list(payload_formats, "report_formats")
        requests: list[tuple[str, str]] = []

        if selected_types is not None or selected_formats is not None:
            provider_keys = selected_types or [
                key
                for key, provider in registry.report_providers.items()
                if getattr(provider, "include_in_default_delivery", False)
            ]
            for provider_key in provider_keys:
                provider = registry.report_providers.get(provider_key)
                if provider is None:
                    warnings.append(
                        f"Requested report {provider_key!r} is unavailable; "
                        "no artifact was generated."
                    )
                    continue
                formats = selected_formats or list(
                    getattr(provider, "default_formats", provider.supported_formats)
                )
                requests.extend((provider_key, value) for value in formats)
        else:
            definitions = list(
                context.db.execute(
                    select(ReportDefinition).where(
                        ReportDefinition.project_id == context.project.id
                    )
                ).scalars()
            )
            if definitions:
                for definition in sorted(
                    definitions, key=lambda item: (item.ordinal, item.key, str(item.id))
                ):
                    provider = registry.report_providers.get(definition.report_type)
                    if provider is None:
                        warnings.append(
                            f"Required report {definition.display_name!r} is unavailable: "
                            f"no provider is installed for {definition.report_type!r}."
                        )
                        continue
                    formats = [str(value) for value in definition.required_formats] or list(
                        getattr(provider, "default_formats", provider.supported_formats)
                    )
                    requests.extend((definition.report_type, value) for value in formats)
            else:
                for provider_key, provider in sorted(registry.report_providers.items()):
                    if not getattr(provider, "include_in_default_delivery", False):
                        continue
                    requests.extend(
                        (provider_key, value)
                        for value in getattr(
                            provider, "default_formats", provider.supported_formats
                        )
                    )

        outputs: list[tuple[str, str]] = []
        for provider_key, export_format in sorted(set(requests)):
            provider = registry.report_providers.get(provider_key)
            if provider is None:
                continue
            if export_format not in provider.supported_formats:
                warnings.append(
                    f"Report {provider_key!r} does not support {export_format!r}; no artifact "
                    "was generated for that requested format."
                )
                continue
            if export_format not in registry.export_formats:
                warnings.append(
                    f"Export format {export_format!r} is not installed; no artifact was generated."
                )
                continue
            outputs.append((provider_key, export_format))
        if not outputs:
            warnings.append("No supported report output was requested or configured.")
        return outputs
