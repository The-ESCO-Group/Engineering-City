"""Ways a model got something past the action contract, and no longer can.

Every test here is a payload that an adversarial audit put through the real
boundary and watched succeed. Each one is paired with a legitimate neighbour
that must still be accepted, because a rule that refuses valid proposals is as
damaging as one that lets bad ones through: an agent facing a refusal it cannot
comply with has no move left except to stop declaring what it is doing.

The boundary under test is the action contract itself — the payload schemas in
``app.agents.base.actions``, the state-aware checks in
``app.agents.base.runner``, and the write-time gates in
``app.agents.base.executor``. Nothing here goes through a model.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agents.base.actions import (
    ACTION_PAYLOAD_MODELS,
    AgentAction,
    ChangeOperationProposal,
    CompleteTaskPayload,
    CreateFindingPayload,
    CreateRecommendationPayload,
    CreateTaskPayload,
    CreateTaskPlanPayload,
    GeneratePreviewPayload,
    ObjectBasis,
    PlannedTask,
    PostMessagePayload,
    ProposeChangesetPayload,
    ReportBlockerPayload,
    ReportNoFindingsPayload,
    RequestApprovalPayload,
    RequestClarificationPayload,
    ReviewChangesetPayload,
    UpdateFindingReviewPayload,
    finding_dedupe_key,
)
from app.agents.base.executor import ActionExecutor
from app.agents.base.provider import ChatMessage, ProviderResponse
from app.agents.base.runner import AgentRunner, RunOutcome, ValidatedAction
from app.core.clock import utcnow
from app.core.config import Settings
from app.core.enums import (
    ActionType,
    AnchorType,
    ApprovalKind,
    AuditAction,
    ChangeOperationType,
    EvidenceKind,
    FileKind,
    FileStatus,
    FindingCategory,
    FindingSeverity,
    FindingStatus,
    ObjectType,
    ProviderKind,
    QuestionCategory,
)
from app.generators.plc.template_registry import get_registry
from app.models.audit import AuditEntry
from app.models.evidence import EvidenceAnchor
from app.models.file import UploadedFile
from app.models.finding import Finding
from app.models.project import Project
from app.models.user import User

AGENT_KIND = "plc_review"
OTHER_AGENT_KIND = "interlock_review"
#: Resolved rather than written out. A basis naming a version this installation
#: does not ship is now refused, which is one of the rules under test here, so a
#: literal would make this file go stale the next time a pattern is revised.
MOTOR_TEMPLATE = f"esco.motor.standard@{get_registry().versions('esco.motor.standard')[-1]}"


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------
class ScriptedProvider:
    """Returns exactly the actions a test names, and records what it was asked."""

    kind = ProviderKind.MOCK
    model = "scripted"

    def __init__(self, actions: list[dict[str, Any]]) -> None:
        self.actions = actions
        self.max_output_tokens = 0
        self.temperature = -1.0

    def complete_structured(
        self,
        *,
        system: str,
        messages: list[ChatMessage],
        schema: dict[str, Any],
        max_output_tokens: int = 4096,
        temperature: float = 0.0,
    ) -> ProviderResponse:
        self.max_output_tokens = max_output_tokens
        self.temperature = temperature
        return ProviderResponse(
            data={"rationale": "scripted", "actions": self.actions},
            provider="mock",
            model=self.model,
        )


def propose(
    db: Session,
    project: Project,
    settings: Settings,
    action: dict[str, Any],
    *,
    agent_kind: str = AGENT_KIND,
    anchor_ids: set[uuid.UUID] | None = None,
    allowed_object_types: tuple[ObjectType, ...] = (),
) -> RunOutcome:
    """Put one proposed action through the real runner."""
    runner = AgentRunner(
        db,
        settings,
        project=project,
        agent_kind=agent_kind,
        provider=ScriptedProvider([action]),
    )
    return runner.run(
        system_prompt="test",
        context={},
        allowed_actions=set(ActionType),
        anchor_ids=anchor_ids if anchor_ids is not None else set(),
        allowed_object_types=allowed_object_types,
    )


def empty_run(db: Session, project: Project, settings: Settings) -> RunOutcome:
    """A completed run row to hang a hand-built action off."""
    return propose(
        db,
        project,
        settings,
        {
            "action_type": ActionType.POST_MESSAGE.value,
            "summary": "Starting.",
            "payload": {"body": "Starting."},
        },
    )


def validated(action_type: ActionType, payload: BaseModel, **action_fields: Any) -> ValidatedAction:
    """An action as it looks after the runner, for driving the executor directly."""
    return ValidatedAction(
        action=AgentAction(
            action_type=action_type, summary=f"probe {action_type.value}", **action_fields
        ),
        payload=payload,
    )


def reasons(outcome: RunOutcome) -> str:
    return " | ".join(item.reason for item in outcome.rejected)


def count_findings(db: Session, project: Project) -> int:
    return int(
        db.execute(
            select(func.count(Finding.id)).where(Finding.project_id == project.id)
        ).scalar_one()
    )


@pytest.fixture
def other_project(db: Session, seeded: dict[str, User], settings: Settings) -> Project:
    """A second project, so "belongs to this one" is testable rather than assumed."""
    from app.core.services import projects as project_service

    created = project_service.create_project(
        db,
        name="Synthetic Bottling Line 1",
        description="A different customer's project.",
        client_reference="TEST-002",
        attributes={},
        creator=seeded["engineer"],
        settings=settings,
    )
    db.flush()
    return created


def make_anchor(db: Session, project: Project, *, key: str = "rung:Main/R1/0") -> EvidenceAnchor:
    """One citable anchor in a project, with the file it needs in order to exist."""
    uploaded = UploadedFile(
        project_id=project.id,
        original_filename=f"{uuid.uuid4().hex[:8]}.L5X",
        stored_filename=f"{uuid.uuid4().hex}.l5x",
        relative_path=f"{project.id}/{uuid.uuid4().hex}.l5x",
        media_type="application/xml",
        size_bytes=64,
        sha256=uuid.uuid4().hex,
        file_kind=FileKind.L5X,
        status=FileStatus.PARSED,
    )
    db.add(uploaded)
    db.flush()

    anchor = EvidenceAnchor(
        project_id=project.id,
        file_id=uploaded.id,
        anchor_key=key,
        anchor_type=AnchorType.L5X_RUNG,
        evidence_kind=EvidenceKind.DIRECT,
        source_label=uploaded.original_filename,
        display_location="MainProgram / Conveyor_Interlocks / Rung 12",
        excerpt="XIC(Guard_Closed)OTE(Conveyor_Run);",
    )
    db.add(anchor)
    db.flush()
    return anchor


def make_finding(db: Session, project: Project, **overrides: Any) -> Finding:
    values: dict[str, Any] = {
        "project_id": project.id,
        "dedupe_key": finding_dedupe_key(AGENT_KIND, "guard-door", "Guard door"),
        "created_by_agent_kind": AGENT_KIND,
        "title": "Guard-door interlock is not proven",
        "category": FindingCategory.INTERLOCK_DISCREPANCY,
        "severity": FindingSeverity.HIGH,
        "fact": "Rung 12 energises the drive with no guard-door contact.",
        "confidence": 0.9,
        "status": FindingStatus.OPEN,
    }
    values.update(overrides)
    finding = Finding(**values)
    db.add(finding)
    db.flush()
    return finding


def accept_as_human(db: Session, finding: Finding, user: User) -> Finding:
    """Exactly what ``api/routes/approvals.py::_apply`` writes on an approval."""
    finding.status = FindingStatus.ACCEPTED
    finding.decided_by_id = user.id
    finding.decided_at = utcnow()
    finding.decision_notes = "Confirmed on site. Raise a work order."
    db.flush()
    return finding


def finding_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "title": "Guard-door interlock is not proven",
        "category": FindingCategory.INTERLOCK_DISCREPANCY.value,
        "severity": FindingSeverity.HIGH.value,
        "fact": "Rung 12 energises the drive with no guard-door contact.",
        "suggested_review_action": "Confirm the guard-door circuit against the schematic.",
        "dedupe_key": "guard-door",
    }
    payload.update(overrides)
    return payload


def finding_action(anchor: EvidenceAnchor, **overrides: Any) -> dict[str, Any]:
    return {
        "action_type": ActionType.CREATE_FINDING.value,
        "summary": "Guard-door interlock is not proven.",
        "confidence": 0.9,
        "evidence_anchor_ids": [str(anchor.id)],
        "payload": finding_payload(**overrides),
    }


def changeset_action(operations: list[dict[str, Any]], **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "title": "Line 4 motor control",
        "summary": "One motor block expanded from the approved standard pattern.",
        "base_revision_number": 0,
        "operations": operations,
    }
    payload.update(overrides)
    return {
        "action_type": ActionType.PROPOSE_CHANGESET.value,
        "summary": "Create the conveyor motor control block.",
        "confidence": 0.5,
        "payload": payload,
    }


# ---------------------------------------------------------------------------
# A human decision is the end of the argument
# ---------------------------------------------------------------------------
def test_a_senior_review_cannot_overturn_a_finding_a_human_accepted(
    db: Session, project: Project, settings: Settings, seeded: dict[str, User]
) -> None:
    """The audited bypass: an accepted critical finding deleted from the report.

    A verdict of 'unsupported' set ``status=rejected`` and cleared
    ``requires_approval``, which removed the finding from
    ``reporting.snapshot._accepted_findings`` — while the engineer's name,
    timestamp and decision notes stayed attached to it.
    """
    finding = accept_as_human(db, make_finding(db, project), seeded["engineer"])

    outcome = propose(
        db,
        project,
        settings,
        {
            "action_type": ActionType.UPDATE_FINDING_REVIEW.value,
            "summary": "Not supported by the cited rung.",
            "confidence": 0.4,
            "payload": {
                "finding_id": str(finding.id),
                "verdict": "unsupported",
                "notes": "I read the rung differently.",
            },
        },
        agent_kind="senior_review",
    )

    assert outcome.accepted == []
    assert "decided by a named engineer" in reasons(outcome)
    db.refresh(finding)
    assert finding.status is FindingStatus.ACCEPTED
    assert finding.decision_notes == "Confirmed on site. Raise a work order."


def test_a_senior_review_may_still_verdict_an_undecided_finding(
    db: Session, project: Project, settings: Settings
) -> None:
    """The neighbour: review is the whole point, right up until a human rules."""
    finding = make_finding(db, project)

    outcome = propose(
        db,
        project,
        settings,
        {
            "action_type": ActionType.UPDATE_FINDING_REVIEW.value,
            "summary": "Not supported by the cited rung.",
            "confidence": 0.4,
            "payload": {
                "finding_id": str(finding.id),
                "verdict": "unsupported",
                "notes": "The cited rung does not show what the fact claims.",
            },
        },
        agent_kind="senior_review",
    )

    assert reasons(outcome) == ""
    assert len(outcome.accepted) == 1


def test_the_executor_refuses_to_re_verdict_a_decided_finding(
    db: Session, project: Project, settings: Settings, seeded: dict[str, User]
) -> None:
    """Second gate, driven straight at the executor.

    The runner refuses this too, but the runner is not the only way into
    ``_update_review`` and a human decision is not something to lose on a code
    path nobody thought about.
    """
    finding = accept_as_human(db, make_finding(db, project), seeded["engineer"])
    outcome = empty_run(db, project, settings)
    outcome.accepted = [
        validated(
            ActionType.UPDATE_FINDING_REVIEW,
            UpdateFindingReviewPayload(finding_id=finding.id, verdict="unsupported"),
        )
    ]

    result = ActionExecutor(db, project, agent_kind="senior_review", run_id=outcome.run.id).execute(
        outcome
    )

    assert result.reviewed == []
    assert [item.action_type for item in result.unexecuted] == ["update_finding_review"]
    db.refresh(finding)
    assert finding.is_reviewed is False
    assert finding.status is FindingStatus.ACCEPTED


def test_a_review_of_a_finding_in_another_project_is_refused(
    db: Session, project: Project, settings: Settings, other_project: Project
) -> None:
    theirs = make_finding(db, other_project)
    outcome = propose(
        db,
        project,
        settings,
        {
            "action_type": ActionType.UPDATE_FINDING_REVIEW.value,
            "summary": "Reviewed.",
            "confidence": 0.4,
            "payload": {"finding_id": str(theirs.id), "verdict": "supported"},
        },
        agent_kind="senior_review",
    )
    assert outcome.accepted == []
    assert "not a finding in this project" in reasons(outcome)


# ---------------------------------------------------------------------------
# One agent cannot address another agent's finding
# ---------------------------------------------------------------------------
def test_a_dedupe_key_is_namespaced_to_the_agent_that_created_it(
    db: Session, project: Project, settings: Settings
) -> None:
    """The audited bypass: rewriting an approved finding by reusing its key.

    ``senior_review.build_context`` shows every finding's ``dedupe_key`` to a
    model, so an un-namespaced key is an address any agent can write to. The
    audit landed an informational documentation note on top of an accepted
    high-severity interlock finding, keeping its status, its review verdict and
    its approval row.
    """
    anchor = make_anchor(db, project)
    original = make_finding(db, project)

    outcome = propose(
        db,
        project,
        settings,
        finding_action(
            anchor,
            title="Naming is slightly inconsistent",
            category=FindingCategory.DOCUMENTATION_GAP.value,
            severity=FindingSeverity.INFORMATIONAL.value,
            fact="Two tags differ in case.",
            suggested_review_action="",
            dedupe_key="guard-door",
        ),
        agent_kind=OTHER_AGENT_KIND,
        anchor_ids={anchor.id},
    )
    ActionExecutor(db, project, agent_kind=OTHER_AGENT_KIND, run_id=outcome.run.id).execute(outcome)

    db.refresh(original)
    assert original.title == "Guard-door interlock is not proven"
    assert original.severity is FindingSeverity.HIGH
    assert count_findings(db, project) == 2, (
        "the second agent's observation must land as its own finding, not overwrite one"
    )


def test_re_running_the_same_agent_still_converges_on_one_finding(
    db: Session, project: Project, settings: Settings
) -> None:
    """The neighbour: the queue is at-least-once, so a re-run must not accumulate."""
    anchor = make_anchor(db, project)

    for _ in range(2):
        outcome = propose(db, project, settings, finding_action(anchor), anchor_ids={anchor.id})
        ActionExecutor(db, project, agent_kind=AGENT_KIND, run_id=outcome.run.id).execute(outcome)

    assert count_findings(db, project) == 1


def test_a_human_decided_finding_is_not_rewritten_by_its_own_agent(
    db: Session, project: Project, settings: Settings, seeded: dict[str, User]
) -> None:
    anchor = make_anchor(db, project)
    finding = accept_as_human(db, make_finding(db, project), seeded["engineer"])

    outcome = propose(
        db,
        project,
        settings,
        finding_action(
            anchor,
            title="Naming is slightly inconsistent",
            category=FindingCategory.DOCUMENTATION_GAP.value,
            severity=FindingSeverity.INFORMATIONAL.value,
            fact="Two tags differ in case.",
            suggested_review_action="",
        ),
        anchor_ids={anchor.id},
    )

    assert outcome.accepted == []
    assert "decided by a named engineer" in reasons(outcome)
    db.refresh(finding)
    assert finding.title == "Guard-door interlock is not proven"
    assert finding.severity is FindingSeverity.HIGH


def test_replacing_a_reviewed_finding_clears_the_verdict_it_invalidates(
    db: Session, project: Project, settings: Settings
) -> None:
    """A verdict is about the text it was made against.

    Not a refusal: an agent is entitled to update its own open finding. What it
    is not entitled to do is inherit "reviewed and supported" for content no
    reviewer read.
    """
    anchor = make_anchor(db, project)
    finding = make_finding(db, project, is_reviewed=True, review_verdict="supported")

    outcome = propose(
        db,
        project,
        settings,
        finding_action(
            anchor,
            title="Guard-door interlock is not proven on the second drive either",
            fact="Rung 14 also energises a drive with no guard-door contact.",
        ),
        anchor_ids={anchor.id},
    )
    ActionExecutor(db, project, agent_kind=AGENT_KIND, run_id=outcome.run.id).execute(outcome)

    db.refresh(finding)
    assert finding.is_reviewed is False
    assert finding.review_verdict == ""
    assert any("cleared" in warning for warning in finding.warnings)


def test_an_unchanged_re_run_keeps_the_review_verdict(
    db: Session, project: Project, settings: Settings
) -> None:
    """The neighbour: an idempotent retry must not throw away a real review."""
    anchor = make_anchor(db, project)
    finding = make_finding(db, project, is_reviewed=True, review_verdict="supported")

    outcome = propose(db, project, settings, finding_action(anchor), anchor_ids={anchor.id})
    ActionExecutor(db, project, agent_kind=AGENT_KIND, run_id=outcome.run.id).execute(outcome)

    db.refresh(finding)
    assert finding.is_reviewed is True
    assert finding.review_verdict == "supported"


def test_an_over_length_dedupe_key_is_refused_rather_than_stored(
    db: Session, project: Project, settings: Settings
) -> None:
    """``Finding.dedupe_key`` is ``String(120)``.

    A 400-character key was stored intact on SQLite; on PostgreSQL the same
    payload raises ``StringDataRightTruncation`` and aborts the transaction,
    failing the whole task rather than one action.
    """
    anchor = make_anchor(db, project)
    outcome = propose(
        db,
        project,
        settings,
        finding_action(anchor, dedupe_key="K" * 400),
        anchor_ids={anchor.id},
    )
    assert outcome.accepted == []
    assert "at most 120 characters" in reasons(outcome)


def test_the_generated_dedupe_key_fits_the_column() -> None:
    assert len(finding_dedupe_key("interlock_review", "", "T" * 400)) == 120


# ---------------------------------------------------------------------------
# Every action type is dispatchable
# ---------------------------------------------------------------------------
def minimal_payloads() -> dict[ActionType, BaseModel]:
    """A valid, minimal payload for every action type in the contract."""
    return {
        ActionType.CREATE_FINDING: CreateFindingPayload(
            title="Probe", fact="The rung exists.", dedupe_key="probe"
        ),
        ActionType.CREATE_RECOMMENDATION: CreateRecommendationPayload(
            title="Probe", body="Consider a permissive."
        ),
        ActionType.CREATE_TASK: CreateTaskPayload(kind="review_plc_logic", title="Probe"),
        ActionType.POST_MESSAGE: PostMessagePayload(body="Probe."),
        ActionType.REQUEST_APPROVAL: RequestApprovalPayload(
            kind=ApprovalKind.FINDING_RELEASE, title="Probe", question="May I?"
        ),
        ActionType.UPDATE_FINDING_REVIEW: UpdateFindingReviewPayload(
            finding_id=uuid.uuid4(), verdict="supported"
        ),
        ActionType.REPORT_BLOCKER: ReportBlockerPayload(reason="No interlock schedule."),
        ActionType.REPORT_NO_FINDINGS: ReportNoFindingsPayload(scope_examined="Every rung."),
        ActionType.CREATE_TASK_PLAN: CreateTaskPlanPayload(
            summary="Probe.",
            tasks=[PlannedTask(key="a", task_kind="review_plc_logic", title="Probe")],
        ),
        ActionType.REQUEST_CLARIFICATION: RequestClarificationPayload(
            category=QuestionCategory.TARGET_PLATFORM,
            question="Which Studio 5000 version?",
            why_it_matters="No L5X can be generated without it.",
        ),
        ActionType.PROPOSE_CHANGESET: ProposeChangesetPayload(
            title="Probe",
            summary="Probe.",
            base_revision_number=0,
            operations=[
                ChangeOperationProposal(
                    operation_type=ChangeOperationType.CREATE_OBJECT,
                    object_type=ObjectType.AREA,
                    temp_key="area-1",
                    values={"code": "PKG", "name": "Packaging"},
                )
            ],
        ),
        ActionType.REVIEW_CHANGESET: ReviewChangesetPayload(
            changeset_id=uuid.uuid4(), verdict="approve"
        ),
        ActionType.GENERATE_PREVIEW: GeneratePreviewPayload(
            preview_kind="summary", title="Probe", content="Probe."
        ),
        ActionType.COMPLETE_TASK: CompleteTaskPayload(outcome="Finished."),
    }


def test_no_action_type_can_crash_the_executor(
    db: Session, project: Project, settings: Settings
) -> None:
    """The audited bypass: six of fourteen action types raised ``KeyError``.

    A tag architect returning the one thing its prompt asks for — a
    ``propose_changeset`` — crashed out of the executor and into the task
    worker, after the provider call had been billed and the run row written.
    Every type must now either be carried out or be recorded, with a reason.
    """
    payloads = minimal_payloads()
    assert set(payloads) == set(ActionType) == set(ACTION_PAYLOAD_MODELS)

    outcome = empty_run(db, project, settings)
    executor = ActionExecutor(db, project, agent_kind=AGENT_KIND, run_id=outcome.run.id)

    for action_type, payload in payloads.items():
        outcome.accepted = [validated(action_type, payload, evidence_anchor_ids=[uuid.uuid4()])]
        result = executor.execute(outcome)
        for item in result.unexecuted:
            assert item.reason, f"{action_type.value} was refused with no reason"


def test_an_unhandled_action_is_recorded_with_a_reason(
    db: Session, project: Project, settings: Settings
) -> None:
    outcome = empty_run(db, project, settings)
    outcome.accepted = [
        validated(ActionType.COMPLETE_TASK, CompleteTaskPayload(outcome="Finished the tag model."))
    ]

    result = ActionExecutor(db, project, agent_kind=AGENT_KIND, run_id=outcome.run.id).execute(
        outcome
    )

    assert [item.action_type for item in result.unexecuted] == ["complete_task"]
    assert "no handler" in result.unexecuted[0].reason


def test_a_changeset_proposal_without_a_declared_consumer_fails_closed(
    db: Session, project: Project, settings: Settings
) -> None:
    """Being consumed by one workflow must not make the action a global no-op."""
    outcome = empty_run(db, project, settings)
    payload = minimal_payloads()[ActionType.PROPOSE_CHANGESET]
    outcome.accepted = [validated(ActionType.PROPOSE_CHANGESET, payload)]

    result = ActionExecutor(db, project, agent_kind=AGENT_KIND, run_id=outcome.run.id).execute(
        outcome
    )

    assert result.handoffs == []
    assert [item.action_type for item in result.unexecuted] == ["propose_changeset"]
    rejection = db.execute(
        select(AuditEntry).where(
            AuditEntry.project_id == project.id,
            AuditEntry.target_id == outcome.run.id,
            AuditEntry.action == AuditAction.AGENT_ACTION_REJECTED,
        )
    ).scalar_one()
    assert rejection.summary == "Did not execute a propose_changeset action"


def test_a_changeset_proposal_declared_for_the_task_handler_is_handed_off(
    db: Session, project: Project, settings: Settings
) -> None:
    """A named downstream consumer is a handoff, not an action rejection."""
    outcome = empty_run(db, project, settings)
    payload = minimal_payloads()[ActionType.PROPOSE_CHANGESET]
    outcome.accepted = [validated(ActionType.PROPOSE_CHANGESET, payload)]

    result = ActionExecutor(
        db,
        project,
        agent_kind=AGENT_KIND,
        run_id=outcome.run.id,
        handoff_handlers={
            ActionType.PROPOSE_CHANGESET: "production design task handler",
        },
    ).execute(outcome)

    assert result.unexecuted == []
    assert [item.as_dict() for item in result.handoffs] == [
        {
            "action_type": "propose_changeset",
            "summary": "probe propose_changeset",
            "handler": "production design task handler",
        }
    ]
    assert result.summary()["action_handoffs"] == [item.as_dict() for item in result.handoffs]
    assert (
        db.execute(
            select(AuditEntry).where(
                AuditEntry.project_id == project.id,
                AuditEntry.target_id == outcome.run.id,
                AuditEntry.action == AuditAction.AGENT_ACTION_REJECTED,
            )
        ).scalar_one_or_none()
        is None
    )


def test_a_handled_action_is_still_carried_out(
    db: Session, project: Project, settings: Settings
) -> None:
    """The neighbour: making dispatch total must not make it inert."""
    outcome = propose(
        db,
        project,
        settings,
        {
            "action_type": ActionType.POST_MESSAGE.value,
            "summary": "Reporting progress.",
            "payload": {"body": "Read the interlock schedule."},
        },
    )
    result = ActionExecutor(db, project, agent_kind=AGENT_KIND, run_id=outcome.run.id).execute(
        outcome
    )

    assert len(result.messages) == 1
    assert result.unexecuted == []


# ---------------------------------------------------------------------------
# A rename is a rename whatever it is called
# ---------------------------------------------------------------------------
def test_a_rename_disguised_as_a_field_update_is_refused() -> None:
    """The audited bypass: ``update_field`` on a tag's ``name``.

    It renames the tag as thoroughly as ``rename_tag`` and reaches none of the
    machinery a rename needs — the approval gate counts only ``rename_tag``
    operations, the merge's ``from_name`` consistency check runs only for them,
    and ``name`` is not a protected field.
    """
    with pytest.raises(ValidationError, match="is a rename"):
        ChangeOperationProposal(
            operation_type=ChangeOperationType.UPDATE_FIELD,
            object_type=ObjectType.TAG,
            object_id=uuid.uuid4(),
            based_on_object_revision=1,
            field_name="name",
            values={"name": "M101_Cmd_Run"},
        )


def test_a_changeset_carrying_the_disguised_rename_is_refused(
    db: Session, project: Project, settings: Settings
) -> None:
    """Through the real runner, in the shape the audit actually submitted."""
    outcome = propose(
        db,
        project,
        settings,
        changeset_action(
            [
                {
                    "operation_type": ChangeOperationType.UPDATE_FIELD.value,
                    "object_type": ObjectType.TAG.value,
                    "object_id": str(uuid.uuid4()),
                    "based_on_object_revision": 1,
                    "field_name": "name",
                    "values": {"name": "M101_Cmd_Run"},
                }
            ],
            title="Naming tidy-up",
            summary="Aligns a tag with house style.",
            requires_human_approval=False,
        ),
    )
    assert outcome.accepted == []
    assert "rename" in reasons(outcome)


def test_updating_a_tags_description_is_still_an_ordinary_field_update() -> None:
    """The neighbour: the rule targets ``name``, not every edit to a tag."""
    operation = ChangeOperationProposal(
        operation_type=ChangeOperationType.UPDATE_FIELD,
        object_type=ObjectType.TAG,
        object_id=uuid.uuid4(),
        based_on_object_revision=1,
        field_name="description",
        values={"description": "Conveyor run command."},
    )
    assert operation.field_name == "description"


def test_renaming_a_non_tag_object_is_not_caught_by_the_tag_rename_rule() -> None:
    """The neighbour: ``impact._renamed_to`` classifies tag renames, not area names."""
    operation = ChangeOperationProposal(
        operation_type=ChangeOperationType.UPDATE_FIELD,
        object_type=ObjectType.AREA,
        object_id=uuid.uuid4(),
        based_on_object_revision=1,
        field_name="name",
        values={"name": "Packaging"},
    )
    assert operation.object_type is ObjectType.AREA


def test_the_declared_rename_form_still_works_with_approval() -> None:
    payload = ProposeChangesetPayload(
        title="Align motor tag names with the naming standard",
        summary="Rename one tag.",
        base_revision_number=4,
        requires_human_approval=True,
        operations=[
            ChangeOperationProposal(
                operation_type=ChangeOperationType.RENAME_TAG,
                object_type=ObjectType.TAG,
                object_id=uuid.uuid4(),
                based_on_object_revision=2,
                values={"from_name": "MTR101_Run", "to_name": "MTR_101_Cmd_Run"},
            )
        ],
    )
    assert payload.requires_human_approval is True


# ---------------------------------------------------------------------------
# Evidence anchors belong to one project
# ---------------------------------------------------------------------------
def test_a_foreign_anchor_nested_in_a_finding_payload_is_refused(
    db: Session, project: Project, settings: Settings, other_project: Project
) -> None:
    """The audited bypass: ``payload.evidence[].anchor_id``.

    The runner checked only ``action.evidence_anchor_ids``, so one in-project
    citation at the top of an action bought passage for any number of foreign
    ones underneath it — and each renders another customer's file path,
    location and quoted text into this project's delivered report.
    """
    mine = make_anchor(db, project)
    theirs = make_anchor(db, other_project, key="rung:Other/R1/0")

    action = finding_action(mine)
    action["payload"]["evidence"] = [
        {"anchor_id": str(theirs.id), "role": "requirement", "note": "cross-project"}
    ]
    outcome = propose(db, project, settings, action, anchor_ids={mine.id})

    assert outcome.accepted == []
    assert str(theirs.id) in reasons(outcome)


def test_an_in_project_nested_anchor_is_accepted(
    db: Session, project: Project, settings: Settings
) -> None:
    """The neighbour: nested evidence with a stated role is the richer form."""
    first = make_anchor(db, project, key="rung:Main/R1/0")
    second = make_anchor(db, project, key="req:FDS/4.2")

    action = finding_action(first)
    action["payload"]["evidence"] = [
        {"anchor_id": str(second.id), "role": "requirement", "note": "the obligation"}
    ]
    outcome = propose(db, project, settings, action, anchor_ids={first.id, second.id})

    assert reasons(outcome) == ""
    assert len(outcome.accepted) == 1


def test_a_foreign_anchor_in_a_changeset_basis_is_refused(
    db: Session, project: Project, settings: Settings, other_project: Project
) -> None:
    theirs = make_anchor(db, other_project, key="rung:Other/R1/0")
    outcome = propose(
        db,
        project,
        settings,
        changeset_action(
            [
                {
                    "operation_type": ChangeOperationType.CREATE_OBJECT.value,
                    "object_type": ObjectType.TAG.value,
                    "temp_key": "tag-1",
                    "values": {"name": "MTR_101_Cmd_Run"},
                    "basis": [{"kind": EvidenceKind.DIRECT.value, "anchor_id": str(theirs.id)}],
                }
            ]
        ),
    )
    assert outcome.accepted == []
    assert "do not belong to this project" in reasons(outcome)


def test_the_executor_will_not_link_a_foreign_anchor(
    db: Session, project: Project, settings: Settings, other_project: Project
) -> None:
    """Second gate: the anchor lookup is scoped to the project."""
    mine = make_anchor(db, project)
    theirs = make_anchor(db, other_project, key="rung:Other/R1/0")

    outcome = empty_run(db, project, settings)
    outcome.accepted = [
        validated(
            ActionType.CREATE_FINDING,
            CreateFindingPayload(
                **finding_payload(
                    evidence=[{"anchor_id": str(theirs.id), "role": "requirement"}],
                )
            ),
            confidence=0.9,
            evidence_anchor_ids=[mine.id],
        )
    ]

    result = ActionExecutor(db, project, agent_kind=AGENT_KIND, run_id=outcome.run.id).execute(
        outcome
    )

    assert len(result.findings) == 1
    assert [link.anchor_id for link in result.findings[0].evidence_links] == [mine.id]


# ---------------------------------------------------------------------------
# A template basis must name a template that exists
# ---------------------------------------------------------------------------
def test_a_basis_naming_a_template_that_does_not_exist_is_refused(
    db: Session, project: Project, settings: Settings
) -> None:
    """The audited bypass: ``template_id='esco.entirely.invented@99.0.0'``.

    ``kind=template_rule`` is what stamps a created object
    ``LogicOrigin.TEMPLATE_EXPANSION`` — the strongest provenance the system
    records. An invented id makes that a citation a reviewer cannot open.
    """
    outcome = propose(
        db,
        project,
        settings,
        changeset_action(
            [
                {
                    "operation_type": ChangeOperationType.CREATE_LOGIC.value,
                    "object_type": ObjectType.LOGIC_RUNG.value,
                    "temp_key": "rung-1",
                    "values": {"text": "XIC(Nonexistent)OTE(Nothing);"},
                    "basis": [
                        {
                            "kind": EvidenceKind.TEMPLATE_RULE.value,
                            "template_id": "esco.entirely.invented@99.0.0",
                        }
                    ],
                }
            ]
        ),
    )
    assert outcome.accepted == []
    assert "has not loaded" in reasons(outcome)


def test_a_basis_naming_a_version_that_was_never_shipped_is_refused(
    db: Session, project: Project, settings: Settings
) -> None:
    """A real pattern at an invented version is the harder half of the same lie."""
    outcome = propose(
        db,
        project,
        settings,
        changeset_action(
            [
                {
                    "operation_type": ChangeOperationType.CREATE_LOGIC.value,
                    "object_type": ObjectType.LOGIC_RUNG.value,
                    "temp_key": "rung-1",
                    "values": {"text": "XIC(Start)OTE(Run);"},
                    "basis": [
                        {
                            "kind": EvidenceKind.TEMPLATE_RULE.value,
                            "template_id": "esco.motor.standard@99.0.0",
                        }
                    ],
                }
            ]
        ),
    )
    assert outcome.accepted == []
    assert "has not loaded" in reasons(outcome)


def test_a_basis_naming_a_loaded_template_is_accepted(
    db: Session, project: Project, settings: Settings
) -> None:
    """The neighbour: expanding a shipped pattern is the intended path."""
    outcome = propose(
        db,
        project,
        settings,
        changeset_action(
            [
                {
                    "operation_type": ChangeOperationType.CREATE_LOGIC.value,
                    "object_type": ObjectType.LOGIC_RUNG.value,
                    "temp_key": "rung-1",
                    "values": {"text": "XIC(Start)OTE(Run);"},
                    "basis": [
                        {
                            "kind": EvidenceKind.TEMPLATE_RULE.value,
                            "template_id": MOTOR_TEMPLATE,
                        }
                    ],
                }
            ]
        ),
    )
    assert reasons(outcome) == ""
    assert len(outcome.accepted) == 1


def test_an_unversioned_template_basis_is_refused() -> None:
    """A pinned version is what makes the citation openable."""
    with pytest.raises(ValidationError, match="qualified template identifier"):
        ObjectBasis(kind=EvidenceKind.TEMPLATE_RULE, template_id="esco.motor.standard")


def test_a_versioned_template_basis_is_accepted() -> None:
    assert ObjectBasis(kind=EvidenceKind.TEMPLATE_RULE, template_id=MOTOR_TEMPLATE).template_id


def test_the_other_basis_forms_are_untouched_by_the_template_rule() -> None:
    """The neighbour: a requirement or a decision is still a whole basis."""
    for basis in (
        ObjectBasis(kind=EvidenceKind.DIRECT, anchor_id=uuid.uuid4()),
        ObjectBasis(kind=EvidenceKind.INFERENCE, requirement_reference="REQ-014"),
        ObjectBasis(kind=EvidenceKind.HUMAN_DECISION, decision_reference="DEC-004"),
    ):
        assert basis.template_id == ""


# ---------------------------------------------------------------------------
# The confidence rule compares the right two numbers
# ---------------------------------------------------------------------------
def test_omitting_the_action_confidence_no_longer_disables_the_review_gate(
    db: Session, project: Project, settings: Settings
) -> None:
    """The audited bypass: ``confidence`` omitted defaults to 0.0.

    The old guard read ``adjusted > action.confidence and action.confidence >
    0``, so an action that simply left ``confidence`` out evaluated ``False``
    and any raise was accepted.
    """
    finding = make_finding(db, project, confidence=0.2)

    outcome = propose(
        db,
        project,
        settings,
        {
            "action_type": ActionType.UPDATE_FINDING_REVIEW.value,
            "summary": "Reviewed and strengthened.",
            "payload": {
                "finding_id": str(finding.id),
                "verdict": "supported",
                "adjusted_confidence": 0.99,
            },
        },
        agent_kind="senior_review",
    )

    assert outcome.accepted == []
    assert "not raise it" in reasons(outcome)


def test_a_diffident_reviewer_may_still_lower_a_confident_finding(
    db: Session, project: Project, settings: Settings
) -> None:
    """The audited false refusal, which mattered as much as the bypass.

    A reviewer wanting to lower a 0.9 finding to 0.4 while honestly stating
    ``confidence=0.2`` in its own review was rejected with "may lower ... but
    not raise it", and had no way to comply except by inflating its own stated
    confidence — precisely backwards.
    """
    finding = make_finding(db, project, confidence=0.9)

    outcome = propose(
        db,
        project,
        settings,
        {
            "action_type": ActionType.UPDATE_FINDING_REVIEW.value,
            "summary": "I am not certain, but the rung does not show this.",
            "confidence": 0.2,
            "payload": {
                "finding_id": str(finding.id),
                "verdict": "needs_more_evidence",
                "adjusted_confidence": 0.4,
            },
        },
        agent_kind="senior_review",
    )

    assert reasons(outcome) == ""
    assert len(outcome.accepted) == 1


def test_a_changeset_review_cannot_raise_the_changesets_confidence(
    db: Session, project: Project, settings: Settings
) -> None:
    """``ReviewChangesetPayload`` said "enforced by the runner" and was not."""
    from app.core.enums import ChangeSetStatus
    from app.models.revision import ChangeSet

    changeset = ChangeSet(
        project_id=project.id,
        title="Line 4 motor control",
        summary="One motor block.",
        status=ChangeSetStatus.DRAFT,
        base_revision_number=0,
        confidence=0.3,
    )
    db.add(changeset)
    db.flush()

    raised = propose(
        db,
        project,
        settings,
        {
            "action_type": ActionType.REVIEW_CHANGESET.value,
            "summary": "Approved.",
            "payload": {
                "changeset_id": str(changeset.id),
                "verdict": "approve",
                "adjusted_confidence": 1.0,
            },
        },
        agent_kind="senior_review",
    )
    assert raised.accepted == []
    assert "not raise it" in reasons(raised)

    lowered = propose(
        db,
        project,
        settings,
        {
            "action_type": ActionType.REVIEW_CHANGESET.value,
            "summary": "Approved with reservations.",
            "payload": {
                "changeset_id": str(changeset.id),
                "verdict": "approve",
                "adjusted_confidence": 0.1,
            },
        },
        agent_kind="senior_review",
    )
    assert reasons(lowered) == ""


# ---------------------------------------------------------------------------
# Declaring safety relevance must not be the expensive option
# ---------------------------------------------------------------------------
def test_declaring_a_recommendation_safety_related_no_longer_loses_it(
    db: Session, project: Project, settings: Settings
) -> None:
    """The audited bypass, which pointed the wrong way.

    The honestly-labelled action was REFUSED and the recommendation lost, while
    the byte-identical action with ``safety_related: false`` was accepted — and
    ``_create_recommendation`` hardcodes ``requires_approval=True`` either way.
    The rule discarded information rather than gating anything, and it did so in
    the one direction that matters.
    """
    anchor = make_anchor(db, project)
    outcome = propose(
        db,
        project,
        settings,
        {
            "action_type": ActionType.CREATE_RECOMMENDATION.value,
            "summary": "Add a guard-door permissive.",
            "confidence": 0.7,
            "evidence_anchor_ids": [str(anchor.id)],
            "payload": {
                "title": "Add a guard-door permissive to the drive rung",
                "body": "Series the guard-door input into the DriveRun rung.",
                "safety_related": True,
            },
        },
        anchor_ids={anchor.id},
    )

    assert reasons(outcome) == ""
    assert len(outcome.accepted) == 1
    assert outcome.accepted[0].action.requires_approval is True
    assert any("requiring approval" in warning for warning in outcome.accepted[0].action.warnings)


# ---------------------------------------------------------------------------
# A preview must be checkable against what will be built
# ---------------------------------------------------------------------------
def test_an_impact_preview_must_name_the_changeset_it_previews() -> None:
    """The audited bypass: a model-written "0 rungs affected. Safe."

    Nothing required the numbers to come from ``engineering/impact.py``, and a
    reviewer approving a rename on that basis is approving the exact thing the
    impact report exists to prevent.
    """
    with pytest.raises(ValidationError, match="must name changeset_id"):
        GeneratePreviewPayload(
            preview_kind="impact",
            title="Rename impact",
            content="Renaming MTR_101_Run affects 0 rungs, 0 HMI bindings and 0 alarms. Safe.",
        )


def test_a_derived_preview_that_names_its_changeset_is_accepted() -> None:
    preview = GeneratePreviewPayload(
        preview_kind="ladder_text",
        title="Rungs this change set would produce",
        content="XIC(MTR_101_Cmd_Start)OTE(MTR_101_Cmd_Run);",
        changeset_id=uuid.uuid4(),
    )
    assert preview.changeset_id is not None


def test_a_narrative_preview_needs_no_changeset() -> None:
    """The neighbour: a summary is prose about the work, not a claim about output."""
    preview = GeneratePreviewPayload(
        preview_kind="summary",
        title="What this change set does",
        content="Adds the standard motor block for MTR_101.",
    )
    assert preview.changeset_id is None


# ---------------------------------------------------------------------------
# Scope, bounds and configuration
# ---------------------------------------------------------------------------
def test_an_operation_outside_the_agents_granted_scope_is_refused(
    db: Session, project: Project, settings: Settings
) -> None:
    """A requirements extractor must not be able to write ladder logic."""
    outcome = propose(
        db,
        project,
        settings,
        changeset_action(
            [
                {
                    "operation_type": ChangeOperationType.CREATE_LOGIC.value,
                    "object_type": ObjectType.LOGIC_RUNG.value,
                    "temp_key": "rung-1",
                    "values": {"text": "XIC(EStop)OTE(Run);"},
                    "basis": [
                        {"kind": EvidenceKind.TEMPLATE_RULE.value, "template_id": MOTOR_TEMPLATE}
                    ],
                }
            ]
        ),
        agent_kind="requirements_planning",
        allowed_object_types=(ObjectType.REQUIREMENT,),
    )
    assert outcome.accepted == []
    assert "logic_rung" in reasons(outcome)


def test_an_operation_inside_the_granted_scope_is_accepted(
    db: Session, project: Project, settings: Settings
) -> None:
    outcome = propose(
        db,
        project,
        settings,
        changeset_action(
            [
                {
                    "operation_type": ChangeOperationType.CREATE_OBJECT.value,
                    "object_type": ObjectType.REQUIREMENT.value,
                    "temp_key": "req-1",
                    "values": {"reference": "REQ-014", "statement": "The conveyor shall stop."},
                    "basis": [
                        {"kind": EvidenceKind.INFERENCE.value, "requirement_reference": "REQ-014"}
                    ],
                }
            ]
        ),
        agent_kind="requirements_planning",
        allowed_object_types=(ObjectType.REQUIREMENT,),
    )
    assert reasons(outcome) == ""
    assert len(outcome.accepted) == 1


def test_an_unbounded_warning_list_is_refused() -> None:
    """A fifty-thousand-element list is not a caveat a human can read."""
    with pytest.raises(ValidationError):
        AgentAction(
            action_type=ActionType.POST_MESSAGE,
            summary="hello",
            warnings=["x"] * 50_000,
        )


def test_a_five_megabyte_string_in_an_uncapped_field_is_refused() -> None:
    """``summary`` was capped at 4,000 while its sibling ``rationale`` was not.

    Both are read by a human, and the pair is what made the omission visible
    rather than deliberate.
    """
    with pytest.raises(ValidationError):
        ProposeChangesetPayload(
            title="Padded",
            summary="Padded.",
            rationale="X" * 5_000_000,
            base_revision_number=0,
            operations=[
                ChangeOperationProposal(
                    operation_type=ChangeOperationType.CREATE_OBJECT,
                    object_type=ObjectType.AREA,
                    temp_key="area-1",
                    values={"code": "PKG"},
                )
            ],
        )


def test_an_ordinary_warning_list_is_still_accepted() -> None:
    """The neighbour: warnings are never suppressed, so the cap must be generous."""
    action = AgentAction(
        action_type=ActionType.POST_MESSAGE,
        summary="hello",
        warnings=["The PDF had no text layer on page 4."] * 20,
    )
    assert len(action.warnings) == 20


def test_the_configured_output_token_limit_reaches_the_provider(
    db: Session, project: Project, settings: Settings
) -> None:
    """An administrator's ``max_output_tokens`` used to be read and discarded.

    A 400-tag change set does not fit in 4096 output tokens; the run arrived as
    truncated JSON and failed as ``invalid_response`` while the admin UI showed
    the raised limit as applied.
    """
    provider = ScriptedProvider([])
    runner = AgentRunner(
        db,
        settings,
        project=project,
        agent_kind=AGENT_KIND,
        provider=provider,
        temperature=0.3,
        max_output_tokens=16_000,
    )
    runner.run(system_prompt="test", context={}, allowed_actions=set(ActionType), anchor_ids=set())

    assert provider.max_output_tokens == 16_000
    assert provider.temperature == 0.3
