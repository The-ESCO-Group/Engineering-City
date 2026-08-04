"""The structured actions a model is permitted to propose.

This is the entire surface through which a model can affect the system. Models
get no database handle, no filesystem access and no shell. They return
:class:`AgentAction` objects; the backend validates each one and decides whether
to execute it.

Two rules are enforced here rather than left to prompt discipline, because a
prompt is not a control:

* an action that asserts a technical claim must cite evidence anchors;
* an action that is safety- or equipment-damage-related must request approval.

Every free-text field and every list is bounded. A model that returns a five
megabyte string or a fifty thousand element list is not proposing anything a
human could read, and an unbounded field that feeds a sized database column
turns a rejected action into a failed transaction.
"""

from __future__ import annotations

import re
import uuid
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.enums import (
    ACCEPTABLE_BASIS_KINDS,
    TRACEABLE_OBJECT_TYPES,
    ActionType,
    ApprovalKind,
    ChangeOperationType,
    EvidenceKind,
    FindingCategory,
    FindingSeverity,
    ObjectType,
    QuestionCategory,
)

#: Action types that make a technical assertion and therefore require at least
#: one evidence anchor.
EVIDENCE_REQUIRED_ACTIONS: frozenset[ActionType] = frozenset(
    {ActionType.CREATE_FINDING, ActionType.CREATE_RECOMMENDATION}
)

#: Categories whose findings always concern safety or equipment damage.
SAFETY_SENSITIVE_CATEGORIES: frozenset[FindingCategory] = frozenset(
    {
        FindingCategory.INTERLOCK_DISCREPANCY,
        FindingCategory.MISSING_LOGIC,
        FindingCategory.FAIL_SAFE_UNCLEAR,
    }
)

#: Length of ``Finding.dedupe_key``. Declared here so a payload can never state
#: a key the column cannot hold: on PostgreSQL an over-length value aborts the
#: transaction, which turns one refusable action into a failed task.
DEDUPE_KEY_MAX_LENGTH = 120

#: Length of the ``reference`` columns a basis points at.
REFERENCE_MAX_LENGTH = 64

# Bounded string and list shapes, named so the intent is readable at each field
# and so one cap cannot drift away from its siblings.
Label = Annotated[str, Field(max_length=120)]
Line = Annotated[str, Field(max_length=500)]
Paragraph = Annotated[str, Field(max_length=4_000)]
LongText = Annotated[str, Field(max_length=8_000)]
Lines = Annotated[list[Line], Field(max_length=100)]
Paragraphs = Annotated[list[Paragraph], Field(max_length=100)]

#: A basis names a template the way an expansion records it: ``id@version``.
#: A bare id would leave a reviewer unable to tell which reviewed version of a
#: pattern an object was built from.
TEMPLATE_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]*@[0-9]+\.[0-9]+\.[0-9]+$")


def finding_dedupe_key(agent_kind: str, dedupe_key: str, title: str) -> str:
    """The identity a finding is stored and deduplicated under.

    Namespaced by the agent that produced it. ``dedupe_key`` is model-supplied
    free text and the senior reviewer is shown every finding's key, so an
    un-namespaced key is an address any agent can write to: reusing another
    agent's key overwrote that finding's title, severity and fact in place while
    leaving its review verdict and human decision attached to text nobody read.
    Prefixing the agent kind makes one agent's keyspace unreachable from
    another's, and re-running the same agent still converges on the same row.
    """
    return f"{agent_kind}:{dedupe_key or title}"[:DEDUPE_KEY_MAX_LENGTH]


