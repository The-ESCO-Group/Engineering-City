"""The Requirements and Planning Agent, tested at its boundaries.

The happy path of this agent is "it extracted some requirements", which a
deterministic test cannot judge. What a test *can* judge is everything the agent
must be unable to do, and that is the list the product's promises actually rest
on: it cannot draw an engineering conclusion, it cannot record an object that
cites nothing, it cannot accept its own inference, and it cannot design the
system it is describing.

Runs entirely offline against a fixed provider — no network, no model, no cost.
For the refusals the MockProvider is deliberately not used: it derives its answer
from the evidence graph, so it cannot be made to propose the thing that has to be
refused.

The last section is the exception, and it is about the provider rather than the
agent. Two of that provider's rules read the context and decide what to say about
it — which schedule rows become requirements, and which narrative passage becomes
a question — and both are judged on what they *do not* produce. A rule that asks
about a sentence nobody was unsure of wastes the attention this product exists to
protect, so the negative cases are the ones asserted.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.base.actions import (
    ChangeOperationProposal,
    ObjectBasis,
    ProposeChangesetPayload,
)
from app.agents.base.agent import AgentContext
from app.agents.base.provider import ChatMessage, ProviderResponse
from app.agents.base.providers.mock import MockProvider
from app.agents.base.runner import AgentRunner, RunOutcome
from app.agents.review.requirements_planning import (
    ALLOWED_OBJECT_TYPES,
    PROMPT,
    RequirementsPlanningAgent,
    refusal_reasons,
)
from app.core.config import Settings
from app.core.enums import (
    ActionType,
    ChangeOperationType,
    ClarificationStatus,
    EvidenceKind,
    ObjectType,
    ProviderKind,
    RequirementKind,
    RequirementStatus,
    TaskKind,
)
from app.core.services.evidence_graph import build_graph
from app.models.evidence import EvidenceAnchor
from app.models.profile import ProjectProfile, ProjectQuestion
from app.models.project import Project
from app.models.requirements import Requirement
from app.models.task import Task

#: Deliberately half narrative and half requirement, with one genuine ambiguity
#: ("stops") — the same shape as a real brief, so the context assertions are
#: about text an extractor would actually have to sort.
BRIEF = (
    "Line 3 was commissioned in 2009 and runs three shifts. "
    "The existing conveyor controls are to be replaced. "
    "The conveyor stops when the guard door is opened. "
    "The operator shall be able to jog the conveyor for cleaning."
)


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------
class _FixedProvider:
    """Returns exactly the actions a test wrote.

    Not a model and not the MockProvider: these tests are about what the backend
    refuses, and a provider whose output is derived from the evidence graph
    cannot be persuaded to propose something the evidence does not support.
    """

    kind = ProviderKind.MOCK
    model = "fixed-for-test"

    def __init__(self, actions: list[dict[str, Any]]) -> None:
        self._actions = actions

    def complete_structured(
        self,
        *,
        system: str,
        messages: list[ChatMessage],
        schema: dict[str, Any],
        max_output_tokens: int = 4096,
        temperature: float = 0.0,
    ) -> ProviderResponse:
        return ProviderResponse(
            data={"rationale": "fixed", "actions": self._actions, "warnings": []},
            provider=self.kind.value,
            model=self.model,
        )


@pytest.fixture
def described_project(db: Session, ingested_project: Project) -> Project:
    """The demo project plus the profile that carries the brief."""
    db.add(
        ProjectProfile(
            project_id=ingested_project.id,
            project_name="Line 3 conveyor controls replacement",
            customer_label="Synthetic Foods",
            project_description=BRIEF,
        )
    )
    db.commit()
    return ingested_project


def _context(db: Session, project: Project, settings: Settings) -> AgentContext:
    return AgentContext(
        db=db, project=project, settings=settings, graph=build_graph(db, project.id)
    )


def _run(
    db: Session,
    project: Project,
    settings: Settings,
    actions: list[dict[str, Any]],
) -> RunOutcome:
    """One validated turn: the agent's own prompt and context, fixed actions."""
    agent = RequirementsPlanningAgent()
    context = _context(db, project, settings)
    runner = AgentRunner(
        db,
        settings,
        project=project,
        agent_kind=agent.kind.value,
        provider=_FixedProvider(actions),
    )
    return runner.run(
        system_prompt=agent.system_prompt(context),
        context=agent.build_context(context),
        allowed_actions=agent.allowed_actions,
        anchor_ids=set(
            db.execute(
                select(EvidenceAnchor.id).where(EvidenceAnchor.project_id == project.id)
            ).scalars()
        ),
    )


