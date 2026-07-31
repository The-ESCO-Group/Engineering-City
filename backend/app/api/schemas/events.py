"""The versioned event envelope broadcast over the project WebSocket.

The envelope is deliberately uniform: clients switch on ``event_type`` and read
``payload``. Payload shapes are additive-only — a new optional key is a
non-breaking change, removing or retyping a key is not.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import Field

from app.api.schemas.common import ApiModel
from app.core.enums import (
    AgentKind,
    ApprovalGateKey,
    ChangeOperationType,
    ChangeSetStatus,
    ClarificationStatus,
    EventType,
    ExportArtifactKind,
    GateStatus,
    ImportVerificationStatus,
    ObjectType,
    QuestionCategory,
    ReleaseCandidateStatus,
    RequirementKind,
    RequirementPriority,
    RequirementStatus,
    RestrictedDomain,
    SemanticLocation,
    TaskKind,
    ValidationDiscipline,
    ValidationLevel,
    ValidationRunStatus,
)


class EventEnvelope(ApiModel):
    """One entry in a project's ordered event stream."""

    id: uuid.UUID
    schema_version: int = Field(
        description="Envelope version. Bumped only on a breaking payload change."
    )
    seq: int = Field(
        description=(
            "Gap-free per-project sequence number. Reconnecting clients pass the "
            "last value they saw as ?since_seq= to replay what they missed."
        )
    )
    event_type: EventType
    project_id: uuid.UUID
    agent_id: uuid.UUID | None = Field(
        default=None, description="Agent instance that caused the event, if any."
    )
    task_id: uuid.UUID | None = None
    actor_user_id: uuid.UUID | None = Field(
        default=None, description="Authenticated human that caused the event, if any."
    )
    occurred_at: datetime
    payload: dict[str, Any] = Field(default_factory=dict)


class WebSocketHello(ApiModel):
    """First frame the server sends after a WebSocket connection is accepted."""

    type: str = "hello"
    project_id: uuid.UUID
    schema_version: int
    #: Highest sequence number currently persisted for the project.
    current_seq: int
    #: True when the requested ``since_seq`` was older than the replay window,
    #: meaning the client must refetch state via REST rather than rely on replay.
    replay_truncated: bool = False
    replayed: int = 0


class WebSocketError(ApiModel):
    """Terminal frame sent before the server closes a WebSocket."""

    type: str = "error"
    code: str
    message: str


# ---------------------------------------------------------------------------
# Payload shapes, documented per event type.
# These are published in contracts/events.schema.json so the frontend can type
# the payload after switching on event_type.
# ---------------------------------------------------------------------------
class ProjectEventPayload(ApiModel):
    project_id: uuid.UUID
    name: str
    status: str
    #: Present on ``project.failed``.
    failure_reason: str = ""


class FileEventPayload(ApiModel):
    file_id: uuid.UUID
    original_filename: str
    file_kind: str
    document_role: str
    status: str
    size_bytes: int = 0
    sha256: str = ""
    #: Present on ``file.parsed`` / ``file.parse_failed``.
    warning_count: int = 0
    detail: str = ""


class WorkflowEventPayload(ApiModel):
    run_id: uuid.UUID | None = None
    task_count: int = 0
    reason: str = ""


class TaskEventPayload(ApiModel):
    task_id: uuid.UUID
    kind: str
    title: str
    status: str
    agent_kind: str = ""
    attempts: int = 0
    #: Present on ``task.failed`` / ``task.blocked``.
    detail: str = ""


class AgentStatusEventPayload(ApiModel):
    agent_id: uuid.UUID
    agent_kind: str
    display_name: str
    status: str
    current_activity: str = ""
    task_id: uuid.UUID | None = None


class AgentLocationEventPayload(ApiModel):
    agent_id: uuid.UUID
    agent_kind: str
    location: str
    previous_location: str = ""


class AgentMessageEventPayload(ApiModel):
    message_id: uuid.UUID
    agent_id: uuid.UUID | None = None
    agent_kind: str = ""
    role: str
    body: str
    task_id: uuid.UUID | None = None


class FindingEventPayload(ApiModel):
    finding_id: uuid.UUID
    title: str
    category: str
    severity: str
    status: str
    confidence: float
    requires_approval: bool
    evidence_count: int = 0
    created_by_agent_kind: str = ""
    #: Present on ``finding.reviewed``.
    review_verdict: str = ""


