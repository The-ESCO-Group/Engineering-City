"""Deterministic, read-only testing and commissioning delivery audit."""

from __future__ import annotations

from sqlalchemy import select

from app.agents.delivery.audit import DeliveryAuditResult, ReadOnlyDeliveryAgent, issue
from app.core.enums import ObjectType, RequirementStatus
from app.models.delivery import TestCase
from app.models.interlocks import Interlock
from app.models.requirements import Requirement
from app.orchestration.handlers import TaskContext


class TestingCommissioningAgent(ReadOnlyDeliveryAgent):
    kind = "testing_commissioning_delivery"
    display_name = "Testing and Commissioning Agent"
    location = "testing_grounds"

    def inspect(self, context: TaskContext) -> DeliveryAuditResult:
        project_id = context.project.id
        requirements = list(
            context.db.execute(
                select(Requirement).where(Requirement.project_id == project_id)
            ).scalars()
        )
        interlocks = list(
            context.db.execute(
                select(Interlock).where(Interlock.project_id == project_id)
            ).scalars()
        )
        tests = list(
            context.db.execute(select(TestCase).where(TestCase.project_id == project_id)).scalars()
        )
        active_requirements = [
            item
            for item in requirements
            if item.status not in {RequirementStatus.REJECTED, RequirementStatus.SUPERSEDED}
        ]
        covered_requirements = {item.requirement_id for item in tests if item.requirement_id}
        covered_interlocks = {item.interlock_id for item in tests if item.interlock_id}
        issues = []

        for requirement in active_requirements:
            if requirement.id in covered_requirements:
                continue
            issues.append(
                issue(
                    code="testing.requirement_uncovered",
                    severity="error" if requirement.is_safety_related else "warning",
                    title=f"Requirement {requirement.reference} has no test case",
                    detail="No canonical test case links to this active requirement.",
                    object_type=ObjectType.REQUIREMENT,
                    object_id=requirement.id,
                    reference=requirement.reference,
                    requires_human_review=requirement.is_safety_related,
                )
            )

        for interlock in interlocks:
            if interlock.id in covered_interlocks:
                continue
            issues.append(
                issue(
                    code="testing.interlock_uncovered",
                    severity="error" if interlock.is_safety_related else "warning",
                    title=f"Interlock {interlock.reference} has no test case",
                    detail="No canonical test case links to this interlock.",
                    object_type=ObjectType.INTERLOCK,
                    object_id=interlock.id,
                    reference=interlock.reference,
                    requires_human_review=interlock.is_safety_related,
                )
            )

        for test in tests:
            verification_steps = [step for step in test.steps if step.is_verification_point]
            if not test.steps or not verification_steps:
                issues.append(
                    issue(
                        code="testing.verification_step_missing",
                        severity="error",
                        title=f"Test {test.reference} has no verification step",
                        detail=(
                            "A test with only setup/teardown steps does not demonstrate coverage."
                        ),
                        object_type=ObjectType.TEST_CASE,
                        object_id=test.id,
                        reference=test.reference,
                        requires_human_review=test.is_destructive_test,
                    )
                )
            if not test.preconditions.strip() or not test.expected_result.strip():
                issues.append(
                    issue(
                        code="testing.procedure_incomplete",
                        severity="warning",
                        title=f"Test {test.reference} has incomplete conditions or result",
                        detail=(
                            "Prerequisites and expected results must come from requirements or "
                            "recorded engineering decisions; none were invented."
                        ),
                        object_type=ObjectType.TEST_CASE,
                        object_id=test.id,
                        reference=test.reference,
                        requires_human_review=test.is_destructive_test,
                    )
                )

        return DeliveryAuditResult(
            agent_kind=self.kind,
            location=self.location,
            scope_examined="Active requirements, interlocks, test cases, and verification steps",
            issues=tuple(issues),
            counts={
                "active_requirements": len(active_requirements),
                "interlocks": len(interlocks),
                "test_cases": len(tests),
                "covered_requirements": len(covered_requirements),
                "covered_interlocks": len(covered_interlocks),
            },
            warnings=(
                "Physical behavior, safe plant state, and commissioning authority are not "
                "inferred from offline project records.",
            ),
        )
