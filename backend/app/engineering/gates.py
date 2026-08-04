"""The six configurable approval gates a project passes through.

A gate is the place a human takes responsibility for a body of engineering work.
Everything here exists to make that act meaningful rather than ceremonial, and
four rules do most of the work:

* **A gate that is not ready says what is missing.** Every gate is defined as an
  ordered set of named criteria — small predicates over the canonical model —
  and a criterion that fails returns the sentence explaining why. A gate that
  reports "not ready" without naming the gap is unusable by a UI and by the
  engineer who has to close it, and that is the single most common way a gate
  feature fails in practice.
* **A decision is recorded against a revision.** ``decided_revision_number`` is
  what stops a later merge inheriting an approval nobody gave. When the project
  moves past the decision *and* the operations merged in between touched an
  object type the gate covers, the gate re-opens. Silent inheritance is how a
  sign-off ends up describing a model that no longer exists.
* **A gate that was not applied is recorded, never skipped quietly.** An
  administrator may disable a gate, but only with a stated reason, and
  :attr:`~app.models.revision.ApprovalGate.disabled_reason` travels into the
  release record. "Nobody reviewed the tag model" has to stay visible.
* **Import verification cannot pass by default.** Gate 5 is unreachable without
  a licensed Rockwell workstation, which is off by default and which most
  deployments will never have. The honest outcome is ``SKIPPED`` with the reason
  stated — never ``PASSED`` — and :func:`record_decision` refuses an approval
  that has no verification evidence behind it. A product like this lies to
  itself here first, so the refusal is enforced rather than documented.

**Models propose; humans decide.** An agent may call :func:`request_decision`,
which raises a pending :class:`~app.models.approval.Approval`. Only
:func:`record_decision` settles one, and it demands an authenticated user
carrying the reviewer role on the project. There is no path through this module
by which an agent settles a gate.

``ACCEPT_RISK`` is not ``APPROVE``. Both leave the gate ``PASSED``, because both
let the work proceed, but the approval row keeps them apart all the way through
(:attr:`Approval.decision` and
:attr:`~app.core.enums.ApprovalStatus.RISK_ACCEPTED`) and :func:`decision_of`
gives the release record a direct way to ask. A release that proceeded on
knowingly carried risk must never be indistinguishable afterwards from one with
no risk at all.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from functools import cached_property
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.clock import utcnow
from app.core.enums import (
    DECISIONS_REQUIRING_COMMENT,
    GATE_ORDER,
    SEVERITY_ORDER,
    ApprovalDecision,
    ApprovalGateKey,
    ApprovalKind,
    ApprovalStatus,
    AuditAction,
    BaseDataType,
    ControllerFamily,
    EventType,
    FindingSeverity,
    FindingStatus,
    GateStatus,
    HmiProduct,
    ImportVerificationStatus,
    ObjectType,
    RecommendationStatus,
    ReleaseMode,
    RequirementStatus,
    RoleName,
    RoutineLanguage,
    SemanticLocation,
    SignalType,
    ValidationDiscipline,
    ValidationLevel,
    ValidationRunStatus,
    ValidationSeverity,
)
from app.core.errors import ConflictError, PermissionDeniedError, ValidationError
from app.core.logging import get_logger
from app.models.approval import Approval
from app.models.finding import Finding, Recommendation
from app.models.profile import TemplateSet
from app.models.project import Project
from app.models.release import (
    ExportArtifact,
    ReleaseCandidate,
    ReleaseManifest,
    ValidationResult,
    ValidationRun,
    WorkstationVerificationJob,
)
from app.models.revision import ApprovalGate, ChangeOperation, ChangeSet
from app.models.user import User
from app.orchestration.events import record_event
from app.security import audit, permissions
from app.validation import ALL_DISCIPLINES
from app.validation.base import ValidationContext

logger = get_logger(__name__)

__all__ = [
    "GATE_SPECS",
    "Criterion",
    "GateContext",
    "GateSpec",
    "InvalidGateStateError",
    "decision_of",
    "ensure_gates",
    "evaluate_all",
    "evaluate_gate",
    "gate_blocking",
    "record_decision",
    "request_decision",
    "set_gate_enabled",
    "spec_for",
]


#: Longest detail sentence stored against a criterion. Bounded because the whole
#: criteria set is carried in an event payload and in the release record, and a
#: gate that names four hundred unwired devices individually is a gate nobody
#: reads.
MAX_DETAIL_CHARS = 400

#: How many offending objects a detail names before it summarises the rest. Five
#: is enough to recognise the pattern and start work; the full list is the
#: validation run's job.
_NAMED_EXAMPLES = 5

#: Gate statuses that mean the gate is finished with, whether or not it passed
#: on its merits. A later gate may proceed past any of these.
_SETTLED_STATUSES: frozenset[GateStatus] = frozenset({GateStatus.PASSED, GateStatus.SKIPPED})

#: Statuses that carry a recorded human decision about content, and so are the
#: ones a later change can invalidate. ``SKIPPED`` is absent on purpose: a gate
#: that was never applied made no statement about the model, so there is nothing
#: for a merge to make stale.
_DECIDED_STATUSES: frozenset[GateStatus] = frozenset({GateStatus.PASSED, GateStatus.FAILED})

#: Decisions that settle a gate outright. ``REQUEST_CHANGES`` and ``DEFER`` are
#: absent because neither is an answer: the work goes back to the engineers and
#: the gate returns to the queue.
_SETTLING_DECISIONS: frozenset[ApprovalDecision] = frozenset(
    {ApprovalDecision.APPROVE, ApprovalDecision.ACCEPT_RISK, ApprovalDecision.REJECT}
)

#: What each decision does to the approval and to the gate. Kept as one table so
#: the two can never drift apart — an approval recorded as ``RISK_ACCEPTED``
#: against a gate marked ``PASSED`` by a different branch would be exactly the
#: sort of quiet disagreement the release record cannot survive.
_OUTCOMES: dict[ApprovalDecision, tuple[ApprovalStatus, GateStatus]] = {
    ApprovalDecision.APPROVE: (ApprovalStatus.APPROVED, GateStatus.PASSED),
    # Risk accepted still lets the work proceed, so the gate passes. What was
    # accepted stays on the approval row, which is what the release record reads.
    ApprovalDecision.ACCEPT_RISK: (ApprovalStatus.RISK_ACCEPTED, GateStatus.PASSED),
    ApprovalDecision.REJECT: (ApprovalStatus.REJECTED, GateStatus.FAILED),
    ApprovalDecision.REQUEST_CHANGES: (ApprovalStatus.CHANGES_REQUESTED, GateStatus.READY),
    ApprovalDecision.DEFER: (ApprovalStatus.DEFERRED, GateStatus.READY),
}

#: Finding severities that oblige the reviewer to write down what they decided
#: and why, wherever a gate covers findings.
_SERIOUS_FINDINGS: frozenset[FindingSeverity] = frozenset(
    {FindingSeverity.CRITICAL, FindingSeverity.HIGH}
)

#: Finding statuses that mean a human has ruled on it. ``OPEN`` is the only one
#: that has not been.
_DECIDED_FINDING_STATUSES: frozenset[FindingStatus] = frozenset(
    {FindingStatus.ACCEPTED, FindingStatus.REJECTED, FindingStatus.RESOLVED}
)

#: Every object type in the canonical model, so the release gate can say "any
#: change at all re-opens this" without overloading an empty set to mean it.
_ALL_OBJECT_TYPES: frozenset[ObjectType] = frozenset(ObjectType)


class InvalidGateStateError(ConflictError):
    """The gate is not in a state this operation can act on.

    Carries the unmet criteria in ``details``. A caller told only "not ready" has
    to go and ask a second question to find out why, and the answer is already
    sitting on the row.
    """

    code = "invalid_gate_state"

    def __init__(self, gate: ApprovalGate, requested: GateStatus) -> None:
        unmet = [
            str(item.get("detail") or item.get("statement", "")) for item in gate.unmet_criteria
        ]
        super().__init__(
            f"'{gate.title}' is {gate.status.value} and cannot move to {requested.value}.",
            details={
                "gate_key": gate.gate_key.value,
                "status": gate.status.value,
                "requested": requested.value,
                "unmet_criteria": unmet[:20],
            },
        )


# ---------------------------------------------------------------------------
# Criteria and specifications
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Criterion:
    """One named condition a gate depends on.

    ``check`` returns the empty string when the condition holds and, when it does
    not, the sentence explaining what is missing. One callable rather than a
    predicate plus a message builder, because the code that discovers *which*
    three devices are unwired is the only code in a position to say so, and
    splitting the two invites a detail that no longer matches the check.
    """

    key: str
    #: What must hold, phrased as a statement an engineer would recognise. Static
    #: text: it describes the rule, not this project's state.
    statement: str
    check: Callable[[GateContext], str]


@dataclass(frozen=True, slots=True)
class GateSpec:
    """The static definition of one gate. Never per-project, never in a database.

    The row in ``approval_gates`` holds a project's *state* against this
    definition; the definition itself ships with the code so that "what was this
    gate asking" is answerable from a released build rather than from whatever a
    row happened to say at the time.
    """

    key: ApprovalGateKey
    title: str
    description: str
    criteria: tuple[Criterion, ...]

    #: Object types whose change after a decision re-opens this gate. Not a
    #: convenience: it is what makes an approval a statement about specific
    #: content rather than about a moment in time.
    covers: frozenset[ObjectType]

    #: Set when the gate rules on findings. Drives the mandatory-comment rule:
    #: a reviewer settling a gate over critical or high findings has to write
    #: down what they decided about them.
    covers_findings: bool = False

    #: Set when a decision on this gate always needs a comment, whatever the
    #: verdict. True for release, where the covering sentence is part of the
    #: record handed to the customer.
    always_requires_comment: bool = False

    #: Returns a reason when this gate cannot be reached at all in this project's
    #: configuration, in which case the honest status is ``SKIPPED``. Gate 5 is
    #: the case this exists for: without a licensed workstation, import
    #: verification is not a thing that can be done, and reporting anything other
    #: than "we did not do this, here is why" would be a lie the whole product is
    #: built to avoid.
    unreachable: Callable[[GateContext], str] | None = None

    @property
    def ordinal(self) -> int:
        return GATE_ORDER.index(self.key)


# ---------------------------------------------------------------------------
# The project as the gates read it
# ---------------------------------------------------------------------------
@dataclass
class GateContext:
    """Everything the criteria read, loaded once per evaluation.

    Wraps :class:`~app.validation.base.ValidationContext` rather than
    re-implementing it. The validators already load the canonical model exactly
    the way a gate needs to see it — undeleted rows, in creation order, cached —
    and a second loader would drift from the first the first time a soft-delete
    rule changed.

    Read-only, like the validation context it wraps. A criterion that mutates a
    loaded row is a bug: a gate is a statement *about* a revision, and one that
    edited what it was measuring would be describing a model that never existed.
    """

    db: Session
    project: Project
    #: Gate rows by key, injected by the evaluator so that a criterion asking
    #: about a sibling gate does not re-query for it.
    gates: dict[ApprovalGateKey, ApprovalGate] = field(default_factory=dict)

    @cached_property
    def revision_number(self) -> int:
        return int(self.project.current_revision_number)

    @cached_property
    def model(self) -> ValidationContext:
        return ValidationContext(
            db=self.db, project=self.project, revision_number=self.revision_number
        )

    # -- findings and recommendations -------------------------------------
    @cached_property
    def findings(self) -> list[Finding]:
        return list(
            self.db.execute(
                select(Finding)
                .where(Finding.project_id == self.project.id)
                .order_by(Finding.created_at, Finding.id)
            ).scalars()
        )

    @cached_property
    def recommendations(self) -> list[Recommendation]:
        return list(
            self.db.execute(
                select(Recommendation)
                .where(Recommendation.project_id == self.project.id)
                .order_by(Recommendation.created_at, Recommendation.id)
            ).scalars()
        )

    @cached_property
    def serious_findings(self) -> list[Finding]:
        return [finding for finding in self.findings if finding.severity in _SERIOUS_FINDINGS]

    # -- validation --------------------------------------------------------
    @cached_property
    def validation_run(self) -> ValidationRun | None:
        """The newest completed run. Runs that failed or are still going say nothing."""
        return self.db.execute(
            select(ValidationRun)
            .where(
                ValidationRun.project_id == self.project.id,
                ValidationRun.status == ValidationRunStatus.COMPLETED,
                ValidationRun.deleted_at.is_(None),
            )
            .order_by(ValidationRun.revision_number.desc(), ValidationRun.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()

    @cached_property
    def validation_results(self) -> list[ValidationResult]:
        run = self.validation_run
        if run is None:
            return []
        return list(
            self.db.execute(
                select(ValidationResult).where(ValidationResult.validation_run_id == run.id)
            ).scalars()
        )

    def run_covers(self, disciplines: Sequence[ValidationDiscipline]) -> bool:
        """Whether the newest run examined all of ``disciplines`` at the head revision.

        Both halves matter. A run over three disciplines must never read as a
        clean result over all of them, and a clean run against an older revision
        says nothing about the one being approved.
        """
        run = self.validation_run
        if run is None or run.revision_number < self.revision_number:
            return False
        covered = set(run.disciplines)
        return all(discipline.value in covered for discipline in disciplines)

    def open_errors(self, disciplines: Sequence[ValidationDiscipline]) -> list[ValidationResult]:
        """Unwaived errors in the newest run, restricted to ``disciplines``.

        Waived results are excluded because a waiver is a named human's decision
        to carry the issue; it stays visible on the run, but it is not a reason to
        hold a gate a second time.
        """
        wanted = set(disciplines)
        return [
            result
            for result in self.validation_results
            if result.severity is ValidationSeverity.ERROR
            and result.discipline in wanted
            and not result.is_waived
        ]

    # -- release -----------------------------------------------------------
    @cached_property
    def release_candidate(self) -> ReleaseCandidate | None:
        """The newest candidate, by candidate number rather than by timestamp."""
        return self.db.execute(
            select(ReleaseCandidate)
            .where(
                ReleaseCandidate.project_id == self.project.id,
                ReleaseCandidate.deleted_at.is_(None),
            )
            .order_by(ReleaseCandidate.candidate_number.desc())
            .limit(1)
        ).scalar_one_or_none()

    @cached_property
    def manifest(self) -> ReleaseManifest | None:
        candidate = self.release_candidate
        if candidate is None:
            return None
        return self.db.execute(
            select(ReleaseManifest).where(ReleaseManifest.release_candidate_id == candidate.id)
        ).scalar_one_or_none()

    @cached_property
    def artifacts(self) -> list[ExportArtifact]:
        """Queried rather than read off ``candidate.artifacts``.

        The relationship is loaded once per instance and does not notice a row
        added afterwards in the same session, which is exactly the shape of a
        gate evaluated straight after a generation run wrote the package.
        """
        candidate = self.release_candidate
        if candidate is None:
            return []
        return list(
            self.db.execute(
                select(ExportArtifact)
                .where(
                    ExportArtifact.release_candidate_id == candidate.id,
                    ExportArtifact.deleted_at.is_(None),
                )
                .order_by(ExportArtifact.import_order, ExportArtifact.relative_path)
            ).scalars()
        )

    @cached_property
    def verification_jobs(self) -> list[WorkstationVerificationJob]:
        candidate = self.release_candidate
        if candidate is None:
            return []
        return list(
            self.db.execute(
                select(WorkstationVerificationJob)
                .where(WorkstationVerificationJob.release_candidate_id == candidate.id)
                .order_by(WorkstationVerificationJob.created_at)
            ).scalars()
        )

    @cached_property
    def import_was_verified(self) -> bool:
        """Whether a licensed workstation actually imported this package cleanly.

        Read from the evidence — a passed verification job and a candidate that
        reached :attr:`~app.core.enums.ValidationLevel.ROCKWELL_IMPORT_VERIFIED` —
        rather than from a configuration flag. An adapter that is switched on but
        has never run proves nothing, and this is the one place where believing a
        setting instead of a result would let the product overclaim.
        """
        candidate = self.release_candidate
        if candidate is None:
            return False
        if candidate.import_verification_status is not ImportVerificationStatus.PASSED:
            return False
        return any(job.status is ImportVerificationStatus.PASSED for job in self.verification_jobs)

    # -- history -----------------------------------------------------------
    @cached_property
    def merged_object_types(self) -> tuple[tuple[int, ObjectType], ...]:
        """``(revision, object_type)`` for every operation that actually landed.

        The merge module's premise made queryable: the operations merged between
        two revisions *are* the difference between them, so "did anything this
        gate covers change since the sign-off" is a filter over this rather than
        a comparison of two reconstructed model states.
        """
        rows = self.db.execute(
            select(ChangeSet.merged_revision_number, ChangeOperation.object_type)
            .join(ChangeOperation, ChangeOperation.change_set_id == ChangeSet.id)
            .where(
                ChangeSet.project_id == self.project.id,
                ChangeSet.merged_revision_number.is_not(None),
                ChangeOperation.applied.is_(True),
            )
            .distinct()
        ).all()
        return tuple((int(revision), object_type) for revision, object_type in rows)

    def changed_since(self, revision: int, covers: frozenset[ObjectType]) -> frozenset[ObjectType]:
        """Object types this gate covers that were touched after ``revision``."""
        return frozenset(
            object_type
            for merged_at, object_type in self.merged_object_types
            if merged_at > revision and object_type in covers
        )

    @cached_property
    def gate_approvals(self) -> list[Approval]:
        return list(
            self.db.execute(
                select(Approval)
                .where(
                    Approval.project_id == self.project.id,
                    Approval.kind == ApprovalKind.GATE_DECISION,
                )
                .order_by(Approval.created_at)
            ).scalars()
        )


# ---------------------------------------------------------------------------
# Criteria — gate 1, project profile and architecture
# ---------------------------------------------------------------------------
def _profile_recorded(context: GateContext) -> str:
    if context.model.profile is None:
        return (
            "No project profile has been recorded. Until one exists there is no statement "
            "of what this project is, and nothing downstream has a standard to work to."
        )
    return ""


def _profile_fields_complete(context: GateContext) -> str:
    profile = context.model.profile
    if profile is None:
        return "No project profile has been recorded."
    unusable = profile.unusable_required_fields
    if not unusable:
        return ""
    described = _naming([f"{name} ({status.value})" for name, status in sorted(unusable.items())])
    return f"These required profile fields are not usable yet: {described}."


def _target_platform_complete(context: GateContext) -> str:
    platform = context.model.target_platform
    if platform is None:
        return (
            "No target platform has been recorded. An L5X is only correct relative to a "
            "controller, a firmware revision and a Studio 5000 version."
        )
    missing = [
        name
        for name, value in (
            ("controller family", platform.controller_family is not ControllerFamily.OTHER),
            ("controller catalog number", bool(platform.controller_catalog_number)),
            ("firmware revision", bool(platform.firmware_revision)),
            ("Studio 5000 version", bool(platform.studio_5000_version)),
        )
        if not value
    ]
    if not missing:
        return ""
    return f"The target platform does not state its {_naming(missing)}."


def _hmi_target_stated(context: GateContext) -> str:
    platform = context.model.target_platform
    if platform is None:
        return "No target platform has been recorded, so the HMI scope is unstated."
    if platform.hmi_product is HmiProduct.NONE:
        # NONE fails closed: it generates nothing. Treated as a settled answer
        # here because a project with no HMI is a real project, and demanding a
        # product name for one would be demanding a fabricated decision.
        return ""
    if not platform.hmi_version:
        return (
            f"The HMI product is {platform.hmi_product.value} but no version is recorded, "
            "and a display is only valid against a version."
        )
    return ""


def _template_set_covers_controller(context: GateContext) -> str:
    profile = context.model.profile
    platform = context.model.target_platform
    if profile is None or platform is None:
        return "The profile and target platform must both exist before templates can be checked."
    if profile.template_set_id is None:
        return "No template set is pinned, so there is no approved pattern to expand."
    template_set = context.db.get(TemplateSet, profile.template_set_id)
    if template_set is None:
        return "The pinned template set no longer exists in this project."
    supported = set(template_set.supported_controller_families)
    if not supported:
        return (
            f"Template set '{template_set.key}' does not state which controller families it "
            "supports, so nothing can confirm it applies to this controller."
        )
    if platform.controller_family.value not in supported:
        return (
            f"Template set '{template_set.key}' supports "
            f"{_naming(sorted(str(family) for family in supported))}, not "
            f"{platform.controller_family.value}."
        )
    return ""


def _generation_scope_settled(context: GateContext) -> str:
    profile = context.model.profile
    if profile is None:
        return "No project profile has been recorded."
    if profile.release_mode is ReleaseMode.REVIEW_ONLY:
        # An explicit decision to read and report. Settled, not blocked.
        return ""
    if not profile.blocks_generation:
        return ""
    if not profile.is_complete_for_generation:
        return (
            "The profile is incomplete, so anything generated would be governed by a guess "
            "rather than by a standard somebody chose."
        )
    return (
        f"This project touches {_naming(sorted(str(item) for item in profile.restricted_domains))} "
        f"and its restricted-domain mode is '{profile.restricted_domain_mode.value}'. Generating "
        "here needs an administrator to enable a qualified workflow, or the project scoped to "
        "review only."
    )


def _requirements_human_decided(context: GateContext) -> str:
    proposed = [
        requirement
        for requirement in context.model.requirements
        if requirement.status is RequirementStatus.PROPOSED
    ]
    if not proposed:
        return ""
    return (
        f"{len(proposed)} extracted requirement(s) still await a human decision: "
        f"{_naming([requirement.reference for requirement in proposed])}. "
        "Accept or reject each one before approving the project profile."
    )


def _accepted_requirement_for_generation(context: GateContext) -> str:
    profile = context.model.profile
    if profile is None or profile.release_mode is ReleaseMode.REVIEW_ONLY:
        return ""
    accepted_states = {
        RequirementStatus.ACCEPTED,
        RequirementStatus.IMPLEMENTED,
        RequirementStatus.VERIFIED,
    }
    if any(requirement.status in accepted_states for requirement in context.model.requirements):
        return ""
    return (
        "Generation requires at least one requirement accepted by a human. None of the "
        "recorded requirements is accepted, implemented, or verified."
    )


def _blocking_questions_answered(context: GateContext) -> str:
    outstanding = [question for question in context.model.questions if question.is_blocking]
    if not outstanding:
        return ""
    return (
        f"{len(outstanding)} mandatory project question(s) are unanswered: "
        f"{_naming([question.reference for question in outstanding])}."
    )


# ---------------------------------------------------------------------------
# Criteria — gate 2, devices, I/O and tags
# ---------------------------------------------------------------------------
def _equipment_model_recorded(context: GateContext) -> str:
    if not context.model.devices:
        return (
            "No devices are recorded, so there is no equipment model to approve. A project "
            "with genuinely no plant should disable this gate with a reason instead."
        )
    return ""


def _every_device_is_wired_or_declared_network_only(context: GateContext) -> str:
    unwired = [
        device
        for device in context.model.devices
        if device.requires_io_points and not context.model.signals_by_device.get(device.id)
    ]
    if not unwired:
        return ""
    return (
        f"{len(unwired)} device(s) have no signals and are not declared network-only: "
        f"{_naming([device.tag_prefix for device in unwired])}. A device that exchanges "
        "everything over the network must say so; otherwise this is unfinished I/O design."
    )


def _tags_recorded(context: GateContext) -> str:
    if not context.model.tags:
        return "No tags are recorded, so there is no vocabulary for logic to reference."
    return ""


def _structured_types_resolve(context: GateContext) -> str:
    """Every UDT- or AOI-typed tag must name a definition that exists here.

    This is the "UDT and AOI approach" the gate signs off, made checkable: a tag
    of type ``ESCO_Motor`` where nothing in the project defines ``ESCO_Motor`` is
    a package that will not import, discovered at the customer's import dialog
    rather than here.
    """
    known = {definition.name.casefold() for definition in context.model.udt_definitions}
    known |= {reference.name.casefold() for reference in context.model.aoi_references}
    unresolved: list[str] = []
    for tag in context.model.tags:
        if tag.base_data_type not in {BaseDataType.UDT, BaseDataType.ADDON_INSTRUCTION}:
            continue
        if not tag.data_type_name:
            unresolved.append(f"{tag.name} (no type name)")
        elif tag.data_type_name.casefold() not in known:
            unresolved.append(f"{tag.name} ({tag.data_type_name})")
    if not unresolved:
        return ""
    return (
        f"{len(unresolved)} tag(s) name a UDT or Add-On Instruction this project does not "
        f"define: {_naming(unresolved)}."
    )


def _tag_and_io_validation_current(context: GateContext) -> str:
    return _validation_currency(context, _TAG_AND_IO_DISCIPLINES)


def _no_open_tag_or_io_errors(context: GateContext) -> str:
    return _error_summary(context, _TAG_AND_IO_DISCIPLINES)


# ---------------------------------------------------------------------------
# Criteria — gate 3, logic and delivery design
# ---------------------------------------------------------------------------
def _controller_organisation_recorded(context: GateContext) -> str:
    if not context.model.programs:
        return "No programs are recorded, so the controller organisation is undecided."
    if not context.model.routines:
        return "No routines are recorded, so no program has any content."
    return ""


def _ladder_routines_hold_logic(context: GateContext) -> str:
    """Only ladder is checked: a structured text routine legitimately has no rungs."""
    rung_counts: dict[uuid.UUID, int] = {}
    for rung in context.model.rungs:
        rung_counts[rung.routine_id] = rung_counts.get(rung.routine_id, 0) + 1
    empty = [
        routine
        for routine in context.model.routines
        if routine.language is RoutineLanguage.LADDER and not rung_counts.get(routine.id)
    ]
    if not empty:
        return ""
    return (
        f"{len(empty)} ladder routine(s) contain no rungs: "
        f"{_naming([routine.name for routine in empty])}."
    )


def _hmi_screens_recorded(context: GateContext) -> str:
    platform = context.model.target_platform
    if platform is None:
        # Normally unreachable — gate 1 requires a platform — but reachable if
        # gate 1 was disabled, and "no screens" would be the wrong complaint.
        return "No target platform is recorded, so whether an HMI is in scope is unstated."
    if platform.hmi_product is HmiProduct.NONE:
        # No HMI in scope; there is nothing to design and nothing to approve.
        return ""
    if not context.model.hmi_screens:
        return (
            f"The target platform names {platform.hmi_product.value} but no screens are "
            "recorded, so there is no display design to approve."
        )
    return ""


def _alarm_model_recorded(context: GateContext) -> str:
    if context.model.alarms:
        return ""
    if not context.model.fault_definitions:
        # No faults defined means nothing to annunciate. Consistent, not empty.
        return ""
    return (
        f"{len(context.model.fault_definitions)} fault(s) are defined but no alarms are, so "
        "nothing tells an operator when one occurs."
    )


def _electrical_mapping_recorded(context: GateContext) -> str:
    """Every real terminal must name the module it lives on.

    A point with an address but no hardware module is an address read out of a
    spreadsheet that nobody has tied to a physical card. It exports as a tag that
    resolves to nothing, and the failure surfaces on the plant floor.
    """
    unmapped = [
        point
        for point in context.model.io_points
        if point.signal_type is not SignalType.VIRTUAL
        and not point.is_spare
        and point.hardware_module_id is None
    ]
    if not unmapped:
        return ""
    return (
        f"{len(unmapped)} I/O point(s) name no hardware module: "
        f"{_naming([point.address for point in unmapped])}."
    )


def _logic_and_delivery_validation_current(context: GateContext) -> str:
    return _validation_currency(context, _LOGIC_AND_DELIVERY_DISCIPLINES)


def _no_open_logic_or_delivery_errors(context: GateContext) -> str:
    return _error_summary(context, _LOGIC_AND_DELIVERY_DISCIPLINES)


# ---------------------------------------------------------------------------
# Criteria — gate 4, QA and risk
# ---------------------------------------------------------------------------
def _full_validation_current(context: GateContext) -> str:
    return _validation_currency(context, ALL_DISCIPLINES)


def _no_unwaived_errors(context: GateContext) -> str:
    return _error_summary(context, ALL_DISCIPLINES)


def _serious_findings_senior_reviewed(context: GateContext) -> str:
    outstanding = [
        finding for finding in context.serious_findings if not finding.is_reviewed
    ]
    if not outstanding:
        return ""
    outstanding.sort(key=lambda finding: -SEVERITY_ORDER[finding.severity])
    return (
        f"{len(outstanding)} critical or high finding(s) have not completed independent "
        f"senior review: "
        f"{_naming([f'{finding.title} ({finding.severity.value})' for finding in outstanding])}."
    )


def _serious_findings_decided(context: GateContext) -> str:
    outstanding = [
        finding
        for finding in context.serious_findings
        if finding.status not in _DECIDED_FINDING_STATUSES
    ]
    if not outstanding:
        return ""
    outstanding.sort(key=lambda finding: -SEVERITY_ORDER[finding.severity])
    return (
        f"{len(outstanding)} critical or high finding(s) have no recorded human disposition: "
        f"{_naming([f'{finding.title} ({finding.severity.value})' for finding in outstanding])}."
    )


def _safety_recommendations_decided(context: GateContext) -> str:
    outstanding = [
        recommendation
        for recommendation in context.recommendations
        if (recommendation.safety_related or recommendation.equipment_damage_related)
        and recommendation.status is RecommendationStatus.PROPOSED
    ]
    if not outstanding:
        return ""
    return (
        f"{len(outstanding)} safety-related or equipment-damage recommendation(s) are still "
        f"proposed: {_naming([item.title for item in outstanding])}. Each needs a named "
        "engineer to approve or reject it."
    )


def _human_review_results_settled(context: GateContext) -> str:
    outstanding = [
        result
        for result in context.validation_results
        if (
            result.requires_human_decision
            or result.severity is ValidationSeverity.HUMAN_REVIEW_REQUIRED
        )
        and not result.is_waived
    ]
    if not outstanding:
        return ""
    return (
        f"{len(outstanding)} validation result(s) need a human decision and have none: "
        f"{_naming([f'{result.code} on {result.object_label}' for result in outstanding])}."
    )


# ---------------------------------------------------------------------------
# Criteria — gate 5, import verification
# ---------------------------------------------------------------------------
def _import_verification_unreachable(context: GateContext) -> str:
    """Why this gate cannot be reached, or ``""`` when it genuinely can.

    Two ways it is out of reach, and both are the normal case rather than an
    error: the project never scoped a workstation import, or no licensed
    workstation ever ran one. Either way the truthful outcome is ``SKIPPED`` with
    this sentence attached — recording that the step did not happen, which a
    ``PASSED`` would erase.
    """
    profile = context.model.profile
    if (
        profile is not None
        and profile.release_mode is not ReleaseMode.ROCKWELL_WORKSTATION_ASSEMBLY
    ):
        return (
            f"This project is scoped to '{profile.release_mode.value}', so no import onto a "
            "licensed Rockwell workstation was ever in scope. The package remains an "
            "import-ready candidate and is labelled as one."
        )
    if not context.verification_jobs:
        return (
            "No workstation verification job has run. Import verification is unreachable "
            "without the optional licensed Rockwell workstation adapter, which is off by "
            "default, so this step did not happen."
        )
    return ""


def _release_candidate_generated(context: GateContext) -> str:
    if context.release_candidate is None:
        return "No release candidate has been generated, so there is nothing to import."
    return ""


def _artifacts_carry_checksums(context: GateContext) -> str:
    candidate = context.release_candidate
    if candidate is None:
        return "No release candidate has been generated."
    if not context.artifacts:
        return f"Release candidate {candidate.candidate_number} contains no artifacts."
    unhashed = [artifact for artifact in context.artifacts if not artifact.sha256]
    if unhashed:
        return (
            f"{len(unhashed)} artifact(s) have no checksum: "
            f"{_naming([artifact.relative_path for artifact in unhashed])}."
        )
    return ""


def _import_verified_on_licensed_workstation(context: GateContext) -> str:
    candidate = context.release_candidate
    if candidate is None:
        return "No release candidate has been generated."
    if not context.import_was_verified:
        detail = candidate.import_verification_detail or "no verification job has passed"
        return (
            f"Import verification status is "
            f"'{candidate.import_verification_status.value}' ({detail}). Nothing this backend "
            "does offline substitutes for an actual Studio 5000 import."
        )
    if candidate.validation_level is not ValidationLevel.ROCKWELL_IMPORT_VERIFIED:
        return (
            f"A verification job passed but the candidate is still recorded at "
            f"'{candidate.validation_level.value}'. The level a package claims must be the "
            "level it reached."
        )
    return ""


def _target_version_verified(context: GateContext) -> str:
    """The package must have been built for the platform currently recorded.

    A candidate generated against firmware 33 and approved after somebody moved
    the project to firmware 35 is a package that will import into the wrong
    controller, and nothing else in the chain compares the two.
    """
    candidate = context.release_candidate
    platform = context.model.target_platform
    if candidate is None or platform is None:
        return "A release candidate and a target platform are both needed to compare versions."
    snapshot = candidate.target_profile_snapshot
    if not snapshot:
        return (
            f"Release candidate {candidate.candidate_number} carries no target profile "
            "snapshot, so what it was generated for cannot be established."
        )
    mismatches = [
        f"{name}: package {built!r}, project {current!r}"
        for name, built, current in (
            (
                "firmware revision",
                str(snapshot.get("firmware_revision", "")),
                platform.firmware_revision,
            ),
            (
                "Studio 5000 version",
                str(snapshot.get("studio_5000_version", "")),
                platform.studio_5000_version,
            ),
            (
                "controller catalog number",
                str(snapshot.get("controller_catalog_number", "")),
                platform.controller_catalog_number,
            ),
        )
        if built != current
    ]
    if mismatches:
        return f"The package was not generated for the recorded platform — {_naming(mismatches)}."
    return ""


# ---------------------------------------------------------------------------
# Criteria — gate 6, release
# ---------------------------------------------------------------------------
def _release_candidate_current(context: GateContext) -> str:
    candidate = context.release_candidate
    if candidate is None:
        return "No release candidate has been generated."
    if candidate.revision_number != context.revision_number:
        return (
            f"Release candidate {candidate.candidate_number} was generated from revision "
            f"{candidate.revision_number}, but the project is at revision "
            f"{context.revision_number}. Releasing it would ship logic nobody reviewed."
        )
    return ""


def _manifest_frozen_with_hash(context: GateContext) -> str:
    manifest = context.manifest
    if manifest is None:
        return "The release candidate has no manifest, so there is no record of what is in it."
    missing = [
        name
        for name, value in (
            ("a content hash", manifest.content_sha256),
            ("a checksum-file hash", manifest.checksum_file_sha256),
        )
        if not value
    ]
    if missing:
        return f"The manifest has no {_naming(missing)}, so the package cannot be verified."
    if manifest.frozen_at is None:
        return "The manifest has not been frozen, so what it describes can still change."
    return ""


def _every_decision_names_a_person(context: GateContext) -> str:
    """No gate on this project may have been settled by something without a name.

    The approval row is the record handed to the customer. An approval that
    resolves to no user is an approval nobody can be asked about.
    """
    unattributed = [
        approval
        for approval in context.gate_approvals
        if approval.status is not ApprovalStatus.PENDING and approval.decided_by_id is None
    ]
    if not unattributed:
        return ""
    return (
        f"{len(unattributed)} gate decision(s) name no decider: "
        f"{_naming([approval.title for approval in unattributed])}."
    )


def _skipped_gates_state_a_reason(context: GateContext) -> str:
    silent = [
        gate
        for key, gate in context.gates.items()
        if key is not ApprovalGateKey.RELEASE
        and not gate.is_enabled
        and not gate.disabled_reason.strip()
    ]
    if not silent:
        return ""
    return (
        f"{len(silent)} gate(s) are disabled without a stated reason: "
        f"{_naming([gate.title for gate in silent])}. A gate nobody applied has to say why "
        "in the release record."
    )


def _no_errors_at_release(context: GateContext) -> str:
    return _error_summary(context, ALL_DISCIPLINES)


# ---------------------------------------------------------------------------
# Shared criterion helpers
# ---------------------------------------------------------------------------
_TAG_AND_IO_DISCIPLINES: tuple[ValidationDiscipline, ...] = (
    ValidationDiscipline.TAGS,
    ValidationDiscipline.IO,
)

#: Interlocks, HMI and alarms are checked, but not under those discipline names:
#: the interlock and mode checks are registered under ``plc_logic`` and the HMI,
#: alarm and test checks under ``cross_discipline``. A gate must ask for the
#: discipline a validator actually reports against — naming
#: :attr:`~app.core.enums.ValidationDiscipline.HMI` here would demand coverage
#: nothing in the registry can give, and the gate would never pass for a reason
#: no engineer could act on. ``test_every_discipline_a_gate_asks_for_is_one_a
#: _validator_covers`` is the guard against that drifting back.
_LOGIC_AND_DELIVERY_DISCIPLINES: tuple[ValidationDiscipline, ...] = (
    ValidationDiscipline.PLC_LOGIC,
    ValidationDiscipline.CROSS_DISCIPLINE,
)


def _validation_currency(context: GateContext, disciplines: Sequence[ValidationDiscipline]) -> str:
    """Whether anybody has actually checked, at the revision being approved.

    Kept apart from the error count on purpose. "We did not check" and "we
    checked and it was clean" produce the same number of errors, and conflating
    them is how a gate ends up approving an unexamined revision.
    """
    run = context.validation_run
    if run is None:
        return "No validation run has completed for this project."
    if run.revision_number < context.revision_number:
        return (
            f"The newest validation run examined revision {run.revision_number}; the project "
            f"is at revision {context.revision_number}."
        )
    uncovered = [
        discipline.value
        for discipline in disciplines
        if discipline.value not in set(run.disciplines)
    ]
    if uncovered:
        return f"The newest validation run did not cover {_naming(uncovered)}."
    return ""


def _error_summary(context: GateContext, disciplines: Sequence[ValidationDiscipline]) -> str:
    errors = context.open_errors(disciplines)
    if not errors:
        return ""
    return (
        f"{len(errors)} unwaived error(s) are outstanding: "
        f"{_naming([f'{result.code} on {result.object_label}' for result in errors])}."
    )


def _naming(items: Sequence[str], limit: int = _NAMED_EXAMPLES) -> str:
    """Name the first few, then count the rest. A detail nobody reads helps nobody."""
    shown = list(items[:limit])
    if len(items) > limit:
        shown.append(f"and {len(items) - limit} more")
    return ", ".join(shown) if shown else "nothing"


# ---------------------------------------------------------------------------
# The six gates
# ---------------------------------------------------------------------------
_PROFILE_OBJECTS: frozenset[ObjectType] = frozenset(
    {
        ObjectType.PROJECT_PROFILE,
        ObjectType.PROJECT_QUESTION,
        ObjectType.PROJECT_DECISION,
        ObjectType.REQUIREMENT,
        ObjectType.ACCEPTANCE_CRITERION,
        ObjectType.ASSUMPTION,
        ObjectType.CONSTRAINT,
        ObjectType.CLARIFICATION,
    }
)

_DEVICE_AND_TAG_OBJECTS: frozenset[ObjectType] = frozenset(
    {
        ObjectType.AREA,
        ObjectType.UNIT,
        ObjectType.EQUIPMENT_MODULE,
        ObjectType.DEVICE,
        ObjectType.DEVICE_TYPE,
        ObjectType.NETWORK_NODE,
        ObjectType.MODULE,
        ObjectType.IO_POINT,
        ObjectType.SIGNAL,
        ObjectType.ADDRESS_ASSIGNMENT,
        ObjectType.TAG,
        ObjectType.TAG_ALIAS,
        ObjectType.TAG_GROUP,
        ObjectType.DATA_TYPE_DEFINITION,
        ObjectType.UDT_DEFINITION,
        ObjectType.AOI_REFERENCE,
    }
)

_LOGIC_AND_DELIVERY_OBJECTS: frozenset[ObjectType] = frozenset(
    {
        ObjectType.CONTROLLER_TASK,
        ObjectType.PROGRAM,
        ObjectType.ROUTINE,
        ObjectType.LOGIC_NETWORK,
        ObjectType.LOGIC_RUNG,
        ObjectType.LOGIC_INSTRUCTION,
        ObjectType.INTERLOCK,
        ObjectType.PERMISSIVE,
        ObjectType.CAUSE_EFFECT_ENTRY,
        ObjectType.MODE_DEFINITION,
        ObjectType.STATE_DEFINITION,
        ObjectType.TRANSITION_DEFINITION,
        ObjectType.SEQUENCE_STEP,
        ObjectType.FAULT_DEFINITION,
        ObjectType.RESET_BEHAVIOR,
        ObjectType.HMI_SCREEN,
        ObjectType.HMI_BINDING,
        ObjectType.ALARM_DEFINITION,
        ObjectType.TEST_CASE,
    }
)

#: The static definitions, in :data:`~app.core.enums.GATE_ORDER`.
GATE_SPECS: tuple[GateSpec, ...] = (
    GateSpec(
        key=ApprovalGateKey.PROJECT_PROFILE,
        title="Project profile and architecture",
        description=(
            "The target controller and HMI, the template and standard sets the project is "
            "pinned to, how the project is organised, and which restricted domains it touches. "
            "Everything downstream is generated under the standards approved here, so an "
            "unanswered question at this gate becomes a guess in every tag name after it."
        ),
        criteria=(
            Criterion(
                "profile_recorded",
                "A project profile has been recorded.",
                _profile_recorded,
            ),
            Criterion(
                "profile_fields_complete",
                "Every required profile field is recorded and usable.",
                _profile_fields_complete,
            ),
            Criterion(
                "target_platform_complete",
                "The controller family, catalog number, firmware revision and Studio 5000 "
                "version are recorded.",
                _target_platform_complete,
            ),
            Criterion(
                "hmi_target_stated",
                "The HMI product and version are recorded, or the project states that no HMI "
                "is in scope.",
                _hmi_target_stated,
            ),
            Criterion(
                "template_set_covers_controller",
                "The pinned template set supports the target controller family.",
                _template_set_covers_controller,
            ),
            Criterion(
                "generation_scope_settled",
                "The project is cleared to generate, or is explicitly scoped to review only.",
                _generation_scope_settled,
            ),
            Criterion(
                "requirements_human_decided",
                "Every extracted requirement has been accepted or rejected by a human.",
                _requirements_human_decided,
            ),
            Criterion(
                "accepted_requirement_for_generation",
                "A generation project has at least one human-accepted requirement.",
                _accepted_requirement_for_generation,
            ),
            Criterion(
                "blocking_questions_answered",
                "Every mandatory project question has been answered.",
                _blocking_questions_answered,
            ),
        ),
        covers=_PROFILE_OBJECTS,
    ),
    GateSpec(
        key=ApprovalGateKey.DEVICES_IO_TAGS,
        title="Devices, I/O and tags",
        description=(
            "The equipment model, the I/O assignments, and the tag names, types and scopes — "
            "including how UDTs and Add-On Instructions are used. Approving this fixes the "
            "vocabulary every rung, alarm, display and test step will reference, and renaming "
            "an approved tag afterwards is a separate, impact-analysed act."
        ),
        criteria=(
            Criterion(
                "equipment_model_recorded",
                "At least one device is recorded.",
                _equipment_model_recorded,
            ),
            Criterion(
                "devices_wired_or_network_only",
                "Every device has signals, or is declared network-only.",
                _every_device_is_wired_or_declared_network_only,
            ),
            Criterion(
                "tags_recorded",
                "At least one tag is recorded.",
                _tags_recorded,
            ),
            Criterion(
                "structured_types_resolve",
                "Every tag typed as a UDT or Add-On Instruction names a definition this "
                "project holds.",
                _structured_types_resolve,
            ),
            Criterion(
                "tag_and_io_validation_current",
                "A validation run covering tags and I/O has completed at the current revision.",
                _tag_and_io_validation_current,
            ),
            Criterion(
                "no_open_tag_or_io_errors",
                "No unwaived tag or I/O error is outstanding.",
                _no_open_tag_or_io_errors,
            ),
        ),
        covers=_DEVICE_AND_TAG_OBJECTS,
    ),
    GateSpec(
        key=ApprovalGateKey.LOGIC_AND_DELIVERY,
        title="Logic and delivery design",
        description=(
            "Programs and routines, the ladder logic itself, the HMI screens, the alarm model "
            "and the electrical mapping. This is the gate where an engineer confirms that what "
            "the platform generated is what the requirements asked for."
        ),
        criteria=(
            Criterion(
                "controller_organisation_recorded",
                "At least one program exists, with at least one routine.",
                _controller_organisation_recorded,
            ),
            Criterion(
                "ladder_routines_hold_logic",
                "Every ladder routine contains at least one rung.",
                _ladder_routines_hold_logic,
            ),
            Criterion(
                "hmi_screens_recorded",
                "HMI screens exist, or the target platform states that no HMI is in scope.",
                _hmi_screens_recorded,
            ),
            Criterion(
                "alarm_model_recorded",
                "An alarm model exists, or the project defines no faults to annunciate.",
                _alarm_model_recorded,
            ),
            Criterion(
                "electrical_mapping_recorded",
                "Every non-spare, non-virtual I/O point names the hardware module it lives on.",
                _electrical_mapping_recorded,
            ),
            Criterion(
                "logic_and_delivery_validation_current",
                "A validation run covering logic and the cross-discipline checks — interlocks, "
                "HMI bindings, alarm coverage and test coverage — has completed at the current "
                "revision.",
                _logic_and_delivery_validation_current,
            ),
            Criterion(
                "no_open_logic_or_delivery_errors",
                "No unwaived logic or cross-discipline error is outstanding.",
                _no_open_logic_or_delivery_errors,
            ),
        ),
        covers=_LOGIC_AND_DELIVERY_OBJECTS,
    ),
    GateSpec(
        key=ApprovalGateKey.QA_AND_RISK,
        title="QA and risk",
        description=(
            "The critical and high findings, everything the platform could not check, and every "
            "safety or equipment-damage recommendation. Nothing here is closed by a validator: "
            "this gate exists because a named engineer has to carry what remains."
        ),
        criteria=(
            Criterion(
                "full_validation_current",
                "A validation run covering every discipline has completed at the current revision.",
                _full_validation_current,
            ),
            Criterion(
                "no_unwaived_errors",
                "No unwaived error-severity result is outstanding.",
                _no_unwaived_errors,
            ),
            Criterion(
                "serious_findings_senior_reviewed",
                "Every critical and high finding has completed independent senior review.",
                _serious_findings_senior_reviewed,
            ),
            Criterion(
                "serious_findings_decided",
                "Every critical and high finding has a recorded human disposition.",
                _serious_findings_decided,
            ),
            Criterion(
                "safety_recommendations_decided",
                "Every safety-related or equipment-damage recommendation has been approved or "
                "rejected by a named engineer.",
                _safety_recommendations_decided,
            ),
            Criterion(
                "human_review_results_settled",
                "Every validation result needing a human decision has been waived with a "
                "stated reason.",
                _human_review_results_settled,
            ),
        ),
        # QA rules on findings raised against tags, I/O and logic alike, so a
        # merge touching any of them makes the sign-off stale.
        covers=_PROFILE_OBJECTS | _DEVICE_AND_TAG_OBJECTS | _LOGIC_AND_DELIVERY_OBJECTS,
        covers_findings=True,
    ),
    GateSpec(
        key=ApprovalGateKey.IMPORT_VERIFICATION,
        title="Import verification",
        description=(
            "The generated artifacts, the diagnostics an actual Studio 5000 import reported, "
            "and confirmation that the package was built for the controller version currently "
            "recorded. Unreachable without a licensed Rockwell workstation, and skipped with a "
            "stated reason rather than passed when there is none."
        ),
        criteria=(
            Criterion(
                "release_candidate_generated",
                "A release candidate has been generated.",
                _release_candidate_generated,
            ),
            Criterion(
                "artifacts_carry_checksums",
                "The candidate contains artifacts, each with a recorded checksum.",
                _artifacts_carry_checksums,
            ),
            Criterion(
                "import_verified_on_licensed_workstation",
                "A licensed Rockwell workstation imported the package and reported no errors.",
                _import_verified_on_licensed_workstation,
            ),
            Criterion(
                "target_version_verified",
                "The package was generated for the controller catalog number, firmware "
                "revision and Studio 5000 version currently recorded.",
                _target_version_verified,
            ),
        ),
        covers=frozenset({ObjectType.EXPORT_ARTIFACT, ObjectType.RELEASE_CANDIDATE}),
        unreachable=_import_verification_unreachable,
    ),
    GateSpec(
        key=ApprovalGateKey.RELEASE,
        title="Release",
        description=(
            "The final sign-off: a named responsible engineer, a covering comment, the revision "
            "being released, and an immutable manifest with its hash. Any merge at all after "
            "this decision re-opens it, because a package with a checksum has to keep meaning "
            "what it meant."
        ),
        criteria=(
            Criterion(
                "release_candidate_current",
                "The release candidate was generated from the project's current revision.",
                _release_candidate_current,
            ),
            Criterion(
                "manifest_frozen_with_hash",
                "The candidate has a frozen manifest carrying a content hash and a "
                "checksum-file hash.",
                _manifest_frozen_with_hash,
            ),
            Criterion(
                "every_decision_names_a_person",
                "Every settled gate decision on this project names the human who made it.",
                _every_decision_names_a_person,
            ),
            Criterion(
                "skipped_gates_state_a_reason",
                "Every disabled gate states why it was not applied.",
                _skipped_gates_state_a_reason,
            ),
            Criterion(
                "no_errors_at_release",
                "No unwaived error-severity result is outstanding.",
                _no_errors_at_release,
            ),
        ),
        # A release approves the whole model, so any merged change re-opens it.
        covers=_ALL_OBJECT_TYPES,
        covers_findings=True,
        always_requires_comment=True,
    ),
)

_SPECS_BY_KEY: dict[ApprovalGateKey, GateSpec] = {spec.key: spec for spec in GATE_SPECS}


def spec_for(gate_key: ApprovalGateKey) -> GateSpec:
    """The static definition of one gate."""
    return _SPECS_BY_KEY[gate_key]


# ---------------------------------------------------------------------------
# Creating the rows
# ---------------------------------------------------------------------------
def ensure_gates(db: Session, *, project: Project) -> list[ApprovalGate]:
    """Create this project's six gate rows if they do not exist, in order.

    Idempotent, and called freely: the workflow, the API and the evaluator all
    need the rows to be there and none of them should have to know which one got
    there first.

    Title and description are refreshed from the spec on every call. They are
    static definition text rather than project state, so a row still carrying the
    wording of an older release would misdescribe the gate a reviewer is looking
    at. Nothing else on the row is touched.
    """
    existing = {
        gate.gate_key: gate
        for gate in db.execute(
            select(ApprovalGate).where(ApprovalGate.project_id == project.id)
        ).scalars()
    }

    gates: list[ApprovalGate] = []
    for spec in GATE_SPECS:
        gate = existing.get(spec.key)
        if gate is None:
            gate = ApprovalGate(
                project_id=project.id,
                gate_key=spec.key,
                ordinal=spec.ordinal,
                status=GateStatus.NOT_READY,
                title=spec.title,
                description=spec.description,
                criteria=[
                    {"key": criterion.key, "statement": criterion.statement, "met": False}
                    for criterion in spec.criteria
                ],
                unmet_criteria=[],
            )
            db.add(gate)
        else:
            gate.ordinal = spec.ordinal
            gate.title = spec.title
            gate.description = spec.description
        gates.append(gate)

    db.flush()
    return gates


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------
def evaluate_all(db: Session, *, project: Project) -> list[ApprovalGate]:
    """Re-evaluate every gate, in order, and return them.

    Order is not an optimisation. A gate cannot be evaluated while an earlier one
    is undecided, so each gate is judged against the freshly computed state of its
    predecessors rather than against whatever the rows said before this call.
    """
    gates = ensure_gates(db, project=project)
    by_key = {gate.gate_key: gate for gate in gates}
    context = GateContext(db=db, project=project, gates=by_key)

    for gate in gates:
        _evaluate(
            db, context=context, gate=gate, preceding=gates[: spec_for(gate.gate_key).ordinal]
        )
    db.flush()
    return gates


def evaluate_gate(db: Session, *, project: Project, gate: ApprovalGate) -> ApprovalGate:
    """Re-evaluate one gate against the model as it stands.

    Its predecessors are evaluated first — a single gate cannot be judged in
    isolation, because "an earlier gate is undecided" is one of the answers.
    """
    gates = evaluate_all(db, project=project)
    for candidate in gates:
        if candidate.gate_key is gate.gate_key:
            return candidate
    # Unreachable: ensure_gates writes a row for every key in GATE_ORDER.
    raise LookupError(f"project {project.id} has no {gate.gate_key.value} gate")


def gate_blocking(
    db: Session, *, project: Project, gate_key: ApprovalGateKey
) -> ApprovalGate | None:
    """The first gate at or before ``gate_key`` that still holds work up.

    The question a caller actually has is "may I start the work behind this
    gate", and the answer has to name the gate that is in the way rather than
    return a bare False. ``None`` means everything up to and including
    ``gate_key`` is settled.

    Deliberately not :attr:`ApprovalGate.blocks_progress`. That property omits
    ``FAILED``, and a gate a reviewer actively refused is the strongest block
    there is — reporting it as non-blocking would let work proceed past a
    rejection. One definition of "settled" governs both this and the ordering
    rule, so the two cannot disagree.
    """
    gates = evaluate_all(db, project=project)
    ordinal = spec_for(gate_key).ordinal
    for gate in gates[: ordinal + 1]:
        if gate.is_enabled and gate.status not in _SETTLED_STATUSES:
            return gate
    return None


def _evaluate(
    db: Session,
    *,
    context: GateContext,
    gate: ApprovalGate,
    preceding: Sequence[ApprovalGate],
) -> ApprovalGate:
    """Recompute one gate's criteria and status, and announce a change."""
    spec = spec_for(gate.gate_key)
    previous_status = gate.status
    outcomes = [_run_criterion(context, gate, criterion) for criterion in spec.criteria]

    status = _status_for(
        context=context, gate=gate, spec=spec, preceding=preceding, outcomes=outcomes
    )
    if status.extra_unmet is not None:
        outcomes.append(status.extra_unmet)

    gate.status = status.status
    gate.criteria = [dict(outcome) for outcome in outcomes]
    gate.unmet_criteria = [dict(outcome) for outcome in outcomes if not outcome["met"]]

    if gate.status is not previous_status:
        _announce(db, project=context.project, gate=gate, previous_status=previous_status)
    return gate