class AgentAction(BaseModel):
    """One proposed action returned by an agent.

    ``payload`` carries the action-specific body; it is re-validated against the
    matching payload model before execution, so a malformed or unexpected shape
    is rejected rather than partially applied.
    """

    model_config = ConfigDict(extra="forbid")

    action_type: ActionType
    summary: Annotated[str, Field(min_length=1, max_length=500)] = Field(
        description="One-sentence, human-readable statement of what this action does."
    )
    target_type: Label = Field(
        default="",
        description="Entity kind this action concerns (project, file, finding, task).",
    )
    target_id: uuid.UUID | None = None
    evidence_anchor_ids: Annotated[list[uuid.UUID], Field(max_length=100)] = Field(
        default_factory=list,
        description="Anchors supporting the action. Required for technical claims.",
    )
    confidence: Annotated[float, Field(ge=0.0, le=1.0)] = 0.0
    requires_approval: bool = False
    payload: dict[str, Any] = Field(default_factory=dict)
    warnings: Paragraphs = Field(
        default_factory=list,
        description="Caveats the agent wants a human to see. Never suppressed.",
    )

    @field_validator("warnings", "evidence_anchor_ids", mode="before")
    @classmethod
    def _none_to_empty(cls, value: object) -> object:
        return [] if value is None else value

    @model_validator(mode="after")
    def _require_evidence_for_claims(self) -> AgentAction:
        if self.action_type in EVIDENCE_REQUIRED_ACTIONS and not self.evidence_anchor_ids:
            raise ValueError(
                f"{self.action_type.value} requires at least one evidence_anchor_id; "
                "the platform does not accept an unsourced technical claim"
            )
        return self


# ---------------------------------------------------------------------------
# Payload models, one per action type.
# ---------------------------------------------------------------------------
class EvidenceRef(BaseModel):
    """Reference from a payload to an anchor, with the role it plays."""

    model_config = ConfigDict(extra="forbid")

    anchor_id: uuid.UUID
    role: Literal["requirement", "logic", "context"] = "context"
    note: Paragraph = ""


class CreateFindingPayload(BaseModel):
    """Body of a ``create_finding`` action.

    ``fact`` and ``inference`` are separate and both required: the product must
    never present a deduction as an observation.
    """

    model_config = ConfigDict(extra="forbid")

    title: Annotated[str, Field(min_length=1, max_length=255)]
    category: FindingCategory = FindingCategory.OTHER
    severity: FindingSeverity = FindingSeverity.INFORMATIONAL
    fact: Annotated[str, Field(min_length=1, max_length=8000)] = Field(
        description="Strictly what the cited evidence shows, with no interpretation."
    )
    inference: Annotated[str, Field(max_length=8000)] = Field(
        default="",
        description="What the agent concludes from the fact. Presented as a conclusion.",
    )
    suggested_review_action: Annotated[str, Field(max_length=4000)] = ""
    limitations: Annotated[str, Field(max_length=4000)] = Field(
        default="",
        description="Anything that weakens the finding: partial parse, ambiguity, missing input.",
    )
    evidence: Annotated[list[EvidenceRef], Field(max_length=50)] = Field(default_factory=list)
    details: dict[str, Any] = Field(default_factory=dict)
    #: Stable identity for deduplication, within the creating agent's namespace.
    #: Generated by the backend if omitted. Capped at the width of the column it
    #: is stored in, so an over-length key is a refused action rather than a
    #: truncated row on SQLite and a failed transaction on PostgreSQL.
    dedupe_key: Annotated[str, Field(max_length=DEDUPE_KEY_MAX_LENGTH)] = ""

    @model_validator(mode="after")
    def _safety_categories_need_review_action(self) -> CreateFindingPayload:
        if self.category in SAFETY_SENSITIVE_CATEGORIES and not self.suggested_review_action:
            raise ValueError(
                f"category {self.category.value} concerns safety-relevant logic and must "
                "state a suggested_review_action for a human engineer"
            )
        return self