def _basis() -> ObjectBasis:
    return ObjectBasis(
        kind=EvidenceKind.DIRECT,
        anchor_id=uuid.uuid4(),
        note="Functional_Description.docx / 3.2 / paragraph 4",
    )


def _requirement(**values: Any) -> ChangeOperationProposal:
    proposed: dict[str, Any] = {
        "reference": "REQ-001",
        "title": "Conveyor stops on guard door open",
        "statement": "The conveyor shall stop when the guard door is opened.",
        "source_text": "The conveyor stops when the guard door is opened.",
        "kind": RequirementKind.FUNCTIONAL.value,
        "confidence": 0.8,
    }
    proposed.update(values)
    return ChangeOperationProposal(
        operation_type=ChangeOperationType.CREATE_OBJECT,
        object_type=ObjectType.REQUIREMENT,
        temp_key="req-1",
        values=proposed,
        basis=[_basis()],
    )


def _payload(*operations: ChangeOperationProposal) -> ProposeChangesetPayload:
    return ProposeChangesetPayload(
        title="Requirements extracted from the project description",
        summary="One change set per extraction pass.",
        base_revision_number=0,
        operations=list(operations),
    )


# ---------------------------------------------------------------------------
# What it is permitted to do
# ---------------------------------------------------------------------------
def test_the_permitted_actions_are_exactly_the_five_it_needs() -> None:
    """The action set is the agent's whole remit, so it is asserted whole.

    Its absences are the interesting half: no create_finding, because it
    establishes what was asked for rather than whether anything is right; no
    create_task_plan, because scheduling belongs to the Project Manager Agent.
    """
    assert RequirementsPlanningAgent().allowed_actions == {
        ActionType.PROPOSE_CHANGESET,
        ActionType.REQUEST_CLARIFICATION,
        ActionType.POST_MESSAGE,
        ActionType.COMPLETE_TASK,
        ActionType.REPORT_BLOCKER,
    }


def test_it_cannot_draw_a_technical_conclusion(
    db: Session, described_project: Project, settings: Settings
) -> None:
    """A finding is an engineering judgement about correctness.

    This agent has no basis for one — it has read a brief, not a plant — so the
    backend refuses the action rather than relying on the prompt to discourage it.
    """
    anchor_id = (
        db.execute(
            select(EvidenceAnchor.id).where(EvidenceAnchor.project_id == described_project.id)
        )
        .scalars()
        .first()
    )
    assert anchor_id is not None

    outcome = _run(
        db,
        described_project,
        settings,
        [
            {
                "action_type": ActionType.CREATE_FINDING.value,
                "summary": "The guard door interlock is not implemented",
                "evidence_anchor_ids": [str(anchor_id)],
                "payload": {"title": "Missing interlock", "fact": "No rung references it."},
            }
        ],
    )

    assert outcome.accepted == []
    assert len(outcome.rejected) == 1
    assert "not permitted" in outcome.rejected[0].reason
    assert ActionType.CREATE_FINDING.value in outcome.rejected[0].reason