@dataclass(frozen=True, slots=True)
class _Status:
    """A computed status, plus the synthetic criterion explaining it where there is one."""

    status: GateStatus
    extra_unmet: dict[str, Any] | None = None


def _status_for(
    *,
    context: GateContext,
    gate: ApprovalGate,
    spec: GateSpec,
    preceding: Sequence[ApprovalGate],
    outcomes: Sequence[dict[str, Any]],
) -> _Status:
    """Where this gate stands, in the order the product rules apply.

    Disabled first, because an administrator's decision outranks everything else.
    Ordering next, because a gate whose predecessor is undecided has not been
    reached yet whatever its own criteria say. Only then does the gate's own
    content matter.
    """
    if not gate.is_enabled:
        return _Status(
            GateStatus.SKIPPED,
            _unmet(
                "gate_disabled",
                "This gate is applied to the project.",
                gate.disabled_reason
                or "This gate was disabled without a stated reason, which the release "
                "record cannot explain.",
            ),
        )

    blocker = next(
        (
            earlier
            for earlier in preceding
            if earlier.is_enabled and earlier.status not in _SETTLED_STATUSES
        ),
        None,
    )
    if blocker is not None:
        return _Status(
            GateStatus.NOT_READY,
            _unmet(
                "earlier_gates_settled",
                "Every earlier gate has passed, been skipped, or been disabled with a reason.",
                f"'{blocker.title}' is {blocker.status.value}. A gate cannot be decided while "
                "an earlier one is undecided.",
            ),
        )

    if spec.unreachable is not None:
        reason = _run_hook(gate, spec.unreachable, context)
        if reason:
            return _Status(
                GateStatus.SKIPPED,
                _unmet("gate_unreachable", spec.title, reason),
            )

    reopened = _reopening_reason(context, gate, spec)
    decided = gate.decided_revision_number is not None and gate.status in _DECIDED_STATUSES
    if decided and not reopened:
        return _Status(gate.status)

    # Re-opened, or never decided: the gate stands or falls on its own criteria.
    #
    # A re-opened gate is deliberately *not* forced to ``READY``. Announcing a
    # gate as ready to sign while one of its own criteria is unmet would
    # contradict the rule the rest of this module enforces, so a merge that both
    # invalidated the decision and broke a criterion lands the gate at
    # ``NOT_READY`` saying both things. The decision fields are left in place: the
    # row keeps saying what was decided and when, and the note below says why it
    # no longer holds, which together are what a reviewer needs in front of them.
    status = _from_criteria(context, gate, outcomes)
    if not reopened:
        return _Status(status)
    return _Status(
        status,
        _unmet(
            "decision_is_current",
            "A recorded decision covers the revision being approved.",
            reopened,
        ),
    )


