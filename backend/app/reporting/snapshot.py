"""Capture one deterministic, auditable reporting snapshot."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict, replace
from datetime import datetime
from enum import Enum

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.clock import ensure_utc, isoformat_z, utcnow
from app.core.enums import SEVERITY_ORDER, FindingStatus
from app.models.approval import Approval
from app.models.delivery import (
    AlarmDefinition,
    HMIBinding,
    HMIScreen,
    ReportDefinition,
    TestCase,
)
from app.models.file import UploadedFile
from app.models.finding import Finding
from app.models.interlocks import Interlock
from app.models.plant import Device, IOPoint
from app.models.plc import ControllerTask, Program, Routine
from app.models.profile import ProjectProfile, TargetPlatform, TemplateSet
from app.models.project import Project
from app.models.release import ExportArtifact, ReleaseCandidate, ValidationResult, ValidationRun
from app.models.requirements import Requirement
from app.models.revision import ChangeSet
from app.models.tags import Tag
from app.reporting.types import (
    ApprovalSnapshot,
    CitationSnapshot,
    FileSnapshot,
    FindingSnapshot,
    ReportDataset,
    ReportSnapshot,
    ValidationSnapshot,
)


def _value(value: object) -> str:
    if value is None:
        return "Not stated"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, Enum):
        return str(value.value)
    if isinstance(value, datetime):
        return isoformat_z(value)
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (list, tuple, dict)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return str(value)


def _sorted_rows(rows: list[tuple[str, ...]]) -> tuple[tuple[str, ...], ...]:
    return tuple(sorted(rows, key=lambda row: tuple(item.casefold() for item in row)))


def _dataset(
    key: str,
    title: str,
    columns: tuple[str, ...],
    rows: list[tuple[str, ...]],
    empty_message: str,
) -> ReportDataset:
    return ReportDataset(
        key=key,
        title=title,
        columns=columns,
        rows=_sorted_rows(rows),
        empty_message=empty_message,
    )


def _files(db: Session, project_id: uuid.UUID) -> tuple[FileSnapshot, ...]:
    files = list(
        db.execute(select(UploadedFile).where(UploadedFile.project_id == project_id)).scalars()
    )
    snapshots: list[FileSnapshot] = []
    for item in files:
        artifacts = sorted(
            item.artifacts,
            key=lambda artifact: (artifact.parser_name, artifact.parser_version, str(artifact.id)),
        )
        snapshots.append(
            FileSnapshot(
                file_id=item.id,
                filename=item.original_filename,
                sha256=item.sha256,
                status=_value(item.status),
                role=_value(item.document_role),
                parser_versions=tuple(
                    f"{artifact.parser_name} {artifact.parser_version}" for artifact in artifacts
                ),
                warnings=tuple(
                    _value(warning) for artifact in artifacts for warning in artifact.warnings
                ),
                unsupported=tuple(
                    _value(entry) for artifact in artifacts for entry in artifact.unsupported
                ),
                status_detail=item.status_detail,
            )
        )
    return tuple(sorted(snapshots, key=lambda item: (item.filename.casefold(), str(item.file_id))))


def _target_profile(
    db: Session, project_id: uuid.UUID
) -> tuple[tuple[tuple[str, str], ...], tuple[tuple[str, str], ...], tuple[str, ...]]:
    profile = db.execute(
        select(ProjectProfile).where(ProjectProfile.project_id == project_id)
    ).scalar_one_or_none()
    target = db.execute(
        select(TargetPlatform).where(TargetPlatform.project_id == project_id)
    ).scalar_one_or_none()
    rows: list[tuple[str, str]] = []
    limitations: list[str] = []
    if profile is not None:
        rows.extend(
            (
                ("Project name", profile.project_name or "Not stated"),
                ("Customer/internal label", profile.customer_label or "Not stated"),
                ("Plant/site", profile.plant_or_site or "Not stated"),
                ("Project type", _value(profile.project_type)),
                ("Release mode", _value(profile.release_mode)),
                ("Naming standard version", profile.naming_standard_version or "Not stated"),
                ("Alarm philosophy version", profile.alarm_philosophy_version or "Not stated"),
                ("Units", _value(profile.units)),
                ("Locale", profile.locale or "Not stated"),
                ("Profile complete for generation", _value(profile.is_complete_for_generation)),
                ("Restricted domains", _value(profile.restricted_domains)),
                ("Restricted-domain mode", _value(profile.restricted_domain_mode)),
            )
        )
        if profile.missing_required_fields:
            limitations.append(
                "Target profile is missing required field(s): "
                + ", ".join(str(item) for item in profile.missing_required_fields)
            )
    if target is not None:
        rows.extend(
            (
                ("Controller family", _value(target.controller_family)),
                ("Controller catalog number", target.controller_catalog_number or "Not stated"),
                ("Firmware revision", target.firmware_revision or "Not stated"),
                ("Studio 5000 version", target.studio_5000_version or "Not stated"),
                ("Safety classification", _value(target.safety_classification)),
                ("HMI product", _value(target.hmi_product)),
                ("HMI version", target.hmi_version or "Not stated"),
                ("HMI terminal/model", target.hmi_terminal_model or "Not stated"),
                (
                    "HMI resolution",
                    (
                        f"{target.hmi_resolution_width} x {target.hmi_resolution_height}"
                        if target.hmi_resolution_width and target.hmi_resolution_height
                        else "Not stated"
                    ),
                ),
                (
                    "Instruction-set profile",
                    target.supported_instruction_set_key or "Not stated",
                ),
            )
        )
        limitations.extend(_value(item) for item in target.known_limitations)
    if not rows:
        rows.append(("Target profile", "No canonical project/target profile was recorded."))

    template_rows = list(
        db.execute(select(TemplateSet).where(TemplateSet.project_id == project_id)).scalars()
    )
    templates: list[tuple[str, str]] = []
    selected_template_rows = (
        [item for item in template_rows if item.id == profile.template_set_id]
        if profile is not None and profile.template_set_id is not None
        else []
    )
    for item in sorted(selected_template_rows, key=lambda row: (row.key, row.version, str(row.id))):
        templates.append((item.key, item.version or "Not versioned"))
        for template_id in sorted(str(value) for value in item.template_ids):
            templates.append((template_id, item.version or "Version inherited from template set"))
    routines = list(db.execute(select(Routine).where(Routine.project_id == project_id)).scalars())
    screens = list(
        db.execute(select(HMIScreen).where(HMIScreen.project_id == project_id)).scalars()
    )
    templates.extend(
        (item.template_id, item.template_version or "Not versioned")
        for item in routines
        if item.template_id
    )
    templates.extend(
        (item.template_id, item.template_version or "Not versioned")
        for item in screens
        if item.template_id
    )
    templates = sorted(set(templates), key=lambda item: (item[0].casefold(), item[1]))
    return tuple(rows), tuple(templates), tuple(limitations)


def _approvals(db: Session, project_id: uuid.UUID) -> tuple[ApprovalSnapshot, ...]:
    rows = list(db.execute(select(Approval).where(Approval.project_id == project_id)).scalars())
    snapshots = [
        ApprovalSnapshot(
            approval_id=item.id,
            kind=_value(item.kind),
            status=_value(item.status),
            decision="" if item.decision is None else _value(item.decision),
            title=item.title,
            revision_number=item.revision_number,
            decided_by_id=item.decided_by_id,
            decided_at=ensure_utc(item.decided_at) if item.decided_at else None,
            decision_notes=item.decision_notes,
        )
        for item in rows
    ]
    return tuple(
        sorted(
            snapshots,
            key=lambda item: (
                item.revision_number if item.revision_number is not None else -1,
                item.title.casefold(),
                str(item.approval_id),
            ),
        )
    )


def _latest_validation(
    db: Session, project_id: uuid.UUID
) -> tuple[ValidationSnapshot | None, ValidationRun | None]:
    runs = list(
        db.execute(select(ValidationRun).where(ValidationRun.project_id == project_id)).scalars()
    )
    if not runs:
        return None, None
    run = max(
        runs,
        key=lambda item: (
            item.revision_number,
            isoformat_z(item.finished_at or item.started_at),
            str(item.id),
        ),
    )
    return (
        ValidationSnapshot(
            run_id=run.id,
            revision_number=run.revision_number,
            status=_value(run.status),
            disciplines=tuple(sorted(_value(item) for item in run.disciplines)),
            checks_run=run.checks_run,
            error_count=run.error_count,
            warning_count=run.warning_count,
            information_count=run.information_count,
            unsupported_count=run.unsupported_count,
            human_review_count=run.human_review_count,
            started_at=ensure_utc(run.started_at),
            finished_at=ensure_utc(run.finished_at) if run.finished_at else None,
            summary=run.summary,
        ),
        run,
    )


def _accepted_findings(db: Session, project_id: uuid.UUID) -> tuple[FindingSnapshot, ...]:
    rows = list(
        db.execute(
            select(Finding).where(
                Finding.project_id == project_id,
                Finding.status == FindingStatus.ACCEPTED,
            )
        ).scalars()
    )
    rows.sort(
        key=lambda item: (-SEVERITY_ORDER[item.severity], item.title.casefold(), str(item.id))
    )
    findings: list[FindingSnapshot] = []
    for finding in rows:
        citations = tuple(
            CitationSnapshot(
                anchor_id=link.anchor_id,
                source_label=link.anchor.source_label if link.anchor else "",
                display_location=link.anchor.display_location if link.anchor else "",
                excerpt=link.anchor.excerpt if link.anchor else "",
                role=link.role,
            )
            for link in sorted(
                finding.evidence_links,
                key=lambda item: (
                    item.role,
                    item.anchor.display_location if item.anchor else "",
                    str(item.anchor_id),
                ),
            )
            if link.anchor_id is not None
        )
        findings.append(
            FindingSnapshot(
                finding_id=finding.id,
                title=finding.title,
                category=_value(finding.category),
                severity=_value(finding.severity),
                fact=finding.fact,
                inference=finding.inference,
                suggested_review_action=finding.suggested_review_action,
                limitations=finding.limitations,
                confidence=finding.confidence,
                requires_approval=finding.requires_approval,
                decision_notes=finding.decision_notes,
                details=dict(finding.details),
                citations=citations,
            )
        )
    return tuple(findings)


def _build_datasets(
    db: Session, project: Project, latest_validation: ValidationRun | None
) -> tuple[ReportDataset, ...]:
    project_id = project.id
    datasets: list[ReportDataset] = []

    requirements = list(
        db.execute(select(Requirement).where(Requirement.project_id == project_id)).scalars()
    )
    datasets.append(
        _dataset(
            "requirements",
            "Requirements traceability",
            (
                "Reference",
                "Title",
                "Statement",
                "Source text",
                "Kind",
                "Priority",
                "Status",
                "Inferred",
                "Confidence",
                "Verification method",
                "Implementation coverage",
            ),
            [
                (
                    item.reference,
                    item.title,
                    item.statement,
                    item.source_text,
                    _value(item.kind),
                    _value(item.priority),
                    _value(item.status),
                    _value(item.is_inferred),
                    f"{item.confidence:.0%}",
                    item.verification_method or "Not stated",
                    item.implemented_by_summary or "No implementation coverage recorded",
                )
                for item in requirements
            ],
            "No canonical requirement was captured for this revision.",
        )
    )

    devices = list(db.execute(select(Device).where(Device.project_id == project_id)).scalars())
    datasets.append(
        _dataset(
            "devices",
            "Device schedule",
            (
                "Tag prefix",
                "Name",
                "Category",
                "Manufacturer",
                "Model",
                "Location",
                "Fail state",
                "Fail-state value",
                "Network only",
                "Safety related",
                "Description",
            ),
            [
                (
                    item.tag_prefix,
                    item.name,
                    _value(item.category),
                    item.manufacturer or "Not stated",
                    item.model_number or "Not stated",
                    item.location_description or "Not stated",
                    _value(item.fail_state),
                    item.fail_state_value or "Not stated",
                    _value(item.is_network_only),
                    _value(item.is_safety_related),
                    item.description,
                )
                for item in devices
            ],
            "No canonical device was captured for this revision.",
        )
    )
    io_points = list(db.execute(select(IOPoint).where(IOPoint.project_id == project_id)).scalars())
    datasets.append(
        _dataset(
            "io_points",
            "I/O schedule",
            (
                "Address",
                "Channel",
                "Signal type",
                "Direction",
                "Description",
                "Engineering units",
                "Engineering range",
                "Raw range",
                "Fail state",
                "Spare",
            ),
            [
                (
                    item.address,
                    _value(item.channel),
                    _value(item.signal_type),
                    _value(item.direction),
                    item.description or "Not stated",
                    item.engineering_units or "Not stated",
                    f"{_value(item.range_min)} to {_value(item.range_max)}",
                    f"{_value(item.raw_min)} to {_value(item.raw_max)}",
                    _value(item.fail_state),
                    _value(item.is_spare),
                )
                for item in io_points
            ],
            "No canonical I/O point was captured for this revision.",
        )
    )

    tags = list(db.execute(select(Tag).where(Tag.project_id == project_id)).scalars())
    datasets.append(
        _dataset(
            "tags",
            "Tag register",
            (
                "Name",
                "Scope",
                "Role",
                "Base data type",
                "Concrete type",
                "Dimensions",
                "Description",
                "Units",
                "External access",
                "Alias target",
                "HMI exposed",
                "Generated",
                "Usage count",
            ),
            [
                (
                    item.name,
                    _value(item.scope),
                    _value(item.role),
                    _value(item.base_data_type),
                    item.data_type_name or "Not applicable",
                    item.dimensions or "Scalar",
                    item.description,
                    item.engineering_units or "Not stated",
                    item.external_access,
                    item.alias_for or "Not an alias",
                    _value(item.hmi_exposed),
                    _value(item.is_generated),
                    str(item.usage_count),
                )
                for item in tags
            ],
            "No canonical tag was captured for this revision.",
        )
    )

    tasks = list(
        db.execute(select(ControllerTask).where(ControllerTask.project_id == project_id)).scalars()
    )
    task_names = {item.id: item.name for item in tasks}
    programs = list(db.execute(select(Program).where(Program.project_id == project_id)).scalars())
    program_names = {item.id: item.name for item in programs}
    routines = list(db.execute(select(Routine).where(Routine.project_id == project_id)).scalars())
    datasets.append(
        _dataset(
            "plc_index",
            "PLC program and routine index",
            (
                "Task",
                "Program",
                "Routine",
                "Language",
                "Main routine",
                "Called",
                "Rung count",
                "Template ID",
                "Template version",
                "Description",
            ),
            [
                (
                    (
                        task_names.get(program.controller_task_id, "Unscheduled")
                        if program.controller_task_id is not None
                        else "Unscheduled"
                    ),
                    program_names.get(item.program_id, "Unknown program"),
                    item.name,
                    _value(item.language),
                    _value(item.is_main),
                    _value(item.is_called),
                    str(item.rung_count),
                    item.template_id or "No template recorded",
                    item.template_version or "Not versioned",
                    item.description,
                )
                for item in routines
                for program in programs
                if program.id == item.program_id
            ],
            "No canonical PLC routine was captured for this revision.",
        )
    )

    interlocks = list(
        db.execute(select(Interlock).where(Interlock.project_id == project_id)).scalars()
    )
    datasets.append(
        _dataset(
            "interlocks",
            "Interlock matrix",
            (
                "Reference",
                "Name",
                "Kind",
                "Trip condition",
                "Trip tag",
                "Comparison",
                "Threshold",
                "Action",
                "Safety related",
                "Implemented",
                "Implementation note",
                "Requirement",
                "Source reference",
            ),
            [
                (
                    item.reference,
                    item.name,
                    _value(item.kind),
                    item.trip_condition,
                    item.trip_tag_name or "Not stated",
                    item.comparison or "Not stated",
                    item.threshold_value or "Not stated",
                    _value(item.action),
                    _value(item.is_safety_related),
                    _value(item.has_implementation),
                    item.implementation_note,
                    item.requirement_reference or "Not linked",
                    item.source_reference or "Not stated",
                )
                for item in interlocks
            ],
            "No canonical interlock was captured for this revision.",
        )
    )

    screens = list(
        db.execute(select(HMIScreen).where(HMIScreen.project_id == project_id)).scalars()
    )
    screen_names = {item.id: item.key for item in screens}
    datasets.append(
        _dataset(
            "hmi_screens",
            "HMI screen register",
            (
                "Screen key",
                "Name",
                "Title",
                "Type",
                "Resolution",
                "Template ID",
                "Template version",
                "Description",
            ),
            [
                (
                    item.key,
                    item.name,
                    item.title,
                    item.screen_type or "Not stated",
                    (
                        f"{item.width} x {item.height}"
                        if item.width is not None and item.height is not None
                        else "Not stated"
                    ),
                    item.template_id or "No template recorded",
                    item.template_version or "Not versioned",
                    item.description,
                )
                for item in screens
            ],
            "No canonical HMI screen was captured for this revision.",
        )
    )
    bindings = list(
        db.execute(select(HMIBinding).where(HMIBinding.project_id == project_id)).scalars()
    )
    datasets.append(
        _dataset(
            "hmi_bindings",
            "HMI binding register",
            (
                "Screen",
                "Property",
                "Tag name",
                "Access",
                "Expression",
                "Resolved",
                "Unresolved reason",
            ),
            [
                (
                    screen_names.get(item.screen_id, str(item.screen_id)),
                    item.property_name,
                    item.tag_name or "Not stated",
                    item.access,
                    item.expression,
                    _value(item.is_resolved),
                    item.unresolved_reason,
                )
                for item in bindings
            ],
            "No canonical HMI binding was captured for this revision.",
        )
    )

    alarms = list(
        db.execute(
            select(AlarmDefinition).where(AlarmDefinition.project_id == project_id)
        ).scalars()
    )
    datasets.append(
        _dataset(
            "alarms",
            "Alarm register",
            (
                "Reference",
                "Name",
                "Severity",
                "Class",
                "Trigger tag",
                "Trigger type",
                "Limit",
                "Deadband",
                "Delay seconds",
                "Acknowledgement required",
                "Driven",
                "Resolved",
                "Messages and operator guidance",
            ),
            [
                (
                    item.reference,
                    item.name,
                    _value(item.severity),
                    item.alarm_class or "Not stated",
                    item.trigger_tag_name or "Not stated",
                    item.trigger_type or "Not stated",
                    item.limit_value or "Not stated",
                    item.deadband or "Not stated",
                    _value(item.delay_seconds),
                    _value(item.requires_acknowledge),
                    _value(item.is_driven),
                    _value(item.is_resolved),
                    "; ".join(
                        f"{message.language_code}: {message.text} | {message.operator_guidance}"
                        for message in sorted(
                            item.messages,
                            key=lambda message: (message.language_code, message.ordinal),
                        )
                    )
                    or "No message recorded",
                )
                for item in alarms
            ],
            "No canonical alarm was captured for this revision.",
        )
    )

    all_findings = list(
        db.execute(select(Finding).where(Finding.project_id == project_id)).scalars()
    )
    datasets.append(
        _dataset(
            "findings",
            "Findings register",
            (
                "Finding ID",
                "Severity",
                "Category",
                "Status",
                "Title",
                "Fact",
                "Inference",
                "Confidence",
                "Approval required",
                "Review action",
                "Limitations",
                "Decision notes",
            ),
            [
                (
                    str(item.id),
                    _value(item.severity),
                    _value(item.category),
                    _value(item.status),
                    item.title,
                    item.fact,
                    item.inference,
                    f"{item.confidence:.0%}",
                    _value(item.requires_approval),
                    item.suggested_review_action,
                    item.limitations,
                    item.decision_notes,
                )
                for item in all_findings
            ],
            "No finding record was captured. This is not a clean-review claim.",
        )
    )

    test_cases = list(
        db.execute(select(TestCase).where(TestCase.project_id == project_id)).scalars()
    )

    def test_rows(cases: list[TestCase]) -> list[tuple[str, ...]]:
        rows: list[tuple[str, ...]] = []
        for case in cases:
            steps = sorted(
                case.steps, key=lambda item: (item.ordinal, item.step_number, str(item.id))
            )
            if not steps:
                rows.append(
                    (
                        case.reference,
                        case.test_type,
                        case.title,
                        case.objective,
                        case.preconditions,
                        "No step recorded",
                        "No action recorded",
                        case.expected_result,
                        case.status,
                        _value(case.is_destructive_test),
                        case.safety_notes,
                        _value(case.requirement_id),
                    )
                )
            for step in steps:
                rows.append(
                    (
                        case.reference,
                        case.test_type,
                        case.title,
                        case.objective,
                        case.preconditions,
                        str(step.step_number),
                        step.action,
                        step.expected_observation or case.expected_result,
                        case.status,
                        _value(case.is_destructive_test),
                        case.safety_notes,
                        _value(case.requirement_id),
                    )
                )
        return rows

    test_columns = (
        "Test ID",
        "Type",
        "Title",
        "Objective",
        "Prerequisites / initial conditions",
        "Step",
        "Action",
        "Expected result",
        "Status",
        "Destructive test",
        "Safety notes",
        "Requirement ID",
    )
    test_sets = {
        "tests": test_cases,
        "iat_tests": [item for item in test_cases if item.test_type.casefold() == "iat"],
        "fat_tests": [item for item in test_cases if item.test_type.casefold() == "fat"],
        "manual_tests": [
            item for item in test_cases if item.test_type.casefold() in {"manual", "manual_mode"}
        ],
        "automatic_tests": [
            item
            for item in test_cases
            if item.test_type.casefold() in {"automatic", "automatic_mode", "auto"}
        ],
    }
    titles = {
        "tests": "All test cases",
        "iat_tests": "IAT plan",
        "fat_tests": "FAT plan",
        "manual_tests": "Manual-mode test plan",
        "automatic_tests": "Automatic-mode test plan",
    }
    for key, cases in test_sets.items():
        datasets.append(
            _dataset(
                key,
                titles[key],
                test_columns,
                test_rows(cases),
                f"No canonical {titles[key].lower()} record was captured; "
                "no test behavior was invented.",
            )
        )

    changes = list(
        db.execute(select(ChangeSet).where(ChangeSet.project_id == project_id)).scalars()
    )
    datasets.append(
        _dataset(
            "change_log",
            "Change log",
            (
                "Change set ID",
                "Base revision",
                "Merged revision",
                "Status",
                "Title",
                "Summary",
                "Rationale",
                "Author agent",
                "Operation count",
                "Approval required",
                "Submitted at",
                "Merged at",
                "Disposition",
            ),
            [
                (
                    str(item.id),
                    str(item.base_revision_number),
                    _value(item.merged_revision_number),
                    _value(item.status),
                    item.title,
                    item.summary,
                    item.rationale,
                    item.author_agent or "Human/user-authored",
                    str(item.operation_count),
                    _value(item.requires_human_approval),
                    _value(item.submitted_at),
                    _value(item.merged_at),
                    item.disposition_reason,
                )
                for item in changes
            ],
            "No change set was captured for this revision.",
        )
    )

    approvals = list(
        db.execute(select(Approval).where(Approval.project_id == project_id)).scalars()
    )
    datasets.append(
        _dataset(
            "approvals",
            "Approval record",
            (
                "Approval ID",
                "Kind",
                "Gate",
                "Revision",
                "Status",
                "Decision",
                "Title",
                "Question",
                "Reviewer ID",
                "Decided at",
                "Decision notes",
            ),
            [
                (
                    str(item.id),
                    _value(item.kind),
                    _value(item.gate_key),
                    _value(item.revision_number),
                    _value(item.status),
                    _value(item.decision),
                    item.title,
                    item.question,
                    _value(item.decided_by_id),
                    _value(item.decided_at),
                    item.decision_notes,
                )
                for item in approvals
            ],
            "No human approval record was captured.",
        )
    )

    results: list[ValidationResult] = []
    if latest_validation is not None:
        results = list(
            db.execute(
                select(ValidationResult).where(
                    ValidationResult.validation_run_id == latest_validation.id
                )
            ).scalars()
        )
    datasets.append(
        _dataset(
            "validation",
            "Validation summary",
            (
                "Code",
                "Discipline",
                "Severity",
                "Message",
                "Object",
                "Suggested action",
                "Human decision required",
                "Waived",
                "Waiver reason",
            ),
            [
                (
                    item.code,
                    _value(item.discipline),
                    _value(item.severity),
                    item.message,
                    item.object_label or "Project-wide",
                    item.suggested_action,
                    _value(item.requires_human_decision),
                    _value(item.is_waived),
                    item.waiver_reason,
                )
                for item in results
            ],
            "No result from a latest deterministic validation run was captured.",
        )
    )

    artifacts = list(
        db.execute(select(ExportArtifact).where(ExportArtifact.project_id == project_id)).scalars()
    )
    candidates = list(
        db.execute(
            select(ReleaseCandidate).where(ReleaseCandidate.project_id == project_id)
        ).scalars()
    )
    candidate_numbers = {item.id: item.candidate_number for item in candidates}
    datasets.append(
        _dataset(
            "import_order",
            "Import order guide",
            (
                "Release candidate",
                "Order",
                "Artifact",
                "Kind",
                "Validation level",
                "Generator",
                "Template",
                "SHA-256",
                "Warnings",
            ),
            [
                (
                    str(candidate_numbers.get(item.release_candidate_id, "Unknown")),
                    str(item.import_order),
                    item.relative_path,
                    _value(item.kind),
                    _value(item.validation_level),
                    f"{item.generator_name} {item.generator_version}".strip(),
                    f"{item.template_id} {item.template_version}".strip() or "No template recorded",
                    item.sha256,
                    _value(item.warnings),
                )
                for item in artifacts
            ],
            "No release artifact or import order was captured. Do not infer an import sequence.",
        )
    )

    report_definitions = list(
        db.execute(
            select(ReportDefinition).where(ReportDefinition.project_id == project_id)
        ).scalars()
    )
    datasets.append(
        _dataset(
            "report_definitions",
            "Required report definitions",
            (
                "Key",
                "Report type",
                "Display name",
                "Required formats",
                "Release required",
                "Provider available",
                "Unavailable reason",
            ),
            [
                (
                    item.key,
                    item.report_type,
                    item.display_name,
                    _value(item.required_formats),
                    _value(item.is_required_for_release),
                    _value(item.provider_available),
                    item.unavailable_reason,
                )
                for item in report_definitions
            ],
            "No report-definition expectation was captured for this project.",
        )
    )

    summary_counts = (
        ("Requirements", str(len(requirements))),
        ("Devices", str(len(devices))),
        ("I/O points", str(len(io_points))),
        ("Tags", str(len(tags))),
        ("PLC programs", str(len(programs))),
        ("PLC routines", str(len(routines))),
        ("Interlocks", str(len(interlocks))),
        ("HMI screens", str(len(screens))),
        ("HMI bindings", str(len(bindings))),
        ("Alarms", str(len(alarms))),
        ("Findings", str(len(all_findings))),
        ("Test cases", str(len(test_cases))),
        ("Approvals", str(len(approvals))),
        ("Release artifacts", str(len(artifacts))),
    )
    datasets.append(
        ReportDataset(
            key="project_summary",
            title="Canonical model summary",
            columns=("Object class", "Count"),
            rows=summary_counts,
        )
    )
    return tuple(datasets)


def _digest(snapshot: ReportSnapshot) -> str:
    payload = asdict(snapshot)
    payload.pop("generated_at", None)
    payload.pop("source_digest", None)

    def fallback(value: object) -> str:
        if isinstance(value, uuid.UUID):
            return str(value)
        if isinstance(value, datetime):
            return isoformat_z(value)
        raise TypeError(f"unsupported report snapshot value: {type(value).__name__}")

    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=fallback,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_report_snapshot(
    db: Session,
    project: Project,
    *,
    generated_at: datetime | None = None,
) -> ReportSnapshot:
    """Capture all reportable values from exactly one canonical revision."""

    files = _files(db, project.id)
    target_profile, templates, profile_limitations = _target_profile(db, project.id)
    validation, validation_row = _latest_validation(db, project.id)
    datasets = _build_datasets(db, project, validation_row)
    limitations = list(profile_limitations)
    for item in files:
        if not item.was_reviewed:
            status_detail = item.status_detail or (
                f"file status is {item.status}; no parser review recorded"
            )
            limitations.append(f"{item.filename}: {status_detail}")
        limitations.extend(f"{item.filename}: {entry}" for entry in item.unsupported)
    candidates = list(
        db.execute(
            select(ReleaseCandidate).where(ReleaseCandidate.project_id == project.id)
        ).scalars()
    )
    for candidate in candidates:
        limitations.extend(_value(item) for item in candidate.limitations)
        limitations.extend(
            f"Unsupported release item: {_value(item)}" for item in candidate.unsupported_items
        )
    if not limitations:
        limitations.append(
            "No structured limitation was recorded; unsupported behavior must still be checked "
            "against the configured target and licensed workstation before release."
        )

    timestamp = ensure_utc(generated_at or utcnow())
    snapshot = ReportSnapshot(
        project_id=project.id,
        project_name=project.name,
        client_reference=project.client_reference,
        findings=_accepted_findings(db, project.id),
        revision_number=project.current_revision_number,
        project_status=_value(project.status),
        target_profile=target_profile,
        files=files,
        templates_used=templates,
        approvals=_approvals(db, project.id),
        validation=validation,
        datasets=datasets,
        known_limitations=tuple(sorted(set(limitations), key=str.casefold)),
        generated_at=timestamp,
    )
    return replace(snapshot, source_digest=_digest(snapshot))
