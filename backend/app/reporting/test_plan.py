"""Evidence-based test-plan report derived from accepted findings."""

from __future__ import annotations

from app.reporting.catalog import safe_stem
from app.reporting.document import build_report_document
from app.reporting.formats import export_format_for
from app.reporting.types import (
    RenderedReport,
    ReportSection,
    ReportSnapshot,
    ReportTable,
)


class TestPlanProvider:
    """Create offline verification steps from accepted review actions."""

    report_type = "test_plan"
    display_name = "Accepted Findings Test Plan"
    version = "2.0.0"
    supported_formats = ("html", "pdf", "docx", "xlsx", "csv", "json", "markdown")
    default_formats = ("xlsx",)
    include_in_default_delivery = True

    def render(self, snapshot: ReportSnapshot, export_format: str) -> RenderedReport:
        if export_format not in self.supported_formats:
            raise ValueError(f"{self.report_type} does not support {export_format!r}")
        formatter = export_format_for(export_format)

        rows: list[tuple[str, ...]] = []
        for index, finding in enumerate(snapshot.findings, start=1):
            citations = "; ".join(
                citation.display_location
                for citation in finding.citations
                if citation.display_location
            )
            review_action = finding.suggested_review_action or (
                "An authorized engineer must define a verification action before execution."
            )
            rows.append(
                (
                    f"TP-{index:03d}",
                    finding.title,
                    finding.fact,
                    finding.inference,
                    review_action,
                    (
                        "Record objective evidence against the cited source; obtain authorized "
                        "human acceptance before treating the item as verified."
                    ),
                    citations or "No resolved citation location",
                    finding.limitations or "None stated",
                    f"{finding.confidence:.0%}",
                )
            )

        scope = (
            "This plan is derived only from human-accepted findings. It defines review and offline "
            "verification work; it does not authorize controller downloads, online edits, "
            "forced I/O, or operation of equipment."
        )
        if not rows:
            scope += (
                " No accepted findings were available, so no executable test cases were generated."
            )

        table = ReportTable(
            heading="Verification cases",
            columns=(
                "Test ID",
                "Accepted finding",
                "Observed fact",
                "Engineering inference",
                "Verification action",
                "Acceptance record",
                "Citation location",
                "Limitations",
                "Confidence",
            ),
            rows=tuple(rows),
        )
        document = build_report_document(
            snapshot,
            report_type=self.report_type,
            report_version=self.version,
            title=f"{snapshot.project_name} - Accepted Findings Test Plan",
            notice=(
                "Draft engineering test plan. Execution requires an authorized human engineer, an "
                "approved site procedure, and any required safety permits."
            ),
            content_sections=(
                ReportSection(heading="Scope and authority", paragraphs=(scope,)),
                ReportSection(heading="Test cases", tables=(table,)),
            ),
        )
        data = formatter.serialize(document)
        filename = (
            f"{safe_stem(snapshot.project_name)}-accepted-findings-test-plan-r"
            f"{snapshot.revision_number}{formatter.file_extension}"
        )
        return RenderedReport(
            title=document.title,
            filename=filename,
            media_type=formatter.media_type,
            data=data,
            finding_ids=snapshot.finding_ids,
            summary={
                "accepted_findings": len(rows),
                "test_cases": len(rows),
                "document_status": document.document_status,
                "human_approved": False,
                "validation_level": document.validation_level,
                "source_snapshot_sha256": snapshot.source_digest,
                "canonical_revision": snapshot.revision_number,
                "report_version": self.version,
            },
        )
