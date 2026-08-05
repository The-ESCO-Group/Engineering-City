"""The deterministic validators, and the run that decides whether a project ships.

Nothing in this package uses a model. Every check is deterministic code over the
canonical engineering model, so the same revision produces the same result every
time it is asked — which is what makes a release gate something an engineer can
argue with rather than something they have to trust.

Result codes are published API
--------------------------------
Every code is namespaced by discipline — ``requirements.*``, ``tags.*``, ``io.*``,
``logic.*``, ``cross.*`` — and is stable for the life of the code. Clients branch
on them and customers write waiver policies against them, so a code is **added or
retired, never quietly repurposed**. Changing what an existing code means silently
invalidates every waiver already written against it, and the customer finds out by
shipping something they thought they had accepted a different thing about.

The one code outside the discipline namespaces is ``validator.failed``. It is the
run reporting on itself; see below.

Severity is an argument, not a label
------------------------------------
Only :attr:`~app.core.enums.ValidationSeverity.ERROR` blocks a release, and it is
used only where proceeding would produce something *wrong*: an operand that
resolves to nothing, two signals on one terminal, a documented interlock with no
logic. Every ``ERROR`` in this package carries a comment at the point it is raised
saying why it is one. A missing description is a ``WARNING``, and it stays a
warning: a gate that treats every warning as a blocker trains people to bypass the
gate, and then it blocks nothing at all.

``UNSUPPORTED`` is a real severity and is used freely. "We did not check this"
must stay distinguishable from "we checked and it was fine" — a structured text
routine, an Add-On Instruction whose definition was never supplied, an HMI
expression stored verbatim. Silence in those places would be the most expensive
kind of clean result.

A validator that raises must not stop the run
---------------------------------------------
:func:`run_validation` catches it, records an ``ERROR`` result coded
``validator.failed`` naming the validator and the exception, and carries on. A run
that dies halfway and reports "3 issues" is worse than one that reports its own
failure: the first looks like a clean result.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.clock import utcnow
from app.core.enums import (
    EventType,
    SemanticLocation,
    ValidationDiscipline,
    ValidationRunStatus,
    ValidationSeverity,
)
from app.core.logging import get_logger
from app.models.project import Project
from app.models.release import ValidationResult, ValidationRun
from app.orchestration.events import record_event
from app.validation.base import (
    MAX_OBJECT_LABEL_CHARS,
    Issue,
    ValidationContext,
    Validator,
    label,
)
from app.validation.cross_discipline import (
    AlarmCoverageValidator,
    ApprovalIntegrityValidator,
    HMIBindingValidator,
    ReleaseFreshnessValidator,
    TestCoverageValidator,
)
from app.validation.io import (
    DeviceCoverageValidator,
    FailStateValidator,
    IOAssignmentValidator,
    IOHardwareValidator,
)
from app.validation.logic import (
    DestructiveWriteValidator,
    InstructionSupportValidator,
    InterlockImplementationValidator,
    ModeLogicValidator,
    OperandValidator,
    OutputCoverageValidator,
    ResetBehaviourValidator,
    RoutineCallValidator,
    SequenceStateValidator,
)
from app.validation.requirements import (
    ClarificationValidator,
    CoverageValidator,
    TraceabilityValidator,
)
from app.validation.tags import (
    TagAliasValidator,
    TagDocumentationValidator,
    TagNameValidator,
    TagUsageValidator,
)

logger = get_logger(__name__)

__all__ = [
    "ALL_DISCIPLINES",
    "VALIDATORS",
    "VALIDATOR_FAILED_CODE",
    "Issue",
    "ValidationContext",
    "Validator",
    "run_validation",
    "validators_for",
]

#: The code a run uses to report its own failure. Outside the discipline
#: namespaces on purpose: it is a statement about this software, not about the
#: customer's engineering.
VALIDATOR_FAILED_CODE = "validator.failed"

#: The registry, in the order a run executes it. Ordering is deliberate rather
#: than incidental: requirements first, because a project whose basis is not
#: settled has nothing worth checking downstream; cross-discipline last, because
#: it compares things the earlier disciplines have already reported on
#: individually.
VALIDATORS: tuple[Validator, ...] = (
    # Requirements — is the basis for this project settled?
    ClarificationValidator(),
    TraceabilityValidator(),
    CoverageValidator(),
    # Tags — is the vocabulary legal, documented and used?
    TagNameValidator(),
    TagDocumentationValidator(),
    TagAliasValidator(),
    TagUsageValidator(),
    # I/O — is every device wired, and is anything wired twice?
    IOAssignmentValidator(),
    IOHardwareValidator(),
    DeviceCoverageValidator(),
    FailStateValidator(),
    # PLC logic — does the ladder do what the documents say?
    OperandValidator(),
    DestructiveWriteValidator(),
    OutputCoverageValidator(),
    RoutineCallValidator(),
    InstructionSupportValidator(),
    ModeLogicValidator(),
    InterlockImplementationValidator(),
    ResetBehaviourValidator(),
    SequenceStateValidator(),
    # Cross-discipline — the comparisons no single discipline can make.
    HMIBindingValidator(),
    AlarmCoverageValidator(),
    TestCoverageValidator(),
    ApprovalIntegrityValidator(),
    ReleaseFreshnessValidator(),
)

#: Every discipline the registry can speak for. A run asked for no particular
#: discipline records this set, because a clean result has to say what it covered.
ALL_DISCIPLINES: tuple[ValidationDiscipline, ...] = tuple(
    dict.fromkeys(validator.discipline for validator in VALIDATORS)
)


def validators_for(
    disciplines: Sequence[ValidationDiscipline] | None = None,
) -> tuple[Validator, ...]:
    """The registered validators covering ``disciplines``, in registry order."""
    if disciplines is None:
        return VALIDATORS
    wanted = set(disciplines)
    return tuple(validator for validator in VALIDATORS if validator.discipline in wanted)


def run_validation(
    db: Session,
    *,
    project: Project,
    disciplines: Sequence[ValidationDiscipline] | None = None,
    task_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
) -> ValidationRun:
    """Execute the validators over ``project`` and persist what they found.

    Does not commit. The caller owns the transaction, so the run and its results
    land if and only if the events describing them do.
    """
    # Validation and reviewer waivers both use the project as their root lock.
    # This makes "newest completed run for the current revision" stable until
    # either transaction commits and prevents a reviewer from signing a result
    # that becomes stale during the decision itself.
    project = db.execute(
        select(Project).where(Project.id == project.id).with_for_update()
    ).scalar_one()

    selected = validators_for(disciplines)
    covered = (
        tuple(dict.fromkeys(validator.discipline for validator in selected))
        if disciplines is not None
        else ALL_DISCIPLINES
    )
    revision_number = project.current_revision_number

    run = ValidationRun(
        project_id=project.id,
        revision_number=revision_number,
        status=ValidationRunStatus.RUNNING,
        triggered_by_task_id=task_id,
        triggered_by_id=user_id,
        disciplines=[discipline.value for discipline in covered],
        started_at=utcnow(),
        created_in_revision=revision_number,
        updated_in_revision=revision_number,
    )
    db.add(run)
    db.flush()

    record_event(
        db,
        project_id=project.id,
        event_type=EventType.VALIDATION_STARTED,
        task_id=task_id,
        actor_user_id=user_id,
        payload={
            "validation_run_id": str(run.id),
            "revision_number": revision_number,
            "location": SemanticLocation.QA_CAMPUS.value,
            "disciplines": [discipline.value for discipline in covered],
            "triggered_by_task_id": str(task_id) if task_id else None,
            "triggered_by_id": str(user_id) if user_id else None,
        },
    )

    context = ValidationContext(db=db, project=project, revision_number=revision_number)

    issues: list[Issue] = []
    for validator in selected:
        try:
            issues.extend(validator.check(context))
        except Exception as exc:
            # Recorded as a finding rather than raised. A run that dies halfway
            # and reports what it managed to find first is indistinguishable
            # from a clean one, and it is the clean-looking failure that gets
            # believed.
            logger.exception(
                "validator raised",
                extra={
                    "project_id": str(project.id),
                    "validation_run_id": str(run.id),
                    "validator": validator.key,
                },
            )
            issues.append(
                Issue(
                    code=VALIDATOR_FAILED_CODE,
                    discipline=validator.discipline,
                    severity=ValidationSeverity.ERROR,
                    message=(
                        f"Validator '{validator.key}' failed with "
                        f"{type(exc).__name__}: {exc}. Everything it checks is unexamined in "
                        "this run, so this result is not a clean one."
                    ),
                    object_label=label(project.name),
                    suggested_action=(
                        "Report this as a platform defect. Do not treat the rest of this run "
                        "as covering what this validator checks."
                    ),
                    detail={
                        "validator": validator.key,
                        "discipline": validator.discipline.value,
                        "exception_type": type(exc).__name__,
                        "exception": str(exc),
                    },
                )
            )

    results = [_persist(db, run=run, project=project, issue=issue) for issue in issues]
    db.flush()

    counts = dict.fromkeys(ValidationSeverity, 0)
    for result in results:
        counts[result.severity] += 1

    run.error_count = counts[ValidationSeverity.ERROR]
    run.warning_count = counts[ValidationSeverity.WARNING]
    run.information_count = counts[ValidationSeverity.INFORMATION]
    run.unsupported_count = counts[ValidationSeverity.UNSUPPORTED]
    # Counted as "results a named human still has to rule on", which is both what
    # the column is called and the number anybody reading it wants: a restricted
    # instruction is reported at UNSUPPORTED severity and still needs a decision,
    # and a roll-up that counted only the severity would report zero.
    run.human_review_count = sum(
        1
        for result in results
        if result.requires_human_decision
        or result.severity is ValidationSeverity.HUMAN_REVIEW_REQUIRED
    )
    run.checks_run = len(selected)
    run.status = ValidationRunStatus.COMPLETED
    run.finished_at = utcnow()
    run.summary = _summarise(run, covered)

    first_error = next(
        (result for result in results if result.severity is ValidationSeverity.ERROR), None
    )
    db.flush()

    record_event(
        db,
        project_id=project.id,
        event_type=EventType.VALIDATION_COMPLETED,
        task_id=task_id,
        actor_user_id=user_id,
        payload={
            "validation_run_id": str(run.id),
            "revision_number": run.revision_number,
            "status": run.status.value,
            "location": SemanticLocation.QA_CAMPUS.value,
            "summary": run.summary,
            "disciplines": [discipline.value for discipline in covered],
            "error_count": run.error_count,
            "warning_count": run.warning_count,
            "information_count": run.information_count,
            "unsupported_count": run.unsupported_count,
            "human_review_count": run.human_review_count,
            "checks_run": run.checks_run,
            "is_blocking": run.is_blocking,
            "first_error_result_id": str(first_error.id) if first_error else None,
            "first_error_object_label": first_error.object_label if first_error else "",
        },
    )

    logger.info(
        "validation run completed",
        extra={
            "project_id": str(project.id),
            "validation_run_id": str(run.id),
            "revision_number": run.revision_number,
            "checks_run": run.checks_run,
            "error_count": run.error_count,
            "warning_count": run.warning_count,
            "unsupported_count": run.unsupported_count,
        },
    )
    return run


def _persist(
    db: Session, *, run: ValidationRun, project: Project, issue: Issue
) -> ValidationResult:
    result = ValidationResult(
        project_id=project.id,
        validation_run_id=run.id,
        code=issue.code,
        discipline=issue.discipline,
        severity=issue.severity,
        message=issue.message,
        detail=dict(issue.detail),
        object_type=issue.object_type,
        object_id=issue.object_id,
        object_label=issue.object_label[:MAX_OBJECT_LABEL_CHARS],
        suggested_action=issue.suggested_action,
        requires_human_decision=issue.requires_human_decision,
    )
    db.add(result)
    return result


def _summarise(run: ValidationRun, covered: Sequence[ValidationDiscipline]) -> str:
    """One line, bounded to what the event payload will carry.

    States the scope before the counts. A reader who sees "no errors" without
    knowing how many checks ran, over which disciplines, at which revision, has
    been handed a number they cannot interpret.
    """
    scope = (
        f"{run.checks_run} check(s) over {len(covered)} discipline(s) "
        f"at revision {run.revision_number}"
    )
    findings = (
        f"{run.error_count} error(s), {run.warning_count} warning(s), "
        f"{run.information_count} information, {run.unsupported_count} unsupported, "
        f"{run.human_review_count} needing a human decision"
    )
    verdict = (
        "Release blocked until the errors are resolved or waived."
        if run.is_blocking
        else "Nothing blocks a release candidate."
    )
    return f"{scope}: {findings}. {verdict}"[:500]
