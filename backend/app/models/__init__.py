"""SQLAlchemy ORM entities.

Importing this package registers every table on ``Base.metadata``, which Alembic
autogeneration and ``create_all`` both depend on. Import it, not the individual
modules, when you need the full schema.

Two layers live here:

* the **review platform** — projects, files, evidence anchors, tasks, agents,
  findings, approvals, reports, audit and events;
* the **canonical engineering model** — profile and target platform,
  requirements, plant and I/O, tags, PLC organisation and logic IR, interlocks
  and sequences, the delivery-domain placeholders, and the revision/change-set
  machinery every one of them is written through.

The second layer never bypasses the first: a generated object cites evidence
from a parsed file, and both are versioned by the same revisions.
"""

from __future__ import annotations

from app.database.base import Base
from app.models.agent import (
    CORE_AGENT_KINDS,
    AgentDefinition,
    AgentInstance,
    AgentMessage,
    AgentRun,
    ModelConfiguration,
    UsageRecord,
)
from app.models.approval import Approval
from app.models.audit import AuditEntry
from app.models.core import (
    ACCEPTABLE_BASIS_KINDS,
    TRACEABLE_OBJECT_TYPES,
    EngineeringObjectMixin,
    ObjectEvidenceLink,
)
from app.models.delivery import (
    AlarmDefinition,
    AlarmMessage,
    HMIBinding,
    HMINavigation,
    HMIObjectInstance,
    HMIScreen,
    ReportDefinition,
    TestCase,
    TestStep,
)
from app.models.event import Event
from app.models.evidence import MAX_EXCERPT_CHARS, EvidenceAnchor
from app.models.file import ParsedArtifact, UploadedFile
from app.models.finding import Finding, FindingComment, FindingEvidence, Recommendation
from app.models.interlocks import (
    CauseEffectEntry,
    FaultDefinition,
    Interlock,
    ModeDefinition,
    Permissive,
    ResetBehavior,
    SequenceStep,
    StateDefinition,
    TransitionDefinition,
)
from app.models.plant import (
    AddressAssignment,
    Area,
    Device,
    DeviceTemplate,
    DeviceType,
    EquipmentModule,
    HardwareModule,
    IOPoint,
    NetworkNode,
    PlantUnit,
    Signal,
)
from app.models.plc import (
    ControllerModel,
    ControllerTask,
    LogicBranch,
    LogicComment,
    LogicInstruction,
    LogicNetwork,
    LogicOperand,
    LogicReference,
    LogicRung,
    Program,
    Routine,
)
from app.models.profile import (
    REQUIRED_PROFILE_FIELDS,
    EngineeringStandardSet,
    ProjectDecision,
    ProjectProfile,
    ProjectQuestion,
    TargetPlatform,
    TemplateSet,
)
from app.models.project import Project, ProjectMember
from app.models.release import (
    ExportArtifact,
    ExportProfile,
    ImportDependency,
    ReleaseCandidate,
    ReleaseManifest,
    ValidationResult,
    ValidationRun,
    WorkstationVerificationJob,
)
from app.models.report import Report
from app.models.requirements import (
    AcceptanceCriterion,
    Assumption,
    Clarification,
    DesignConstraint,
    Requirement,
    RequirementSource,
)
from app.models.revision import (
    ApprovalGate,
    ChangeOperation,
    ChangeSet,
    ImpactReport,
    MergeConflict,
    MergeResult,
    ProjectRevision,
)
from app.models.tags import (
    AOIDefinitionReference,
    DataTypeDefinition,
    Tag,
    TagAlias,
    TagGroup,
    TagGroupMember,
    TagNamingRule,
    TagValidationResult,
    UDTDefinition,
    UDTMember,
)
from app.models.task import Task, TaskDependency
from app.models.user import RevokedToken, Role, User, user_roles

__all__ = [
    "ACCEPTABLE_BASIS_KINDS",
    "CORE_AGENT_KINDS",
    "MAX_EXCERPT_CHARS",
    "REQUIRED_PROFILE_FIELDS",
    "TRACEABLE_OBJECT_TYPES",
    "AOIDefinitionReference",
    "AcceptanceCriterion",
    "AddressAssignment",
    "AgentDefinition",
    "AgentInstance",
    "AgentMessage",
    "AgentRun",
    "AlarmDefinition",
    "AlarmMessage",
    "Approval",
    "ApprovalGate",
    "Area",
    "Assumption",
    "AuditEntry",
    "Base",
    "CauseEffectEntry",
    "ChangeOperation",
    "ChangeSet",
    "Clarification",
    "ControllerModel",
    "ControllerTask",
    "DataTypeDefinition",
    "DesignConstraint",
    "Device",
    "DeviceTemplate",
    "DeviceType",
    "EngineeringObjectMixin",
    "EngineeringStandardSet",
    "EquipmentModule",
    "Event",
    "EvidenceAnchor",
    "ExportArtifact",
    "ExportProfile",
    "FaultDefinition",
    "Finding",
    "FindingComment",
    "FindingEvidence",
    "HMIBinding",
    "HMINavigation",
    "HMIObjectInstance",
    "HMIScreen",
    "HardwareModule",
    "IOPoint",
    "ImpactReport",
    "ImportDependency",
    "Interlock",
    "LogicBranch",
    "LogicComment",
    "LogicInstruction",
    "LogicNetwork",
    "LogicOperand",
    "LogicReference",
    "LogicRung",
    "MergeConflict",
    "MergeResult",
    "ModeDefinition",
    "ModelConfiguration",
    "NetworkNode",
    "ObjectEvidenceLink",
    "ParsedArtifact",
    "Permissive",
    "PlantUnit",
    "Program",
    "Project",
    "ProjectDecision",
    "ProjectMember",
    "ProjectProfile",
    "ProjectQuestion",
    "ProjectRevision",
    "Recommendation",
    "ReleaseCandidate",
    "ReleaseManifest",
    "Report",
    "ReportDefinition",
    "Requirement",
    "RequirementSource",
    "ResetBehavior",
    "RevokedToken",
    "Role",
    "Routine",
    "SequenceStep",
    "Signal",
    "StateDefinition",
    "Tag",
    "TagAlias",
    "TagGroup",
    "TagGroupMember",
    "TagNamingRule",
    "TagValidationResult",
    "TargetPlatform",
    "Task",
    "TaskDependency",
    "TemplateSet",
    "TestCase",
    "TestStep",
    "TransitionDefinition",
    "UDTDefinition",
    "UDTMember",
    "UploadedFile",
    "UsageRecord",
    "User",
    "ValidationResult",
    "ValidationRun",
    "WorkstationVerificationJob",
    "user_roles",
]
