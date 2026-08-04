"""The PLC Logic Agent, tested at its boundaries rather than at its happy path.

An agent that generates control logic is judged by what it refuses. Every test
here is a refusal the product would be wrong without: an action outside its
permissions, a rung that cites nothing, an inferred requirement treated as
approved, a restricted domain nobody qualified has signed off, an instruction the
target cannot run, an operand reported unresolved with no reason.

Entirely offline and deterministic. The provider is scripted by the test rather
than derived from the context, because these tests are about what the *backend*
does with a proposal, and that has to be exercised with proposals no real
provider would produce.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import replace
from typing import Any

import pytest
from sqlalchemy.orm import Session

from app.agents.base.actions import ChangeOperationProposal
from app.agents.base.agent import AgentContext
from app.agents.base.provider import ChatMessage, ProviderResponse
from app.agents.base.runner import AgentRunner, RunOutcome
from app.agents.review.plc_logic import (
    EXPANSION_KEY,
    PROMPT,
    PLCLogicAgent,
    TemplateSelection,
    UnresolvedOperand,
    check_selection,
    generation_block,
    render_ladder_preview,
    selection_of,
    unresolved_operand_reports,
)
from app.core.config import Settings
from app.core.enums import (
    ActionType,
    ChangeOperationType,
    ControllerFamily,
    EvidenceKind,
    ObjectType,
    ProviderKind,
    QuestionCategory,
    ReleaseMode,
    RequirementStatus,
    RestrictedDomain,
    RestrictedDomainMode,
    SafetyClassification,
)
from app.core.services.evidence_graph import EvidenceGraph
from app.generators.plc.template_registry import get_registry
from app.models.profile import ProjectProfile, TargetPlatform
from app.models.project import Project
from app.models.requirements import Clarification, Requirement

MOTOR_TEMPLATE = "esco.motor.standard"
#: Read from the registry rather than written as a literal. A basis naming a
#: version this installation does not ship is now refused at the action
#: boundary, and the literal that used to be here went stale silently the first
#: time the pattern was revised.
MOTOR_VERSION = get_registry().versions(MOTOR_TEMPLATE)[-1]


# ---------------------------------------------------------------------------
# Scaffolding
# ---------------------------------------------------------------------------
class ScriptedProvider:
    """Returns exactly the actions a test hands it, and records what it was asked.

    Not the MockProvider: that one only restates the evidence graph, which is the
    right behaviour for a demonstration and useless for testing a refusal. Here
    the response is arbitrary on purpose.
    """

    kind = ProviderKind.MOCK
    model = "scripted-for-test"

    def __init__(self, actions: list[dict[str, Any]]) -> None:
        self._actions = actions
        self.system_prompt = ""
        self.context: dict[str, Any] = {}

    def complete_structured(
        self,
        *,
        system: str,
        messages: list[ChatMessage],
        schema: dict[str, Any],
        max_output_tokens: int = 4096,
        temperature: float = 0.0,
    ) -> ProviderResponse:
        self.system_prompt = system
        self.context = json.loads(messages[-1].content)
        return ProviderResponse(
            data={"rationale": "scripted", "actions": self._actions, "warnings": []},
            provider=self.kind.value,
            model=self.model,
        )


def propose(
    db: Session, project: Project, settings: Settings, action: dict[str, Any]
) -> RunOutcome:
    """Put one proposed action through the runner with this agent's permissions."""
    agent = PLCLogicAgent()
    runner = AgentRunner(
        db,
        settings,
        project=project,
        agent_kind=agent.kind.value,
        provider=ScriptedProvider([action]),
    )
    return runner.run(
        system_prompt="test",
        context={"agent_kind": agent.kind.value},
        allowed_actions=agent.allowed_actions,
        anchor_ids=set(),
    )


def changeset_action(operations: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "action_type": ActionType.PROPOSE_CHANGESET.value,
        "summary": "Create the conveyor motor control block.",
        "confidence": 0.5,
        "payload": {
            "title": "Line 4 motor control",
            "summary": "One motor block expanded from the approved standard pattern.",
            "base_revision_number": 0,
            "operations": operations,
        },
    }