def test_an_ambiguity_with_only_one_reading_is_refused(
    db: Session, described_project: Project, settings: Settings
) -> None:
    """ "Stop is ambiguous" leaves a human with nothing to decide."""
    outcome = _run(
        db,
        described_project,
        settings,
        [
            {
                "action_type": ActionType.REQUEST_CLARIFICATION.value,
                "summary": "What does 'stops' mean for the conveyor?",
                "payload": {
                    "category": "requirement_ambiguity",
                    "question": "What does 'the conveyor stops' mean here?",
                    "why_it_matters": "It decides whether the output is ramped or dropped.",
                    "competing_readings": ["A controlled ramp-down"],
                },
            }
        ],
    )

    assert outcome.accepted == []
    assert "competing_readings" in outcome.rejected[0].reason


def test_an_ambiguity_naming_both_readings_is_accepted(
    db: Session, described_project: Project, settings: Settings
) -> None:
    outcome = _run(
        db,
        described_project,
        settings,
        [
            {
                "action_type": ActionType.REQUEST_CLARIFICATION.value,
                "summary": "What does 'stops' mean for the conveyor?",
                "payload": {
                    "category": "requirement_ambiguity",
                    "question": "What does 'the conveyor stops' mean here?",
                    "why_it_matters": "It decides whether the output is ramped or dropped.",
                    "competing_readings": [
                        "A controlled ramp-down over the configured decel time",
                        "An immediate drive disable",
                    ],
                },
            }
        ],
    )

    assert outcome.rejected == []
    assert len(outcome.accepted) == 1
    assert outcome.accepted[0].action_type is ActionType.REQUEST_CLARIFICATION


def test_an_accepted_clarification_is_persisted_once_for_a_human_to_answer(
    db: Session, described_project: Project, settings: Settings
) -> None:
    task = Task(
        project_id=described_project.id,
        kind=TaskKind.EXTRACT_REQUIREMENTS,
        title="Extract requirements",
        idempotency_key="requirements-question-persistence",
    )
    db.add(task)
    db.flush()
    context = AgentContext(
        db=db,
        project=described_project,
        settings=settings,
        graph=build_graph(db, described_project.id),
        task_id=task.id,
    )
    action: dict[str, Any] = {
        "action_type": ActionType.REQUEST_CLARIFICATION.value,
        "summary": "Clarify what stop means.",
        "payload": {
            "category": "requirement_ambiguity",
            "question": "Does stop mean a controlled ramp-down or immediate drive disable?",
            "why_it_matters": "The answer changes the commanded stop behaviour.",
            "competing_readings": [
                "Controlled ramp-down over the configured deceleration time",
                "Immediate drive disable",
            ],
            "blocks_task_kinds": [TaskKind.GENERATE_LOGIC.value],
        },
    }
    agent = RequirementsPlanningAgent()

    first = agent.run(context, provider=_FixedProvider([action]))
    second = agent.run(context, provider=_FixedProvider([action]))
    questions = list(
        db.execute(
            select(ProjectQuestion).where(ProjectQuestion.project_id == described_project.id)
        ).scalars()
    )

    assert len(questions) == 1
    question = questions[0]
    assert question.question == action["payload"]["question"]
    assert question.is_blocking is True
    assert question.asked_by_agent == "requirements_planning"
    assert question.raised_in_task_id == task.id
    assert question.blocks_task_kinds == [TaskKind.GENERATE_LOGIC.value]
    assert question.attributes["competing_readings"] == action["payload"]["competing_readings"]
    assert first.execution.questions[0].id == question.id
    assert second.execution.questions[0].id == question.id
    assert all(
        item.action_type != ActionType.REQUEST_CLARIFICATION.value
        for item in first.execution.unexecuted
    )