def _from_criteria(
    context: GateContext, gate: ApprovalGate, outcomes: Sequence[dict[str, Any]]
) -> GateStatus:
    if any(not outcome["met"] for outcome in outcomes):
        return GateStatus.NOT_READY
    if _pending_approval(context.db, gate) is not None:
        return GateStatus.AWAITING_DECISION
    return GateStatus.READY


def _reopening_reason(context: GateContext, gate: ApprovalGate, spec: GateSpec) -> str:
    """Why a recorded decision no longer covers this gate, or ``""`` if it does.

    Both halves are required. A revision moving on its own does not invalidate a
    tag-model sign-off — plenty of merges touch nothing this gate covers — and
    re-opening every gate on every merge would train reviewers to click through
    them. What invalidates the decision is a merge that changed something the
    decision was about.

    Keeps returning the reason until a fresh decision moves
    ``decided_revision_number`` forward, so the row stays legible to anyone who
    looks at it later rather than only to whoever happened to see the event.
    """
    if gate.decided_revision_number is None:
        return ""
    if context.revision_number <= gate.decided_revision_number:
        return ""
    changed = context.changed_since(gate.decided_revision_number, spec.covers)
    if not changed:
        return ""
    return (
        f"This gate was decided at revision {gate.decided_revision_number}; the project is now "
        f"at revision {context.revision_number} and the merges since then changed "
        f"{_naming(sorted(object_type.value for object_type in changed))}. The decision does "
        "not carry over."
    )