def create_rung(*, basis: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "operation_type": ChangeOperationType.CREATE_LOGIC.value,
        "object_type": ObjectType.LOGIC_RUNG.value,
        "temp_key": "rung-1",
        "values": {"ordinal": 0, "comment": "Motor run request."},
        "basis": basis or [],
    }


TEMPLATE_BASIS: dict[str, Any] = {
    "kind": EvidenceKind.TEMPLATE_RULE.value,
    "template_id": f"{MOTOR_TEMPLATE}@{MOTOR_VERSION}",
    "note": "Standard across-the-line motor start/stop.",
}


def make_profile(db: Session, project: Project, **overrides: Any) -> ProjectProfile:
    """A profile that permits generation, unless a test says otherwise."""
    values: dict[str, Any] = {
        "project_id": project.id,
        "project_name": project.name,
        "naming_standard_version": "esco-naming-3.2",
        "release_mode": ReleaseMode.IMPORT_PACKAGE_ONLY,
        "restricted_domains": [],
        "restricted_domain_mode": RestrictedDomainMode.MANUAL_REVIEW_ONLY,
        "is_complete_for_generation": True,
    }
    values.update(overrides)
    profile = ProjectProfile(**values)
    db.add(profile)
    db.flush()
    return profile


def make_platform(db: Session, project: Project, **overrides: Any) -> TargetPlatform:
    values: dict[str, Any] = {
        "project_id": project.id,
        "controller_family": ControllerFamily.CONTROLLOGIX,
        "controller_catalog_number": "1756-L83E",
        "firmware_revision": "35.011",
        "safety_classification": SafetyClassification.STANDARD,
    }
    values.update(overrides)
    platform = TargetPlatform(**values)
    db.add(platform)
    db.flush()
    return platform


def make_requirement(
    db: Session, project: Project, reference: str, **overrides: Any
) -> Requirement:
    values: dict[str, Any] = {
        "project_id": project.id,
        "reference": reference,
        "title": f"{reference} conveyor motor",
        "statement": "The conveyor motor shall start from the HMI when its permissive is met.",
        "status": RequirementStatus.ACCEPTED,
    }
    values.update(overrides)
    requirement = Requirement(**values)
    db.add(requirement)
    db.flush()
    return requirement


@pytest.fixture
def logic_context(db: Session, project: Project, settings: Settings) -> AgentContext:
    """A context for the agent. The evidence graph is empty and unused.

    This agent builds from the canonical model — profile, platform, requirements,
    templates — not from parsed documents, so an empty graph keeps the tests
    independent of the parsers without weakening anything they assert.
    """
    return AgentContext(
        db=db,
        project=project,
        settings=settings,
        graph=EvidenceGraph(project_id=project.id),
    )


# ---------------------------------------------------------------------------
# Permissions
# ---------------------------------------------------------------------------
def test_the_allowed_actions_are_exactly_the_six_the_boundary_names() -> None:
    assert PLCLogicAgent().allowed_actions == {
        ActionType.PROPOSE_CHANGESET,
        ActionType.REQUEST_CLARIFICATION,
        ActionType.GENERATE_PREVIEW,
        ActionType.POST_MESSAGE,
        ActionType.COMPLETE_TASK,
        ActionType.REPORT_BLOCKER,
    }


def test_the_agent_cannot_approve_its_own_change_set(
    db: Session, project: Project, settings: Settings
) -> None:
    """``review_changeset`` belongs to Senior Review and to nobody else.

    An author that could sign off its own work is not reviewed work, and the
    refusal is a permission check in the runner rather than a line in a prompt.
    """
    outcome = propose(
        db,
        project,
        settings,
        {
            "action_type": ActionType.REVIEW_CHANGESET.value,
            "summary": "Approve the logic I just proposed.",
            "payload": {"changeset_id": str(uuid.uuid4()), "verdict": "approve"},
        },
    )

    assert not outcome.accepted
    assert len(outcome.rejected) == 1
    assert "not permitted" in outcome.rejected[0].reason
    assert outcome.rejected[0].action_type == ActionType.REVIEW_CHANGESET.value


