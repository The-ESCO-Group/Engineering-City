"""Deterministic, read-only alarm engineering delivery audit."""

from __future__ import annotations

from collections import Counter

from sqlalchemy import select

from app.agents.delivery.audit import DeliveryAuditResult, ReadOnlyDeliveryAgent, issue
from app.agents.delivery.generators import plan_alarm_candidates
from app.core.enums import AlarmSeverity, ObjectType
from app.models.delivery import AlarmDefinition
from app.orchestration.handlers import TaskContext


class AlarmEngineeringAgent(ReadOnlyDeliveryAgent):
    kind = "alarm_engineering_delivery"
    display_name = "Alarm Engineering Agent"
    location = "alarm_center"

    def inspect(self, context: TaskContext) -> DeliveryAuditResult:
        generation_plan = plan_alarm_candidates(context)
        alarms = list(
            context.db.execute(
                select(AlarmDefinition).where(AlarmDefinition.project_id == context.project.id)
            ).scalars()
        )
        reference_counts = Counter(item.reference.casefold() for item in alarms)
        trigger_counts = Counter(
            item.trigger_tag_name.casefold() for item in alarms if item.trigger_tag_name.strip()
        )
        issues = []
        for alarm in alarms:
            human_review = alarm.severity in {AlarmSeverity.HIGH, AlarmSeverity.URGENT}
            if reference_counts[alarm.reference.casefold()] > 1:
                issues.append(
                    issue(
                        code="alarm.duplicate_identifier",
                        severity="error",
                        title=f"Alarm identifier {alarm.reference} is duplicated",
                        detail="Multiple canonical alarm rows use the same case-insensitive ID.",
                        object_type=ObjectType.ALARM_DEFINITION,
                        object_id=alarm.id,
                        reference=alarm.reference,
                        requires_human_review=True,
                    )
                )
            if not alarm.trigger_tag_name.strip() or not alarm.is_resolved:
                issues.append(
                    issue(
                        code="alarm.missing_trigger",
                        severity="error",
                        title=f"Alarm {alarm.reference} has no resolved trigger tag",
                        detail=(
                            f"Recorded trigger is {alarm.trigger_tag_name or '(blank)'}; "
                            "the agent does not infer a replacement."
                        ),
                        object_type=ObjectType.ALARM_DEFINITION,
                        object_id=alarm.id,
                        reference=alarm.reference,
                        requires_human_review=human_review,
                    )
                )
            elif not alarm.is_driven:
                issues.append(
                    issue(
                        code="alarm.trigger_not_driven",
                        severity="error",
                        title=f"Alarm {alarm.reference} trigger is not driven",
                        detail=(
                            f"Tag {alarm.trigger_tag_name} resolves, but canonical logic coverage "
                            "does not record it as driven."
                        ),
                        object_type=ObjectType.ALARM_DEFINITION,
                        object_id=alarm.id,
                        reference=alarm.reference,
                        requires_human_review=human_review,
                    )
                )
            if (
                alarm.trigger_tag_name.strip()
                and trigger_counts[alarm.trigger_tag_name.casefold()] > 1
            ):
                issues.append(
                    issue(
                        code="alarm.duplicate_trigger",
                        severity="warning",
                        title=f"Alarm {alarm.reference} shares a trigger",
                        detail=f"More than one alarm references {alarm.trigger_tag_name}.",
                        object_type=ObjectType.ALARM_DEFINITION,
                        object_id=alarm.id,
                        reference=alarm.reference,
                        requires_human_review=human_review,
                    )
                )
            if not alarm.messages or any(not message.text.strip() for message in alarm.messages):
                issues.append(
                    issue(
                        code="alarm.message_missing",
                        severity="warning",
                        title=f"Alarm {alarm.reference} has no complete operator message",
                        detail="At least one operator-facing alarm message is absent or blank.",
                        object_type=ObjectType.ALARM_DEFINITION,
                        object_id=alarm.id,
                        reference=alarm.reference,
                        requires_human_review=human_review,
                    )
                )
            if not alarm.messages or any(
                not message.operator_guidance.strip() for message in alarm.messages
            ):
                issues.append(
                    issue(
                        code="alarm.operator_response_missing",
                        severity="warning",
                        title=f"Alarm {alarm.reference} lacks operator-response guidance",
                        detail="No response was invented; a qualified reviewer must supply it.",
                        object_type=ObjectType.ALARM_DEFINITION,
                        object_id=alarm.id,
                        reference=alarm.reference,
                        requires_human_review=human_review,
                    )
                )

        return DeliveryAuditResult(
            agent_kind=self.kind,
            location=self.location,
            scope_examined="Alarm identifiers, trigger bindings, logic drive state, and messages",
            issues=tuple(issues),
            counts={"alarms": len(alarms)},
            warnings=("Target-product delay/deadband behavior is not inferred by this audit.",),
            generator_plan=generation_plan.summary(),
        )
