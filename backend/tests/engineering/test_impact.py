"""Rename and change impact analysis: what the reviewer is shown, and what they are not.

Two halves, and the second is the one that earns its keep.

The positive tests say that every place a tag can be written is found. The
negative tests say that nothing else is — ``MTR_1010`` is not ``MTR_101``,
``Conveyor.MTR_101`` is a member of ``Conveyor``, and a reference that resolved
to a different tag is not a reference to this one. A false positive in an impact
report is worse than a missing one: it teaches a reviewer to skim the report,
and the true positives go with it. So each reference kind is asserted from both
ends, and the near-miss has a test of its own.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.base.actions import ChangeOperationProposal, ProposeChangesetPayload
from app.core.enums import (
    AlarmSeverity,
    ChangeOperationType,
    FaultClass,
    InstructionCategory,
    InterlockKind,
    ObjectType,
    OperandKind,
    SignalDirection,
    SignalType,
    TagScope,
    ValidationSeverity,
)
from app.core.errors import ValidationError
from app.engineering import changesets, impact
from app.models.delivery import AlarmDefinition, HMIBinding, HMIScreen, TestCase, TestStep
from app.models.interlocks import (
    CauseEffectEntry,
    FaultDefinition,
    Interlock,
    ModeDefinition,
    Permissive,
    ResetBehavior,
    SequenceStep,
    StateDefinition,
    TransitionDefinition,
)
from app.models.plant import AddressAssignment, Device, IOPoint, Signal
from app.models.plc import (
    LogicComment,
    LogicInstruction,
    LogicOperand,
    LogicReference,
    LogicRung,
    Program,
    Routine,
)
from app.models.project import Project
from app.models.revision import ImpactReport
from app.models.tags import Tag, TagAlias

TAG_NAME = "MTR_101"
#: One character longer than the tag, and the whole point of boundary matching.
NEAR_MISS = "MTR_1010"


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------
def _add(db: Session, row: object) -> object:
    db.add(row)
    db.flush()
    return row


def _tag(db: Session, project: Project, name: str = TAG_NAME, **values: object) -> Tag:
    tag = Tag(project_id=project.id, name=name, **values)
    db.add(tag)
    db.flush()
    return tag


def _routine(db: Session, project: Project) -> Routine:
    """The one program and routine every logic fixture hangs off."""
    existing = db.execute(select(Routine).where(Routine.project_id == project.id)).scalars().first()
    if existing is not None:
        return existing
    program = Program(project_id=project.id, name="MainProgram", ordinal=0)
    db.add(program)
    db.flush()
    routine = Routine(
        project_id=project.id, program_id=program.id, name="Conveyor_Interlocks", ordinal=0
    )
    db.add(routine)
    db.flush()
    return routine


def _rung(db: Session, project: Project, ordinal: int = 12) -> LogicRung:
    routine = _routine(db, project)
    rung = LogicRung(project_id=project.id, routine_id=routine.id, ordinal=ordinal)
    db.add(rung)
    db.flush()
    return rung


def _operand(
    db: Session,
    project: Project,
    rung: LogicRung,
    *,
    text: str,
    resolved_name: str = "",
    tag: Tag | None = None,
    destination: bool = False,
) -> LogicOperand:
    instruction = LogicInstruction(
        project_id=project.id,
        rung_id=rung.id,
        ordinal=0,
        mnemonic="OTE" if destination else "XIC",
        category=InstructionCategory.BIT,
    )
    db.add(instruction)
    db.flush()
    operand = LogicOperand(
        project_id=project.id,
        instruction_id=instruction.id,
        ordinal=0,
        kind=OperandKind.TAG if tag is not None else OperandKind.UNRESOLVED,
        text=text,
        resolved_name=resolved_name,
        tag_id=tag.id if tag is not None else None,
        is_destination=destination,
        unresolved_reason="" if tag is not None else "no tag of that name in controller scope",
    )
    db.add(operand)
    db.flush()
    return operand


def _screen(db: Session, project: Project) -> HMIScreen:
    existing = (
        db.execute(select(HMIScreen).where(HMIScreen.project_id == project.id)).scalars().first()
    )
    if existing is not None:
        return existing
    screen = HMIScreen(
        project_id=project.id, key="CV_OVERVIEW", name="Conveyor overview", ordinal=0
    )
    db.add(screen)
    db.flush()
    return screen


def _binding(
    db: Session,
    project: Project,
    *,
    tag_name: str = "",
    expression: str = "",
    tag: Tag | None = None,
) -> HMIBinding:
    binding = HMIBinding(
        project_id=project.id,
        screen_id=_screen(db, project).id,
        property_name="Value",
        tag_name=tag_name,
        expression=expression,
        tag_id=tag.id if tag is not None else None,
        is_resolved=tag is not None,
    )
    db.add(binding)
    db.flush()
    return binding


def _alarm(db: Session, project: Project, *, name: str, tag: Tag | None = None) -> AlarmDefinition:
    alarm = AlarmDefinition(
        project_id=project.id,
        reference=f"ALM-{uuid.uuid4().hex[:6]}",
        name="Motor 101 tripped",
        severity=AlarmSeverity.HIGH,
        trigger_tag_name=name,
        trigger_tag_id=tag.id if tag is not None else None,
        ordinal=0,
    )
    db.add(alarm)
    db.flush()
    return alarm


def _test_step(db: Session, project: Project, *, name: str, tag: Tag | None = None) -> TestStep:
    case = TestCase(
        project_id=project.id,
        reference=f"TC-{uuid.uuid4().hex[:6]}",
        title="Verify the run command",
        ordinal=0,
    )
    db.add(case)
    db.flush()
    step = TestStep(
        project_id=project.id,
        test_case_id=case.id,
        step_number=3,
        ordinal=0,
        action="Force the run command.",
        tag_name=name,
        tag_id=tag.id if tag is not None else None,
    )
    db.add(step)
    db.flush()
    return step


def _states(db: Session, project: Project) -> tuple[StateDefinition, StateDefinition]:
    suffix = uuid.uuid4().hex[:6]
    first = StateDefinition(project_id=project.id, name=f"IDLE_{suffix}", ordinal=0)
    second = StateDefinition(project_id=project.id, name=f"RUNNING_{suffix}", ordinal=1)
    db.add_all([first, second])
    db.flush()
    return first, second


# ---------------------------------------------------------------------------
# One case per place a tag name can be written
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class _Case:
    """A reference kind, and how to write one naming a given tag name."""

    id: str
    kind: str
    discipline: str
    build: Callable[[Session, Project, str, Tag | None], None]


def _alias_case(db: Session, project: Project, name: str, tag: Tag | None) -> None:
    alias_tag = _tag(db, project, name=f"ALIAS_{uuid.uuid4().hex[:6]}", is_alias=True)
    _add(
        db,
        TagAlias(
            project_id=project.id,
            alias_tag_id=alias_tag.id,
            target_tag_id=tag.id if tag is not None else None,
            target_expression=name,
            is_resolved=tag is not None,
        ),
    )


def _aliasing_tag_case(db: Session, project: Project, name: str, tag: Tag | None) -> None:
    _tag(db, project, name=f"ALIAS_{uuid.uuid4().hex[:6]}", is_alias=True, alias_for=name)


def _comment_case(db: Session, project: Project, name: str, tag: Tag | None) -> None:
    _add(
        db,
        LogicComment(
            project_id=project.id,
            rung_id=_rung(db, project).id,
            operand_text=name,
            text="Run command from the HMI.",
        ),
    )


CASES: tuple[_Case, ...] = (
    _Case("tag_alias", "tag_alias", "logic", _alias_case),
    _Case("aliasing_tag", "aliasing_tag", "logic", _aliasing_tag_case),
    _Case(
        "hmi_binding_tag_name",
        "hmi_binding",
        "hmi",
        lambda db, p, name, tag: _binding(db, p, tag_name=name, tag=tag) and None,
    ),
    _Case(
        "hmi_binding_expression",
        "hmi_binding",
        "hmi",
        lambda db, p, name, tag: _binding(db, p, expression=f"{name} AND NOT Bypass") and None,
    ),
    _Case(
        "alarm_trigger",
        "alarm_trigger",
        "alarms",
        lambda db, p, name, tag: _alarm(db, p, name=name, tag=tag) and None,
    ),
    _Case(
        "test_step",
        "test_step",
        "tests",
        lambda db, p, name, tag: _test_step(db, p, name=name, tag=tag) and None,
    ),
    _Case(
        "interlock_trip",
        "interlock",
        "interlocks",
        lambda db, p, name, tag: (
            _add(
                db,
                Interlock(
                    project_id=p.id,
                    reference=f"ILK-{uuid.uuid4().hex[:6]}",
                    name="Guard door open stops the conveyor",
                    kind=InterlockKind.TRIP,
                    trip_tag_name=name,
                    trip_tag_id=tag.id if tag is not None else None,
                ),
            )
            and None
        ),
    ),
    _Case(
        "interlock_affected",
        "interlock",
        "interlocks",
        lambda db, p, name, tag: (
            _add(
                db,
                Interlock(
                    project_id=p.id,
                    reference=f"ILK-{uuid.uuid4().hex[:6]}",
                    name="Guard door open stops the conveyor",
                    kind=InterlockKind.TRIP,
                    affected_tag_name=name,
                ),
            )
            and None
        ),
    ),
    _Case(
        "permissive",
        "permissive",
        "interlocks",
        lambda db, p, name, tag: (
            _add(
                db,
                Permissive(
                    project_id=p.id,
                    name="Guard closed before start",
                    condition_tag_name=name,
                    condition_tag_id=tag.id if tag is not None else None,
                ),
            )
            and None
        ),
    ),
    _Case(
        "cause_effect_cause",
        "cause_effect_entry",
        "interlocks",
        lambda db, p, name, tag: (
            _add(
                db,
                CauseEffectEntry(
                    project_id=p.id,
                    ordinal=0,
                    cause_tag_name=name,
                    cause_tag_id=tag.id if tag is not None else None,
                ),
            )
            and None
        ),
    ),
    _Case(
        "cause_effect_effect",
        "cause_effect_entry",
        "interlocks",
        lambda db, p, name, tag: (
            _add(
                db,
                CauseEffectEntry(
                    project_id=p.id,
                    ordinal=0,
                    effect_tag_name=name,
                    effect_tag_id=tag.id if tag is not None else None,
                ),
            )
            and None
        ),
    ),
    _Case(
        "mode_definition",
        "mode_definition",
        "interlocks",
        lambda db, p, name, tag: (
            _add(
                db,
                ModeDefinition(
                    project_id=p.id,
                    name=f"AUTO_{uuid.uuid4().hex[:6]}",
                    ordinal=0,
                    selection_tag_name=name,
                    selection_tag_id=tag.id if tag is not None else None,
                ),
            )
            and None
        ),
    ),
    _Case(
        "state_definition",
        "state_definition",
        "interlocks",
        lambda db, p, name, tag: (
            _add(
                db,
                StateDefinition(
                    project_id=p.id,
                    name=f"RUNNING_{uuid.uuid4().hex[:6]}",
                    ordinal=0,
                    state_tag_name=name,
                ),
            )
            and None
        ),
    ),
    _Case(
        "transition_definition",
        "transition_definition",
        "interlocks",
        lambda db, p, name, tag: (
            _add(
                db,
                TransitionDefinition(
                    project_id=p.id,
                    from_state_id=_states(db, p)[0].id,
                    to_state_id=_states(db, p)[1].id,
                    ordinal=0,
                    condition_tag_name=name,
                    condition_tag_id=tag.id if tag is not None else None,
                ),
            )
            and None
        ),
    ),
    _Case(
        "sequence_step",
        "sequence_step",
        "interlocks",
        lambda db, p, name, tag: (
            _add(
                db,
                SequenceStep(
                    project_id=p.id,
                    reference=f"SEQ-{uuid.uuid4().hex[:6]}",
                    step_number=10,
                    ordinal=0,
                    completion_tag_name=name,
                ),
            )
            and None
        ),
    ),
    _Case(
        "fault_definition",
        "fault_definition",
        "interlocks",
        lambda db, p, name, tag: (
            _add(
                db,
                FaultDefinition(
                    project_id=p.id,
                    name="Drive fault",
                    fault_class=FaultClass.EQUIPMENT,
                    trigger_tag_name=name,
                    trigger_tag_id=tag.id if tag is not None else None,
                ),
            )
            and None
        ),
    ),
    _Case(
        "reset_behavior",
        "reset_behavior",
        "interlocks",
        lambda db, p, name, tag: (
            _add(
                db,
                ResetBehavior(
                    project_id=p.id,
                    name="Local reset at the panel",
                    reset_tag_name=name,
                    reset_tag_id=tag.id if tag is not None else None,
                ),
            )
            and None
        ),
    ),
    _Case("operand_comment", "operand_comment", "documentation", _comment_case),
    _Case(
        "logic_operand",
        "logic_rung",
        "logic",
        lambda db, p, name, tag: (
            _operand(db, p, _rung(db, p), text=name, resolved_name=name, tag=tag) and None
        ),
    ),
    _Case(
        "logic_reference",
        "logic_rung",
        "logic",
        lambda db, p, name, tag: (
            _add(
                db,
                LogicReference(
                    project_id=p.id,
                    rung_id=_rung(db, p).id,
                    tag_name=name,
                    tag_id=tag.id if tag is not None else None,
                    routine_name="Conveyor_Interlocks",
                    program_name="MainProgram",
                    reference_kind="read",
                ),
            )
            and None
        ),
    ),
)


# ---------------------------------------------------------------------------
# Every reference kind, from both ends
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("case", CASES, ids=[case.id for case in CASES])
def test_a_reference_to_the_renamed_tag_is_reported(
    db: Session, project: Project, case: _Case
) -> None:
    tag = _tag(db, project)
    case.build(db, project, TAG_NAME, tag)

    report = impact.analyse_rename(db, project=project, tag=tag, new_name="MTR_101_Cmd_Run")

    assert [entry["kind"] for entry in report.affected_objects] == [case.kind]
    assert report.affected_counts[case.discipline] == 1
    assert report.requires_human_approval is True
    assert report.affected_objects[0]["label"]


@pytest.mark.parametrize("case", CASES, ids=[case.id for case in CASES])
def test_a_reference_to_a_similarly_named_tag_is_not_reported(
    db: Session, project: Project, case: _Case
) -> None:
    """``MTR_101`` must not match ``MTR_1010`` anywhere it can be written."""
    tag = _tag(db, project)
    case.build(db, project, NEAR_MISS, None)

    report = impact.analyse_rename(db, project=project, tag=tag, new_name="MTR_101_Cmd_Run")

    assert report.affected_objects == []
    assert report.unresolved_references == []
    assert report.requires_human_approval is False


# ---------------------------------------------------------------------------
# Identifier boundaries
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "operand_text",
    [
        TAG_NAME,
        f"{TAG_NAME}.Running",
        f"{TAG_NAME}[3]",
        f"{TAG_NAME}.Data[2]",
        f"{TAG_NAME.lower()}.running",
    ],
    ids=["bare", "member", "array_element", "member_element", "different_case"],
)
def test_a_member_or_element_reference_reaches_the_tag(
    db: Session, project: Project, operand_text: str
) -> None:
    tag = _tag(db, project)
    _operand(db, project, _rung(db, project), text=operand_text)

    report = impact.analyse_rename(db, project=project, tag=tag, new_name="MTR_101_Cmd_Run")

    assert [entry["kind"] for entry in report.unresolved_references] == ["logic_rung"]
    assert report.unresolved_references[0]["references"] == [operand_text]


@pytest.mark.parametrize(
    "operand_text",
    [NEAR_MISS, f"{NEAR_MISS}.Running", "Conveyor.MTR_101", "PRE_MTR_101", "MTR_101_Cmd"],
    ids=["longer", "longer_member", "member_of_another_tag", "prefixed", "suffixed"],
)
def test_a_near_miss_is_not_a_reference(db: Session, project: Project, operand_text: str) -> None:
    """The false-positive class that would make the report worth skimming."""
    tag = _tag(db, project)
    _operand(db, project, _rung(db, project), text=operand_text)

    report = impact.analyse_rename(db, project=project, tag=tag, new_name="MTR_101_Cmd_Run")

    assert report.affected_objects == []
    assert report.unresolved_references == []
    assert report.is_safe_to_apply is True


def test_an_alias_onto_a_member_of_the_tag_is_reported(db: Session, project: Project) -> None:
    """The case a naive equality match misses: ``MTR_101.Running``, not ``MTR_101``."""
    tag = _tag(db, project, name="MTR_101_Status")
    _alias_case(db, project, "MTR_101_Status.Running", None)

    report = impact.analyse_rename(db, project=project, tag=tag, new_name="MTR_101_Stat")

    assert [entry["kind"] for entry in report.unresolved_references] == ["tag_alias"]
    assert report.unresolved_references[0]["references"] == ["MTR_101_Status.Running"]


# ---------------------------------------------------------------------------
# Resolved, unresolved and the boundary between them
# ---------------------------------------------------------------------------
def test_an_unresolved_reference_is_listed_apart_from_the_affected_total(
    db: Session, project: Project
) -> None:
    tag = _tag(db, project)
    _binding(db, project, tag_name=TAG_NAME, tag=tag)
    _binding(db, project, tag_name=TAG_NAME)

    report = impact.analyse_rename(db, project=project, tag=tag, new_name="MTR_101_Cmd_Run")

    assert len(report.affected_objects) == 1
    assert len(report.unresolved_references) == 1
    assert report.affected_counts["hmi"] == 1
    assert report.unresolved_references[0]["reason"]
    assert "One reference could not be resolved." in report.summary


def test_a_reference_that_resolved_to_a_different_tag_is_not_reported(
    db: Session, project: Project
) -> None:
    """The foreign key is authoritative: the text says ``MTR_101``, the binding does not."""
    tag = _tag(db, project)
    other = _tag(db, project, name="MTR_101", scope=TagScope.PROGRAM)
    _binding(db, project, tag_name=TAG_NAME, tag=other)

    report = impact.analyse_rename(db, project=project, tag=tag, new_name="MTR_101_Cmd_Run")

    assert report.affected_objects == []
    assert report.unresolved_references == []


def test_a_binding_bound_by_foreign_key_alone_is_still_reported(
    db: Session, project: Project
) -> None:
    """A stale ``tag_name`` does not hide a binding the platform already resolved."""
    tag = _tag(db, project)
    _binding(db, project, tag_name="LEGACY_NAME", tag=tag)

    report = impact.analyse_rename(db, project=project, tag=tag, new_name="MTR_101_Cmd_Run")

    assert [entry["kind"] for entry in report.affected_objects] == ["hmi_binding"]


def test_one_rung_naming_the_tag_four_times_is_one_affected_rung(
    db: Session, project: Project
) -> None:
    tag = _tag(db, project)
    rung = _rung(db, project)
    for text in (TAG_NAME, f"{TAG_NAME}.Running", f"{TAG_NAME}[0]", TAG_NAME):
        _operand(db, project, rung, text=text, resolved_name=TAG_NAME, tag=tag)

    report = impact.analyse_rename(db, project=project, tag=tag, new_name="MTR_101_Cmd_Run")

    assert len(report.affected_objects) == 1
    assert report.affected_counts["logic"] == 1


def test_a_rung_that_writes_the_tag_says_so(db: Session, project: Project) -> None:
    tag = _tag(db, project)
    rung = _rung(db, project)
    _operand(db, project, rung, text=TAG_NAME, resolved_name=TAG_NAME, tag=tag)
    _operand(db, project, rung, text=TAG_NAME, resolved_name=TAG_NAME, tag=tag, destination=True)

    report = impact.analyse_rename(db, project=project, tag=tag, new_name="MTR_101_Cmd_Run")

    assert report.affected_objects[0]["effect"] == "writes the tag"


def test_a_rung_is_labelled_so_a_reviewer_can_open_it(db: Session, project: Project) -> None:
    tag = _tag(db, project)
    _operand(db, project, _rung(db, project, ordinal=12), text=TAG_NAME, tag=tag)

    report = impact.analyse_rename(db, project=project, tag=tag, new_name="MTR_101_Cmd_Run")

    assert report.affected_objects[0]["label"] == "MainProgram / Conveyor_Interlocks / Rung 12"


# ---------------------------------------------------------------------------
# I/O bindings
# ---------------------------------------------------------------------------
def test_the_physical_bindings_are_reported_under_io(db: Session, project: Project) -> None:
    device = Device(project_id=project.id, tag_prefix="MTR_101", name="Conveyor motor")
    db.add(device)
    db.flush()
    signal = Signal(
        project_id=project.id,
        device_id=device.id,
        name="Run_Cmd",
        signal_type=SignalType.DISCRETE_OUTPUT,
        direction=SignalDirection.OUTPUT,
        ordinal=0,
    )
    io_point = IOPoint(
        project_id=project.id,
        address="Local:2:O.Data.3",
        signal_type=SignalType.DISCRETE_OUTPUT,
        direction=SignalDirection.OUTPUT,
    )
    db.add_all([signal, io_point])
    db.flush()
    db.add(AddressAssignment(project_id=project.id, signal_id=signal.id, io_point_id=io_point.id))
    db.flush()
    tag = _tag(db, project, signal_id=signal.id, io_point_id=io_point.id)

    report = impact.analyse_rename(db, project=project, tag=tag, new_name="MTR_101_Cmd_Run")

    kinds = sorted(entry["kind"] for entry in report.affected_objects)
    assert kinds == ["address_assignment", "io_point", "signal"]
    assert report.affected_counts["io"] == 3


def test_a_tag_with_no_physical_binding_reports_no_io(db: Session, project: Project) -> None:
    tag = _tag(db, project)

    report = impact.analyse_rename(db, project=project, tag=tag, new_name="MTR_101_Cmd_Run")

    assert report.affected_counts["io"] == 0


# ---------------------------------------------------------------------------
# The new name
# ---------------------------------------------------------------------------
def test_renaming_onto_an_existing_name_is_an_error(db: Session, project: Project) -> None:
    tag = _tag(db, project, name="MTR101_Run")
    existing = _tag(db, project, name="MTR_101_Cmd_Run")

    report = impact.analyse_rename(db, project=project, tag=tag, new_name="MTR_101_Cmd_Run")

    assert report.highest_severity is ValidationSeverity.ERROR
    assert report.requires_human_approval is True
    collisions = [e for e in report.affected_objects if e["kind"] == "colliding_tag"]
    assert [entry["object_id"] for entry in collisions] == [str(existing.id)]
    assert "already the name of Tag MTR_101_Cmd_Run" in report.summary


def test_a_collision_is_case_insensitive_because_logix_is(db: Session, project: Project) -> None:
    tag = _tag(db, project, name="MTR101_Run")
    _tag(db, project, name="MTR_101_Cmd_Run")

    report = impact.analyse_rename(db, project=project, tag=tag, new_name="mtr_101_cmd_run")

    assert report.highest_severity is ValidationSeverity.ERROR


def test_a_name_that_only_looks_similar_is_not_a_collision(db: Session, project: Project) -> None:
    """``_`` is a ``LIKE`` wildcard; ``MTRX101`` must not read as ``MTR_101``."""
    tag = _tag(db, project, name="CV101_Run")
    _tag(db, project, name="MTRX101")

    report = impact.analyse_rename(db, project=project, tag=tag, new_name="MTR_101")

    assert report.highest_severity is ValidationSeverity.INFORMATION
    assert report.affected_objects == []


def test_the_same_name_in_another_scope_is_not_a_collision(db: Session, project: Project) -> None:
    """Logix identity is (scope, program, name), so both may legitimately exist."""
    program = Program(project_id=project.id, name="Line4", ordinal=0)
    db.add(program)
    db.flush()
    tag = _tag(db, project, name="MTR101_Run")
    _tag(
        db,
        project,
        name="MTR_101_Cmd_Run",
        scope=TagScope.PROGRAM,
        scope_program_id=program.id,
    )

    report = impact.analyse_rename(db, project=project, tag=tag, new_name="MTR_101_Cmd_Run")

    assert report.highest_severity is ValidationSeverity.INFORMATION


def test_a_rename_needs_the_name_it_would_take(db: Session, project: Project) -> None:
    tag = _tag(db, project)

    with pytest.raises(ValidationError):
        impact.analyse_rename(db, project=project, tag=tag, new_name="   ")


# ---------------------------------------------------------------------------
# Nothing means nothing
# ---------------------------------------------------------------------------
def test_a_rename_that_affects_nothing_needs_no_approval(db: Session, project: Project) -> None:
    tag = _tag(db, project)

    report = impact.analyse_rename(db, project=project, tag=tag, new_name="MTR_101_Cmd_Run")

    assert report.affected_objects == []
    assert report.unresolved_references == []
    assert report.requires_human_approval is False
    assert report.highest_severity is ValidationSeverity.INFORMATION
    assert report.is_safe_to_apply is True
    assert report.summary.startswith("Renaming MTR_101 to MTR_101_Cmd_Run affects no other object:")


def test_an_unresolved_reference_alone_still_needs_a_human(db: Session, project: Project) -> None:
    """Nothing means nothing: an unresolved reference is still a reference."""
    tag = _tag(db, project)
    _binding(db, project, tag_name=TAG_NAME)

    report = impact.analyse_rename(db, project=project, tag=tag, new_name="MTR_101_Cmd_Run")

    assert report.affected_objects == []
    assert report.requires_human_approval is True
    assert report.is_safe_to_apply is False


# ---------------------------------------------------------------------------
# The summary a reviewer reads aloud
# ---------------------------------------------------------------------------
def test_the_summary_reads_as_a_sentence(db: Session, project: Project) -> None:
    tag = _tag(db, project, name="MTR101_Run")
    for ordinal in (3, 7, 12, 19):
        _operand(
            db,
            project,
            _rung(db, project, ordinal=ordinal),
            text="MTR101_Run",
            resolved_name="MTR101_Run",
            tag=tag,
        )
    _binding(db, project, tag_name="MTR101_Run", tag=tag)
    _alarm(db, project, name="MTR101_Run", tag=tag)
    _test_step(db, project, name="MTR101_Run", tag=tag)
    _add(
        db,
        Interlock(
            project_id=project.id,
            reference="ILK-003",
            name="Guard door open stops the conveyor",
            kind=InterlockKind.TRIP,
            trip_tag_name="MTR101_Run",
        ),
    )

    report = impact.analyse_rename(db, project=project, tag=tag, new_name="MTR_101_Cmd_Run")

    assert report.summary == (
        "Renaming MTR101_Run to MTR_101_Cmd_Run affects 7 objects: 4 rungs, 1 HMI binding, "
        "1 alarm trigger and 1 test step. One reference could not be resolved."
    )
    assert report.affected_counts == {
        "logic": 4,
        "hmi": 1,
        "alarms": 1,
        "tests": 1,
        "interlocks": 0,
        "io": 0,
        "documentation": 0,
    }


def test_the_summary_pluralises_an_irregular_noun(db: Session, project: Project) -> None:
    tag = _tag(db, project)
    _alias_case(db, project, TAG_NAME, tag)
    _alias_case(db, project, TAG_NAME, tag)

    report = impact.analyse_rename(db, project=project, tag=tag, new_name="MTR_101_Cmd_Run")

    assert "2 tag aliases" in report.summary


# ---------------------------------------------------------------------------
# Deletes
# ---------------------------------------------------------------------------
def test_deleting_a_tag_reports_the_same_references_a_rename_would(
    db: Session, project: Project
) -> None:
    tag = _tag(db, project)
    _operand(db, project, _rung(db, project), text=TAG_NAME, resolved_name=TAG_NAME, tag=tag)
    _binding(db, project, tag_name=TAG_NAME, tag=tag)
    _alias_case(db, project, TAG_NAME, tag)

    report = impact.analyse_delete(
        db, project=project, object_type=ObjectType.TAG, object_id=tag.id
    )

    assert report.object_type is ObjectType.TAG
    assert sorted(entry["kind"] for entry in report.affected_objects) == [
        "hmi_binding",
        "logic_rung",
        "tag_alias",
    ]
    assert report.requires_human_approval is True
    assert report.summary.startswith("Deleting Tag MTR_101 affects 3 objects:")


def test_deleting_a_tag_also_finds_what_only_a_foreign_key_holds(
    db: Session, project: Project
) -> None:
    """An alias row points at its own tag by id alone; no text names it."""
    alias_tag = _tag(db, project, name="MTR_101_Alias", is_alias=True)
    _add(
        db,
        TagAlias(
            project_id=project.id,
            alias_tag_id=alias_tag.id,
            target_expression="Local:1:I.Data[0]",
        ),
    )

    report = impact.analyse_delete(
        db, project=project, object_type=ObjectType.TAG, object_id=alias_tag.id
    )

    assert [entry["kind"] for entry in report.affected_objects] == ["tag_alias"]
    assert report.affected_objects[0]["effect"] == "references it through alias_tag_id"


def test_deleting_a_program_finds_what_hangs_off_it(db: Session, project: Project) -> None:
    """The generic sweep: inbound foreign keys, read off the registry."""
    program = Program(project_id=project.id, name="Line4", ordinal=0)
    db.add(program)
    db.flush()
    routine = Routine(project_id=project.id, program_id=program.id, name="Main", ordinal=0)
    db.add(routine)
    db.flush()
    _tag(db, project, name="Line4_Step", scope=TagScope.PROGRAM, scope_program_id=program.id)

    report = impact.analyse_delete(
        db, project=project, object_type=ObjectType.PROGRAM, object_id=program.id
    )

    entries = {entry["kind"]: entry for entry in report.affected_objects}
    assert set(entries) == {"routine", "tag"}
    assert entries["routine"]["effect"] == "references it through program_id"
    assert report.affected_counts["logic"] == 2


def test_deleting_an_object_nothing_references_needs_no_approval(
    db: Session, project: Project
) -> None:
    program = Program(project_id=project.id, name="Unused", ordinal=0)
    db.add(program)
    db.flush()

    report = impact.analyse_delete(
        db, project=project, object_type=ObjectType.PROGRAM, object_id=program.id
    )

    assert report.affected_objects == []
    assert report.requires_human_approval is False
    assert report.summary.startswith("Deleting Program Unused affects no other object:")


# ---------------------------------------------------------------------------
# Change sets
# ---------------------------------------------------------------------------
def _rename_changeset(db: Session, project: Project, tag: Tag, to_name: str) -> object:
    payload = ProposeChangesetPayload(
        title="Bring motor 101 onto the naming standard",
        summary="Rename the run command to the ESCO standard form.",
        base_revision_number=0,
        operations=[
            ChangeOperationProposal(
                operation_type=ChangeOperationType.RENAME_TAG,
                object_type=ObjectType.TAG,
                object_id=tag.id,
                based_on_object_revision=tag.object_revision,
                values={"from_name": tag.name, "to_name": to_name},
                description=f"Rename {tag.name} to {to_name}.",
            )
        ],
        requires_human_approval=True,
    )
    changeset = changesets.build_changeset(
        db, project=project, payload=payload, author_agent="tag_architecture"
    )
    db.flush()
    return changeset


def test_a_changeset_rename_produces_a_linked_report(db: Session, project: Project) -> None:
    tag = _tag(db, project)
    _binding(db, project, tag_name=TAG_NAME, tag=tag)
    changeset = _rename_changeset(db, project, tag, "MTR_101_Cmd_Run")

    reports = impact.analyse_changeset(db, changeset=changeset)  # type: ignore[arg-type]

    assert len(reports) == 1
    report = reports[0]
    assert report.change_set_id == changeset.id  # type: ignore[attr-defined]
    assert report.operation_id is not None
    assert report.change_description == "Rename MTR_101 to MTR_101_Cmd_Run."
    assert [entry["kind"] for entry in report.affected_objects] == ["hmi_binding"]


def test_an_update_of_the_name_field_is_analysed_as_a_rename(db: Session, project: Project) -> None:
    """An operation that renames without saying "rename" renames just as thoroughly.

    An agent can no longer *propose* this shape: ``ChangeOperationProposal``
    refuses ``update_field`` on a tag's ``name`` and names ``rename_tag`` as the
    form to use, so the approval gate and the impact report cannot be stepped
    around at the action boundary. This rule is the layer behind that one and
    still has to hold — a change set can reach the merge from the API, from a
    human editor, or from a proposal recorded before that refusal existed. The
    operation is therefore written directly onto the change set here, which is
    exactly how one arrives by any of those routes.
    """
    tag = _tag(db, project)
    _binding(db, project, tag_name=TAG_NAME, tag=tag)
    payload = ProposeChangesetPayload(
        title="Tidy the tag name",
        summary="Update the name field directly.",
        base_revision_number=0,
        operations=[
            ChangeOperationProposal(
                operation_type=ChangeOperationType.UPDATE_FIELD,
                object_type=ObjectType.TAG,
                object_id=tag.id,
                based_on_object_revision=tag.object_revision,
                field_name="description",
                values={"description": "Run command."},
            )
        ],
    )
    changeset = changesets.build_changeset(
        db, project=project, payload=payload, author_agent="tag_architecture"
    )
    db.flush()

    operation = changeset.operations[0]
    operation.field_name = "name"
    operation.after = {"name": "MTR_101_Cmd_Run"}
    db.flush()

    reports = impact.analyse_changeset(db, changeset=changeset)

    assert [report.change_description for report in reports] == [
        "Rename MTR_101 to MTR_101_Cmd_Run."
    ]


def test_a_changeset_delete_produces_a_report_linked_to_its_operation(
    db: Session, project: Project
) -> None:
    tag = _tag(db, project)
    _binding(db, project, tag_name=TAG_NAME, tag=tag)
    payload = ProposeChangesetPayload(
        title="Retire the unused command tag",
        summary="Delete MTR_101.",
        base_revision_number=0,
        operations=[
            ChangeOperationProposal(
                operation_type=ChangeOperationType.DELETE_OBJECT,
                object_type=ObjectType.TAG,
                object_id=tag.id,
                based_on_object_revision=tag.object_revision,
            )
        ],
    )
    changeset = changesets.build_changeset(
        db, project=project, payload=payload, author_agent="tag_architecture"
    )
    db.flush()

    reports = impact.analyse_changeset(db, changeset=changeset)

    assert len(reports) == 1
    assert reports[0].operation_id == changeset.operations[0].id
    assert reports[0].change_description == "Delete Tag MTR_101."


def test_a_changeset_that_neither_renames_nor_deletes_produces_no_report(
    db: Session, project: Project
) -> None:
    tag = _tag(db, project)
    payload = ProposeChangesetPayload(
        title="Describe the command tag",
        summary="Add a description.",
        base_revision_number=0,
        operations=[
            ChangeOperationProposal(
                operation_type=ChangeOperationType.UPDATE_FIELD,
                object_type=ObjectType.TAG,
                object_id=tag.id,
                based_on_object_revision=tag.object_revision,
                field_name="description",
                values={"description": "Run command from the HMI."},
            )
        ],
    )
    changeset = changesets.build_changeset(
        db, project=project, payload=payload, author_agent="tag_architecture"
    )
    db.flush()

    assert impact.analyse_changeset(db, changeset=changeset) == []


def test_a_rename_of_a_tag_that_no_longer_exists_is_skipped(db: Session, project: Project) -> None:
    """A missing object is a merge conflict with a name; it is not an impact report."""
    tag = _tag(db, project)
    changeset = _rename_changeset(db, project, tag, "MTR_101_Cmd_Run")
    db.delete(tag)
    db.flush()

    assert impact.analyse_changeset(db, changeset=changeset) == []  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# The report is evidence, so it is durable
# ---------------------------------------------------------------------------
def test_the_report_is_persisted_against_the_project(db: Session, project: Project) -> None:
    tag = _tag(db, project)
    _binding(db, project, tag_name=TAG_NAME, tag=tag)

    report = impact.analyse_rename(db, project=project, tag=tag, new_name="MTR_101_Cmd_Run")

    stored = db.execute(select(ImpactReport).where(ImpactReport.id == report.id)).scalars().one()
    assert stored.project_id == project.id
    assert stored.object_label == "Tag MTR_101"
    assert stored.total_affected == 1


# ---------------------------------------------------------------------------
# The tables this module is driven by
# ---------------------------------------------------------------------------
def test_every_object_type_has_a_discipline() -> None:
    """A new object type must not land in a silent default bucket."""
    assert set(impact._DISCIPLINE_BY_TYPE) == set(ObjectType)
    assert set(impact._DISCIPLINE_BY_TYPE.values()) <= set(impact.DISCIPLINES)
