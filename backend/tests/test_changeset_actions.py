"""The action contract for proposing changes to the canonical model.

Every test here is a rule the product would be wrong without, enforced in the
schema rather than in prompt wording — because a prompt is not a control. The
schema is the last point at which a malformed or unaccountable proposal can be
refused for free, before it reaches conflict detection, a human reviewer, or a
generated L5X file.
"""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from app.agents.base.actions import (
    ChangeOperationProposal,
    CreateTaskPlanPayload,
    ObjectBasis,
    PlannedTask,
    ProposeChangesetPayload,
    RequestClarificationPayload,
    ReviewChangesetPayload,
)
from app.core.enums import (
    TRACEABLE_OBJECT_TYPES,
    ChangeOperationType,
    EvidenceKind,
    ObjectType,
    QuestionCategory,
)


def _template_basis() -> ObjectBasis:
    return ObjectBasis(
        kind=EvidenceKind.TEMPLATE_RULE,
        template_id="esco.motor.standard@1.0.0",
        note="Standard motor start/stop pattern.",
    )


def _create_tag(**overrides: object) -> ChangeOperationProposal:
    payload: dict[str, object] = {
        "operation_type": ChangeOperationType.CREATE_OBJECT,
        "object_type": ObjectType.TAG,
        "temp_key": "tag-1",
        "values": {"name": "MTR_101_Cmd_Run"},
        "basis": [_template_basis()],
    }
    payload.update(overrides)
    return ChangeOperationProposal(**payload)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Traceability
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("object_type", sorted(TRACEABLE_OBJECT_TYPES, key=lambda t: t.value))
def test_creating_a_traceable_object_without_a_basis_is_refused(object_type: ObjectType) -> None:
    """An object that exists for no stated reason cannot be reviewed.

    This is the whole traceability promise in one assertion, and it is checked
    for every object kind the rule covers rather than for one sample of it.
    """
    with pytest.raises(ValidationError, match="requires at least one basis"):
        ChangeOperationProposal(
            operation_type=ChangeOperationType.CREATE_OBJECT,
            object_type=object_type,
            temp_key="x",
            values={"name": "SOMETHING"},
        )


def test_creating_an_untraceable_object_needs_no_basis() -> None:
    """The rule is targeted, not blanket.

    An area or a network node is organisational scaffolding; demanding a
    requirement citation for one would train authors to attach a meaningless
    basis to everything, which devalues the citations that matter.
    """
    operation = ChangeOperationProposal(
        operation_type=ChangeOperationType.CREATE_OBJECT,
        object_type=ObjectType.AREA,
        temp_key="area-1",
        values={"code": "PKG", "name": "Packaging"},
    )
    assert operation.basis == []


def test_a_parser_warning_is_not_an_acceptable_basis() -> None:
    """ "The PDF had no text layer" is a fact. It is not a reason for a rung."""
    with pytest.raises(ValidationError, match="evidence \\*about\\* a document"):
        ObjectBasis(kind=EvidenceKind.PARSER_WARNING, anchor_id=uuid.uuid4())


def test_a_basis_must_point_at_something() -> None:
    """An empty basis asserts traceability without providing it."""
    with pytest.raises(ValidationError, match="must name an anchor"):
        ObjectBasis(kind=EvidenceKind.TEMPLATE_RULE)


def test_every_acceptable_basis_form_is_usable() -> None:
    for basis in (
        ObjectBasis(kind=EvidenceKind.DIRECT, anchor_id=uuid.uuid4()),
        ObjectBasis(kind=EvidenceKind.INFERENCE, requirement_reference="REQ-014"),
        ObjectBasis(kind=EvidenceKind.HUMAN_DECISION, decision_reference="DEC-004"),
        ObjectBasis(kind=EvidenceKind.TEMPLATE_RULE, template_id="esco.valve.standard@1.0.0"),
    ):
        assert basis.kind is not None


# ---------------------------------------------------------------------------
# Optimistic concurrency
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "operation_type",
    [
        ChangeOperationType.UPDATE_FIELD,
        ChangeOperationType.DELETE_OBJECT,
        ChangeOperationType.UPDATE_LOGIC,
    ],
)
def test_an_edit_must_state_the_revision_it_was_written_against(
    operation_type: ChangeOperationType,
) -> None:
    """Without it a concurrent edit cannot be detected, only overwritten."""
    with pytest.raises(ValidationError, match="based_on_object_revision"):
        ChangeOperationProposal(
            operation_type=operation_type,
            object_type=ObjectType.TAG,
            object_id=uuid.uuid4(),
            field_name="description",
            values={"description": "Run command"},
        )


def test_an_edit_must_name_the_object_it_edits() -> None:
    with pytest.raises(ValidationError, match="must name an object_id"):
        ChangeOperationProposal(
            operation_type=ChangeOperationType.UPDATE_FIELD,
            object_type=ObjectType.TAG,
            field_name="description",
            based_on_object_revision=3,
        )


def test_a_field_update_must_name_the_field() -> None:
    with pytest.raises(ValidationError, match="must name the field"):
        ChangeOperationProposal(
            operation_type=ChangeOperationType.UPDATE_FIELD,
            object_type=ObjectType.TAG,
            object_id=uuid.uuid4(),
            based_on_object_revision=3,
            values={"description": "Run command"},
        )


def test_a_create_needs_no_base_revision() -> None:
    """There is nothing to conflict with; requiring one would be noise."""
    assert _create_tag().based_on_object_revision is None