class CreateRecommendationPayload(BaseModel):
    """Body of a ``create_recommendation`` action.

    A recommendation is a proposal for a human. The platform never applies one,
    and a safety- or damage-related proposal cannot be marked as not needing
    approval.
    """

    model_config = ConfigDict(extra="forbid")

    title: Annotated[str, Field(min_length=1, max_length=255)]
    body: Annotated[str, Field(min_length=1, max_length=8000)]
    rationale: LongText = ""
    safety_related: bool = False
    equipment_damage_related: bool = False
    effort_estimate: Label = ""
    finding_id: uuid.UUID | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class CreateTaskPayload(BaseModel):
    """Body of a ``create_task`` action (Project Manager Agent only)."""

    model_config = ConfigDict(extra="forbid")

    kind: Label
    title: Annotated[str, Field(min_length=1, max_length=255)]
    description: Paragraph = ""
    agent_kind: Label = ""
    #: Indices into the same plan's task list that must complete first.
    depends_on_indices: Annotated[list[int], Field(max_length=100)] = Field(default_factory=list)
    priority: int = 100
    payload: dict[str, Any] = Field(default_factory=dict)


class PostMessagePayload(BaseModel):
    """Body of a ``post_message`` action: a short note for the activity feed."""

    model_config = ConfigDict(extra="forbid")

    body: Annotated[str, Field(min_length=1, max_length=4000)]
    context: dict[str, Any] = Field(default_factory=dict)


class RequestApprovalPayload(BaseModel):
    """Body of a ``request_approval`` action."""

    model_config = ConfigDict(extra="forbid")

    kind: ApprovalKind
    title: Annotated[str, Field(min_length=1, max_length=255)]
    question: Annotated[str, Field(min_length=1, max_length=4000)]
    finding_id: uuid.UUID | None = None
    recommendation_id: uuid.UUID | None = None
    context: dict[str, Any] = Field(default_factory=dict)


class UpdateFindingReviewPayload(BaseModel):
    """Body of an ``update_finding_review`` action (Senior Review Agent only).

    The senior reviewer may lower confidence, adjust severity and reject
    unsupported claims. It may not delete a finding, and it may not raise
    confidence above what the originating agent claimed — review can only add
    doubt, never manufacture certainty.
    """

    model_config = ConfigDict(extra="forbid")

    finding_id: uuid.UUID
    verdict: Literal["supported", "unsupported", "duplicate", "needs_more_evidence"]
    notes: Paragraph = ""
    adjusted_severity: FindingSeverity | None = None
    #: Compared against the *finding's* current confidence by the runner, which
    #: is the only place that number is in hand.
    adjusted_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    duplicate_of_finding_id: uuid.UUID | None = None
    parser_limitations: Lines = Field(default_factory=list)

    @model_validator(mode="after")
    def _duplicate_needs_target(self) -> UpdateFindingReviewPayload:
        if self.verdict == "duplicate" and self.duplicate_of_finding_id is None:
            raise ValueError("verdict 'duplicate' requires duplicate_of_finding_id")
        return self


class ReportBlockerPayload(BaseModel):
    """Body of a ``report_blocker`` action: work cannot proceed, and why."""

    model_config = ConfigDict(extra="forbid")

    reason: Annotated[str, Field(min_length=1, max_length=4000)]
    #: Inputs that were expected but not supplied.
    missing_inputs: Lines = Field(default_factory=list)
    #: Whether a human can unblock this by uploading something.
    resolvable_by_upload: bool = False


class ReportNoFindingsPayload(BaseModel):
    """Body of a ``report_no_findings`` action.

    Explicit rather than implied: an agent that found nothing must say so, and
    say what it looked at, so silence is never mistaken for a clean result.
    """

    model_config = ConfigDict(extra="forbid")

    scope_examined: Annotated[str, Field(min_length=1, max_length=4000)]
    limitations: Paragraph = ""


