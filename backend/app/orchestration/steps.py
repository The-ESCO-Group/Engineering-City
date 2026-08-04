"""Deterministic workflow steps.

The mechanical parts of a run: inventory, parsing, building the evidence graph,
finalising, and — on a production run — every checkpoint between a merged
revision and a frozen package. No model is involved in any of them. They are the
same every time for the same inputs, so making them agent tasks would add
nondeterminism and cost for nothing.

The steps that require judgement (planning, the specialist reviews, senior
review, the three production agents) are agent tasks and live in
:mod:`app.agents`.

The production steps are thin on purpose: each one asks a service that already
owns the rule — :mod:`app.validation`, :mod:`app.engineering.gates`,
:mod:`app.engineering.release`, :mod:`app.adapters.workstation` — and translates
the answer into a task outcome. What a task *is* is decided here; what is true
is decided there, and duplicating any of it would give the workflow a second
opinion that could disagree with the API's.

Two outcomes recur and are deliberately distinct. **Blocked** means a human has
to supply or decide something before this step can pass — an unanswered
question, a validation error, a gate whose criteria are unmet. **Awaiting
approval** means the platform has raised the decision and is holding for it.
Neither is a failure, and neither may be reported as completion.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.workstation import get_adapter
from app.core.clock import utcnow
from app.core.enums import (
    ApprovalGateKey,
    ApprovalKind,
    ApprovalStatus,
    ClarificationStatus,
    EventType,
    FileKind,
    FileStatus,
    GateStatus,
    ImportVerificationStatus,
    ReleaseCandidateStatus,
    TaskKind,
    ValidationSeverity,
)
from app.core.logging import get_logger
from app.core.services import canonical_import, evidence_graph, parsing
from app.engineering import gates, release
from app.engineering.release_archive import ReleaseArchiveRefused, finalize_release_archive
from app.models.approval import Approval
from app.models.file import UploadedFile
from app.models.finding import Finding
from app.models.profile import ProjectQuestion
from app.models.project import Project
from app.models.release import ReleaseCandidate, ValidationResult
from app.models.requirements import Clarification
from app.models.task import Task
from app.models.user import User
from app.orchestration.events import record_event
from app.orchestration.handlers import HandlerResult, TaskContext, register
from app.validation import run_validation

logger = get_logger(__name__)

#: How many offending objects a blocked step names before it summarises the
#: rest. Enough to recognise the pattern and start work; the full list is on the
#: validation run or the gate row.
_NAMED_EXAMPLES = 5

#: Gate statuses that mean a gate is finished with, whether or not it passed on
#: its merits. The same definition :mod:`app.engineering.gates` uses, so a gate
#: task and the gate itself cannot disagree about what "settled" means.
_SETTLED_GATES: frozenset[GateStatus] = frozenset({GateStatus.PASSED, GateStatus.SKIPPED})


@register(TaskKind.INVENTORY_FILES)
def inventory_files(context: TaskContext) -> HandlerResult:
    """Record what was supplied, and name what was expected but is not here.

    Absence is the finding nobody makes. A review over four documents when six
    were expected is a different thing from a complete review, and this is where
    that gets written down.
    """
    files = list(
        context.db.execute(
            select(UploadedFile).where(
                UploadedFile.project_id == context.project.id,
                UploadedFile.deleted_at.is_(None),
            )
        ).scalars()
    )

    by_kind: dict[str, int] = {}
    by_role: dict[str, int] = {}
    rejected: list[dict[str, str]] = []
    for record in files:
        by_kind[record.file_kind.value] = by_kind.get(record.file_kind.value, 0) + 1
        by_role[record.document_role.value] = by_role.get(record.document_role.value, 0) + 1
        if record.status is FileStatus.REJECTED:
            rejected.append({"filename": record.original_filename, "reason": record.status_detail})

    analysable = [
        record
        for record in files
        if record.file_kind is not FileKind.ZIP and record.status is not FileStatus.REJECTED
    ]

    expected = {
        "control_logic": "Studio 5000 L5X export",
        "interlock_schedule": "Interlock schedule",
        "io_list": "I/O list",
        "functional_description": "Functional description",
    }
    missing = [label for role, label in expected.items() if not by_role.get(role)]

    if not analysable:
        return HandlerResult.blocked(
            "No analysable files were supplied. Upload engineering documents and start "
            "the review again.",
            file_count=len(files),
            rejected=rejected,
        )

    return HandlerResult.completed(
        file_count=len(files),
        analysable_count=len(analysable),
        by_kind=by_kind,
        by_document_role=by_role,
        rejected=rejected,
        missing_expected_inputs=missing,
    )


@register(TaskKind.PARSE_FILE)
def parse_one_file(context: TaskContext) -> HandlerResult:
    """Parse a single file.

    One task per file so that an unreadable document costs only itself. A parse
    failure completes the task rather than failing it: the failure is already
    recorded on the file row and carried into the evidence graph as a limitation,
    and failing the task would stall a run over a document that simply cannot be
    read.
    """
    raw_id = context.payload.get("file_id")
    if not raw_id:
        return HandlerResult.failed("bad_payload", "The task carries no file_id.")

    try:
        file_id = uuid.UUID(str(raw_id))
    except ValueError:
        return HandlerResult.failed("bad_payload", f"'{raw_id}' is not a valid file id.")

    record = context.db.get(UploadedFile, file_id)
    if record is None:
        return HandlerResult.completed(
            file_id=str(file_id), skipped=True, reason="The file was deleted before parsing."
        )

    outcome = parsing.parse_file(
        context.db,
        record,
        context.settings,
        force=bool(context.payload.get("force")),
        task_id=context.task.id,
    )

    return HandlerResult.completed(
        file_id=str(file_id),
        filename=record.original_filename,
        succeeded=outcome.succeeded,
        skipped=outcome.skipped,
        anchor_count=outcome.anchor_count,
        warning_count=outcome.warning_count,
        error_code=outcome.error_code,
        error_message=outcome.error_message,
    )


@register(TaskKind.BUILD_EVIDENCE_GRAPH)
def build_evidence_graph(context: TaskContext) -> HandlerResult:
    """Join the parsed artifacts into one cross-file structure.

    The graph is rebuilt from artifacts rather than persisted as its own table:
    it is a pure function of them, and a stale cached graph would be worse than
    no cache. Only the counts and the shape are stored on the task.
    """
    imported = canonical_import.import_latest_parsed_artifacts(context.db, context.project.id)
    graph = evidence_graph.build_graph(context.db, context.project.id)
    counts = graph.counts()

    if not graph.has_logic and not graph.has_interlock_schedule:
        return HandlerResult.blocked(
            "Neither control logic nor an interlock schedule could be read from the "
            "uploaded files, so there is nothing to compare. "
            + (
                "Files that could not be read: "
                + ", ".join(item["filename"] for item in graph.unreadable_files)
                if graph.unreadable_files
                else "Upload an L5X export and an interlock schedule."
            ),
            counts=counts,
        )

    # Named, not just counted. A document that parsed successfully but yielded
    # nothing readable — a scanned drawing with no text layer — is invisible in
    # ``unreadable_files`` because the parse did not fail. Recording the
    # filenames here is what stops the run implying that document was examined.
    unread = sorted(
        {
            item.filename
            for item in graph.limitations
            if item.code in {"no_text_layer", "low_text_yield", "unsupported_content", "truncated"}
        }
    )

    return HandlerResult.completed(
        counts=counts,
        canonical_import={
            "artifact_ids": [str(artifact_id) for artifact_id in imported.artifact_ids],
            "created": dict(sorted(imported.created.items())),
            "updated": dict(sorted(imported.updated.items())),
            "created_count": imported.created_count,
            "updated_count": imported.updated_count,
            "issues": [
                {
                    "code": issue.code,
                    "message": issue.message,
                    "artifact_id": str(issue.artifact_id),
                    "source_key": issue.source_key,
                }
                for issue in imported.issues[:100]
            ],
            "issue_count": len(imported.issues),
        },
        missing_inputs=graph.missing_inputs,
        unreadable_files=graph.unreadable_files,
        partially_read_files=unread,
        tag_usage=evidence_graph.tag_usage_summary(graph),
        limitation_count=len(graph.limitations),
        limitations=[
            {"filename": item.filename, "code": item.code, "message": item.message}
            for item in graph.limitations[:50]
        ],
    )


@register(TaskKind.FINALIZE_PROJECT)
def finalize_project(context: TaskContext) -> HandlerResult:
    """Complete the run, but only if nothing is still waiting on a human.

    The guard is the point. A project marked complete while a safety-related
    approval is still pending would tell an engineer the review was finished when
    the most consequential part of it had not been read by anyone.
    """
    pending = list(
        context.db.execute(
            select(Approval).where(
                Approval.project_id == context.project.id,
                Approval.status == "pending",
            )
        ).scalars()
    )
    if pending:
        return HandlerResult.awaiting_approval(
            f"{len(pending)} approval(s) are still awaiting an engineering decision.",
            pending_approvals=len(pending),
        )

    findings = list(
        context.db.execute(
            select(Finding).where(Finding.project_id == context.project.id)
        ).scalars()
    )
    unreviewed = [finding for finding in findings if not finding.is_reviewed]
    if unreviewed:
        return HandlerResult.blocked(
            f"{len(unreviewed)} finding(s) have not been through senior review, so the "
            "review cannot be reported as complete.",
            unreviewed=len(unreviewed),
        )

    by_severity: dict[str, int] = {}
    for finding in findings:
        by_severity[finding.severity.value] = by_severity.get(finding.severity.value, 0) + 1

    archive_summary: dict[str, Any] = {}
    candidate_task = _production_candidate_task(context)
    if candidate_task is not None:
        candidate_id = candidate_task.result.get("candidate_id")
        try:
            candidate_uuid = uuid.UUID(str(candidate_id))
        except (TypeError, ValueError):
            return HandlerResult.blocked(
                "The production run generated no durable release-candidate identity, so "
                "its customer delivery cannot be finalized.",
                release_candidate_task_id=str(candidate_task.id),
            )
        candidate = context.db.get(ReleaseCandidate, candidate_uuid)
        if candidate is None or candidate.project_id != context.project.id:
            return HandlerResult.blocked(
                "The release candidate recorded by this production run no longer exists.",
                candidate_id=str(candidate_uuid),
            )
        try:
            archive = finalize_release_archive(
                context.db,
                candidate=candidate,
                finalized_by=None,
                settings=context.settings,
            )
        except ReleaseArchiveRefused as exc:
            return HandlerResult.blocked(
                exc.message,
                code=exc.code,
                details=exc.details,
                candidate_id=str(candidate.id),
            )
        archive_summary = {
            "release_candidate_id": str(candidate.id),
            "release_archive_id": str(archive.id),
            "release_archive_path": archive.relative_path,
            "release_archive_sha256": archive.sha256,
            "release_archive_bytes": archive.size_bytes,
        }

    record_event(
        context.db,
        project_id=context.project.id,
        event_type=EventType.PROJECT_COMPLETED,
        task_id=context.task.id,
        payload={
            "project_id": str(context.project.id),
            "name": context.project.name,
            "status": "completed",
            "finding_count": len(findings),
            **archive_summary,
        },
    )

    return HandlerResult.completed(
        finding_count=len(findings),
        findings_by_severity=by_severity,
        accepted=sum(1 for f in findings if f.status.value == "accepted"),
        rejected=sum(1 for f in findings if f.status.value == "rejected"),
        **archive_summary,
    )


# ---------------------------------------------------------------------------
# Production steps
# ---------------------------------------------------------------------------
@register(TaskKind.RESOLVE_CLARIFICATIONS)
def resolve_clarifications(context: TaskContext) -> HandlerResult:
    """Hold the run until the questions that block design are answered.

    This is where the product's refusal to guess turns into a stopped run. An
    agent that hit a missing threshold raised a question instead of inventing a
    value; if the run walked past it, the invention it declined to make would be
    made instead by whoever downstream needed a number.

    Two sources, because they block different things. A
    :class:`~app.models.profile.ProjectQuestion` is about the project — which
    controller family, which naming standard — and a mandatory one holds up
    everything. A :class:`~app.models.requirements.Clarification` is local to one
    requirement or device, and only the ones flagged ``blocks_generation`` stop
    work: the rest are things somebody still owes an answer for, which is not the
    same as work that cannot honestly proceed.
    """
    questions = [
        question
        for question in context.db.execute(
            select(ProjectQuestion).where(
                ProjectQuestion.project_id == context.project.id,
                ProjectQuestion.deleted_at.is_(None),
            )
        ).scalars()
        if question.is_blocking
    ]
    clarifications = list(
        context.db.execute(
            select(Clarification).where(
                Clarification.project_id == context.project.id,
                Clarification.deleted_at.is_(None),
                Clarification.status == ClarificationStatus.OPEN,
                Clarification.blocks_generation.is_(True),
            )
        ).scalars()
    )

    outstanding = [
        *(f"{question.reference}: {question.question}" for question in questions),
        *(item.question or item.ambiguity for item in clarifications),
    ]
    if outstanding:
        return HandlerResult.blocked(
            f"{len(outstanding)} question(s) must be answered before design can continue: "
            + _naming(outstanding),
            open_questions=len(questions),
            blocking_clarifications=len(clarifications),
            questions=outstanding[:50],
        )

    return HandlerResult.completed(open_questions=0, blocking_clarifications=0)


@register(TaskKind.VALIDATE_CROSS_DISCIPLINE)
def validate_cross_discipline(context: TaskContext) -> HandlerResult:
    """Run every validator over the revision as it stands.

    All disciplines, deliberately: the errors this step exists to catch are the
    ones that live between two of them — a tag the logic references and the tag
    model does not declare, an alarm bound to a signal nobody wired — and a run
    scoped to one discipline would miss exactly those.

    Only ``ERROR`` blocks. A warning is information a human weighs; treating one
    as a stop would train people to waive warnings, which is how the errors stop
    being read too.
    """
    run = run_validation(context.db, project=context.project, task_id=context.task.id)
    errors = list(
        context.db.execute(
            select(ValidationResult).where(
                ValidationResult.validation_run_id == run.id,
                ValidationResult.severity == ValidationSeverity.ERROR,
                ValidationResult.is_waived.is_(False),
            )
        ).scalars()
    )

    summary: dict[str, Any] = {
        "validation_run_id": str(run.id),
        "revision_number": run.revision_number,
        "checks_run": run.checks_run,
        "error_count": run.error_count,
        "warning_count": run.warning_count,
        "human_review_count": run.human_review_count,
    }
    if errors:
        return HandlerResult.blocked(
            f"{len(errors)} unwaived validation error(s) must be resolved before this "
            f"revision can be released: "
            + _naming([f"{item.code} at {item.object_label or 'the project'}" for item in errors]),
            **summary,
            errors=[
                {"code": item.code, "object": item.object_label, "message": item.message}
                for item in errors[:50]
            ],
        )
    return HandlerResult.completed(**summary)


@register(TaskKind.EVALUATE_APPROVAL_GATE)
def evaluate_approval_gate(context: TaskContext) -> HandlerResult:
    """Evaluate one gate, and raise the human decision it is waiting for.

    The task never decides anything. It re-evaluates the gate against the model
    as it stands and then, if and only if the gate is ``READY``, calls
    :func:`~app.engineering.gates.request_decision` — which creates a pending
    approval and moves the gate to ``AWAITING_DECISION``. Settling one needs
    :func:`~app.engineering.gates.record_decision` and a named reviewer, and
    there is no path from here to that.

    Convergence matters more than it looks. The queue is at-least-once and this
    task is returned to it after every approval decision on the project, so each
    branch below has to be reachable twice without asking the same question
    again: ``request_decision`` hands back the approval already in front of the
    reviewer rather than raising a second one, and a settled gate completes.
    """
    gate_key = _gate_key(context)
    if gate_key is None:
        return HandlerResult.failed(
            "bad_payload",
            f"The task carries no recognised gate_key: {context.payload.get('gate_key')!r}.",
        )

    gate = next(
        item
        for item in gates.evaluate_all(context.db, project=context.project)
        if item.gate_key is gate_key
    )
    unmet = [str(item.get("detail") or item.get("statement")) for item in gate.unmet_criteria]
    summary: dict[str, Any] = {
        "gate_key": gate_key.value,
        "title": gate.title,
        "status": gate.status.value,
        "unmet_criteria": unmet,
    }

    if gate.status in _SETTLED_GATES:
        return HandlerResult.completed(**summary, decided_revision=gate.decided_revision_number)

    if gate.status is GateStatus.FAILED:
        return HandlerResult.blocked(
            f"'{gate.title}' was refused by a reviewer, so the work behind it has to "
            f"change before this run can continue.",
            **summary,
        )

    if gate.status is GateStatus.NOT_READY:
        # A gate whose criteria include "every serious finding has been decided"
        # is not waiting on missing work, it is waiting on a person — and the
        # difference decides whether the run ever restarts. Only an
        # ``AWAITING_APPROVAL`` task is returned to the queue when the last
        # approval is settled, so reporting this as blocked would strand the run
        # at the moment the reviewer finished answering it.
        outstanding = _pending_approvals(context)
        if outstanding:
            return HandlerResult.awaiting_approval(
                f"'{gate.title}' cannot be decided while {outstanding} approval(s) on this "
                f"project are outstanding: " + _naming(unmet),
                **summary,
                pending_approvals=outstanding,
            )
        return HandlerResult.blocked(
            f"'{gate.title}' is not ready to be decided: " + _naming(unmet), **summary
        )

    approval = gates.request_decision(
        context.db, project=context.project, gate=gate, requested_by_agent="project_manager"
    )
    return HandlerResult.awaiting_approval(
        f"'{gate.title}' is waiting for a named reviewer to accept responsibility for "
        f"the work behind it at revision {context.project.current_revision_number}.",
        **summary,
        approval_id=str(approval.id),
    )


@register(TaskKind.GENERATE_RELEASE_CANDIDATE)
def generate_release_candidate(context: TaskContext) -> HandlerResult:
    """Reserve a candidate at the current revision and build its files.

    Readiness is asked first even though :func:`create_release_candidate` asks it
    again. The difference is what the caller can do with the answer: a refusal
    that arrives as an exception is a failed task, and every one of these blocks
    is something a human clears rather than something that went wrong.

    A generator refusal comes back as a *candidate* in ``VALIDATION_FAILED``
    rather than as an exception, so it is checked for explicitly. It means the
    model held something the generator will not emit, the offending object is
    named on the candidate, and nothing was left on disk.
    """
    readiness = release.release_readiness(
        context.db, project=context.project, settings=context.settings
    )
    if not readiness.is_ready:
        return HandlerResult.blocked(
            "This revision cannot produce a release candidate: "
            + _naming([f"{block.summary} {block.detail}" for block in readiness.blocking]),
            **_readiness_view(readiness),
        )

    try:
        candidate = release.create_release_candidate(
            context.db, project=context.project, settings=context.settings
        )
        candidate = release.generate_artifacts(
            context.db, candidate=candidate, settings=context.settings
        )
    except release.ReleaseRefused as exc:
        return HandlerResult.blocked(
            exc.message, blocks=[block.as_document() for block in exc.blocks]
        )

    summary: dict[str, Any] = {
        "candidate_id": str(candidate.id),
        "candidate_number": candidate.candidate_number,
        "revision_number": candidate.revision_number,
        "status": candidate.status.value,
        "validation_level": candidate.validation_level.value,
        "artifact_count": len(candidate.artifacts),
        "package_sha256": candidate.package_sha256,
        "highest_attainable_validation_level": (
            readiness.highest_attainable_validation_level.value
        ),
    }
    if candidate.status is ReleaseCandidateStatus.VALIDATION_FAILED:
        return HandlerResult.blocked(
            f"The generator refused to emit a package for revision "
            f"{candidate.revision_number}: {candidate.notes}",
            **summary,
            unsupported_items=candidate.unsupported_items[:50],
        )
    return HandlerResult.completed(**summary)


@register(TaskKind.VERIFY_IMPORT)
def verify_import(context: TaskContext) -> HandlerResult:
    """Ask the workstation adapter to verify the package, and record that it cannot.

    The normal answer is that there is no licensed Rockwell workstation, and the
    normal answer is *recorded* rather than skipped: ``UNAVAILABLE`` with the
    adapter's own sentence, written onto the candidate so it travels into the
    manifest. A blank here would read as a pass, which is the exact place a
    product like this lies to itself first.

    When an adapter *is* configured the step blocks instead of running anything.
    A verification job needs a named human approver — an agent's say-so is not
    enough — and an unattended queue task cannot supply one.
    """
    candidate = _newest_candidate(context.db, context.project)
    if candidate is None:
        return HandlerResult.blocked(
            "No release candidate has been generated, so there is nothing to verify."
        )

    health = get_adapter(context.settings).health()
    summary: dict[str, Any] = {
        "candidate_id": str(candidate.id),
        "candidate_number": candidate.candidate_number,
        "adapter_status": health.status.value,
        "adapter_key": health.adapter_key,
        "detail": health.detail,
    }

    if health.available:
        return HandlerResult.blocked(
            "A licensed workstation adapter is configured, so import verification is "
            "possible and has not been attempted. Running it needs a named human "
            "approver on the job, which an unattended workflow task cannot supply.",
            **summary,
        )

    candidate.import_verification_status = ImportVerificationStatus.UNAVAILABLE
    candidate.import_verification_detail = health.detail
    context.db.flush()
    return HandlerResult.completed(
        **summary,
        import_verification_status=candidate.import_verification_status.value,
        import_ready_candidate_only=True,
    )


@register(TaskKind.ASSEMBLE_PACKAGE)
def assemble_package(context: TaskContext) -> HandlerResult:
    """Freeze the manifest, on a named human's authority and nobody else's.

    :func:`~app.engineering.release.freeze_manifest` refuses without an approver
    at runtime rather than only in its type signature, so this step's whole job
    is to find one honestly. It raises an approval against the candidate and
    waits; the freeze is then recorded against whoever decided it.

    That approval is deliberately not a gate decision. Gate 6 is a statement
    *about* a frozen manifest — its criteria include the manifest's hash — so
    demanding it before the manifest exists would deadlock the run.
    """
    candidate = _newest_candidate(context.db, context.project)
    if candidate is None:
        return HandlerResult.blocked(
            "No release candidate has been generated, so there is no package to seal."
        )
    if candidate.manifest is not None:
        # The manifest is written once. A task the queue re-runs converges on
        # saying so rather than on a refusal.
        return HandlerResult.completed(
            candidate_id=str(candidate.id),
            candidate_number=candidate.candidate_number,
            manifest_content_sha256=candidate.manifest.content_sha256,
            already_frozen=True,
        )

    approval = _release_approval(context, candidate)
    if approval.status is ApprovalStatus.PENDING:
        return HandlerResult.awaiting_approval(
            f"Release candidate {candidate.candidate_number} is waiting for a named "
            f"engineer to accept responsibility for handing it over.",
            candidate_id=str(candidate.id),
            candidate_number=candidate.candidate_number,
            approval_id=str(approval.id),
        )
    if approval.status not in {ApprovalStatus.APPROVED, ApprovalStatus.RISK_ACCEPTED}:
        return HandlerResult.blocked(
            f"Release candidate {candidate.candidate_number} was {approval.status.value} "
            f"by a reviewer: {approval.decision_notes or 'no reason was recorded'}.",
            candidate_id=str(candidate.id),
            approval_id=str(approval.id),
        )

    approver = context.db.get(User, approval.decided_by_id) if approval.decided_by_id else None
    if approver is None:
        return HandlerResult.blocked(
            f"The approval for release candidate {candidate.candidate_number} names no "
            f"person, so there is nobody to record the release against.",
            approval_id=str(approval.id),
        )

    try:
        manifest = release.freeze_manifest(
            context.db, candidate=candidate, approved_by=approver, settings=context.settings
        )
    except release.ReleaseRefused as exc:
        return HandlerResult.blocked(
            exc.message, blocks=[block.as_document() for block in exc.blocks]
        )

    return HandlerResult.completed(
        candidate_id=str(candidate.id),
        candidate_number=candidate.candidate_number,
        manifest_content_sha256=manifest.content_sha256,
        checksum_file_sha256=manifest.checksum_file_sha256,
        approved_by=approver.display_name,
        forced_past_stale_revision=release.was_forced_past_stale_revision(candidate),
    )


# ---------------------------------------------------------------------------
# Shared by the production steps
# ---------------------------------------------------------------------------
def _naming(items: list[str], limit: int = _NAMED_EXAMPLES) -> str:
    """Name a few offenders and count the rest.

    A message listing four hundred is one nobody reads; a message listing none is
    one nobody can act on.
    """
    if not items:
        return "no detail was recorded, which is a defect in this step"
    shown = "; ".join(items[:limit])
    remainder = len(items) - limit
    return shown + (f"; and {remainder} more" if remainder > 0 else "")


def _pending_approvals(context: TaskContext) -> int:
    """How many decisions on this project are still in front of a human."""
    return len(
        list(
            context.db.execute(
                select(Approval.id).where(
                    Approval.project_id == context.project.id,
                    Approval.status == ApprovalStatus.PENDING,
                )
            ).scalars()
        )
    )


def _gate_key(context: TaskContext) -> ApprovalGateKey | None:
    try:
        return ApprovalGateKey(str(context.payload.get("gate_key", "")))
    except ValueError:
        return None


def _readiness_view(readiness: release.ReleaseReadiness) -> dict[str, Any]:
    return {
        "revision_number": readiness.revision_number,
        "blocks": [block.as_document() for block in readiness.blocking],
        "outstanding_gates": [gate.value for gate in readiness.outstanding_gates],
        "validation_error_count": readiness.validation_error_count,
        "highest_attainable_validation_level": (
            readiness.highest_attainable_validation_level.value
        ),
        "import_verification_possible": readiness.import_verification_possible,
    }


def _newest_candidate(db: Session, project: Project) -> ReleaseCandidate | None:
    """The candidate this run is working on.

    The newest one, because a candidate number is never reused and generating
    another is what starting a second release run means. Read back rather than
    passed between tasks, so the steps do not depend on each other's result
    payloads surviving a retry.
    """
    return (
        db.execute(
            select(ReleaseCandidate)
            .where(ReleaseCandidate.project_id == project.id)
            .order_by(ReleaseCandidate.candidate_number.desc())
            .limit(1)
        )
        .scalars()
        .first()
    )


def _production_candidate_task(context: TaskContext) -> Task | None:
    """Return this run's release-generation task, or ``None`` for review runs."""

    if context.task.run_id is None:
        return None
    return (
        context.db.execute(
            select(Task)
            .where(
                Task.project_id == context.project.id,
                Task.run_id == context.task.run_id,
                Task.kind == TaskKind.GENERATE_RELEASE_CANDIDATE,
            )
            .limit(1)
        )
        .scalars()
        .first()
    )