# ---------------------------------------------------------------------------
# Traceability: nothing exists here for no stated reason
# ---------------------------------------------------------------------------
def test_a_requirement_created_with_no_basis_is_refused() -> None:
    """A requirement with no source is an invention.

    Neither the action schema nor the merge catches this one: a requirement is
    not in TRACEABLE_OBJECT_TYPES, so this agent is the only thing standing
    between an unsourced requirement and every object that will later cite it.
    """
    unsourced = ChangeOperationProposal(
        operation_type=ChangeOperationType.CREATE_OBJECT,
        object_type=ObjectType.REQUIREMENT,
        temp_key="req-1",
        values={"reference": "REQ-001", "title": "Conveyor stops on guard door open"},
    )

    reasons = refusal_reasons(_payload(unsourced))

    assert len(reasons) == 1
    assert "REQ-001" in reasons[0]
    assert "no basis" in reasons[0]


def test_a_requirement_that_cites_the_passage_it_came_from_is_accepted() -> None:
    assert refusal_reasons(_payload(_requirement())) == []


def test_a_clarification_needs_no_basis() -> None:
    """A question about something nobody wrote down has nothing to cite.

    The exemption is narrow and deliberate: demanding a citation for an absence
    would train the agent to attach a meaningless anchor to every question, which
    devalues the citations that carry weight.
    """
    clarification = ChangeOperationProposal(
        operation_type=ChangeOperationType.CREATE_OBJECT,
        object_type=ObjectType.CLARIFICATION,
        temp_key="clr-1",
        values={
            "question": "What is the jog speed for cleaning?",
            "ambiguity": "The brief asks for jog but states no speed and no limit.",
        },
    )

    assert refusal_reasons(_payload(clarification)) == []


def test_an_assumption_must_say_what_breaks_if_it_is_wrong() -> None:
    """An assumption with no consequence is a guess with a reference number.

    It cannot be prioritised, cannot be tested, and will never be revisited — so
    it is worse than no record at all, because it looks like one.
    """
    assumption = ChangeOperationProposal(
        operation_type=ChangeOperationType.CREATE_OBJECT,
        object_type=ObjectType.ASSUMPTION,
        temp_key="asm-1",
        values={
            "reference": "ASM-001",
            "statement": "Jog is assumed to be momentary while the button is held.",
            "made_by_agent": "requirements_planning",
        },
        basis=[_basis()],
    )

    reasons = refusal_reasons(_payload(assumption))

    assert len(reasons) == 1
    assert "ASM-001" in reasons[0]
    assert "breaks if the assumption is wrong" in reasons[0]


# ---------------------------------------------------------------------------
# It never accepts its own work
# ---------------------------------------------------------------------------
def test_an_inferred_requirement_may_not_be_proposed_as_accepted() -> None:
    """The single rule the requirements model is built around.

    An inferred requirement may be perfectly sensible and still be something the
    customer never asked for. It stays PROPOSED until a named human rules on it.
    """
    reasons = refusal_reasons(
        _payload(_requirement(is_inferred=True, status=RequirementStatus.ACCEPTED.value))
    )

    assert len(reasons) == 1
    assert "status" in reasons[0]
    assert "only a named human" in reasons[0]


def test_a_proposed_requirement_is_left_at_the_status_it_belongs_at() -> None:
    """Stating the permitted value explicitly is not self-approval."""
    assert refusal_reasons(_payload(_requirement(status=RequirementStatus.PROPOSED.value))) == []


def test_it_may_not_confirm_its_own_assumption() -> None:
    assumption = ChangeOperationProposal(
        operation_type=ChangeOperationType.CREATE_OBJECT,
        object_type=ObjectType.ASSUMPTION,
        temp_key="asm-1",
        values={
            "reference": "ASM-001",
            "statement": "Jog is assumed to be momentary.",
            "impact_if_wrong": "A latched jog would leave the conveyor running unattended.",
            "is_confirmed": True,
        },
        basis=[_basis()],
    )

    reasons = refusal_reasons(_payload(assumption))

    assert len(reasons) == 1
    assert "is_confirmed" in reasons[0]


