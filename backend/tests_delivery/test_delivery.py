"""Delivery-agent and auditable report-provider tests."""

from __future__ import annotations

import io
import json
from collections.abc import Sequence
from datetime import UTC, datetime

import pytest
from docx import Document
from openpyxl import load_workbook
from pypdf import PdfReader
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.base.plugins import reload_plugins
from app.agents.delivery.report_delivery import EngineeringReportDeliveryAgent
from app.core.config import Settings
from app.core.enums import (
    AnchorType,
    EvidenceKind,
    FindingCategory,
    FindingSeverity,
    FindingStatus,
    TaskKind,
)
from app.models.evidence import EvidenceAnchor
from app.models.file import UploadedFile
from app.models.finding import Finding, FindingEvidence
from app.models.project import Project
from app.models.report import Report
from app.models.task import Task
from app.orchestration.handlers import TaskContext
from app.reporting.catalog import CATALOG_REPORT_PROVIDERS
from app.reporting.formats import REQUIRED_EXPORT_FORMATS, export_format_for
from app.reporting.service import generate_report
from app.reporting.snapshot import build_report_snapshot
from app.reporting.types import RenderedReport, ReportSnapshot

REQUIRED_REPORT_KEYS = {
    "executive_summary",
    "detailed_engineering_report",
    "requirements_traceability_matrix",
    "device_io_schedule",
    "tag_register",
    "plc_program_routine_index",
    "interlock_matrix",
    "hmi_screen_binding_register",
    "alarm_register",
    "findings_register",
    "iat_plan",
    "fat_plan",
    "manual_mode_test_plan",
    "automatic_mode_test_plan",
    "change_log",
    "approval_record",
    "validation_summary",
    "import_order_guide",
    "known_limitations",
}


def _finding(db: Session, project: Project, status: FindingStatus, key: str) -> Finding:
    row = Finding(
        project_id=project.id,
        title=f"Synthetic {key}",
        category=FindingCategory.INTERLOCK_DISCREPANCY,
        severity=FindingSeverity.HIGH,
        status=status,
        fact="The exported rung does not contain the documented permissive.",
        inference="The offline exports disagree and require engineering review.",
        suggested_review_action="Verify the permissive in an approved offline test.",
        limitations="No claim is made about the running controller.",
        confidence=0.86,
        requires_approval=True,
        is_reviewed=True,
        dedupe_key=key,
        decision_notes=(
            "Accepted for report generation." if status is FindingStatus.ACCEPTED else ""
        ),
        details={"interlock_id": "ILK-003", "comparison_result": "missing_in_logic"},
    )
    db.add(row)
    db.flush()
    return row


def _attach_citation(db: Session, project: Project, finding: Finding) -> EvidenceAnchor:
    uploaded = UploadedFile(
        project_id=project.id,
        original_filename="Conveyor_Line3.L5X",
        stored_filename="Conveyor_Line3.L5X",
        relative_path="originals/Conveyor_Line3.L5X",
        sha256="b" * 64,
    )
    db.add(uploaded)
    db.flush()
    anchor = EvidenceAnchor(
        project_id=project.id,
        file_id=uploaded.id,
        anchor_key="rung:MainProgram/Conveyor_Interlocks/0",
        anchor_type=AnchorType.L5X_RUNG,
        evidence_kind=EvidenceKind.DIRECT,
        source_label=uploaded.original_filename,
        display_location="MainProgram / Conveyor_Interlocks / Rung 0",
        excerpt="XIC ConveyorRunning OTE ConveyorEnable",
        content_hash="a" * 64,
        parser_version="test",
    )
    db.add(anchor)
    db.flush()
    db.add(
        FindingEvidence(
            finding_id=finding.id,
            anchor_id=anchor.id,
            evidence_kind=EvidenceKind.DIRECT,
            role="logic",
        )
    )
    db.flush()
    return anchor


def _context(
    db: Session,
    project: Project,
    settings: Settings,
    *,
    payload: dict[str, object] | None = None,
) -> TaskContext:
    task = Task(
        project_id=project.id,
        kind=TaskKind.DELIVERY,
        title="Generate accepted-finding deliverables",
        idempotency_key="delivery:test",
        payload=payload or {},
    )
    db.add(task)
    db.flush()
    return TaskContext(db=db, task=task, project=project, settings=settings)


def _snapshot(db: Session, project: Project) -> ReportSnapshot:
    return build_report_snapshot(
        db,
        project,
        generated_at=datetime(2026, 7, 31, 12, 30, tzinfo=UTC),
    )