def _release_approval(context: TaskContext, candidate: ReleaseCandidate) -> Approval:
    """The decision on this candidate — the existing one, or a newly raised one.

    Looked up before it is raised, so a task the queue re-runs puts the same
    question back in front of the reviewer instead of a second copy of it.
    """
    existing = (
        context.db.execute(
            select(Approval)
            .where(
                Approval.project_id == context.project.id,
                Approval.release_candidate_id == candidate.id,
                Approval.kind == ApprovalKind.RELEASE_CANDIDATE,
            )
            .order_by(Approval.created_at.desc())
            .limit(1)
        )
        .scalars()
        .first()
    )
    if existing is not None:
        return existing

    verification = candidate.import_verification_status.value
    approval = Approval(
        project_id=context.project.id,
        task_id=context.task.id,
        release_candidate_id=candidate.id,
        kind=ApprovalKind.RELEASE_CANDIDATE,
        status=ApprovalStatus.PENDING,
        revision_number=candidate.revision_number,
        title=f"Release candidate {candidate.candidate_number}",
        question=(
            f"Release candidate {candidate.candidate_number} was generated from revision "
            f"{candidate.revision_number} and reached {candidate.validation_level.value}. "
            f"Import verification is {verification}"
            + (
                f": {candidate.import_verification_detail}"
                if candidate.import_verification_detail
                else ""
            )
            + ". Approving seals the manifest and records you as the person who handed "
            "these files over."
        ),
        context={
            "candidate_number": candidate.candidate_number,
            "revision_number": candidate.revision_number,
            "validation_level": candidate.validation_level.value,
            "import_verification_status": verification,
            "package_sha256": candidate.package_sha256,
            "artifact_count": len(candidate.artifacts),
            "limitations": candidate.limitations,
        },
        requested_at=utcnow(),
    )
    context.db.add(approval)
    context.db.flush()

    record_event(
        context.db,
        project_id=context.project.id,
        event_type=EventType.APPROVAL_REQUESTED,
        task_id=context.task.id,
        payload={
            "approval_id": str(approval.id),
            "kind": approval.kind.value,
            "status": approval.status.value,
            "title": approval.title,
            "question": approval.question,
        },
    )
    return approval