def test_it_may_not_answer_its_own_clarification() -> None:
    """Answering its own question would close a gap nobody looked at."""
    clarification = ChangeOperationProposal(
        operation_type=ChangeOperationType.CREATE_OBJECT,
        object_type=ObjectType.CLARIFICATION,
        temp_key="clr-1",
        values={
            "question": "What does 'the conveyor stops' mean here?",
            "ambiguity": "Controlled ramp-down, or immediate drive disable.",
            "status": ClarificationStatus.ANSWERED.value,
            "resolution": "Take it as a controlled ramp-down.",
        },
    )

    reasons = refusal_reasons(_payload(clarification))

    assert len(reasons) == 2
    assert any("status" in reason for reason in reasons)
    assert any("resolution" in reason for reason in reasons)


def test_a_later_edit_cannot_promote_a_requirement_either() -> None:
    """The rule is about the value, not about the operation that carries it.

    Checking only creates would leave the obvious way round it open: create the
    requirement proposed, then update the field.
    """
    promotion = ChangeOperationProposal(
        operation_type=ChangeOperationType.UPDATE_FIELD,
        object_type=ObjectType.REQUIREMENT,
        object_id=uuid.uuid4(),
        based_on_object_revision=1,
        field_name="status",
        values={"status": RequirementStatus.ACCEPTED.value},
    )

    reasons = refusal_reasons(_payload(promotion))

    assert len(reasons) == 1
    assert "only a named human" in reasons[0]


# ---------------------------------------------------------------------------
# It describes the system; it does not design it
# ---------------------------------------------------------------------------
def test_the_declared_scope_is_the_requirements_model_only() -> None:
    assert set(ALLOWED_OBJECT_TYPES) == {
        ObjectType.REQUIREMENT,
        ObjectType.ACCEPTANCE_CRITERION,
        ObjectType.ASSUMPTION,
        ObjectType.CONSTRAINT,
        ObjectType.CLARIFICATION,
    }
    assert RequirementsPlanningAgent().allowed_object_types == ALLOWED_OBJECT_TYPES


@pytest.mark.parametrize(
    "object_type", [ObjectType.TAG, ObjectType.LOGIC_RUNG, ObjectType.DEVICE, ObjectType.IO_POINT]
)
def test_it_may_not_propose_the_thing_that_satisfies_its_own_requirement(
    object_type: ObjectType,
) -> None:
    """Deciding what the system must do and deciding how to build it are
    separate acts with separate reviewers. An extractor that could also write a
    rung could satisfy its own requirement before anyone agreed to it."""
    operation = ChangeOperationProposal(
        operation_type=ChangeOperationType.CREATE_OBJECT,
        object_type=object_type,
        temp_key="x-1",
        values={"name": "CV101_GUARD_OK"},
        basis=[_basis()],
    )

    reasons = refusal_reasons(_payload(operation))

    assert len(reasons) == 1
    assert "outside this agent's scope" in reasons[0]


# ---------------------------------------------------------------------------
# The prompt states the limits its evidence cannot exceed
# ---------------------------------------------------------------------------
def test_the_prompt_defines_confidence_as_requirement_or_narrative() -> None:
    """The one thing most likely to be misread by whoever tunes this later."""
    assert "HOW SURE YOU ARE THAT THIS IS A REQUIREMENT" in PROMPT
    assert "does not mean how sure you are that the content is true" in PROMPT


def test_the_prompt_forbids_filling_a_gap_with_a_plausible_default() -> None:
    assert "NEVER FILL A GAP" in PROMPT
    for alternative in ("request_clarification", "impact_if_wrong", "limitations"):
        assert alternative in PROMPT
    assert "indistinguishable from something the customer asked" in PROMPT


def test_the_prompt_states_what_a_brief_cannot_establish() -> None:
    assert "WHAT THIS EVIDENCE CANNOT ESTABLISH" in PROMPT
    assert "not required because nobody wrote it down" in PROMPT
    assert "reading a brief, not a site" in PROMPT


def test_the_prompt_forbids_self_approval_in_the_same_words_the_code_enforces() -> None:
    for forbidden in ("is_confirmed", "resolution", '"accepted"'):
        assert forbidden in PROMPT


