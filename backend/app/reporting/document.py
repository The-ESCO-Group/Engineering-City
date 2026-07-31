"""Shared report provenance and draft-status sections."""

from __future__ import annotations

from app.core.clock import isoformat_z
from app.reporting.types import (
    REPORT_GENERATOR_NAME,
    REPORT_GENERATOR_VERSION,
    ReportDocument,
    ReportSection,
    ReportSnapshot,
    ReportTable,
)

DRAFT_STATUS = "DRAFT - GENERATED; NOT REPORT-APPROVED"
GENERATED_VALIDATION_LEVEL = "generated"


def _yes_no(value: bool) -> str:
    return "Yes" if value else "No"


def _control_section(
    snapshot: ReportSnapshot, report_type: str, report_version: str
) -> ReportSection:
    validation = snapshot.validation
    validation_run = "Not run"
    if validation is not None:
        validation_run = (
            f"{validation.status}; revision {validation.revision_number}; "
            f"{validation.checks_run} check(s), {validation.error_count} error(s), "
            f"{validation.warning_count} warning(s)"
        )
    rows = (
        ("Document status", DRAFT_STATUS),
        ("Project", snapshot.project_name),
        ("Project ID", str(snapshot.project_id)),
        ("Client/internal reference", snapshot.client_reference or "Not stated"),
        ("Canonical revision", str(snapshot.revision_number)),
        ("Project workflow status", snapshot.project_status),
        ("Report type", report_type),
        ("Report version", report_version),
        ("Report artifact validation level", GENERATED_VALIDATION_LEVEL),
        ("Latest project validation run", validation_run),
        ("Generated at (UTC)", isoformat_z(snapshot.generated_at)),
        ("Source snapshot SHA-256", snapshot.source_digest or "Not available"),
        ("Generator", f"{REPORT_GENERATOR_NAME} {REPORT_GENERATOR_VERSION}"),
    )
    return ReportSection(
        heading="Document control",
        paragraphs=(
            "This artifact is generated from one captured canonical revision. Generation is not "
            "human approval, import verification, or release authority.",
        ),
        tables=(ReportTable("Document metadata", ("Field", "Value"), rows),),
    )


def _target_section(snapshot: ReportSnapshot) -> ReportSection:
    rows = snapshot.target_profile or (("Profile", "No target profile was recorded."),)
    return ReportSection(
        heading="Target profile",
        paragraphs=(
            "Target values are reproduced from the canonical project profile. Missing values "
            "remain missing and are not inferred by the report generator.",
        ),
        tables=(ReportTable("Configured target", ("Field", "Value"), rows),),
    )


def _file_section(snapshot: ReportSnapshot) -> ReportSection:
    reviewed_rows = tuple(
        (
            item.filename,
            item.role,
            item.status,
            ", ".join(item.parser_versions) or "No parser version recorded",
            item.sha256 or "No hash recorded",
        )
        for item in snapshot.files_reviewed
    )
    unsupported_rows = tuple(
        (
            item.filename,
            item.status,
            item.status_detail or "No status detail recorded",
            "; ".join(item.unsupported) or "No structured unsupported item recorded",
            "; ".join(item.warnings) or "None recorded",
        )
        for item in snapshot.files_unsupported
    )
    return ReportSection(
        heading="Files reviewed and unsupported",
        paragraphs=(
            "Only files listed as reviewed have a successful parser record. Unsupported or "
            "failed content remains visible below and is not treated as having been reviewed.",
        ),
        tables=(
            ReportTable(
                "Files reviewed",
                ("Filename", "Role", "Status", "Parser version(s)", "SHA-256"),
                reviewed_rows,
                "No file has a successful parser record in this snapshot.",
            ),
            ReportTable(
                "Files unsupported or unavailable",
                ("Filename", "Status", "Status detail", "Unsupported content", "Warnings"),
                unsupported_rows,
                "No unsupported file record was captured. This does not prove every source was "
                "read completely.",
            ),
        ),
    )


def _template_section(snapshot: ReportSnapshot, report_version: str) -> ReportSection:
    template_rows = tuple((key, version) for key, version in snapshot.templates_used)
    generator_rows = (
        (REPORT_GENERATOR_NAME, REPORT_GENERATOR_VERSION, "Format-neutral report assembly"),
        ("Report provider", report_version, "Report-specific content definition"),
    )
    return ReportSection(
        heading="Templates and generators",
        paragraphs=(
            "Template identifiers and versions are listed only when pinned in the canonical "
            "project model.",
        ),
        tables=(
            ReportTable(
                "Templates used",
                ("Template ID", "Version"),
                template_rows,
                "No pinned template was recorded for this revision.",
            ),
            ReportTable(
                "Generator versions",
                ("Generator", "Version", "Purpose"),
                generator_rows,
            ),
        ),
    )