def _run_criterion(
    context: GateContext, gate: ApprovalGate, criterion: Criterion
) -> dict[str, Any]:
    """Evaluate one criterion, and never let it take the evaluation down with it.

    A criterion that raises is recorded as unmet, naming the exception. Same
    argument as the validation run: a gate that reported "ready" because the
    check that would have blocked it crashed is worse than one that reports its
    own failure, because the first looks like a pass.
    """
    try:
        detail = criterion.check(context)
    except Exception as exc:
        logger.exception(
            "gate criterion raised",
            extra={
                "project_id": str(gate.project_id),
                "gate_key": gate.gate_key.value,
                "criterion": criterion.key,
            },
        )
        detail = (
            f"This criterion could not be evaluated: {type(exc).__name__}: {exc}. "
            "Treat it as unmet and report the defect."
        )
    if detail:
        return _unmet(criterion.key, criterion.statement, detail)
    return {"key": criterion.key, "statement": criterion.statement, "met": True, "detail": ""}


def _run_hook(gate: ApprovalGate, hook: Callable[[GateContext], str], context: GateContext) -> str:
    """A spec hook that raises must fail closed, not silently open the gate."""
    try:
        return hook(context)
    except Exception as exc:
        logger.exception(
            "gate hook raised",
            extra={"project_id": str(gate.project_id), "gate_key": gate.gate_key.value},
        )
        return f"This gate could not be evaluated: {type(exc).__name__}: {exc}."


