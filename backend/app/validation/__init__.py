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

An accepted error stays accepted until the thing changes
--------------------------------------------------------
A run writes fresh :class:`~app.models.release.ValidationResult` rows every time,
so "blocks a release until resolved **or** accepted" only holds if a reviewer's
waiver survives the next run of the validators. It is carried forward on the
check code and the object the result is about, never on the row id — the row id
is new every run, which is what made an accepted error come back unwaived and
park the pipeline for good.

It is carried forward *only while the object has not moved on*. A waiver is
recorded against a revision, and the revision is what makes that meaningful: a
decision taken over the model as it stood at revision 7 says nothing about an
object somebody edited at revision 8, so its waiver is dropped and the error
returns for a fresh decision. Everything about this fails closed — an object that
cannot be resolved, a preceding run claiming a revision the project has not
reached, a second occurrence of a check nobody waived twice — because losing a
waiver stops a release and inheriting one ships something nobody signed for.
"""

from __future__ import annotations

import uuid
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.clock import ensure_utc, utcnow
from app.core.enums import (
    EventType,
    SemanticLocation,
    ValidationDiscipline,
    ValidationRunStatus,
    ValidationSeverity,
)
from app.core.logging import get_logger
from app.models.core import EngineeringObjectMixin
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

#: What a result is *about*, rather than which row happens to be carrying it:
#: the check code, the object type, the object id and the label an engineer
#: follows. Stable across runs, which is the whole requirement — the row id is
#: not, so a waiver keyed on one would be lost the moment the validators ran
#: again.
_ResultIdentity = tuple[str, str, uuid.UUID | None, str]

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

    carried = _carried_waivers(
        db, project=project, revision_number=revision_number, excluding=run.id
    )
    results = [
        _persist(db, run=run, project=project, issue=issue, waiver=_claim(carried, issue))
        for issue in issues
    ]
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
    db: Session,
    *,
    run: ValidationRun,
    project: Project,
    issue: Issue,
    waiver: _CarriedWaiver | None = None,
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
        is_waived=waiver is not None,
        waived_by_id=waiver.waived_by_id if waiver is not None else None,
        waiver_reason=waiver.reason if waiver is not None else "",
    )
    db.add(result)
    return result


@dataclass(frozen=True, slots=True)
class _CarriedWaiver:
    """One reviewer's acceptance, ready to re-attach to a freshly written result.

    The name and the reason travel with it. A carried waiver that lost them would
    say "somebody accepted this", which is the one thing a waiver must never say.
    """

    waived_by_id: uuid.UUID | None
    reason: str


def _carried_waivers(
    db: Session, *, project: Project, revision_number: int, excluding: uuid.UUID
) -> dict[_ResultIdentity, deque[_CarriedWaiver]]:
    """The waivers from the preceding run that still describe the same object.

    Only the immediately preceding completed run is read, and its
    ``revision_number`` is the revision every waiver on it is anchored to: a
    waiver can only be recorded against the newest completed run at the current
    revision, so anything it carries forward was decided over the model as that
    run saw it.

    A queue per identity rather than a single entry, because one run can
    legitimately raise the same check against the same object twice — two
    unresolved operands on one rung — and a reviewer waived one of them.
    """
    recent = list(
        db.execute(
            select(ValidationRun)
            .where(
                ValidationRun.project_id == project.id,
                ValidationRun.id != excluding,
                ValidationRun.status == ValidationRunStatus.COMPLETED,
                ValidationRun.deleted_at.is_(None),
            )
            .order_by(ValidationRun.created_at.desc(), ValidationRun.id.desc())
            .limit(2)
        ).scalars()
    )
    if not recent or recent[0].revision_number > revision_number:
        # A run claiming a revision the project has not reached cannot anchor a
        # decision to one, so nothing is carried and the errors come back.
        return {}
    if len(recent) > 1 and ensure_utc(recent[0].created_at) == ensure_utc(recent[1].created_at):
        # The rule the waiver service already applies: two runs sharing a
        # recorded creation time leave neither authoritative, so there is no
        # snapshot a waiver can be said to have been decided over.
        return {}
    previous = recent[0]

    carried: dict[_ResultIdentity, deque[_CarriedWaiver]] = {}
    waived = db.execute(
        select(ValidationResult)
        .where(
            ValidationResult.validation_run_id == previous.id,
            ValidationResult.is_waived.is_(True),
        )
        .order_by(ValidationResult.created_at, ValidationResult.id)
    ).scalars()
    for result in waived:
        if _object_moved_on(
            db,
            result=result,
            waived_revision=previous.revision_number,
            revision_number=revision_number,
        ):
            logger.info(
                "validation waiver went stale",
                extra={
                    "project_id": str(project.id),
                    "code": result.code,
                    "object_label": result.object_label,
                    "waived_revision": previous.revision_number,
                    "revision_number": revision_number,
                },
            )
            continue
        carried.setdefault(_result_identity(result), deque()).append(
            _CarriedWaiver(waived_by_id=result.waived_by_id, reason=result.waiver_reason)
        )
    return carried


def _claim(
    carried: dict[_ResultIdentity, deque[_CarriedWaiver]], issue: Issue
) -> _CarriedWaiver | None:
    """The waiver covering this issue, consumed so no second issue inherits it."""
    queue = carried.get(_issue_identity(issue))
    return queue.popleft() if queue else None


def _object_moved_on(
    db: Session,
    *,
    result: ValidationResult,
    waived_revision: int,
    revision_number: int,
) -> bool:
    """Whether the thing a waiver was about has changed since it was signed."""
    if result.object_type is None or result.object_id is None:
        # Nothing narrower than the project was named, so the project is the
        # subject and reaching a new revision is a change to it.
        return revision_number > waived_revision

    # Imported at call time, not at module scope: :mod:`app.engineering.gates`
    # imports this package, so a top-level import here would close the cycle.
    # ``app.engineering`` is always loaded by the time this runs — it is on the
    # only import path by which this module can be reached at all — so this is
    # not a lazy import hiding an optional dependency.
    from app.engineering.registry import UnknownObjectTypeError, model_for

    try:
        model = model_for(result.object_type)
    except UnknownObjectTypeError:
        # Every object type the validators name today resolves. One that does not
        # has no revision history to compare, so the waiver cannot be shown to
        # still hold and the error returns.
        return True
    row = db.get(model, result.object_id)
    if not isinstance(row, EngineeringObjectMixin):
        return True
    return row.updated_in_revision > waived_revision


def _result_identity(result: ValidationResult) -> _ResultIdentity:
    return (
        result.code,
        result.object_type.value if result.object_type is not None else "",
        result.object_id,
        result.object_label,
    )


def _issue_identity(issue: Issue) -> _ResultIdentity:
    return (
        issue.code,
        issue.object_type.value if issue.object_type is not None else "",
        issue.object_id,
        issue.object_label[:MAX_OBJECT_LABEL_CHARS],
    )


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