class ApprovalEventPayload(ApiModel):
    approval_id: uuid.UUID
    kind: str
    status: str
    title: str
    finding_id: uuid.UUID | None = None
    recommendation_id: uuid.UUID | None = None
    decided_by_id: uuid.UUID | None = None


class ReportEventPayload(ApiModel):
    report_id: uuid.UUID
    report_type: str
    export_format: str
    title: str
    status: str
    provider_name: str = ""


# ---------------------------------------------------------------------------
# Production pipeline payloads.
#
# Four rules shape every model below, and they are product constraints rather
# than house style:
#
# * **Locations are semantic.** Each payload names the district the work
#   happened in as a :class:`~app.core.enums.SemanticLocation`, never a
#   coordinate. Where a district sits on screen is the frontend's business, and
#   the city must not have to map an event type onto a place itself.
# * **A payload is not a document.** Ids, short labels, one-line summaries and
#   counts. Free text is capped, and a merge reports operation counts by type
#   rather than the operations.
# * **Certainty is never blurred.** Validation reports counts per severity, and
#   an artifact carries the :class:`~app.core.enums.ValidationLevel` it actually
#   reached rather than a boolean that would read as a pass.
# * **A failure names something.** Every failure event carries a reason a human
#   can act on, plus the id of whatever failed where one exists.
# ---------------------------------------------------------------------------
class ProjectQuestionEventPayload(ApiModel):
    """A clarification the platform needs answered, raised or closed.

    ``is_blocking`` and ``blocks_task_kinds`` travel with the event because the
    city has to show what has stopped: without them a question that holds up
    nothing looks exactly like one holding up the whole project.
    """

    question_id: uuid.UUID
    #: Short human-facing key, e.g. ``Q-001``. Stable for the life of the project.
    reference: Annotated[str, Field(max_length=40)]
    category: QuestionCategory
    status: ClarificationStatus
    location: SemanticLocation
    #: The question as it is put to the person answering it, capped. The full
    #: text lives on the question resource.
    question: Annotated[str, Field(max_length=500)] = ""
    #: What is blocked until it is answered. A question without a stated
    #: consequence cannot be prioritised by whoever is asked to unblock it.
    why_it_matters: Annotated[str, Field(max_length=500)] = ""
    is_mandatory: bool = True
    is_blocking: bool = False
    #: Task kinds held up while this stays open.
    blocks_task_kinds: list[TaskKind] = Field(default_factory=list)
    #: Present on ``project.question_resolved``.
    answer_summary: Annotated[str, Field(max_length=500)] = ""
    #: The recorded human decision that answered it, so the feed can link to a
    #: named person's statement rather than to an anonymous status change.
    decision_id: uuid.UUID | None = None
    answered_by_id: uuid.UUID | None = None


class RequirementEventPayload(ApiModel):
    """One requirement the platform extracted from the brief.

    ``is_inferred`` is carried rather than looked up: a requirement the platform
    derived is a proposal, and a feed that renders it identically to one the
    customer wrote is misreporting what happened.
    """

    requirement_id: uuid.UUID
    #: Stable per-project key, e.g. ``REQ-014``.
    reference: Annotated[str, Field(max_length=64)]
    title: Annotated[str, Field(max_length=300)]
    kind: RequirementKind
    priority: RequirementPriority
    status: RequirementStatus
    location: SemanticLocation
    is_inferred: bool = False
    #: How sure the extractor is that this is a requirement at all, rather than
    #: narrative or a worked example.
    confidence: float = 0.0
    #: Human-followable origin, e.g. ``Functional_Description.docx / 3.2``.
    #: Empty when the requirement was inferred rather than read.
    source_label: Annotated[str, Field(max_length=200)] = ""
    acceptance_criteria_count: int = 0
    parent_id: uuid.UUID | None = None


class WorkflowPlannedEventPayload(ApiModel):
    """The plan a Project Manager agent produced for a run.

    Task counts by kind rather than the tasks themselves: the plan is a
    document, and the feed only needs how much work was scheduled and what
    already holds it up.
    """

    location: SemanticLocation
    run_id: uuid.UUID | None = None
    task_count: int = 0
    #: How many tasks of each :class:`~app.core.enums.TaskKind` were planned.
    task_counts_by_kind: dict[TaskKind, int] = Field(default_factory=dict)
    summary: Annotated[str, Field(max_length=500)] = ""
    #: References of the questions that must be answered before the plan can
    #: start, e.g. ``["Q-001"]``. Empty when nothing blocks it.
    blocked_by_question_references: list[Annotated[str, Field(max_length=40)]] = Field(
        default_factory=list
    )
    risk_count: int = 0
    #: Work the plan deliberately does not cover. A count, because the reasons
    #: are part of the plan rather than of this event.
    out_of_scope_count: int = 0


