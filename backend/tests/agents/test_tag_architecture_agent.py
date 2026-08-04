"""The Tag Architecture Agent's boundaries.

Not a happy-path test. Every case here is something the agent must be unable to
do — propose an action outside its remit, create a tag nobody can trace, rename
an approved tag without a human seeing the impact, treat something this platform
inferred as something a customer asked for.

Each one is checked through :class:`~app.agents.base.runner.AgentRunner` with a
scripted provider standing in for a model, because that is where the refusal
actually happens. Asserting on the prompt alone would prove only that the
wording is present, and a prompt is not a control.

Deterministic and offline: no network, no model, no cost.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.agents.base.actions import (
    ChangeOperationProposal,
    ObjectBasis,
    ProposeChangesetPayload,
)
from app.agents.base.agent import AgentContext
from app.agents.base.provider import ChatMessage, ProviderResponse
from app.agents.base.runner import AgentRunner, RunOutcome
from app.agents.review import tag_architecture
from app.agents.review.tag_architecture import PROMPT, TagArchitectureAgent
from app.core.config import Settings
from app.core.enums import (
    ActionType,
    AddressAssignmentStatus,
    AgentKind,
    AlarmSeverity,
    BaseDataType,
    ChangeOperationType,
    DeviceCategory,
    EvidenceKind,
    InterlockKind,
    ObjectApprovalState,
    ObjectType,
    ProviderKind,
    QuestionCategory,
    RequirementStatus,
    SignalDirection,
    SignalType,
    TagRole,
    TagScope,
    ValidationSeverity,
)
from app.core.services.evidence_graph import build_graph
from app.generators.plc.template_registry import get_registry
from app.models.delivery import AlarmDefinition
from app.models.interlocks import Interlock
from app.models.plant import AddressAssignment, Device, IOPoint, Signal
from app.models.project import Project
from app.models.requirements import Requirement
from app.models.tags import Tag, TagNamingRule
from app.validation.tags import MAX_TAG_NAME_CHARS, RESERVED_TAG_NAMES

#: Resolved against the registry rather than written as a literal: a basis
#: naming a version this installation does not ship is refused at the action
#: boundary, and a literal here went stale the first time the pattern was
#: revised.
TEMPLATE_ID = f"esco.motor.standard@{get_registry().versions('esco.motor.standard')[-1]}"


# ---------------------------------------------------------------------------
# A scripted stand-in for a model
# ---------------------------------------------------------------------------
class _ScriptedProvider:
    """Returns exactly what a test tells it to.

    Not the MockProvider: that one has rules of its own, and a test of what the
    backend *refuses* has to be able to say precisely what was proposed.
    """

    kind = ProviderKind.MOCK

    def __init__(self, *actions: dict[str, Any]) -> None:
        self.model = "scripted-for-tests"
        self._actions = list(actions)

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
            data={"rationale": "Scripted for a test.", "actions": self._actions, "warnings": []},
            provider=self.kind.value,
            model=self.model,
            prompt_tokens=1,
            completion_tokens=1,
        )


def _run(db: Session, project: Project, settings: Settings, *actions: dict[str, Any]) -> RunOutcome:
    """Put the scripted actions through the same gate a real run uses."""
    agent = TagArchitectureAgent()
    runner = AgentRunner(
        db,
        settings,
        project=project,
        agent_kind=agent.kind.value,
        provider=_ScriptedProvider(*actions),
    )
    return runner.run(
        system_prompt=agent.system_prompt(_context(db, project, settings)),
        context={"agent_kind": agent.kind.value},
        allowed_actions=agent.allowed_actions,
        anchor_ids=set(),
    )


def _context(db: Session, project: Project, settings: Settings) -> AgentContext:
    return AgentContext(
        db=db,
        project=project,
        settings=settings,
        graph=build_graph(db, project.id),
    )


def _built(db: Session, project: Project, settings: Settings) -> dict[str, Any]:
    return TagArchitectureAgent().build_context(_context(db, project, settings))


def _reasons(outcome: RunOutcome) -> str:
    return " ".join(item.reason for item in outcome.rejected)


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------
def _tag(db: Session, project: Project, name: str, **values: Any) -> Tag:
    tag = Tag(
        project_id=project.id,
        name=name,
        source_name=values.pop("source_name", name),
        role=values.pop("role", TagRole.COMMAND),
        base_data_type=values.pop("base_data_type", BaseDataType.BOOL),
        **values,
    )
    db.add(tag)
    db.flush()
    return tag


def _propose(*operations: ChangeOperationProposal, **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "title": "Line 4 tag model",
        "summary": "Create the motor 101 tags from the standard motor pattern.",
        "base_revision_number": 0,
        "operations": list(operations),
    }
    body.update(overrides)
    payload = ProposeChangesetPayload(**body)
    return {
        "action_type": ActionType.PROPOSE_CHANGESET.value,
        "summary": "Propose the tag model.",
        "payload": payload.model_dump(mode="json"),
    }


def _create_tag_operation(name: str = "MTR_101_Cmd_Run") -> ChangeOperationProposal:
    return ChangeOperationProposal(
        operation_type=ChangeOperationType.CREATE_OBJECT,
        object_type=ObjectType.TAG,
        temp_key=name.lower(),
        values={
            "name": name,
            # Populated on every tag, always: it is what makes a later rename an
            # explicable transformation rather than an unclosable discrepancy.
            "source_name": name,
            "role": TagRole.COMMAND.value,
            "base_data_type": BaseDataType.BOOL.value,
            "description": "Sealed run command for motor 101.",
        },
        basis=[
            ObjectBasis(
                kind=EvidenceKind.TEMPLATE_RULE,
                template_id=TEMPLATE_ID,
                note="Standard motor start/stop requires a sealed run command.",
            )
        ],
        description=f"Create {name}.",
    )


def _rename_operation(tag: Tag, to_name: str = "MTR_101_Cmd_Run") -> ChangeOperationProposal:
    return ChangeOperationProposal(
        operation_type=ChangeOperationType.RENAME_TAG,
        object_type=ObjectType.TAG,
        object_id=tag.id,
        based_on_object_revision=tag.object_revision,
        values={"from_name": tag.name, "to_name": to_name},
        description=(
            f"Rename {tag.name} to {to_name}. Impact: 1 interlock and 1 alarm "
            f"reference the current name; both move with it."
        ),
    )


# ---------------------------------------------------------------------------
# The remit
# ---------------------------------------------------------------------------
def test_the_agent_may_only_propose_the_actions_its_remit_covers() -> None:
    """Stated as an exact set, not a superset.

    A permission that grows by accident is the failure this assertion exists to
    catch: the agent's remit is the design of the tag model, and every action
    outside that list is a different agent's job.
    """
    assert TagArchitectureAgent().allowed_actions == {
        ActionType.PROPOSE_CHANGESET,
        ActionType.REQUEST_CLARIFICATION,
        ActionType.POST_MESSAGE,
        ActionType.COMPLETE_TASK,
        ActionType.REPORT_BLOCKER,
    }
    assert TagArchitectureAgent().kind is AgentKind.TAG_ARCHITECTURE


@pytest.mark.parametrize(
    ("action_type", "payload"),
    [
        (
            ActionType.CREATE_FINDING,
            {"title": "Tag naming is inconsistent", "fact": "Two names for one signal."},
        ),
        (
            ActionType.CREATE_RECOMMENDATION,
            {"title": "Adopt the standard", "body": "Rename the legacy tags."},
        ),
        (ActionType.REVIEW_CHANGESET, {"changeset_id": str(uuid.uuid4()), "verdict": "approve"}),
        (ActionType.CREATE_TASK, {"kind": "design_tags", "title": "Design the tags"}),
    ],
)
def test_an_action_outside_the_remit_is_refused(
    db: Session,
    project: Project,
    settings: Settings,
    action_type: ActionType,
    payload: dict[str, Any],
) -> None:
    """The agent cannot widen its own permissions by proposing well-formed work.

    Each of these is a valid action *somewhere* in the product. Being valid is
    not the question; being this agent's to make is.
    """
    outcome = _run(
        db,
        project,
        settings,
        {
            "action_type": action_type.value,
            "summary": "Out of remit.",
            # Findings and recommendations need an anchor to pass schema
            # validation at all; the point is that they are refused afterwards
            # for the permission, not for the citation.
            "evidence_anchor_ids": [str(uuid.uuid4())],
            "payload": payload,
        },
    )

    assert outcome.accepted == []
    assert f"not permitted to propose '{action_type.value}'" in _reasons(outcome)
    # Refusals are part of the record, not discarded: what a model tried to do
    # is sometimes the thing an engineer needs to see.
    assert outcome.run.rejected_actions


# ---------------------------------------------------------------------------
# Traceability
# ---------------------------------------------------------------------------
def test_a_proposed_tag_with_no_basis_is_refused(
    db: Session, project: Project, settings: Settings
) -> None:
    """A tag that exists for no stated reason cannot be reviewed.

    Written as a raw payload rather than through the model, because that is the
    shape a model actually emits and the schema is the thing under test.
    """
    outcome = _run(
        db,
        project,
        settings,
        {
            "action_type": ActionType.PROPOSE_CHANGESET.value,
            "summary": "Create a tag with no reason to exist.",
            "payload": {
                "title": "Line 4 tag model",
                "summary": "Create MTR_101_Cmd_Run.",
                "base_revision_number": 0,
                "operations": [
                    {
                        "operation_type": ChangeOperationType.CREATE_OBJECT.value,
                        "object_type": ObjectType.TAG.value,
                        "temp_key": "tag-1",
                        "values": {"name": "MTR_101_Cmd_Run", "source_name": "MTR_101_Cmd_Run"},
                    }
                ],
            },
        },
    )

    assert outcome.accepted == []
    assert "requires at least one basis" in _reasons(outcome)


def test_a_tag_created_from_an_approved_template_is_accepted(
    db: Session, project: Project, settings: Settings
) -> None:
    """The counterpart to the rule above: a cited basis is all it takes."""
    outcome = _run(db, project, settings, _propose(_create_tag_operation()))

    assert len(outcome.accepted) == 1
    payload = outcome.accepted[0].payload
    assert isinstance(payload, ProposeChangesetPayload)
    operation = payload.operations[0]
    assert operation.basis[0].template_id == TEMPLATE_ID
    assert operation.values["source_name"] == "MTR_101_Cmd_Run"


# ---------------------------------------------------------------------------
# Renaming
# ---------------------------------------------------------------------------
def test_a_rename_that_claims_it_needs_no_approval_is_refused(
    db: Session, project: Project, settings: Settings
) -> None:
    """A rename reaches into logic, HMI, alarms, tests and documentation.

    The impact report in front of a reviewer is the whole point of the gate, so
    a change set that renames while declaring it needs no approval is asking to
    skip exactly that step.

    Written as a raw payload because the model refuses to build one at all —
    which is itself the assertion: this shape cannot be constructed in-process,
    only proposed by something outside, and the runner is where it dies.
    """
    tag = _tag(db, project, "M101_RUN", approval_state=ObjectApprovalState.APPROVED)

    outcome = _run(
        db,
        project,
        settings,
        {
            "action_type": ActionType.PROPOSE_CHANGESET.value,
            "summary": "Quietly rename an approved tag.",
            "payload": {
                "title": "Line 4 tag model",
                "summary": "Bring M101_RUN onto the naming standard.",
                "base_revision_number": 0,
                "requires_human_approval": False,
                "operations": [_rename_operation(tag).model_dump(mode="json")],
            },
        },
    )

    assert outcome.accepted == []
    assert "requires_human_approval is false" in _reasons(outcome)


def test_a_rename_with_its_impact_and_the_human_gate_is_accepted(
    db: Session, project: Project, settings: Settings
) -> None:
    tag = _tag(db, project, "M101_RUN", approval_state=ObjectApprovalState.APPROVED)

    outcome = _run(
        db,
        project,
        settings,
        _propose(_rename_operation(tag), requires_human_approval=True),
    )

    assert len(outcome.accepted) == 1
    payload = outcome.accepted[0].payload
    assert isinstance(payload, ProposeChangesetPayload)
    operation = payload.operations[0]
    assert operation.operation_type is ChangeOperationType.RENAME_TAG
    assert operation.values == {"from_name": "M101_RUN", "to_name": "MTR_101_Cmd_Run"}
    # Without the revision the merge has nothing to detect a concurrent edit
    # against, and the rename would silently overwrite it.
    assert operation.based_on_object_revision == tag.object_revision
    assert "Impact:" in operation.description


def test_a_name_edit_is_now_refused_rather_than_left_to_the_prompt(
    db: Session, project: Project, settings: Settings
) -> None:
    """Revisited deliberately, as the previous version of this test asked.

    It used to assert the opposite: that ``requires_human_approval`` keyed on the
    presence of a ``rename_tag`` operation and nothing else, so an
    ``update_field`` writing ``name`` carried no gate and only the prompt told
    this agent not to write one. An adversarial audit submitted exactly that
    operation and it validated, renaming the tag at revision N with no approval
    and no impact report. The schema now refuses the shape, so the prompt is
    reinforcement rather than the only control.
    """
    tag = _tag(db, project, "M101_RUN")

    with pytest.raises(ValidationError, match="is a rename"):
        ChangeOperationProposal(
            operation_type=ChangeOperationType.UPDATE_FIELD,
            object_type=ObjectType.TAG,
            object_id=tag.id,
            based_on_object_revision=tag.object_revision,
            field_name="name",
            values={"name": "MTR_101_Cmd_Run"},
        )

    assert "never otherwise" in PROMPT


# ---------------------------------------------------------------------------
# Asking rather than guessing
# ---------------------------------------------------------------------------
def test_the_agent_may_ask_instead_of_choosing_a_plausible_value(
    db: Session, project: Project, settings: Settings
) -> None:
    outcome = _run(
        db,
        project,
        settings,
        {
            "action_type": ActionType.REQUEST_CLARIFICATION.value,
            "summary": "The engineering unit for the hopper level was never stated.",
            "payload": {
                "category": QuestionCategory.TAG_NAMING.value,
                "question": "What engineering unit does the hopper level transmitter report?",
                "why_it_matters": (
                    "The tag carries the unit onto the faceplate and into the alarm "
                    "message; a guessed unit looks deliberate and is wrong."
                ),
                "competing_readings": [],
                "blocks_task_kinds": ["design_tags"],
            },
        },
    )

    assert len(outcome.accepted) == 1
    assert outcome.accepted[0].action_type is ActionType.REQUEST_CLARIFICATION


def test_an_ambiguity_that_names_only_one_reading_is_refused(
    db: Session, project: Project, settings: Settings
) -> None:
    """ "This is ambiguous" without saying how leaves a human nothing to decide."""
    outcome = _run(
        db,
        project,
        settings,
        {
            "action_type": ActionType.REQUEST_CLARIFICATION.value,
            "summary": "Ambiguous stop.",
            "payload": {
                "category": QuestionCategory.TAG_NAMING.value,
                "question": "Does Stop mean a ramp or a drive disable?",
                "why_it_matters": "It decides the tag role and the fail state.",
                "competing_readings": ["a controlled ramp-down"],
            },
        },
    )

    assert outcome.accepted == []
    assert "competing_readings" in _reasons(outcome)


# ---------------------------------------------------------------------------
# What the context is allowed to say
# ---------------------------------------------------------------------------
def test_an_inferred_requirement_is_not_presented_as_accepted(
    db: Session, project: Project, settings: Settings
) -> None:
    """Something this platform derived is not something a customer asked for.

    The distinction is computed in the context rather than left to the model to
    read off a status string, because the rule that hangs off it — an inferred,
    unruled requirement is not a basis for creating a tag — is too important to
    depend on that.
    """
    db.add(
        Requirement(
            project_id=project.id,
            reference="REQ-014",
            title="Motor 101 shall seal in on start",
            status=RequirementStatus.ACCEPTED,
        )
    )
    db.add(
        Requirement(
            project_id=project.id,
            reference="REQ-900",
            title="A jam detection timer is presumably wanted",
            status=RequirementStatus.PROPOSED,
            is_inferred=True,
        )
    )
    db.flush()

    by_reference = {
        item["reference"]: item for item in _built(db, project, settings)["requirements"]
    }

    assert by_reference["REQ-014"]["is_accepted"] is True
    assert by_reference["REQ-014"]["is_inferred"] is False

    inferred = by_reference["REQ-900"]
    assert inferred["is_accepted"] is False
    assert inferred["is_inferred"] is True
    assert inferred["needs_clarification"] is True


def test_every_existing_tag_carries_what_an_edit_would_need(
    db: Session, project: Project, settings: Settings
) -> None:
    """A rename needs the revision it was written against and the original name.

    Without ``object_revision`` the merge cannot detect a concurrent edit;
    without ``source_name`` the rename is unexplainable six months later.
    """
    _tag(db, project, "M101_RUN", source_name="MTR-101 RUN")

    view = _built(db, project, settings)["existing_tags"][0]

    assert view["name"] == "M101_RUN"
    assert view["source_name"] == "MTR-101 RUN"
    assert view["object_revision"] >= 1
    assert view["approval_state"] == ObjectApprovalState.DRAFT.value
    assert view["is_protected"] is False


def test_an_approved_tag_is_marked_protected_and_the_limitation_is_stated(
    db: Session, project: Project, settings: Settings
) -> None:
    _tag(db, project, "MTR_101_Cmd_Run", approval_state=ObjectApprovalState.APPROVED)

    built = _built(db, project, settings)

    assert built["existing_tags"][0]["is_protected"] is True
    assert any("approved" in item and "protected" in item for item in built["limitations"])


def test_the_impact_of_a_rename_is_countable_from_the_context(
    db: Session, project: Project, settings: Settings
) -> None:
    """The impact report's raw material, counted from what the project holds.

    Counted rather than taken from ``Tag.usage_count``, which is a denormalised
    ranking aid and explicitly not proof of use — an impact report built on it
    would understate exactly the case it exists for.
    """
    _tag(db, project, "CV102_JAM", role=TagRole.INTERLOCK)
    db.add(
        Interlock(
            project_id=project.id,
            reference="ILK-003",
            name="Conveyor 102 jam stops the line",
            trip_tag_name="CV102_JAM",
            kind=InterlockKind.INTERLOCK,
        )
    )
    db.add(
        AlarmDefinition(
            project_id=project.id,
            reference="ALM-011",
            name="Conveyor 102 jammed",
            severity=AlarmSeverity.HIGH,
            trigger_tag_name="CV102_JAM",
            ordinal=1,
        )
    )
    db.flush()

    built = _built(db, project, settings)

    assert built["existing_tags"][0]["references"] == {"interlocks": 1, "alarms": 1}
    referenced = {item["name"]: item for item in built["referenced_tag_names"]}
    assert referenced["cv102_jam"]["declared"] is True


def test_a_name_the_model_references_but_never_declared_is_visible_as_such(
    db: Session, project: Project, settings: Settings
) -> None:
    """``declared`` is a statement about this project's tag table and nothing else.

    False here means the tag model does not hold the name yet. It is not
    evidence that the signal does not exist.
    """
    db.add(
        Interlock(
            project_id=project.id,
            reference="ILK-003",
            name="Conveyor 102 jam stops the line",
            trip_tag_name="CV102_JAM",
            kind=InterlockKind.INTERLOCK,
        )
    )
    db.flush()

    referenced = {
        item["name"]: item for item in _built(db, project, settings)["referenced_tag_names"]
    }
    assert referenced["cv102_jam"]["declared"] is False
    assert referenced["cv102_jam"]["referenced_by"] == {"interlocks": 1}


def test_an_io_alias_is_only_offered_where_an_assignment_resolves(
    db: Session, project: Project, settings: Settings
) -> None:
    """An alias to an address nobody chose is a fabricated I/O assignment."""
    device = Device(
        project_id=project.id,
        tag_prefix="MTR_101",
        name="Conveyor 102 drive motor",
        category=DeviceCategory.MOTOR,
    )
    db.add(device)
    db.flush()

    wired = Signal(
        project_id=project.id,
        device_id=device.id,
        name="Run_Out",
        signal_type=SignalType.DISCRETE_OUTPUT,
        direction=SignalDirection.OUTPUT,
        ordinal=1,
    )
    unwired = Signal(
        project_id=project.id,
        device_id=device.id,
        name="Run_Fbk",
        signal_type=SignalType.DISCRETE_INPUT,
        direction=SignalDirection.INPUT,
        ordinal=2,
    )
    point = IOPoint(
        project_id=project.id,
        address="Local:2:O.Data.3",
        signal_type=SignalType.DISCRETE_OUTPUT,
        direction=SignalDirection.OUTPUT,
    )
    db.add_all([wired, unwired, point])
    db.flush()
    db.add(
        AddressAssignment(
            project_id=project.id,
            signal_id=wired.id,
            io_point_id=point.id,
            status=AddressAssignmentStatus.ASSIGNED,
        )
    )
    # Recorded intent with no terminal chosen yet: not an assignment.
    db.add(
        AddressAssignment(
            project_id=project.id,
            signal_id=unwired.id,
            status=AddressAssignmentStatus.PROPOSED,
        )
    )
    db.flush()

    signals = {
        signal["name"]: signal for signal in _built(db, project, settings)["devices"][0]["signals"]
    }
    assert signals["Run_Out"]["io_assignment"]["address"] == "Local:2:O.Data.3"
    assert signals["Run_Fbk"]["io_assignment"] is None


def test_the_identifier_rules_come_from_the_validator_that_enforces_them(
    db: Session, project: Project, settings: Settings
) -> None:
    """One answer to "what is a legal tag name".

    A second copy in the agent would drift from the validator, and the drift
    would surface as a change set the agent accepted and the merge rejected.
    """
    rules = _built(db, project, settings)["identifier_rules"]

    assert rules["max_characters"] == MAX_TAG_NAME_CHARS
    assert rules["no_trailing_underscore"] is True
    assert rules["no_double_underscore"] is True
    assert set(rules["reserved_words"]) <= RESERVED_TAG_NAMES
    assert rules["reserved_words_total"] == len(RESERVED_TAG_NAMES)
    # The list is not claimed to be complete, and the context says so rather
    # than letting an agent treat it as authoritative.
    assert "not claimed to be exhaustive" in rules["note"]


def test_only_the_active_naming_rules_are_offered(
    db: Session, project: Project, settings: Settings
) -> None:
    """A withdrawn rule is kept so old results still resolve, not to design against."""
    db.add(
        TagNamingRule(
            project_id=project.id,
            standard_key="esco.tag_naming",
            standard_version="3.1",
            rule_key="command_suffix",
            pattern=r"^[A-Za-z0-9_]+_Cmd_[A-Za-z]+$",
            example="MTR_101_Cmd_Run",
            severity=ValidationSeverity.WARNING,
        )
    )
    db.add(
        TagNamingRule(
            project_id=project.id,
            standard_key="esco.tag_naming",
            standard_version="3.1",
            rule_key="legacy_prefix",
            pattern=r"^M\d+_",
            is_active=False,
        )
    )
    db.flush()

    standard = _built(db, project, settings)["naming_standard"]

    assert [rule["rule_key"] for rule in standard["rules"]] == ["command_suffix"]
    assert standard["standards"] == ["esco.tag_naming@3.1"]
    assert standard["note"] == ""


def test_a_project_with_no_pinned_standard_is_told_so_rather_than_given_a_default(
    db: Session, project: Project, settings: Settings
) -> None:
    standard = _built(db, project, settings)["naming_standard"]

    assert standard["rules"] == []
    assert "raise a clarification" in standard["note"]


def test_a_truncated_tag_list_says_that_it_is_truncated(
    db: Session,
    project: Project,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An analysis of a list the agent only partly read must not read as complete."""
    monkeypatch.setattr(tag_architecture, "MAX_EXISTING_TAGS", 1)
    _tag(db, project, "MTR_101_Cmd_Run")
    _tag(db, project, "MTR_102_Cmd_Run")

    built = _built(db, project, settings)

    assert len(built["existing_tags"]) == 1
    assert built["counts"]["existing_tags"] == 2
    assert any("Only 1 of 2 existing tags" in item for item in built["limitations"])