def test_the_agent_cannot_create_a_finding(
    db: Session, project: Project, settings: Settings
) -> None:
    """Generating logic and judging logic are different jobs with different gates."""
    outcome = propose(
        db,
        project,
        settings,
        {
            "action_type": ActionType.CREATE_FINDING.value,
            "summary": "The existing interlock logic is wrong.",
            "evidence_anchor_ids": [],
            "payload": {"title": "Interlock missing", "fact": "No rung implements it."},
        },
    )

    assert not outcome.accepted
    assert outcome.rejected


# ---------------------------------------------------------------------------
# Traceability
# ---------------------------------------------------------------------------
def test_a_proposed_rung_with_no_basis_is_rejected(
    db: Session, project: Project, settings: Settings
) -> None:
    """The traceability promise, at the point it costs least to keep.

    A rung that exists for no stated reason cannot be reviewed: there is nothing
    for an engineer to check it against.
    """
    outcome = propose(db, project, settings, changeset_action([create_rung()]))

    assert not outcome.accepted
    assert "requires at least one basis" in outcome.rejected[0].reason


def test_the_same_rung_is_accepted_once_it_cites_the_template_it_came_from(
    db: Session, project: Project, settings: Settings
) -> None:
    """The rule is a requirement to cite, not a blanket refusal to generate."""
    outcome = propose(
        db, project, settings, changeset_action([create_rung(basis=[TEMPLATE_BASIS])])
    )

    assert not outcome.rejected, outcome.rejected[0].reason if outcome.rejected else ""
    assert len(outcome.accepted) == 1


def test_a_basis_that_names_nothing_is_rejected(
    db: Session, project: Project, settings: Settings
) -> None:
    """An empty basis asserts traceability without providing any."""
    outcome = propose(
        db,
        project,
        settings,
        changeset_action(
            [create_rung(basis=[{"kind": EvidenceKind.TEMPLATE_RULE.value, "note": "standard"}])]
        ),
    )

    assert not outcome.accepted
    assert outcome.rejected


def test_a_proposal_citing_an_anchor_from_outside_the_project_is_rejected(
    db: Session, project: Project, settings: Settings
) -> None:
    """A hallucinated anchor id and a cross-project leak look the same here.

    Neither may become evidence, so the runner refuses both.
    """
    action = changeset_action([create_rung(basis=[TEMPLATE_BASIS])])
    action["evidence_anchor_ids"] = [str(uuid.uuid4())]

    outcome = propose(db, project, settings, action)

    assert not outcome.accepted
    assert "do not belong to this project" in outcome.rejected[0].reason


# ---------------------------------------------------------------------------
# What the model is allowed to build on
# ---------------------------------------------------------------------------
def test_an_inferred_requirement_is_not_offered_as_something_to_build_from(
    db: Session, project: Project, logic_context: AgentContext
) -> None:
    """An inferred, unruled requirement is something the platform made up.

    Building logic on one promotes a guess into the delivered program, so it is
    handed over in a separate list that says why it is there — not mixed into the
    citable set with a prompt asking the model to be careful.
    """
    make_profile(db, project)
    make_platform(db, project)
    make_requirement(db, project, "REQ-001")
    make_requirement(
        db,
        project,
        "REQ-002",
        status=RequirementStatus.PROPOSED,
        is_inferred=True,
    )
    db.flush()

    payload = PLCLogicAgent().build_context(logic_context)

    assert [row["reference"] for row in payload["requirements"]] == ["REQ-001"]
    awaiting = {row["reference"]: row for row in payload["requirements_awaiting_decision"]}
    assert "REQ-002" in awaiting
    assert awaiting["REQ-002"]["is_inferred"] is True
    assert "inferred" in awaiting["REQ-002"]["why_not_citable"]


