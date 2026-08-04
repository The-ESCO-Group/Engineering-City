"""Focused tests for importing parser facts into the canonical model."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.enums import (
    BaseDataType,
    DocumentRole,
    FailState,
    FileKind,
    FileStatus,
    InstructionCategory,
    LogicOrigin,
    SignalDirection,
    SignalType,
    TagScope,
)
from app.core.services.canonical_import import import_latest_parsed_artifacts
from app.models.file import ParsedArtifact, UploadedFile
from app.models.plant import AddressAssignment, Device, HardwareModule, IOPoint, Signal
from app.models.plc import (
    ControllerModel,
    ControllerTask,
    LogicInstruction,
    LogicOperand,
    LogicRung,
    Program,
    Routine,
)
from app.models.project import Project
from app.models.tags import AOIDefinitionReference, Tag, UDTDefinition, UDTMember


def _artifact(
    db: Session,
    project: Project,
    *,
    file_kind: FileKind,
    role: DocumentRole,
    content: dict[str, Any],
    name: str,
) -> ParsedArtifact:
    uploaded = UploadedFile(
        project_id=project.id,
        original_filename=name,
        stored_filename=name,
        relative_path=f"uploads/{name}",
        file_kind=file_kind,
        document_role=role,
        role_declared_by_user=True,
        status=FileStatus.PARSED,
    )
    db.add(uploaded)
    db.flush()
    artifact = ParsedArtifact(
        project_id=project.id,
        file_id=uploaded.id,
        artifact_kind=file_kind.value,
        parser_name=f"test-{file_kind.value}",
        parser_version="1",
        content=content,
    )
    db.add(artifact)
    db.flush()
    return artifact


def _ids(db: Session, model: type[Any], project_id: object) -> set[object]:
    return set(db.scalars(select(model.id).where(model.project_id == project_id)))


def _revision_map(
    db: Session,
    models: tuple[type[Any], ...],
    project_id: object,
) -> dict[tuple[str, object], int]:
    revisions: dict[tuple[str, object], int] = {}
    for model in models:
        rows: Any = db.scalars(select(model).where(model.project_id == project_id))
        for row in rows:
            revisions[(type(row).__name__, row.id)] = row.object_revision
    return revisions


def test_real_parser_artifacts_import_complete_source_model_idempotently(
    db: Session,
    ingested_project: Project,
) -> None:
    first = import_latest_parsed_artifacts(db, ingested_project.id)

    controller = db.scalar(
        select(ControllerModel).where(ControllerModel.project_id == ingested_project.id)
    )
    assert controller is not None
    assert controller.name == "Conveyor_Line3"
    assert controller.catalog_number == "1756-L83E"
    assert (controller.major_revision, controller.minor_revision) == (32, 11)
    # ExportDate is provenance, not the controller project's creation date.
    assert controller.project_creation_date == ""

    assert (
        db.scalar(
            select(ControllerTask).where(
                ControllerTask.project_id == ingested_project.id,
                ControllerTask.name == "MainTask",
            )
        )
        is not None
    )
    assert (
        db.scalar(
            select(Program).where(
                Program.project_id == ingested_project.id,
                Program.name == "MainProgram",
            )
        )
        is not None
    )
    assert len(list(db.scalars(select(Routine)))) == 5
    assert len(list(db.scalars(select(LogicRung)))) == 18
    assert len(list(db.scalars(select(LogicInstruction)))) == 45
    assert list(db.scalars(select(LogicOperand)))

    udt = db.scalar(select(UDTDefinition).where(UDTDefinition.name == "ConveyorStatus"))
    assert udt is not None
    assert len(list(db.scalars(select(UDTMember).where(UDTMember.udt_id == udt.id)))) == 4
    aoi = db.scalar(
        select(AOIDefinitionReference).where(AOIDefinitionReference.name == "MotorStarter")
    )
    assert aoi is not None and aoi.is_available

    modules = {row.name: row for row in db.scalars(select(HardwareModule))}
    assert modules["DI_01"].catalog_number == "1756-IB16"
    assert modules["DO_01"].catalog_number == "1756-OB16E"
    assert len(list(db.scalars(select(IOPoint)))) == 13
    # The fixture's ``Signal`` values are electrical formats (24VDC/4-20mA),
    # and it declares no device identity, so no physical device chain is guessed.
    assert not list(db.scalars(select(Device)))
    assert not list(db.scalars(select(Signal)))
    assert not list(db.scalars(select(AddressAssignment)))

    tags = list(db.scalars(select(Tag)))
    assert any(tag.name == "CV102_JAM" and tag.io_point_id is not None for tag in tags)
    assert all(not tag.is_generated for tag in tags)
    assert all(tag.origin is LogicOrigin.PARSED_FROM_SOURCE for tag in tags)

    unknown_aoi = db.scalar(
        select(LogicInstruction).where(LogicInstruction.mnemonic == "MOTORSTARTER")
    )
    assert unknown_aoi is not None
    assert unknown_aoi.category is InstructionCategory.UNSUPPORTED
    assert not unknown_aoi.is_supported
    assert "catalogue" in unknown_aoi.unsupported_reason

    source_models: tuple[type[Any], ...] = (
        ControllerModel,
        ControllerTask,
        Program,
        Routine,
        LogicRung,
        LogicInstruction,
        LogicOperand,
        HardwareModule,
        IOPoint,
        UDTDefinition,
        UDTMember,
        AOIDefinitionReference,
    )
    before_ids = {model.__name__: _ids(db, model, ingested_project.id) for model in source_models}
    before_revisions = _revision_map(db, source_models, ingested_project.id)

    second = import_latest_parsed_artifacts(db, ingested_project.id)

    assert first.created_count > 0
    assert second.created_count == 0
    assert second.updated_count == 0
    assert {
        model.__name__: _ids(db, model, ingested_project.id) for model in source_models
    } == before_ids
    assert _revision_map(db, source_models, ingested_project.id) == before_revisions


def test_explicit_io_columns_create_a_complete_physical_binding_without_safety_guesses(
    db: Session,
    project: Project,
) -> None:
    record = {
        "Device": "Pump 101",
        "Device Tag Prefix": "PMP_101",
        "Device Category": "pump",
        "Signal": "Run_Cmd",
        "Tag": "PMP_101_RUN_CMD",
        "Type": "DO",
        "Module": "DO_REMOTE",
        "Slot": "3",
        "Channel": "7",
        "Address": "DO_REMOTE:O.Data.7",
        "Description": "Pump 101 run command",
        "Engineering Units": "",
        "Fail State": "",
        "Safety Related": "",
        "Required": "true",
    }
    _artifact(
        db,
        project,
        file_kind=FileKind.CSV,
        role=DocumentRole.IO_LIST,
        content={
            "headers": list(record),
            "rows": [{"row": 2, "record": record}],
        },
        name="explicit_io.csv",
    )

    result = import_latest_parsed_artifacts(db, project.id)

    device = db.scalar(select(Device).where(Device.tag_prefix == "PMP_101"))
    signal = db.scalar(select(Signal).where(Signal.name == "Run_Cmd"))
    module = db.scalar(select(HardwareModule).where(HardwareModule.name == "DO_REMOTE"))
    point = db.scalar(select(IOPoint).where(IOPoint.address == "DO_REMOTE:O.Data.7"))
    tag = db.scalar(select(Tag).where(Tag.name == "PMP_101_RUN_CMD"))
    assignment = db.scalar(select(AddressAssignment))

    assert result.created_count == 6
    assert device is not None and signal is not None and module is not None
    assert point is not None and tag is not None and assignment is not None
    assert device.fail_state is FailState.UNDEFINED
    assert not device.is_safety_related
    assert signal.fail_state is FailState.UNDEFINED
    assert signal.signal_type is SignalType.DISCRETE_OUTPUT
    assert signal.direction is SignalDirection.OUTPUT
    assert point.hardware_module_id == module.id
    assert assignment.signal_id == signal.id and assignment.io_point_id == point.id
    assert tag.device_id == device.id
    assert tag.signal_id == signal.id
    assert tag.io_point_id == point.id
    assert tag.scope is TagScope.CONTROLLER
    assert tag.base_data_type is BaseDataType.BOOL
    assert not tag.is_generated
    assert all(
        row.origin is LogicOrigin.PARSED_FROM_SOURCE
        for row in (device, signal, module, point, tag, assignment)
    )


def test_source_import_does_not_overwrite_human_or_generated_key_owners(
    db: Session,
    project: Project,
) -> None:
    controller = ControllerModel(
        project_id=project.id,
        name="Human Controller",
        catalog_number="HUMAN-CATALOG",
        origin=LogicOrigin.HUMAN_AUTHORED,
    )
    program = Program(
        project_id=project.id,
        name="ProtectedProgram",
        description="generated description",
        ordinal=4,
        origin=LogicOrigin.TEMPLATE_EXPANSION,
    )
    tag = Tag(
        project_id=project.id,
        name="Protected_Tag",
        scope=TagScope.CONTROLLER,
        base_data_type=BaseDataType.DINT,
        description="human description",
        source_name="Protected_Tag",
        is_generated=False,
        origin=LogicOrigin.HUMAN_AUTHORED,
    )
    db.add_all([controller, program, tag])
    db.flush()
    _artifact(
        db,
        project,
        file_kind=FileKind.L5X,
        role=DocumentRole.CONTROL_LOGIC,
        name="protected.L5X",
        content={
            "controller": {
                "name": "Source Controller",
                "processor_type": "SOURCE-CATALOG",
            },
            "tasks": [
                {
                    "name": "SourceTask",
                    "type": "CONTINUOUS",
                    "scheduled_programs": ["ProtectedProgram"],
                }
            ],
            "programs": [
                {
                    "name": "ProtectedProgram",
                    "description": "source description",
                    "main_routine": "SourceRoutine",
                    "routines": [{"name": "SourceRoutine", "type": "RLL"}],
                }
            ],
            "tags": [
                {
                    "name": "Protected_Tag",
                    "scope": "controller",
                    "program": "",
                    "data_type": "BOOL",
                    "description": "source description",
                }
            ],
            "rungs": [],
        },
    )

    result = import_latest_parsed_artifacts(db, project.id)

    db.refresh(controller)
    db.refresh(program)
    db.refresh(tag)
    assert (controller.name, controller.catalog_number) == (
        "Human Controller",
        "HUMAN-CATALOG",
    )
    assert program.description == "generated description"
    assert program.ordinal == 4
    assert tag.description == "human description"
    assert tag.base_data_type is BaseDataType.DINT
    assert db.scalar(select(Routine).where(Routine.program_id == program.id)) is None
    assert sum(issue.code == "canonical_key_owned" for issue in result.issues) == 3


def test_unknown_and_restricted_source_instructions_are_explicitly_unsupported(
    db: Session,
    project: Project,
) -> None:
    _artifact(
        db,
        project,
        file_kind=FileKind.L5X,
        role=DocumentRole.CONTROL_LOGIC,
        name="instruction_boundaries.L5X",
        content={
            "controller": {"name": "InstructionBoundaries"},
            "tasks": [
                {
                    "name": "MainTask",
                    "type": "CONTINUOUS",
                    "scheduled_programs": ["MainProgram"],
                }
            ],
            "programs": [
                {
                    "name": "MainProgram",
                    "main_routine": "MainRoutine",
                    "routines": [{"name": "MainRoutine", "type": "RLL"}],
                }
            ],
            "tags": [
                {
                    "name": "SourceBit",
                    "scope": "controller",
                    "program": "",
                    "data_type": "BOOL",
                }
            ],
            "rungs": [
                {
                    "program": "MainProgram",
                    "routine": "MainRoutine",
                    "number": 0,
                    "text": "XIC(SourceBit)MSG(MessageCtl)MYSTERY(SourceBit);",
                    "instructions": [
                        {
                            "index": 0,
                            "mnemonic": "XIC",
                            "operands": ["SourceBit"],
                            "text": "XIC(SourceBit)",
                        },
                        {
                            "index": 1,
                            "mnemonic": "MSG",
                            "operands": ["MessageCtl"],
                            "text": "MSG(MessageCtl)",
                        },
                        {
                            "index": 2,
                            "mnemonic": "MYSTERY",
                            "operands": ["SourceBit"],
                            "text": "MYSTERY(SourceBit)",
                        },
                    ],
                }
            ],
        },
    )

    import_latest_parsed_artifacts(db, project.id)

    instructions = {row.mnemonic: row for row in db.scalars(select(LogicInstruction))}
    assert instructions["XIC"].is_supported
    assert instructions["XIC"].category is InstructionCategory.BIT
    assert not instructions["MSG"].is_supported
    assert instructions["MSG"].category is InstructionCategory.UNSUPPORTED
    assert "another device" in instructions["MSG"].unsupported_reason
    assert not instructions["MYSTERY"].is_supported
    assert instructions["MYSTERY"].category is InstructionCategory.UNSUPPORTED
    assert "catalogue" in instructions["MYSTERY"].unsupported_reason
