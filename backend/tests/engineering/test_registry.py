"""What an operation may name, what it may write, and how it is named back.

The registry is small, but two of its properties are load-bearing enough to be
asserted rather than assumed: that no operation can ever write a column the
service layer owns, and that every object type produces a label a human can act
on. A conflict report is only useful if the reader can find the object.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import Session

from app.core.enums import BaseDataType, ObjectType, ProgramType, TagScope
from app.engineering import registry
from app.models.plc import LogicRung, Program, Routine
from app.models.project import Project
from app.models.tags import Tag

WRITABLE = sorted(registry.OBJECT_TYPE_MODELS, key=lambda t: t.value)


# ---------------------------------------------------------------------------
# Coverage of the vocabulary
# ---------------------------------------------------------------------------
def test_every_object_type_is_either_writable_or_refused_with_a_reason() -> None:
    """No object type may fall through the registry unexplained.

    An unmapped type would reach the merge as an operation nobody could apply or
    refuse coherently. Every member is therefore either registered or listed as
    unwritable *with the reason a caller can show a human*.
    """
    for object_type in ObjectType:
        registered = object_type in registry.OBJECT_TYPE_MODELS
        refused = registry.UNWRITABLE_OBJECT_TYPES.get(object_type, "")
        assert registered != bool(refused), f"{object_type.value} is neither writable nor refused"


def test_findings_are_not_writable_through_a_change_set() -> None:
    """Findings predate the canonical model and carry no ``object_revision``.

    Mapping them here would admit an operation that conflict detection has no
    way to reason about, so the refusal is explicit and says where findings do
    come from.
    """
    assert not registry.is_writable(ObjectType.FINDING)
    with pytest.raises(registry.UnknownObjectTypeError, match="create_finding action"):
        registry.model_for(ObjectType.FINDING)


# ---------------------------------------------------------------------------
# The allowlist
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("object_type", WRITABLE, ids=lambda t: t.value)
def test_the_allowlist_is_the_mapper_minus_the_denylist(object_type: ObjectType) -> None:
    """The shape matters more than the contents.

    Derived rather than enumerated, so ordinary model additions remain writable
    while service-owned and explicitly held-closed fields stay out.
    """
    mapper = sa_inspect(registry.model_for(object_type))
    columns = {column.key for column in mapper.columns}
    assert registry.allowed_fields(object_type) == (
        columns - registry.PROTECTED_FIELDS - registry.restricted_fields(object_type)
    )


@pytest.mark.parametrize("object_type", WRITABLE, ids=lambda t: t.value)
def test_no_service_owned_column_is_ever_writable(object_type: ObjectType) -> None:
    """An agent that could set ``approval_state`` could approve its own work."""
    assert not registry.allowed_fields(object_type) & registry.PROTECTED_FIELDS


def test_the_protected_set_names_the_columns_the_brief_requires() -> None:
    assert {
        "approval_state",
        "object_revision",
        "created_in_revision",
        "deleted_at",
        "approved_by_id",
        "id",
        "project_id",
    } <= registry.PROTECTED_FIELDS


def test_ordinary_engineering_fields_stay_writable() -> None:
    """The denylist is targeted; it is not a general lock on the model."""
    allowed = registry.allowed_fields(ObjectType.TAG)
    assert {"name", "description", "role", "base_data_type", "device_id"} <= allowed


def test_instruction_branch_identity_is_held_closed_until_branch_validation_exists() -> None:
    """A new foreign key must not become a write surface before its invariants do.

    The generic reference checker cannot validate ``LogicBranch`` because branch
    rows are not registered change-set objects.  Until import and generation use
    branch identities, clients keep supplying the established coordinates.
    """
    assert "branch_id" not in registry.allowed_fields(ObjectType.LOGIC_INSTRUCTION)
    assert registry.restricted_fields(ObjectType.LOGIC_INSTRUCTION) == frozenset({"branch_id"})
    reason = registry.field_write_restriction(ObjectType.LOGIC_INSTRUCTION, "branch_id")
    assert "branch identity" in reason
    assert "branch_level and branch_index" in reason


def test_required_fields_are_the_columns_nothing_else_supplies() -> None:
    """Checked before the write, so a missing name is a message, not an error."""
    assert registry.required_fields(ObjectType.TAG) == frozenset({"name"})
    assert registry.required_fields(ObjectType.DEVICE) == frozenset({"name", "tag_prefix"})


# ---------------------------------------------------------------------------
# Natural keys
# ---------------------------------------------------------------------------
def test_natural_keys_are_read_from_the_unique_constraints() -> None:
    """Duplicate detection tracks the schema instead of a copy of it."""
    assert registry.natural_keys(ObjectType.TAG) == (
        ("project_id", "scope", "scope_program_id", "name"),
    )
    assert registry.natural_keys(ObjectType.DEVICE) == (("project_id", "tag_prefix"),)


def test_column_default_supplies_what_a_proposal_leaves_out() -> None:
    """A tag created without a scope is still controller-scoped.

    The duplicate check has to know that, or two proposals for the same
    controller tag name would look like different keys.
    """
    assert registry.column_default(ObjectType.TAG, "scope") is TagScope.CONTROLLER
    assert registry.column_default(ObjectType.TAG, "scope_program_id") is None


# ---------------------------------------------------------------------------
# Coercion
# ---------------------------------------------------------------------------
def test_json_is_coerced_into_what_the_column_actually_stores() -> None:
    identifier = uuid.uuid4()
    assert registry.coerce_value(ObjectType.TAG, "device_id", str(identifier)) == identifier
    assert registry.coerce_value(ObjectType.TAG, "scope", "program") is TagScope.PROGRAM
    assert registry.coerce_value(ObjectType.TAG, "base_data_type", "BOOL") is BaseDataType.BOOL
    assert registry.coerce_value(ObjectType.TAG, "hmi_exposed", True) is True


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("name", {"nested": "object"}),
        ("hmi_exposed", "yes"),
        ("usage_count", "seven"),
        ("scope", "somewhere_else"),
        ("device_id", "not-a-uuid"),
        ("attributes", ["not", "a", "mapping"]),
    ],
)
def test_a_value_the_column_cannot_hold_is_refused_by_name(field: str, value: object) -> None:
    """The field is named in the error, because that is what the author fixes."""
    with pytest.raises(registry.FieldValueError) as raised:
        registry.coerce_value(ObjectType.TAG, field, value)
    assert raised.value.field == field
    assert field in str(raised.value)


def test_a_string_longer_than_the_column_is_refused_rather_than_truncated() -> None:
    """Silently truncating a tag name would rename it."""
    with pytest.raises(registry.FieldValueError, match="characters"):
        registry.coerce_value(ObjectType.TAG, "name", "X" * 200)


# ---------------------------------------------------------------------------
# Labels
# ---------------------------------------------------------------------------
def test_a_tag_labels_itself_by_name(db: Session, project: Project) -> None:
    tag = Tag(project_id=project.id, name="MTR_101_Cmd_Run")
    db.add(tag)
    db.flush()
    assert registry.label_for(tag) == "Tag MTR_101_Cmd_Run"


def test_a_rung_labels_itself_by_the_path_an_engineer_would_follow(
    db: Session, project: Project
) -> None:
    """``MainProgram / Conveyor_Interlocks / Rung 12`` — where to look, in order.

    "Rung 12" on its own names nothing; there are as many rung 12s as there are
    routines.
    """
    program = Program(
        project_id=project.id, name="MainProgram", program_type=ProgramType.NORMAL, ordinal=0
    )
    db.add(program)
    db.flush()
    routine = Routine(
        project_id=project.id, program_id=program.id, name="Conveyor_Interlocks", ordinal=0
    )
    db.add(routine)
    db.flush()
    rung = LogicRung(project_id=project.id, routine_id=routine.id, ordinal=12)
    db.add(rung)
    db.flush()

    assert registry.label_for(rung) == "MainProgram / Conveyor_Interlocks / Rung 12"
    assert registry.label_for(routine) == "Routine MainProgram / Conveyor_Interlocks"


def test_an_object_that_does_not_exist_is_still_named(db: Session) -> None:
    """Conflict reports are written about objects that turned out to be missing."""
    identifier = uuid.uuid4()
    assert registry.describe(ObjectType.TAG, object_id=identifier) == f"Tag {identifier}"
    assert registry.describe(ObjectType.LOGIC_RUNG) == "a new rung"


def test_label_for_never_raises_and_never_returns_nothing() -> None:
    """Labels are produced on failure paths; failing there would be the worst time.

    An unsaved, unnamed object has nothing to be labelled by, and still gets a
    phrase that reads correctly in a sentence rather than an empty string.
    """
    assert registry.label_for(Tag(name="")) == "a new tag"
    assert registry.label_for(object()) == "object"