def _unmet(key: str, statement: str, detail: str) -> dict[str, Any]:
    return {
        "key": key,
        "statement": statement,
        "met": False,
        "detail": " ".join(detail.split())[:MAX_DETAIL_CHARS],
    }


def _pending_approval(db: Session, gate: ApprovalGate) -> Approval | None:
    """The undecided request against this gate, if an agent raised one."""
    if gate.approval_id is None:
        return None
    approval = db.get(Approval, gate.approval_id)
    if approval is None or not approval.is_pending:
        return None
    return approval


# ---------------------------------------------------------------------------
# Enabling and disabling
# ---------------------------------------------------------------------------
def set_gate_enabled(
    db: Session,
    *,
    project: Project,
    gate: ApprovalGate,
    is_enabled: bool,
    reason: str,
    user: User,
) -> ApprovalGate:
    """Turn a gate on or off for this project, with the reason recorded.

    Administrator only, and disabling refuses a blank reason. The reason is the
    entire point of the function: a gate nobody applied is allowed, but "nobody
    reviewed the tag model" has to stay legible in the release record, and a
    reason supplied at the moment of the decision is the only one anybody will
    ever write. Removing a review step is also not something the people whose
    work it reviews should be able to do, which is why it takes the role it does.
    """
    permissions.require_global_role(user, RoleName.ADMINISTRATOR)
    stated = reason.strip()
    if not is_enabled and not stated:
        raise ValidationError(
            f"Disabling '{gate.title}' requires a stated reason. A gate that was not applied "
            "is recorded in the release package, and an unexplained one cannot be defended."
        )

    gate.is_enabled = is_enabled
    gate.disabled_reason = stated if not is_enabled else ""
    db.flush()

    # Written under GATE_DECIDED rather than a member of its own: turning a gate
    # off *is* a recorded decision about that gate, against the same target row.
    # ``details["change"]`` keeps the two readings apart for anyone querying the
    # trail. A dedicated audit action would be better and needs a contract
    # change; see the handoff note.
    audit.record(
        db,
        AuditAction.GATE_DECIDED,
        actor_user_id=user.id,
        project_id=project.id,
        target_type="approval_gate",
        target_id=gate.id,
        summary=(
            f"{'Enabled' if is_enabled else 'Disabled'} approval gate '{gate.title}'"
            + (f": {stated}" if stated else "")
        ),
        details={
            "change": "enablement",
            "gate_key": gate.gate_key.value,
            "is_enabled": is_enabled,
            "reason": stated,
        },
    )
    return evaluate_gate(db, project=project, gate=gate)


