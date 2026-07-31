"""Builders for the validation tests.

Every validator is a pure function of a :class:`~app.validation.base.ValidationContext`,
so these tests build the two or three rows a check actually reads and nothing
else. They deliberately do **not** use the file fixtures: a check that only fires
against the synthetic demo project is a check nobody can reason about, and a
negative case built from a large fixture proves nothing about *why* the validator
stayed quiet.

Each builder fills in only what the column layer requires and leaves everything
else at its model default, so a test that cares about ``fail_state`` says so and a
test that does not is not silently asserting one.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy.orm import Session

from app.core.enums import (
    AlarmSeverity,
    EvidenceKind,
    FaultClass,
    InstructionCategory,
    InterlockKind,
    LogicElementKind,
    ObjectType,
    OperandKind,
    SignalDirection,
    SignalType,
)
from app.models.core import ObjectEvidenceLink
from app.models.delivery import AlarmDefinition, HMIBinding, HMIScreen, TestCase, TestStep
from app.models.interlocks import (
    FaultDefinition,
    Interlock,
    ModeDefinition,
    Permissive,
    ResetBehavior,
    StateDefinition,
    TransitionDefinition,
)
from app.models.plant import AddressAssignment, Device, HardwareModule, IOPoint, Signal
from app.models.plc import (
    LogicInstruction,
    LogicOperand,
    LogicRung,
    Program,
    Routine,
)
from app.models.profile import ProjectQuestion, TargetPlatform
from app.models.project import Project
from app.models.release import ReleaseCandidate
from app.models.requirements import Clarification, Requirement
from app.models.tags import AOIDefinitionReference, Tag, TagAlias
from app.validation.base import Issue, ValidationContext, Validator


# ---------------------------------------------------------------------------
# Running a validator
# ---------------------------------------------------------------------------
def context_for(db: Session, project: Project) -> ValidationContext:
    """A fresh context. Never reused between checks — it caches by design."""
    return ValidationContext(
        db=db, project=project, revision_number=project.current_revision_number
    )


def run_check(validator: Validator, db: Session, project: Project) -> list[Issue]:
    return list(validator.check(context_for(db, project)))


def codes(issues: Sequence[Issue]) -> list[str]:
    return [issue.code for issue in issues]


def issues_coded(issues: Sequence[Issue], code: str) -> list[Issue]:
    return [issue for issue in issues if issue.code == code]


def only(issues: Sequence[Issue], code: str) -> Issue:
    """The single issue with ``code``, failing loudly if there is not exactly one."""
    matches = issues_coded(issues, code)
    assert len(matches) == 1, f"expected exactly one {code}, got {codes(issues)}"
    return matches[0]


# ---------------------------------------------------------------------------
# Persistence helper
# ---------------------------------------------------------------------------
def _add[T](db: Session, instance: T) -> T:
    db.add(instance)
    db.flush()
    return instance


# ---------------------------------------------------------------------------
# Requirements
# ---------------------------------------------------------------------------
def make_requirement(
    db: Session, project: Project, reference: str = "REQ-001", **kwargs: Any
) -> Requirement:
    kwargs.setdefault("title", f"{reference} title")
    return _add(db, Requirement(project_id=project.id, reference=reference, **kwargs))


def make_clarification(db: Session, project: Project, **kwargs: Any) -> Clarification:
    kwargs.setdefault("question", "Does 'stop' mean a ramp-down or a drive disable?")
    kwargs.setdefault("ambiguity", "Controlled stop, or immediate disable.")
    return _add(db, Clarification(project_id=project.id, **kwargs))


def make_question(
    db: Session, project: Project, reference: str = "Q-001", **kwargs: Any
) -> ProjectQuestion:
    kwargs.setdefault("question", "Which controller family is the target?")
    return _add(db, ProjectQuestion(project_id=project.id, reference=reference, **kwargs))


def make_evidence_link(
    db: Session,
    project: Project,
    *,
    object_type: ObjectType,
    object_id: Any,
    basis_kind: EvidenceKind = EvidenceKind.TEMPLATE_RULE,
    **kwargs: Any,
) -> ObjectEvidenceLink:
    if basis_kind is EvidenceKind.TEMPLATE_RULE:
        kwargs.setdefault("template_id", "esco.motor.standard.v1")
    return _add(
        db,
        ObjectEvidenceLink(
            project_id=project.id,
            object_type=object_type,
            object_id=object_id,
            basis_kind=basis_kind,
            **kwargs,
        ),
    )


# ---------------------------------------------------------------------------
# Tags
# ---------------------------------------------------------------------------
def make_tag(db: Session, project: Project, name: str = "Motor_Run_Cmd", **kwargs: Any) -> Tag:
    kwargs.setdefault("description", "Run command for the conveyor motor.")
    return _add(db, Tag(project_id=project.id, name=name, **kwargs))


def make_tag_alias(db: Session, project: Project, alias: Tag, **kwargs: Any) -> TagAlias:
    return _add(db, TagAlias(project_id=project.id, alias_tag_id=alias.id, **kwargs))


def make_aoi_reference(
    db: Session, project: Project, name: str = "ESCO_Motor", **kwargs: Any
) -> AOIDefinitionReference:
    return _add(db, AOIDefinitionReference(project_id=project.id, name=name, **kwargs))


# ---------------------------------------------------------------------------
# Plant and I/O
# ---------------------------------------------------------------------------
def make_device(
    db: Session, project: Project, tag_prefix: str = "MTR_101", **kwargs: Any
) -> Device:
    kwargs.setdefault("name", f"{tag_prefix} conveyor motor")
    return _add(db, Device(project_id=project.id, tag_prefix=tag_prefix, **kwargs))


def make_signal(
    db: Session,
    project: Project,
    device: Device,
    name: str = "Run_Cmd",
    *,
    signal_type: SignalType = SignalType.DISCRETE_OUTPUT,
    direction: SignalDirection = SignalDirection.OUTPUT,
    ordinal: int = 0,
    **kwargs: Any,
) -> Signal:
    return _add(
        db,
        Signal(
            project_id=project.id,
            device_id=device.id,
            name=name,
            signal_type=signal_type,
            direction=direction,
            ordinal=ordinal,
            **kwargs,
        ),
    )


def make_module(
    db: Session, project: Project, name: str = "Local:2", **kwargs: Any
) -> HardwareModule:
    return _add(db, HardwareModule(project_id=project.id, name=name, **kwargs))


def make_io_point(
    db: Session,
    project: Project,
    address: str = "Local:2:O.Data.0",
    *,
    signal_type: SignalType = SignalType.DISCRETE_OUTPUT,
    direction: SignalDirection = SignalDirection.OUTPUT,
    module: HardwareModule | None = None,
    **kwargs: Any,
) -> IOPoint:
    return _add(
        db,
        IOPoint(
            project_id=project.id,
            address=address,
            signal_type=signal_type,
            direction=direction,
            hardware_module_id=module.id if module is not None else None,
            **kwargs,
        ),
    )


def make_assignment(
    db: Session,
    project: Project,
    signal: Signal,
    point: IOPoint | None = None,
    **kwargs: Any,
) -> AddressAssignment:
    return _add(
        db,
        AddressAssignment(
            project_id=project.id,
            signal_id=signal.id,
            io_point_id=point.id if point is not None else None,
            **kwargs,
        ),
    )


# ---------------------------------------------------------------------------
# PLC organisation and logic
# ---------------------------------------------------------------------------
def make_program(
    db: Session, project: Project, name: str = "MainProgram", ordinal: int = 0, **kwargs: Any
) -> Program:
    return _add(db, Program(project_id=project.id, name=name, ordinal=ordinal, **kwargs))


def make_routine(
    db: Session,
    project: Project,
    program: Program,
    name: str = "MainRoutine",
    *,
    ordinal: int = 0,
    is_main: bool = True,
    **kwargs: Any,
) -> Routine:
    return _add(
        db,
        Routine(
            project_id=project.id,
            program_id=program.id,
            name=name,
            ordinal=ordinal,
            is_main=is_main,
            **kwargs,
        ),
    )


def make_rung(
    db: Session, project: Project, routine: Routine, ordinal: int = 0, **kwargs: Any
) -> LogicRung:
    return _add(
        db,
        LogicRung(project_id=project.id, routine_id=routine.id, ordinal=ordinal, **kwargs),
    )


def make_ladder(db: Session, project: Project, rung_ordinal: int = 0) -> LogicRung:
    """A program, its main routine and one rung — the usual minimum."""
    program = make_program(db, project)
    routine = make_routine(db, project, program)
    return make_rung(db, project, routine, ordinal=rung_ordinal)


def make_instruction(
    db: Session,
    project: Project,
    rung: LogicRung,
    mnemonic: str,
    operands: Sequence[str] = (),
    *,
    ordinal: int = 0,
    category: InstructionCategory = InstructionCategory.BIT,
    element_kind: LogicElementKind = LogicElementKind.CONDITION,
    unresolved: Sequence[int] = (),
    literals: Sequence[int] = (),
    destinations: Sequence[int] = (),
    unresolved_reason: str = "no tag by that name in controller scope",
) -> LogicInstruction:
    """One instruction and its operands.

    Operand kinds default to ``TAG``; ``unresolved`` and ``literals`` name the
    positions that are something else. ``destinations`` marks the positions the
    parser concluded are written, which is what an Add-On Instruction's outputs
    look like to a catalogue that does not describe it.
    """
    instruction = _add(
        db,
        LogicInstruction(
            project_id=project.id,
            rung_id=rung.id,
            ordinal=ordinal,
            mnemonic=mnemonic,
            category=category,
            element_kind=element_kind,
        ),
    )
    for index, text in enumerate(operands):
        if index in set(unresolved):
            kind = OperandKind.UNRESOLVED
            resolved = ""
            reason = unresolved_reason
        elif index in set(literals):
            kind = OperandKind.LITERAL
            resolved = ""
            reason = ""
        else:
            kind = OperandKind.TAG
            resolved = text
            reason = ""
        _add(
            db,
            LogicOperand(
                project_id=project.id,
                instruction_id=instruction.id,
                ordinal=index,
                kind=kind,
                text=text,
                resolved_name=resolved,
                unresolved_reason=reason,
                is_destination=index in set(destinations),
            ),
        )
    return instruction


def write_tag(
    db: Session,
    project: Project,
    rung: LogicRung,
    tag_name: str,
    *,
    mnemonic: str = "OTE",
    ordinal: int = 0,
) -> LogicInstruction:
    """An output instruction driving ``tag_name``."""
    return make_instruction(
        db,
        project,
        rung,
        mnemonic,
        [tag_name],
        ordinal=ordinal,
        element_kind=LogicElementKind.OUTPUT,
    )


def read_tag(
    db: Session,
    project: Project,
    rung: LogicRung,
    tag_name: str,
    *,
    ordinal: int = 0,
) -> LogicInstruction:
    """A condition instruction examining ``tag_name``."""
    return make_instruction(db, project, rung, "XIC", [tag_name], ordinal=ordinal)


# ---------------------------------------------------------------------------
# Interlocks, modes and states
# ---------------------------------------------------------------------------
def make_interlock(
    db: Session,
    project: Project,
    reference: str = "ILK-001",
    *,
    kind: InterlockKind = InterlockKind.INTERLOCK,
    **kwargs: Any,
) -> Interlock:
    kwargs.setdefault("name", f"{reference} low suction pressure trip")
    return _add(db, Interlock(project_id=project.id, reference=reference, kind=kind, **kwargs))


def make_permissive(
    db: Session, project: Project, name: str = "Guard door closed", **kwargs: Any
) -> Permissive:
    return _add(db, Permissive(project_id=project.id, name=name, **kwargs))


def make_mode(
    db: Session, project: Project, name: str = "AUTO", ordinal: int = 0, **kwargs: Any
) -> ModeDefinition:
    return _add(db, ModeDefinition(project_id=project.id, name=name, ordinal=ordinal, **kwargs))


def make_state(
    db: Session, project: Project, name: str = "RUNNING", ordinal: int = 0, **kwargs: Any
) -> StateDefinition:
    return _add(db, StateDefinition(project_id=project.id, name=name, ordinal=ordinal, **kwargs))


def make_transition(
    db: Session,
    project: Project,
    from_state: StateDefinition,
    to_state: StateDefinition,
    ordinal: int = 0,
    **kwargs: Any,
) -> TransitionDefinition:
    return _add(
        db,
        TransitionDefinition(
            project_id=project.id,
            from_state_id=from_state.id,
            to_state_id=to_state.id,
            ordinal=ordinal,
            **kwargs,
        ),
    )


def make_fault(
    db: Session,
    project: Project,
    name: str = "Low suction pressure",
    *,
    fault_class: FaultClass = FaultClass.PROCESS,
    **kwargs: Any,
) -> FaultDefinition:
    return _add(
        db,
        FaultDefinition(project_id=project.id, name=name, fault_class=fault_class, **kwargs),
    )


def make_reset_behavior(
    db: Session, project: Project, name: str = "Panel reset", **kwargs: Any
) -> ResetBehavior:
    return _add(db, ResetBehavior(project_id=project.id, name=name, **kwargs))


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------
def make_alarm(
    db: Session,
    project: Project,
    reference: str = "ALM-001",
    *,
    severity: AlarmSeverity = AlarmSeverity.HIGH,
    ordinal: int = 0,
    **kwargs: Any,
) -> AlarmDefinition:
    kwargs.setdefault("name", f"{reference} hopper level high")
    return _add(
        db,
        AlarmDefinition(
            project_id=project.id,
            reference=reference,
            severity=severity,
            ordinal=ordinal,
            **kwargs,
        ),
    )


def make_screen(
    db: Session, project: Project, key: str = "overview", ordinal: int = 0, **kwargs: Any
) -> HMIScreen:
    kwargs.setdefault("name", "Line 3 overview")
    return _add(db, HMIScreen(project_id=project.id, key=key, ordinal=ordinal, **kwargs))


def make_binding(db: Session, project: Project, screen: HMIScreen, **kwargs: Any) -> HMIBinding:
    kwargs.setdefault("property_name", "Value")
    return _add(db, HMIBinding(project_id=project.id, screen_id=screen.id, **kwargs))


def make_test_case(
    db: Session, project: Project, reference: str = "TC-001", ordinal: int = 0, **kwargs: Any
) -> TestCase:
    kwargs.setdefault("title", f"{reference} conveyor start")
    return _add(db, TestCase(project_id=project.id, reference=reference, ordinal=ordinal, **kwargs))


def make_test_step(
    db: Session,
    project: Project,
    case: TestCase,
    step_number: int = 10,
    ordinal: int = 0,
    **kwargs: Any,
) -> TestStep:
    return _add(
        db,
        TestStep(
            project_id=project.id,
            test_case_id=case.id,
            step_number=step_number,
            ordinal=ordinal,
            **kwargs,
        ),
    )


# ---------------------------------------------------------------------------
# Profile and release
# ---------------------------------------------------------------------------
def make_target_platform(db: Session, project: Project, **kwargs: Any) -> TargetPlatform:
    return _add(db, TargetPlatform(project_id=project.id, **kwargs))


def make_release_candidate(
    db: Session, project: Project, candidate_number: int = 1, **kwargs: Any
) -> ReleaseCandidate:
    return _add(
        db,
        ReleaseCandidate(project_id=project.id, candidate_number=candidate_number, **kwargs),
    )