def test_a_requirement_with_an_open_blocking_clarification_is_not_citable(
    db: Session, project: Project, logic_context: AgentContext
) -> None:
    """Continuing past an unresolved ambiguity means picking a reading silently."""
    make_profile(db, project)
    make_platform(db, project)
    requirement = make_requirement(db, project, "REQ-003")
    db.add(
        Clarification(
            project_id=project.id,
            requirement_id=requirement.id,
            question="Does 'stop' mean a controlled ramp or an immediate drive disable?",
            ambiguity="Both readings are supported by the text.",
        )
    )
    db.flush()

    payload = PLCLogicAgent().build_context(logic_context)

    assert payload["requirements"] == []
    awaiting = payload["requirements_awaiting_decision"]
    assert len(awaiting) == 1
    assert "open clarification" in awaiting[0]["why_not_citable"]


# ---------------------------------------------------------------------------
# Restricted domains and the block
# ---------------------------------------------------------------------------
def test_a_restricted_domain_blocks_generation_and_names_the_question_to_ask(
    db: Session, project: Project, settings: Settings
) -> None:
    profile = make_profile(db, project, restricted_domains=[RestrictedDomain.MOTION.value])
    platform = make_platform(db, project)

    block = generation_block(profile, platform, settings)

    assert block is not None
    assert block.restricted_domains == ("motion",)
    assert block.category is QuestionCategory.RESTRICTED_DOMAIN
    assert "motion" in block.question


def test_a_project_cannot_grant_itself_a_restricted_domain_the_administrator_has_not(
    db: Session, project: Project, settings: Settings
) -> None:
    """The project flag is not the permission.

    ``restricted_domain_mode`` says what the project intends; the administrator's
    ``enabled_restricted_domains`` says what this installation permits. If a
    project flag alone could open the gate, the administrator control would be
    decorative.
    """
    profile = make_profile(
        db,
        project,
        restricted_domains=[RestrictedDomain.MOTION.value],
        restricted_domain_mode=RestrictedDomainMode.QUALIFIED_WORKFLOW_ENABLED,
    )
    platform = make_platform(db, project)

    assert generation_block(profile, platform, settings) is not None

    enabled = settings.model_copy(update={"enabled_restricted_domains": ["motion"]})
    assert generation_block(profile, platform, enabled) is None


def test_a_safety_controller_blocks_generation_whatever_the_profile_says(
    db: Session, project: Project, settings: Settings
) -> None:
    """A GuardLogix is a safety controller however the classification field reads."""
    profile = make_profile(db, project, restricted_domains=[])
    platform = make_platform(
        db,
        project,
        controller_family=ControllerFamily.GUARDLOGIX,
        safety_classification=SafetyClassification.UNKNOWN,
    )

    block = generation_block(profile, platform, settings)

    assert block is not None
    assert block.restricted_domains == ("safety_logic",)


def test_a_family_this_platform_does_not_describe_blocks_generation(
    db: Session, project: Project, settings: Settings
) -> None:
    """Micro800 is a different instruction set, not a subset of the Logix one."""
    profile = make_profile(db, project)
    platform = make_platform(db, project, controller_family=ControllerFamily.MICRO800)

    block = generation_block(profile, platform, settings)

    assert block is not None
    assert block.category is QuestionCategory.TARGET_PLATFORM
    assert "micro800" in block.reason


def test_a_project_with_no_target_platform_blocks_generation(
    db: Session, project: Project, settings: Settings
) -> None:
    assert generation_block(make_profile(db, project), None, settings) is not None


def test_a_project_with_no_profile_blocks_generation(settings: Settings) -> None:
    assert generation_block(None, None, settings) is not None


def test_an_incomplete_profile_blocks_generation_and_names_the_missing_fields(
    db: Session, project: Project, settings: Settings
) -> None:
    profile = make_profile(
        db,
        project,
        is_complete_for_generation=False,
        missing_required_fields=["naming_standard_version", "units"],
    )
    platform = make_platform(db, project)

    block = generation_block(profile, platform, settings)

    assert block is not None
    assert "naming_standard_version" in block.reason
    assert "units" in block.reason


