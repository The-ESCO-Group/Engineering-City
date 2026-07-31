"""Read-only delivery-agent results over the canonical Project Core.

The delivery plugin boundary currently exposes a :class:`TaskContext`, but not
the validated action/change-set executor used by core engineering agents.  The
agents in this package therefore inspect canonical rows and return structured,
traceable issues only.  They never insert, update, merge, or approve canonical
objects.  This is deliberate: an issue summary is preferable to bypassing the
Project Core to make an apparently useful edit that has no approval history.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, replace
from typing import ClassVar

from sqlalchemy import select

from app.core.enums import ObjectType, TaskKind
from app.models.core import ObjectEvidenceLink
from app.orchestration.handlers import TaskContext


@dataclass(frozen=True, slots=True)
class DeliveryIssue:
    """One deterministic issue found in canonical delivery-domain data."""

    code: str
    severity: str
    title: str
    detail: str
    object_type: ObjectType
    object_id: uuid.UUID
    reference: str
    evidence_anchor_ids: tuple[uuid.UUID, ...] = ()
    requires_human_review: bool = False

    def summary(self) -> dict[str, object]:
        return {
            "code": self.code,
            "severity": self.severity,
            "title": self.title,
            "detail": self.detail,
            "object_type": self.object_type.value,
            "object_id": str(self.object_id),
            "reference": self.reference,
            "evidence_anchor_ids": [str(value) for value in self.evidence_anchor_ids],
            "requires_human_review": self.requires_human_review,
        }


@dataclass(frozen=True, slots=True)
class DeliveryAuditResult:
    """Serializable return value consumed by the core delivery handler."""

    agent_kind: str
    location: str
    scope_examined: str
    issues: tuple[DeliveryIssue, ...] = ()
    warnings: tuple[str, ...] = ()
    counts: dict[str, int] = field(default_factory=dict)
    generator_plan: dict[str, object] | None = None

    def summary(self) -> dict[str, object]:
        return {
            "agent_kind": self.agent_kind,
            "location": self.location,
            "mode": "read_only_canonical_audit",
            "scope_examined": self.scope_examined,
            "counts": dict(sorted(self.counts.items())),
            "issue_count": len(self.issues),
            "issues": [item.summary() for item in self.issues],
            "warnings": list(self.warnings),
            "generator_plan": self.generator_plan,
            "canonical_writes": 0,
        }


class ReadOnlyDeliveryAgent:
    """Base plugin that attaches canonical evidence without mutating state."""

    kind: ClassVar[str]
    display_name: ClassVar[str]
    version: ClassVar[str] = "1.0.0"
    location: ClassVar[str]
    handles_task_kinds: ClassVar[tuple[str, ...]] = (TaskKind.DELIVERY.value,)
    persistence_warning: ClassVar[str] = (
        "Read-only delivery audit: the plugin boundary does not expose the validated "
        "Project Core change-set/action executor. No canonical object, finding, approval, "
        "or engineering decision was created or modified."
    )

    def run(self, context: TaskContext) -> DeliveryAuditResult:
        result = self.inspect(context)
        object_keys = {(item.object_type, item.object_id) for item in result.issues}
        anchor_map: dict[tuple[ObjectType, uuid.UUID], set[uuid.UUID]] = {
            key: set() for key in object_keys
        }
        if object_keys:
            links = context.db.execute(
                select(ObjectEvidenceLink).where(
                    ObjectEvidenceLink.project_id == context.project.id
                )
            ).scalars()
            for link in links:
                key = (link.object_type, link.object_id)
                if key in anchor_map and link.anchor_id is not None:
                    anchor_map[key].add(link.anchor_id)

        issues = tuple(
            sorted(
                (
                    replace(
                        item,
                        evidence_anchor_ids=tuple(
                            sorted(
                                anchor_map.get((item.object_type, item.object_id), set()),
                                key=str,
                            )
                        ),
                    )
                    for item in result.issues
                ),
                key=lambda item: (
                    item.severity,
                    item.code,
                    item.reference.casefold(),
                    str(item.object_id),
                ),
            )
        )
        warnings = list(result.warnings)
        warnings.append(self.persistence_warning)
        if any(not item.evidence_anchor_ids for item in issues):
            warnings.append(
                "One or more issue summaries have no source evidence anchor on the canonical "
                "object. They remain audit observations and must not be presented as accepted "
                "findings."
            )
        return replace(result, issues=issues, warnings=tuple(dict.fromkeys(warnings)))

    def inspect(self, context: TaskContext) -> DeliveryAuditResult:
        raise NotImplementedError


def issue(
    *,
    code: str,
    severity: str,
    title: str,
    detail: str,
    object_type: ObjectType,
    object_id: uuid.UUID,
    reference: str,
    requires_human_review: bool = False,
) -> DeliveryIssue:
    """Small constructor that keeps discipline modules terse and consistent."""

    return DeliveryIssue(
        code=code,
        severity=severity,
        title=title,
        detail=detail,
        object_type=object_type,
        object_id=object_id,
        reference=reference,
        requires_human_review=requires_human_review,
    )