def test_the_prompt_reconciles_the_shared_rule_it_cannot_obey() -> None:
    """Rule 6 of SHARED_RULES names report_no_findings, which this agent lacks.

    Leaving that contradiction in the prompt would invite the model to emit an
    action that is refused every time, so the prompt says what to do instead.
    """
    assert "report_no_findings is not one of your actions" in PROMPT
    assert "report_blocker" in PROMPT


def test_the_prompt_carries_the_shared_rules() -> None:
    assert "Never fabricate" in PROMPT
    assert "Do not include reasoning traces" in PROMPT


# ---------------------------------------------------------------------------
# The context it reasons over
# ---------------------------------------------------------------------------
def test_the_context_withholds_the_existing_logic(
    db: Session, described_project: Project, settings: Settings
) -> None:
    """Requirements come from what was asked for, not from what was built.

    Handed the existing rungs, an extractor writes requirements the
    implementation satisfies by construction, and the gap between brief and
    build — the thing this stage exists to find — closes silently.
    """
    agent = RequirementsPlanningAgent()
    context = agent.build_context(_context(db, described_project, settings))

    assert set(context["graph"]) == {
        "counts",
        "documented_interlocks",
        "documented_alarms",
        "missing_inputs",
        "unreadable_files",
        "limitations",
    }
    encoded = json.dumps(context, default=str)
    assert "XIC(" not in encoded, "rung text reached the requirements context"


def test_the_context_carries_the_brief_verbatim(
    db: Session, described_project: Project, settings: Settings
) -> None:
    """Requirements cite back into this text, so a summarised copy would break
    the citation it is the root of."""
    agent = RequirementsPlanningAgent()
    context = agent.build_context(_context(db, described_project, settings))

    assert context["profile"]["present"] is True
    assert context["profile"]["project_description"] == BRIEF


def test_a_project_with_no_profile_still_reads_as_an_empty_brief(
    db: Session, project: Project, settings: Settings
) -> None:
    """No profile row and an empty description must look the same to the agent:
    both mean there is nothing to extract from, and neither is a missing key it
    has to reason about."""
    agent = RequirementsPlanningAgent()
    context = agent.build_context(_context(db, project, settings))

    assert context["profile"]["present"] is False
    assert context["profile"]["project_description"] == ""


def test_the_context_offers_an_anchor_for_the_statements_it_asks_to_be_cited(
    db: Session, described_project: Project, settings: Settings
) -> None:
    """A statement with no resolvable anchor cannot become a cited requirement."""
    agent = RequirementsPlanningAgent()
    context = agent.build_context(_context(db, described_project, settings))

    statements = [
        statement for document in context["documents"] for statement in document["statements"]
    ]
    assert statements, "no narrative statement was offered to the extractor"
    assert any(statement["anchor_id"] for statement in statements)
    for statement in statements:
        assert statement["anchor_id"] is None or statement["anchor_id"] in set(
            context["anchor_index"].values()
        )


def test_the_context_reports_what_was_never_supplied(
    db: Session, project: Project, settings: Settings
) -> None:
    """A brief reviewed with four of six documents is a narrower answer than one
    reviewed with all six, and the agent has to be able to say which it gave."""
    agent = RequirementsPlanningAgent()
    context = agent.build_context(_context(db, project, settings))

    assert context["graph"]["missing_inputs"]
    assert "Functional description" in context["graph"]["missing_inputs"]


def test_the_context_names_the_references_already_in_use(
    db: Session, described_project: Project, settings: Settings
) -> None:
    """The queue is at-least-once, so a re-run must converge.

    A reference is unique per project: proposing one that is taken fails the
    whole change set at merge, and the agent has no other way to know.
    """
    db.add(
        Requirement(
            project_id=described_project.id,
            reference="REQ-001",
            title="Conveyor stops on guard door open",
            statement="The conveyor shall stop when the guard door is opened.",
            status=RequirementStatus.PROPOSED,
        )
    )
    db.commit()

    agent = RequirementsPlanningAgent()
    context = agent.build_context(_context(db, described_project, settings))

    assert context["existing"]["references_in_use"][ObjectType.REQUIREMENT.value] == ["REQ-001"]
    assert context["existing"]["requirements"][0]["title"] == "Conveyor stops on guard door open"


