"""SQLAlchemy ORM entities.

Importing this package registers every table on ``Base.metadata``, which Alembic
autogeneration and ``create_all`` both depend on. Import it, not the individual
modules, when you need the full schema.
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
from app.models.event import Event
from app.models.evidence import MAX_EXCERPT_CHARS, EvidenceAnchor
from app.models.file import ParsedArtifact, UploadedFile
from app.models.finding import Finding, FindingComment, FindingEvidence, Recommendation
from app.models.project import Project, ProjectMember
from app.models.report import Report
from app.models.task import Task, TaskDependency
from app.models.user import RevokedToken, Role, User, user_roles

__all__ = [
    "CORE_AGENT_KINDS",
    "MAX_EXCERPT_CHARS",
    "AgentDefinition",
    "AgentInstance",
    "AgentMessage",
    "AgentRun",
    "Approval",
    "AuditEntry",
    "Base",
    "Event",
    "EvidenceAnchor",
    "Finding",
    "FindingComment",
    "FindingEvidence",
    "ModelConfiguration",
    "ParsedArtifact",
    "Project",
    "ProjectMember",
    "Recommendation",
    "Report",
    "RevokedToken",
    "Role",
    "Task",
    "TaskDependency",
    "UploadedFile",
    "UsageRecord",
    "User",
    "user_roles",
]