def test_a_blocked_project_is_handed_no_template_and_no_instruction_set(
    db: Session, project: Project, logic_context: AgentContext
) -> None:
    """The refusal is structural rather than a request in the prompt.

    An agent with no template in front of it cannot select one, and a rung it
    invented anyway would have no approved template rule to cite as its basis.
    """
    make_profile(db, project, restricted_domains=[RestrictedDomain.MOTION.value])
    make_platform(db, project)
    db.flush()

    payload = PLCLogicAgent().build_context(logic_context)

    assert payload["generation_blocked"]["restricted_domains"] == ["motion"]
    assert payload["templates"] == []
    assert payload["instruction_set"] == {}


def test_the_blocked_prompt_tells_the_agent_to_ask_rather_than_to_generate(
    db: Session, project: Project, logic_context: AgentContext
) -> None:
    make_profile(db, project, restricted_domains=[RestrictedDomain.MOTION.value])
    make_platform(db, project)
    db.flush()

    prompt = PLCLogicAgent().system_prompt(logic_context)

    assert "GENERATION IS BLOCKED FOR THIS PROJECT" in prompt
    assert "Propose no change set" in prompt
    assert QuestionCategory.RESTRICTED_DOMAIN.value in prompt


# ---------------------------------------------------------------------------
# The prompt states what the evidence cannot support
# ---------------------------------------------------------------------------
def test_the_prompt_states_the_limits_its_evidence_cannot_exceed(
    db: Session, project: Project, logic_context: AgentContext
) -> None:
    """Static generation establishes nothing about a running controller.

    The claim an engineer is most likely to read into a generated L5X is the one
    it cannot support, so the prompt has to say so in as many words.
    """
    make_profile(db, project)
    make_platform(db, project)
    db.flush()

    prompt = PLCLogicAgent().system_prompt(logic_context)

    # It writes IR. XML is somebody else's output.
    assert "Emit XML" in prompt
    assert "intermediate representation" in prompt
    # It has not run anything.
    assert "never been compiled" in prompt
    assert "downloaded, scanned or tested" in prompt
    assert "firmware" in prompt
    assert "offline proposal" in prompt
    # It asks rather than assumes.
    assert "request_clarification" in prompt
    assert "threshold" in prompt and "fail state" in prompt and "reset behaviour" in prompt
    # It reports what it could not resolve.
    assert "unresolved_operands" in prompt
    # It cannot quietly change approved work.
    assert "Rename an approved tag" in prompt
    # And the limits of this particular target.
    assert "controllogix" in prompt


def test_the_static_prompt_carries_the_shared_rules() -> None:
    """Every agent inherits the same absolute rules; this one does not opt out."""
    assert "EVERY technical claim must cite evidence_anchor_ids" in PROMPT
    assert "Do not include reasoning traces" in PROMPT


# ---------------------------------------------------------------------------
# The instruction set offered to the model
# ---------------------------------------------------------------------------
def test_the_context_offers_only_templates_approved_for_the_target_family(
    db: Session, project: Project, logic_context: AgentContext
) -> None:
    make_profile(db, project)
    make_platform(db, project, controller_family=ControllerFamily.CONTROLLOGIX)
    db.flush()

    payload = PLCLogicAgent().build_context(logic_context)

    assert payload["templates"], "no approved pattern was offered for a supported family"
    registry = get_registry()
    for row in payload["templates"]:
        template = registry.get(row["template_id"], row["version"])
        assert template is not None
        assert template.supports(ControllerFamily.CONTROLLOGIX)
        # The template's own statement of what it does not do is the part a
        # chooser most needs and can least infer.
        assert row["restrictions"]


