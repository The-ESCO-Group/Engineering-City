"""Requirements-discipline checks: is the basis for this project settled?

Codes in this module are namespaced ``requirements.*`` and are published API —
see the package docstring.

The three checks here are about *provenance*, not about content. This module can
tell that a question nobody answered still blocks work, that a generated rung
cites nothing, and that an accepted requirement has nothing pointing back at it.
It cannot tell whether the thing that cites a requirement actually satisfies it,
and it says so with an ``UNSUPPORTED`` issue rather than letting a clean result
imply a coverage claim it never made.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterable, Sequence
from typing import Any

from app.core.enums import (
    ClarificationStatus,
    LogicOrigin,
    ObjectType,
    RequirementPriority,
    RequirementStatus,
    ValidationDiscipline,
    ValidationSeverity,
)
from app.validation.base import Issue, ValidationContext, label

__all__ = [
    "ClarificationValidator",
    "CoverageValidator",
    "TraceabilityValidator",
]

_DISCIPLINE = ValidationDiscipline.REQUIREMENTS

#: Clarification states that still owe an answer. ``DEFERRED`` is included on
#: purpose: deferring records that a human looked and chose to come back later,
#: which is not the same as supplying the missing reading.
_UNRESOLVED_STATUSES = frozenset({ClarificationStatus.OPEN, ClarificationStatus.DEFERRED})

#: Requirement states where an absent implementation is a real gap. A proposed
#: requirement nobody has accepted, or one that was rejected or superseded, is
#: *supposed* to have nothing built for it.
_LIVE_REQUIREMENT_STATUSES = frozenset(
    {
        RequirementStatus.ACCEPTED,
        RequirementStatus.IMPLEMENTED,
        RequirementStatus.VERIFIED,
    }
)

#: Origins that mean this platform produced the object rather than read it out of
#: a customer document or received it from an engineer's own hand. These are the
#: objects the traceability promise is about.
_GENERATED_ORIGINS = frozenset({LogicOrigin.TEMPLATE_EXPANSION, LogicOrigin.AGENT_PROPOSED})


class ClarificationValidator:
    """Ambiguities and project questions that are still open."""

    key = "requirements.clarifications"
    discipline = _DISCIPLINE
    description = "Clarifications and project questions that have not been answered."

    def check(self, context: ValidationContext) -> Iterable[Issue]:
        issues: list[Issue] = []
        requirement_reference = {
            requirement.id: requirement.reference for requirement in context.requirements
        }

        for clarification in context.clarifications:
            if clarification.status not in _UNRESOLVED_STATUSES:
                continue

            target = "the project"
            if clarification.requirement_id is not None:
                target = requirement_reference.get(
                    clarification.requirement_id, str(clarification.requirement_id)
                )
            elif clarification.object_type is not None:
                target = f"{clarification.object_type.value} {clarification.object_id}"

            # Severity follows the field the engineer already set. A
            # clarification marked as blocking generation says, in the model,
            # that continuing means choosing one of two readings silently — and
            # a reading picked without an author is a requirement this platform
            # invented. One that does not block is a question worth answering
            # that somebody has already decided the work can proceed past.
            blocking = clarification.blocks_generation
            issues.append(
                Issue(
                    code="requirements.unresolved_clarification",
                    discipline=_DISCIPLINE,
                    severity=(ValidationSeverity.ERROR if blocking else ValidationSeverity.WARNING),
                    message=(
                        f"Clarification on {target} is still {clarification.status.value}: "
                        f"{clarification.question.strip() or 'no question text was recorded'}"
                    ),
                    object_type=ObjectType.CLARIFICATION,
                    object_id=clarification.id,
                    object_label=label(f"Clarification / {target}"),
                    suggested_action=(
                        "Record the answer on the clarification, or clear "
                        "blocks_generation if the work can proceed either way."
                        if blocking
                        else "Answer the clarification so the reading it names is on record."
                    ),
                    detail={
                        "status": clarification.status.value,
                        "blocks_generation": blocking,
                        "ambiguity": clarification.ambiguity,
                    },
                )
            )

        for question in context.questions:
            if question.status not in _UNRESOLVED_STATUSES:
                continue
            issues.append(
                Issue(
                    code="requirements.unanswered_question",
                    discipline=_DISCIPLINE,
                    # ``is_blocking`` already folds in ``is_mandatory``: a
                    # mandatory question is by definition one the platform
                    # cannot proceed without, so proceeding past it produces
                    # work governed by a guess.
                    severity=(
                        ValidationSeverity.ERROR
                        if question.is_blocking
                        else ValidationSeverity.WARNING
                    ),
                    message=(
                        f"{question.reference} is still {question.status.value}: "
                        f"{question.question.strip()}"
                    ),
                    object_type=ObjectType.PROJECT_QUESTION,
                    object_id=question.id,
                    object_label=label(f"Question {question.reference}"),
                    suggested_action=(
                        "Answer the question and record the decision it produces, "
                        "or mark it dismissed if the answer is no longer needed."
                    ),
                    detail={
                        "reference": question.reference,
                        "status": question.status.value,
                        "is_mandatory": question.is_mandatory,
                        "category": question.category.value,
                        "why_it_matters": question.why_it_matters,
                        "blocks_task_kinds": list(question.blocks_task_kinds),
                    },
                )
            )

        return issues


class TraceabilityValidator:
    """Generated objects that cite no basis at all."""

    key = "requirements.traceability"
    discipline = _DISCIPLINE
    description = (
        "Objects this platform generated that cite no requirement, template or human decision."
    )

    def check(self, context: ValidationContext) -> Iterable[Issue]:
        issues: list[Issue] = []

        for object_type, rows, describe in _traceable_populations(context):
            for row in rows:
                if row.origin not in _GENERATED_ORIGINS:
                    # Read out of a customer document, or written by an engineer
                    # directly. Neither is a thing this platform generated, and
                    # reporting them here would drown the finding that matters.
                    continue
                links = context.evidence_by_object.get(row.id, ())
                if any(link.satisfies_traceability for link in links):
                    continue

                issues.append(
                    Issue(
                        code="requirements.object_without_basis",
                        discipline=_DISCIPLINE,
                        # ERROR: the traceability promise is the product. An
                        # object that exists for no stated reason cannot be
                        # reviewed — a reviewer has nothing to check it
                        # against — so a package containing one is a package
                        # nobody can sign.
                        severity=ValidationSeverity.ERROR,
                        message=(
                            f"{object_type.value} '{describe(row)}' was generated by "
                            f"{row.origin.value} but cites no requirement, template rule or "
                            "human decision as its basis."
                        ),
                        object_type=object_type,
                        object_id=row.id,
                        object_label=label(f"{object_type.value} / {describe(row)}"),
                        suggested_action=(
                            "Add an evidence link citing the requirement, approved template "
                            "or recorded decision this object was produced from, or delete it."
                        ),
                        detail={
                            "origin": row.origin.value,
                            "created_by_agent": row.created_by_agent,
                            "evidence_link_count": len(links),
                        },
                    )
                )
        return issues


class CoverageValidator:
    """Accepted requirements that nothing points back at."""

    key = "requirements.coverage"
    discipline = _DISCIPLINE
    description = "Live requirements with no object citing them as its basis."

    def check(self, context: ValidationContext) -> Iterable[Issue]:
        issues: list[Issue] = []
        cited: set[uuid.UUID] = {
            link.requirement_id
            for link in context.evidence_links
            if link.requirement_id is not None
        }

        for requirement in context.requirements:
            if requirement.status not in _LIVE_REQUIREMENT_STATUSES:
                continue
            if requirement.id in cited:
                continue

            mandatory = requirement.priority is RequirementPriority.MANDATORY
            issues.append(
                Issue(
                    code="requirements.no_implementation_coverage",
                    discipline=_DISCIPLINE,
                    # A mandatory requirement the customer accepted, with
                    # nothing in the model built against it, means the package
                    # does not do something it was agreed it would do. That is
                    # producing something wrong, so it blocks. An expected or
                    # optional requirement in the same state is a gap for an
                    # engineer to weigh.
                    severity=(
                        ValidationSeverity.ERROR if mandatory else ValidationSeverity.WARNING
                    ),
                    message=(
                        f"{requirement.reference} is {requirement.status.value} and "
                        f"{requirement.priority.value}, but no object in the model cites it "
                        "as its basis."
                    ),
                    object_type=ObjectType.REQUIREMENT,
                    object_id=requirement.id,
                    object_label=label(f"{requirement.reference} / {requirement.title}"),
                    suggested_action=(
                        "Build what the requirement asks for and cite it, or record why it "
                        "is out of scope and move the requirement to rejected or superseded."
                    ),
                    detail={
                        "reference": requirement.reference,
                        "status": requirement.status.value,
                        "priority": requirement.priority.value,
                        "kind": requirement.kind.value,
                        "is_inferred": requirement.is_inferred,
                    },
                )
            )

        if context.requirements:
            # Stated once per run, not once per requirement. The distinction
            # between "we checked this and it was fine" and "we did not check
            # this" only has to be made once, and repeating it per row would
            # bury the findings that are actionable.
            issues.append(
                Issue(
                    code="requirements.coverage_is_citation_only",
                    discipline=_DISCIPLINE,
                    severity=ValidationSeverity.UNSUPPORTED,
                    message=(
                        "Requirement coverage was established from evidence links only. "
                        "Whether the cited logic, tags or tests actually satisfy what each "
                        "requirement asks for is an engineering judgement this validator "
                        "does not make."
                    ),
                    object_label=label(context.project.name),
                    suggested_action=(
                        "Have a qualified engineer read each requirement against the objects "
                        "that cite it before the package is released."
                    ),
                    detail={"requirements_examined": len(context.requirements)},
                )
            )

        return issues


def _traceable_populations(
    context: ValidationContext,
) -> Sequence[tuple[ObjectType, Sequence[Any], Callable[[Any], str]]]:
    """The traceable object kinds this validator can reach, with a label for each.

    Written out rather than derived from ``TRACEABLE_OBJECT_TYPES`` so that a new
    member of that set fails to be checked *visibly* — an object kind silently
    dropped from a traceability sweep is exactly the gap the sweep exists to
    close, and a loop over an enum would hide it behind a missing branch.
    """
    return (
        (ObjectType.TAG, context.tags, lambda row: row.name),
        (
            ObjectType.LOGIC_RUNG,
            context.rungs,
            lambda row: context.rung_label(row.id),
        ),
        (ObjectType.ROUTINE, context.routines, lambda row: row.name),
        (ObjectType.PROGRAM, context.programs, lambda row: row.name),
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
        (
            ObjectType.HMI_BINDING,
            context.hmi_bindings,
            lambda row: f"{row.tag_name or '(no tag)'} on {row.property_name or '(no property)'}",
        ),
        (ObjectType.HMI_SCREEN, context.hmi_screens, lambda row: row.name),
        (
            ObjectType.TEST_CASE,
            context.test_cases,
            lambda row: f"{row.reference} {row.title}",
        ),
        (ObjectType.DEVICE, context.devices, lambda row: row.tag_prefix),
        (ObjectType.IO_POINT, context.io_points, lambda row: row.address),
        (ObjectType.UDT_DEFINITION, context.udt_definitions, lambda row: row.name),
    )