def test_the_context_states_the_revision_a_proposal_must_be_written_against(
    db: Session, described_project: Project, settings: Settings
) -> None:
    agent = RequirementsPlanningAgent()
    context = agent.build_context(_context(db, described_project, settings))

    assert context["base_revision_number"] == described_project.current_revision_number


# ---------------------------------------------------------------------------
# What the deterministic provider says about this agent's context
# ---------------------------------------------------------------------------
# One interlock schedule row as the evidence graph serialises it. Both `scope`
# and `is_current` are present because the point of these tests is that this
# consumer keys off the first and not the second.
def _row(**values: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "identifier": "ILK-901",
        "description": "Guard door open",
        "trip_tag": "GD901_CLOSED",
        "trip_condition": "GD901_CLOSED = 0",
        "action": "Drop the line permissive",
        "failsafe": "De-energise to trip",
        "scope_text": "Existing",
        "scope": "existing",
        "is_current": True,
        "from_hidden_sheet": False,
        "is_specified": True,
        "missing_fields": [],
        "sources": [{"anchor_key": "row:901"}],
    }
    row.update(values)
    return row


def _statement(index: int, section: str, text: str) -> dict[str, Any]:
    return {
        "anchor_key": f"paragraph:{index}",
        "anchor_id": f"anchor-{index}",
        "section": section,
        "text": text,
    }


