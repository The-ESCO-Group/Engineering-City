"""Complete documentation-agent report catalog."""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.reporting.document import build_report_document
from app.reporting.formats import REQUIRED_EXPORT_FORMATS, export_format_for
from app.reporting.types import RenderedReport, ReportSection, ReportSnapshot, ReportTable


def safe_stem(value: str) -> str:
    """Return a stable, path-safe report filename stem."""

    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip("-.").lower()
    return stem or "project"


@dataclass(frozen=True, slots=True)
class ReportSpecification:
    key: str
    display_name: str
    purpose: str
    dataset_keys: tuple[str, ...]
    default_formats: tuple[str, ...]
    include_in_default_delivery: bool = False
    version: str = "1.0.0"


REPORT_SPECIFICATIONS = (
    ReportSpecification(
        "executive_summary",
        "Executive Summary",
        "Management-level status, scope, material findings, approvals, and validation facts.",
        ("project_summary", "findings", "approvals", "validation", "report_definitions"),
        ("html", "pdf"),
        True,
    ),
    ReportSpecification(
        "detailed_engineering_report",
        "Detailed Engineering Report",
        "Cross-discipline canonical engineering model and review record.",
        (
            "project_summary",
            "requirements",
            "devices",
            "io_points",
            "tags",
            "plc_index",
            "interlocks",
            "hmi_screens",
            "hmi_bindings",
            "alarms",
            "findings",
            "tests",
            "validation",
        ),
        ("pdf", "docx"),
        True,
    ),
    ReportSpecification(
        "requirements_traceability_matrix",
        "Requirements Traceability Matrix",
        "Requirements, source wording, status, implementation coverage, and verification method.",
        ("requirements",),
        ("xlsx", "csv"),
    ),
    ReportSpecification(
        "device_io_schedule",
        "Device and I/O Schedule",
        "Canonical devices, fail states, physical addresses, types, ranges, and spare status.",
        ("devices", "io_points"),
        ("xlsx", "csv"),
    ),
    ReportSpecification(
        "tag_register",
        "Tag Register",
        "Canonical tag names, scopes, data types, roles, aliases, and physical bindings.",
        ("tags",),
        ("xlsx", "csv"),
    ),
    ReportSpecification(
        "plc_program_routine_index",
        "PLC Program and Routine Index",
        "Controller task, program, routine, language, reachability, and template provenance.",
        ("plc_index",),
        ("xlsx", "html"),
    ),
    ReportSpecification(
        "interlock_matrix",
        "Interlock Matrix",
        "Documented trip conditions compared with located canonical implementation evidence.",
        ("interlocks",),
        ("xlsx", "pdf"),
    ),
    ReportSpecification(
        "hmi_screen_binding_register",
        "HMI Screen and Binding Register",
        "Template-based HMI screens and bindings, including unresolved references.",
        ("hmi_screens", "hmi_bindings"),
        ("xlsx", "csv"),
    ),
    ReportSpecification(
        "alarm_register",
        "Alarm Register",
        "Alarm triggers, priorities, delays, acknowledgement behavior, messages, and guidance.",
        ("alarms",),
        ("xlsx", "csv"),
    ),
    ReportSpecification(
        "findings_register",
        "Findings Register",
        "All finding states with facts, inferences, evidence posture, and human disposition.",
        ("findings",),
        ("xlsx", "html"),
        True,
    ),
    ReportSpecification(
        "iat_plan",
        "IAT Plan",
        "Recorded integration acceptance tests; missing steps remain explicit.",
        ("iat_tests",),
        ("xlsx", "pdf"),
    ),
    ReportSpecification(
        "fat_plan",
        "FAT Plan",
        "Recorded factory acceptance tests; no physical behavior is inferred.",
        ("fat_tests",),
        ("xlsx", "pdf"),
    ),
    ReportSpecification(
        "manual_mode_test_plan",
        "Manual Mode Test Plan",
        "Recorded manual-mode procedures, expected results, safety notes, and traceability.",
        ("manual_tests",),
        ("xlsx", "pdf"),
    ),
    ReportSpecification(
        "automatic_mode_test_plan",
        "Automatic Mode Test Plan",
        "Recorded automatic-mode procedures, expected results, safety notes, and traceability.",
        ("automatic_tests",),
        ("xlsx", "pdf"),
    ),
    ReportSpecification(
        "change_log",
        "Change Log",
        "Revision-scoped structured change-set history and human disposition.",
        ("change_log",),
        ("html", "csv", "json"),
    ),
    ReportSpecification(
        "approval_record",
        "Approval Record",
        "Named human requests and decisions pinned to their affected revisions.",
        ("approvals",),
        ("pdf", "docx", "json"),
    ),
    ReportSpecification(
        "validation_summary",
        "Validation Summary",
        "Latest deterministic validation result set, exact scope, waivers, and required actions.",
        ("validation",),
        ("html", "json"),
        True,
    ),
    ReportSpecification(
        "import_order_guide",
        "Import Order Guide",
        "Recorded release artifact order and validation level; never a fabricated import sequence.",
        ("import_order",),
        ("pdf", "docx", "json"),
    ),
    ReportSpecification(
        "known_limitations",
        "Known Limitations",
        "Unsupported inputs, target limitations, verification boundaries, and unresolved scope.",
        ("report_definitions", "validation"),
        ("html", "docx", "json"),
        True,
    ),
)


