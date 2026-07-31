"""Immutable reporting inputs and format-neutral document values.

The reporting boundary is intentionally made of frozen dataclasses. Providers
receive a complete snapshot and never a database session, so rendering a PDF
cannot discover extra rows that were not part of the HTML generated beside it.
The same snapshot therefore produces byte-for-byte stable artifacts in every
supported format.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

REPORT_SCHEMA_VERSION = "1.0.0"
REPORT_GENERATOR_NAME = "ESCO Engineering City Reporting"
REPORT_GENERATOR_VERSION = "2.0.0"


@dataclass(frozen=True, slots=True)
class CitationSnapshot:
    """One exact, human-followable evidence citation."""

    anchor_id: uuid.UUID
    source_label: str
    display_location: str
    excerpt: str
    role: str


@dataclass(frozen=True, slots=True)
class FindingSnapshot:
    """The report-safe view of one human-accepted finding."""

    finding_id: uuid.UUID
    title: str
    category: str
    severity: str
    fact: str
    inference: str
    suggested_review_action: str
    limitations: str
    confidence: float
    requires_approval: bool
    decision_notes: str
    details: dict[str, Any]
    citations: tuple[CitationSnapshot, ...] = ()


@dataclass(frozen=True, slots=True)
class FileSnapshot:
    """One input file and the parser facts needed for provenance."""

    file_id: uuid.UUID
    filename: str
    sha256: str
    status: str
    role: str
    parser_versions: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    unsupported: tuple[str, ...] = ()
    status_detail: str = ""

    @property
    def was_reviewed(self) -> bool:
        return self.status == "parsed" and bool(self.parser_versions)


@dataclass(frozen=True, slots=True)
class ApprovalSnapshot:
    """One named human decision; an undecided request remains explicit."""

    approval_id: uuid.UUID
    kind: str
    status: str
    decision: str
    title: str
    revision_number: int | None
    decided_by_id: uuid.UUID | None
    decided_at: datetime | None
    decision_notes: str


@dataclass(frozen=True, slots=True)
class ValidationSnapshot:
    """Facts from the latest deterministic validation run."""

    run_id: uuid.UUID
    revision_number: int
    status: str
    disciplines: tuple[str, ...]
    checks_run: int
    error_count: int
    warning_count: int
    information_count: int
    unsupported_count: int
    human_review_count: int
    started_at: datetime
    finished_at: datetime | None
    summary: str


@dataclass(frozen=True, slots=True)
class ReportDataset:
    """A named, already-sorted table captured from the canonical model."""

    key: str
    title: str
    columns: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...] = ()
    empty_message: str = (
        "No records were present in the captured project revision. This is not a clean-result "
        "claim; it may indicate that the source data or delivery capability is unavailable."
    )


@dataclass(frozen=True, slots=True)
class ReportSnapshot:
    """A deterministic project snapshot consumed by report providers."""

    project_id: uuid.UUID
    project_name: str
    client_reference: str
    findings: tuple[FindingSnapshot, ...]
    revision_number: int = 0
    project_status: str = "draft"
    target_profile: tuple[tuple[str, str], ...] = ()
    files: tuple[FileSnapshot, ...] = ()
    templates_used: tuple[tuple[str, str], ...] = ()
    approvals: tuple[ApprovalSnapshot, ...] = ()
    validation: ValidationSnapshot | None = None
    datasets: tuple[ReportDataset, ...] = ()
    known_limitations: tuple[str, ...] = ()
    generated_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    source_digest: str = ""

    @property
    def finding_ids(self) -> tuple[uuid.UUID, ...]:
        return tuple(item.finding_id for item in self.findings)

    @property
    def files_reviewed(self) -> tuple[FileSnapshot, ...]:
        return tuple(item for item in self.files if item.was_reviewed)

    @property
    def files_unsupported(self) -> tuple[FileSnapshot, ...]:
        return tuple(item for item in self.files if not item.was_reviewed or item.unsupported)

    def dataset(self, key: str) -> ReportDataset | None:
        return next((item for item in self.datasets if item.key == key), None)


@dataclass(frozen=True, slots=True)
class ReportTable:
    """A format-neutral table."""

    heading: str
    columns: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]
    empty_message: str = "No records available for this table."


@dataclass(frozen=True, slots=True)
class ReportSection:
    """A format-neutral section containing paragraphs and optional tables."""

    heading: str
    paragraphs: tuple[str, ...] = ()
    tables: tuple[ReportTable, ...] = ()


@dataclass(frozen=True, slots=True)
class ReportDocument:
    """Logical report document serialized by an export-format provider."""

    title: str
    subtitle: str
    notice: str
    sections: tuple[ReportSection, ...]
    report_type: str = "report"
    report_version: str = "1.0.0"
    document_status: str = "DRAFT - GENERATED; NOT REPORT-APPROVED"
    validation_level: str = "generated"
    generated_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    source_digest: str = ""
    metadata: tuple[tuple[str, str], ...] = ()
    disclaimers: tuple[str, ...] = ()
    generator_name: str = REPORT_GENERATOR_NAME
    generator_version: str = REPORT_GENERATOR_VERSION


@dataclass(frozen=True, slots=True)
class RenderedReport:
    """Concrete report bytes plus traceability metadata."""

    title: str
    filename: str
    media_type: str
    data: bytes
    finding_ids: tuple[uuid.UUID, ...]
    summary: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class DeliveryRunResult:
    """Summary returned by a delivery plugin to the workflow handler."""

    report_ids: tuple[uuid.UUID, ...] = ()
    generated: tuple[str, ...] = ()
    skipped: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def summary(self) -> dict[str, Any]:
        return {
            "report_ids": [str(value) for value in self.report_ids],
            "generated": list(self.generated),
            "skipped": list(self.skipped),
            "warnings": list(self.warnings),
        }