def _finding_section(snapshot: ReportSnapshot) -> ReportSection:
    finding_rows = tuple(
        (
            str(item.finding_id),
            item.severity,
            item.category,
            item.title,
            item.fact,
            item.inference,
            f"{item.confidence:.0%}",
            _yes_no(item.requires_approval),
        )
        for item in snapshot.findings
    )
    evidence_rows = tuple(
        (
            str(finding.finding_id),
            str(citation.anchor_id),
            citation.role,
            citation.source_label,
            citation.display_location,
            citation.excerpt,
        )
        for finding in snapshot.findings
        for citation in finding.citations
    )
    return ReportSection(
        heading="Findings and evidence references",
        paragraphs=(
            "This provenance appendix contains human-accepted findings used by delivery reports. "
            "Facts and engineering inferences remain separate.",
        ),
        tables=(
            ReportTable(
                "Human-accepted findings",
                (
                    "Finding ID",
                    "Severity",
                    "Category",
                    "Title",
                    "Fact",
                    "Inference",
                    "Confidence",
                    "Approval required",
                ),
                finding_rows,
                "No human-accepted findings were captured. This is not a discrepancy-free claim.",
            ),
            ReportTable(
                "Evidence references",
                (
                    "Finding ID",
                    "Anchor ID",
                    "Role",
                    "Source",
                    "Exact location",
                    "Excerpt",
                ),
                evidence_rows,
                "No evidence reference was captured for an accepted finding.",
            ),
        ),
    )


def _approval_validation_section(snapshot: ReportSnapshot) -> ReportSection:
    approval_rows = tuple(
        (
            str(item.approval_id),
            item.kind,
            item.status,
            item.decision or "No decision",
            item.title,
            str(item.revision_number) if item.revision_number is not None else "Not pinned",
            str(item.decided_by_id) if item.decided_by_id else "No named reviewer",
            isoformat_z(item.decided_at) if item.decided_at else "Not decided",
            item.decision_notes,
        )
        for item in snapshot.approvals
    )
    validation_rows: tuple[tuple[str, ...], ...] = ()
    if snapshot.validation is not None:
        item = snapshot.validation
        validation_rows = (
            (
                str(item.run_id),
                str(item.revision_number),
                item.status,
                ", ".join(item.disciplines) or "No disciplines recorded",
                str(item.checks_run),
                str(item.error_count),
                str(item.warning_count),
                str(item.unsupported_count),
                str(item.human_review_count),
                item.summary,
            ),
        )
    return ReportSection(
        heading="Human approvals and validation",
        paragraphs=(
            "Approval records below concern the objects and gates they name. They do not approve "
            "this newly generated report unless a separate report approval is recorded later.",
        ),
        tables=(
            ReportTable(
                "Human approval records",
                (
                    "Approval ID",
                    "Kind",
                    "Status",
                    "Decision",
                    "Title",
                    "Revision",
                    "Reviewer ID",
                    "Decided at",
                    "Notes",
                ),
                approval_rows,
                "No human approval record was captured for this project revision.",
            ),
            ReportTable(
                "Latest project validation run",
                (
                    "Run ID",
                    "Revision",
                    "Status",
                    "Disciplines",
                    "Checks",
                    "Errors",
                    "Warnings",
                    "Unsupported",
                    "Human review",
                    "Summary",
                ),
                validation_rows,
                "No deterministic project validation run was captured.",
            ),
        ),
    )


def _limitations_section(snapshot: ReportSnapshot) -> ReportSection:
    rows = tuple((str(index), item) for index, item in enumerate(snapshot.known_limitations, 1))
    return ReportSection(
        heading="Known limitations and verification disclaimer",
        paragraphs=(
            "This artifact does not authorize PLC upload/download, online editing, controller "
            "mode changes, forcing, or operation of equipment.",
            "Generated and internally checked are not synonyms for Rockwell import-verified. "
            "Import verification may be claimed only from a recorded licensed-workstation result.",
            "AI-produced content is advisory. A named qualified engineer remains responsible for "
            "engineering decisions and release approval.",
        ),
        tables=(
            ReportTable(
                "Captured limitations",
                ("No.", "Limitation"),
                rows,
                "No structured limitation was captured. This is not a claim of unrestricted "
                "support.",
            ),
        ),
    )


def build_report_document(
    snapshot: ReportSnapshot,
    *,
    report_type: str,
    report_version: str,
    title: str,
    notice: str,
    content_sections: tuple[ReportSection, ...],
) -> ReportDocument:
    """Add the same complete provenance envelope to every report."""

    disclaimers = (
        "DRAFT generated artifact; no report-specific human approval has been recorded.",
        "Validation level is GENERATED unless an artifact-specific validator records otherwise.",
        "No Rockwell import verification or live-controller behavior is claimed.",
    )
    sections = (
        _control_section(snapshot, report_type, report_version),
        *content_sections,
        _target_section(snapshot),
        _file_section(snapshot),
        _template_section(snapshot, report_version),
        _finding_section(snapshot),
        _approval_validation_section(snapshot),
        _limitations_section(snapshot),
    )
    return ReportDocument(
        title=title,
        subtitle=(
            f"{snapshot.client_reference or snapshot.project_id} | Revision "
            f"{snapshot.revision_number}"
        ),
        notice=notice,
        sections=sections,
        report_type=report_type,
        report_version=report_version,
        document_status=DRAFT_STATUS,
        validation_level=GENERATED_VALIDATION_LEVEL,
        generated_at=snapshot.generated_at,
        source_digest=snapshot.source_digest,
        metadata=(
            ("Project", snapshot.project_name),
            ("Revision", str(snapshot.revision_number)),
            ("Report type", report_type),
            ("Report version", report_version),
        ),
        disclaimers=disclaimers,
    )
