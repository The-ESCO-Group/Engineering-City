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
    """Project lifecycle.

    The first eight members predate the production platform and are kept because
    they are persisted in existing rows and published in the contract. Two of
    them are the brief's phases under earlier names: ``INGESTING`` is the
    brief's ``INTAKE``, and ``AWAITING_REVIEW`` is its ``HUMAN_APPROVAL``.
    Renaming either would break every stored row and the frontend, so the
    remaining phases are added alongside instead.
    """

    DRAFT = "draft"
    INGESTING = "ingesting"
    PLANNED = "planned"
    RUNNING = "running"
    AWAITING_REVIEW = "awaiting_review"
    COMPLETED = "completed"
    FAILED = "failed"
    ARCHIVED = "archived"

    # Production-platform phases.
    CLARIFICATION_REQUIRED = "clarification_required"
    TAG_AND_IO_DESIGN = "tag_and_io_design"
    PLC_ARCHITECTURE = "plc_architecture"
    IMPLEMENTATION = "implementation"
    CROSS_DISCIPLINE_REVIEW = "cross_discipline_review"
    TEST_DEVELOPMENT = "test_development"
    RELEASE_VALIDATION = "release_validation"
    READY_FOR_EXPORT = "ready_for_export"
    BLOCKED = "blocked"


#: Statuses from which no further workflow progress is possible without an
#: explicit operator action (restart, unarchive).
TERMINAL_PROJECT_STATUSES: frozenset[ProjectStatus] = frozenset(
    {ProjectStatus.COMPLETED, ProjectStatus.FAILED, ProjectStatus.ARCHIVED}
)