def test_no_restricted_instruction_is_offered_as_available(
    db: Session, project: Project, logic_context: AgentContext
) -> None:
    """Motion, messaging and the safety instruction set are read, never generated."""
    make_profile(db, project)
    make_platform(db, project)
    db.flush()

    instruction_set = PLCLogicAgent().build_context(logic_context)["instruction_set"]
    offered = {row["mnemonic"] for row in instruction_set["supported"]}

    assert "XIC" in offered and "OTE" in offered and "TON" in offered
    assert offered.isdisjoint(set(instruction_set["refused"]))
    for mnemonic in ("MAM", "MSG", "ESTOP"):
        assert mnemonic not in offered
        assert mnemonic in instruction_set["refused"]


# ---------------------------------------------------------------------------
# Checking a selection before anything is expanded
# ---------------------------------------------------------------------------
def test_an_approved_template_on_a_supported_family_has_nothing_wrong_with_it() -> None:
    problems = check_selection(
        TemplateSelection(MOTOR_TEMPLATE, MOTOR_VERSION, {"device_tag": "MTR_101"}),
        registry=get_registry(),
        family=ControllerFamily.CONTROLLOGIX,
    )
    assert problems == []


def test_a_template_id_that_is_not_in_the_registry_is_refused() -> None:
    """A template nobody loaded is a template nobody reviewed."""
    problems = check_selection(
        TemplateSelection("esco.motor.invented", "9.9.9", {}),
        registry=get_registry(),
        family=ControllerFamily.CONTROLLOGIX,
    )
    assert problems
    assert "not been reviewed" in problems[0]


def test_a_template_is_refused_for_a_family_it_is_not_approved_for() -> None:
    problems = check_selection(
        TemplateSelection(MOTOR_TEMPLATE, MOTOR_VERSION, {"device_tag": "MTR_101"}),
        registry=get_registry(),
        family=ControllerFamily.GUARDLOGIX,
    )
    assert any("guardlogix" in problem for problem in problems)


def test_a_missing_required_parameter_is_refused_rather_than_defaulted() -> None:
    """There is no sensible default for a device tag, and inventing one is the
    exact failure this product exists to prevent."""
    problems = check_selection(
        TemplateSelection(MOTOR_TEMPLATE, MOTOR_VERSION, {}),
        registry=get_registry(),
        family=ControllerFamily.CONTROLLOGIX,
    )
    assert any("device_tag" in problem for problem in problems)


def test_a_parameter_the_template_never_declared_is_refused() -> None:
    """Usually a renamed parameter. Ignoring it would expand on the old default
    while the caller believed otherwise."""
    problems = check_selection(
        TemplateSelection(
            MOTOR_TEMPLATE, MOTOR_VERSION, {"device_tag": "MTR_101", "motor_tag": "MTR_101"}
        ),
        registry=get_registry(),
        family=ControllerFamily.CONTROLLOGIX,
    )
    assert any("motor_tag" in problem for problem in problems)


def test_an_unresolved_operand_with_no_stated_reason_is_refused() -> None:
    """It tells an engineer something is wrong and gives them nothing to act on."""
    problems = check_selection(
        TemplateSelection(
            MOTOR_TEMPLATE,
            MOTOR_VERSION,
            {"device_tag": "MTR_101"},
            unresolved_operands=(UnresolvedOperand(operand="MTR_101_Permissive", reason=""),),
        ),
        registry=get_registry(),
        family=ControllerFamily.CONTROLLOGIX,
    )
    assert any("no stated" in problem for problem in problems)


def test_safety_related_logic_cannot_be_self_approved() -> None:
    """The rung flags exist so a human reads the logic; a selection that sets one
    and clears the other is asking to skip exactly that step."""
    problems = check_selection(
        TemplateSelection(
            MOTOR_TEMPLATE,
            MOTOR_VERSION,
            {"device_tag": "MTR_101"},
            is_safety_related=True,
            requires_human_approval=False,
        ),
        registry=get_registry(),
        family=ControllerFamily.CONTROLLOGIX,
    )
    assert any("cannot be self-approved" in problem for problem in problems)


