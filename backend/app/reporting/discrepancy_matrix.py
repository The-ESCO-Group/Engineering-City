"""Accepted-finding discrepancy matrix report."""

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


def _text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return ", ".join(str(item) for item in value)
    return str(value)


class DiscrepancyMatrixProvider:
    """Render accepted discrepancies without collapsing fact and inference."""

    report_type = "discrepancy_matrix"
    display_name = "Discrepancy Matrix"
    version = "2.0.0"
    supported_formats = ("html", "pdf", "docx", "xlsx", "csv", "json", "markdown")
    default_formats = ("html",)
    include_in_default_delivery = True

    def render(self, snapshot: ReportSnapshot, export_format: str) -> RenderedReport:
        if export_format not in self.supported_formats:
            raise ValueError(f"{self.report_type} does not support {export_format!r}")
        formatter = export_format_for(export_format)

        rows: list[tuple[str, ...]] = []
        for finding in snapshot.findings:
            citation_text = "; ".join(
                citation.display_location
                for citation in finding.citations
                if citation.display_location
            )
            rows.append(
                (
                    _text(finding.details.get("interlock_id")) or str(finding.finding_id),
                    finding.severity,
                    finding.title,
                    finding.fact,
                    finding.inference,
                    _text(finding.details.get("comparison_result")) or finding.category,
                    f"{finding.confidence:.0%}",
                    finding.limitations or "None stated",
                    citation_text or "No resolved citation location",
                    finding.suggested_review_action,
                )
            )

        introduction = (
            f"This matrix contains {len(rows)} human-accepted finding(s). Facts and inferences "
            "remain separate, and citation locations are reproduced verbatim from the "
            "evidence anchors."
        )
        if not rows:
            introduction = (
                "No human-accepted findings were available when this report was generated. "
                "This is not a claim that the project is discrepancy-free."
            )

        table = ReportTable(
            heading="Accepted discrepancies",
            columns=(
                "Reference",
                "Severity",
                "Finding",
                "Fact",
                "Inference",
                "Comparison",
                "Confidence",
                "Limitations",
                "Citation location",
                "Suggested review action",
            ),
            rows=tuple(rows),
        )
        document = build_report_document(
            snapshot,
            report_type=self.report_type,
            report_version=self.version,
            title=f"{snapshot.project_name} - Discrepancy Matrix",
            notice=(
                "Engineering review artifact. Only findings explicitly accepted by a human "
                "reviewer are included; AI output remains advisory."
            ),
            content_sections=(
                ReportSection(
                    heading="Scope and provenance",
                    paragraphs=(introduction,),
                    tables=(table,),
                ),
            ),
        )
        data = formatter.serialize(document)
        filename = (
            f"{safe_stem(snapshot.project_name)}-discrepancy-matrix-r"
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
                "fact_inference_separated": True,
                "document_status": document.document_status,
                "human_approved": False,
                "validation_level": document.validation_level,
                "source_snapshot_sha256": snapshot.source_digest,
                "canonical_revision": snapshot.revision_number,
                "report_version": self.version,
            },
        )