# ---------------------------------------------------------------------------
# Requesting and recording a decision
# ---------------------------------------------------------------------------
def request_decision(
    db: Session, *, project: Project, gate: ApprovalGate, requested_by_agent: str
) -> Approval:
    """Raise the human decision this gate is waiting for.

    An agent may reach this. It creates a pending
    :class:`~app.models.approval.Approval` and moves the gate to
    ``AWAITING_DECISION``; it decides nothing. The ``context`` snapshot is taken
    here so that what the reviewer was shown cannot be changed by a later edit —
    an approval is only worth recording alongside what it was given for.

    Refused unless the gate is ``READY``. Asking a human to sign off a gate whose
    own criteria are unmet is asking them to approve a list of things nobody has
    done, and it is how a gate becomes a button people press.
    """
    gate = evaluate_gate(db, project=project, gate=gate)

    if not gate.is_enabled:
        raise ConflictError(
            f"'{gate.title}' is disabled for this project ("
            f"{gate.disabled_reason or 'no reason recorded'}), so there is no decision to make."
        )

    # Convergence is checked before readiness, and the order matters: the task
    # queue is at-least-once, so the second call arrives at a gate this function
    # has already moved to AWAITING_DECISION. Checking readiness first would turn
    # a retry into an error and put the task into a failure loop, when the
    # correct behaviour is to hand back the one question already in front of the
    # reviewer rather than asking it twice.
    existing = _pending_approval(db, gate)
    if existing is not None:
        return existing

    if gate.status is not GateStatus.READY:
        raise InvalidGateStateError(gate, GateStatus.AWAITING_DECISION)

    approval = Approval(
        project_id=project.id,
        gate_key=gate.gate_key,
        kind=ApprovalKind.GATE_DECISION,
        status=ApprovalStatus.PENDING,
        revision_number=project.current_revision_number,
        title=f"Approval gate: {gate.title}",
        question=(
            f"{gate.description}\n\n"
            f"Approving this gate accepts responsibility for the work behind it at revision "
            f"{project.current_revision_number}. A later change to what this gate covers "
            f"re-opens it rather than inheriting your decision."
        ),
        context={
            "gate_key": gate.gate_key.value,
            "revision_number": project.current_revision_number,
            "criteria": gate.criteria,
            "unmet_criteria": gate.unmet_criteria,
        },
        requested_by_agent_kind=requested_by_agent[:40],
        requested_at=utcnow(),
    )
    db.add(approval)
    db.flush()

    previous_status = gate.status
    gate.approval_id = approval.id
    gate.status = GateStatus.AWAITING_DECISION
    db.flush()

    audit.record(
        db,
        AuditAction.APPROVAL_REQUESTED,
        actor_agent_kind=requested_by_agent,
        project_id=project.id,
        target_type="approval",
        target_id=approval.id,
        summary=f"Requested a decision on approval gate '{gate.title}'",
        details={
            "gate_key": gate.gate_key.value,
            "revision_number": project.current_revision_number,
        },
    )
    record_event(
        db,
        project_id=project.id,
        event_type=EventType.APPROVAL_REQUESTED,
        payload={
            "approval_id": str(approval.id),
            "kind": approval.kind.value,
            "status": approval.status.value,
            "title": approval.title,
            "gate_key": gate.gate_key.value,
        },
    )
    _announce(db, project=project, gate=gate, previous_status=previous_status)
    return approval


