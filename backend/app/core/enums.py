"""Canonical enumerations for the ESCO Engineering City domain.

These values are part of the published contract (``contracts/domain.schema.json``).
Changing a member value is a breaking change for the frontend and for any
persisted database row, so members are only ever added, never renamed.

Every enum inherits from ``str`` so that SQLAlchemy stores the stable string
value and FastAPI/Pydantic serialise it directly to JSON.
"""

from __future__ import annotations

from enum import StrEnum


class RoleName(StrEnum):
    """Global (system-wide) role assigned to a :class:`~app.models.user.User`."""

    ENGINEER = "engineer"
    REVIEWER = "reviewer"
    ADMINISTRATOR = "administrator"


class ProjectRole(StrEnum):
    """Per-project membership role.

    Distinct from :class:`RoleName`: a user may be a global ``engineer`` but only
    a ``viewer`` on a specific project.
    """

    OWNER = "owner"
    ENGINEER = "engineer"
    REVIEWER = "reviewer"
    VIEWER = "viewer"


class ProjectStatus(StrEnum):
    DRAFT = "draft"
    INGESTING = "ingesting"
    PLANNED = "planned"
    RUNNING = "running"
    AWAITING_REVIEW = "awaiting_review"
    COMPLETED = "completed"
    FAILED = "failed"
    ARCHIVED = "archived"


#: Statuses from which no further workflow progress is possible without an
#: explicit operator action (restart, unarchive).
TERMINAL_PROJECT_STATUSES: frozenset[ProjectStatus] = frozenset(
    {ProjectStatus.COMPLETED, ProjectStatus.FAILED, ProjectStatus.ARCHIVED}
)


class TaskStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    BLOCKED = "blocked"
    AWAITING_APPROVAL = "awaiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


#: Task statuses that will never change again without operator intervention.
TERMINAL_TASK_STATUSES: frozenset[TaskStatus] = frozenset(
    {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}
)


class AgentStatus(StrEnum):
    """Presence state of an agent instance.

    ``WALKING`` exists purely for the city visualisation layer; the backend
    never gates behaviour on it.
    """

    IDLE = "idle"
    WALKING = "walking"
    WORKING = "working"
    WAITING = "waiting"
    BLOCKED = "blocked"
    OFFLINE = "offline"


class AgentRunStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    BUDGET_EXCEEDED = "budget_exceeded"


class FindingStatus(StrEnum):
    OPEN = "open"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    RESOLVED = "resolved"


class FindingSeverity(StrEnum):
    INFORMATIONAL = "informational"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


#: Ordering used when sorting or comparing severities. Higher is more severe.
SEVERITY_ORDER: dict[FindingSeverity, int] = {
    FindingSeverity.INFORMATIONAL: 0,
    FindingSeverity.LOW: 1,
    FindingSeverity.MEDIUM: 2,
    FindingSeverity.HIGH: 3,
    FindingSeverity.CRITICAL: 4,
}


class FindingCategory(StrEnum):
    """What kind of engineering concern a finding describes."""

    INTERLOCK_DISCREPANCY = "interlock_discrepancy"
    MISSING_LOGIC = "missing_logic"
    UNDOCUMENTED_LOGIC = "undocumented_logic"
    NAMING_MISMATCH = "naming_mismatch"
    UNRESOLVED_REFERENCE = "unresolved_reference"
    DUPLICATE_PATTERN = "duplicate_pattern"
    FAIL_SAFE_UNCLEAR = "fail_safe_unclear"
    ALARM_COVERAGE = "alarm_coverage"
    IO_MISMATCH = "io_mismatch"
    DOCUMENTATION_GAP = "documentation_gap"
    PARSER_LIMITATION = "parser_limitation"
    OTHER = "other"


class EvidenceKind(StrEnum):
    """How strongly a piece of evidence supports a claim.

    The product must never blur these together: an inference drawn from partial
    data is not the same as a direct quotation from a source file.
    """

    DIRECT = "direct"
    INFERENCE = "inference"
    MISSING_EXPECTED = "missing_expected"
    PARSER_WARNING = "parser_warning"
    UNSUPPORTED_CONTENT = "unsupported_content"