# ---------------------------------------------------------------------------
# Production platform: proposing changes to the canonical engineering model.
# ---------------------------------------------------------------------------
class ObjectBasis(BaseModel):
    """Why a proposed object should exist.

    A generated tag, rung, alarm or test case must be derivable from something a
    human can check: a document, a requirement, an approved template rule, or a
    named engineering decision. This is that statement, and it is validated at
    the action boundary — the earliest and cheapest point at which a missing
    basis can be caught.
    """

    model_config = ConfigDict(extra="forbid")

    kind: EvidenceKind
    anchor_id: uuid.UUID | None = None
    requirement_reference: Annotated[str, Field(max_length=REFERENCE_MAX_LENGTH)] = ""
    decision_reference: Annotated[str, Field(max_length=REFERENCE_MAX_LENGTH)] = ""
    #: Qualified template identifier, e.g. ``esco.motor.standard@1.0.0``. The
    #: shape is checked here; whether the template is actually loaded is checked
    #: by the runner, which has the registry.
    template_id: Annotated[str, Field(max_length=140)] = ""
    note: Paragraph = ""

    @model_validator(mode="after")
    def _must_actually_point_at_something(self) -> ObjectBasis:
        if self.kind not in ACCEPTABLE_BASIS_KINDS:
            raise ValueError(
                f"{self.kind.value} is evidence *about* a document, not a reason for an "
                f"object to exist; acceptable: "
                f"{sorted(k.value for k in ACCEPTABLE_BASIS_KINDS)}"
            )
        if not (
            self.anchor_id
            or self.requirement_reference
            or self.decision_reference
            or self.template_id
        ):
            raise ValueError(
                "a basis must name an anchor, a requirement, a decision or a template; "
                "an empty basis asserts traceability without providing it"
            )
        return self

    @model_validator(mode="after")
    def _a_template_basis_must_name_a_version(self) -> ObjectBasis:
        """``template_expansion`` is the strongest provenance the system has.

        An object whose basis is a template rule is stamped as produced by an
        approved, checksummed, once-reviewed pattern. A reviewer following that
        citation has to be able to open the exact version it names, so an
        unversioned or malformed id is refused before it can be recorded as
        provenance it is not.
        """
        if self.template_id and not TEMPLATE_ID_PATTERN.match(self.template_id):
            raise ValueError(
                f"template_id {self.template_id!r} is not a qualified template "
                "identifier; state it as 'id@major.minor.patch' (for example "
                "'esco.motor.standard@1.0.0') so a reviewer can open the exact "
                "version this object was built from"
            )
        return self


class ChangeOperationProposal(BaseModel):
    """One structured edit a model is proposing.

    Deliberately narrow. A model cannot write SQL, cannot name a table, and
    cannot supply a value for a column that is not part of the object it names
    — deterministic code decides how an operation becomes rows.
    """

    model_config = ConfigDict(extra="forbid")

    operation_type: ChangeOperationType
    object_type: ObjectType
    #: Omitted for a create; the id is assigned deterministically at merge.
    object_id: uuid.UUID | None = None
    #: Lets a later operation in the same change set refer to something an
    #: earlier one creates, before a real id exists.
    temp_key: Label = ""
    #: The ``object_revision`` this edit was written against. Required for an
    #: update or a delete: without it there is nothing to detect a conflict
    #: against, and the edit would silently overwrite concurrent work.
    based_on_object_revision: int | None = None

    field_name: Label = ""
    values: dict[str, Any] = Field(default_factory=dict)

    related_object_type: ObjectType | None = None
    related_object_id: uuid.UUID | None = None
    relationship_key: Label = ""

    #: Why this object should exist. Required when creating one of the object
    #: kinds the traceability rule covers.
    basis: Annotated[list[ObjectBasis], Field(max_length=20)] = Field(default_factory=list)
    description: Paragraph = ""

    @model_validator(mode="after")
    def _updates_must_state_what_they_are_based_on(self) -> ChangeOperationProposal:
        needs_base = {
            ChangeOperationType.UPDATE_FIELD,
            ChangeOperationType.DELETE_OBJECT,
            ChangeOperationType.UPDATE_LOGIC,
            ChangeOperationType.RENAME_TAG,
        }
        if self.operation_type in needs_base:
            if self.object_id is None:
                raise ValueError(f"{self.operation_type.value} must name an object_id")
            if self.based_on_object_revision is None:
                raise ValueError(
                    f"{self.operation_type.value} must state based_on_object_revision; "
                    "without it a concurrent edit cannot be detected and would be "
                    "silently overwritten"
                )
        if self.operation_type is ChangeOperationType.UPDATE_FIELD and not self.field_name:
            raise ValueError("update_field must name the field")
        return self

    @model_validator(mode="after")
    def _created_objects_must_state_a_basis(self) -> ChangeOperationProposal:
        creating = self.operation_type in {
            ChangeOperationType.CREATE_OBJECT,
            ChangeOperationType.CREATE_LOGIC,
        }
        if creating and self.object_type in TRACEABLE_OBJECT_TYPES and not self.basis:
            raise ValueError(
                f"creating a {self.object_type.value} requires at least one basis "
                "(a requirement, a template rule, a human decision or a cited "
                "document); an object that exists for no stated reason cannot be "
                "reviewed"
            )
        return self

    @model_validator(mode="after")
    def _renames_must_supply_both_names(self) -> ChangeOperationProposal:
        if self.operation_type is ChangeOperationType.RENAME_TAG:
            missing = [key for key in ("from_name", "to_name") if not self.values.get(key)]
            if missing:
                raise ValueError(f"rename_tag requires values {missing}")
        return self

    @model_validator(mode="after")
    def _a_rename_cannot_be_disguised_as_a_field_update(self) -> ChangeOperationProposal:
        """Writing ``name`` on a tag is a rename, whatever the operation is called.

        ``update_field`` on a tag's ``name`` renames it exactly as thoroughly as
        ``rename_tag`` — ``impact._renamed_to`` says so — but it reaches none of
        the machinery a rename needs: the change-set approval gate counts only
        ``rename_tag`` operations, the merge's ``from_name``-versus-current-name
        consistency check runs only for ``rename_tag``, and ``name`` is not a
        protected field. Left open, the operation that skips the impact report is
        the easier of the two to write. It is refused here, at the earliest and
        cheapest point, and the author is told which form to use instead.
        """
        if (
            self.operation_type is ChangeOperationType.UPDATE_FIELD
            and self.object_type is ObjectType.TAG
            and self.field_name == "name"
        ):
            raise ValueError(
                "update_field on a tag's 'name' is a rename: propose it as "
                "rename_tag with from_name and to_name, on a change set whose "
                "requires_human_approval is true, so the impact report reaches "
                "the reviewer before the tag moves"
            )
        return self