class WorkflowFailedEventPayload(ApiModel):
    """A run that stopped and will not continue on its own.

    ``reason`` is required and ``failed_task_id`` names the task that produced
    it where one did. A failure broadcast as a status word tells a reader that
    something is wrong and nothing about what to do next.
    """

    location: SemanticLocation
    #: Why the run stopped, in one sentence an engineer can act on.
    reason: Annotated[str, Field(min_length=1, max_length=500)]
    run_id: uuid.UUID | None = None
    #: The task whose failure ended the run, when the failure was attributable
    #: to one rather than to the run as a whole.
    failed_task_id: uuid.UUID | None = None
    failed_task_kind: TaskKind | None = None
    tasks_completed: int = 0
    tasks_remaining: int = 0


class TaskAssignedEventPayload(ApiModel):
    """A queued task handed to an agent.

    Distinct from ``task.started``: assignment is what sends an agent across the
    city, so the destination is on the event rather than derived from the kind.
    """

    task_id: uuid.UUID
    kind: TaskKind
    title: Annotated[str, Field(max_length=300)]
    agent_kind: AgentKind
    #: Where the agent will be working.
    location: SemanticLocation
    #: The agent instance, once one exists. A task can be assigned to a kind
    #: before an instance has been spun up for it.
    agent_id: uuid.UUID | None = None
    attempt: int = 1


class ChangeSetEventPayload(ApiModel):
    """A proposed, scoped batch of changes to the canonical model.

    Counts and scope, never the operations: a change set may carry two thousand
    of them, and inlining them would put a document on the wire. ``confidence``,
    ``assumption_count`` and ``warning_count`` are here because a proposal that
    states no uncertainty is more suspicious than one that does.
    """

    changeset_id: uuid.UUID
    title: Annotated[str, Field(max_length=300)]
    status: ChangeSetStatus
    location: SemanticLocation
    #: The revision this change set was written against.
    base_revision_number: int
    operation_count: int = 0
    #: The object types this agent was permitted to touch. Enforced at merge,
    #: and shown before it so a reviewer can see the scope it claimed.
    allowed_object_types: list[ObjectType] = Field(default_factory=list)
    author_agent_kind: AgentKind | None = None
    author_user_id: uuid.UUID | None = None
    confidence: float = 0.0
    assumption_count: int = 0
    warning_count: int = 0
    requires_human_approval: bool = False
    #: Non-empty when the change touches a restricted domain, naming which one.
    restricted_domains: list[RestrictedDomain] = Field(default_factory=list)


class ChangeSetValidationFailedEventPayload(ApiModel):
    """A change set the deterministic validators refused.

    Actionable by construction: ``reason`` says what was wrong in a sentence,
    the counts are per severity so nobody has to guess how much of it blocks,
    and the failing operation is named where the failure was attributable to
    one.
    """

    changeset_id: uuid.UUID
    location: SemanticLocation
    #: One sentence, written for the engineer who has to fix it.
    reason: Annotated[str, Field(min_length=1, max_length=500)]
    error_count: int = 0
    warning_count: int = 0
    unsupported_count: int = 0
    human_review_count: int = 0
    #: The first operation that failed, when one operation was at fault.
    failed_operation_id: uuid.UUID | None = None
    failed_object_type: ObjectType | None = None
    #: Human-followable, e.g. ``Tag MTR_101_Cmd_Run``.
    failed_object_label: Annotated[str, Field(max_length=200)] = ""


class ChangeSetReviewedEventPayload(ApiModel):
    """A review verdict on a change set.

    The concerns ride along as short lines rather than as a bare count. A
    rejection the author cannot act on is the failure this event exists to
    prevent; ``concern_count`` is the true total, since the list is capped.
    """

    changeset_id: uuid.UUID
    status: ChangeSetStatus
    location: SemanticLocation
    verdict: Literal["approve", "request_changes", "reject"]
    concern_count: int = 0
    #: The leading concerns, one line each.
    concerns: Annotated[list[Annotated[str, Field(max_length=200)]], Field(max_length=10)] = Field(
        default_factory=list
    )
    reviewed_by_agent_kind: AgentKind | None = None
    reviewed_by_user_id: uuid.UUID | None = None
    #: Review may lower a confidence, never raise it.
    adjusted_confidence: float | None = None


