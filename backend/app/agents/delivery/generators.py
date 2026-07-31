"""Target-aware, deterministic HMI and alarm candidate generators.

Candidates stay in memory. Persisting them must go through the canonical
export/release service once that service exposes a delivery-plugin entry point.
No arbitrary FactoryTalk display XML is written: vendor-approved display schema
and template assets are absent, so that capability is reported unavailable.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
from dataclasses import dataclass
from typing import Protocol, TypeVar

from sqlalchemy import select

from app.core.enums import HmiProduct
from app.models.delivery import AlarmDefinition, HMIBinding, HMIScreen
from app.models.profile import TargetPlatform
from app.orchestration.handlers import TaskContext

CANDIDATE_STATUS = "DRAFT - GENERATED CANDIDATE; NOT IMPORT-VERIFIED"
GENERATOR_VERSION = "1.0.0"


def _value(value: object) -> str:
    return str(getattr(value, "value", value))


def _safe_csv_value(value: object) -> str:
    text = "" if value is None else _value(value)
    return f"'{text}" if text.startswith(("=", "+", "-", "@")) else text


def _csv_bytes(rows: list[list[object]]) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\r\n")
    for row in rows:
        writer.writerow([_safe_csv_value(value) for value in row])
    return stream.getvalue().encode("utf-8-sig")


def _slug(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    return cleaned or "project"


@dataclass(frozen=True, slots=True)
class HMITemplateDefinition:
    """Approved template identity; intentionally not a free-form XML body."""

    template_id: str
    version: str
    supported_products: tuple[HmiProduct, ...]
    screen_types: tuple[str, ...]
    source_checksum: str


class HMITemplateRegistry:
    """Exact-version registry for approved HMI template identities."""

    def __init__(self) -> None:
        self._templates: dict[tuple[str, str], HMITemplateDefinition] = {}

    def add(self, template: HMITemplateDefinition) -> None:
        key = (template.template_id, template.version)
        if not all(key):
            raise ValueError("HMI template id and version must be non-empty")
        if key in self._templates:
            raise ValueError(f"duplicate HMI template identity: {key!r}")
        if len(template.source_checksum) != 64:
            raise ValueError("HMI template source_checksum must be a SHA-256 hex digest")
        self._templates[key] = template

    def resolve(
        self, template_id: str, version: str, product: HmiProduct
    ) -> HMITemplateDefinition | None:
        found = self._templates.get((template_id, version))
        return found if found is not None and product in found.supported_products else None

    def identities(self) -> tuple[tuple[str, str], ...]:
        return tuple(sorted(self._templates))


@dataclass(frozen=True, slots=True)
class GeneratorCapability:
    """One explicit supported/unavailable target answer."""

    discipline: str
    provider_key: str
    target_product: str
    target_version: str
    available: bool
    artifact_kinds: tuple[str, ...] = ()
    unavailable_artifacts: tuple[tuple[str, str], ...] = ()
    reason: str = ""
    version: str = GENERATOR_VERSION

    def summary(self) -> dict[str, object]:
        return {
            "discipline": self.discipline,
            "provider_key": self.provider_key,
            "target_product": self.target_product,
            "target_version": self.target_version,
            "available": self.available,
            "artifact_kinds": list(self.artifact_kinds),
            "unavailable_artifacts": dict(self.unavailable_artifacts),
            "reason": self.reason,
            "version": self.version,
        }


@dataclass(frozen=True, slots=True)
class GeneratedCandidate:
    """In-memory candidate with honest validation and provenance labels."""

    kind: str
    filename: str
    media_type: str
    data: bytes
    target_product: str
    target_version: str
    source_revision: int
    generator_key: str
    generator_version: str = GENERATOR_VERSION
    document_status: str = CANDIDATE_STATUS
    validation_level: str = "generated"
    import_verified: bool = False
    limitations: tuple[str, ...] = ()
    metadata: tuple[tuple[str, str], ...] = ()

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()

    def summary(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "filename": self.filename,
            "media_type": self.media_type,
            "size_bytes": len(self.data),
            "sha256": self.sha256,
            "target_product": self.target_product,
            "target_version": self.target_version,
            "source_revision": self.source_revision,
            "generator_key": self.generator_key,
            "generator_version": self.generator_version,
            "document_status": self.document_status,
            "validation_level": self.validation_level,
            "import_verified": self.import_verified,
            "limitations": list(self.limitations),
            "metadata": dict(self.metadata),
            "canonical_writes": 0,
        }


@dataclass(frozen=True, slots=True)
class GenerationPlan:
    capability: GeneratorCapability
    candidates: tuple[GeneratedCandidate, ...] = ()
    warnings: tuple[str, ...] = ()

    def summary(self) -> dict[str, object]:
        return {
            "capability": self.capability.summary(),
            "candidates": [item.summary() for item in self.candidates],
            "warnings": list(self.warnings),
        }


class HMIGeneratorPlugin(Protocol):
    key: str
    version: str
    supported_products: tuple[HmiProduct, ...]

    def plan(
        self,
        context: TaskContext,
        target: TargetPlatform,
        templates: HMITemplateRegistry,
    ) -> GenerationPlan: ...


class AlarmGeneratorPlugin(Protocol):
    key: str
    version: str
    supported_products: tuple[HmiProduct, ...]

    def plan(self, context: TaskContext, target: TargetPlatform) -> GenerationPlan: ...


class FactoryTalkMEHMIPlugin:
    """Generate a deterministic HMI tag-binding CSV candidate for View ME."""

    key = "factorytalk_view_me.hmi_csv_candidate"
    version = GENERATOR_VERSION
    supported_products: tuple[HmiProduct, ...] = (HmiProduct.FACTORYTALK_VIEW_ME,)

    def plan(
        self,
        context: TaskContext,
        target: TargetPlatform,
        templates: HMITemplateRegistry,
    ) -> GenerationPlan:
        if not target.hmi_version.strip():
            return GenerationPlan(
                GeneratorCapability(
                    discipline="hmi",
                    provider_key=self.key,
                    target_product=target.hmi_product.value,
                    target_version="",
                    available=False,
                    reason="FactoryTalk View ME target version is not recorded.",
                )
            )
        screens = list(
            context.db.execute(
                select(HMIScreen).where(HMIScreen.project_id == context.project.id)
            ).scalars()
        )
        bindings = list(
            context.db.execute(
                select(HMIBinding).where(HMIBinding.project_id == context.project.id)
            ).scalars()
        )
        screen_by_id = {item.id: item for item in screens}
        rows: list[list[object]] = [
            [
                "screen_key",
                "property_name",
                "tag_name",
                "tag_id",
                "resolved",
                "access",
                "expression",
                "template_id",
                "template_version",
                "template_registered",
                "target_product",
                "target_version",
                "validation_level",
                "import_verified",
            ]
        ]
        unresolved_count = 0
        unregistered_templates: set[str] = set()
        for binding in sorted(
            bindings,
            key=lambda item: (
                screen_by_id[item.screen_id].key.casefold()
                if item.screen_id in screen_by_id
                else "",
                item.property_name.casefold(),
                item.tag_name.casefold(),
                str(item.id),
            ),
        ):
            screen = screen_by_id.get(binding.screen_id)
            template_registered = bool(
                screen
                and templates.resolve(
                    screen.template_id, screen.template_version, target.hmi_product
                )
            )
            if screen and screen.template_id and not template_registered:
                unregistered_templates.add(
                    f"{screen.template_id}@{screen.template_version or '(unversioned)'}"
                )
            if not binding.is_resolved:
                unresolved_count += 1
            rows.append(
                [
                    screen.key if screen else "",
                    binding.property_name,
                    binding.tag_name,
                    binding.tag_id or "",
                    str(binding.is_resolved).lower(),
                    binding.access,
                    binding.expression,
                    screen.template_id if screen else "",
                    screen.template_version if screen else "",
                    str(template_registered).lower(),
                    target.hmi_product.value,
                    target.hmi_version,
                    "generated",
                    "false",
                ]
            )
        limitations = [
            "Candidate only; no FactoryTalk import or runtime validation was performed.",
            (
                "FactoryTalk display XML is unavailable because no approved vendor schema and "
                "versioned XML template assets are installed."
            ),
        ]
        if unresolved_count:
            limitations.append(f"{unresolved_count} binding(s) are unresolved.")
        if unregistered_templates:
            limitations.append(
                "Unregistered templates: " + ", ".join(sorted(unregistered_templates))
            )
        candidate = GeneratedCandidate(
            kind="hmi_tags_csv_candidate",
            filename=(
                f"{_slug(context.project.name)}-hmi-tags-r"
                f"{context.project.current_revision_number}.csv"
            ),
            media_type="text/csv; charset=utf-8",
            data=_csv_bytes(rows),
            target_product=target.hmi_product.value,
            target_version=target.hmi_version,
            source_revision=context.project.current_revision_number,
            generator_key=self.key,
            limitations=tuple(limitations),
            metadata=(
                ("binding_count", str(len(bindings))),
                ("unresolved_binding_count", str(unresolved_count)),
                ("template_registry_count", str(len(templates.identities()))),
            ),
        )
        capability = GeneratorCapability(
            discipline="hmi",
            provider_key=self.key,
            target_product=target.hmi_product.value,
            target_version=target.hmi_version,
            available=True,
            artifact_kinds=("hmi_tags_csv_candidate",),
            unavailable_artifacts=(
                (
                    "display_xml_candidate",
                    "No approved vendor schema/versioned display XML templates are installed.",
                ),
            ),
        )
        return GenerationPlan(capability=capability, candidates=(candidate,))


class FactoryTalkMEAlarmPlugin:
    """Generate deterministic View ME alarm and cross-reference CSV candidates."""

    key = "factorytalk_view_me.alarm_csv_candidate"
    version = GENERATOR_VERSION
    supported_products: tuple[HmiProduct, ...] = (HmiProduct.FACTORYTALK_VIEW_ME,)

    def plan(self, context: TaskContext, target: TargetPlatform) -> GenerationPlan:
        if not target.hmi_version.strip():
            return GenerationPlan(
                GeneratorCapability(
                    discipline="alarm",
                    provider_key=self.key,
                    target_product=target.hmi_product.value,
                    target_version="",
                    available=False,
                    reason="FactoryTalk View ME target version is not recorded.",
                )
            )
        alarms = list(
            context.db.execute(
                select(AlarmDefinition).where(AlarmDefinition.project_id == context.project.id)
            ).scalars()
        )
        import_rows: list[list[object]] = [
            [
                "reference",
                "name",
                "trigger_tag",
                "trigger_type",
                "severity",
                "message",
                "operator_guidance",
                "delay_seconds",
                "deadband",
                "requires_acknowledge",
                "resolved",
                "driven",
                "target_product",
                "target_version",
                "validation_level",
                "import_verified",
            ]
        ]
        cross_reference_rows: list[list[object]] = [
            [
                "reference",
                "trigger_tag",
                "trigger_tag_id",
                "fault_definition_id",
                "group",
                "resolved",
                "driven",
            ]
        ]
        trigger_gap_count = 0
        for alarm in sorted(alarms, key=lambda item: (item.reference.casefold(), str(item.id))):
            messages = sorted(alarm.messages, key=lambda item: (item.language_code, item.ordinal))
            selected_message = messages[0] if messages else None
            if not alarm.trigger_tag_name or not alarm.is_resolved or not alarm.is_driven:
                trigger_gap_count += 1
            import_rows.append(
                [
                    alarm.reference,
                    alarm.name,
                    alarm.trigger_tag_name,
                    alarm.trigger_type,
                    alarm.severity.value,
                    selected_message.text if selected_message else "",
                    selected_message.operator_guidance if selected_message else "",
                    alarm.delay_seconds if alarm.delay_seconds is not None else "",
                    alarm.deadband,
                    str(alarm.requires_acknowledge).lower(),
                    str(alarm.is_resolved).lower(),
                    str(alarm.is_driven).lower(),
                    target.hmi_product.value,
                    target.hmi_version,
                    "generated",
                    "false",
                ]
            )
            cross_reference_rows.append(
                [
                    alarm.reference,
                    alarm.trigger_tag_name,
                    alarm.trigger_tag_id or "",
                    alarm.fault_definition_id or "",
                    alarm.group_name,
                    str(alarm.is_resolved).lower(),
                    str(alarm.is_driven).lower(),
                ]
            )
        limitations = [
            "Candidates only; no FactoryTalk import or runtime validation was performed.",
            "Delay/deadband semantics require target-version validation on licensed tooling.",
        ]
        if trigger_gap_count:
            limitations.append(
                f"{trigger_gap_count} alarm(s) have an absent, unresolved, or undriven trigger."
            )
        common: dict[str, object] = {
            "target_product": target.hmi_product.value,
            "target_version": target.hmi_version,
            "source_revision": context.project.current_revision_number,
            "generator_key": self.key,
            "limitations": tuple(limitations),
            "metadata": (
                ("alarm_count", str(len(alarms))),
                ("trigger_gap_count", str(trigger_gap_count)),
            ),
        }
        stem = f"{_slug(context.project.name)}-alarms-r{context.project.current_revision_number}"
        candidates = (
            GeneratedCandidate(
                kind="alarm_import_csv_candidate",
                filename=f"{stem}.csv",
                media_type="text/csv; charset=utf-8",
                data=_csv_bytes(import_rows),
                **common,  # type: ignore[arg-type]
            ),
            GeneratedCandidate(
                kind="alarm_cross_reference_csv",
                filename=f"{stem}-cross-reference.csv",
                media_type="text/csv; charset=utf-8",
                data=_csv_bytes(cross_reference_rows),
                **common,  # type: ignore[arg-type]
            ),
        )
        return GenerationPlan(
            capability=GeneratorCapability(
                discipline="alarm",
                provider_key=self.key,
                target_product=target.hmi_product.value,
                target_version=target.hmi_version,
                available=True,
                artifact_kinds=tuple(item.kind for item in candidates),
            ),
            candidates=candidates,
        )


PluginT = TypeVar("PluginT", HMIGeneratorPlugin, AlarmGeneratorPlugin)


class DeliveryGeneratorRegistry:
    """Target selector for installed HMI and alarm candidate plugins."""

    def __init__(self) -> None:
        self.hmi_plugins: dict[str, HMIGeneratorPlugin] = {}
        self.alarm_plugins: dict[str, AlarmGeneratorPlugin] = {}

    @staticmethod
    def _add(target: dict[str, PluginT], plugin: PluginT) -> None:
        if plugin.key in target:
            raise ValueError(f"duplicate delivery generator key: {plugin.key!r}")
        target[plugin.key] = plugin

    def add_hmi(self, plugin: HMIGeneratorPlugin) -> None:
        self._add(self.hmi_plugins, plugin)

    def add_alarm(self, plugin: AlarmGeneratorPlugin) -> None:
        self._add(self.alarm_plugins, plugin)

    @staticmethod
    def _select(plugins: dict[str, PluginT], product: HmiProduct) -> PluginT | None:
        return next(
            (
                plugin
                for _key, plugin in sorted(plugins.items())
                if product in plugin.supported_products
            ),
            None,
        )

    def plan_hmi(
        self,
        context: TaskContext,
        target: TargetPlatform | None,
        templates: HMITemplateRegistry,
    ) -> GenerationPlan:
        if target is None or target.hmi_product is HmiProduct.NONE:
            return _unavailable_plan("hmi", target, "No HMI target product is configured.")
        plugin = self._select(self.hmi_plugins, target.hmi_product)
        if plugin is None:
            return _unavailable_plan(
                "hmi",
                target,
                f"No target-specific HMI generator is installed for {target.hmi_product.value}.",
            )
        return plugin.plan(context, target, templates)

    def plan_alarm(self, context: TaskContext, target: TargetPlatform | None) -> GenerationPlan:
        if target is None or target.hmi_product is HmiProduct.NONE:
            return _unavailable_plan("alarm", target, "No HMI/alarm target is configured.")
        plugin = self._select(self.alarm_plugins, target.hmi_product)
        if plugin is None:
            return _unavailable_plan(
                "alarm",
                target,
                f"No target-specific alarm generator is installed for {target.hmi_product.value}.",
            )
        return plugin.plan(context, target)


def _unavailable_plan(
    discipline: str, target: TargetPlatform | None, reason: str
) -> GenerationPlan:
    return GenerationPlan(
        capability=GeneratorCapability(
            discipline=discipline,
            provider_key="unavailable",
            target_product=target.hmi_product.value if target is not None else "not_configured",
            target_version=target.hmi_version if target is not None else "",
            available=False,
            reason=reason,
        ),
        warnings=("No candidate artifact was generated.",),
    )


DEFAULT_HMI_TEMPLATES = HMITemplateRegistry()
DEFAULT_GENERATORS = DeliveryGeneratorRegistry()
DEFAULT_GENERATORS.add_hmi(FactoryTalkMEHMIPlugin())
DEFAULT_GENERATORS.add_alarm(FactoryTalkMEAlarmPlugin())


def target_for(context: TaskContext) -> TargetPlatform | None:
    return context.db.execute(
        select(TargetPlatform).where(TargetPlatform.project_id == context.project.id)
    ).scalar_one_or_none()


def plan_hmi_candidates(
    context: TaskContext,
    *,
    registry: DeliveryGeneratorRegistry = DEFAULT_GENERATORS,
    templates: HMITemplateRegistry = DEFAULT_HMI_TEMPLATES,
) -> GenerationPlan:
    return registry.plan_hmi(context, target_for(context), templates)


def plan_alarm_candidates(
    context: TaskContext, *, registry: DeliveryGeneratorRegistry = DEFAULT_GENERATORS
) -> GenerationPlan:
    return registry.plan_alarm(context, target_for(context))