def test_a_template_that_needs_senior_review_forces_human_approval() -> None:
    """The approval level is the template author's judgement about the pattern,
    and an expansion cannot quietly overrule it."""
    registry = get_registry()
    template = registry.get("esco.permissive.interlock")
    if template is None:  # pragma: no cover - the shipped pattern set moved
        pytest.skip("the permissive/interlock pattern is not in this template set")
    assert template.needs_senior_review

    problems = check_selection(
        TemplateSelection(template.id, template.version, {"device_tag": "MTR_101"}),
        registry=registry,
        family=ControllerFamily.CONTROLLOGIX,
    )
    assert any("must require human approval" in problem for problem in problems)

    approved = check_selection(
        TemplateSelection(
            template.id,
            template.version,
            {"device_tag": "MTR_101"},
            requires_human_approval=True,
        ),
        registry=registry,
        family=ControllerFamily.CONTROLLOGIX,
    )
    assert approved == []


def test_an_unresolved_operand_that_says_why_is_accepted() -> None:
    """Reporting what could not be bound is the correct outcome, not a failure."""
    problems = check_selection(
        TemplateSelection(
            MOTOR_TEMPLATE,
            MOTOR_VERSION,
            {"device_tag": "MTR_101"},
            unresolved_operands=(
                UnresolvedOperand(
                    operand="MTR_101_Permissive",
                    reason="No permissive schedule was supplied for this device.",
                ),
            ),
        ),
        registry=get_registry(),
        family=ControllerFamily.CONTROLLOGIX,
    )
    assert problems == []


# ---------------------------------------------------------------------------
# Reading a selection back out of a proposal
# ---------------------------------------------------------------------------
def network_operation(**attributes: Any) -> ChangeOperationProposal:
    return ChangeOperationProposal(
        operation_type=ChangeOperationType.CREATE_OBJECT,
        object_type=ObjectType.LOGIC_NETWORK,
        temp_key="net-1",
        values={
            "name": "MTR_101 motor control",
            "template_id": MOTOR_TEMPLATE,
            "template_version": MOTOR_VERSION,
            "attributes": {EXPANSION_KEY: attributes},
        },
    )


def test_a_selection_is_read_back_from_the_operation_that_carries_it() -> None:
    selection = selection_of(
        network_operation(
            parameters={"device_tag": "MTR_101"},
            unresolved_operands=[{"operand": "MTR_101_Permissive", "reason": "not scheduled"}],
        )
    )

    assert selection is not None
    assert selection.qualified_id == f"{MOTOR_TEMPLATE}@{MOTOR_VERSION}"
    assert selection.parameters == {"device_tag": "MTR_101"}
    assert selection.unresolved_operands[0].reason == "not scheduled"


def test_the_safety_flags_are_read_back_from_the_operation_that_carries_them() -> None:
    """They are declared on the network because the network is what a model
    chooses; the expansion stamps them onto every rung it creates."""
    selection = selection_of(
        network_operation(
            parameters={"device_tag": "MTR_101"},
            is_safety_related=True,
            requires_human_approval=True,
        )
    )

    assert selection is not None
    assert selection.is_safety_related is True
    assert selection.requires_human_approval is True


def test_the_safety_flags_default_to_off_when_the_operation_omits_them() -> None:
    """Off means "nobody said", which is why the flag alone is never the gate:
    the restricted-domain check and the template's approval level both stand
    behind it."""
    selection = selection_of(network_operation(parameters={"device_tag": "MTR_101"}))

    assert selection is not None
    assert selection.is_safety_related is False
    assert selection.requires_human_approval is False


def test_an_operation_that_names_no_template_carries_no_selection() -> None:
    """Creating a task or a program is not a pattern expansion, and reading one
    into it would make every organisational row look like generated logic."""
    operation = ChangeOperationProposal(
        operation_type=ChangeOperationType.CREATE_OBJECT,
        object_type=ObjectType.CONTROLLER_TASK,
        temp_key="task-1",
        values={"name": "MainTask"},
    )
    assert selection_of(operation) is None


