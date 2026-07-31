"""Checks that only exist between two disciplines.

Codes in this module are namespaced ``cross.*`` and are published API — see the
package docstring.

Everything here is a comparison. An HMI binding is not wrong on its own and
neither is a tag list; the defect is that the binding names a tag the controller
does not have. A test plan is not wrong on its own; the defect is that the safety
interlock in the schedule appears in none of it. These are the findings the
delivery tables were pulled into one database for — a plugin keeping its screens
in its own store would push every one of them out to commissioning, which is
exactly where they are caught today and exactly where they cost the most.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterable, Sequence
from typing import Any

from app.core.clock import ensure_utc
from app.core.enums import (
    ObjectApprovalState,
    ObjectType,
    ReleaseCandidateStatus,
    RequirementStatus,
    TagRole,
    ValidationDiscipline,
    ValidationSeverity,
)
from app.models.delivery import HMIBinding
from app.validation.base import Issue, ValidationContext, fold, label, references

__all__ = [
    "AlarmCoverageValidator",
    "ApprovalIntegrityValidator",
    "HMIBindingValidator",
    "ReleaseFreshnessValidator",
    "TestCoverageValidator",
]

_DISCIPLINE = ValidationDiscipline.CROSS_DISCIPLINE

#: Requirement states where an absent test is a real gap; see the requirements
#: module for the same reasoning about implementation coverage.
_LIVE_REQUIREMENT_STATUSES = frozenset(
    {
        RequirementStatus.ACCEPTED,
        RequirementStatus.IMPLEMENTED,
        RequirementStatus.VERIFIED,
    }
)

#: Release candidate states where the package is still on its way out of the
#: door. A released or superseded candidate is history: it was built from the
#: revision it was built from, and saying so afterwards would be noise.
_LIVE_CANDIDATE_STATUSES = frozenset(
    {
        ReleaseCandidateStatus.DRAFT,
        ReleaseCandidateStatus.GENERATING,
        ReleaseCandidateStatus.GENERATED,
        ReleaseCandidateStatus.AWAITING_APPROVAL,
        ReleaseCandidateStatus.APPROVED,
    }
)


class HMIBindingValidator:
    """Bindings that reach no tag, and tags no logic drives or acts on."""

    key = "cross.hmi_bindings"
    discipline = _DISCIPLINE
    description = "HMI bindings against the tag model and the logic that has to serve them."

    def check(self, context: ValidationContext) -> Iterable[Issue]:
        issues: list[Issue] = []
        io_driven = {fold(tag.name) for tag in context.tags if tag.is_io_bound or tag.is_alias}

        for binding in context.hmi_bindings:
            screen = context.hmi_screens_by_id.get(binding.screen_id)
            screen_name = screen.name if screen is not None else "unknown screen"
            location = label(
                f"{screen_name} / {binding.tag_name or binding.expression or '(no reference)'}"
                f" / {binding.property_name or 'no property'}"
            )

            if not binding.tag_name.strip() and binding.expression.strip():
                issues.append(
                    Issue(
                        code="cross.hmi_expression_unchecked",
                        discipline=_DISCIPLINE,
                        severity=ValidationSeverity.UNSUPPORTED,
                        message=(
                            f"Binding on '{screen_name}' animates "
                            f"{binding.property_name or 'a property'} from the expression "
                            f"'{binding.expression}'. Expressions are stored verbatim and "
                            "not parsed, so the tags inside it were not checked."
                        ),
                        object_type=ObjectType.HMI_BINDING,
                        object_id=binding.id,
                        object_label=location,
                        suggested_action=(
                            "Check by hand that every tag named in the expression exists and "
                            "is driven."
                        ),
                        detail={"expression": binding.expression, "screen": screen_name},
                    )
                )
                continue

            resolved = self._resolved_name(context, binding.tag_name, binding.tag_id)
            if resolved is None:
                issues.append(self._unresolved_issue(binding, location, screen_name))
                continue

            if not context.has_ladder_logic:
                continue

            if binding.is_command:
                if not any(references(name, resolved) for name in context.read_names):
                    issues.append(
                        Issue(
                            code="cross.hmi_write_not_handled",
                            discipline=_DISCIPLINE,
                            # ERROR, and higher than the equivalent read defect
                            # for a reason worth stating: an unhandled write is
                            # invisible. The button depresses, the value is
                            # accepted, the display looks entirely normal and
                            # the controller does nothing with it. An unresolved
                            # read shows itself on the display, where the
                            # operator can see something is wrong before it
                            # matters.
                            severity=ValidationSeverity.ERROR,
                            message=(
                                f"'{screen_name}' writes '{resolved}' but no rung reads it. "
                                "The operator's command is accepted and nothing acts on it."
                            ),
                            object_type=ObjectType.HMI_BINDING,
                            object_id=binding.id,
                            object_label=location,
                            suggested_action=(
                                "Add the logic that acts on this command, or remove the "
                                "write binding if the control is not in scope."
                            ),
                            detail={
                                "tag_name": resolved,
                                "screen": screen_name,
                                "property_name": binding.property_name,
                            },
                        )
                    )
                continue

            if fold(resolved) in io_driven:
                # Driven by the I/O module rather than by a rung.
                continue
            if not context.is_written(resolved):
                issues.append(
                    Issue(
                        code="cross.status_never_driven",
                        discipline=_DISCIPLINE,
                        # WARNING: the operator sees a field that never changes.
                        # It is a real defect, but it is visible on the display
                        # and it is also the shape a value written by an Add-On
                        # Instruction takes when the AOI's definition was not
                        # supplied — so it is put in front of a human rather
                        # than used to stop a release.
                        severity=ValidationSeverity.WARNING,
                        message=(
                            f"'{screen_name}' displays '{resolved}', which no rung writes and "
                            "which is not bound to an input. The field will never change."
                        ),
                        object_type=ObjectType.HMI_BINDING,
                        object_id=binding.id,
                        object_label=location,
                        suggested_action=(
                            "Add the logic that maintains this status, or bind the display "
                            "to the tag that already carries it."
                        ),
                        detail={
                            "tag_name": resolved,
                            "screen": screen_name,
                            "property_name": binding.property_name,
                        },
                    )
                )

        return issues

    @staticmethod
    def _resolved_name(
        context: ValidationContext, tag_name: str, tag_id: uuid.UUID | None
    ) -> str | None:
        """The tag this binding actually reaches, or ``None``.

        The tag table is the authority rather than ``HMIBinding.is_resolved``: the
        flag records what a resolver concluded at import time, and a tag deleted
        or renamed since would leave it saying yes.
        """
        match = context.tags_by_id.get(tag_id) if tag_id is not None else None
        if match is not None:
            return match.name
        cleaned = tag_name.strip()
        if not cleaned:
            return None
        base = fold(cleaned).split(".", 1)[0].split("[", 1)[0]
        candidates = context.tags_by_folded_name.get(base)
        if candidates:
            return cleaned
        return None

    @staticmethod
    def _unresolved_issue(binding: HMIBinding, location: str, screen_name: str) -> Issue:
        write = binding.is_command
        return Issue(
            code=(
                "cross.hmi_write_binding_unresolved"
                if write
                else "cross.hmi_read_binding_unresolved"
            ),
            discipline=_DISCIPLINE,
            # A write binding to a tag that does not exist is an ERROR for the
            # same reason an unhandled write is: the control looks like it works
            # and reaches nothing, and that failure arrives on the plant floor.
            # An unresolved read is a WARNING because it announces itself — the
            # field is blank or dashed on the display, in front of the operator,
            # long before anybody depends on it.
            severity=ValidationSeverity.ERROR if write else ValidationSeverity.WARNING,
            message=(
                f"'{screen_name}' {'writes' if write else 'displays'} "
                f"'{binding.tag_name or '(no tag named)'}', which is not a tag in this "
                "project." + (f" {binding.unresolved_reason}" if binding.unresolved_reason else "")
            ),
            object_type=ObjectType.HMI_BINDING,
            object_id=binding.id,
            object_label=location,
            suggested_action=(
                "Point the binding at a declared tag, or create the tag it was written against."
            ),
            detail={
                "tag_name": binding.tag_name,
                "screen": screen_name,
                "access": binding.access,
                "property_name": binding.property_name,
            },
        )


class AlarmCoverageValidator:
    """Alarms with nothing to raise them, and conditions nothing annunciates."""

    key = "cross.alarm_coverage"
    discipline = _DISCIPLINE
    description = "Alarm definitions without a trigger, and triggers without an alarm."

    def check(self, context: ValidationContext) -> Iterable[Issue]:
        issues: list[Issue] = []
        faults_by_id = {fault.id: fault for fault in context.fault_definitions}

        for alarm in context.alarms:
            trigger = alarm.trigger_tag_name.strip()
            if not trigger and alarm.fault_definition_id is not None:
                fault = faults_by_id.get(alarm.fault_definition_id)
                if fault is not None:
                    trigger = fault.trigger_tag_name.strip()
            if trigger:
                continue
            issues.append(
                Issue(
                    code="cross.alarm_without_trigger",
                    discipline=_DISCIPLINE,
                    # ERROR: an alarm with no trigger is an entry in the alarm
                    # list and nothing else. It exports, it appears in the HMI
                    # alarm summary, and it can never be raised by anything.
                    severity=ValidationSeverity.ERROR,
                    message=(
                        f"Alarm {alarm.reference} '{alarm.name}' names no trigger tag and no "
                        "fault definition that supplies one."
                    ),
                    object_type=ObjectType.ALARM_DEFINITION,
                    object_id=alarm.id,
                    object_label=label(f"Alarm / {alarm.reference} / {alarm.name}"),
                    suggested_action=(
                        "Record the tag or the condition that raises this alarm, or remove "
                        "the alarm if it is not in scope."
                    ),
                    detail={
                        "reference": alarm.reference,
                        "severity": alarm.severity.value,
                        "trigger_type": alarm.trigger_type,
                    },
                )
            )

        annunciated_faults = {
            alarm.fault_definition_id
            for alarm in context.alarms
            if alarm.fault_definition_id is not None
        }
        alarm_triggers = {
            fold(alarm.trigger_tag_name.strip())
            for alarm in context.alarms
            if alarm.trigger_tag_name.strip()
        }

        for fault in context.fault_definitions:
            if fault.id in annunciated_faults:
                continue
            if fault.trigger_tag_name.strip() and fold(fault.trigger_tag_name) in alarm_triggers:
                continue
            issues.append(
                Issue(
                    code="cross.trigger_without_alarm",
                    discipline=_DISCIPLINE,
                    # WARNING: the condition is detected and nobody is told. It
                    # is not always a defect — a diagnostic bit may be
                    # deliberately internal — so the judgement is left with the
                    # engineer who knows which.
                    severity=ValidationSeverity.WARNING,
                    message=(
                        f"Fault '{fault.name}' is defined but no alarm annunciates it. The "
                        "condition is detected and the operator is never told."
                    ),
                    object_type=ObjectType.FAULT_DEFINITION,
                    object_id=fault.id,
                    object_label=label(f"Fault / {fault.reference or fault.name}"),
                    suggested_action=(
                        "Add the alarm that annunciates this fault, or record that it is "
                        "deliberately internal."
                    ),
                    detail={
                        "reference": fault.reference,
                        "fault_class": fault.fault_class.value,
                        "trigger_tag_name": fault.trigger_tag_name,
                    },
                )
            )

        for tag in context.tags:
            if tag.role is not TagRole.ALARM:
                continue
            if fold(tag.name) in alarm_triggers:
                continue
            if any(
                references(fold(fault.trigger_tag_name), tag.name)
                for fault in context.fault_definitions
                if fault.trigger_tag_name.strip()
            ):
                continue
            issues.append(
                Issue(
                    code="cross.trigger_without_alarm",
                    discipline=_DISCIPLINE,
                    severity=ValidationSeverity.WARNING,
                    message=(
                        f"'{tag.name}' is declared as an alarm tag but no alarm or fault "
                        "definition uses it as a trigger."
                    ),
                    object_type=ObjectType.TAG,
                    object_id=tag.id,
                    object_label=label(f"Alarm tag / {tag.name}"),
                    suggested_action=(
                        "Define the alarm this tag raises, or change the tag's role if it is "
                        "not an alarm."
                    ),
                    detail={"tag_name": tag.name, "role": tag.role.value},
                )
            )

        return issues


class TestCoverageValidator:
    """What the test plan proves, and what it leaves unproved."""

    key = "cross.test_coverage"
    discipline = _DISCIPLINE
    description = "Tests that trace to nothing, and requirements and interlocks with no test."

    def check(self, context: ValidationContext) -> Iterable[Issue]:
        issues: list[Issue] = []

        for case in context.test_cases:
            if case.requirement_id is not None or case.interlock_id is not None:
                continue
            issues.append(
                Issue(
                    code="cross.test_without_requirement",
                    discipline=_DISCIPLINE,
                    # WARNING: a test that traces to nothing may still be a
                    # perfectly good test. What it cannot do is count towards
                    # coverage, and that is what this is here to say.
                    severity=ValidationSeverity.WARNING,
                    message=(
                        f"Test {case.reference} '{case.title}' cites no requirement and no "
                        "interlock, so it counts towards no coverage."
                    ),
                    object_type=ObjectType.TEST_CASE,
                    object_id=case.id,
                    object_label=label(f"Test / {case.reference} / {case.title}"),
                    suggested_action=(
                        "Link the test to the requirement or interlock it exercises, or "
                        "record why it stands alone."
                    ),
                    detail={"reference": case.reference, "test_type": case.test_type},
                )
            )

        tested_requirements = {
            case.requirement_id for case in context.test_cases if case.requirement_id is not None
        }
        for requirement in context.requirements:
            if requirement.status not in _LIVE_REQUIREMENT_STATUSES:
                continue
            if requirement.id in tested_requirements:
                continue
            issues.append(
                Issue(
                    code="cross.requirement_without_test",
                    discipline=_DISCIPLINE,
                    # WARNING across the board. A missing test is a gap in the
                    # deliverable rather than a wrong artifact, and a gate that
                    # blocked every release on complete test coverage would be
                    # bypassed on the first project that ships in phases.
                    severity=ValidationSeverity.WARNING,
                    message=(
                        f"{requirement.reference} is {requirement.status.value} and no test "
                        "case exercises it."
                    ),
                    object_type=ObjectType.REQUIREMENT,
                    object_id=requirement.id,
                    object_label=label(f"{requirement.reference} / {requirement.title}"),
                    suggested_action=(
                        "Write the test that would demonstrate this requirement, or record "
                        "the verification method that replaces one."
                    ),
                    detail={
                        "reference": requirement.reference,
                        "kind": requirement.kind.value,
                        "priority": requirement.priority.value,
                        "verification_method": requirement.verification_method,
                    },
                )
            )

        tested_interlocks = {
            case.interlock_id for case in context.test_cases if case.interlock_id is not None
        }
        for interlock in context.interlocks:
            if interlock.id in tested_interlocks:
                continue
            safety = interlock.is_safety_related
            issues.append(
                Issue(
                    code="cross.interlock_without_test",
                    discipline=_DISCIPLINE,
                    # ERROR for a safety-related interlock: the package would
                    # ship with a FAT that passes without ever exercising the
                    # protection, and a signed test sheet that does not cover a
                    # safety function is worse than no test sheet, because it
                    # reads as assurance. A non-safety interlock is a coverage
                    # gap for an engineer to weigh.
                    severity=ValidationSeverity.ERROR if safety else ValidationSeverity.WARNING,
                    message=(
                        f"Interlock {interlock.reference} '{interlock.name}'"
                        f"{' is safety-related and' if safety else ''} has no test case."
                    ),
                    object_type=ObjectType.INTERLOCK,
                    object_id=interlock.id,
                    object_label=label(f"Interlock / {interlock.reference} / {interlock.name}"),
                    suggested_action=(
                        "Write the test that trips this interlock and observes the action it "
                        "is documented to take."
                    ),
                    detail={
                        "reference": interlock.reference,
                        "kind": interlock.kind.value,
                        "is_safety_related": safety,
                    },
                )
            )

        return issues


class ApprovalIntegrityValidator:
    """Objects that carry a signature for content they no longer hold."""

    key = "cross.approval_integrity"
    discipline = _DISCIPLINE
    description = "Approved objects edited after the approval was recorded."

    def check(self, context: ValidationContext) -> Iterable[Issue]:
        issues: list[Issue] = []

        for object_type, rows, describe in _approvable_populations(context):
            for row in rows:
                if row.approval_state is not ObjectApprovalState.APPROVED:
                    continue
                if row.approved_at is None:
                    continue
                approved_at = ensure_utc(row.approved_at)
                updated_at = ensure_utc(row.updated_at)
                if updated_at <= approved_at:
                    continue
                issues.append(
                    Issue(
                        code="cross.approved_object_modified",
                        discipline=_DISCIPLINE,
                        # ERROR: an approved object is protected precisely so an
                        # engineer's signature stays attached to what they read.
                        # One that changed afterwards carries a name against
                        # content that person never saw, which is the failure
                        # the whole approval mechanism exists to prevent.
                        severity=ValidationSeverity.ERROR,
                        message=(
                            f"{object_type.value} '{describe(row)}' was approved at "
                            f"{approved_at.isoformat()} and modified at "
                            f"{updated_at.isoformat()}, without a fresh approval."
                        ),
                        object_type=object_type,
                        object_id=row.id,
                        object_label=label(f"{object_type.value} / {describe(row)}"),
                        suggested_action=(
                            "Supersede the approved object with the edited version and take "
                            "a fresh approval, or revert the edit."
                        ),
                        detail={
                            "approved_at": approved_at.isoformat(),
                            "updated_at": updated_at.isoformat(),
                            "object_revision": row.object_revision,
                        },
                    )
                )

        return issues


class ReleaseFreshnessValidator:
    """Packages assembled from a revision the project has moved past."""

    key = "cross.release_freshness"
    discipline = _DISCIPLINE
    description = "Release candidates built from a revision older than the one being validated."

    def check(self, context: ValidationContext) -> Iterable[Issue]:
        issues: list[Issue] = []

        for candidate in context.release_candidates:
            if candidate.status not in _LIVE_CANDIDATE_STATUSES:
                continue
            if candidate.revision_number >= context.revision_number:
                continue
            issues.append(
                Issue(
                    code="cross.stale_release_artifact",
                    discipline=_DISCIPLINE,
                    # ERROR: the package does not contain the model. Every
                    # checksum in it is correct and every artifact in it is
                    # stale, which is the worst combination — it looks verified
                    # and it is a picture of a project that has moved on.
                    severity=ValidationSeverity.ERROR,
                    message=(
                        f"Release candidate {candidate.candidate_number} was built from "
                        f"revision {candidate.revision_number}; the project is at revision "
                        f"{context.revision_number}. Its "
                        f"{len(candidate.artifacts)} artifact(s) do not contain the current "
                        "model."
                    ),
                    object_type=ObjectType.RELEASE_CANDIDATE,
                    object_id=candidate.id,
                    object_label=label(
                        f"RC {candidate.candidate_number}"
                        f"{f' / {candidate.name}' if candidate.name else ''}"
                    ),
                    suggested_action=(
                        "Regenerate the candidate from the current revision, or supersede it."
                    ),
                    detail={
                        "candidate_number": candidate.candidate_number,
                        "candidate_revision": candidate.revision_number,
                        "project_revision": context.revision_number,
                        "status": candidate.status.value,
                    },
                )
            )

        return issues


def _approvable_populations(
    context: ValidationContext,
) -> Sequence[tuple[ObjectType, Sequence[Any], Callable[[Any], str]]]:
    """The object kinds whose approvals this validator checks.

    Written out rather than derived, for the same reason as the traceability
    sweep: an object kind that silently drops out of an integrity check is the
    defect the check exists to catch.
    """
    return (
        (ObjectType.REQUIREMENT, context.requirements, lambda row: row.reference),
        (ObjectType.TAG, context.tags, lambda row: row.name),
        (
            ObjectType.LOGIC_RUNG,
            context.rungs,
            lambda row: context.rung_label(row.id),
        ),
        (ObjectType.ROUTINE, context.routines, lambda row: row.name),
        (
            ObjectType.INTERLOCK,
            context.interlocks,
            lambda row: f"{row.reference} {row.name}",
        ),
        (ObjectType.PERMISSIVE, context.permissives, lambda row: row.name),
        (
            ObjectType.ALARM_DEFINITION,
            context.alarms,
            lambda row: f"{row.reference} {row.name}",
        ),
        (ObjectType.DEVICE, context.devices, lambda row: row.tag_prefix),
        (ObjectType.IO_POINT, context.io_points, lambda row: row.address),
        (
            ObjectType.TEST_CASE,
            context.test_cases,
            lambda row: f"{row.reference} {row.title}",
        ),
    )