def record_decision(
    db: Session,
    *,
    gate: ApprovalGate,
    approval: Approval,
    decision: ApprovalDecision,
    user: User,
    comment: str = "",
) -> ApprovalGate:
    """Settle a gate on a named human's authority.

    Everything this refuses is a product rule rather than defensive coding:

    * an agent, or a user without the reviewer role on this project, cannot reach
      a decision at all;
    * a decision on a gate that is not awaiting one is a decision about nothing;
    * reject, request-changes and accept-risk demand a comment, as does any gate
      covering critical or high findings and the release gate itself;
    * gate 5 cannot be approved without evidence that a licensed workstation
      actually imported the package. That is the exact place a product like this
      lies to itself, so it is a refusal and not a warning.

    ``REQUEST_CHANGES`` and ``DEFER`` are recorded on the approval but leave the
    gate undecided and its ``decided_revision_number`` unset — neither is an
    answer, and treating either as one would put a revision number against a
    decision nobody made.
    """
    project = db.get(Project, gate.project_id)
    if project is None:  # pragma: no cover - a gate cannot outlive its project
        raise LookupError(f"approval gate {gate.id} references a project that no longer exists")

    _require_reviewer(db, project=project, user=user)

    if approval.project_id != gate.project_id or approval.gate_key is not gate.gate_key:
        raise ConflictError(
            f"Approval '{approval.title}' is not the decision this gate is waiting for."
        )
    if not approval.is_pending:
        raise ConflictError(
            f"This approval was already {approval.status.value}"
            + (f" on {approval.decided_at:%Y-%m-%d}." if approval.decided_at else ".")
        )
    if gate.status is not GateStatus.AWAITING_DECISION:
        raise InvalidGateStateError(gate, GateStatus.PASSED)

    context = GateContext(db=db, project=project)
    spec = spec_for(gate.gate_key)

    reason = _comment_required_because(context, spec=spec, decision=decision)
    if reason and not comment.strip():
        raise ValidationError(
            f"This decision requires a comment: {reason} Refusing to record it without one."
        )

    if decision is ApprovalDecision.APPROVE and spec.unreachable is not None:
        blocked = _run_hook(gate, spec.unreachable, context)
        if blocked:
            raise ValidationError(
                f"'{gate.title}' cannot be approved: {blocked} Record it as skipped with that "
                "reason instead — approving a step that did not happen is the one thing this "
                "gate exists to prevent."
            )

    approval_status, gate_status = _OUTCOMES[decision]
    approval.status = approval_status
    approval.decision = decision
    approval.decided_by_id = user.id
    approval.decided_at = utcnow()
    approval.decision_notes = comment

    previous_status = gate.status
    gate.status = gate_status
    gate.decision_comment = comment
    if decision in _SETTLING_DECISIONS:
        gate.decided_by_id = user.id
        gate.decided_at = approval.decided_at
        gate.decided_revision_number = approval.revision_number
    db.flush()

    audit.record(
        db,
        AuditAction.GATE_DECIDED,
        actor_user_id=user.id,
        project_id=project.id,
        target_type="approval_gate",
        target_id=gate.id,
        summary=f"{decision.value} on approval gate '{gate.title}'",
        details={
            "gate_key": gate.gate_key.value,
            "decision": decision.value,
            "approval_status": approval_status.value,
            "gate_status": gate_status.value,
            "revision_number": approval.revision_number,
            "comment": comment,
        },
    )
    record_event(
        db,
        project_id=project.id,
        event_type=EventType.APPROVAL_DECIDED,
        actor_user_id=user.id,
        payload={
            "approval_id": str(approval.id),
            "kind": approval.kind.value,
            "status": approval.status.value,
            "title": approval.title,
            "gate_key": gate.gate_key.value,
            "decided_by_id": str(user.id),
        },
    )
    _announce(db, project=project, gate=gate, previous_status=previous_status)

    logger.info(
        "approval gate decided",
        extra={
            "project_id": str(project.id),
            "gate_key": gate.gate_key.value,
            "decision": decision.value,
            "gate_status": gate_status.value,
            "revision_number": approval.revision_number,
        },
    )

    # Settling a gate is what makes the next one reachable, so the whole set is
    # re-evaluated here rather than at whatever moment somebody next asks. The
    # gate behind this one becomes ready the instant this decision lands, and a
    # city that only learns about it on the next unrelated page load would be
    # showing an engineer work they could already have started.
    evaluate_all(db, project=project)
    return gate