class CatalogReportProvider:
    """Render one specification from the immutable canonical snapshot."""

    supported_formats = REQUIRED_EXPORT_FORMATS

    def __init__(self, specification: ReportSpecification) -> None:
        self.specification = specification
        self.report_type = specification.key
        self.display_name = specification.display_name
        self.version = specification.version
        self.default_formats = specification.default_formats
        self.include_in_default_delivery = specification.include_in_default_delivery

    def render(self, snapshot: ReportSnapshot, export_format: str) -> RenderedReport:
        if export_format not in self.supported_formats:
            raise ValueError(
                f"{self.report_type} does not support {export_format!r}; "
                f"supported formats: {', '.join(self.supported_formats)}"
            )
        formatter = export_format_for(export_format)
        content_sections: list[ReportSection] = [
            ReportSection(
                heading="Purpose and scope",
                paragraphs=(
                    self.specification.purpose,
                    "The tables below reproduce canonical records from the captured revision. "
                    "Blank or absent records are stated rather than completed with generated "
                    "engineering assumptions.",
                ),
            )
        ]
        counts: dict[str, int] = {}
        for key in self.specification.dataset_keys:
            dataset = snapshot.dataset(key)
            if dataset is None:
                counts[key] = 0
                content_sections.append(
                    ReportSection(
                        heading=key.replace("_", " ").title(),
                        paragraphs=(
                            "The reporting snapshot does not contain this canonical dataset. "
                            "Capability is unavailable; no substitute data was generated.",
                        ),
                    )
                )
                continue
            counts[key] = len(dataset.rows)
            content_sections.append(
                ReportSection(
                    heading=dataset.title,
                    tables=(
                        ReportTable(
                            heading=dataset.title,
                            columns=dataset.columns,
                            rows=dataset.rows,
                            empty_message=dataset.empty_message,
                        ),
                    ),
                )
            )
        document = build_report_document(
            snapshot,
            report_type=self.report_type,
            report_version=self.version,
            title=f"{snapshot.project_name} - {self.display_name}",
            notice=(
                "Draft engineering artifact. It is generated from canonical project records and "
                "does not claim report approval, Rockwell import verification, or release."
            ),
            content_sections=tuple(content_sections),
        )
        data = formatter.serialize(document)
        filename = (
            f"{safe_stem(snapshot.project_name)}-{self.report_type}-r{snapshot.revision_number}"
            f"{formatter.file_extension}"
        )
        return RenderedReport(
            title=document.title,
            filename=filename,
            media_type=formatter.media_type,
            data=data,
            finding_ids=snapshot.finding_ids,
            summary={
                "document_status": document.document_status,
                "human_approved": False,
                "validation_level": document.validation_level,
                "source_snapshot_sha256": snapshot.source_digest,
                "canonical_revision": snapshot.revision_number,
                "report_version": self.version,
                "dataset_counts": counts,
            },
        )


CATALOG_REPORT_PROVIDERS = tuple(
    CatalogReportProvider(specification) for specification in REPORT_SPECIFICATIONS
)