class ProposeChangesetPayload(BaseModel):
    """Body of a ``propose_changeset`` action.

    A model proposes; deterministic code validates, detects conflicts, applies a
    human gate where one is warranted, and merges. Nothing here writes anything.
    """

    model_config = ConfigDict(extra="forbid")

    title: Annotated[str, Field(min_length=1, max_length=300)]
    summary: Annotated[str, Field(min_length=1, max_length=4000)]
    #: Short and user-facing. Never a transcript of model reasoning.
    rationale: Paragraph = ""
    base_revision_number: int = Field(
        ge=0, description="The revision this change set was written against."
    )
    operations: Annotated[list[ChangeOperationProposal], Field(min_length=1, max_length=2000)]
    assumptions: Paragraphs = Field(default_factory=list)
    warnings: Paragraphs = Field(default_factory=list)
    #: Named by the agent when it believes the change touches a restricted
    #: domain. The backend checks independently; this is a declaration, not a
    #: permission.
    restricted_domains: Annotated[list[Label], Field(max_length=50)] = Field(default_factory=list)
    requires_human_approval: bool = False

    @model_validator(mode="after")
    def _renames_always_need_a_human(self) -> ProposeChangesetPayload:
        """A tag rename cannot be self-approved.

        Renaming a tag reaches into logic, HMI bindings, alarms, tests and
        documentation. The impact report exists so an engineer can see that list
        before agreeing to it, and a change set that renames while claiming to
        need no approval is asking to skip exactly that step.

        Only ``rename_tag`` is counted here because that is now the only shape a
        rename can arrive in:
        :meth:`ChangeOperationProposal._a_rename_cannot_be_disguised_as_a_field_update`
        refuses the ``update_field``-on-``name`` form one level down.
        """
        renames = [
            op for op in self.operations if op.operation_type is ChangeOperationType.RENAME_TAG
        ]
        if renames and not self.requires_human_approval:
            raise ValueError(
                f"{len(renames)} rename_tag operation(s) present but "
                "requires_human_approval is false; a rename must be approved with "
                "its impact report in front of the reviewer"
            )
        return self