class ChangeSetMergedEventPayload(ApiModel):
    """A change set applied to the canonical model in one transaction.

    ``operation_counts_by_type`` is the point of the shape. A merge reported as
    "38 operations" hides whether it created a tag or deleted thirty of them,
    and the city cannot animate what it cannot tell apart.
    """

    changeset_id: uuid.UUID
    location: SemanticLocation
    base_revision_number: int
    resulting_revision_number: int
    #: How many operations of each type were applied. Counts, never the
    #: operations: the diff between two revisions stays a query.
    operation_counts_by_type: dict[ChangeOperationType, int] = Field(default_factory=dict)
    #: How many objects of each type the merge touched.
    object_counts_by_type: dict[ObjectType, int] = Field(default_factory=dict)
    operations_applied: int = 0
    operations_skipped: int = 0
    merge_result_id: uuid.UUID | None = None
    merged_by_user_id: uuid.UUID | None = None
    #: What this merge did, in one line.
    summary: Annotated[str, Field(max_length=500)] = ""


class RevisionCreatedEventPayload(ApiModel):
    """A new numbered point on the project's timeline.

    A revision is a label rather than a copy, so the payload names what produced
    it and how far it moved the model. ``is_release_baseline`` is carried
    because a baseline must never be the base for an in-place edit.
    """

    revision_id: uuid.UUID
    number: int
    location: SemanticLocation
    parent_revision_number: int | None = None
    #: The change set whose merge produced this revision. Null on revision 0.
    changeset_id: uuid.UUID | None = None
    operation_count: int = 0
    object_counts_by_type: dict[ObjectType, int] = Field(default_factory=dict)
    summary: Annotated[str, Field(max_length=500)] = ""
    created_by_agent_kind: AgentKind | None = None
    created_by_id: uuid.UUID | None = None
    is_release_baseline: bool = False


class GateStatusChangedEventPayload(ApiModel):
    """One of the six approval gates moving between states.

    The unmet criteria are carried, not just the status. A gate that says "not
    ready" without saying what is missing gives a UI nothing to render and an
    engineer nothing to do, so ``unmet_criteria`` is non-empty whenever the gate
    is not passable and ``unmet_criteria_count`` is the true total behind the
    capped list. ``is_enabled`` and ``disabled_reason`` keep a gate an
    administrator switched off visible instead of silently passing.
    """

    gate_key: ApprovalGateKey
    #: The state the gate was in before this event.
    previous_status: GateStatus
    status: GateStatus
    location: SemanticLocation
    title: Annotated[str, Field(max_length=200)] = ""
    criteria_count: int = 0
    unmet_criteria_count: int = 0
    #: Short labels for what is still outstanding, one line each.
    unmet_criteria: Annotated[list[Annotated[str, Field(max_length=200)]], Field(max_length=20)] = (
        Field(default_factory=list)
    )
    is_enabled: bool = True
    disabled_reason: Annotated[str, Field(max_length=500)] = ""
    approval_id: uuid.UUID | None = None
    decided_by_id: uuid.UUID | None = None
    #: The revision the decision was made against. A later change re-opens the
    #: gate rather than inheriting the approval.
    decided_revision_number: int | None = None
    decision_comment: Annotated[str, Field(max_length=500)] = ""


class ValidationStartedEventPayload(ApiModel):
    """A validation run beginning over one revision.

    ``disciplines`` is on the event because a clean result across three
    disciplines must never be read as a clean result across all of them, and
    ``revision_number`` because a run is a statement about a specific state of
    the model rather than about the project as it is now.
    """

    validation_run_id: uuid.UUID
    revision_number: int
    location: SemanticLocation
    disciplines: list[ValidationDiscipline] = Field(default_factory=list)
    triggered_by_task_id: uuid.UUID | None = None
    triggered_by_id: uuid.UUID | None = None


