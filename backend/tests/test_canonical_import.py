"""Focused tests for importing parser facts into the canonical model."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.enums import (
    AlarmSeverity,
    BaseDataType,
    DocumentRole,
    FailState,
    FileKind,
    FileStatus,
    InstructionCategory,
    InterlockKind,
    LogicOrigin,
    ObjectApprovalState,
    SignalDirection,
    SignalType,
    TagScope,
    ValidationDiscipline,
    ValidationSeverity,
)
from app.core.services.canonical_import import import_latest_parsed_artifacts
from app.models.delivery import (
    AlarmDefinition,
    AlarmMessage,
    HMIBinding,
    HMIObjectInstance,
    HMIScreen,
)
from app.models.file import ParsedArtifact, UploadedFile
from app.models.interlocks import Interlock
from app.models.plant import AddressAssignment, Device, HardwareModule, IOPoint, Signal
from app.models.plc import (
    ControllerModel,
    ControllerTask,
    LogicInstruction,
    LogicNetwork,
    LogicOperand,
    LogicRung,
    Program,
    Routine,
)
from app.models.project import Project
from app.models.release import ValidationResult
from app.models.tags import AOIDefinitionReference, Tag, UDTDefinition, UDTMember
from app.validation import run_validation


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
    routines = {row.name: row for row in db.scalars(select(Routine))}
    assert len(routines) == 5
    assert len(list(db.scalars(select(LogicRung)))) == 18
    assert len(list(db.scalars(select(LogicInstruction)))) == 45
    assert list(db.scalars(select(LogicOperand)))

    # One network per routine the export gives rungs to, holding all of them.
    # The export states no grouping below the routine, and no template expanded
    # these rungs, so nothing claims one was chosen and no requirement is
    # claimed either: the interlock schedule this project also carries records
    # no implementing rung for anything.
    networks = {row.name: row for row in db.scalars(select(LogicNetwork))}
    assert set(networks) == set(routines)
    assert [networks[name].routine_id for name in networks] == [
        routines[name].id for name in networks
    ]
    assert {name: row.rung_count for name, row in networks.items()} == {
        name: row.rung_count for name, row in routines.items()
    }
    assert sum(row.rung_count for row in networks.values()) == 18
    assert all(row.ordinal == 0 for row in networks.values())
    assert networks["MainRoutine"].purpose == "Scheduler."
    assert all(not row.template_id and not row.template_version for row in networks.values())
    assert all(not row.requirement_reference for row in networks.values())
    assert all(row.origin is LogicOrigin.PARSED_FROM_SOURCE for row in networks.values())

    by_routine = {row.routine_id: row.id for row in networks.values()}
    assert all(
        rung.logic_network_id == by_routine[rung.routine_id]
        for rung in db.scalars(select(LogicRung))
    )

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
        LogicNetwork,
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


def test_device_tag_column_preserves_the_device_signal_and_assignment_model(
    db: Session,
    project: Project,
) -> None:
    """A common compact I/O schedule must not lose its physical model.

    ``Device Tag`` is an explicit device identity as well as the controller tag
    in this schedule shape.  Both rows deliberately claim the same terminal so
    the canonical model must retain two assignments; collapsing them would hide
    the exact conflict the I/O validator is meant to report.
    """
    rows = [
        {
            "Module": "DI_401",
            "Slot": "1",
            "Channel": "10",
            "Address": "Local:1:I.Data.10",
            "Signal Type": "DI 24VDC sinking",
            "Device Tag": "GD401_CLOSED",
            "Description": "Reject guard closed",
        },
        {
            "Module": "DI_401",
            "Slot": "1",
            "Channel": "10",
            "Address": "Local:1:I.Data.10",
            "Signal Type": "DI 24VDC sinking",
            "Device Tag": "MTR403_FAULT",
            "Description": "Conveyor 403 drive fault",
        },
    ]
    _artifact(
        db,
        project,
        file_kind=FileKind.CSV,
        role=DocumentRole.IO_LIST,
        content={
            "headers": list(rows[0]),
            "rows": [
                {"row": number, "record": record} for number, record in enumerate(rows, start=2)
            ],
        },
        name="compact_io.csv",
    )

    first = import_latest_parsed_artifacts(db, project.id)
    second = import_latest_parsed_artifacts(db, project.id)

    devices = list(db.scalars(select(Device).order_by(Device.tag_prefix)))
    signals = list(db.scalars(select(Signal).order_by(Signal.name)))
    points = list(db.scalars(select(IOPoint)))
    assignments = list(db.scalars(select(AddressAssignment)))
    tags = list(db.scalars(select(Tag).order_by(Tag.name)))

    assert [item.tag_prefix for item in devices] == ["GD401_CLOSED", "MTR403_FAULT"]
    assert [item.name for item in signals] == ["GD401_CLOSED", "MTR403_FAULT"]
    assert [item.name for item in tags] == ["GD401_CLOSED", "MTR403_FAULT"]
    assert len(points) == 1
    assert points[0].address == "Local:1:I.Data.10"
    assert len(assignments) == 2
    assert {item.io_point_id for item in assignments} == {points[0].id}

    # The second row claims a terminal the first row already established, and
    # the two describe it differently. Nothing prefers one over the other, so
    # the terminal keeps the first row's values and the clash is named. Letting
    # the later row win instead made every import rewrite the point twice, so a
    # re-import read afterwards as an edit to an approved terminal.
    assert points[0].description == "Reject guard closed"
    assert points[0].object_revision == 1
    assert [item.source_key for item in first.issues if item.code == "repeated_io_address"] == [
        "csv-row:3/point:Local:1:I.Data.10"
    ]
    assert second.created_count == 0
    assert second.updated_count == 0


def _alarm_logic_artifact(db: Session, project: Project, *, name: str) -> ParsedArtifact:
    """A controller that declares two alarm bits and drives exactly one of them."""
    return _artifact(
        db,
        project,
        file_kind=FileKind.L5X,
        role=DocumentRole.CONTROL_LOGIC,
        name=name,
        content={
            "controller": {"name": "AlarmRegisterHost"},
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
                    "main_routine": "Alarms",
                    "routines": [{"name": "Alarms", "type": "RLL"}],
                }
            ],
            "tags": [
                {"name": "ALM_DRIVEN", "scope": "controller", "program": "", "data_type": "BOOL"},
                {"name": "ALM_ORPHAN", "scope": "controller", "program": "", "data_type": "BOOL"},
                {"name": "SENSOR_OK", "scope": "controller", "program": "", "data_type": "BOOL"},
            ],
            "rungs": [
                {
                    "program": "MainProgram",
                    "routine": "Alarms",
                    "number": 0,
                    "text": "XIO(SENSOR_OK)OTE(ALM_DRIVEN);",
                    "instructions": [
                        {
                            "index": 0,
                            "mnemonic": "XIO",
                            "operands": ["SENSOR_OK"],
                            "text": "XIO(SENSOR_OK)",
                        },
                        {
                            "index": 1,
                            "mnemonic": "OTE",
                            "operands": ["ALM_DRIVEN"],
                            "text": "OTE(ALM_DRIVEN)",
                        },
                    ],
                }
            ],
        },
    )


def test_alarm_register_becomes_alarm_definitions_a_validator_can_compare(
    db: Session,
    project: Project,
) -> None:
    """An alarm nothing drives is only findable once the register is in the model.

    Both alarms below are configured, both name a tag the controller declares,
    and only one has a rung that sets it. The difference is invisible while the
    register sits in a parsed artifact, so this asserts the whole chain: the rows
    exist, ``is_driven`` distinguishes them, and the deterministic logic
    validator reaches the one that can never annunciate.
    """
    _alarm_logic_artifact(db, project, name="alarm_host.L5X")
    rows = [
        {
            "Alarm ID": "ALM-001",
            "Trigger Tag": "ALM_DRIVEN",
            "Message": "Sensor lost - line stopped",
            "Severity": "Critical",
            "Group": "Safety",
            "Latched": "Yes",
            "Ack Required": "Yes",
        },
        {
            "Alarm ID": "ALM-002",
            "Trigger Tag": "ALM_ORPHAN",
            "Message": "Stop gate failed to open",
            "Severity": "Medium",
            "Group": "Machine",
            "Latched": "No",
            "Ack Required": "No",
        },
    ]
    _artifact(
        db,
        project,
        file_kind=FileKind.CSV,
        role=DocumentRole.ALARM_LIST,
        name="Alarm_Register.csv",
        content={
            "headers": list(rows[0]),
            "column_names": list(rows[0]),
            "rows": [{"row": number, "record": record} for number, record in enumerate(rows, 3)],
        },
    )

    first = import_latest_parsed_artifacts(db, project.id)
    second = import_latest_parsed_artifacts(db, project.id)

    alarms = {
        row.reference: row
        for row in db.scalars(select(AlarmDefinition).order_by(AlarmDefinition.reference))
    }
    driven = alarms["ALM-001"]
    orphan = alarms["ALM-002"]
    tags = {row.name: row for row in db.scalars(select(Tag))}

    assert driven.severity is AlarmSeverity.URGENT
    assert driven.trigger_tag_id == tags["ALM_DRIVEN"].id
    assert (driven.is_resolved, driven.is_driven) == (True, True)
    assert driven.requires_acknowledge
    assert (driven.group_name, driven.name) == ("Safety", "Sensor lost - line stopped")
    assert orphan.severity is AlarmSeverity.MEDIUM
    assert orphan.trigger_tag_id == tags["ALM_ORPHAN"].id
    assert (orphan.is_resolved, orphan.is_driven) == (True, False)
    assert not orphan.requires_acknowledge
    assert all(row.origin is LogicOrigin.PARSED_FROM_SOURCE for row in alarms.values())

    # The register's ``Latched`` column is read and deliberately not applied: the
    # canonical latch state gates a release-blocking reset-behaviour rule and is a
    # statement about the controller, which a terminal's alarm register is not
    # evidence about. Applied here, it produced a blocker saying nothing clears an
    # alarm the same export unlatches on the next rung. Left unapplied, it is a
    # limitation a review can cite rather than a silent drop.
    assert (driven.latched, orphan.latched) == (None, None)
    unapplied = [item for item in first.issues if item.code == "unapplied_source_column"]
    assert [item.source_key for item in unapplied] == ["column:Latched"]
    assert "controller" in unapplied[0].message

    messages = {row.alarm_id: row for row in db.scalars(select(AlarmMessage))}
    assert messages[orphan.id].text == "Stop gate failed to open"
    # No operator response was stated, and none is invented: the alarm delivery
    # audit reports the gap rather than the importer closing it.
    assert messages[orphan.id].operator_guidance == ""

    assert first.created.get("AlarmDefinition") == 2
    assert first.created.get("AlarmMessage") == 2
    assert second.created_count == 0
    assert second.updated_count == 0

    run = run_validation(db, project=project, disciplines=[ValidationDiscipline.PLC_LOGIC])
    undriven = list(
        db.scalars(
            select(ValidationResult).where(
                ValidationResult.validation_run_id == run.id,
                ValidationResult.code == "logic.alarm_trigger_never_written",
            )
        )
    )

    assert [row.object_id for row in undriven] == [orphan.id]
    assert undriven[0].severity is ValidationSeverity.ERROR
    assert "ALM_ORPHAN" in undriven[0].message


def test_an_alarm_register_uploaded_first_still_resolves_against_the_io_list(
    db: Session,
    project: Project,
) -> None:
    """Import order follows what a document declares, not when it was uploaded.

    The register is created before the I/O list here, and the tag its trigger
    names is declared only by that I/O list. If the importer followed upload
    order the first pass would record an unresolved trigger and the second would
    quietly "update" the row — an edit with no author, on a source nobody
    changed, which is also how an idempotent import stops being idempotent.
    """
    alarm_record = {
        "Alarm ID": "ALM-020",
        "Trigger Tag": "GD401_CLOSED",
        "Message": "Guard door open",
        "Severity": "High",
    }
    _artifact(
        db,
        project,
        file_kind=FileKind.CSV,
        role=DocumentRole.ALARM_LIST,
        name="Alarm_Register.csv",
        content={
            "headers": list(alarm_record),
            "column_names": list(alarm_record),
            "rows": [{"row": 3, "record": alarm_record}],
        },
    )
    io_record = {
        "Module": "DI_401",
        "Slot": "1",
        "Channel": "10",
        "Address": "Local:1:I.Data.10",
        "Signal Type": "DI",
        "Device Tag": "GD401_CLOSED",
        "Description": "Divert-zone guard door closed",
    }
    _artifact(
        db,
        project,
        file_kind=FileKind.CSV,
        role=DocumentRole.IO_LIST,
        name="IO_List.csv",
        content={
            "headers": list(io_record),
            "column_names": list(io_record),
            "rows": [{"row": 3, "record": io_record}],
        },
    )

    first = import_latest_parsed_artifacts(db, project.id)
    second = import_latest_parsed_artifacts(db, project.id)

    alarm = db.scalar(select(AlarmDefinition))
    tag = db.scalar(select(Tag).where(Tag.name == "GD401_CLOSED"))
    assert alarm is not None and tag is not None
    assert (alarm.trigger_tag_id, alarm.is_resolved) == (tag.id, True)
    assert first.created_count > 0
    assert second.created_count == 0
    assert second.updated_count == 0


def test_an_unreadable_alarm_severity_is_reported_rather_than_guessed(
    db: Session,
    project: Project,
) -> None:
    """Severity states how fast an operator must respond, so it is never invented.

    A band nobody wrote down would read afterwards as an engineering judgement
    somebody made, so the row is left out and named instead.
    """
    record = {
        "Alarm ID": "ALM-010",
        "Trigger Tag": "ALM_SOMETHING",
        "Message": "Condition detected",
        "Severity": "P2",
    }
    _artifact(
        db,
        project,
        file_kind=FileKind.CSV,
        role=DocumentRole.ALARM_LIST,
        name="Alarm_Register.csv",
        content={
            "headers": list(record),
            "column_names": list(record),
            "rows": [{"row": 3, "record": record}],
        },
    )

    result = import_latest_parsed_artifacts(db, project.id)

    assert not list(db.scalars(select(AlarmDefinition)))
    reported = [item for item in result.issues if item.code == "unsupported_alarm_severity"]
    assert len(reported) == 1
    assert "'P2'" in reported[0].message
    assert reported[0].source_key == "csv-row:3/alarm:ALM-010"


def test_a_column_the_importer_cannot_place_is_reported_as_a_limitation(
    db: Session,
    project: Project,
) -> None:
    """A dropped column is how a whole document goes unread with nobody noticing.

    Both files below carry a column the canonical model has nowhere to put. The
    rows still import; what must not happen is that the unread column leaves no
    trace, because then a review can state it examined the schedule without
    anybody being able to see which part of it was never looked at.
    """
    io_record = {
        "Module": "DI_401",
        "Slot": "1",
        "Channel": "0",
        "Address": "Local:1:I.Data.0",
        "Signal Type": "DI",
        "Device Tag": "ESTOP_OK",
        "Description": "E-stop string healthy",
        "Range": "0 = tripped / 1 = healthy",
    }
    _artifact(
        db,
        project,
        file_kind=FileKind.CSV,
        role=DocumentRole.IO_LIST,
        name="IO_List.csv",
        content={
            "headers": list(io_record),
            "column_names": list(io_record),
            "rows": [{"row": 3, "record": io_record}],
        },
    )
    alarm_record = {
        "Alarm ID": "ALM-001",
        "Trigger Tag": "ALM_ESTOP",
        "Message": "E-stop actuated",
        "Severity": "High",
        "Associated Display": "Line4_Alarms",
        "Interlock ID": "ILK-401",
    }
    _artifact(
        db,
        project,
        file_kind=FileKind.CSV,
        role=DocumentRole.ALARM_LIST,
        name="Alarm_Register.csv",
        content={
            "headers": list(alarm_record),
            "column_names": list(alarm_record),
            "rows": [{"row": 3, "record": alarm_record}],
        },
    )

    result = import_latest_parsed_artifacts(db, project.id)

    unplaced = {
        item.source_key: item.message
        for item in result.issues
        if item.code == "unplaced_source_column"
    }
    assert set(unplaced) == {
        "column:Range",
        "column:Associated Display",
        "column:Interlock ID",
    }
    assert "I/O schedule" in unplaced["column:Range"]
    assert "alarm register" in unplaced["column:Interlock ID"]
    # The rows themselves still import; the limitation is about the column.
    assert db.scalar(select(IOPoint).where(IOPoint.address == "Local:1:I.Data.0")) is not None
    assert db.scalar(select(AlarmDefinition).where(AlarmDefinition.reference == "ALM-001"))


def test_alarm_columns_are_read_from_the_keys_the_parser_recorded(
    db: Session,
    project: Project,
) -> None:
    """A padded or repeated heading must not silently empty a whole column.

    The CSV parser keeps ``headers`` verbatim and derives the ``column_names``
    every record is actually keyed by. Reading the verbatim row would look up
    names no record holds, so every value would arrive blank while the file still
    looked like a clean parse.
    """
    _artifact(
        db,
        project,
        file_kind=FileKind.CSV,
        role=DocumentRole.ALARM_LIST,
        name="Alarm_Register.csv",
        content={
            "headers": ["  Alarm ID  ", " Severity", "Message "],
            "column_names": ["Alarm ID", "Severity", "Message"],
            "rows": [
                {
                    "row": 3,
                    "record": {
                        "Alarm ID": "ALM-001",
                        "Severity": "High",
                        "Message": "Drive fault",
                    },
                }
            ],
        },
    )

    import_latest_parsed_artifacts(db, project.id)

    alarm = db.scalar(select(AlarmDefinition))
    assert alarm is not None
    assert (alarm.reference, alarm.severity, alarm.name) == (
        "ALM-001",
        AlarmSeverity.HIGH,
        "Drive fault",
    )


def test_declared_hmi_xml_imports_screens_objects_and_truthful_bindings(
    db: Session,
    project: Project,
) -> None:
    known = Tag(
        project_id=project.id,
        name="LINE_RUNNING",
        scope=TagScope.CONTROLLER,
        base_data_type=BaseDataType.BOOL,
    )
    db.add(known)
    db.flush()
    elements = [
        {
            "tag": "Display",
            "xpath": "/Project/Displays/Display",
            "attributes": {
                "Name": "Line_Overview",
                "Number": "10",
                "Width": "800",
                "Height": "600",
                "Type": "Replace",
            },
        },
        {
            "tag": "Description",
            "xpath": "/Project/Displays/Display/Description",
            "attributes": {},
            "text": "Line overview",
        },
        {
            "tag": "MultiStateIndicator",
            "xpath": "/Project/Displays/Display/GraphicElements/MultiStateIndicator",
            "attributes": {"Name": "ind_Running", "Left": "20", "Top": "60"},
        },
        {
            "tag": "Connection",
            "xpath": ("/Project/Displays/Display/GraphicElements/MultiStateIndicator/Connection"),
            "attributes": {
                "Name": "Indicator",
                "Expression": "{::[Line]LINE_RUNNING}",
            },
        },
        {
            "tag": "MomentaryPushButton",
            "xpath": "/Project/Displays/Display/GraphicElements/MomentaryPushButton",
            "attributes": {"Name": "btn_Missing"},
        },
        {
            "tag": "Connection",
            "xpath": ("/Project/Displays/Display/GraphicElements/MomentaryPushButton/Connection"),
            "attributes": {
                "Name": "Value",
                "Expression": "{::[Line]MISSING_COMMAND}",
            },
        },
    ]
    _artifact(
        db,
        project,
        file_kind=FileKind.XML,
        role=DocumentRole.HMI_EXPORT,
        content={"elements": elements},
        name="HMI_Screens.xml",
    )

    first = import_latest_parsed_artifacts(db, project.id)
    second = import_latest_parsed_artifacts(db, project.id)

    screen = db.scalar(select(HMIScreen))
    objects = list(db.scalars(select(HMIObjectInstance).order_by(HMIObjectInstance.key)))
    bindings = {item.tag_name: item for item in db.scalars(select(HMIBinding))}
    assert screen is not None
    assert (screen.key, screen.width, screen.height) == ("Line_Overview", 800, 600)
    assert [item.key for item in objects] == ["btn_Missing", "ind_Running"]
    assert len(bindings) == 2
    missing = bindings["MISSING_COMMAND"]
    running = bindings["LINE_RUNNING"]
    assert (missing.tag_name, missing.access, missing.is_resolved) == (
        "MISSING_COMMAND",
        "write",
        False,
    )
    assert (running.tag_name, running.access, running.tag_id, running.is_resolved) == (
        "LINE_RUNNING",
        "read",
        known.id,
        True,
    )
    assert first.created_count == 5
    assert second.created_count == 0
    assert second.updated_count == 0


def test_imported_hmi_read_binding_to_missing_tag_reaches_persisted_validation(
    db: Session,
    project: Project,
) -> None:
    display_path = "/Project/Displays/Display"
    object_path = f"{display_path}/GraphicElements/NumericDisplay"
    _artifact(
        db,
        project,
        file_kind=FileKind.XML,
        role=DocumentRole.HMI_EXPORT,
        name="HMI_Screens.xml",
        content={
            "elements": [
                {
                    "tag": "Display",
                    "xpath": display_path,
                    "attributes": {"Name": "Line4_Overview", "Number": "10"},
                },
                {
                    "tag": "NumericDisplay",
                    "xpath": object_path,
                    "attributes": {"Name": "num_CasesAccumulated"},
                },
                {
                    "tag": "Connection",
                    "xpath": f"{object_path}/Connection",
                    "attributes": {
                        "Name": "Value",
                        "Expression": "{::[Line4]Accum_Case_Count}",
                    },
                },
            ]
        },
    )

    import_latest_parsed_artifacts(db, project.id)

    object_instance = db.scalar(select(HMIObjectInstance))
    binding = db.scalar(select(HMIBinding))
    assert object_instance is not None and object_instance.key == "num_CasesAccumulated"
    assert binding is not None
    assert (binding.tag_name, binding.access, binding.tag_id, binding.is_resolved) == (
        "Accum_Case_Count",
        "read",
        None,
        False,
    )

    run = run_validation(
        db,
        project=project,
        disciplines=[ValidationDiscipline.CROSS_DISCIPLINE],
    )
    result = db.scalar(
        select(ValidationResult).where(
            ValidationResult.validation_run_id == run.id,
            ValidationResult.code == "cross.hmi_read_binding_unresolved",
        )
    )

    assert result is not None
    assert result.severity is ValidationSeverity.WARNING
    assert result.object_id == binding.id
    assert "Line4_Overview" in result.message
    assert "Accum_Case_Count" in result.message


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


def _rung(routine: str, number: int, tag: str, coil: str) -> dict[str, Any]:
    return {
        "program": "Line4_Control",
        "routine": routine,
        "number": number,
        "text": f"XIC({tag})OTE({coil});",
        "instructions": [
            {"index": 0, "mnemonic": "XIC", "operands": [tag], "text": f"XIC({tag})"},
            {"index": 1, "mnemonic": "OTE", "operands": [coil], "text": f"OTE({coil})"},
        ],
    }


def _logic_artifact(db: Session, project: Project) -> ParsedArtifact:
    """Two routines the export gives rungs to, and one it declares and leaves empty."""
    tag_names = ("ESTOP_OK", "LINE_PERMIT", "DIV_EXTEND", "DIV_FAULT", "DIV_RETRACT")
    return _artifact(
        db,
        project,
        file_kind=FileKind.L5X,
        role=DocumentRole.CONTROL_LOGIC,
        name="Conveyor_Line4.L5X",
        content={
            "controller": {"name": "Line4"},
            "tasks": [
                {
                    "name": "MainTask",
                    "type": "CONTINUOUS",
                    "scheduled_programs": ["Line4_Control"],
                }
            ],
            "programs": [
                {
                    "name": "Line4_Control",
                    "main_routine": "Permissives",
                    "routines": [
                        {
                            "name": "Permissives",
                            "type": "RLL",
                            "description": "Start permissives for the line.",
                        },
                        {"name": "Diverter_Control", "type": "RLL"},
                        {"name": "Spare", "type": "RLL"},
                    ],
                }
            ],
            "tags": [
                {"name": name, "scope": "controller", "program": "", "data_type": "BOOL"}
                for name in tag_names
            ],
            "rungs": [
                _rung("Permissives", 0, "ESTOP_OK", "LINE_PERMIT"),
                _rung("Diverter_Control", 0, "DIV_EXTEND", "DIV_FAULT"),
                _rung("Diverter_Control", 1, "DIV_RETRACT", "DIV_FAULT"),
            ],
        },
    )


def _network(db: Session, name: str) -> LogicNetwork:
    network = db.scalar(select(LogicNetwork).where(LogicNetwork.name == name))
    assert network is not None
    return network


def _routine_rung(db: Session, routine: str, ordinal: int) -> LogicRung:
    rung = db.scalar(
        select(LogicRung)
        .join(Routine, Routine.id == LogicRung.routine_id)
        .where(Routine.name == routine, LogicRung.ordinal == ordinal)
    )
    assert rung is not None
    return rung


def test_an_imported_network_claims_only_a_requirement_the_model_already_traces(
    db: Session,
    project: Project,
) -> None:
    """The requirement a network carries is read off the model, never matched here.

    ``Interlock.implementing_rung_id`` is the record that a search located the
    logic implementing a documented protection, and the requirement it traces to
    is on the same row.  Where both are present the network holding that rung can
    say what it implements; where the search has not run — ``is_implemented``
    NULL — there is nothing to say, and inferring a claim from the rung id alone
    would put a conclusion nobody reached into the model.
    """
    _logic_artifact(db, project)
    import_latest_parsed_artifacts(db, project.id)

    # A routine the export declares and gives no rungs evidences no group.
    assert {row.name for row in db.scalars(select(LogicNetwork))} == {
        "Permissives",
        "Diverter_Control",
    }
    assert not any(row.requirement_reference for row in db.scalars(select(LogicNetwork)))

    db.add(
        Interlock(
            project_id=project.id,
            reference="ILK-401",
            name="Emergency stop drops the line permissive",
            kind=InterlockKind.TRIP,
            requirement_reference="REQ-ILK-401",
            is_implemented=True,
            implementing_rung_id=_routine_rung(db, "Permissives", 0).id,
        )
    )
    db.add(
        Interlock(
            project_id=project.id,
            reference="ILK-403",
            name="Diverter must confirm extension",
            kind=InterlockKind.INTERLOCK,
            requirement_reference="REQ-ILK-403",
            # No search has run, so the rung named beside it is a lead rather
            # than a located implementation.
            is_implemented=None,
            implementing_rung_id=_routine_rung(db, "Diverter_Control", 0).id,
        )
    )
    db.flush()

    import_latest_parsed_artifacts(db, project.id)

    assert len(list(db.scalars(select(LogicNetwork)))) == 2
    assert _network(db, "Permissives").requirement_reference == "REQ-ILK-401"
    assert _network(db, "Diverter_Control").requirement_reference == ""


def test_two_traced_requirements_in_one_routine_leave_the_network_unclaimed(
    db: Session,
    project: Project,
) -> None:
    """A network that is a whole routine cannot claim two requirements at once.

    The export states no boundary inside the routine, so nothing decides which of
    the two the group implements.  Recording the first, or the alphabetically
    smaller, would be this importer choosing — so it claims neither and says why.
    """
    _logic_artifact(db, project)
    import_latest_parsed_artifacts(db, project.id)

    for reference, ordinal in (("REQ-ILK-403", 0), ("REQ-ILK-405", 1)):
        db.add(
            Interlock(
                project_id=project.id,
                reference=reference.removeprefix("REQ-"),
                name=f"{reference} diverter protection",
                kind=InterlockKind.INTERLOCK,
                requirement_reference=reference,
                is_implemented=True,
                implementing_rung_id=_routine_rung(db, "Diverter_Control", ordinal).id,
            )
        )
    db.flush()

    result = import_latest_parsed_artifacts(db, project.id)

    assert _network(db, "Diverter_Control").requirement_reference == ""
    ambiguous = [item for item in result.issues if item.code == "ambiguous_network_requirement"]
    assert len(ambiguous) == 1
    assert ambiguous[0].source_key == "routine:Line4_Control/Diverter_Control"
    assert "REQ-ILK-403, REQ-ILK-405" in ambiguous[0].message


def test_an_approved_network_is_reported_rather_than_reclaimed(
    db: Session,
    project: Project,
) -> None:
    """Approving the group freezes it, including what it says it implements.

    The rungs stay inside it: the membership is one this importer recorded and
    still stands, and clearing it to avoid touching an approved row would delete
    the only link saying which network those rungs belong to.
    """
    _logic_artifact(db, project)
    import_latest_parsed_artifacts(db, project.id)

    network = _network(db, "Permissives")
    network.approval_state = ObjectApprovalState.APPROVED
    db.add(
        Interlock(
            project_id=project.id,
            reference="ILK-401",
            name="Emergency stop drops the line permissive",
            kind=InterlockKind.TRIP,
            requirement_reference="REQ-ILK-401",
            is_implemented=True,
            implementing_rung_id=_routine_rung(db, "Permissives", 0).id,
        )
    )
    db.flush()
    revision = network.object_revision

    result = import_latest_parsed_artifacts(db, project.id)

    db.refresh(network)
    assert network.requirement_reference == ""
    assert network.object_revision == revision
    assert _routine_rung(db, "Permissives", 0).logic_network_id == network.id
    owned = [
        item
        for item in result.issues
        if item.code == "canonical_key_owned"
        and item.source_key == "routine:Line4_Control/Permissives"
    ]
    assert len(owned) == 1