def _provider_operations(
    *, rows: list[dict[str, Any]] | None = None, documents: list[dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    """The change-set operations the deterministic provider proposes for a context."""
    anchors = {"row:901": "anchor-row-901"}
    context = {
        "agent_kind": "requirements_planning",
        "base_revision_number": 0,
        "anchor_index": anchors,
        "documents": documents or [],
        "graph": {"documented_interlocks": rows or []},
        "existing": {},
    }
    response = MockProvider().complete_structured(
        system="",
        messages=[ChatMessage(role="user", content=json.dumps(context))],
        schema={},
    )
    return [
        operation
        for action in response.data["actions"]
        if action["action_type"] == ActionType.PROPOSE_CHANGESET.value
        for operation in action["payload"]["operations"]
    ]


def _clarifications(operations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        operation
        for operation in operations
        if operation["object_type"] == ObjectType.CLARIFICATION.value
    ]


# A functional description recording that a passage of the customer's brief is
# still open, and the brief it names. Modelled on the Line 4 fixture, which is the
# shape a real document set arrives in: the notice lives in the companion
# document, and it says which document and which section it is about.
def _deferring_pair(
    notice: str = (
        "The release behaviour requested in section 3 of the project description has "
        "not been settled. It is not specified here and no logic has been written for it."
    ),
) -> list[dict[str, Any]]:
    return [
        {
            "filename": "Project_Description.docx",
            "statements": [
                _statement(11, "Brief / 3. Scope of work", "3. Scope of work"),
                _statement(
                    12,
                    "Brief / 3. Scope of work",
                    "We want the accumulation zone to do the work the operators do by hand.",
                ),
                _statement(
                    13,
                    "Brief / 3. Scope of work",
                    "When the palletiser calls for product, CV402 shall release the "
                    "accumulated cases to the discharge.",
                ),
                _statement(
                    14,
                    "Brief / 4. Control system",
                    "Keep the existing controller and the existing I/O.",
                ),
            ],
        },
        {
            "filename": "Functional_Description.docx",
            "statements": [_statement(30, "FDS / 8. To be confirmed", notice)],
        },
    ]


def test_a_passage_a_companion_document_records_as_unsettled_becomes_a_question() -> None:
    """The signal is the document set's own words, not a reading of the sentence.

    One document says the passage is not settled and names where it was asked for.
    That is a fact about the documents, so restating it is the same act as
    restating a schedule row — and it is the only narrative rule this provider has.
    """
    raised = _clarifications(_provider_operations(documents=_deferring_pair()))

    assert len(raised) == 1
    question = raised[0]["values"]["question"]
    assert "release the accumulated cases" in question, question
    assert "8. To be confirmed" in question, question
    assert raised[0]["values"]["blocks_generation"] is True
    cited = {str(item["anchor_id"]) for item in raised[0]["basis"]}
    assert cited == {"anchor-13", "anchor-30"}, "the question cites the passage and the notice"


def test_the_question_says_the_problem_is_missing_information_not_two_readings() -> None:
    """Naming one reading is worse than naming none, and inventing two is worse still.

    ``RequestClarificationPayload`` refuses a single competing reading for that
    reason. Neither document here writes the alternatives down, so the honest
    answer is that the information is missing — a rule that composed the two
    readings would be inventing the requirement it is asking about.
    """
    raised = _clarifications(_provider_operations(documents=_deferring_pair()))

    ambiguity = raised[0]["values"]["ambiguity"]
    assert "missing information rather than a stated ambiguity" in ambiguity, ambiguity
    assert "has not been settled" in ambiguity, "the notice is quoted, not paraphrased"


@pytest.mark.parametrize(
    ("notice", "why"),
    [
        (
            "The release behaviour has not been settled.",
            "no cross-reference, so no passage can be quoted",
        ),
        (
            "The release behaviour requested in section 3 of the project description is "
            "implemented as described.",
            "a cross-reference with nothing deferred is an ordinary citation",
        ),
        (
            "The reset behaviour requested in section 3 of the project description has "
            "not been settled.",
            "the notice's subject names nothing in the cited section",
        ),
        (
            "The extension of CYL401 has to be confirmed within 1.5 s of the solenoid "
            "energising, as section 3 of the project description requires.",
            "an obligation on the plant is a requirement, not an open decision",
        ),
        (
            "The release behaviour requested in section 9 of the project description has "
            "not been settled.",
            "the cited section does not exist in the document named",
        ),
    ],
)
def test_a_passage_is_only_questioned_when_every_condition_holds(notice: str, why: str) -> None:
    """Each condition removed on its own, and the rule must fall silent.

    This is the list the rule's honesty rests on. A deferral phrase alone is prose
    about anything; a cross-reference alone is a citation; a shared word alone is a
    coincidence of vocabulary. Firing on any one of them would put a question
    against a sentence that was perfectly clear.
    """
    assert _clarifications(_provider_operations(documents=_deferring_pair(notice))) == [], why


def test_a_row_the_upgrade_adds_is_still_transcribed_as_a_requirement() -> None:
    """Scope decides what the row can be *compared* against, not whether it is asked for.

    ``is_current`` is false for a row this upgrade adds, because an export of the
    controller as it runs today is not supposed to implement it. Reusing that as
    the filter here dropped the interlocks the job was raised to build out of the
    requirements model entirely.
    """
    operations = _provider_operations(
        rows=[
            _row(
                identifier="ILK-905", scope_text="New (this upgrade)", scope="new", is_current=False
            )
        ]
    )

    assert [operation["values"]["reference"] for operation in operations] == ["REQ-ILK-905"]
    assert "New (this upgrade)" in operations[0]["values"]["source_text"], (
        "an engineer accepting the requirement cannot tell existing from new without it"
    )


def test_a_row_from_a_hidden_worksheet_is_not_transcribed() -> None:
    """Superseded or working content, and an engineer opening the workbook cannot see it.

    Recording it as a requirement would put something nobody asked for in front of
    a reviewer with a citation that looks as good as any other.
    """
    assert _provider_operations(rows=[_row(from_hidden_sheet=True, is_current=False)]) == []