class ReviewChangesetPayload(BaseModel):
    """Body of a ``review_changeset`` action: the Senior Review verdict."""

    model_config = ConfigDict(extra="forbid")

    changeset_id: uuid.UUID
    verdict: Literal["approve", "request_changes", "reject"]
    #: Required for anything other than a clean approval, so a rejection is
    #: never a bare refusal an author cannot act on.
    concerns: Paragraphs = Field(default_factory=list)
    #: Senior review may lower a confidence, never raise it. Enforced by the
    #: runner against the change set's own recorded confidence; declared here so
    #: the intent is visible in the payload.
    adjusted_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    notes: Paragraph = ""

    @model_validator(mode="after")
    def _a_refusal_must_say_why(self) -> ReviewChangesetPayload:
        if self.verdict != "approve" and not self.concerns:
            raise ValueError(f"verdict '{self.verdict}' requires at least one concern")
        return self


class PlannedTask(BaseModel):
    """One task in a proposed plan."""

    model_config = ConfigDict(extra="forbid")

    key: Annotated[str, Field(min_length=1, max_length=60)]
    task_kind: Label
    title: Annotated[str, Field(min_length=1, max_length=300)]
    reason: Paragraph = ""
    depends_on: Annotated[list[Label], Field(max_length=100)] = Field(default_factory=list)
    #: Whether the dependency is required. A soft dependency lets a run continue
    #: past an input that could not be read, rather than stalling forever.
    hard_dependencies: bool = True
    payload: dict[str, Any] = Field(default_factory=dict)


class CreateTaskPlanPayload(BaseModel):
    """Body of a ``create_task_plan`` action: the Project Manager's plan."""

    model_config = ConfigDict(extra="forbid")

    summary: Annotated[str, Field(min_length=1, max_length=4000)]
    tasks: Annotated[list[PlannedTask], Field(min_length=1, max_length=200)]
    #: Work the plan deliberately does not cover, and why.
    out_of_scope: Paragraphs = Field(default_factory=list)
    risks: Paragraphs = Field(default_factory=list)
    #: Blockers that must be resolved before the plan can start.
    blocked_by_questions: Paragraphs = Field(default_factory=list)

    @model_validator(mode="after")
    def _dependencies_must_exist_within_the_plan(self) -> CreateTaskPlanPayload:
        keys = {task.key for task in self.tasks}
        for task in self.tasks:
            unknown = [dep for dep in task.depends_on if dep not in keys]
            if unknown:
                raise ValueError(
                    f"task {task.key!r} depends on {unknown}, which the plan does not "
                    "define; a dangling dependency would leave the task queued forever"
                )
        return self


class RequestClarificationPayload(BaseModel):
    """Body of a ``request_clarification`` action.

    The product must never fill a gap by inventing a plausible answer. This is
    the alternative, and it is why ``competing_readings`` is not optional
    decoration: "this is ambiguous" without saying *how* leaves a human with
    nothing to decide.
    """

    model_config = ConfigDict(extra="forbid")

    category: QuestionCategory
    question: Annotated[str, Field(min_length=1, max_length=2000)]
    #: What is blocked until this is answered.
    why_it_matters: Annotated[str, Field(min_length=1, max_length=2000)]
    #: The readings the text will support. Two or more, or none at all if the
    #: problem is missing information rather than ambiguity.
    competing_readings: Paragraphs = Field(default_factory=list)
    is_mandatory: bool = True
    blocks_task_kinds: Annotated[list[Label], Field(max_length=50)] = Field(default_factory=list)
    related_object_type: ObjectType | None = None
    related_object_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def _ambiguity_must_name_the_alternatives(self) -> RequestClarificationPayload:
        if len(self.competing_readings) == 1:
            raise ValueError(
                "competing_readings has one entry; either name the alternative "
                "reading as well or leave the list empty and state the missing "
                "information in the question"
            )
        return self