class ValidationCompletedEventPayload(ApiModel):
    """What a validation run found, counted per severity.

    A single total invites the reader to guess how much of it blocks, so the
    counts are broken out; ``checks_run`` sits beside them because a run that
    found nothing because it ran nothing must not look like a clean one, and
    ``is_blocking`` states the consequence rather than leaving it to be inferred
    from the numbers. ``summary`` and ``first_error_result_id`` make a failing
    run name something the reader can open.
    """

    validation_run_id: uuid.UUID
    revision_number: int
    status: ValidationRunStatus
    location: SemanticLocation
    #: What the run found, or why it did not finish, in one line.
    summary: Annotated[str, Field(min_length=1, max_length=500)]
    disciplines: list[ValidationDiscipline] = Field(default_factory=list)
    error_count: int = 0
    warning_count: int = 0
    information_count: int = 0
    unsupported_count: int = 0
    human_review_count: int = 0
    #: How many checks actually executed.
    checks_run: int = 0
    #: Only ``error`` stops a release candidate being produced.
    is_blocking: bool = False
    #: The first blocking result, and where it is, so a failure points at a row.
    first_error_result_id: uuid.UUID | None = None
    first_error_object_label: Annotated[str, Field(max_length=200)] = ""


class ExportStartedEventPayload(ApiModel):
    """Generation of a release package beginning.

    The artifact kinds the run intends to produce are named up front, so a
    package that ends up missing one is visible as a gap rather than as an
    absence nobody expected.
    """

    release_candidate_id: uuid.UUID
    candidate_number: int
    revision_number: int
    location: SemanticLocation
    export_profile_id: uuid.UUID | None = None
    #: Stable machine name of the export profile, e.g. ``full_controller``.
    export_profile_key: Annotated[str, Field(max_length=60)] = ""
    artifact_kinds: list[ExportArtifactKind] = Field(default_factory=list)
    expected_artifact_count: int = 0


class ExportArtifactCreatedEventPayload(ApiModel):
    """One generated file inside a release package.

    ``validation_level`` rather than a boolean, and per file rather than rolled
    up to the candidate: files do not all reach the same level, and labelling
    the weakest one with the strength of the strongest would claim something
    nobody proved. ``is_produced_by_plugin`` is carried for the same reason —
    we do not warrant code we did not generate.
    """

    artifact_id: uuid.UUID
    release_candidate_id: uuid.UUID
    kind: ExportArtifactKind
    filename: Annotated[str, Field(max_length=255)]
    location: SemanticLocation
    #: How far this file has actually been proved. Never inferred from a check
    #: at a lower level having passed.
    validation_level: ValidationLevel
    #: Path within the package, e.g. ``01_Controller/Programs/MainProgram.L5X``.
    relative_path: Annotated[str, Field(max_length=500)] = ""
    size_bytes: int = 0
    sha256: Annotated[str, Field(max_length=64)] = ""
    #: Position in the import sequence. Load-bearing: importing a program before
    #: the UDTs it references fails in Studio 5000.
    import_order: int = 0
    is_produced_by_plugin: bool = False
    plugin_name: Annotated[str, Field(max_length=60)] = ""
    warning_count: int = 0


class ReleaseCandidateCreatedEventPayload(ApiModel):
    """A newly assembled package, at the level it has actually attained.

    ``limitation_count`` and ``unsupported_item_count`` are on the event rather
    than only inside the package: a limitation the customer first meets at the
    import dialog is a limitation we hid.
    """

    release_candidate_id: uuid.UUID
    #: Monotonic per project, and human-facing: "RC 4" is how people refer to it.
    candidate_number: int
    revision_number: int
    status: ReleaseCandidateStatus
    validation_level: ValidationLevel
    location: SemanticLocation
    name: Annotated[str, Field(max_length=120)] = ""
    artifact_count: int = 0
    package_bytes: int = 0
    package_sha256: Annotated[str, Field(max_length=64)] = ""
    validation_run_id: uuid.UUID | None = None
    export_profile_id: uuid.UUID | None = None
    #: Which build of the generator wrote these files.
    generator_version: Annotated[str, Field(max_length=40)] = ""
    limitation_count: int = 0
    unsupported_item_count: int = 0


class ReleaseReadyEventPayload(ApiModel):
    """A package that has cleared its gates and is ready to hand over.

    Ready to *export* is not a release Studio 5000 has accepted, so both facts
    are carried and neither is collapsed into a flag: ``validation_level`` says
    how far the package was actually proved, and ``import_verification_status``
    says whether a licensed workstation has ever imported it —
    ``not_attempted`` being the normal end state, because most deployments have
    no workstation adapter at all. ``is_import_ready_candidate_only`` is the
    claim the UI, the manifest and the covering email must all repeat while it
    is true.
    """

    release_candidate_id: uuid.UUID
    candidate_number: int
    revision_number: int
    status: ReleaseCandidateStatus
    location: SemanticLocation
    #: The highest level this package has reached, never raised by a weaker
    #: check having passed.
    validation_level: ValidationLevel
    import_verification_status: ImportVerificationStatus
    #: True until something has actually imported this package on a workstation.
    is_import_ready_candidate_only: bool = True
    #: Why verification was not attempted, or did not pass, when it did not.
    import_verification_detail: Annotated[str, Field(max_length=500)] = ""
    artifact_count: int = 0
    package_bytes: int = 0
    package_sha256: Annotated[str, Field(max_length=64)] = ""
    #: Hash of the frozen manifest, which in turn hashes every file.
    manifest_sha256: Annotated[str, Field(max_length=64)] = ""
    approved_by_id: uuid.UUID | None = None
    limitation_count: int = 0
    unsupported_item_count: int = 0


