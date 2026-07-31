"""Declarative base and shared column mixins."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, ClassVar

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.core.clock import new_id, utcnow
from app.core.enums import (
    AddressAssignmentStatus,
    AgentRunStatus,
    AgentStatus,
    AlarmSeverity,
    AnchorType,
    ApprovalDecision,
    ApprovalGateKey,
    ApprovalKind,
    ApprovalStatus,
    AuditAction,
    BaseDataType,
    ChangeOperationType,
    ChangeSetStatus,
    ClarificationStatus,
    ConflictKind,
    ControllerFamily,
    ControllerTaskType,
    DecisionKind,
    DeviceCategory,
    DocumentRole,
    EventType,
    EvidenceKind,
    ExportArtifactKind,
    FailState,
    FaultClass,
    FileKind,
    FileStatus,
    FindingCategory,
    FindingSeverity,
    FindingStatus,
    GateStatus,
    HmiProduct,
    ImportDependencyKind,
    ImportVerificationStatus,
    InstructionCategory,
    InterlockAction,
    InterlockKind,
    LogicElementKind,
    LogicOrigin,
    MergeOutcome,
    MessageRole,
    ModeKind,
    ModuleRole,
    NetworkProtocol,
    ObjectApprovalState,
    ObjectType,
    OperandKind,
    ProfileFieldStatus,
    ProgramType,
    ProjectRole,
    ProjectStatus,
    ProjectType,
    QuestionCategory,
    RecommendationStatus,
    ReleaseCandidateStatus,
    ReleaseMode,
    ReportStatus,
    RequirementKind,
    RequirementPriority,
    RequirementStatus,
    ResetBehaviorKind,
    RestrictedDomainMode,
    RoleName,
    RoutineLanguage,
    SafetyClassification,
    SemanticLocation,
    SignalDirection,
    SignalType,
    TagRole,
    TagScope,
    TagValidationCode,
    TaskKind,
    TaskStatus,
    TemplateApprovalLevel,
    TemplateCategory,
    UnitSystem,
    ValidationDiscipline,
    ValidationLevel,
    ValidationRunStatus,
    ValidationSeverity,
    WorkstationAdapterStatus,
)
from app.database.types import GUID, JSONDict, StrEnumType, UTCDateTime

#: Explicit constraint naming so Alembic can autogenerate reversible migrations
#: (SQLite in particular cannot drop an unnamed constraint).
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Root of the ORM hierarchy."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    #: Maps Python annotations onto the portable column types, so models can be
    #: written with plain ``Mapped[uuid.UUID]`` / ``Mapped[datetime]`` /
    #: ``Mapped[ProjectStatus]``.
    #:
    #: Every enum column MUST be registered here. Passing ``String(n)`` directly
    #: on a ``Mapped[SomeStrEnum]`` column stores the value correctly but loads it
    #: back as a plain ``str``, which silently breaks identity comparisons and
    #: ``.value`` access on any row read from the database. Listing the enum here
    #: means a column whose enum was forgotten fails at mapper configuration
    #: instead. Lengths match the original columns, so adding this changed no
    #: DDL and required no migration.
    type_annotation_map: ClassVar[dict[Any, Any]] = {
        uuid.UUID: GUID,
        datetime: UTCDateTime,
        dict[str, Any]: JSONDict,
        RoleName: StrEnumType(RoleName, 32),
        ProjectRole: StrEnumType(ProjectRole, 32),
        ProjectStatus: StrEnumType(ProjectStatus, 32),
        FileKind: StrEnumType(FileKind, 32),
        FileStatus: StrEnumType(FileStatus, 32),
        DocumentRole: StrEnumType(DocumentRole, 40),
        AnchorType: StrEnumType(AnchorType, 40),
        EvidenceKind: StrEnumType(EvidenceKind, 32),
        TaskKind: StrEnumType(TaskKind, 40),
        TaskStatus: StrEnumType(TaskStatus, 32),
        AgentStatus: StrEnumType(AgentStatus, 32),
        AgentRunStatus: StrEnumType(AgentRunStatus, 32),
        MessageRole: StrEnumType(MessageRole, 16),
        FindingCategory: StrEnumType(FindingCategory, 40),
        FindingSeverity: StrEnumType(FindingSeverity, 20),
        FindingStatus: StrEnumType(FindingStatus, 20),
        RecommendationStatus: StrEnumType(RecommendationStatus, 20),
        ApprovalKind: StrEnumType(ApprovalKind, 48),
        ApprovalStatus: StrEnumType(ApprovalStatus, 20),
        ReportStatus: StrEnumType(ReportStatus, 20),
        EventType: StrEnumType(EventType, 48),
        AuditAction: StrEnumType(AuditAction, 48),
        # -- engineering production platform ------------------------------
        SemanticLocation: StrEnumType(SemanticLocation, 40),
        ProjectType: StrEnumType(ProjectType, 32),
        ControllerFamily: StrEnumType(ControllerFamily, 32),
        SafetyClassification: StrEnumType(SafetyClassification, 40),
        RestrictedDomainMode: StrEnumType(RestrictedDomainMode, 40),
        HmiProduct: StrEnumType(HmiProduct, 40),
        ReleaseMode: StrEnumType(ReleaseMode, 40),
        UnitSystem: StrEnumType(UnitSystem, 16),
        ProfileFieldStatus: StrEnumType(ProfileFieldStatus, 20),
        RequirementKind: StrEnumType(RequirementKind, 24),
        RequirementStatus: StrEnumType(RequirementStatus, 20),
        RequirementPriority: StrEnumType(RequirementPriority, 16),
        ClarificationStatus: StrEnumType(ClarificationStatus, 16),
        QuestionCategory: StrEnumType(QuestionCategory, 32),
        DecisionKind: StrEnumType(DecisionKind, 32),
        DeviceCategory: StrEnumType(DeviceCategory, 32),
        SignalType: StrEnumType(SignalType, 24),
        SignalDirection: StrEnumType(SignalDirection, 16),
        FailState: StrEnumType(FailState, 20),
        NetworkProtocol: StrEnumType(NetworkProtocol, 24),
        ModuleRole: StrEnumType(ModuleRole, 20),
        AddressAssignmentStatus: StrEnumType(AddressAssignmentStatus, 16),
        TagScope: StrEnumType(TagScope, 24),
        TagRole: StrEnumType(TagRole, 32),
        BaseDataType: StrEnumType(BaseDataType, 24),
        TagValidationCode: StrEnumType(TagValidationCode, 32),
        ControllerTaskType: StrEnumType(ControllerTaskType, 16),
        ProgramType: StrEnumType(ProgramType, 20),
        RoutineLanguage: StrEnumType(RoutineLanguage, 32),
        InstructionCategory: StrEnumType(InstructionCategory, 24),
        OperandKind: StrEnumType(OperandKind, 24),
        LogicElementKind: StrEnumType(LogicElementKind, 16),
        LogicOrigin: StrEnumType(LogicOrigin, 24),
        InterlockKind: StrEnumType(InterlockKind, 16),
        InterlockAction: StrEnumType(InterlockAction, 24),
        ResetBehaviorKind: StrEnumType(ResetBehaviorKind, 20),
        ModeKind: StrEnumType(ModeKind, 20),
        FaultClass: StrEnumType(FaultClass, 20),
        AlarmSeverity: StrEnumType(AlarmSeverity, 16),
        ObjectType: StrEnumType(ObjectType, 40),
        ObjectApprovalState: StrEnumType(ObjectApprovalState, 16),
        ChangeSetStatus: StrEnumType(ChangeSetStatus, 24),
        ChangeOperationType: StrEnumType(ChangeOperationType, 32),
        MergeOutcome: StrEnumType(MergeOutcome, 24),
        ConflictKind: StrEnumType(ConflictKind, 32),
        TemplateCategory: StrEnumType(TemplateCategory, 32),
        TemplateApprovalLevel: StrEnumType(TemplateApprovalLevel, 32),
        ValidationSeverity: StrEnumType(ValidationSeverity, 24),
        ValidationDiscipline: StrEnumType(ValidationDiscipline, 24),
        ValidationRunStatus: StrEnumType(ValidationRunStatus, 16),
        ValidationLevel: StrEnumType(ValidationLevel, 40),
        ExportArtifactKind: StrEnumType(ExportArtifactKind, 32),
        ReleaseCandidateStatus: StrEnumType(ReleaseCandidateStatus, 24),
        ImportDependencyKind: StrEnumType(ImportDependencyKind, 32),
        WorkstationAdapterStatus: StrEnumType(WorkstationAdapterStatus, 20),
        ImportVerificationStatus: StrEnumType(ImportVerificationStatus, 20),
        ApprovalGateKey: StrEnumType(ApprovalGateKey, 32),
        ApprovalDecision: StrEnumType(ApprovalDecision, 20),
        GateStatus: StrEnumType(GateStatus, 20),
    }

    def __repr__(self) -> str:  # pragma: no cover - debugging affordance
        ident = getattr(self, "id", None)
        return f"<{type(self).__name__} id={ident}>"


class UUIDPrimaryKeyMixin:
    """Adds a random-UUID primary key generated in Python.

    Generating client-side (rather than via a database default) means the id is
    known before flush, which the event and audit writers rely on.
    """

    id: Mapped[uuid.UUID] = mapped_column(GUID, primary_key=True, default=new_id)


class TimestampMixin:
    """Adds UTC ``created_at`` / ``updated_at`` columns."""

    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utcnow, nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utcnow, onupdate=utcnow, nullable=False
    )