class AnchorType(StrEnum):
    """The addressing scheme used by an :class:`~app.models.evidence.EvidenceAnchor`."""

    # Studio 5000 L5X
    L5X_CONTROLLER = "l5x_controller"
    L5X_PROGRAM = "l5x_program"
    L5X_ROUTINE = "l5x_routine"
    L5X_RUNG = "l5x_rung"
    L5X_INSTRUCTION = "l5x_instruction"
    L5X_TAG = "l5x_tag"
    L5X_MODULE = "l5x_module"
    L5X_ADDON_INSTRUCTION = "l5x_addon_instruction"

    # Spreadsheets
    XLSX_WORKBOOK = "xlsx_workbook"
    XLSX_WORKSHEET = "xlsx_worksheet"
    XLSX_CELL = "xlsx_cell"
    XLSX_RANGE = "xlsx_range"

    # Delimited text
    CSV_ROW = "csv_row"
    CSV_CELL = "csv_cell"

    # Word processing
    DOCX_HEADING = "docx_heading"
    DOCX_PARAGRAPH = "docx_paragraph"
    DOCX_TABLE_CELL = "docx_table_cell"

    # PDF
    PDF_PAGE = "pdf_page"
    PDF_TEXT_RANGE = "pdf_text_range"

    # Generic XML
    XML_ELEMENT = "xml_element"

    # Fallbacks
    FILE_LINE = "file_line"
    FILE_SECTION = "file_section"
    FILE_WHOLE = "file_whole"


class FileKind(StrEnum):
    """Classification assigned during intake, driving parser selection."""

    L5X = "l5x"
    XLSX = "xlsx"
    CSV = "csv"
    DOCX = "docx"
    PDF = "pdf"
    XML = "xml"
    ZIP = "zip"
    TEXT = "text"
    UNKNOWN = "unknown"


class FileStatus(StrEnum):
    UPLOADED = "uploaded"
    PARSING = "parsing"
    PARSED = "parsed"
    PARSE_FAILED = "parse_failed"
    REJECTED = "rejected"
    DELETED = "deleted"


class DocumentRole(StrEnum):
    """The engineering purpose a file serves, as declared by the uploader or
    inferred during intake. Drives which agents care about the file."""

    CONTROL_LOGIC = "control_logic"
    INTERLOCK_SCHEDULE = "interlock_schedule"
    IO_LIST = "io_list"
    FUNCTIONAL_DESCRIPTION = "functional_description"
    TESTING_NOTES = "testing_notes"
    HMI_EXPORT = "hmi_export"
    ALARM_LIST = "alarm_list"
    OTHER = "other"
    UNCLASSIFIED = "unclassified"


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class ApprovalKind(StrEnum):
    """Why a human decision is required."""

    SAFETY_RECOMMENDATION = "safety_recommendation"
    EQUIPMENT_DAMAGE_RECOMMENDATION = "equipment_damage_recommendation"
    DESIGN_RECOMMENDATION = "design_recommendation"
    FINDING_RELEASE = "finding_release"
    REPORT_RELEASE = "report_release"


class RecommendationStatus(StrEnum):
    PROPOSED = "proposed"
    APPROVED = "approved"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


class ReportStatus(StrEnum):
    DRAFT = "draft"
    GENERATED = "generated"
    FAILED = "failed"


class TaskKind(StrEnum):
    """Identifies the handler that executes a task."""

    INTAKE = "intake"
    INVENTORY_FILES = "inventory_files"
    PARSE_FILE = "parse_file"
    BUILD_EVIDENCE_GRAPH = "build_evidence_graph"
    PLAN_PROJECT = "plan_project"
    REVIEW_PLC = "review_plc"
    REVIEW_INTERLOCKS = "review_interlocks"
    SENIOR_REVIEW = "senior_review"
    REQUEST_APPROVALS = "request_approvals"
    DELIVERY = "delivery"
    FINALIZE_PROJECT = "finalize_project"


class AgentKind(StrEnum):
    """Stable identifier for an agent definition."""

    PROJECT_MANAGER = "project_manager"
    PLC_REVIEW = "plc_review"
    INTERLOCK_REVIEW = "interlock_review"
    SENIOR_REVIEW = "senior_review"
    DELIVERY = "delivery"