def test_plugins_register_complete_report_and_export_capabilities() -> None:
    registry = reload_plugins()

    assert set(registry.delivery_agents) == {
        "alarm_engineering_delivery",
        "electrical_io_delivery",
        "engineering_report_delivery",
        "hmi_engineering_delivery",
        "testing_commissioning_delivery",
    }
    assert REQUIRED_REPORT_KEYS <= set(registry.report_providers)
    assert {"discrepancy_matrix", "test_plan"} <= set(registry.report_providers)
    assert set(REQUIRED_EXPORT_FORMATS) <= set(registry.export_formats)
    assert "markdown" in registry.export_formats
    assert registry.errors == []


def test_snapshot_separates_accepted_delivery_findings_from_full_register(
    db: Session, project: Project
) -> None:
    accepted = _finding(db, project, FindingStatus.ACCEPTED, "accepted")
    rejected = _finding(db, project, FindingStatus.REJECTED, "rejected")
    anchor = _attach_citation(db, project, accepted)

    snapshot = _snapshot(db, project)

    assert snapshot.finding_ids == (accepted.id,)
    assert rejected.id not in snapshot.finding_ids
    assert snapshot.findings[0].fact != snapshot.findings[0].inference
    assert snapshot.findings[0].citations[0].display_location == anchor.display_location
    findings_register = snapshot.dataset("findings")
    assert findings_register is not None
    assert len(findings_register.rows) == 2
    assert snapshot.source_digest and len(snapshot.source_digest) == 64


def test_source_digest_tracks_canonical_data_not_render_timestamp(
    db: Session, project: Project
) -> None:
    first = build_report_snapshot(db, project, generated_at=datetime(2026, 7, 31, 8, tzinfo=UTC))
    second = build_report_snapshot(db, project, generated_at=datetime(2026, 7, 31, 9, tzinfo=UTC))
    assert first.source_digest == second.source_digest

    _finding(db, project, FindingStatus.ACCEPTED, "changes-source")
    changed = build_report_snapshot(db, project, generated_at=datetime(2026, 7, 31, 9, tzinfo=UTC))
    assert changed.source_digest != first.source_digest


@pytest.mark.parametrize("export_format", REQUIRED_EXPORT_FORMATS)
def test_required_export_formats_are_real_deterministic_artifacts(
    db: Session, project: Project, export_format: str
) -> None:
    accepted = _finding(db, project, FindingStatus.ACCEPTED, f"format-{export_format}")
    _attach_citation(db, project, accepted)
    snapshot = _snapshot(db, project)
    provider = next(
        item for item in CATALOG_REPORT_PROVIDERS if item.report_type == "executive_summary"
    )

    first = provider.render(snapshot, export_format)
    second = provider.render(snapshot, export_format)

    assert first.data == second.data
    assert first.filename.endswith(export_format_for(export_format).file_extension)
    assert first.media_type == export_format_for(export_format).media_type
    assert first.summary["document_status"].startswith("DRAFT")
    assert first.summary["human_approved"] is False
    assert first.summary["validation_level"] == "generated"
    assert first.summary["source_snapshot_sha256"] == snapshot.source_digest

    if export_format == "html":
        text = first.data.decode("utf-8")
        assert "<!doctype html>" in text and "DRAFT - GENERATED" in text
        assert "Source snapshot SHA-256" in text
    elif export_format == "pdf":
        reader = PdfReader(io.BytesIO(first.data))
        assert reader.pages
        extracted = "\n".join(page.extract_text() or "" for page in reader.pages)
        assert "DRAFT - GENERATED" in extracted
        assert "Source snapshot SHA-256" in extracted
    elif export_format == "docx":
        document = Document(io.BytesIO(first.data))
        text = "\n".join(paragraph.text for paragraph in document.paragraphs)
        table_text = "\n".join(
            cell.text for table in document.tables for row in table.rows for cell in row.cells
        )
        assert "DRAFT - GENERATED" in table_text
        assert "Artifact metadata" in text
        assert document.core_properties.category.startswith("DRAFT")
    elif export_format == "xlsx":
        workbook = load_workbook(io.BytesIO(first.data), read_only=False, data_only=False)
        assert "Report Overview" in workbook.sheetnames
        assert workbook["Report Overview"]["B2"].value.startswith("DRAFT")
        assert workbook.properties.category.startswith("DRAFT")
    elif export_format == "csv":
        text = first.data.decode("utf-8-sig")
        assert "record_type,section,table,field,value,values_json" in text
        assert "DRAFT - GENERATED" in text
    elif export_format == "json":
        payload = json.loads(first.data)
        assert payload["report"]["document_status"].startswith("DRAFT")
        assert payload["report"]["source_snapshot_sha256"] == snapshot.source_digest