def decision_of(db: Session, gate: ApprovalGate) -> ApprovalDecision | None:
    """What was actually chosen, for a caller that must not conflate the two passes.

    ``APPROVE`` and ``ACCEPT_RISK`` both leave a gate ``PASSED``. The release
    manifest, the approval log and anyone auditing a package afterwards need to
    know which, so the distinction is readable from the gate rather than only
    from whoever remembers to join the approval table.
    """
    if gate.approval_id is None:
        return None
    approval = db.get(Approval, gate.approval_id)
    return approval.decision if approval is not None else None


def _require_reviewer(db: Session, *, project: Project, user: User) -> None:
    """Only a human with the reviewer role on this project may settle a gate.

    Membership alone is not enough. An engineer signing off their own project's
    safety work is the separation the reviewer role exists to create, and a gate
    decision is the point where that separation either holds or does not.
    """
    role = permissions.project_role_for(db, user, project.id)
    if not permissions.can_decide_approval(user, role):
        raise PermissionDeniedError(
            "Recording an approval gate decision requires the reviewer role on this project. "
            "This is an engineering sign-off, not a bookkeeping action.",
            details={"required_any_of": sorted(item.value for item in permissions.REVIEW_ROLES)},
        )
    if not user.is_active:
        raise PermissionDeniedError("A deactivated account cannot record an engineering decision.")


def _comment_required_because(
    context: GateContext, *, spec: GateSpec, decision: ApprovalDecision
) -> str:
    """Why this decision needs a written reason, or ``""`` when it does not."""
    if decision in DECISIONS_REQUIRING_COMMENT:
        return (
            f"a decision of '{decision.value}' obliges the reviewer to say why, so the next "
            f"person can act on it."
        )
    if spec.always_requires_comment:
        return (
            "the release gate carries a covering statement into the delivery package, and a "
            "release nobody wrote a sentence about cannot be explained afterwards."
        )
    if spec.covers_findings and context.serious_findings:
        severities = sorted({finding.severity.value for finding in context.serious_findings})
        return (
            f"this gate covers {len(context.serious_findings)} finding(s) at "
            f"{_naming(severities)} severity, and what was decided about them has to be on "
            f"the record."
        )
    return ""


# ---------------------------------------------------------------------------
# Announcing a change
# ---------------------------------------------------------------------------
def _announce(
    db: Session, *, project: Project, gate: ApprovalGate, previous_status: GateStatus
) -> None:
    """Broadcast a status change, carrying what is still outstanding.

    The unmet criteria travel with the event rather than being fetched
    afterwards: a UI told only that a gate is "not ready" has nothing to render
    and nothing for an engineer to act on, which is the failure this whole module
    is shaped around.
    """
    unmet = [
        str(item.get("detail") or item.get("statement", ""))[:200] for item in gate.unmet_criteria
    ]
    record_event(
        db,
        project_id=project.id,
        event_type=EventType.GATE_STATUS_CHANGED,
        actor_user_id=gate.decided_by_id,
        payload={
            "gate_key": gate.gate_key.value,
            "previous_status": previous_status.value,
            "status": gate.status.value,
            "location": SemanticLocation.APPROVAL_PLAZA.value,
            "title": gate.title[:200],
            "criteria_count": len(gate.criteria),
            "unmet_criteria_count": len(gate.unmet_criteria),
            "unmet_criteria": unmet[:20],
            "is_enabled": gate.is_enabled,
            "disabled_reason": gate.disabled_reason[:500],
            "approval_id": str(gate.approval_id) if gate.approval_id else None,
            "decided_by_id": str(gate.decided_by_id) if gate.decided_by_id else None,
            "decided_revision_number": gate.decided_revision_number,
            "decision_comment": gate.decision_comment[:500],
        },
    )