class MessageRole(StrEnum):
    """Author of a persisted, human-readable agent message.

    Note: this is *not* a transcript of model reasoning. Only concise,
    reviewable statements are stored.
    """

    AGENT = "agent"
    SYSTEM = "system"
    HUMAN = "human"


class ProviderKind(StrEnum):
    MOCK = "mock"
    OLLAMA = "ollama"
    OPENAI = "openai"
    ANTHROPIC = "anthropic"


class ActionType(StrEnum):
    """The closed set of actions a model is permitted to propose.

    Anything outside this set is rejected by the backend before execution.
    """

    CREATE_FINDING = "create_finding"
    CREATE_RECOMMENDATION = "create_recommendation"
    CREATE_TASK = "create_task"
    POST_MESSAGE = "post_message"
    REQUEST_APPROVAL = "request_approval"
    UPDATE_FINDING_REVIEW = "update_finding_review"
    REPORT_BLOCKER = "report_blocker"
    REPORT_NO_FINDINGS = "report_no_findings"


class AuditAction(StrEnum):
    """Auditable operations. Every member must be written by exactly one call
    site so the audit trail stays interpretable."""

    USER_CREATED = "user.created"
    USER_UPDATED = "user.updated"
    USER_LOGIN_SUCCEEDED = "user.login_succeeded"
    USER_LOGIN_FAILED = "user.login_failed"
    USER_LOGOUT = "user.logout"
    PERMISSION_CHANGED = "permission.changed"

    PROJECT_CREATED = "project.created"
    PROJECT_UPDATED = "project.updated"
    PROJECT_ARCHIVED = "project.archived"

    FILE_UPLOADED = "file.uploaded"
    FILE_REJECTED = "file.rejected"
    FILE_DELETED = "file.deleted"

    ANALYSIS_STARTED = "analysis.started"
    ANALYSIS_PAUSED = "analysis.paused"
    ANALYSIS_RESUMED = "analysis.resumed"
    ANALYSIS_CANCELLED = "analysis.cancelled"

    AGENT_ACTION = "agent.action"
    AGENT_ACTION_REJECTED = "agent.action_rejected"

    FINDING_CREATED = "finding.created"
    FINDING_UPDATED = "finding.updated"

    APPROVAL_REQUESTED = "approval.requested"
    APPROVAL_DECIDED = "approval.decided"

    REPORT_CREATED = "report.created"

    MODEL_CONFIG_CHANGED = "model_configuration.changed"


class EventType(StrEnum):
    """Event types broadcast over the project WebSocket.

    Persisted before broadcast; see ``contracts/events.schema.json``.
    """

    PROJECT_CREATED = "project.created"
    PROJECT_UPDATED = "project.updated"
    PROJECT_COMPLETED = "project.completed"
    PROJECT_FAILED = "project.failed"

    FILE_UPLOADED = "file.uploaded"
    FILE_PARSING_STARTED = "file.parsing_started"
    FILE_PARSED = "file.parsed"
    FILE_PARSE_FAILED = "file.parse_failed"
    FILE_DELETED = "file.deleted"

    WORKFLOW_STARTED = "workflow.started"
    WORKFLOW_PAUSED = "workflow.paused"
    WORKFLOW_RESUMED = "workflow.resumed"
    WORKFLOW_CANCELLED = "workflow.cancelled"

    TASK_CREATED = "task.created"
    TASK_STARTED = "task.started"
    TASK_COMPLETED = "task.completed"
    TASK_FAILED = "task.failed"
    TASK_BLOCKED = "task.blocked"

    AGENT_STATUS_CHANGED = "agent.status_changed"
    AGENT_LOCATION_CHANGED = "agent.location_changed"
    AGENT_MESSAGE_CREATED = "agent.message_created"

    FINDING_CREATED = "finding.created"
    FINDING_REVIEWED = "finding.reviewed"
    FINDING_UPDATED = "finding.updated"

    APPROVAL_REQUESTED = "approval.requested"
    APPROVAL_DECIDED = "approval.decided"

    REPORT_CREATED = "report.created"
