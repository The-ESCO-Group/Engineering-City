"""Structural guarantees of the canonical engineering model.

These are not tests of behaviour — they are tests that the *shape* of the model
still holds. Nine model modules were written to one set of conventions, and a
convention that nothing checks is a convention that drifts. Each test here
fails for a whole class of future mistake rather than for one instance of it.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import inspect
from sqlalchemy.orm import Session

from app.core.enums import (
    VALIDATION_LEVEL_ORDER,
    EvidenceKind,
    ObjectApprovalState,
    ObjectType,
    ValidationLevel,
)
from app.database.types import StrEnumType
from app.models import Base, ObjectEvidenceLink, Project, Tag
from app.models.core import ACCEPTABLE_BASIS_KINDS, TRACEABLE_OBJECT_TYPES, EngineeringObjectMixin

#: Every mapped class that uses the engineering-object mixin.
ENGINEERING_CLASSES = sorted(
    (
        mapper.class_
        for mapper in Base.registry.mappers
        if issubclass(mapper.class_, EngineeringObjectMixin)
    ),
    key=lambda cls: cls.__name__,
)

#: The columns the mixin exists to supply. Listed rather than derived from the
#: mixin so that deleting one from the mixin fails here loudly.
MIXIN_COLUMNS = (
    "project_id",
    "object_revision",
    "created_in_revision",
    "updated_in_revision",
    "approval_state",
    "approved_at",
    "approved_by_id",
    "approved_object_revision",
    "origin",
    "source_changeset_id",
    "created_by_agent",
    "created_by_id",
    "deleted_at",
    "superseded_by_id",
    "attributes",
)


def test_the_engineering_model_is_actually_present() -> None:
    """A sanity floor: if this number collapses, something failed to import."""
    assert len(ENGINEERING_CLASSES) >= 60, [cls.__name__ for cls in ENGINEERING_CLASSES]


@pytest.mark.parametrize("model", ENGINEERING_CLASSES, ids=lambda cls: cls.__name__)
def test_every_engineering_object_carries_the_versioning_columns(model: type) -> None:
    """Without these an object cannot be versioned, attributed or protected.

    A model module that redeclares one of them — or forgets the mixin — breaks
    conflict detection for its whole table, silently, because a change set based
    on a stale revision would have nothing to compare against.
    """
    columns = set(inspect(model).columns.keys())
    missing = [name for name in MIXIN_COLUMNS if name not in columns]
    assert not missing, f"{model.__name__} is missing {missing}"


@pytest.mark.parametrize("model", ENGINEERING_CLASSES, ids=lambda cls: cls.__name__)
def test_every_engineering_table_is_project_scoped(model: type) -> None:
    """A row that belongs to no project could leak between engagements."""
    column = inspect(model).columns["project_id"]
    assert not column.nullable
    assert column.index, f"{model.__name__}.project_id must be indexed"
    assert [fk.target_fullname for fk in column.foreign_keys] == ["projects.id"]


@pytest.mark.parametrize("model", ENGINEERING_CLASSES, ids=lambda cls: cls.__name__)
def test_enum_columns_use_the_portable_enum_type(model: type) -> None:
    """A ``Mapped[SomeEnum]`` on a plain ``String`` stores fine and loads as ``str``.

    That is the defect this repository has already been bitten by once: identity
    comparison silently returns ``False`` for every row read back from the
    database, so a status check passes when it should fail. Registering the enum
    in ``Base.type_annotation_map`` is what prevents it, and this asserts it was
    not forgotten for any new column.
    """
    for name, annotation in getattr(model, "__annotations__", {}).items():
        text = str(annotation)
        if "Mapped[" not in text:
            continue
        # Crude but sufficient: the enums all live in app.core.enums, and any
        # annotation naming one must have produced a StrEnumType column.
        column = inspect(model).columns.get(name)
        if column is None:
            continue
        looks_like_enum = any(
            token in text
            for token in ("Kind", "Status", "State", "Type", "Level", "Role", "Scope", "Category")
        )
        if not looks_like_enum:
            continue
        if isinstance(column.type, StrEnumType):
            continue
        pytest.fail(
            f"{model.__name__}.{name} is annotated {text} but its column type is "
            f"{column.type!r}; register the enum in Base.type_annotation_map"
        )


def test_named_constraints_everywhere() -> None:
    """SQLite cannot drop an unnamed constraint, so one is un-migratable.

    Autogeneration produces a working migration only because every constraint
    has a name it can refer to.
    """
    unnamed: list[str] = []
    for table in Base.metadata.tables.values():
        for constraint in table.constraints:
            if constraint.name is None:
                unnamed.append(f"{table.name}.{type(constraint).__name__}")
        for index in table.indexes:
            if index.name is None:
                unnamed.append(f"{table.name}.Index")
    assert not unnamed, unnamed


def test_traceable_object_types_are_real_members() -> None:
    for object_type in TRACEABLE_OBJECT_TYPES:
        assert isinstance(object_type, ObjectType)


def test_a_parser_warning_is_not_a_reason_for_an_object_to_exist() -> None:
    """The traceability rule has to exclude evidence *about* a document.

    "The PDF had no text layer" is a fact worth recording. It is not a basis for
    generating a rung, and treating it as one would let a generated object claim
    a citation that supports nothing.
    """
    assert EvidenceKind.PARSER_WARNING not in ACCEPTABLE_BASIS_KINDS
    assert EvidenceKind.UNSUPPORTED_CONTENT not in ACCEPTABLE_BASIS_KINDS
    assert EvidenceKind.MISSING_EXPECTED not in ACCEPTABLE_BASIS_KINDS
    assert EvidenceKind.TEMPLATE_RULE in ACCEPTABLE_BASIS_KINDS
    assert EvidenceKind.HUMAN_DECISION in ACCEPTABLE_BASIS_KINDS


def test_an_evidence_link_with_no_target_does_not_satisfy_traceability() -> None:
    empty = ObjectEvidenceLink(
        project_id=uuid.uuid4(),
        object_type=ObjectType.TAG,
        object_id=uuid.uuid4(),
        basis_kind=EvidenceKind.TEMPLATE_RULE,
    )
    assert not empty.satisfies_traceability

    cited = ObjectEvidenceLink(
        project_id=uuid.uuid4(),
        object_type=ObjectType.TAG,
        object_id=uuid.uuid4(),
        basis_kind=EvidenceKind.TEMPLATE_RULE,
        template_id="esco.motor.standard@1.0.0",
    )
    assert cited.satisfies_traceability


def test_validation_levels_are_totally_ordered() -> None:
    """ "At least X" has to be a comparison, not a hand-written table."""
    assert set(VALIDATION_LEVEL_ORDER) == set(ValidationLevel)
    ranks = list(VALIDATION_LEVEL_ORDER.values())
    assert ranks == sorted(ranks)
    assert len(set(ranks)) == len(ranks)
    assert (
        VALIDATION_LEVEL_ORDER[ValidationLevel.ROUND_TRIP_PARSED]
        < VALIDATION_LEVEL_ORDER[ValidationLevel.ROCKWELL_IMPORT_VERIFIED]
    ), "round-trip parsing must never rank as high as a real Studio 5000 import"


def test_an_approved_object_reports_itself_protected(db: Session, project: Project) -> None:
    """Protection is what stops an agent editing something an engineer signed off.

    Deliberately exercised through a real flush and expire rather than on a bare
    instance. The interesting failure is not the property — it is the enum
    coming back off the database as a plain ``str``, which would make
    ``is_protected`` return ``False`` for every approved row ever loaded.
    """
    tag = Tag(project_id=project.id, name="MTR_101_Cmd_Run")
    db.add(tag)
    db.flush()
    db.refresh(tag)

    assert tag.approval_state is ObjectApprovalState.DRAFT
    assert not tag.is_protected

    tag.approval_state = ObjectApprovalState.APPROVED
    db.flush()
    db.expire(tag)

    assert tag.approval_state is ObjectApprovalState.APPROVED
    assert tag.is_protected


def test_a_fresh_project_starts_at_revision_zero(project: Project) -> None:
    """Revision 0 is a real base, so no caller has to special-case a null."""
    assert project.current_revision_number == 0
    assert project.release_candidate_seq == 0