def test_unresolved_operands_are_collected_across_the_whole_change_set() -> None:
    """An operand nobody could bind is worthless if it is only discoverable by
    opening each of forty operations in turn."""
    reports = unresolved_operand_reports(
        [
            network_operation(
                parameters={"device_tag": "MTR_101"},
                unresolved_operands=[{"operand": "MTR_101_Permissive", "reason": "not scheduled"}],
            ),
            network_operation(
                parameters={"device_tag": "MTR_102"},
                unresolved_operands=[{"operand": "MTR_102_Interlock_OK", "reason": "no schedule"}],
            ),
        ]
    )

    assert [item.operand for item in reports] == ["MTR_101_Permissive", "MTR_102_Interlock_OK"]


# ---------------------------------------------------------------------------
# The preview a reviewer reads
# ---------------------------------------------------------------------------
def test_the_preview_renders_the_expansion_as_ladder() -> None:
    """Rung number, comment, and the neutral rendering — in that order.

    A reviewer approving generated logic reads it as logic. The same information
    as a list of change operations is not reviewable by anybody.
    """
    template = get_registry().get(MOTOR_TEMPLATE, MOTOR_VERSION)
    assert template is not None

    preview = render_ladder_preview(
        template, {"device_tag": "MTR_101"}, network_name="MTR_101 motor control"
    )

    assert "Network: MTR_101 motor control" in preview
    assert f"{MOTOR_TEMPLATE}@{MOTOR_VERSION}" in preview
    assert "MTR_101 run request" in preview
    assert "; Three-wire start/stop." in preview
    # Series conditions, a parallel branch as [leg,leg], and a trailing semicolon —
    # the same form render_rung_text produces for the export.
    assert (
        "[XIC(MTR_101_Cmd_Start)ONS(MTR_101_Start_OneShot)"
        "XIC(MTR_101_Permissive),XIC(MTR_101_Cmd_Run)]"
        "XIO(MTR_101_Cmd_Stop)XIC(MTR_101_Interlock_OK)XIO(MTR_101_Sts_Faulted)"
        "OTE(MTR_101_Cmd_Run);"
    ) in preview
    assert "XIC(MTR_101_Cmd_Run)OTE(MTR_101_Out_Run);" in preview


def test_the_preview_shows_only_the_rungs_the_parameters_actually_produce() -> None:
    """A rung gated off by a parameter is not in the expansion, so it is not in
    the preview. A reviewer must not approve logic that will not be built."""
    template = get_registry().get(MOTOR_TEMPLATE, MOTOR_VERSION)
    assert template is not None

    without_feedback = render_ladder_preview(
        template, {"device_tag": "MTR_101", "has_run_feedback": False}
    )

    assert "running status from command" in without_feedback
    assert "In_Run_Fbk" not in without_feedback
    assert "Tmr_FailToStart" not in without_feedback


def test_a_parameter_nobody_supplied_renders_as_a_hole_and_not_as_a_value() -> None:
    """A visible ``{placeholder}`` is a question. A plausible substituted value
    is a fabrication that reads exactly like an engineered one."""
    template = get_registry().get("esco.analog.input.scaling")
    if template is None:  # pragma: no cover - the shipped pattern set moved
        pytest.skip("the analog scaling pattern is not in this template set")

    preview = render_ladder_preview(template, {})

    assert "{signal_tag}" in preview, "an unsupplied parameter was silently filled in"
    # Parameters the template *does* declare a default for are filled, because
    # the default is the reviewed value rather than a guess.
    assert "32767" in preview


def test_the_preview_says_so_when_an_expansion_produces_no_rungs() -> None:
    """Silence and 'this template contributes nothing here' must not look alike."""
    template = get_registry().get(MOTOR_TEMPLATE, MOTOR_VERSION)
    assert template is not None
    gated = replace(
        template,
        generated_objects=replace(template.generated_objects, rungs=()),
    )

    preview = render_ladder_preview(gated, {"device_tag": "MTR_101"})

    assert "no rungs" in preview
