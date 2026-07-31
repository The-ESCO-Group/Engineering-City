"""Small, explicit model fixtures built in memory.

The generators read the canonical engineering model, so the tests build that
model directly — transient ORM objects, never flushed, with every field the
generators read set on purpose. Nothing here goes near a session: a generator
that needed one would be a generator that could not be run twice on the same
revision and compared, which is the property under test.

Object ids are fixed (``uuid.UUID(int=n)``) rather than generated, for the same
reason the generators never generate one: an id that changed between two runs
would show up in the manifest and break the byte-identity the whole package
rests on.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

import pytest

from app.core.enums import (
    BaseDataType,
    ControllerFamily,
    ControllerTaskType,
    InstructionCategory,
    LogicElementKind,
    OperandKind,
    ProgramType,
    RoutineLanguage,
    TagScope,
)
from app.generators.rockwell.package import (
    PackageOptions,
    ProgramContent,
    ProjectModel,
    RoutineContent,
    TargetProfile,
    TaskContent,
    UdtContent,
)
from app.models.plant import HardwareModule
from app.models.plc import (
    ControllerModel,
    ControllerTask,
    LogicBranch,
    LogicInstruction,
    LogicOperand,
    LogicReference,
    LogicRung,
    Program,
    Routine,
)
from app.models.tags import AOIDefinitionReference, Tag, UDTDefinition, UDTMember

PROJECT_ID = uuid.UUID(int=1)


def oid(number: int) -> uuid.UUID:
    """A fixed identifier, so two generations of one revision agree."""
    return uuid.UUID(int=number)


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def make_controller(
    *, name: str = "Line3", catalog_number: str = "1756-L83E", description: str = ""
) -> ControllerModel:
    return ControllerModel(
        id=oid(100),
        project_id=PROJECT_ID,
        name=name,
        catalog_number=catalog_number,
        major_revision=32,
        minor_revision=11,
        description=description,
        project_creation_date="Mon Nov 04 08:00:00 2024",
        use_as_folder=False,
        redundancy_enabled=False,
        safety_enabled=False,
        comm_path="",
    )


def make_task(
    *,
    name: str = "MainTask",
    ordinal: int = 0,
    task_type: ControllerTaskType = ControllerTaskType.PERIODIC,
    identifier: int = 110,
) -> ControllerTask:
    return ControllerTask(
        id=oid(identifier),
        project_id=PROJECT_ID,
        name=name,
        task_type=task_type,
        priority=10,
        rate_ms=20.0,
        watchdog_ms=500.0,
        disable_update_outputs=False,
        inhibit_task=False,
        event_trigger="",
        description="Line 3 scan.",
        ordinal=ordinal,
    )


def make_module(
    *, name: str = "DI_01", catalog_number: str = "1756-IB16", identifier: int = 120
) -> HardwareModule:
    return HardwareModule(
        id=oid(identifier),
        project_id=PROJECT_ID,
        name=name,
        catalog_number=catalog_number,
        major_revision=3,
        minor_revision=1,
        description="Digital input card.",
        point_count=16,
        chassis="",
        electronic_keying="",
        is_referenced_only=False,
    )


def make_tag(
    name: str,
    *,
    base_data_type: BaseDataType = BaseDataType.BOOL,
    data_type_name: str = "",
    description: str = "",
    scope: TagScope = TagScope.CONTROLLER,
    scope_program_id: uuid.UUID | None = None,
    is_alias: bool = False,
    alias_for: str = "",
    dimensions: str = "",
    external_access: str = "Read/Write",
    is_constant: bool = False,
    identifier: int | None = None,
) -> Tag:
    return Tag(
        id=None if identifier is None else oid(identifier),
        project_id=PROJECT_ID,
        name=name,
        scope=scope,
        scope_program_id=scope_program_id,
        base_data_type=base_data_type,
        data_type_name=data_type_name,
        dimensions=dimensions,
        description=description,
        engineering_units="",
        external_access=external_access,
        is_constant=is_constant,
        is_alias=is_alias,
        alias_for=alias_for,
        hmi_exposed=False,
        initial_value="",
        source_name="",
        is_generated=True,
        usage_count=0,
    )


def make_operand(
    ordinal: int,
    text: str,
    *,
    kind: OperandKind = OperandKind.TAG,
    resolved_name: str | None = None,
    is_destination: bool = False,
    unresolved_reason: str = "",
) -> LogicOperand:
    if resolved_name is None:
        resolved_name = text if kind is OperandKind.TAG else ""
    return LogicOperand(
        project_id=PROJECT_ID,
        ordinal=ordinal,
        kind=kind,
        text=text,
        resolved_name=resolved_name,
        data_type_name="",
        is_destination=is_destination,
        unresolved_reason=unresolved_reason,
    )


def make_instruction(
    ordinal: int,
    mnemonic: str,
    operands: Sequence[str] | Sequence[LogicOperand] = (),
    *,
    category: InstructionCategory = InstructionCategory.BIT,
    element_kind: LogicElementKind = LogicElementKind.CONDITION,
    branch_level: int = 0,
    branch_index: int = 0,
    is_destructive: bool = False,
    is_supported: bool = True,
    unsupported_reason: str = "",
    identifier: int | None = None,
) -> LogicInstruction:
    rows: list[LogicOperand] = []
    for position, operand in enumerate(operands):
        rows.append(
            operand if isinstance(operand, LogicOperand) else make_operand(position, operand)
        )
    instruction = LogicInstruction(
        id=None if identifier is None else oid(identifier),
        project_id=PROJECT_ID,
        ordinal=ordinal,
        mnemonic=mnemonic,
        category=category,
        element_kind=element_kind,
        branch_level=branch_level,
        branch_index=branch_index,
        is_destructive=is_destructive,
        text="",
        is_supported=is_supported,
        unsupported_reason=unsupported_reason,
    )
    instruction.operands = rows
    return instruction


def make_branch(*, level: int, leg_index: int, ordinal: int = 0) -> LogicBranch:
    return LogicBranch(
        project_id=PROJECT_ID,
        level=level,
        leg_index=leg_index,
        ordinal=ordinal,
        description="",
    )


def make_rung(
    ordinal: int,
    instructions: Sequence[LogicInstruction],
    *,
    comment: str = "",
    branches: Sequence[LogicBranch] = (),
    identifier: int | None = None,
) -> LogicRung:
    rung = LogicRung(
        id=None if identifier is None else oid(identifier),
        project_id=PROJECT_ID,
        ordinal=ordinal,
        title="",
        comment=comment,
        text="",
        is_safety_related=False,
        requires_human_approval=False,
        risk_note="",
        has_unresolved_operands=False,
    )
    rung.instructions = list(instructions)
    rung.branches = list(branches)
    return rung


def make_routine(
    name: str,
    *,
    ordinal: int = 0,
    description: str = "",
    is_main: bool = False,
    template_id: str = "",
    template_version: str = "",
    identifier: int = 200,
) -> Routine:
    return Routine(
        id=oid(identifier),
        project_id=PROJECT_ID,
        program_id=oid(300),
        name=name,
        language=RoutineLanguage.LADDER,
        description=description,
        ordinal=ordinal,
        is_main=is_main,
        is_called=not is_main,
        rung_count=0,
        template_id=template_id,
        template_version=template_version,
    )


def make_program(
    name: str = "MainProgram",
    *,
    ordinal: int = 0,
    description: str = "",
    main_routine_name: str = "MainRoutine",
    identifier: int = 300,
) -> Program:
    return Program(
        id=oid(identifier),
        project_id=PROJECT_ID,
        name=name,
        program_type=ProgramType.NORMAL,
        description=description,
        main_routine_name=main_routine_name,
        fault_routine_name="",
        disabled=False,
        ordinal=ordinal,
        test_edits=False,
    )


def make_udt(
    name: str = "ConveyorStatus",
    *,
    description: str = "Per-conveyor status word.",
    identifier: int = 400,
) -> UDTDefinition:
    return UDTDefinition(
        id=oid(identifier),
        project_id=PROJECT_ID,
        name=name,
        description=description,
        family="",
        member_count=0,
        template_id="",
        checksum="",
    )


def make_udt_member(
    name: str,
    ordinal: int,
    *,
    base_data_type: BaseDataType = BaseDataType.BOOL,
    description: str = "",
    dimensions: str = "",
) -> UDTMember:
    return UDTMember(
        project_id=PROJECT_ID,
        udt_id=oid(400),
        name=name,
        base_data_type=base_data_type,
        data_type_name="",
        dimensions=dimensions,
        ordinal=ordinal,
        description=description,
        external_access="Read/Write",
        hidden=False,
    )


def make_aoi(
    name: str = "MotorStarter",
    *,
    is_available: bool = True,
    identifier: int = 500,
) -> AOIDefinitionReference:
    return AOIDefinitionReference(
        id=oid(identifier),
        project_id=PROJECT_ID,
        name=name,
        revision="1.2",
        description="Seals in a motor run command while all permissives hold.",
        parameter_count=3,
        parameters=[
            {
                "name": "StartCmd",
                "data_type": "BOOL",
                "usage": "Input",
                "required": True,
                "visible": True,
            },
            {
                "name": "RunOut",
                "data_type": "BOOL",
                "usage": "Output",
                "required": True,
                "visible": True,
            },
        ],
        is_available=is_available,
        availability_note="" if is_available else "referenced by MainRoutine, not supplied",
    )


def make_reference(
    tag_name: str,
    *,
    program_name: str = "MainProgram",
    routine_name: str = "MainRoutine",
    rung_id: uuid.UUID | None = None,
    is_destructive: bool = False,
    reference_kind: str = "read",
) -> LogicReference:
    return LogicReference(
        project_id=PROJECT_ID,
        rung_id=rung_id or oid(700),
        tag_name=tag_name,
        is_destructive=is_destructive,
        routine_name=routine_name,
        program_name=program_name,
        reference_kind=reference_kind,
    )


# ---------------------------------------------------------------------------
# A small, complete project
# ---------------------------------------------------------------------------


def build_demo_model() -> ProjectModel:
    """One controller, one program, two routines, a UDT and an AOI call.

    Deliberately small and written out longhand: a fixture assembled by a
    pipeline that does not exist yet would test the pipeline, and a fixture
    nobody can read cannot be checked against what the generator emitted.
    """
    program = make_program("MainProgram", description="Line 3 control.")
    controller_tags = [
        make_tag("ESTOP_OK", description="Emergency stop healthy.", identifier=600),
        make_tag("LINE_PERMISSIVE", description="Master permissive.", identifier=601),
        make_tag(
            "CV101_Status",
            base_data_type=BaseDataType.UDT,
            data_type_name="ConveyorStatus",
            description="CV101 status word.",
            identifier=602,
        ),
        make_tag(
            "CV101_Starter",
            base_data_type=BaseDataType.ADDON_INSTRUCTION,
            data_type_name="MotorStarter",
            description="CV101 starter instance.",
            identifier=603,
        ),
        make_tag(
            "DI_01_ESTOP",
            description="E-stop contact from the input card.",
            is_alias=True,
            alias_for="DI_01:I.Data[0]",
            identifier=604,
        ),
    ]
    program_tags = [
        make_tag(
            "CV101_Start_PB",
            description="CV101 start pushbutton.",
            scope=TagScope.PROGRAM,
            scope_program_id=program.id,
            identifier=610,
        ),
        make_tag(
            "CV101_Run_Cmd",
            description="CV101 run command.",
            scope=TagScope.PROGRAM,
            scope_program_id=program.id,
            identifier=611,
        ),
    ]

    main = make_routine("MainRoutine", ordinal=0, is_main=True, identifier=200)
    main_rungs = [
        make_rung(
            0,
            [
                make_instruction(
                    0,
                    "JSR",
                    [
                        make_operand(0, "Conveyor_Control", kind=OperandKind.ROUTINE_REFERENCE),
                        make_operand(1, "0", kind=OperandKind.LITERAL),
                    ],
                    category=InstructionCategory.PROGRAM_CONTROL,
                    element_kind=LogicElementKind.OUTPUT,
                    identifier=800,
                )
            ],
            comment="Conveyor logic runs every scan.",
            identifier=700,
        )
    ]

    control = make_routine(
        "Conveyor_Control",
        ordinal=1,
        description="CV101 run logic.",
        template_id="esco.motor.standard",
        template_version="v1",
        identifier=201,
    )
    control_rungs = [
        make_rung(
            0,
            [
                make_instruction(0, "XIC", ["ESTOP_OK"], identifier=810),
                make_instruction(1, "XIC", ["LINE_PERMISSIVE"], identifier=811),
                make_instruction(
                    2, "XIC", ["CV101_Start_PB"], branch_level=1, branch_index=0, identifier=812
                ),
                make_instruction(
                    3, "XIC", ["CV101_Run_Cmd"], branch_level=1, branch_index=1, identifier=813
                ),
                make_instruction(
                    4,
                    "OTE",
                    ["CV101_Run_Cmd"],
                    element_kind=LogicElementKind.OUTPUT,
                    is_destructive=True,
                    identifier=814,
                ),
            ],
            comment="Seal-in with the master permissive.",
            branches=[
                make_branch(level=1, leg_index=0),
                make_branch(level=1, leg_index=1),
            ],
            identifier=701,
        ),
        make_rung(
            1,
            [
                make_instruction(
                    0,
                    "MotorStarter",
                    [
                        make_operand(0, "CV101_Starter"),
                        make_operand(1, "CV101_Run_Cmd"),
                    ],
                    category=InstructionCategory.ADDON_INSTRUCTION,
                    element_kind=LogicElementKind.OUTPUT,
                    identifier=815,
                )
            ],
            comment="Starter faceplate instance.",
            identifier=702,
        ),
    ]

    udt = make_udt()
    return ProjectModel(
        project_name="Conveyor Line 3",
        revision_number=7,
        controller=make_controller(description="Line 3 conveyors."),
        tasks=(
            TaskContent(
                task=make_task(name="MainTask", ordinal=0),
                scheduled_program_names=("MainProgram",),
            ),
        ),
        programs=(
            ProgramContent(
                program=program,
                tags=tuple(program_tags),
                routines=(
                    RoutineContent(routine=main, rungs=tuple(main_rungs)),
                    RoutineContent(routine=control, rungs=tuple(control_rungs)),
                ),
            ),
        ),
        controller_tags=tuple(controller_tags),
        udts=(
            UdtContent(
                udt=udt,
                members=(
                    make_udt_member("Running", 0, description="Drive is running."),
                    make_udt_member("Faulted", 1),
                    make_udt_member("SpeedFeedback", 2, base_data_type=BaseDataType.REAL),
                ),
            ),
        ),
        aoi_references=(make_aoi(),),
        modules=(make_module(),),
        logic_references=(
            make_reference("ESTOP_OK", routine_name="Conveyor_Control", rung_id=oid(701)),
            make_reference(
                "CV101_Run_Cmd",
                routine_name="Conveyor_Control",
                rung_id=oid(701),
                is_destructive=True,
                reference_kind="write",
            ),
        ),
    )


def build_options(**overrides: object) -> PackageOptions:
    base: dict[str, object] = {
        "target": TargetProfile(
            controller_family=ControllerFamily.CONTROLLOGIX,
            target_studio_version="32.03",
            l5x_schema_revision="1.0",
            software_revision="32.00",
            export_profile_key="full_controller",
            restricted_domain_modes=(("motion", "manual_review_only"),),
        )
    }
    base.update(overrides)
    return PackageOptions(**base)  # type: ignore[arg-type]


@pytest.fixture
def demo_model() -> ProjectModel:
    return build_demo_model()


@pytest.fixture
def demo_options() -> PackageOptions:
    return build_options()