def test_every_required_report_contains_complete_provenance_envelope(
    db: Session, project: Project
) -> None:
    snapshot = _snapshot(db, project)
    providers = {item.report_type: item for item in CATALOG_REPORT_PROVIDERS}

    for report_type in REQUIRED_REPORT_KEYS:
        payload = json.loads(providers[report_type].render(snapshot, "json").data)
        headings = {section["heading"] for section in payload["sections"]}
        assert {
            "Document control",
            "Target profile",
            "Files reviewed and unsupported",
            "Templates and generators",
            "Findings and evidence references",
            "Human approvals and validation",
            "Known limitations and verification disclaimer",
        } <= headings
        assert payload["report"]["document_status"].startswith("DRAFT")
        assert payload["report"]["validation_level"] == "generated"


def test_delivery_generates_requested_files_and_resumes_without_duplicates(
    db: Session, project: Project, settings: Settings
) -> None:
    accepted = _finding(db, project, FindingStatus.ACCEPTED, "accepted-report")
    _finding(db, project, FindingStatus.REJECTED, "rejected-report")
    anchor = _attach_citation(db, project, accepted)
    context = _context(
        db,
        project,
        settings,
        payload={
            "report_types": ["discrepancy_matrix", "test_plan"],
            "report_formats": ["html", "json"],
        },
    )
    reload_plugins()

    first = EngineeringReportDeliveryAgent().run(context)
    db.flush()
    reports = list(db.execute(select(Report).order_by(Report.report_type)).scalars())

    assert len(first.generated) == 4
    assert len(reports) == 4
    assert context.done() == {
        "report:discrepancy_matrix:html",
        "report:discrepancy_matrix:json",
        "report:test_plan:html",
        "report:test_plan:json",
    }
    assert all(report.finding_ids == [str(accepted.id)] for report in reports)
    assert all(report.summary["document_status"].startswith("DRAFT") for report in reports)
    assert all(report.summary["human_approved"] is False for report in reports)
    assert all(report.summary["source_snapshot_sha256"] for report in reports)
    for report in reports:
        artifact = settings.project_dir(project.id).joinpath(report.relative_path)
        assert artifact.is_file()
        assert artifact.stat().st_size == report.size_bytes

    html_report = next(
        report
        for report in reports
        if report.report_type == "discrepancy_matrix" and report.export_format == "html"
    )
    content = (
        settings.project_dir(project.id).joinpath(html_report.relative_path).read_text("utf-8")
    )
    assert "Fact" in content and "Inference" in content and "Limitations" in content
    assert anchor.display_location in content

    second = EngineeringReportDeliveryAgent().run(context)
    db.flush()
    assert len(second.skipped) == 4
    assert len(list(db.execute(select(Report)).scalars())) == 4


def test_unavailable_requested_capabilities_are_warnings_not_fake_files(
    db: Session, project: Project, settings: Settings
) -> None:
    context = _context(
        db,
        project,
        settings,
        payload={
            "report_types": ["not_installed"],
            "report_formats": ["pdf"],
        },
    )
    reload_plugins()

    result = EngineeringReportDeliveryAgent().run(context)

    assert result.generated == ()
    assert any("unavailable" in warning for warning in result.warnings)
    assert any("No supported report output" in warning for warning in result.warnings)
    assert list(db.execute(select(Report)).scalars()) == []


def test_report_service_rejects_unsafe_provider_output_before_writing(
    db: Session, project: Project, settings: Settings
) -> None:
    context = _context(db, project, settings)
    snapshot = _snapshot(db, project)

    class UnsafeProvider:
        report_type = "unsafe"
        display_name = "Unsafe"
        version = "1"
        supported_formats: Sequence[str] = ("html",)

        def render(self, snapshot: object, export_format: str) -> RenderedReport:
            return RenderedReport(
                title="Unsafe",
                filename="../unsafe.html",
                media_type="text/html; charset=utf-8",
                data=b"not written",
                finding_ids=(),
            )

    with pytest.raises(ValueError, match="path instead of a filename"):
        generate_report(context, UnsafeProvider(), snapshot, "html")

    report_dir = settings.project_dir(project.id) / "reports"
    assert not report_dir.exists() or list(report_dir.iterdir()) == []