#: Maps each event type to the model documenting its payload. Used by the
#: contract generator and by the payload-shape test.
EVENT_PAYLOAD_MODELS: dict[EventType, type[ApiModel]] = {
    EventType.PROJECT_CREATED: ProjectEventPayload,
    EventType.PROJECT_UPDATED: ProjectEventPayload,
    EventType.PROJECT_COMPLETED: ProjectEventPayload,
    EventType.PROJECT_FAILED: ProjectEventPayload,
    EventType.FILE_UPLOADED: FileEventPayload,
    EventType.FILE_PARSING_STARTED: FileEventPayload,
    EventType.FILE_PARSED: FileEventPayload,
    EventType.FILE_PARSE_FAILED: FileEventPayload,
    EventType.FILE_DELETED: FileEventPayload,
    EventType.WORKFLOW_STARTED: WorkflowEventPayload,
    EventType.WORKFLOW_PAUSED: WorkflowEventPayload,
    EventType.WORKFLOW_RESUMED: WorkflowEventPayload,
    EventType.WORKFLOW_CANCELLED: WorkflowEventPayload,
    EventType.TASK_CREATED: TaskEventPayload,
    EventType.TASK_STARTED: TaskEventPayload,
    EventType.TASK_COMPLETED: TaskEventPayload,
    EventType.TASK_FAILED: TaskEventPayload,
    EventType.TASK_BLOCKED: TaskEventPayload,
    EventType.AGENT_STATUS_CHANGED: AgentStatusEventPayload,
    EventType.AGENT_LOCATION_CHANGED: AgentLocationEventPayload,
    EventType.AGENT_MESSAGE_CREATED: AgentMessageEventPayload,
    EventType.FINDING_CREATED: FindingEventPayload,
    EventType.FINDING_REVIEWED: FindingEventPayload,
    EventType.FINDING_UPDATED: FindingEventPayload,
    EventType.APPROVAL_REQUESTED: ApprovalEventPayload,
    EventType.APPROVAL_DECIDED: ApprovalEventPayload,
    EventType.REPORT_CREATED: ReportEventPayload,
    # Production pipeline.
    EventType.PROJECT_QUESTION_CREATED: ProjectQuestionEventPayload,
    EventType.PROJECT_QUESTION_RESOLVED: ProjectQuestionEventPayload,
    EventType.REQUIREMENT_CREATED: RequirementEventPayload,
    EventType.WORKFLOW_PLANNED: WorkflowPlannedEventPayload,
    EventType.WORKFLOW_FAILED: WorkflowFailedEventPayload,
    EventType.TASK_ASSIGNED: TaskAssignedEventPayload,
    EventType.CHANGESET_CREATED: ChangeSetEventPayload,
    EventType.CHANGESET_VALIDATION_FAILED: ChangeSetValidationFailedEventPayload,
    EventType.CHANGESET_SUBMITTED: ChangeSetEventPayload,
    EventType.CHANGESET_REVIEWED: ChangeSetReviewedEventPayload,
    EventType.CHANGESET_MERGED: ChangeSetMergedEventPayload,
    EventType.REVISION_CREATED: RevisionCreatedEventPayload,
    EventType.GATE_STATUS_CHANGED: GateStatusChangedEventPayload,
    EventType.VALIDATION_STARTED: ValidationStartedEventPayload,
    EventType.VALIDATION_COMPLETED: ValidationCompletedEventPayload,
    EventType.EXPORT_STARTED: ExportStartedEventPayload,
    EventType.EXPORT_ARTIFACT_CREATED: ExportArtifactCreatedEventPayload,
    EventType.RELEASE_CANDIDATE_CREATED: ReleaseCandidateCreatedEventPayload,
    EventType.RELEASE_READY: ReleaseReadyEventPayload,
}
