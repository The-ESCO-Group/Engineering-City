"""Deterministic, read-only HMI engineering delivery audit."""

from __future__ import annotations

from sqlalchemy import select

from app.agents.delivery.audit import DeliveryAuditResult, ReadOnlyDeliveryAgent, issue
from app.agents.delivery.generators import plan_hmi_candidates
from app.core.enums import ObjectType
from app.models.delivery import HMIBinding, HMINavigation, HMIScreen
from app.orchestration.handlers import TaskContext


class HMIEngineeringAgent(ReadOnlyDeliveryAgent):
    kind = "hmi_engineering_delivery"
    display_name = "HMI Engineering Agent"
    location = "hmi_district"

    def inspect(self, context: TaskContext) -> DeliveryAuditResult:
        project_id = context.project.id
        generation_plan = plan_hmi_candidates(context)
        screens = list(
            context.db.execute(
                select(HMIScreen).where(HMIScreen.project_id == project_id)
            ).scalars()
        )
        bindings = list(
            context.db.execute(
                select(HMIBinding).where(HMIBinding.project_id == project_id)
            ).scalars()
        )
        navigation = list(
            context.db.execute(
                select(HMINavigation).where(HMINavigation.project_id == project_id)
            ).scalars()
        )
        screen_names = {item.id: item.key for item in screens}
        issues = []

        for screen in screens:
            if not screen.template_id or not screen.template_version:
                issues.append(
                    issue(
                        code="hmi.template_unpinned",
                        severity="warning",
                        title=f"HMI screen {screen.key} has no pinned template version",
                        detail=(
                            "A target-specific display candidate cannot be reproduced without "
                            "a template identifier and version."
                        ),
                        object_type=ObjectType.HMI_SCREEN,
                        object_id=screen.id,
                        reference=screen.key,
                    )
                )

        for binding in bindings:
            screen_key = screen_names.get(binding.screen_id, str(binding.screen_id))
            if not binding.tag_name.strip() or not binding.is_resolved:
                reason = binding.unresolved_reason or "no canonical tag resolution was recorded"
                issues.append(
                    issue(
                        code="hmi.missing_binding",
                        severity="error" if binding.is_command else "warning",
                        title=f"HMI binding on {screen_key} is unresolved",
                        detail=(
                            f"Property {binding.property_name or '(unspecified)'} references "
                            f"{binding.tag_name or '(blank tag)'}: {reason}."
                        ),
                        object_type=ObjectType.HMI_BINDING,
                        object_id=binding.id,
                        reference=f"{screen_key}:{binding.property_name or 'binding'}",
                        requires_human_review=binding.is_command,
                    )
                )

        for edge in navigation:
            if not edge.is_resolved:
                source = screen_names.get(edge.from_screen_id, str(edge.from_screen_id))
                issues.append(
                    issue(
                        code="hmi.navigation_unresolved",
                        severity="warning",
                        title=f"Navigation from {source} has no resolved destination",
                        detail=f"Destination key {edge.to_screen_key or '(blank)'} is unresolved.",
                        object_type=ObjectType.HMI_SCREEN,
                        object_id=edge.from_screen_id,
                        reference=f"{source} -> {edge.to_screen_key or '(blank)'}",
                    )
                )

        return DeliveryAuditResult(
            agent_kind=self.kind,
            location=self.location,
            scope_examined="HMI screens, tag bindings, and navigation edges",
            issues=tuple(issues),
            counts={
                "screens": len(screens),
                "bindings": len(bindings),
                "navigation_edges": len(navigation),
            },
            warnings=(
                "This is a design-data audit, not FactoryTalk runtime or import validation.",
            ),
            generator_plan=generation_plan.summary(),
        )