#: Engineering phases in the order the workflow moves through them. Used for
#: progress reporting; the workflow does not step blindly down this list, since
#: a clarification or a failed gate sends a project backwards.
PROJECT_PHASE_ORDER: tuple[ProjectStatus, ...] = (
    ProjectStatus.DRAFT,
    ProjectStatus.INGESTING,
    ProjectStatus.CLARIFICATION_REQUIRED,
    ProjectStatus.PLANNED,
    ProjectStatus.TAG_AND_IO_DESIGN,
    ProjectStatus.PLC_ARCHITECTURE,
    ProjectStatus.IMPLEMENTATION,
    ProjectStatus.CROSS_DISCIPLINE_REVIEW,
    ProjectStatus.TEST_DEVELOPMENT,
    ProjectStatus.AWAITING_REVIEW,
    ProjectStatus.RELEASE_VALIDATION,
    ProjectStatus.READY_FOR_EXPORT,
    ProjectStatus.COMPLETED,
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

    #: The basis is an approved ESCO template rule rather than a source document.
    TEMPLATE_RULE = "template_rule"
    #: The basis is a named human's recorded decision.
    HUMAN_DECISION = "human_decision"
    #: The basis is an object this platform generated, cited so a chain of
    #: derivation stays followable back to its own root.
    GENERATED_OBJECT = "generated_object"
    #: The basis is the output of a deterministic validator.
    VALIDATION_OUTPUT = "validation_output"


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

    # Non-file anchors: the basis for a statement is not always a document.
    DESCRIPTION_PARAGRAPH = "description_paragraph"
    REQUIREMENT = "requirement"
    TEMPLATE = "template"
    HUMAN_DECISION = "human_decision"
    GENERATED_OBJECT = "generated_object"
    VALIDATION_RESULT = "validation_result"

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
    CHANGES_REQUESTED = "changes_requested"
    #: The risk was read, understood and knowingly carried by a named engineer.
    #: Deliberately not folded into ``APPROVED``: a release that proceeded on
    #: accepted risk must not be indistinguishable from one with no risk.
    RISK_ACCEPTED = "risk_accepted"
    DEFERRED = "deferred"


class ApprovalDecision(StrEnum):
    """What a reviewer chose. Maps onto :class:`ApprovalStatus`."""

    APPROVE = "approve"
    REJECT = "reject"
    REQUEST_CHANGES = "request_changes"
    ACCEPT_RISK = "accept_risk"
    DEFER = "defer"


#: Decisions that oblige the reviewer to write down why.
DECISIONS_REQUIRING_COMMENT: frozenset[ApprovalDecision] = frozenset(
    {
        ApprovalDecision.REJECT,
        ApprovalDecision.REQUEST_CHANGES,
        ApprovalDecision.ACCEPT_RISK,
    }
)


class ApprovalKind(StrEnum):
    """Why a human decision is required."""

    SAFETY_RECOMMENDATION = "safety_recommendation"
    EQUIPMENT_DAMAGE_RECOMMENDATION = "equipment_damage_recommendation"
    DESIGN_RECOMMENDATION = "design_recommendation"
    FINDING_RELEASE = "finding_release"
    REPORT_RELEASE = "report_release"

    # Production pipeline.
    GATE_DECISION = "gate_decision"
    CHANGESET_MERGE = "changeset_merge"
    TAG_RENAME = "tag_rename"
    RESTRICTED_DOMAIN = "restricted_domain"
    RELEASE_CANDIDATE = "release_candidate"


class ApprovalGateKey(StrEnum):
    """The six configurable gates a project passes through."""

    PROJECT_PROFILE = "project_profile"
    DEVICES_IO_TAGS = "devices_io_tags"
    LOGIC_AND_DELIVERY = "logic_and_delivery"
    QA_AND_RISK = "qa_and_risk"
    IMPORT_VERIFICATION = "import_verification"
    RELEASE = "release"


#: Gate order. A gate cannot be evaluated while an earlier one is undecided.
GATE_ORDER: tuple[ApprovalGateKey, ...] = (
    ApprovalGateKey.PROJECT_PROFILE,
    ApprovalGateKey.DEVICES_IO_TAGS,
    ApprovalGateKey.LOGIC_AND_DELIVERY,
    ApprovalGateKey.QA_AND_RISK,
    ApprovalGateKey.IMPORT_VERIFICATION,
    ApprovalGateKey.RELEASE,
)


class GateStatus(StrEnum):
    NOT_READY = "not_ready"
    READY = "ready"
    AWAITING_DECISION = "awaiting_decision"
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"


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

    # Production pipeline.
    EXTRACT_REQUIREMENTS = "extract_requirements"
    RESOLVE_CLARIFICATIONS = "resolve_clarifications"
    DESIGN_TAGS = "design_tags"
    DESIGN_PLC_ARCHITECTURE = "design_plc_architecture"
    GENERATE_LOGIC = "generate_logic"
    VALIDATE_CROSS_DISCIPLINE = "validate_cross_discipline"
    EVALUATE_APPROVAL_GATE = "evaluate_approval_gate"
    GENERATE_RELEASE_CANDIDATE = "generate_release_candidate"
    VERIFY_IMPORT = "verify_import"
    ASSEMBLE_PACKAGE = "assemble_package"


class AgentKind(StrEnum):
    """Stable identifier for an agent definition."""

    PROJECT_MANAGER = "project_manager"
    PLC_REVIEW = "plc_review"
    INTERLOCK_REVIEW = "interlock_review"
    SENIOR_REVIEW = "senior_review"
    DELIVERY = "delivery"

    # Production pipeline.
    REQUIREMENTS_PLANNING = "requirements_planning"
    TAG_ARCHITECTURE = "tag_architecture"
    PLC_LOGIC = "plc_logic"


class SemanticLocation(StrEnum):
    """Where in the city an agent is working.

    The backend broadcasts these names and never pixel coordinates: where a
    district *is* on screen is the frontend's business, and coupling the
    backend to a layout would make moving a building a backend change.
    """

    PROJECT_INTAKE = "project_intake"
    REQUIREMENTS_HALL = "requirements_hall"
    PROJECT_CORE = "project_core"
    TAG_FOUNDRY = "tag_foundry"
    AUTOMATION_CORE = "automation_core"
    LADDER_LOGIC_LAB = "ladder_logic_lab"
    ELECTRICAL_WORKS = "electrical_works"
    HMI_DISTRICT = "hmi_district"
    ALARM_CENTER = "alarm_center"
    QA_CAMPUS = "qa_campus"
    TESTING_GROUNDS = "testing_grounds"
    APPROVAL_PLAZA = "approval_plaza"
    DOCUMENTATION_QUARTER = "documentation_quarter"
    RELEASE_LOGISTICS = "release_logistics"
    TEMPLATE_LIBRARY = "template_library"
    DATA_CENTER = "data_center"


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

    # Production pipeline. A model still only ever *proposes*: a change set is
    # a structured request that deterministic code validates and merges.
    CREATE_TASK_PLAN = "create_task_plan"
    REQUEST_CLARIFICATION = "request_clarification"
    PROPOSE_CHANGESET = "propose_changeset"
    REVIEW_CHANGESET = "review_changeset"
    GENERATE_PREVIEW = "generate_preview"
    COMPLETE_TASK = "complete_task"


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

    # Production pipeline.
    PROFILE_UPDATED = "profile.updated"
    QUESTION_CREATED = "question.created"
    QUESTION_ANSWERED = "question.answered"
    DECISION_RECORDED = "decision.recorded"
    CHANGESET_CREATED = "changeset.created"
    CHANGESET_SUBMITTED = "changeset.submitted"
    CHANGESET_MERGED = "changeset.merged"
    #: A reviewer turned the change set down. Distinct from abandonment: a
    #: rejection is a verdict on the work, abandonment is a decision to stop
    #: carrying it. Reserved for the review path; nothing writes it yet, and
    #: ``changesets.abandon`` deliberately no longer does.
    CHANGESET_REJECTED = "changeset.rejected"
    CHANGESET_ABANDONED = "changeset.abandoned"
    REVISION_CREATED = "revision.created"
    TAG_RENAMED = "tag.renamed"
    GATE_DECIDED = "gate.decided"
    VALIDATION_RUN = "validation.run"
    EXPORT_GENERATED = "export.generated"
    RELEASE_CANDIDATE_CREATED = "release.candidate_created"
    RELEASE_APPROVED = "release.approved"
    WORKSTATION_ADAPTER_CONFIGURED = "workstation.configured"
    WORKSTATION_VERIFICATION_REQUESTED = "workstation.verification_requested"


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

    # Production pipeline.
    PROJECT_QUESTION_CREATED = "project.question_created"
    PROJECT_QUESTION_RESOLVED = "project.question_resolved"
    REQUIREMENT_CREATED = "requirement.created"
    WORKFLOW_PLANNED = "workflow.planned"
    WORKFLOW_FAILED = "workflow.failed"
    TASK_ASSIGNED = "task.assigned"
    CHANGESET_CREATED = "changeset.created"
    CHANGESET_VALIDATION_FAILED = "changeset.validation_failed"
    CHANGESET_SUBMITTED = "changeset.submitted"
    CHANGESET_REVIEWED = "changeset.reviewed"
    #: Closed without ever being merged. Separate from ``CHANGESET_REVIEWED``
    #: because a client that reads the stream to answer "what happened to this
    #: proposal" cannot tell a verdict from a withdrawal if both arrive under
    #: the same type.
    CHANGESET_ABANDONED = "changeset.abandoned"
    CHANGESET_MERGED = "changeset.merged"
    REVISION_CREATED = "revision.created"
    GATE_STATUS_CHANGED = "gate.status_changed"
    VALIDATION_STARTED = "validation.started"
    VALIDATION_COMPLETED = "validation.completed"
    EXPORT_STARTED = "export.started"
    EXPORT_ARTIFACT_CREATED = "export.artifact_created"
    RELEASE_CANDIDATE_CREATED = "release.candidate_created"
    RELEASE_READY = "release.ready"


# ===========================================================================
# Engineering production platform
# ===========================================================================
# Everything below describes the canonical engineering model rather than the
# review pipeline. Same rule applies: these values are published contract, so
# members are added and never renamed.


class ProjectType(StrEnum):
    NEW_INSTALLATION = "new_installation"
    UPGRADE = "upgrade"
    RETROFIT = "retrofit"
    MIGRATION = "migration"
    EXPANSION = "expansion"
    OTHER = "other"


class ControllerFamily(StrEnum):
    COMPACTLOGIX = "compactlogix"
    CONTROLLOGIX = "controllogix"
    GUARDLOGIX = "guardlogix"
    COMPACT_GUARDLOGIX = "compact_guardlogix"
    MICRO800 = "micro800"
    SLC500 = "slc500"
    PLC5 = "plc5"
    OTHER = "other"


class SafetyClassification(StrEnum):
    """How a controller is classified for safety purposes.

    Anything other than ``STANDARD`` puts the project into manual-review-only
    mode unless an administrator has enabled a qualified workflow.
    """

    STANDARD = "standard"
    SAFETY_RATED = "safety_rated"
    MIXED_STANDARD_AND_SAFETY = "mixed_standard_and_safety"
    UNKNOWN = "unknown"


class RestrictedDomain(StrEnum):
    """Engineering domains this platform will not generate for by default.

    Each is a domain where a plausible-looking generated artifact could cause
    injury or major equipment damage, and where the competence to review it is
    a formal qualification rather than general controls experience.
    """

    MOTION = "motion"
    SAFETY_LOGIC = "safety_logic"
    BURNER_MANAGEMENT = "burner_management"
    PROCESS_SAFETY = "process_safety"
    HIGH_ENERGY = "high_energy"
    ELECTRICAL_POWER_DISTRIBUTION = "electrical_power_distribution"
    ROBOTICS = "robotics"


class RestrictedDomainMode(StrEnum):
    """What the platform is permitted to do in a restricted domain."""

    #: Refuse the work outright and create a clarification.
    BLOCKED = "blocked"
    #: Read and report, but generate nothing.
    MANUAL_REVIEW_ONLY = "manual_review_only"
    #: An administrator explicitly enabled a qualified workflow.
    QUALIFIED_WORKFLOW_ENABLED = "qualified_workflow_enabled"


class HmiProduct(StrEnum):
    FACTORYTALK_VIEW_ME = "factorytalk_view_me"
    FACTORYTALK_VIEW_SE = "factorytalk_view_se"
    FACTORYTALK_OPTIX = "factorytalk_optix"
    PANELVIEW_800 = "panelview_800"
    NONE = "none"
    OTHER = "other"


class ReleaseMode(StrEnum):
    """How far this project intends to take generation."""

    #: Produce import-ready candidate files only. The default, and the only mode
    #: that needs no licensed Rockwell workstation.
    IMPORT_PACKAGE_ONLY = "import_package_only"
    #: Additionally verify the import on a configured, licensed workstation.
    ROCKWELL_WORKSTATION_ASSEMBLY = "rockwell_workstation_assembly"
    #: Generate nothing; review and report only.
    REVIEW_ONLY = "review_only"


class UnitSystem(StrEnum):
    METRIC = "metric"
    IMPERIAL = "imperial"
    MIXED = "mixed"


class ProfileFieldStatus(StrEnum):
    """Why a required profile field is not usable yet."""

    PRESENT = "present"
    MISSING = "missing"
    AMBIGUOUS = "ambiguous"
    UNSUPPORTED = "unsupported"


# ---------------------------------------------------------------------------
# Requirements
# ---------------------------------------------------------------------------
class RequirementKind(StrEnum):
    FUNCTIONAL = "functional"
    PERFORMANCE = "performance"
    SAFETY = "safety"
    INTERFACE = "interface"
    OPERATIONAL = "operational"
    REGULATORY = "regulatory"
    CONSTRAINT = "constraint"
    OTHER = "other"


class RequirementStatus(StrEnum):
    PROPOSED = "proposed"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"
    IMPLEMENTED = "implemented"
    VERIFIED = "verified"


class RequirementPriority(StrEnum):
    MANDATORY = "mandatory"
    EXPECTED = "expected"
    OPTIONAL = "optional"


class ClarificationStatus(StrEnum):
    OPEN = "open"
    ANSWERED = "answered"
    DEFERRED = "deferred"
    DISMISSED = "dismissed"
    OBSOLETE = "obsolete"


class QuestionCategory(StrEnum):
    """What a clarification is about. Drives who should answer it."""

    TARGET_PLATFORM = "target_platform"
    REQUIREMENT_AMBIGUITY = "requirement_ambiguity"
    SCOPE = "scope"
    DEVICE_OR_EQUIPMENT = "device_or_equipment"
    IO_ASSIGNMENT = "io_assignment"
    TAG_NAMING = "tag_naming"
    INTERLOCK = "interlock"
    SEQUENCE_OR_MODE = "sequence_or_mode"
    ALARM = "alarm"
    HMI = "hmi"
    TESTING = "testing"
    TEMPLATE_SELECTION = "template_selection"
    RESTRICTED_DOMAIN = "restricted_domain"
    OTHER = "other"


class DecisionKind(StrEnum):
    """A recorded human engineering decision, citable as evidence."""

    PROFILE_SELECTION = "profile_selection"
    TEMPLATE_SELECTION = "template_selection"
    NAMING_STANDARD = "naming_standard"
    CLARIFICATION_ANSWER = "clarification_answer"
    SCOPE_DECISION = "scope_decision"
    RISK_ACCEPTANCE = "risk_acceptance"
    DESIGN_CHOICE = "design_choice"
    OTHER = "other"


# ---------------------------------------------------------------------------
# Plant, devices and I/O
# ---------------------------------------------------------------------------
class DeviceCategory(StrEnum):
    MOTOR = "motor"
    VFD = "vfd"
    SERVO = "servo"
    VALVE = "valve"
    CYLINDER = "cylinder"
    PUMP = "pump"
    CONVEYOR = "conveyor"
    PHOTOEYE = "photoeye"
    PROXIMITY_SENSOR = "proximity_sensor"
    LIMIT_SWITCH = "limit_switch"
    PRESSURE_TRANSMITTER = "pressure_transmitter"
    TEMPERATURE_TRANSMITTER = "temperature_transmitter"
    LEVEL_TRANSMITTER = "level_transmitter"
    FLOW_TRANSMITTER = "flow_transmitter"
    ENCODER = "encoder"
    ESTOP = "estop"
    GUARD_SWITCH = "guard_switch"
    LIGHT_CURTAIN = "light_curtain"
    PUSHBUTTON = "pushbutton"
    SELECTOR_SWITCH = "selector_switch"
    INDICATOR = "indicator"
    HORN = "horn"
    OTHER = "other"


class SignalType(StrEnum):
    DISCRETE_INPUT = "discrete_input"
    DISCRETE_OUTPUT = "discrete_output"
    ANALOG_INPUT = "analog_input"
    ANALOG_OUTPUT = "analog_output"
    NETWORK_INPUT = "network_input"
    NETWORK_OUTPUT = "network_output"
    #: Exists only in logic; has no terminal.
    VIRTUAL = "virtual"


class SignalDirection(StrEnum):
    INPUT = "input"
    OUTPUT = "output"
    BIDIRECTIONAL = "bidirectional"


class FailState(StrEnum):
    """What a signal does when its source is lost.

    ``UNDEFINED`` is a first-class value, not a null. A device whose fail state
    nobody wrote down is a documented gap that a validator reports; silently
    defaulting it to something safe-sounding would invent an engineering
    decision nobody made.
    """

    DE_ENERGIZE = "de_energize"
    ENERGIZE = "energize"
    HOLD_LAST_STATE = "hold_last_state"
    GO_TO_VALUE = "go_to_value"
    UNDEFINED = "undefined"


class NetworkProtocol(StrEnum):
    ETHERNET_IP = "ethernet_ip"
    CONTROLNET = "controlnet"
    DEVICENET = "devicenet"
    POINT_IO_BACKPLANE = "point_io_backplane"
    LOCAL_BACKPLANE = "local_backplane"
    HARDWIRED = "hardwired"
    OTHER = "other"


class ModuleRole(StrEnum):
    CONTROLLER = "controller"
    LOCAL_IO = "local_io"
    REMOTE_IO = "remote_io"
    COMMUNICATION = "communication"
    POWER_SUPPLY = "power_supply"
    DRIVE = "drive"
    SAFETY = "safety"
    OTHER = "other"


class AddressAssignmentStatus(StrEnum):
    PROPOSED = "proposed"
    ASSIGNED = "assigned"
    CONFLICTED = "conflicted"
    SPARE = "spare"
    RESERVED = "reserved"


# ---------------------------------------------------------------------------
# Tags
# ---------------------------------------------------------------------------
class TagScope(StrEnum):
    CONTROLLER = "controller"
    PROGRAM = "program"
    ADDON_INSTRUCTION = "addon_instruction"


class TagRole(StrEnum):
    """What a tag is *for*. Drives naming, HMI exposure and validation."""

    COMMAND = "command"
    STATUS = "status"
    FEEDBACK = "feedback"
    PERMISSIVE = "permissive"
    INTERLOCK = "interlock"
    ALARM = "alarm"
    MODE = "mode"
    SEQUENCE = "sequence"
    ANALOG_PROCESS_VALUE = "analog_process_value"
    SETPOINT = "setpoint"
    CONFIGURATION = "configuration"
    DIAGNOSTIC = "diagnostic"
    IO_ALIAS = "io_alias"
    HMI_INTERFACE = "hmi_interface"
    OTHER = "other"


class BaseDataType(StrEnum):
    """Atomic Logix data types, plus the two structured forms.

    ``UDT`` and ``ADDON_INSTRUCTION`` carry the concrete type name in a separate
    field, because that name is project data rather than contract vocabulary.
    """

    BOOL = "BOOL"
    SINT = "SINT"
    INT = "INT"
    DINT = "DINT"
    LINT = "LINT"
    USINT = "USINT"
    UINT = "UINT"
    UDINT = "UDINT"
    ULINT = "ULINT"
    REAL = "REAL"
    LREAL = "LREAL"
    STRING = "STRING"
    TIMER = "TIMER"
    COUNTER = "COUNTER"
    CONTROL = "CONTROL"
    MESSAGE = "MESSAGE"
    UDT = "UDT"
    ADDON_INSTRUCTION = "ADDON_INSTRUCTION"
    UNKNOWN = "UNKNOWN"


class TagValidationCode(StrEnum):
    """Stable codes for tag-level validation failures."""

    INVALID_IDENTIFIER = "invalid_identifier"
    RESERVED_WORD = "reserved_word"
    NAME_TOO_LONG = "name_too_long"
    DUPLICATE_NAME = "duplicate_name"
    SCOPE_COLLISION = "scope_collision"
    NAMING_STANDARD_VIOLATION = "naming_standard_violation"
    DATA_TYPE_MISMATCH = "data_type_mismatch"
    UNKNOWN_DATA_TYPE = "unknown_data_type"
    MISSING_DESCRIPTION = "missing_description"
    MISSING_UNITS = "missing_units"
    ORPHANED_TAG = "orphaned_tag"
    STALE_ALIAS = "stale_alias"
    ALIAS_TARGET_MISSING = "alias_target_missing"


# ---------------------------------------------------------------------------
# PLC organisation and logic
# ---------------------------------------------------------------------------
class ControllerTaskType(StrEnum):
    CONTINUOUS = "continuous"
    PERIODIC = "periodic"
    EVENT = "event"


class ProgramType(StrEnum):
    NORMAL = "normal"
    SAFETY = "safety"
    EQUIPMENT_PHASE = "equipment_phase"


class RoutineLanguage(StrEnum):
    LADDER = "ladder"
    STRUCTURED_TEXT = "structured_text"
    FUNCTION_BLOCK = "function_block"
    SEQUENTIAL_FUNCTION_CHART = "sequential_function_chart"


class InstructionCategory(StrEnum):
    BIT = "bit"
    TIMER_COUNTER = "timer_counter"
    COMPARE = "compare"
    COMPUTE_MATH = "compute_math"
    MOVE_LOGICAL = "move_logical"
    PROGRAM_CONTROL = "program_control"
    ADDON_INSTRUCTION = "addon_instruction"
    SPECIAL = "special"
    UNSUPPORTED = "unsupported"


class OperandKind(StrEnum):
    TAG = "tag"
    LITERAL = "literal"
    EXPRESSION = "expression"
    ROUTINE_REFERENCE = "routine_reference"
    #: The operand names something that cannot be resolved. Kept explicitly so
    #: an unresolved reference is a reportable state rather than a silent gap.
    UNRESOLVED = "unresolved"


class LogicElementKind(StrEnum):
    """Position of an element within a rung."""

    CONDITION = "condition"
    OUTPUT = "output"
    BRANCH_START = "branch_start"
    BRANCH_LEVEL = "branch_level"
    BRANCH_END = "branch_end"


class LogicOrigin(StrEnum):
    """Where a piece of logic came from. Never inferred after the fact."""

    PARSED_FROM_SOURCE = "parsed_from_source"
    TEMPLATE_EXPANSION = "template_expansion"
    AGENT_PROPOSED = "agent_proposed"
    HUMAN_AUTHORED = "human_authored"


# ---------------------------------------------------------------------------
# Interlocks, modes and sequences
# ---------------------------------------------------------------------------
class InterlockKind(StrEnum):
    PERMISSIVE = "permissive"
    INTERLOCK = "interlock"
    TRIP = "trip"
    ALARM_ONLY = "alarm_only"


class InterlockAction(StrEnum):
    STOP_IMMEDIATELY = "stop_immediately"
    CONTROLLED_STOP = "controlled_stop"
    PREVENT_START = "prevent_start"
    INHIBIT_OUTPUT = "inhibit_output"
    ANNUNCIATE_ONLY = "annunciate_only"
    UNDEFINED = "undefined"


class ResetBehaviorKind(StrEnum):
    AUTOMATIC = "automatic"
    MANUAL_LOCAL = "manual_local"
    MANUAL_HMI = "manual_hmi"
    POWER_CYCLE = "power_cycle"
    UNDEFINED = "undefined"


class ModeKind(StrEnum):
    MANUAL = "manual"
    AUTOMATIC = "automatic"
    SEMI_AUTOMATIC = "semi_automatic"
    MAINTENANCE = "maintenance"
    CLEANING = "cleaning"
    SETUP = "setup"
    OFF = "off"
    OTHER = "other"


class FaultClass(StrEnum):
    EQUIPMENT = "equipment"
    PROCESS = "process"
    SAFETY = "safety"
    COMMUNICATION = "communication"
    DIAGNOSTIC = "diagnostic"
    OPERATOR = "operator"


class AlarmSeverity(StrEnum):
    DIAGNOSTIC = "diagnostic"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    URGENT = "urgent"


# ---------------------------------------------------------------------------
# Change sets, revisions and merge
# ---------------------------------------------------------------------------
class ObjectType(StrEnum):
    """Stable names for canonical objects.

    Used wherever a reference has to be polymorphic — evidence links, change
    operations, impact analysis. A string rather than a foreign key per target
    so that adding an object type does not require a schema change everywhere
    it could be cited.
    """

    PROJECT_PROFILE = "project_profile"
    PROJECT_QUESTION = "project_question"
    PROJECT_DECISION = "project_decision"
    REQUIREMENT = "requirement"
    ACCEPTANCE_CRITERION = "acceptance_criterion"
    ASSUMPTION = "assumption"
    CONSTRAINT = "constraint"
    CLARIFICATION = "clarification"
    AREA = "area"
    UNIT = "unit"
    EQUIPMENT_MODULE = "equipment_module"
    DEVICE = "device"
    DEVICE_TYPE = "device_type"
    NETWORK_NODE = "network_node"
    MODULE = "module"
    IO_POINT = "io_point"
    SIGNAL = "signal"
    ADDRESS_ASSIGNMENT = "address_assignment"
    TAG = "tag"
    TAG_ALIAS = "tag_alias"
    TAG_GROUP = "tag_group"
    DATA_TYPE_DEFINITION = "data_type_definition"
    UDT_DEFINITION = "udt_definition"
    AOI_REFERENCE = "aoi_reference"
    CONTROLLER_TASK = "controller_task"
    PROGRAM = "program"
    ROUTINE = "routine"
    LOGIC_NETWORK = "logic_network"
    LOGIC_RUNG = "logic_rung"
    LOGIC_INSTRUCTION = "logic_instruction"
    INTERLOCK = "interlock"
    PERMISSIVE = "permissive"
    CAUSE_EFFECT_ENTRY = "cause_effect_entry"
    MODE_DEFINITION = "mode_definition"
    STATE_DEFINITION = "state_definition"
    TRANSITION_DEFINITION = "transition_definition"
    SEQUENCE_STEP = "sequence_step"
    FAULT_DEFINITION = "fault_definition"
    RESET_BEHAVIOR = "reset_behavior"
    HMI_SCREEN = "hmi_screen"
    HMI_BINDING = "hmi_binding"
    ALARM_DEFINITION = "alarm_definition"
    TEST_CASE = "test_case"
    FINDING = "finding"
    EXPORT_ARTIFACT = "export_artifact"
    RELEASE_CANDIDATE = "release_candidate"


#: Object kinds that may not exist without a stated basis.
#:
#: The traceability promise made concrete. A rung, tag, alarm, HMI binding or
#: test case that exists for no stated reason is exactly the failure the product
#: is meant to prevent, so both the action schema and the merge validator refuse
#: one. Lives here rather than beside the ORM so that the rule can be enforced
#: at the action boundary without the agent layer importing the database.
TRACEABLE_OBJECT_TYPES: frozenset[ObjectType] = frozenset(
    {
        ObjectType.TAG,
        ObjectType.LOGIC_RUNG,
        ObjectType.ROUTINE,
        ObjectType.PROGRAM,
        ObjectType.INTERLOCK,
        ObjectType.PERMISSIVE,
        ObjectType.ALARM_DEFINITION,
        ObjectType.HMI_BINDING,
        ObjectType.HMI_SCREEN,
        ObjectType.TEST_CASE,
        ObjectType.DEVICE,
        ObjectType.IO_POINT,
        ObjectType.UDT_DEFINITION,
    }
)

#: Evidence kinds that satisfy the traceability rule. A parser warning is
#: evidence *about* a document; it is not a reason for a rung to exist.
ACCEPTABLE_BASIS_KINDS: frozenset[EvidenceKind] = frozenset(
    {
        EvidenceKind.DIRECT,
        EvidenceKind.INFERENCE,
        EvidenceKind.TEMPLATE_RULE,
        EvidenceKind.HUMAN_DECISION,
        EvidenceKind.GENERATED_OBJECT,
    }
)


class ObjectApprovalState(StrEnum):
    """How settled a canonical object is.

    ``APPROVED`` is what makes an object protected: changing it afterwards
    requires an explicit supersede path and a fresh approval, so an agent
    cannot quietly edit something an engineer signed off.
    """

    DRAFT = "draft"
    PROPOSED = "proposed"
    APPROVED = "approved"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


class ChangeSetStatus(StrEnum):
    DRAFT = "draft"
    SUBMITTED = "submitted"
    VALIDATING = "validating"
    VALIDATION_FAILED = "validation_failed"
    IN_REVIEW = "in_review"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    MERGED = "merged"
    CONFLICTED = "conflicted"
    SUPERSEDED = "superseded"
    ABANDONED = "abandoned"


class ChangeOperationType(StrEnum):
    CREATE_OBJECT = "create_object"
    UPDATE_FIELD = "update_field"
    DELETE_OBJECT = "delete_object"
    LINK_OBJECTS = "link_objects"
    UNLINK_OBJECTS = "unlink_objects"
    RENAME_TAG = "rename_tag"
    ASSIGN_IO = "assign_io"
    CREATE_LOGIC = "create_logic"
    UPDATE_LOGIC = "update_logic"
    CREATE_FINDING = "create_finding"
    RESOLVE_FINDING = "resolve_finding"
    CREATE_EXPORT = "create_export"
    REQUEST_CLARIFICATION = "request_clarification"


class MergeOutcome(StrEnum):
    MERGED = "merged"
    DRY_RUN_OK = "dry_run_ok"
    CONFLICT = "conflict"
    VALIDATION_FAILED = "validation_failed"
    APPROVAL_REQUIRED = "approval_required"
    NO_OP = "no_op"
    REFUSED = "refused"


class ConflictKind(StrEnum):
    """Why a change set could not be merged."""

    STALE_REVISION = "stale_revision"
    CONCURRENT_FIELD_EDIT = "concurrent_field_edit"
    OBJECT_DELETED = "object_deleted"
    OBJECT_MISSING = "object_missing"
    PROTECTED_OBJECT = "protected_object"
    DUPLICATE_KEY = "duplicate_key"
    DEPENDENCY_MISSING = "dependency_missing"
    SCOPE_VIOLATION = "scope_violation"
    RESTRICTED_DOMAIN = "restricted_domain"


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------
class TemplateCategory(StrEnum):
    CONTROLLER_PROJECT = "controller_project"
    PROGRAM_LAYOUT = "program_layout"
    MOTOR_CONTROL = "motor_control"
    VFD_CONTROL = "vfd_control"
    VALVE_CONTROL = "valve_control"
    ANALOG_INPUT_SCALING = "analog_input_scaling"
    PERMISSIVE_INTERLOCK = "permissive_interlock"
    FAULT_RESET = "fault_reset"
    MODE_SELECTION = "mode_selection"
    SEQUENCE_STATE_MACHINE = "sequence_state_machine"
    COMMUNICATION_LOSS = "communication_loss"
    DIAGNOSTIC = "diagnostic"
    UDT = "udt"
    ADDON_INSTRUCTION = "addon_instruction"


class TemplateApprovalLevel(StrEnum):
    """Who has to sign off logic produced from a template."""

    #: Generated logic still needs the normal engineering gate, nothing more.
    STANDARD = "standard"
    #: A senior engineer must review every expansion of this template.
    SENIOR_REVIEW_REQUIRED = "senior_review_required"
    #: Restricted domain: qualified sign-off only.
    QUALIFIED_REVIEW_REQUIRED = "qualified_review_required"


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
class ValidationSeverity(StrEnum):
    ERROR = "error"
    WARNING = "warning"
    INFORMATION = "information"
    UNSUPPORTED = "unsupported"
    HUMAN_REVIEW_REQUIRED = "human_review_required"


#: Severities that stop a release candidate from being generated at all.
BLOCKING_VALIDATION_SEVERITIES: frozenset[ValidationSeverity] = frozenset(
    {ValidationSeverity.ERROR}
)


class ValidationDiscipline(StrEnum):
    REQUIREMENTS = "requirements"
    TAGS = "tags"
    IO = "io"
    PLC_LOGIC = "plc_logic"
    INTERLOCKS = "interlocks"
    HMI = "hmi"
    ALARMS = "alarms"
    TESTING = "testing"
    DOCUMENTATION = "documentation"
    RELEASE = "release"
    CROSS_DISCIPLINE = "cross_discipline"


class ValidationRunStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


# ---------------------------------------------------------------------------
# Release
# ---------------------------------------------------------------------------
class ValidationLevel(StrEnum):
    """How far an artifact has actually been proved.

    Strictly ordered, and never collapsed to a boolean. Everything this backend
    can do offline stops at ``ROUND_TRIP_PARSED``: we can prove our own output
    parses back into the same model, which is not the same as proving Studio
    5000 will import it.
    """

    GENERATED = "generated"
    SCHEMA_VALIDATED = "schema_validated"
    INTERNAL_SEMANTIC_VALIDATED = "internal_semantic_validated"
    ROUND_TRIP_PARSED = "round_trip_parsed"
    ROCKWELL_IMPORT_VERIFIED = "rockwell_import_verified"
    HUMAN_APPROVED = "human_approved"
    RELEASED = "released"


#: Ordering, so "at least X" is a comparison rather than a hand-written table.
VALIDATION_LEVEL_ORDER: dict[ValidationLevel, int] = {
    ValidationLevel.GENERATED: 0,
    ValidationLevel.SCHEMA_VALIDATED: 1,
    ValidationLevel.INTERNAL_SEMANTIC_VALIDATED: 2,
    ValidationLevel.ROUND_TRIP_PARSED: 3,
    ValidationLevel.ROCKWELL_IMPORT_VERIFIED: 4,
    ValidationLevel.HUMAN_APPROVED: 5,
    ValidationLevel.RELEASED: 6,
}

#: The highest level reachable without a licensed Rockwell workstation. Named
#: so the limit is stated in one place rather than assumed at each call site.
MAX_OFFLINE_VALIDATION_LEVEL: ValidationLevel = ValidationLevel.ROUND_TRIP_PARSED


class ExportArtifactKind(StrEnum):
    CONTROLLER_PROJECT_L5X = "controller_project_l5x"
    PROGRAM_L5X = "program_l5x"
    ROUTINE_L5X = "routine_l5x"
    UDT_L5X = "udt_l5x"
    AOI_L5X = "aoi_l5x"
    TAG_CSV = "tag_csv"
    COMMENT_CSV = "comment_csv"
    CROSS_REFERENCE_CSV = "cross_reference_csv"
    IMPORT_ORDER = "import_order"
    LIMITATIONS = "limitations"
    TARGET_PROFILE = "target_profile"
    VALIDATION_SUMMARY = "validation_summary"
    MANIFEST = "manifest"
    CHECKSUMS = "checksums"
    CHANGE_LOG = "change_log"
    APPROVAL_LOG = "approval_log"
    EVIDENCE_INDEX = "evidence_index"
    # Produced by partner-owned delivery plugins.
    HMI_DISPLAY_XML = "hmi_display_xml"
    HMI_TAG_CSV = "hmi_tag_csv"
    ALARM_IMPORT = "alarm_import"
    TEST_PLAN = "test_plan"
    REPORT = "report"


class ReleaseCandidateStatus(StrEnum):
    DRAFT = "draft"
    GENERATING = "generating"
    GENERATED = "generated"
    VALIDATION_FAILED = "validation_failed"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    RELEASED = "released"
    SUPERSEDED = "superseded"
    FAILED = "failed"


class ImportDependencyKind(StrEnum):
    """Why one artifact has to be imported before another."""

    REQUIRES_DATA_TYPE = "requires_data_type"
    REQUIRES_ADDON_INSTRUCTION = "requires_addon_instruction"
    REQUIRES_TAG = "requires_tag"
    REQUIRES_MODULE = "requires_module"
    REQUIRES_PROGRAM = "requires_program"
    ORDERED_AFTER = "ordered_after"


class WorkstationAdapterStatus(StrEnum):
    """State of the optional licensed Rockwell workstation adapter."""

    #: The default. No adapter is configured and none will be contacted.
    DISABLED = "disabled"
    NOT_CONFIGURED = "not_configured"
    UNREACHABLE = "unreachable"
    READY = "ready"
    BUSY = "busy"
    ERROR = "error"


class ImportVerificationStatus(StrEnum):
    NOT_ATTEMPTED = "not_attempted"
    UNAVAILABLE = "unavailable"
    QUEUED = "queued"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"
