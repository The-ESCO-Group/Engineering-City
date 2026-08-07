"""Deciding when a release package may be produced, from what, and by whom.

:mod:`app.generators.rockwell` knows how to *emit* files. It reads a slice of
the canonical model, writes bytes, and refuses when the model would produce a
file that fails at import. What it deliberately does not know is whether anyone
should be generating at all. That decision is the whole of this module, and it
is a different kind of decision: it is about approvals, validation state,
revision currency and scope, none of which a generator has any business reading.

The pipeline is four calls, in order, and each one can refuse:

``create_release_candidate``
    Reserves a candidate number, pins the revision it will be cut from, and
    marks that revision a release baseline so it can never become the base of an
    in-place edit. A package with a checksum has to keep meaning what it meant.
``generate_artifacts``
    Loads the model, builds the package in memory, writes it under the project's
    ``releases/`` tree, and records one :class:`~app.models.release.ExportArtifact`
    per file with the level that file actually reached.
``freeze_manifest``
    Writes the immutable record of what was released, against a named human. It
    re-reads the files from disk and checks them against the recorded checksums,
    because a manifest that describes bytes nobody re-examined is a manifest that
    only proves somebody hashed something once.
``release_readiness``
    Answers "may I press the button" **without generating anything**, because
    that is the question a UI asks before offering the button. Every other
    function is a refusal path; this one is the same rules read out loud.

Three rules run through all of it.

**The candidate's level is the weakest of its artifacts.** One CSV that only
reached ``GENERATED`` means the package reached ``GENERATED``, whatever the L5X
files managed. Rolling up to the strongest would promote a file nobody checked
to the standing of one that was.

**Import verification comes from the adapter, never from a default.** With no
licensed workstation configured the status is ``UNAVAILABLE`` carrying the
adapter's own reason. ``NOT_ATTEMPTED`` on a package nobody could ever have
attempted reads as "fine so far", which is exactly the overclaim this product
exists to avoid.

**A refusal names what and where.** Every block carries a stable code, a summary
and a detail written to be shown to an engineer unedited, because "release
blocked" gives a UI nothing to render and the person holding the gate nothing
to do.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import set_committed_value

from app.adapters.workstation import get_adapter
from app.core.clock import ensure_utc, isoformat_z, utcnow
from app.core.config import Settings, get_settings
from app.core.enums import (
    MAX_OFFLINE_VALIDATION_LEVEL,
    VALIDATION_LEVEL_ORDER,
    ApprovalGateKey,
    ApprovalStatus,
    AuditAction,
    ControllerFamily,
    EventType,
    GateStatus,
    ImportVerificationStatus,
    ObjectApprovalState,
    ProfileFieldStatus,
    ReleaseCandidateStatus,
    ReleaseMode,
    RestrictedDomainMode,
    SemanticLocation,
    TagScope,
    ValidationLevel,
    ValidationRunStatus,
    ValidationSeverity,
)
from app.core.errors import ConflictError, NotFoundError
from app.core.logging import get_logger
from app.core.storage import ProjectStorage
from app.database.locking import lock_project_root
from app.engineering import gates, registry
from app.engineering.revisions import (
    current_revision_number,
    ensure_initial_revision,
    get_revision,
    mark_release_baseline,
)
from app.generators.rockwell import (
    ApprovalRecord,
    ChangeLogEntry,
    DeliverySlot,
    EvidenceIndexEntry,
    GenerationRefused,
    Limitation,
    PackageOptions,
    ProgramContent,
    ProjectModel,
    ReleasePackage,
    RoutineContent,
    TargetProfile,
    TaskContent,
    UdtContent,
    ValidationSummary,
    build_release_package,
)
from app.generators.rockwell.package import AUDIT_DIR, DEFAULT_DELIVERY_SLOTS, MANIFEST_DIR
from app.models.approval import Approval
from app.models.core import EngineeringObjectMixin, ObjectEvidenceLink
from app.models.evidence import EvidenceAnchor
from app.models.plant import HardwareModule
from app.models.plc import (
    ControllerModel,
    ControllerTask,
    LogicComment,
    LogicInstruction,
    LogicOperand,
    LogicReference,
    LogicRung,
    Program,
    Routine,
)
from app.models.profile import ProjectDecision, ProjectProfile, TargetPlatform
from app.models.project import Project
from app.models.release import (
    ExportProfile,
    ImportDependency,
    ReleaseCandidate,
    ReleaseManifest,
    ValidationResult,
    ValidationRun,
)
from app.models.requirements import Requirement
from app.models.revision import ChangeOperation, ChangeSet
from app.models.tags import AOIDefinitionReference, Tag, UDTDefinition, UDTMember
from app.models.user import User
from app.orchestration.events import record_event
from app.security import audit
from app.security.paths import resolve_within

logger = get_logger(__name__)

__all__ = [
    "FORCED_STALE_REVISION_CODE",
    "RELEASES_DIR",
    "BlockCode",
    "ReleaseBlock",
    "ReleaseReadiness",
    "ReleaseRefused",
    "create_release_candidate",
    "freeze_manifest",
    "generate_artifacts",
    "release_readiness",
    "was_forced_past_stale_revision",
]

#: Directory under the project store that holds every generated package. A
#: sibling of ``originals/`` and ``derived/`` rather than a child: a release is
#: neither an upload nor parser output, and mixing it into ``derived/`` would
#: put files that are sealed by a checksum next to files that are rewritten
#: whenever a parser is upgraded.
RELEASES_DIR = "releases"

#: The limitation code that records a package generated from a revision the
#: project has already moved past. Written into the candidate at creation and
#: shipped inside the package, so the fact travels to the customer rather than
#: living in a log nobody reads.
FORCED_STALE_REVISION_CODE = "release.forced_stale_revision"

#: Statuses a candidate may be generated from. ``VALIDATION_FAILED`` is included
#: because the normal response to a refused generation is to fix the model and
#: run it again on the same candidate; the alternative is a candidate number
#: burnt for every mistake, which teaches people to avoid the pipeline.
_GENERATABLE_STATUSES: frozenset[ReleaseCandidateStatus] = frozenset(
    {
        ReleaseCandidateStatus.DRAFT,
        ReleaseCandidateStatus.GENERATING,
        ReleaseCandidateStatus.GENERATED,
        ReleaseCandidateStatus.VALIDATION_FAILED,
    }
)

#: Statuses a manifest may be frozen from. A draft has no files, and a candidate
#: that already carries a manifest is immutable by construction.
_FREEZABLE_STATUSES: frozenset[ReleaseCandidateStatus] = frozenset(
    {
        ReleaseCandidateStatus.GENERATED,
        ReleaseCandidateStatus.AWAITING_APPROVAL,
        ReleaseCandidateStatus.APPROVED,
    }
)

#: The last gate that can honestly be judged *before* a package exists.
#:
#: Gate 5 asks whether a licensed workstation imported the candidate and gate 6
#: asks whether its manifest is frozen — both are statements about a package, so
#: demanding them before generating one is a deadlock rather than a safeguard.
#: Gates 1 to 4 cover the engineering the package is made of, which is what a
#: generation run actually depends on.
_PRE_GENERATION_GATE = ApprovalGateKey.QA_AND_RISK

#: The bar rises for a freeze: the package now exists, so gate 5 can be
#: evaluated. It is ``SKIPPED`` with a stated reason wherever no workstation is
#: configured, which counts as settled — the honest outcome, not a pass.
#: Gate 6 stays out because the frozen manifest it looks for is what this call
#: is in the middle of writing.
_PRE_FREEZE_GATE = ApprovalGateKey.IMPORT_VERIFICATION

#: Approval statuses that represent a settled human decision worth carrying into
#: the package. ``RISK_ACCEPTED`` is included and kept distinct: a release that
#: proceeded on knowingly carried risk must never look afterwards like one with
#: no risk at all.
_SETTLED_APPROVALS: frozenset[ApprovalStatus] = frozenset(
    {ApprovalStatus.APPROVED, ApprovalStatus.RISK_ACCEPTED}
)


class BlockCode(StrEnum):
    """Why a release cannot proceed. Stable: clients branch on these."""

    NO_PROFILE = "no_profile"
    REVIEW_ONLY = "review_only"
    PROFILE_INCOMPLETE = "profile_incomplete"
    RESTRICTED_DOMAIN = "restricted_domain"
    NO_CONTROLLER = "no_controller"
    NO_VALIDATION_RUN = "no_validation_run"
    VALIDATION_ERRORS = "validation_errors"
    GATE_UNDECIDED = "gate_undecided"
    STALE_REVISION = "stale_revision"
    CANDIDATE_STATUS = "candidate_status"
    GENERATION_REFUSED = "generation_refused"
    NO_NAMED_APPROVER = "no_named_approver"
    MANIFEST_ALREADY_FROZEN = "manifest_already_frozen"
    PACKAGE_MISSING = "package_missing"
    PACKAGE_ALTERED = "package_altered"
    UNKNOWN_ARTIFACT_KIND = "unknown_artifact_kind"


@dataclass(frozen=True, slots=True)
class ReleaseBlock:
    """One reason a release cannot proceed, written to be shown to an engineer.

    ``detail`` is not a restatement of ``summary``. The summary is the rule that
    was broken; the detail names the object, the count or the gate, because a
    block a reviewer cannot locate is a block nobody can clear.
    """

    code: BlockCode
    summary: str
    detail: str

    def as_document(self) -> dict[str, str]:
        return {"code": str(self.code), "summary": self.summary, "detail": self.detail}


class ReleaseRefused(ConflictError):
    """The release pipeline will not take this step.

    Carries every block rather than the first, so a caller that fixes one thing
    does not discover the next one on the following attempt. ``details`` mirrors
    the blocks into the API error shape.
    """

    code = "release_refused"

    def __init__(self, blocks: Sequence[ReleaseBlock]) -> None:
        summary = "; ".join(f"{block.summary} {block.detail}".strip() for block in blocks)
        super().__init__(
            summary or "The release was refused without a stated reason, which is a defect.",
            details={"blocks": [block.as_document() for block in blocks]},
        )
        self.blocks = tuple(blocks)


@dataclass(frozen=True, slots=True)
class ReleaseReadiness:
    """Whether a package may be generated, and everything a UI needs to say why not.

    Answerable without generating anything. The button, the tooltip explaining
    why it is disabled and the list of gates a project manager has to chase all
    come from one call, so they cannot disagree with each other or with what the
    pipeline will actually do.
    """

    revision_number: int
    is_ready: bool
    blocking: tuple[ReleaseBlock, ...]
    #: Gates at or before :data:`_PRE_GENERATION_GATE` that are still open, in
    #: gate order, so a UI can point at the first one to work on.
    outstanding_gates: tuple[ApprovalGateKey, ...]
    #: Every error the newest completed validation run found, waived or not.
    #: Deliberately the total and not the outstanding subset: it is the number the
    #: package manifest records, and a project that reported zero once a reviewer
    #: had carried them would look afterwards exactly like one that never had any.
    #: What *blocks* is the unwaived subset, which is why this number can be
    #: nonzero while :attr:`is_ready` is true — the waivers are on the results
    #: themselves, each naming who carried it and why.
    validation_error_count: int
    #: The ceiling this deployment can reach at all — not what any package has
    #: reached. Without a licensed workstation it is ``ROUND_TRIP_PARSED``, and
    #: saying so up front is what stops "verified" being expected later.
    highest_attainable_validation_level: ValidationLevel
    #: Whether a Studio 5000 import could even be attempted here.
    import_verification_possible: bool
    #: The adapter's own words for why it cannot be, when it cannot be.
    import_verification_detail: str

    def as_document(self) -> dict[str, Any]:
        return {
            "revision_number": self.revision_number,
            "is_ready": self.is_ready,
            "blocking": [block.as_document() for block in self.blocking],
            "outstanding_gates": [str(key) for key in self.outstanding_gates],
            "validation_error_count": self.validation_error_count,
            "highest_attainable_validation_level": str(self.highest_attainable_validation_level),
            "import_verification_possible": self.import_verification_possible,
            "import_verification_detail": self.import_verification_detail,
        }


@dataclass(frozen=True, slots=True)
class _ValidationRunSelection:
    """The newest run plus whether its authoritative ordering is ambiguous."""

    run: ValidationRun | None
    ambiguous_current_revision: bool = False
    future_revision: int | None = None


# ---------------------------------------------------------------------------
# Readiness
# ---------------------------------------------------------------------------
def release_readiness(
    db: Session, *, project: Project, settings: Settings | None = None
) -> ReleaseReadiness:
    """Whether ``project`` may produce a release candidate, and what is in the way.

    Generates nothing and writes no release row. It does re-evaluate the
    approval gates, because a gate status that was computed against an older
    revision is not an answer to this question — that is a read of the model
    expressed as a row update, and it is how :mod:`app.engineering.gates` is
    meant to be used.
    """
    active = settings or get_settings()
    revision = current_revision_number(db, project.id)
    selection = _latest_completed_run_selection(db, project, revision_number=revision)
    run = selection.run

    blocks = [
        *_profile_blocks(_profile_of(db, project)),
        *_controller_blocks(db, project),
        *_validation_blocks(
            run,
            revision,
            unwaived_errors=_unwaived_error_count(db, run),
            ambiguous_current_revision=selection.ambiguous_current_revision,
            future_revision=selection.future_revision,
        ),
    ]
    outstanding = _outstanding_gates(db, project, _PRE_GENERATION_GATE)
    if outstanding:
        blocks.append(_gate_block(outstanding))

    health = get_adapter(active).health()
    return ReleaseReadiness(
        revision_number=revision,
        is_ready=not blocks,
        blocking=tuple(blocks),
        outstanding_gates=outstanding,
        # Zero when the newest run is older than the model: it counted errors in
        # a revision nobody is releasing, and reporting them here would attach
        # them to one that has not been examined.
        validation_error_count=(
            int(run.error_count or 0) if run is not None and run.revision_number >= revision else 0
        ),
        highest_attainable_validation_level=(
            ValidationLevel.ROCKWELL_IMPORT_VERIFIED
            if health.available
            else MAX_OFFLINE_VALIDATION_LEVEL
        ),
        import_verification_possible=health.available,
        import_verification_detail=health.detail,
    )


# ---------------------------------------------------------------------------
# Stage 1: the candidate
# ---------------------------------------------------------------------------
def create_release_candidate(
    db: Session,
    *,
    project: Project,
    export_profile: ExportProfile | None = None,
    requested_by: User | None = None,
    force: bool = False,
    settings: Settings | None = None,
) -> ReleaseCandidate:
    """Reserve a candidate at the project's current revision, or refuse to.

    ``force`` buys exactly one thing: permission for the later stages to proceed
    when the project has moved on underneath this candidate. It is taken here,
    at creation, because that is the moment a human is present to give it —
    :func:`generate_artifacts` may well run unattended from a queue, and a
    prompt nobody sees is not consent. The permission is recorded as a
    *limitation on the package*, which means it ships inside the package rather
    than living in a log: a customer holding files cut from revision 7 while the
    project stands at revision 9 has to be able to see that from the files.

    Nothing else is forceable. A blocking validation error, an undecided gate
    and a profile that forbids generation are refusals, not warnings.
    """
    active = settings or get_settings()
    # Candidate numbering and "latest candidate" gate binding are project-wide
    # invariants. Take the same cross-dialect write root as gate decisions
    # before reading readiness or MAX(candidate_number), then replace any
    # route/worker-preloaded identity with the authoritative project row.
    locked_project = lock_project_root(db, project.id)
    if locked_project is None:  # pragma: no cover - callers already authorize the project
        raise NotFoundError("Project", project.id)
    project = locked_project
    readiness = release_readiness(db, project=project, settings=active)
    if not readiness.is_ready:
        raise ReleaseRefused(readiness.blocking)

    # Revision 0 exists from the moment a project has an engineering model at
    # all. Ensured here rather than assumed, because a model written by an
    # importer rather than by a merge would otherwise have no row to baseline —
    # and a candidate pinned to a revision nobody recorded is a checksum with
    # nothing behind it.
    ensure_initial_revision(db, project)
    revision_number = readiness.revision_number
    revision = get_revision(db, project.id, revision_number)
    if revision is None:
        raise NotFoundError("Project revision", revision_number)

    # Before the row exists, not after: a candidate pointing at a revision that
    # is still editable is a checksum with nothing behind it.
    mark_release_baseline(db, revision)

    profile = export_profile or _default_export_profile(db, project)
    number = _next_candidate_number(db, project)
    candidate = ReleaseCandidate(
        project_id=project.id,
        revision_number=revision_number,
        candidate_number=number,
        name=f"RC {number}",
        status=ReleaseCandidateStatus.DRAFT,
        export_profile_id=profile.id if profile is not None else None,
        validation_run_id=_latest_completed_run_id(db, project),
        created_in_revision=revision_number,
        updated_in_revision=revision_number,
        created_by_id=requested_by.id if requested_by is not None else None,
        limitations=[_forced_limitation(revision_number).as_document()] if force else [],
    )
    db.add(candidate)
    db.flush()

    record_event(
        db,
        project_id=project.id,
        event_type=EventType.RELEASE_CANDIDATE_CREATED,
        actor_user_id=requested_by.id if requested_by is not None else None,
        payload={
            "release_candidate_id": str(candidate.id),
            "candidate_number": candidate.candidate_number,
            "revision_number": candidate.revision_number,
            "status": str(candidate.status),
            # The column default, and the weakest member there is. A reader
            # cannot mistake it for a claim: status is ``draft`` and
            # ``artifact_count`` is zero, so nothing has been produced yet.
            "validation_level": str(candidate.validation_level),
            "location": SemanticLocation.RELEASE_LOGISTICS.value,
            "name": candidate.name,
            "artifact_count": 0,
            "package_bytes": 0,
            "package_sha256": "",
            "validation_run_id": (
                str(candidate.validation_run_id) if candidate.validation_run_id else None
            ),
            "export_profile_id": str(profile.id) if profile is not None else None,
            "generator_version": "",
            "limitation_count": len(candidate.limitations),
            "unsupported_item_count": 0,
        },
    )
    audit.record(
        db,
        AuditAction.RELEASE_CANDIDATE_CREATED,
        actor_user_id=requested_by.id if requested_by is not None else None,
        project_id=project.id,
        target_type="release_candidate",
        target_id=candidate.id,
        summary=f"Release candidate {number} reserved at revision {revision_number}.",
        details={
            "candidate_number": number,
            "revision_number": revision_number,
            "export_profile_key": profile.key if profile is not None else "",
            "forced_past_stale_revision": force,
        },
    )
    # The session runs with autoflush off, so an audit row written and not
    # flushed is invisible to the next query in the same transaction. Flushed
    # here rather than left to the caller: an operation that reports success
    # while its own audit entry is still pending is exactly the pairing the
    # audit log exists to make impossible.
    db.flush()
    return candidate


# ---------------------------------------------------------------------------
# Stage 2: the files
# ---------------------------------------------------------------------------
def generate_artifacts(
    db: Session, *, candidate: ReleaseCandidate, settings: Settings | None = None
) -> ReleaseCandidate:
    """Build the package, write it, and record one artifact row per file.

    Everything is assembled in memory first and only then written, which is what
    makes the generator's refusals cheap: a model the generator will not emit
    leaves nothing on disk to clean up, and the candidate is left in
    ``VALIDATION_FAILED`` naming the object that stopped it.

    Re-running against the same revision is deliberately allowed and produces
    byte-identical files. That property is the reason a checksum in a manifest
    is worth recording at all — it lets a regenerated package be diffed against
    an approved one instead of merely re-hashed.
    """
    active = settings or get_settings()
    project = _project_of(db, candidate)

    blocks = [
        *_candidate_status_blocks(candidate, _GENERATABLE_STATUSES, "generated"),
        *_staleness_blocks(db, project, candidate),
        *_profile_blocks(_profile_of(db, project)),
    ]
    if blocks:
        raise ReleaseRefused(blocks)

    export_profile = _export_profile_of(db, candidate)
    target = _target_profile(db, project, export_profile)
    model = _load_project_model(db, project, revision_number=candidate.revision_number)
    options = _package_options(
        db,
        project=project,
        candidate=candidate,
        model=model,
        target=target,
        export_profile=export_profile,
        settings=active,
    )

    candidate.status = ReleaseCandidateStatus.GENERATING
    db.flush()

    try:
        package = build_release_package(model, options)
    except GenerationRefused as exc:
        return _record_generation_refusal(db, project=project, candidate=candidate, exc=exc)

    # Announced here rather than before the build: an export that is going to
    # refuse produces no files, and an ``export.started`` with no matching
    # artifacts is an event a client cannot resolve. By this point the kinds and
    # the count are facts rather than intentions.
    record_event(
        db,
        project_id=project.id,
        event_type=EventType.EXPORT_STARTED,
        payload={
            "release_candidate_id": str(candidate.id),
            "candidate_number": candidate.candidate_number,
            "revision_number": candidate.revision_number,
            "location": SemanticLocation.RELEASE_LOGISTICS.value,
            "export_profile_id": (str(export_profile.id) if export_profile is not None else None),
            "export_profile_key": export_profile.key if export_profile is not None else "",
            "artifact_kinds": sorted({str(file.kind) for file in package.files}),
            "expected_artifact_count": len(package.files),
        },
    )

    storage = ProjectStorage(project.id, active)
    root = _write_package(storage, candidate=candidate, package=package)

    _replace_artifact_rows(db, project=project, candidate=candidate, package=package)

    package.apply_to_candidate(candidate)
    candidate.package_path = _relative_package_path(storage, root)
    candidate.package_bytes = sum(file.size_bytes for file in package.files)
    # One value that verifies the whole package: ``checksums.sha256`` hashes
    # every other file, so hashing it covers all of them. Recorded on the
    # candidate as well as in the manifest because the two are read by different
    # people at different times and neither should have to open the other.
    candidate.package_sha256 = _checksum_file_hash(package)
    candidate.validation_level = _weakest_level(package)
    _apply_verification_status(candidate, settings=active)
    _record_target_versions(candidate, _target_platform_of(db, project))
    candidate.status = ReleaseCandidateStatus.GENERATED
    candidate.updated_in_revision = candidate.revision_number
    db.flush()

    for artifact in sorted(candidate.artifacts, key=lambda row: row.relative_path):
        record_event(
            db,
            project_id=project.id,
            event_type=EventType.EXPORT_ARTIFACT_CREATED,
            payload={
                "artifact_id": str(artifact.id),
                "release_candidate_id": str(candidate.id),
                "kind": str(artifact.kind),
                "filename": artifact.filename,
                "location": SemanticLocation.RELEASE_LOGISTICS.value,
                "validation_level": str(artifact.validation_level),
                "relative_path": artifact.relative_path,
                "size_bytes": artifact.size_bytes,
                "sha256": artifact.sha256,
                "import_order": artifact.import_order,
                "is_produced_by_plugin": artifact.is_produced_by_plugin,
                "plugin_name": artifact.plugin_name,
                "warning_count": len(artifact.warnings),
            },
        )

    audit.record(
        db,
        AuditAction.EXPORT_GENERATED,
        project_id=project.id,
        target_type="release_candidate",
        target_id=candidate.id,
        summary=(
            f"Release candidate {candidate.candidate_number} generated "
            f"{len(package.files)} artifact(s) at {candidate.validation_level.value}."
        ),
        details={
            "outcome": "generated",
            "revision_number": candidate.revision_number,
            "artifact_count": len(package.files),
            "package_path": candidate.package_path,
            "package_sha256": candidate.package_sha256,
            "validation_level": str(candidate.validation_level),
            "import_verification_status": str(candidate.import_verification_status),
        },
    )
    db.flush()
    return candidate


# ---------------------------------------------------------------------------
# Stage 3: the manifest
# ---------------------------------------------------------------------------
def freeze_manifest(
    db: Session,
    *,
    candidate: ReleaseCandidate,
    approved_by: User | None,
    settings: Settings | None = None,
) -> ReleaseManifest:
    """Write the immutable record of what was released, against a named person.

    ``approved_by`` is typed optional and refused at runtime on purpose. A
    signature is the one thing in this pipeline that cannot be inferred,
    defaulted or supplied by an agent, so the rule is enforced where it can be
    tested rather than left to a type checker that does not run in production.

    The manifest is built from the bytes on disk, not from the package object
    that wrote them, and every artifact is re-read rather than only the three
    documents the row quotes. Re-reading and re-hashing is the only way
    ``content_sha256`` means "this is what the customer holds" rather than "this
    is what we intended to write"; a mismatch against the recorded artifact
    checksums is a refusal, because a package that changed after generation is
    not a package anyone can sign.
    """
    project = _project_of(db, candidate)
    blocks: list[ReleaseBlock] = []

    if approved_by is None or approved_by.id is None:
        blocks.append(
            ReleaseBlock(
                code=BlockCode.NO_NAMED_APPROVER,
                summary="Freezing a release manifest requires a named human approver.",
                detail=(
                    "No user was supplied. The manifest is the record handed to the "
                    "customer, and an approval that resolves to nobody is an approval "
                    "nobody can be asked about."
                ),
            )
        )
    blocks.extend(_candidate_status_blocks(candidate, _FREEZABLE_STATUSES, "frozen"))
    blocks.extend(_staleness_blocks(db, project, candidate))
    if _manifest_of(db, candidate) is not None:
        blocks.append(
            ReleaseBlock(
                code=BlockCode.MANIFEST_ALREADY_FROZEN,
                summary="This candidate already has a manifest.",
                detail=(
                    f"Release candidate {candidate.candidate_number} was frozen already. "
                    "A manifest is written once; any change at all produces the next "
                    "candidate with its own manifest."
                ),
            )
        )
    outstanding = _outstanding_gates(db, project, _PRE_FREEZE_GATE)
    if outstanding:
        blocks.append(_gate_block(outstanding))
    # ``approved_by`` is repeated in the condition so the refusal above is what
    # narrows it, rather than an assert that would vanish under ``python -O``.
    if blocks or approved_by is None:
        raise ReleaseRefused(blocks)

    documents = _reread_manifest_documents(candidate, settings or get_settings())

    manifest = ReleaseManifest(
        project_id=project.id,
        release_candidate_id=candidate.id,
        manifest_version=str(documents.content.get("manifest_version", "")),
        content=documents.content,
        content_sha256=documents.content_sha256,
        artifact_count=len(candidate.artifacts),
        checksum_file_sha256=documents.checksum_file_sha256,
        evidence_index_sha256=documents.evidence_index_sha256,
        approval_summary=dict(documents.content.get("approvals_summary", {})),
        frozen_at=utcnow(),
        frozen_by_id=approved_by.id,
        created_in_revision=candidate.revision_number,
        updated_in_revision=candidate.revision_number,
        created_by_id=approved_by.id,
        approval_state=ObjectApprovalState.APPROVED,
        approved_at=utcnow(),
        approved_by_id=approved_by.id,
    )
    db.add(manifest)

    candidate.status = ReleaseCandidateStatus.APPROVED
    candidate.approval_state = ObjectApprovalState.APPROVED
    candidate.approved_at = manifest.frozen_at
    candidate.approved_by_id = approved_by.id
    db.flush()

    record_event(
        db,
        project_id=project.id,
        event_type=EventType.RELEASE_READY,
        actor_user_id=approved_by.id,
        payload={
            "release_candidate_id": str(candidate.id),
            "candidate_number": candidate.candidate_number,
            "revision_number": candidate.revision_number,
            "status": str(candidate.status),
            "location": SemanticLocation.RELEASE_LOGISTICS.value,
            "validation_level": str(candidate.validation_level),
            "import_verification_status": str(candidate.import_verification_status),
            "is_import_ready_candidate_only": candidate.is_import_ready_candidate_only,
            "import_verification_detail": candidate.import_verification_detail[:500],
            "artifact_count": manifest.artifact_count,
            "package_bytes": candidate.package_bytes,
            "package_sha256": candidate.package_sha256,
            "manifest_sha256": manifest.content_sha256,
            "approved_by_id": str(approved_by.id),
            "limitation_count": len(candidate.limitations),
            "unsupported_item_count": len(candidate.unsupported_items),
        },
    )
    audit.record(
        db,
        AuditAction.RELEASE_APPROVED,
        actor_user_id=approved_by.id,
        actor_label=approved_by.display_name,
        project_id=project.id,
        target_type="release_manifest",
        target_id=manifest.id,
        summary=(
            f"Release candidate {candidate.candidate_number} frozen by {approved_by.display_name}."
        ),
        details={
            "candidate_number": candidate.candidate_number,
            "revision_number": candidate.revision_number,
            "manifest_sha256": manifest.content_sha256,
            "checksum_file_sha256": manifest.checksum_file_sha256,
            "validation_level": str(candidate.validation_level),
            "import_verification_status": str(candidate.import_verification_status),
            "is_import_ready_candidate_only": candidate.is_import_ready_candidate_only,
        },
    )
    db.flush()
    return manifest


def was_forced_past_stale_revision(candidate: ReleaseCandidate) -> bool:
    """Whether this candidate carries permission to ship from a superseded revision.

    Read from the candidate's own limitations rather than from a flag, so the
    permission and the sentence the customer reads about it cannot come apart.
    """
    return any(
        str(entry.get("code", "")) == FORCED_STALE_REVISION_CODE
        for entry in candidate.limitations
        if isinstance(entry, Mapping)
    )


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------
def _profile_blocks(profile: ProjectProfile | None) -> list[ReleaseBlock]:
    """The three independent reasons a project profile forbids generating.

    :attr:`~app.models.profile.ProjectProfile.blocks_generation` is the single
    rule; this names *which* of its conditions applies, because "the profile
    blocks generation" is not something anyone can act on.
    """
    if profile is None:
        return [
            ReleaseBlock(
                code=BlockCode.NO_PROFILE,
                summary="This project has no profile, so nothing governs what may be generated.",
                detail=(
                    "The naming standard, alarm philosophy and template set that would "
                    "govern the output have not been recorded. Anything generated now "
                    "would be governed by a guess."
                ),
            )
        ]
    if not profile.blocks_generation:
        return []

    if profile.release_mode is ReleaseMode.REVIEW_ONLY:
        return [
            ReleaseBlock(
                code=BlockCode.REVIEW_ONLY,
                summary="This project is scoped to review and report only.",
                detail=(
                    "Its release mode is 'review_only', which was an explicit scoping "
                    "decision. Changing it is a profile change, not a release option."
                ),
            )
        ]
    if not profile.is_complete_for_generation:
        unusable = profile.unusable_required_fields
        named = ", ".join(
            f"{name} ({status.value})"
            for name, status in sorted(unusable.items())
            if status is not ProfileFieldStatus.PRESENT
        )
        return [
            ReleaseBlock(
                code=BlockCode.PROFILE_INCOMPLETE,
                summary="The project profile is not complete enough to generate from.",
                detail=(
                    f"{len(unusable)} required field(s) are not usable: {named or 'unnamed'}. "
                    "Each one is a question for a human, not a default this platform "
                    "may choose."
                ),
            )
        ]
    domains = ", ".join(sorted(str(domain) for domain in profile.restricted_domains))
    return [
        ReleaseBlock(
            code=BlockCode.RESTRICTED_DOMAIN,
            summary="This project covers a restricted engineering domain.",
            detail=(
                f"{domains} require a reviewer with a formal qualification, and the "
                f"project's restricted-domain mode is "
                f"'{profile.restricted_domain_mode.value}' rather than "
                f"'{RestrictedDomainMode.QUALIFIED_WORKFLOW_ENABLED.value}'. Enabling a "
                "qualified workflow is an administrator decision."
            ),
        )
    ]


def _controller_blocks(db: Session, project: Project) -> list[ReleaseBlock]:
    if _controller_of(db, project) is not None:
        return []
    return [
        ReleaseBlock(
            code=BlockCode.NO_CONTROLLER,
            summary="No controller has been recorded for this project.",
            detail=(
                "Every file in a Rockwell package is written into a controller context — "
                "its name, catalogue number and firmware revision. None of that can be "
                "invented, so there is nothing to generate against."
            ),
        )
    ]


def _validation_blocks(
    run: ValidationRun | None,
    revision: int,
    *,
    unwaived_errors: int,
    ambiguous_current_revision: bool = False,
    future_revision: int | None = None,
) -> list[ReleaseBlock]:
    """Why the newest validation run stops a release, or nothing if it does not.

    ``unwaived_errors`` rather than :attr:`ValidationRun.is_blocking` is the whole
    content of this signature, and it has no default deliberately: a caller that
    omitted it would get "nothing is in the way", and this function's failure
    direction has to be closed. ``error_count`` on the run is the total the
    validators found, and it stays the total because the package manifest records
    it. What a release turns on is narrower: an error a named reviewer has
    carried, with a stated reason, through
    :func:`app.core.services.validation_review.waive`, has been *decided* rather
    than left outstanding. Gates 2 to 4 already count it that way
    (:meth:`app.engineering.gates.GateContext.errors` filters waived results), so
    reading the raw total here made a project able to pass every gate on accepted
    risk and then be refused a package for the errors those gates had just
    settled — the waiver mechanism blocking on the thing it exists to permit.
    """
    if future_revision is not None:
        return [
            ReleaseBlock(
                code=BlockCode.NO_VALIDATION_RUN,
                summary="The validation history claims a future project revision.",
                detail=(
                    f"The leading completed run claims revision {future_revision}; the project "
                    f"is at revision {revision}. A future-revision run cannot be authoritative. "
                    "Repair the validation history and run validation again at the current "
                    "revision."
                ),
            )
        ]
    if ambiguous_current_revision:
        return [
            ReleaseBlock(
                code=BlockCode.NO_VALIDATION_RUN,
                summary="No authoritative validation run can be identified.",
                detail=(
                    f"Two completed runs for revision {revision} share the same recorded "
                    "creation time. Neither can safely supersede the other; run validation "
                    "again before creating a release candidate."
                ),
            )
        ]
    if run is None:
        return [
            ReleaseBlock(
                code=BlockCode.NO_VALIDATION_RUN,
                summary="No validation run has completed for this project.",
                detail=(
                    "A package generated from a model nobody checked would carry no "
                    "statement about what was checked, which is the one thing the "
                    "manifest exists to record."
                ),
            )
        ]
    if run.revision_number != revision:
        return [
            ReleaseBlock(
                code=BlockCode.NO_VALIDATION_RUN,
                summary="The newest validation run is older than the model.",
                detail=(
                    f"The run covers revision {run.revision_number}; the project is at "
                    f"revision {revision}. A clean result against an older revision says "
                    "nothing about the one being released."
                ),
            )
        ]
    if unwaived_errors <= 0:
        return []
    carried = max(int(run.error_count or 0) - unwaived_errors, 0)
    return [
        ReleaseBlock(
            code=BlockCode.VALIDATION_ERRORS,
            summary="The model has unresolved validation errors.",
            detail=(
                f"Validation run at revision {run.revision_number} reported "
                f"{unwaived_errors} error(s) that nobody has resolved or waived"
                + (f", alongside {carried} a reviewer has carried" if carried else "")
                + ". Generating from a model with errors produces a package that fails at "
                "import for a reason already known."
            ),
        )
    ]


def _unwaived_error_count(db: Session, run: ValidationRun | None) -> int:
    """Errors in ``run`` that no reviewer has carried.

    Counted with a query rather than from ``run.error_count`` because a waiver is
    recorded on the result long after the run computed its totals, and because
    those totals are what the manifest reports and must stay the totals.
    """
    if run is None:
        return 0
    return int(
        db.execute(
            select(func.count())
            .select_from(ValidationResult)
            .where(
                ValidationResult.validation_run_id == run.id,
                ValidationResult.severity == ValidationSeverity.ERROR,
                ValidationResult.is_waived.is_(False),
            )
        ).scalar_one()
        or 0
    )


def _gate_block(outstanding: Sequence[ApprovalGateKey]) -> ReleaseBlock:
    named = ", ".join(str(key) for key in outstanding)
    return ReleaseBlock(
        code=BlockCode.GATE_UNDECIDED,
        summary="An approval gate covering this work is still open.",
        detail=(
            f"{len(outstanding)} gate(s) are undecided: {named}. A gate is where a human "
            "takes responsibility for a body of engineering work, and a release that "
            "walked past one records a sign-off nobody gave."
        ),
    )


def _candidate_status_blocks(
    candidate: ReleaseCandidate,
    allowed: frozenset[ReleaseCandidateStatus],
    action: str,
) -> list[ReleaseBlock]:
    if candidate.status in allowed:
        return []
    return [
        ReleaseBlock(
            code=BlockCode.CANDIDATE_STATUS,
            summary=f"A candidate in this state cannot be {action}.",
            detail=(
                f"Release candidate {candidate.candidate_number} is "
                f"'{candidate.status.value}'. Allowed here: "
                f"{', '.join(sorted(status.value for status in allowed))}."
            ),
        )
    ]


def _staleness_blocks(
    db: Session, project: Project, candidate: ReleaseCandidate
) -> list[ReleaseBlock]:
    """Refuse a candidate the project has moved past, unless it was forced.

    The alternative to refusing is regenerating from the head revision, which
    this module deliberately does not do behind the caller's back: a candidate
    number is human-facing and "RC 3" must keep meaning the revision it was cut
    from. So the choice — refuse, or create a new candidate — stays with whoever
    is holding it.
    """
    head = current_revision_number(db, project.id)
    if candidate.revision_number >= head:
        return []
    if was_forced_past_stale_revision(candidate):
        logger.info(
            "release candidate proceeding from a superseded revision under a recorded force",
            extra={
                "project_id": str(project.id),
                "candidate_number": candidate.candidate_number,
                "candidate_revision": candidate.revision_number,
                "head_revision": head,
            },
        )
        return []
    return [
        ReleaseBlock(
            code=BlockCode.STALE_REVISION,
            summary="This candidate was cut from a revision the project has moved past.",
            detail=(
                f"Release candidate {candidate.candidate_number} is pinned to revision "
                f"{candidate.revision_number}; the project is at revision {head}. Create a "
                "candidate at the current revision, or create one with force=True to "
                "record deliberately shipping the older model."
            ),
        )
    ]


def _forced_limitation(revision_number: int) -> Limitation:
    return Limitation(
        code=FORCED_STALE_REVISION_CODE,
        summary="This package may be generated from a revision the project has moved past.",
        detail=(
            f"A named human accepted, when this candidate was created at revision "
            f"{revision_number}, that it may be generated and frozen even after later "
            "work has been merged. Anything merged after that revision is not in these "
            "files. Compare against the change log before importing."
        ),
    )


def _record_generation_refusal(
    db: Session,
    *,
    project: Project,
    candidate: ReleaseCandidate,
    exc: GenerationRefused,
) -> ReleaseCandidate:
    """Turn a generator refusal into a recorded failure on the candidate.

    Returned rather than raised. The generator has made an engineering
    statement — this object, in this place, would not import — and that belongs
    on the candidate where a reviewer will look for it, not only in a stack
    trace. Nothing was written to disk: the package is assembled in memory and
    the refusal happened before any of it reached the filesystem.
    """
    candidate.status = ReleaseCandidateStatus.VALIDATION_FAILED
    candidate.notes = exc.message
    candidate.unsupported_items = [
        {
            "kind": str(exc.reason),
            "subject": exc.subject,
            "detail": exc.detail,
            "location": exc.location,
        }
    ]
    candidate.package_path = ""
    candidate.package_sha256 = ""
    candidate.package_bytes = 0
    db.flush()

    audit.record(
        db,
        AuditAction.EXPORT_GENERATED,
        project_id=project.id,
        target_type="release_candidate",
        target_id=candidate.id,
        summary=(
            f"Release candidate {candidate.candidate_number} refused: {exc.subject} at "
            f"{exc.location}."
        ),
        details={
            "outcome": "refused",
            "reason": str(exc.reason),
            "subject": exc.subject,
            "location": exc.location,
            "detail": exc.detail,
            "revision_number": candidate.revision_number,
        },
    )
    db.flush()
    logger.info(
        "release generation refused",
        extra={
            "project_id": str(project.id),
            "candidate_number": candidate.candidate_number,
            "reason": str(exc.reason),
            "subject": exc.subject,
            "location": exc.location,
        },
    )
    return candidate


# ---------------------------------------------------------------------------
# Writing, and reading back
# ---------------------------------------------------------------------------
def _release_dir(storage: ProjectStorage, candidate: ReleaseCandidate) -> Path:
    """One directory per candidate, named after the number people will quote.

    Zero-padded so a directory listing sorts the way an engineer reads it, and
    resolved through :func:`~app.security.paths.resolve_within` like every other
    path in the store.
    """
    return resolve_within(storage.root, RELEASES_DIR, f"RC-{candidate.candidate_number:04d}")


def _write_package(
    storage: ProjectStorage, *, candidate: ReleaseCandidate, package: ReleasePackage
) -> Path:
    """Write the package, leaving nothing behind if the write does not finish.

    The candidate's directory is cleared first. A regeneration produces
    byte-identical files, so overwriting is harmless — but a *different*
    generation, after the model changed, would otherwise leave a file from the
    previous run sitting in the package with a checksum nothing references.
    """
    storage.ensure_layout()
    release_dir = _release_dir(storage, candidate)
    if release_dir.exists():
        shutil.rmtree(release_dir)
    release_dir.mkdir(parents=True, exist_ok=True)
    try:
        return package.write(release_dir)
    except BaseException:
        shutil.rmtree(release_dir, ignore_errors=True)
        raise


def _relative_package_path(storage: ProjectStorage, root: Path) -> str:
    """The package root as a POSIX path relative to the project store.

    Stored relative because an absolute server path in a database row leaks the
    deployment layout to anything that reads the row back, and because a project
    store that is moved must not invalidate every candidate in it.
    """
    return root.resolve().relative_to(storage.root.resolve()).as_posix()


def _checksum_file_hash(package: ReleasePackage) -> str:
    return package.file_at(f"{MANIFEST_DIR}/checksums.sha256").sha256


def _weakest_level(package: ReleasePackage) -> ValidationLevel:
    """The level the package as a whole reached, which is its weakest file's.

    :attr:`~app.generators.rockwell.package.ReleasePackage.validation_level`
    already computes this; it is restated here because the rule belongs to the
    release record, and a future package format that forgot it would otherwise
    silently promote the candidate.
    """
    levels = [file.validation_level for file in package.files] or [ValidationLevel.GENERATED]
    return min(levels, key=lambda level: VALIDATION_LEVEL_ORDER[level])


def _apply_verification_status(candidate: ReleaseCandidate, *, settings: Settings) -> None:
    """Take the import-verification status from the adapter, never from a default.

    :meth:`~app.generators.rockwell.package.ReleasePackage.apply_to_candidate`
    leaves ``NOT_ATTEMPTED``, which is right for a generator that has no idea
    whether a workstation exists. Here we do know. With no adapter configured
    the honest answer is ``UNAVAILABLE`` carrying the adapter's own reason:
    ``NOT_ATTEMPTED`` on something nobody could ever attempt reads as "fine so
    far", and that is the overclaim this whole level scale exists to prevent.
    """
    health = get_adapter(settings).health()
    if health.available:
        candidate.import_verification_status = ImportVerificationStatus.NOT_ATTEMPTED
        candidate.import_verification_detail = (
            f"A licensed workstation adapter ({health.adapter_key or 'unnamed'}) is "
            "configured, but no verification job has run against this candidate. The "
            "package remains an import-ready candidate until one passes."
        )
        return
    candidate.import_verification_status = ImportVerificationStatus.UNAVAILABLE
    candidate.import_verification_detail = health.detail


def _record_target_versions(candidate: ReleaseCandidate, platform: TargetPlatform | None) -> None:
    """Add the platform identity to the target snapshot the package wrote.

    The generator's snapshot describes the *export* — controller family, schema
    revision, Studio version it was written for. Gate 5 compares a package
    against the platform the project currently records, and it needs the three
    fields that identify a controller to do it. Recorded here, at generation,
    because that is the only moment the two are known to agree.
    """
    if platform is None:
        return
    snapshot = dict(candidate.target_profile_snapshot)
    snapshot.update(
        {
            "controller_catalog_number": platform.controller_catalog_number,
            "firmware_revision": platform.firmware_revision,
            "studio_5000_version": platform.studio_5000_version,
        }
    )
    candidate.target_profile_snapshot = snapshot


def _replace_artifact_rows(
    db: Session, *, project: Project, candidate: ReleaseCandidate, package: ReleasePackage
) -> None:
    """Rebuild the artifact and dependency rows for this candidate.

    Deleted and rewritten rather than merged. Import dependencies are edges in a
    graph that is recomputed wholesale with every generation, and a stale edge
    pointing at an artifact that no longer exists would send a customer looking
    for a file that was never written.
    """
    existing = list(
        db.execute(
            select(ImportDependency).where(ImportDependency.release_candidate_id == candidate.id)
        ).scalars()
    )
    for row in existing:
        db.delete(row)
    for artifact in list(candidate.artifacts):
        db.delete(artifact)
    db.flush()

    artifacts = package.artifact_rows(project_id=project.id, release_candidate_id=candidate.id)
    for artifact in artifacts:
        artifact.created_in_revision = candidate.revision_number
        artifact.updated_in_revision = candidate.revision_number
        db.add(artifact)
    db.flush()

    ids = {artifact.relative_path: artifact.id for artifact in artifacts}
    for dependency in package.dependency_rows(
        ids, project_id=project.id, release_candidate_id=candidate.id
    ):
        db.add(dependency)
    db.flush()
    db.expire(candidate, ["artifacts"])


@dataclass(frozen=True, slots=True)
class _ManifestDocuments:
    """The three package documents a manifest row is built from, read from disk."""

    content: dict[str, Any]
    content_sha256: str
    checksum_file_sha256: str
    evidence_index_sha256: str


def _reread_manifest_documents(
    candidate: ReleaseCandidate, settings: Settings
) -> _ManifestDocuments:
    """Re-read the whole package from disk and check it against what was recorded.

    Every artifact, not only the three documents the manifest row is built from.
    Hashing the checksum file alone would prove the *list* is intact while a
    changed ``Controller_Tags.csv`` sailed past, and the freeze is the moment a
    named human puts their name to these bytes. A file that has changed since
    generation is refused rather than re-hashed: a manifest quietly describing
    whatever is on disk today would make the signature meaningless, and the
    difference between "the package we generated" and "the package that is there
    now" is precisely what a customer will one day need established.
    """
    storage = ProjectStorage(candidate.project_id, settings)
    if not candidate.package_path or not candidate.artifacts:
        raise ReleaseRefused(
            [
                ReleaseBlock(
                    code=BlockCode.PACKAGE_MISSING,
                    summary="This candidate has no package on disk.",
                    detail=(
                        f"Release candidate {candidate.candidate_number} records no package "
                        "path or no artifacts, so there is nothing to freeze a manifest over."
                    ),
                )
            ]
        )

    root = resolve_within(storage.root, candidate.package_path)
    payloads: dict[str, bytes] = {}
    hashes: dict[str, str] = {}
    for artifact in candidate.artifacts:
        file_path = resolve_within(root, artifact.relative_path)
        if not file_path.is_file():
            raise ReleaseRefused(
                [
                    ReleaseBlock(
                        code=BlockCode.PACKAGE_MISSING,
                        summary="A file this candidate recorded is missing from the package.",
                        detail=(
                            f"{artifact.relative_path} is not present under "
                            f"{candidate.package_path}."
                        ),
                    )
                ]
            )
        data = file_path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if artifact.sha256 and artifact.sha256 != digest:
            raise ReleaseRefused(
                [
                    ReleaseBlock(
                        code=BlockCode.PACKAGE_ALTERED,
                        summary="A package file no longer matches the checksum recorded for it.",
                        detail=(
                            f"{artifact.relative_path} hashes to {digest}; generation recorded "
                            f"{artifact.sha256}. Regenerate the candidate rather than signing "
                            "files nobody can account for."
                        ),
                    )
                ]
            )
        payloads[artifact.relative_path] = data
        hashes[artifact.relative_path] = digest

    manifest_path = f"{MANIFEST_DIR}/manifest.json"
    checksums_path = f"{MANIFEST_DIR}/checksums.sha256"
    evidence_path = f"{AUDIT_DIR}/evidence_index.json"
    missing = [
        name for name in (manifest_path, checksums_path, evidence_path) if name not in hashes
    ]
    if missing:
        raise ReleaseRefused(
            [
                ReleaseBlock(
                    code=BlockCode.PACKAGE_MISSING,
                    summary="A document the manifest is built from was never recorded.",
                    detail=(
                        f"{', '.join(missing)} has no artifact row on release candidate "
                        f"{candidate.candidate_number}, so the package cannot describe itself."
                    ),
                )
            ]
        )

    content = json.loads(payloads[manifest_path].decode("utf-8"))
    return _ManifestDocuments(
        content=content,
        content_sha256=hashes[manifest_path],
        checksum_file_sha256=hashes[checksums_path],
        evidence_index_sha256=hashes[evidence_path],
    )


# ---------------------------------------------------------------------------
# Loading the model the generators read
# ---------------------------------------------------------------------------
def _load_project_model(db: Session, project: Project, *, revision_number: int) -> ProjectModel:
    """Assemble the slice of the canonical model a package is generated from.

    Everything is sorted explicitly and soft-deleted rows are excluded. Both
    matter for the same reason: the generators promise byte-identical output for
    one revision, and a collection whose order came from whatever the database
    returned — or that contained an instruction somebody deleted — would break
    that promise silently.
    """
    controller = _controller_of(db, project)
    if controller is None:
        raise ReleaseRefused(_controller_blocks(db, project))

    programs = _live(db, project, Program, order=(Program.ordinal, Program.name))
    routines = _live(db, project, Routine, order=(Routine.ordinal, Routine.name))
    rungs = _live(db, project, LogicRung, order=(LogicRung.ordinal,))
    _attach_live_logic(db, project, rungs)

    routines_by_program: dict[uuid.UUID, list[Routine]] = {}
    for routine in routines:
        routines_by_program.setdefault(routine.program_id, []).append(routine)
    rungs_by_routine: dict[uuid.UUID, list[LogicRung]] = {}
    for rung in rungs:
        rungs_by_routine.setdefault(rung.routine_id, []).append(rung)

    tags = _live(db, project, Tag, order=(Tag.name,))
    program_tags: dict[uuid.UUID, list[Tag]] = {}
    controller_tags: list[Tag] = []
    for tag in tags:
        if tag.scope is TagScope.PROGRAM and tag.scope_program_id is not None:
            program_tags.setdefault(tag.scope_program_id, []).append(tag)
        elif tag.scope is TagScope.CONTROLLER:
            controller_tags.append(tag)

    tasks = _live(db, project, ControllerTask, order=(ControllerTask.ordinal, ControllerTask.name))
    programs_by_task: dict[uuid.UUID, list[str]] = {}
    for program in programs:
        if program.controller_task_id is not None:
            programs_by_task.setdefault(program.controller_task_id, []).append(program.name)

    udts = _live(db, project, UDTDefinition, order=(UDTDefinition.name,))
    members = _live(db, project, UDTMember, order=(UDTMember.ordinal, UDTMember.name))
    members_by_udt: dict[uuid.UUID, list[UDTMember]] = {}
    for member in members:
        members_by_udt.setdefault(member.udt_id, []).append(member)

    return ProjectModel(
        project_name=project.name,
        revision_number=revision_number,
        controller=controller,
        tasks=tuple(
            TaskContent(
                task=task,
                scheduled_program_names=tuple(sorted(programs_by_task.get(task.id, []))),
            )
            for task in tasks
        ),
        programs=tuple(
            ProgramContent(
                program=program,
                tags=tuple(program_tags.get(program.id, [])),
                routines=tuple(
                    RoutineContent(
                        routine=routine,
                        rungs=tuple(rungs_by_routine.get(routine.id, [])),
                    )
                    for routine in routines_by_program.get(program.id, [])
                ),
            )
            for program in programs
        ),
        controller_tags=tuple(controller_tags),
        udts=tuple(
            UdtContent(udt=udt, members=tuple(members_by_udt.get(udt.id, []))) for udt in udts
        ),
        aoi_references=tuple(
            _live(db, project, AOIDefinitionReference, order=(AOIDefinitionReference.name,))
        ),
        modules=tuple(_live(db, project, HardwareModule, order=(HardwareModule.name,))),
        logic_references=tuple(
            db.execute(
                select(LogicReference)
                .where(LogicReference.project_id == project.id)
                .order_by(LogicReference.tag_name, LogicReference.id)
            ).scalars()
        ),
        operand_comments=_operand_comments(db, project, programs, routines, rungs),
    )


def _attach_live_logic(db: Session, project: Project, rungs: Sequence[LogicRung]) -> None:
    """Install undeleted instruction and operand lists onto the loaded rungs.

    The relationships do not filter ``deleted_at``, and the generator renders a
    rung from ``rung.instructions``. A soft-deleted instruction reaching a
    generated file would be this platform emitting logic somebody removed —
    which is the same class of failure as silently altering approved logic, and
    it would be invisible in the output.

    ``set_committed_value`` installs the collection as though it had just been
    loaded, so nothing is marked dirty and no write is implied.
    """
    instructions = _live(
        db, project, LogicInstruction, order=(LogicInstruction.rung_id, LogicInstruction.ordinal)
    )
    operands = _live(
        db, project, LogicOperand, order=(LogicOperand.instruction_id, LogicOperand.ordinal)
    )

    operands_by_instruction: dict[uuid.UUID, list[LogicOperand]] = {}
    for operand in operands:
        operands_by_instruction.setdefault(operand.instruction_id, []).append(operand)
    instructions_by_rung: dict[uuid.UUID, list[LogicInstruction]] = {}
    for instruction in instructions:
        instructions_by_rung.setdefault(instruction.rung_id, []).append(instruction)
        set_committed_value(
            instruction, "operands", operands_by_instruction.get(instruction.id, [])
        )
    for rung in rungs:
        set_committed_value(rung, "instructions", instructions_by_rung.get(rung.id, []))


def _operand_comments(
    db: Session,
    project: Project,
    programs: Sequence[Program],
    routines: Sequence[Routine],
    rungs: Sequence[LogicRung],
) -> tuple[tuple[str, str, int, LogicComment], ...]:
    """Operand comments with their location resolved to names an engineer reads.

    A comment nobody can locate is a comment nobody acts on, and the CSV row has
    to keep working after the rung it came from has been renumbered — hence the
    program and routine names travel with it rather than an id.
    """
    program_names = {program.id: program.name for program in programs}
    routine_locations = {
        routine.id: (program_names.get(routine.program_id, ""), routine.name)
        for routine in routines
    }
    rung_locations = {
        rung.id: (*routine_locations.get(rung.routine_id, ("", "")), rung.ordinal) for rung in rungs
    }

    comments = _live(db, project, LogicComment, order=(LogicComment.rung_id, LogicComment.id))
    located: list[tuple[str, str, int, LogicComment]] = []
    for comment in comments:
        location = rung_locations.get(comment.rung_id)
        if location is None:
            # The rung was deleted; the comment row survives as a tombstone. It
            # cannot be placed in a file, and inventing a location for it would
            # be worse than leaving it out of the export.
            continue
        program_name, routine_name, ordinal = location
        located.append((program_name, routine_name, ordinal, comment))
    return tuple(located)


# ---------------------------------------------------------------------------
# What the package says about itself
# ---------------------------------------------------------------------------
def _target_profile(
    db: Session, project: Project, export_profile: ExportProfile | None
) -> TargetProfile:
    """The target platform as it stands, snapshotted into the package.

    The export profile wins where it states something, because it is the record
    of *this* generation run; the project's target platform supplies the rest.
    Nothing is defaulted from a clock: ``export_date`` stays empty unless a
    caller supplied one that is itself part of the approved input, since a date
    read at generation time would make two runs over one revision differ.
    """
    platform = _target_platform_of(db, project)
    profile = _profile_of(db, project)
    family = ControllerFamily.OTHER
    if (
        export_profile is not None
        and export_profile.controller_family is not ControllerFamily.OTHER
    ):
        family = export_profile.controller_family
    elif platform is not None:
        family = platform.controller_family

    studio_version = ""
    if export_profile is not None and export_profile.target_studio_version:
        studio_version = export_profile.target_studio_version
    elif platform is not None:
        studio_version = platform.studio_5000_version

    schema_revision = "1.0"
    if export_profile is not None and export_profile.l5x_schema_revision:
        schema_revision = export_profile.l5x_schema_revision

    modes: tuple[tuple[str, str], ...] = ()
    if profile is not None:
        modes = tuple(
            (str(domain), profile.restricted_domain_mode.value)
            for domain in sorted(str(item) for item in profile.restricted_domains)
        )

    return TargetProfile(
        controller_family=family,
        target_studio_version=studio_version,
        l5x_schema_revision=schema_revision,
        software_revision=studio_version,
        export_date="",
        export_profile_key=export_profile.key if export_profile is not None else "",
        restricted_domain_modes=modes,
    )


def _package_options(
    db: Session,
    *,
    project: Project,
    candidate: ReleaseCandidate,
    model: ProjectModel,
    target: TargetProfile,
    export_profile: ExportProfile | None,
    settings: Settings,
) -> PackageOptions:
    """Everything about the package that is not the engineering model itself.

    ``settings`` is passed through so every emitted L5X is parsed back and can
    honestly reach ``ROUND_TRIP_PARSED``. Withholding it would leave every file
    at ``GENERATED`` — which is not a safer answer, it is a less true one.
    """
    run = _latest_completed_run(db, project)
    labels = _object_labels(model)
    return PackageOptions(
        target=target,
        validation=(
            ValidationSummary.from_run(run)
            if run is not None
            else ValidationSummary(revision_number=candidate.revision_number)
        ),
        approvals=_approval_records(db, project),
        change_log=_change_log(db, project, candidate.revision_number, labels),
        evidence_index=_evidence_index(db, project, labels),
        extra_limitations=_extra_limitations(candidate),
        delivery_slots=_delivery_slots(export_profile),
        settings=settings,
    )


def _extra_limitations(candidate: ReleaseCandidate) -> tuple[Limitation, ...]:
    """Limitations the release service adds to whatever the generator states.

    Currently just the forced-stale-revision record, re-rendered from the
    candidate so the sentence inside the package and the sentence on the row are
    the same sentence.
    """
    if not was_forced_past_stale_revision(candidate):
        return ()
    return (_forced_limitation(candidate.revision_number),)


def _delivery_slots(export_profile: ExportProfile | None) -> tuple[DeliverySlot, ...]:
    """The partner-plugin directories, each carrying why it is empty.

    An empty folder with no explanation reads as "nothing was needed", which is
    a claim nobody made. Whether the profile excluded delivery artifacts and
    whether no plugin produced any are different facts and are stated as such.
    """
    excluded = export_profile is not None and not export_profile.include_delivery_artifacts
    reason = (
        "The export profile for this run excludes partner-plugin deliverables, so none "
        "were requested."
        if excluded
        else "No delivery plugin produced anything for this discipline in this run. That "
        "is an absence, not a statement that nothing is needed."
    )
    return tuple(
        DeliverySlot(
            directory=slot.directory,
            discipline=slot.discipline,
            plugin_name=slot.plugin_name,
            is_present=False,
            absence_reason=reason,
        )
        for slot in DEFAULT_DELIVERY_SLOTS
    )


def _object_labels(model: ProjectModel) -> dict[uuid.UUID, str]:
    """Labels for every object the package was generated from.

    Built from the model already in memory rather than queried per citation:
    the evidence index and the change log both need "what is this object called"
    for hundreds of rows, and forty round trips per file is how a release run
    becomes a coffee break.
    """
    labels: dict[uuid.UUID, str] = {}

    def remember(obj: Any) -> None:
        identifier = getattr(obj, "id", None)
        if isinstance(identifier, uuid.UUID):
            labels[identifier] = registry.label_for(obj)

    remember(model.controller)
    for entry in model.tasks:
        remember(entry.task)
    for program in model.programs:
        remember(program.program)
        for tag in program.tags:
            remember(tag)
        for routine in program.routines:
            remember(routine.routine)
            for rung in routine.rungs:
                remember(rung)
    for tag in model.controller_tags:
        remember(tag)
    for udt in model.udts:
        remember(udt.udt)
        for member in udt.members:
            remember(member)
    for aoi in model.aoi_references:
        remember(aoi)
    for module in model.modules:
        remember(module)
    return labels


def _approval_records(db: Session, project: Project) -> tuple[ApprovalRecord, ...]:
    """Every settled human decision on this project, denormalised into the package.

    ``approved_at`` is rendered from the stored decision rather than read from a
    clock: an approval timestamp is a fact about something that already
    happened, and reading the clock here would also break the byte-identity the
    manifest checksums depend on.
    """
    rows = list(
        db.execute(
            select(Approval)
            .where(
                Approval.project_id == project.id,
                Approval.status.in_(sorted(_SETTLED_APPROVALS, key=str)),
            )
            .order_by(Approval.created_at, Approval.id)
        ).scalars()
    )
    names = _user_names(db, [row.decided_by_id for row in rows])
    return tuple(
        ApprovalRecord(
            object_type=str(row.kind),
            object_label=row.title,
            decision=str(row.decision) if row.decision is not None else str(row.status),
            approver=names.get(row.decided_by_id, "") if row.decided_by_id else "",
            approved_at=isoformat_z(row.decided_at) if row.decided_at is not None else "",
            note=row.decision_notes,
        )
        for row in rows
    )


def _change_log(
    db: Session, project: Project, revision_number: int, labels: Mapping[uuid.UUID, str]
) -> tuple[ChangeLogEntry, ...]:
    """Everything that landed in the model up to and including this revision.

    Read from the applied change operations rather than reconstructed by
    diffing two model states, because the operations merged between two
    revisions *are* the difference between them — that is the premise the whole
    change-set engine rests on, and re-deriving it here would give the package
    a second, disagreeing answer.
    """
    rows = list(
        db.execute(
            select(ChangeSet, ChangeOperation)
            .join(ChangeOperation, ChangeOperation.change_set_id == ChangeSet.id)
            .where(
                ChangeSet.project_id == project.id,
                ChangeSet.merged_revision_number.is_not(None),
                ChangeSet.merged_revision_number <= revision_number,
                ChangeOperation.applied.is_(True),
            )
            .order_by(
                ChangeSet.merged_revision_number,
                ChangeSet.id,
                ChangeOperation.ordinal,
            )
        ).all()
    )
    names = _user_names(db, [change_set.author_user_id for change_set, _ in rows])
    return tuple(
        ChangeLogEntry(
            revision_number=int(change_set.merged_revision_number or 0),
            operation=str(operation.operation_type),
            object_type=str(operation.object_type),
            object_label=_label_of(operation.object_id, operation.object_type, labels),
            field_name=operation.field_name,
            before=_render_value(operation.before, operation.field_name),
            after=_render_value(operation.after, operation.field_name),
            author=(
                names.get(change_set.author_user_id, "")
                if change_set.author_user_id
                else change_set.author_agent
            ),
        )
        for change_set, operation in rows
    )


def _evidence_index(
    db: Session, project: Project, labels: Mapping[uuid.UUID, str]
) -> tuple[EvidenceIndexEntry, ...]:
    """Every generated object and the basis it was generated from.

    Shipped in the package because "every generated object cites a basis" is
    only checkable by a customer if the citations travel with the files. A link
    whose basis cannot be resolved is still written, carrying its own note: the
    fact that a basis was recorded is worth more than the fact that this reader
    could not follow it.
    """
    links = list(
        db.execute(
            select(ObjectEvidenceLink)
            .where(ObjectEvidenceLink.project_id == project.id)
            .order_by(
                ObjectEvidenceLink.object_type,
                ObjectEvidenceLink.object_id,
                ObjectEvidenceLink.id,
            )
        ).scalars()
    )
    if not links:
        return ()

    anchors = {
        anchor.id: anchor
        for anchor in db.execute(
            select(EvidenceAnchor).where(
                EvidenceAnchor.id.in_({link.anchor_id for link in links if link.anchor_id})
            )
        ).scalars()
    }
    requirements = {
        item.id: item
        for item in db.execute(
            select(Requirement).where(
                Requirement.id.in_({link.requirement_id for link in links if link.requirement_id})
            )
        ).scalars()
    }
    decisions = {
        item.id: item
        for item in db.execute(
            select(ProjectDecision).where(
                ProjectDecision.id.in_({link.decision_id for link in links if link.decision_id})
            )
        ).scalars()
    }

    entries: list[EvidenceIndexEntry] = []
    for link in links:
        basis_label = link.note
        source_label = ""
        anchor_key = ""
        if link.anchor_id is not None and link.anchor_id in anchors:
            anchor = anchors[link.anchor_id]
            basis_label = basis_label or anchor.display_location
            source_label = anchor.source_label
            anchor_key = anchor.anchor_key
        elif link.requirement_id is not None and link.requirement_id in requirements:
            requirement = requirements[link.requirement_id]
            basis_label = f"{requirement.reference} {requirement.title}".strip()
        elif link.decision_id is not None and link.decision_id in decisions:
            decision = decisions[link.decision_id]
            basis_label = f"{decision.reference} {decision.title}".strip()
        elif link.template_id:
            basis_label = basis_label or link.template_id

        entries.append(
            EvidenceIndexEntry(
                object_type=str(link.object_type),
                object_label=_label_of(link.object_id, link.object_type, labels),
                basis_kind=str(link.basis_kind),
                basis_label=basis_label,
                source_label=source_label,
                anchor_key=anchor_key,
            )
        )
    return tuple(entries)


# ---------------------------------------------------------------------------
# Small shared reads
# ---------------------------------------------------------------------------
def _live[ModelT: EngineeringObjectMixin](
    db: Session,
    project: Project,
    model: type[ModelT],
    *,
    order: tuple[Any, ...],
) -> list[ModelT]:
    """Undeleted rows of ``model`` for this project, in an order stated here.

    ``id`` is appended to every ordering so two rows that tie on the stated key
    still come back in the same order on every run. Without it a package could
    differ between two generations of one revision for no reason anybody could
    see, which is exactly the failure the checksums are supposed to catch.
    """
    statement = (
        select(model)
        .where(model.project_id == project.id, model.deleted_at.is_(None))
        .order_by(*order, model.id)  # type: ignore[attr-defined]
    )
    return list(db.execute(statement).scalars())


def _controller_of(db: Session, project: Project) -> ControllerModel | None:
    return db.execute(
        select(ControllerModel).where(
            ControllerModel.project_id == project.id, ControllerModel.deleted_at.is_(None)
        )
    ).scalar_one_or_none()


def _profile_of(db: Session, project: Project) -> ProjectProfile | None:
    return db.execute(
        select(ProjectProfile).where(
            ProjectProfile.project_id == project.id, ProjectProfile.deleted_at.is_(None)
        )
    ).scalar_one_or_none()


def _target_platform_of(db: Session, project: Project) -> TargetPlatform | None:
    return db.execute(
        select(TargetPlatform)
        .where(TargetPlatform.project_id == project.id, TargetPlatform.deleted_at.is_(None))
        .order_by(TargetPlatform.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()


def _project_of(db: Session, candidate: ReleaseCandidate) -> Project:
    project = db.get(Project, candidate.project_id)
    if project is None:
        raise NotFoundError("Project", candidate.project_id)
    return project


def _default_export_profile(db: Session, project: Project) -> ExportProfile | None:
    return db.execute(
        select(ExportProfile)
        .where(
            ExportProfile.project_id == project.id,
            ExportProfile.deleted_at.is_(None),
            ExportProfile.is_default.is_(True),
        )
        .order_by(ExportProfile.created_at, ExportProfile.id)
        .limit(1)
    ).scalar_one_or_none()


def _manifest_of(db: Session, candidate: ReleaseCandidate) -> ReleaseManifest | None:
    """Queried rather than read off ``candidate.manifest``.

    The relationship is loaded once per instance and does not notice a row added
    afterwards in the same session — which is precisely the shape of a second
    freeze attempt in one request, the case the immutability rule exists for.
    """
    return db.execute(
        select(ReleaseManifest).where(ReleaseManifest.release_candidate_id == candidate.id)
    ).scalar_one_or_none()


def _export_profile_of(db: Session, candidate: ReleaseCandidate) -> ExportProfile | None:
    if candidate.export_profile_id is None:
        return None
    return db.get(ExportProfile, candidate.export_profile_id)


def _latest_completed_run_selection(
    db: Session,
    project: Project,
    *,
    revision_number: int | None = None,
) -> _ValidationRunSelection:
    """The newest completed run. A run still going or failed says nothing.

    Same rule and same ordering as
    :attr:`~app.engineering.gates.GateContext.validation_run`, so the gate and
    the release service cannot form different opinions about which run counts.
    """
    rows = tuple(
        db.execute(
            select(ValidationRun)
            .where(
                ValidationRun.project_id == project.id,
                ValidationRun.status == ValidationRunStatus.COMPLETED,
                ValidationRun.deleted_at.is_(None),
            )
            .order_by(
                ValidationRun.revision_number.desc(),
                ValidationRun.created_at.desc(),
                ValidationRun.id.desc(),
            )
            .limit(2)
        ).scalars()
    )
    if not rows:
        return _ValidationRunSelection(run=None)

    revision = (
        current_revision_number(db, project.id) if revision_number is None else revision_number
    )
    newest = rows[0]
    if newest.revision_number > revision:
        return _ValidationRunSelection(run=None, future_revision=newest.revision_number)
    ambiguous = (
        len(rows) > 1
        and newest.revision_number == revision
        and rows[1].revision_number == revision
        and ensure_utc(newest.created_at) == ensure_utc(rows[1].created_at)
    )
    return _ValidationRunSelection(
        run=None if ambiguous else newest,
        ambiguous_current_revision=ambiguous,
    )


def _latest_completed_run(db: Session, project: Project) -> ValidationRun | None:
    return _latest_completed_run_selection(db, project).run


def _latest_completed_run_id(db: Session, project: Project) -> uuid.UUID | None:
    run = _latest_completed_run(db, project)
    return run.id if run is not None else None


def _outstanding_gates(
    db: Session, project: Project, up_to: ApprovalGateKey
) -> tuple[ApprovalGateKey, ...]:
    """Every enabled gate at or before ``up_to`` that is not settled, in gate order.

    :func:`~app.engineering.gates.gate_blocking` answers with the first one,
    which is the right answer for "may I proceed". A UI chasing sign-offs needs
    the whole list, and both have to come from one evaluation: two calls could
    straddle a merge and disagree about which gates are open.

    "Settled" is ``PASSED`` or ``SKIPPED``, matching ``gate_blocking`` exactly.
    ``FAILED`` is not settled — a gate a reviewer actively refused is the
    strongest block there is.
    """
    ordinal = gates.spec_for(up_to).ordinal
    evaluated = gates.evaluate_all(db, project=project)
    return tuple(
        gate.gate_key
        for gate in evaluated[: ordinal + 1]
        if gate.is_enabled and gate.status not in {GateStatus.PASSED, GateStatus.SKIPPED}
    )


def _next_candidate_number(db: Session, project: Project) -> int:
    """One past the highest number ever used on this project.

    Deliberately not a count of live rows: "RC 4" is how people refer to a
    candidate in email, so a number is never reused even after the candidate it
    named was abandoned.
    """
    highest = db.execute(
        select(func.max(ReleaseCandidate.candidate_number)).where(
            ReleaseCandidate.project_id == project.id
        )
    ).scalar()
    return int(highest or 0) + 1


def _user_names(db: Session, ids: Sequence[uuid.UUID | None]) -> dict[uuid.UUID, str]:
    wanted = {identifier for identifier in ids if identifier is not None}
    if not wanted:
        return {}
    return {
        user.id: user.display_name
        for user in db.execute(select(User).where(User.id.in_(wanted))).scalars()
    }


def _label_of(
    object_id: uuid.UUID | None, object_type: Any, labels: Mapping[uuid.UUID, str]
) -> str:
    if object_id is not None and object_id in labels:
        return labels[object_id]
    return registry.describe(object_type, object_id=object_id)


def _render_value(payload: Mapping[str, Any], field_name: str) -> str:
    """One field's value as a short string for the change log.

    Only the named field when the operation named one. A whole ``before`` dict
    rendered into a CSV cell is unreadable, and the change log exists to be
    read by the engineer deciding whether to import.
    """
    if not payload:
        return ""
    if field_name and field_name in payload:
        return "" if payload[field_name] is None else str(payload[field_name])
    return ", ".join(f"{key}={payload[key]}" for key in sorted(payload))