# ---------------------------------------------------------------------------
# Renames
# ---------------------------------------------------------------------------
def _rename(**overrides: object) -> ChangeOperationProposal:
    payload: dict[str, object] = {
        "operation_type": ChangeOperationType.RENAME_TAG,
        "object_type": ObjectType.TAG,
        "object_id": uuid.uuid4(),
        "based_on_object_revision": 2,
        "values": {"from_name": "MTR101_Run", "to_name": "MTR_101_Cmd_Run"},
    }
    payload.update(overrides)
    return ChangeOperationProposal(**payload)  # type: ignore[arg-type]


def test_a_rename_must_supply_both_names() -> None:
    """A rename that does not say what it is renaming from cannot be reviewed."""
    with pytest.raises(ValidationError, match="rename_tag requires values"):
        _rename(values={"to_name": "MTR_101_Cmd_Run"})


def test_a_changeset_containing_a_rename_cannot_self_approve() -> None:
    """A rename reaches into logic, HMI, alarms, tests and documentation.

    The impact report exists so an engineer sees that list before agreeing to
    it. A change set that renames while claiming to need no approval is asking
    to skip precisely that step, so the schema refuses it.
    """
    with pytest.raises(ValidationError, match="requires_human_approval is false"):
        ProposeChangesetPayload(
            title="Align motor tag names with the naming standard",
            summary="Rename one tag.",
            base_revision_number=4,
            operations=[_rename()],
            requires_human_approval=False,
        )


def test_a_rename_with_approval_requested_is_accepted() -> None:
    payload = ProposeChangesetPayload(
        title="Align motor tag names with the naming standard",
        summary="Rename one tag.",
        base_revision_number=4,
        operations=[_rename()],
        requires_human_approval=True,
    )
    assert len(payload.operations) == 1


# ---------------------------------------------------------------------------
# Change sets
# ---------------------------------------------------------------------------
def test_an_empty_changeset_is_refused() -> None:
    with pytest.raises(ValidationError):
        ProposeChangesetPayload(
            title="Nothing at all",
            summary="No operations.",
            base_revision_number=0,
            operations=[],
        )


def test_a_changeset_states_the_revision_it_was_built_against() -> None:
    payload = ProposeChangesetPayload(
        title="Create the motor command tags",
        summary="Three tags from the standard motor pattern.",
        base_revision_number=7,
        operations=[_create_tag()],
    )
    assert payload.base_revision_number == 7
    assert payload.assumptions == []


def test_a_changeset_rejects_an_unknown_field() -> None:
    """``extra='forbid'`` is what stops a model widening its own action surface."""
    with pytest.raises(ValidationError):
        ProposeChangesetPayload(
            title="Create the motor command tags",
            summary="Three tags.",
            base_revision_number=1,
            operations=[_create_tag()],
            apply_immediately=True,  # type: ignore[call-arg]
        )


# ---------------------------------------------------------------------------
# Review, plans and clarifications
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("verdict", ["request_changes", "reject"])
def test_a_refused_changeset_must_say_why(verdict: str) -> None:
    """A bare refusal leaves the author with nothing to act on."""
    with pytest.raises(ValidationError, match="requires at least one concern"):
        ReviewChangesetPayload(changeset_id=uuid.uuid4(), verdict=verdict)  # type: ignore[arg-type]


def test_an_approval_needs_no_concerns() -> None:
    review = ReviewChangesetPayload(changeset_id=uuid.uuid4(), verdict="approve")
    assert review.concerns == []


def test_a_plan_cannot_depend_on_a_task_it_does_not_define() -> None:
    """A dangling dependency leaves the task queued forever with no error."""
    with pytest.raises(ValidationError, match="which the plan does not"):
        CreateTaskPlanPayload(
            summary="Design the tag model.",
            tasks=[
                PlannedTask(
                    key="tags",
                    task_kind="design_tags",
                    title="Design the canonical tag model",
                    depends_on=["requirements"],
                )
            ],
        )


def test_a_plan_with_resolvable_dependencies_is_accepted() -> None:
    plan = CreateTaskPlanPayload(
        summary="Extract requirements, then design tags.",
        tasks=[
            PlannedTask(key="reqs", task_kind="extract_requirements", title="Extract requirements"),
            PlannedTask(
                key="tags",
                task_kind="design_tags",
                title="Design the canonical tag model",
                depends_on=["reqs"],
            ),
        ],
    )
    assert [task.key for task in plan.tasks] == ["reqs", "tags"]


def test_an_ambiguity_must_name_more_than_one_reading() -> None:
    """ "This is ambiguous" without saying how leaves nothing to decide."""
    with pytest.raises(ValidationError, match="name the alternative"):
        RequestClarificationPayload(
            category=QuestionCategory.REQUIREMENT_AMBIGUITY,
            question="What does 'the conveyor stops safely' mean here?",
            why_it_matters="It decides whether ILK-003 is a controlled stop or a trip.",
            competing_readings=["A controlled stop"],
        )


def test_a_missing_information_question_needs_no_readings() -> None:
    """Ambiguity and absence are different problems with different shapes."""
    clarification = RequestClarificationPayload(
        category=QuestionCategory.TARGET_PLATFORM,
        question="Which Studio 5000 version is the target?",
        why_it_matters="No L5X can be generated until the schema revision is known.",
    )
    assert clarification.competing_readings == []
    assert clarification.is_mandatory is True