def test_a_program_scoped_name_is_not_presented_as_a_duplicate(
    db: Session, project: Project, settings: Settings
) -> None:
    """Logix identity is (scope, name), so one name can be two legitimate tags."""
    _tag(db, project, "Cmd_Run", scope=TagScope.PROGRAM)

    built = _built(db, project, settings)

    assert built["existing_tags"][0]["scope"] == TagScope.PROGRAM.value
    assert any("program-scoped" in item for item in built["limitations"])


def test_the_context_handed_to_the_model_is_plain_data(
    db: Session, project: Project, settings: Settings
) -> None:
    """No handles leave the agent layer.

    A model gets no database session, no filesystem and no shell; the context is
    encoded to JSON before it is sent, so anything that is not serialisable is a
    handle that should never have been there.
    """
    _tag(db, project, "MTR_101_Cmd_Run")

    encoded = json.dumps(_built(db, project, settings), sort_keys=True)

    assert '"base_revision_number"' in encoded
    assert "Session" not in encoded


# ---------------------------------------------------------------------------
# The prompt
# ---------------------------------------------------------------------------
def test_the_prompt_states_the_limits_its_evidence_cannot_exceed() -> None:
    """Each of these is a claim the input cannot support.

    They are asserted individually because a prompt that quietly loses one of
    them produces confident output about something nobody established.
    """
    for boundary in (
        "absent from what you were shown",
        "offline export at export time",
        "hard-wired circuitry",
        "have diverged",
    ):
        assert boundary in PROMPT, f"the prompt no longer states: {boundary}"