#: Preview kinds whose content is a statement about what will actually be built:
#: the rungs, the import order, the objects a rename touches. These are computed
#: from the change set, never composed by a model, so each one must name the
#: change set it is a preview *of*.
DERIVED_PREVIEW_KINDS: frozenset[str] = frozenset({"ladder_text", "import_order", "impact"})


class GeneratePreviewPayload(BaseModel):
    """Body of a ``generate_preview`` action: something for a human to read.

    A preview is a rendering, never an artifact. It exists so an engineer can
    see what a change set would produce before approving it, and producing one
    must not create or modify anything.
    """

    model_config = ConfigDict(extra="forbid")

    preview_kind: Literal["ladder_text", "tag_table", "import_order", "impact", "summary"]
    title: Annotated[str, Field(min_length=1, max_length=300)]
    content: Annotated[str, Field(min_length=1, max_length=200_000)]
    changeset_id: uuid.UUID | None = None
    object_type: ObjectType | None = None
    object_id: uuid.UUID | None = None
    limitations: Paragraphs = Field(default_factory=list)

    @model_validator(mode="after")
    def _a_derived_preview_must_name_its_changeset(self) -> GeneratePreviewPayload:
        """The one thing a preview must not do is disagree with what will be built.

        An impact preview reading "0 rungs, 0 HMI bindings and 0 alarms affected"
        is the exact claim the impact report exists to make, and a model can
        write that sentence without consulting anything. Requiring the change set
        id is the part of that this layer can enforce: it makes the preview
        checkable against ``engineering.impact`` rather than free-floating prose.
        """
        if self.preview_kind in DERIVED_PREVIEW_KINDS and self.changeset_id is None:
            raise ValueError(
                f"a '{self.preview_kind}' preview states what a change set would "
                "produce, so it must name changeset_id; content that cites no "
                "change set cannot be checked against what will actually be built"
            )
        return self


class CompleteTaskPayload(BaseModel):
    """Body of a ``complete_task`` action.

    ``limitations`` is required in spirit and empty only when the agent really
    did examine everything it was asked to. A completion that states no limits
    is a stronger claim than most runs can support.
    """

    model_config = ConfigDict(extra="forbid")

    outcome: Annotated[str, Field(min_length=1, max_length=4000)]
    objects_created: dict[str, int] = Field(default_factory=dict)
    limitations: Paragraphs = Field(default_factory=list)
    follow_up: Paragraphs = Field(default_factory=list)


#: Payload model for each action type. An action whose payload fails to validate
#: against its model is rejected before execution.
ACTION_PAYLOAD_MODELS: dict[ActionType, type[BaseModel]] = {
    ActionType.CREATE_FINDING: CreateFindingPayload,
    ActionType.CREATE_RECOMMENDATION: CreateRecommendationPayload,
    ActionType.CREATE_TASK: CreateTaskPayload,
    ActionType.POST_MESSAGE: PostMessagePayload,
    ActionType.REQUEST_APPROVAL: RequestApprovalPayload,
    ActionType.UPDATE_FINDING_REVIEW: UpdateFindingReviewPayload,
    ActionType.REPORT_BLOCKER: ReportBlockerPayload,
    ActionType.REPORT_NO_FINDINGS: ReportNoFindingsPayload,
    ActionType.CREATE_TASK_PLAN: CreateTaskPlanPayload,
    ActionType.REQUEST_CLARIFICATION: RequestClarificationPayload,
    ActionType.PROPOSE_CHANGESET: ProposeChangesetPayload,
    ActionType.REVIEW_CHANGESET: ReviewChangesetPayload,
    ActionType.GENERATE_PREVIEW: GeneratePreviewPayload,
    ActionType.COMPLETE_TASK: CompleteTaskPayload,
}


def payload_model_for(action_type: ActionType) -> type[BaseModel]:
    """Return the payload model for an action type."""
    try:
        return ACTION_PAYLOAD_MODELS[action_type]
    except KeyError as exc:  # pragma: no cover - unreachable while the map is total
        raise ValueError(f"no payload model registered for {action_type!r}") from exc
