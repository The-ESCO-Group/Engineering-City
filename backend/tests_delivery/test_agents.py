"""Milestone 5 cross-discipline delivery plugin tests."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agents.base.plugins import reload_plugins
from app.agents.delivery.alarm_engineering import AlarmEngineeringAgent
from app.agents.delivery.electrical_io import ElectricalIOAgent
from app.agents.delivery.generators import plan_alarm_candidates, plan_hmi_candidates
from app.agents.delivery.hmi_engineering import HMIEngineeringAgent
from app.agents.delivery.testing_commissioning import TestingCommissioningAgent
from app.core.config import Settings
from app.core.enums import (
    AlarmSeverity,
    AnchorType,
    EvidenceKind,
    HmiProduct,
    ObjectType,
    RequirementStatus,
    SignalDirection,
    SignalType,
    TaskKind,
)
from app.models.core import ObjectEvidenceLink
from app.models.delivery import AlarmDefinition, HMIBinding, HMIScreen
from app.models.evidence import EvidenceAnchor
from app.models.file import UploadedFile
from app.models.plant import AddressAssignment, Device, IOPoint, Signal
from app.models.profile import TargetPlatform
from app.models.project import Project
from app.models.requirements import Requirement
from app.models.task import Task
from app.orchestration.handlers import TaskContext


def _context(db: Session, project: Project, settings: Settings, *, key: str) -> TaskContext:
    task = Task(
        project_id=project.id,
        kind=TaskKind.DELIVERY,
        title=f"Run {key} delivery audit",
        idempotency_key=f"delivery:{key}",
    )
    db.add(task)
    db.flush()
    return TaskContext(db=db, task=task, project=project, settings=settings)


def _issue_codes(result: object) -> set[str]:
    return {item.code for item in result.issues}  # type: ignore[attr-defined]


def test_registry_installs_exactly_five_cross_discipline_agents() -> None:
    registry = reload_plugins()

    assert set(registry.delivery_agents) == {
        "alarm_engineering_delivery",
        "electrical_io_delivery",
        "engineering_report_delivery",
        "hmi_engineering_delivery",
        "testing_commissioning_delivery",
    }
    assert all(
        TaskKind.DELIVERY.value in plugin.handles_task_kinds
        for plugin in registry.delivery_agents.values()
    )
    assert registry.errors == []


def test_electrical_agent_reports_conflicts_and_preserves_evidence(
    db: Session, project: Project, settings: Settings
) -> None:
    device = Device(project_id=project.id, tag_prefix="MTR_101", name="Conveyor motor")
    db.add(device)
    db.flush()
    first = Signal(
        project_id=project.id,
        device_id=device.id,
        name="Run_Cmd",
        signal_type=SignalType.DISCRETE_OUTPUT,
        direction=SignalDirection.OUTPUT,
        description="Run command",
        ordinal=1,
    )
    second = Signal(
        project_id=project.id,
        device_id=device.id,
        name="Reset_Cmd",
        signal_type=SignalType.DISCRETE_OUTPUT,
        direction=SignalDirection.OUTPUT,
        description="Reset command",
        ordinal=2,
    )
    point = IOPoint(
        project_id=project.id,
        address="Local:2:O.Data.0",
        signal_type=SignalType.DISCRETE_OUTPUT,
        direction=SignalDirection.OUTPUT,
        description="Motor output",
    )
    db.add_all([first, second, point])
    db.flush()
    db.add_all(
        [
            AddressAssignment(project_id=project.id, signal_id=first.id, io_point_id=point.id),
            AddressAssignment(project_id=project.id, signal_id=second.id, io_point_id=point.id),
        ]
    )
    uploaded = UploadedFile(
        project_id=project.id,
        original_filename="io_schedule.xlsx",
        stored_filename="io_schedule.xlsx",
        relative_path="originals/io_schedule.xlsx",
        sha256="b" * 64,
    )
    db.add(uploaded)
    db.flush()
    anchor = EvidenceAnchor(
        project_id=project.id,
        file_id=uploaded.id,
        anchor_key="xlsx:IO/2/Address",
        anchor_type=AnchorType.XLSX_CELL,
        source_label="io_schedule.xlsx",
        content_hash="a" * 64,
    )
    db.add(anchor)
    db.flush()
    db.add(
        ObjectEvidenceLink(
            project_id=project.id,
            object_type=ObjectType.IO_POINT,
            object_id=point.id,
            anchor_id=anchor.id,
            basis_kind=EvidenceKind.DIRECT,
            note="Address from I/O schedule",
        )
    )
    db.flush()

    before_assignments = db.scalar(select(func.count()).select_from(AddressAssignment))
    result = ElectricalIOAgent().run(_context(db, project, settings, key="electrical"))

    assert "electrical.address_conflict" in _issue_codes(result)
    conflict = next(item for item in result.issues if item.code == "electrical.address_conflict")
    assert conflict.evidence_anchor_ids == (anchor.id,)
    assert conflict.requires_human_review is True
    assert db.scalar(select(func.count()).select_from(AddressAssignment)) == before_assignments
    assert result.summary()["canonical_writes"] == 0


def test_hmi_agent_reports_missing_binding_without_inventing_tag(
    db: Session, project: Project, settings: Settings
) -> None:
    screen = HMIScreen(
        project_id=project.id,
        key="L4_OVERVIEW",
        name="Line 4 Overview",
        title="Line 4 Overview",
        screen_type="overview",
        ordinal=1,
    )
    db.add(screen)
    db.flush()
    binding = HMIBinding(
        project_id=project.id,
        screen_id=screen.id,
        property_name="Value",
        tag_name="MTR_404_Missing",
        is_resolved=False,
        unresolved_reason="No canonical tag of this name",
    )
    db.add(binding)
    db.flush()

    result = HMIEngineeringAgent().run(_context(db, project, settings, key="hmi"))

    assert "hmi.missing_binding" in _issue_codes(result)
    assert binding.tag_id is None
    assert binding.is_resolved is False
    assert result.summary()["mode"] == "read_only_canonical_audit"


def test_alarm_agent_reports_missing_trigger_and_operator_response(
    db: Session, project: Project, settings: Settings
) -> None:
    alarm = AlarmDefinition(
        project_id=project.id,
        reference="ALM-101",
        name="Motor overload",
        severity=AlarmSeverity.HIGH,
        trigger_tag_name="",
        ordinal=1,
    )
    db.add(alarm)
    db.flush()

    result = AlarmEngineeringAgent().run(_context(db, project, settings, key="alarm"))

    assert {
        "alarm.missing_trigger",
        "alarm.message_missing",
        "alarm.operator_response_missing",
    } <= _issue_codes(result)
    missing_trigger = next(item for item in result.issues if item.code == "alarm.missing_trigger")
    assert missing_trigger.requires_human_review is True
    assert alarm.trigger_tag_id is None


def test_testing_agent_reports_requirement_coverage_gap_without_writing_test(
    db: Session, project: Project, settings: Settings
) -> None:
    requirement = Requirement(
        project_id=project.id,
        reference="REQ-014",
        title="Guard interlock",
        statement="Opening the guard shall inhibit motion.",
        source_text="Opening the guard shall inhibit motion.",
        status=RequirementStatus.ACCEPTED,
    )
    db.add(requirement)
    db.flush()

    result = TestingCommissioningAgent().run(_context(db, project, settings, key="testing"))

    assert "testing.requirement_uncovered" in _issue_codes(result)
    assert result.counts["test_cases"] == 0
    assert any("not inferred" in warning for warning in result.warnings)


def test_read_only_agents_do_not_leave_canonical_mutations(
    db: Session, project: Project, settings: Settings
) -> None:
    context = _context(db, project, settings, key="read-only")
    db.flush()

    for agent in (
        ElectricalIOAgent(),
        HMIEngineeringAgent(),
        AlarmEngineeringAgent(),
        TestingCommissioningAgent(),
    ):
        result = agent.run(context)
        assert result.summary()["canonical_writes"] == 0
        assert any("No canonical object" in warning for warning in result.warnings)
        assert not db.new
        assert not db.dirty


def test_delivery_generators_fail_closed_for_unsupported_or_missing_targets(
    db: Session, project: Project, settings: Settings
) -> None:
    context = _context(db, project, settings, key="unsupported-generators")

    no_target = plan_alarm_candidates(context)
    assert no_target.capability.available is False
    assert no_target.candidates == ()
    assert "No candidate artifact" in no_target.warnings[0]

    db.add(
        TargetPlatform(
            project_id=project.id,
            hmi_product=HmiProduct.FACTORYTALK_VIEW_SE,
            hmi_version="15.00",
        )
    )
    db.flush()
    unsupported = plan_hmi_candidates(context)
    assert unsupported.capability.available is False
    assert unsupported.candidates == ()
    assert "No target-specific HMI generator" in unsupported.capability.reason


def test_factorytalk_me_candidates_are_deterministic_and_honest(
    db: Session, project: Project, settings: Settings
) -> None:
    db.add(
        TargetPlatform(
            project_id=project.id,
            hmi_product=HmiProduct.FACTORYTALK_VIEW_ME,
            hmi_version="14.00",
        )
    )
    screen = HMIScreen(
        project_id=project.id,
        key="L4_OVERVIEW",
        name="Line 4 Overview",
        title="Line 4 Overview",
        screen_type="overview",
        template_id="esco.overview",
        template_version="1.0.0",
        ordinal=1,
    )
    alarm = AlarmDefinition(
        project_id=project.id,
        reference="ALM-201",
        name="Jam detected",
        severity=AlarmSeverity.HIGH,
        trigger_tag_name="Jam_Active",
        is_resolved=False,
        is_driven=False,
        ordinal=1,
    )
    db.add_all([screen, alarm])
    db.flush()
    db.add(
        HMIBinding(
            project_id=project.id,
            screen_id=screen.id,
            property_name="Value",
            tag_name="Jam_Active",
            is_resolved=False,
            unresolved_reason="No approved canonical tag",
        )
    )
    db.flush()
    context = _context(db, project, settings, key="factorytalk-me-generators")

    first_hmi = plan_hmi_candidates(context)
    second_hmi = plan_hmi_candidates(context)
    alarm_plan = plan_alarm_candidates(context)

    assert first_hmi.capability.available is True
    assert first_hmi.candidates[0].sha256 == second_hmi.candidates[0].sha256
    assert first_hmi.candidates[0].document_status.startswith("DRAFT - GENERATED")
    assert first_hmi.candidates[0].import_verified is False
    assert dict(first_hmi.capability.unavailable_artifacts)["display_xml_candidate"]
    assert any("Unregistered templates" in item for item in first_hmi.candidates[0].limitations)
    assert alarm_plan.capability.available is True
    assert {item.kind for item in alarm_plan.candidates} == {
        "alarm_import_csv_candidate",
        "alarm_cross_reference_csv",
    }
    assert all(item.import_verified is False for item in alarm_plan.candidates)
    assert all(item.summary()["canonical_writes"] == 0 for item in alarm_plan.candidates)