def test_the_prompt_refuses_to_fill_a_gap_with_a_plausible_value() -> None:
    assert "NEVER INVENT ENGINEERING INFORMATION" in PROMPT
    assert "request_clarification" in PROMPT
    assert "AN INFERRED REQUIREMENT IS NOT AN ACCEPTED ONE" in PROMPT


def test_the_prompt_states_the_rename_gate_and_the_basis_rule() -> None:
    assert "A RENAME IS NEVER SILENT" in PROMPT
    assert "requires_human_approval" in PROMPT
    assert "EVERY TAG CITES A BASIS" in PROMPT
    assert "SET source_name ON EVERY TAG" in PROMPT


def test_the_prompt_states_the_rockwell_identifier_constraints() -> None:
    assert str(MAX_TAG_NAME_CHARS) in PROMPT
    for rule in (
        "start with a letter or an underscore",
        "letters, digits and underscores",
        "two in a row",
        "reserved word",
    ):
        assert rule in PROMPT, f"the prompt no longer states: {rule}"


def test_the_prompt_keeps_restricted_domains_review_only() -> None:
    assert "qualified_workflow_enabled" in PROMPT
    assert "review-only" in PROMPT


def test_the_prompt_carries_the_shared_rules() -> None:
    """The boundaries the whole product is built on are inherited, not restated."""
    assert "Do not include reasoning traces" in PROMPT
    assert "Never fabricate" in PROMPT
