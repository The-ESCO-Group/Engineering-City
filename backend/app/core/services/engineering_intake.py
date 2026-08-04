"""Canonical engineering intake and human clarification resolution.

The product UI collects a profile before upload.  This service turns that
explicit statement into Project Core rows; it never derives a production scope
from the project's free-form ``attributes`` field.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.schemas.engineering import EngineeringProfileInput
from app.core.clock import utcnow
from app.core.config import Settings
from app.core.enums import (
    AuditAction,
    ClarificationStatus,
    DecisionKind,
    EventType,
    HmiProduct,
    ProfileFieldStatus,
    QuestionCategory,
    RestrictedDomainMode,
    SemanticLocation,
    TaskKind,
    TaskStatus,
)
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.models.profile import (
    REQUIRED_PROFILE_FIELDS,
    EngineeringStandardSet,
    ProjectDecision,
    ProjectProfile,
    ProjectQuestion,
    TargetPlatform,
    TemplateSet,
)
from app.models.project import Project
from app.models.release import ExportProfile, ReleaseCandidate
from app.models.requirements import Clarification
from app.models.task import Task
from app.models.user import User
from app.orchestration import engine
from app.orchestration.events import record_event
from app.orchestration.queue import DatabaseTaskQueue
from app.security import audit

_RESOLUTION = re.compile(r"^\s*(?P<width>\d{2,5})\s*[xX\u00d7]\s*(?P<height>\d{2,5})\s*$")


def _checksum(document: dict[str, Any]) -> str:
    encoded = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _resolution(value: str, *, required: bool) -> tuple[int, int]:
    if not value.strip() and not required:
        return (0, 0)
    match = _RESOLUTION.fullmatch(value)
    if match is None:
        raise ValidationError(
            "HMI resolution must be written as width x height, for example 1280 x 800.",
            details={"field": "target.hmi_resolution", "value": value},
        )
    return (int(match.group("width")), int(match.group("height")))


def _ensure_configurable(db: Session, project: Project) -> None:
    if project.is_archived:
        raise ConflictError("Archived projects are read-only.")
    has_tasks = bool(
        db.execute(select(func.count(Task.id)).where(Task.project_id == project.id)).scalar_one()
    )
    has_candidates = bool(
        db.execute(
            select(func.count(ReleaseCandidate.id)).where(ReleaseCandidate.project_id == project.id)
        ).scalar_one()
    )
    if has_tasks or has_candidates:
        raise ConflictError(
            "The engineering profile is revision-bound once workflow work exists. "
            "Create a reviewed change set instead of rewriting intake in place."
        )


def configure_project(
    db: Session,
    *,
    project: Project,
    payload: EngineeringProfileInput,
    actor: User,
) -> ProjectProfile:
    """Create or replace the pre-run canonical profile from explicit user input."""

    _ensure_configurable(db, project)
    target_input = payload.target
    hmi_required = target_input.hmi_product is not HmiProduct.NONE
    if hmi_required and not target_input.hmi_version.strip():
        raise ValidationError(
            "An HMI product in scope requires an exact HMI version.",
            details={"field": "target.hmi_version"},
        )
    width, height = _resolution(target_input.hmi_resolution, required=hmi_required)

    existing_profile = db.execute(
        select(ProjectProfile).where(
            ProjectProfile.project_id == project.id,
            ProjectProfile.deleted_at.is_(None),
        )
    ).scalar_one_or_none()
    existing_target = db.execute(
        select(TargetPlatform).where(
            TargetPlatform.project_id == project.id,
            TargetPlatform.deleted_at.is_(None),
        )
    ).scalar_one_or_none()

    standards_document = {
        "naming_standard_version": payload.standards.naming_standard_version.strip(),
        "alarm_philosophy_version": payload.standards.alarm_philosophy_version.strip(),
        "aoi_udt_library": payload.standards.aoi_udt_library.strip(),
        "hmi_library": payload.standards.hmi_library.strip(),
        "test_template_set": payload.standards.test_template_set.strip(),
    }
    template_document = {
        "controller_template": payload.standards.controller_template.strip(),
        "aoi_udt_library": payload.standards.aoi_udt_library.strip(),
        "hmi_library": payload.standards.hmi_library.strip(),
        "test_template_set": payload.standards.test_template_set.strip(),
        "controller_family": target_input.controller_family.value,
    }

    standard_set = db.execute(
        select(EngineeringStandardSet).where(
            EngineeringStandardSet.project_id == project.id,
            EngineeringStandardSet.key == "esco.intake-selected-standards",
            EngineeringStandardSet.version == "1",
            EngineeringStandardSet.deleted_at.is_(None),
        )
    ).scalar_one_or_none()
    if standard_set is None:
        standard_set = EngineeringStandardSet(
            project_id=project.id,
            key="esco.intake-selected-standards",
            version="1",
            created_by_id=actor.id,
        )
        db.add(standard_set)
    standard_set.display_name = "Standards selected at project intake"
    standard_set.description = "Exact standards identifiers supplied by the engineer at intake."
    standard_set.rules = standards_document
    standard_set.checksum = _checksum(standards_document)

    template_set = db.execute(
        select(TemplateSet).where(
            TemplateSet.project_id == project.id,
            TemplateSet.key == "esco.intake-selected-templates",
            TemplateSet.version == "1",
            TemplateSet.deleted_at.is_(None),
        )
    ).scalar_one_or_none()
    if template_set is None:
        template_set = TemplateSet(
            project_id=project.id,
            key="esco.intake-selected-templates",
            version="1",
            created_by_id=actor.id,
        )
        db.add(template_set)
    template_set.display_name = "Templates selected at project intake"
    template_set.description = "Exact template identifiers supplied by the engineer at intake."
    template_set.template_ids = [
        value
        for value in (
            template_document["controller_template"],
            template_document["aoi_udt_library"],
            template_document["hmi_library"],
            template_document["test_template_set"],
        )
        if value
    ]
    template_set.checksum = _checksum(template_document)
    template_set.supported_controller_families = [target_input.controller_family.value]
    db.flush()

    completeness = dict.fromkeys(REQUIRED_PROFILE_FIELDS, ProfileFieldStatus.PRESENT.value)
    profile_values: dict[str, Any] = {
        "project_name": payload.project_name.strip(),
        "customer_label": payload.customer_label.strip(),
        "plant_or_site": payload.plant_or_site.strip(),
        "project_description": payload.project_description.strip(),
        "project_type": payload.project_type,
        "naming_standard_version": payload.standards.naming_standard_version.strip(),
        "alarm_philosophy_version": payload.standards.alarm_philosophy_version.strip(),
        "template_set_id": template_set.id,
        "engineering_standard_set_id": standard_set.id,
        "units": payload.units,
        "locale": payload.locale.strip(),
        "network_architecture": payload.network_architecture.strip(),
        "release_mode": payload.release_mode,
        "restricted_domains": [item.value for item in payload.restricted_domains],
        "restricted_domain_mode": RestrictedDomainMode.MANUAL_REVIEW_ONLY,
        "completeness": completeness,
        "missing_required_fields": [],
        "is_complete_for_generation": True,
    }
    if existing_profile is None:
        profile = ProjectProfile(project_id=project.id, created_by_id=actor.id, **profile_values)
        db.add(profile)
    else:
        profile = existing_profile
        for field, value in profile_values.items():
            setattr(profile, field, value)

    target_values: dict[str, Any] = {
        "controller_family": target_input.controller_family,
        "controller_catalog_number": target_input.controller_catalog_number.strip(),
        "firmware_revision": target_input.firmware_revision.strip(),
        "studio_5000_version": target_input.studio_5000_version.strip(),
        "safety_classification": target_input.safety_classification,
        "hmi_product": target_input.hmi_product,
        "hmi_version": target_input.hmi_version.strip(),
        "hmi_terminal_model": target_input.hmi_terminal_model.strip(),
        "hmi_resolution_width": width,
        "hmi_resolution_height": height,
        "factorytalk_architecture": target_input.factorytalk_architecture.strip(),
        "supported_instruction_set_key": target_input.supported_instruction_set_key.strip(),
        "known_limitations": list(target_input.known_limitations),
    }
    if existing_target is None:
        target = TargetPlatform(project_id=project.id, created_by_id=actor.id, **target_values)
        db.add(target)
    else:
        target = existing_target
        for field, value in target_values.items():
            setattr(target, field, value)

    export_profile = db.execute(
        select(ExportProfile).where(
            ExportProfile.project_id == project.id,
            ExportProfile.is_default.is_(True),
            ExportProfile.deleted_at.is_(None),
        )
    ).scalar_one_or_none()
    if export_profile is None:
        db.add(
            ExportProfile(
                project_id=project.id,
                key="full_controller",
                name="Full engineering release package",
                description="Canonical seven-folder release selected at project intake.",
                target_studio_version=target_input.studio_5000_version.strip(),
                controller_family=target_input.controller_family,
                l5x_schema_revision="1.0",
                include_delivery_artifacts=True,
                is_default=True,
                created_by_id=actor.id,
            )
        )
    else:
        export_profile.target_studio_version = target_input.studio_5000_version.strip()
        export_profile.controller_family = target_input.controller_family

    db.flush()
    audit.record(
        db,
        AuditAction.PROFILE_UPDATED,
        actor_user_id=actor.id,
        actor_label=actor.display_name,
        project_id=project.id,
        target_type="project_profile",
        target_id=profile.id,
        summary="Recorded the canonical engineering profile supplied at project intake.",
        details={
            "release_mode": profile.release_mode.value,
            "controller_family": target_input.controller_family.value,
            "studio_5000_version": target_input.studio_5000_version.strip(),
            "restricted_domains": list(profile.restricted_domains),
            "blocks_generation": profile.blocks_generation,
        },
    )
    record_event(
        db,
        project_id=project.id,
        event_type=EventType.PROJECT_UPDATED,
        actor_user_id=actor.id,
        payload={
            "project_id": str(project.id),
            "name": project.name,
            "status": project.status.value,
        },
    )
    db.flush()
    return profile


def get_profile(db: Session, project_id: uuid.UUID) -> tuple[ProjectProfile, TargetPlatform]:
    profile = db.execute(
        select(ProjectProfile).where(
            ProjectProfile.project_id == project_id,
            ProjectProfile.deleted_at.is_(None),
        )
    ).scalar_one_or_none()
    if profile is None:
        raise NotFoundError("Engineering profile", project_id)
    target = db.execute(
        select(TargetPlatform).where(
            TargetPlatform.project_id == project_id,
            TargetPlatform.deleted_at.is_(None),
        )
    ).scalar_one_or_none()
    if target is None:
        raise NotFoundError("Target platform", project_id)
    return profile, target


def clarification_inbox(
    db: Session, project_id: uuid.UUID
) -> tuple[list[ProjectQuestion], list[Clarification]]:
    questions = list(
        db.execute(
            select(ProjectQuestion)
            .where(ProjectQuestion.project_id == project_id, ProjectQuestion.deleted_at.is_(None))
            .order_by(ProjectQuestion.created_at, ProjectQuestion.reference)
        ).scalars()
    )
    clarifications = list(
        db.execute(
            select(Clarification)
            .where(Clarification.project_id == project_id, Clarification.deleted_at.is_(None))
            .order_by(Clarification.created_at)
        ).scalars()
    )
    return questions, clarifications


def _decision(
    db: Session,
    *,
    project: Project,
    actor: User,
    answer: str,
    title: str,
    question_id: uuid.UUID | None,
    scope: dict[str, Any],
) -> ProjectDecision:
    now = utcnow()
    decision = ProjectDecision(
        project_id=project.id,
        reference=f"DEC-{uuid.uuid4().hex[:8].upper()}",
        kind=DecisionKind.CLARIFICATION_ANSWER,
        title=title[:255],
        statement=answer.strip(),
        rationale="Recorded verbatim from the authenticated engineer resolving the clarification.",
        decided_by_id=actor.id,
        decided_by_name=actor.display_name,
        decided_at=now,
        question_id=question_id,
        scope=scope,
        created_by_id=actor.id,
        created_in_revision=project.current_revision_number,
        updated_in_revision=project.current_revision_number,
    )
    db.add(decision)
    db.flush()
    return decision


def _unblock_clarification_tasks(
    db: Session,
    project: Project,
    settings: Settings,
    *,
    question: ProjectQuestion | None = None,
) -> int:
    queue = DatabaseTaskQueue(db, settings)
    kinds = {TaskKind.RESOLVE_CLARIFICATIONS}
    task_ids: set[uuid.UUID] = set()
    if question is not None:
        if question.raised_in_task_id is not None:
            task_ids.add(question.raised_in_task_id)
        for raw_kind in question.blocks_task_kinds:
            try:
                kinds.add(TaskKind(str(raw_kind)))
            except ValueError:
                # Persisted agent data may outlive an enum member.  An unknown
                # value must not prevent the exact originating task from being
                # requeued or the clarification checkpoint from re-evaluating.
                continue
    tasks = list(
        db.execute(
            select(Task).where(
                Task.project_id == project.id,
                (Task.kind.in_(kinds) | Task.id.in_(task_ids)),
                Task.status.in_([TaskStatus.BLOCKED, TaskStatus.AWAITING_APPROVAL]),
            )
        ).scalars()
    )
    for task in tasks:
        queue.unblock(task)
    if tasks:
        engine.refresh_project_status(db, project)
    return len(tasks)


def resolve_project_question(
    db: Session,
    *,
    project: Project,
    question: ProjectQuestion,
    answer: str,
    resolution: str,
    actor: User,
    settings: Settings,
) -> tuple[ProjectDecision, int]:
    if question.project_id != project.id:
        raise NotFoundError("Project question", question.id)
    if question.status not in {ClarificationStatus.OPEN, ClarificationStatus.DEFERRED}:
        raise ConflictError(f"Question {question.reference} is already {question.status.value}.")
    now = utcnow()
    status = (
        ClarificationStatus.ANSWERED if resolution == "answered" else ClarificationStatus.DISMISSED
    )
    decision = _decision(
        db,
        project=project,
        actor=actor,
        answer=answer,
        title=f"Answer to {question.reference}",
        question_id=question.id,
        scope={"project_question_id": str(question.id), "reference": question.reference},
    )
    question.answer = answer.strip()
    question.status = status
    question.answered_by_id = actor.id
    question.answered_at = now
    unblocked = _unblock_clarification_tasks(db, project, settings, question=question)
    _record_resolution(
        db,
        project=project,
        actor=actor,
        decision=decision,
        subject_id=question.id,
        reference=question.reference,
        category=question.category,
        question=question.question,
        why_it_matters=question.why_it_matters,
        status=status,
        blocks_task_kinds=list(question.blocks_task_kinds),
    )
    return decision, unblocked


def resolve_clarification(
    db: Session,
    *,
    project: Project,
    clarification: Clarification,
    answer: str,
    resolution: str,
    actor: User,
    settings: Settings,
) -> tuple[ProjectDecision, int]:
    if clarification.project_id != project.id:
        raise NotFoundError("Clarification", clarification.id)
    if clarification.status not in {ClarificationStatus.OPEN, ClarificationStatus.DEFERRED}:
        raise ConflictError(f"Clarification is already {clarification.status.value}.")
    now = utcnow()
    status = (
        ClarificationStatus.ANSWERED if resolution == "answered" else ClarificationStatus.DISMISSED
    )
    decision = _decision(
        db,
        project=project,
        actor=actor,
        answer=answer,
        title="Requirement clarification",
        question_id=None,
        scope={
            "clarification_id": str(clarification.id),
            "requirement_id": (
                str(clarification.requirement_id) if clarification.requirement_id else None
            ),
            "object_type": str(clarification.object_type) if clarification.object_type else None,
            "object_id": str(clarification.object_id) if clarification.object_id else None,
        },
    )
    was_blocking = clarification.blocks_generation
    clarification.resolution = answer.strip()
    clarification.status = status
    clarification.resolved_by_id = actor.id
    clarification.resolved_at = now
    clarification.blocks_generation = False
    unblocked = _unblock_clarification_tasks(db, project, settings)
    _record_resolution(
        db,
        project=project,
        actor=actor,
        decision=decision,
        subject_id=clarification.id,
        reference=f"CL-{str(clarification.id)[:8].upper()}",
        category=QuestionCategory.REQUIREMENT_AMBIGUITY,
        question=clarification.question or clarification.ambiguity,
        why_it_matters=clarification.ambiguity,
        status=status,
        blocks_task_kinds=[TaskKind.RESOLVE_CLARIFICATIONS.value] if was_blocking else [],
    )
    return decision, unblocked


def _record_resolution(
    db: Session,
    *,
    project: Project,
    actor: User,
    decision: ProjectDecision,
    subject_id: uuid.UUID,
    reference: str,
    category: QuestionCategory,
    question: str,
    why_it_matters: str,
    status: ClarificationStatus,
    blocks_task_kinds: list[Any],
) -> None:
    audit.record(
        db,
        AuditAction.QUESTION_ANSWERED,
        actor_user_id=actor.id,
        actor_label=actor.display_name,
        project_id=project.id,
        target_type="clarification",
        target_id=subject_id,
        summary=f"{actor.display_name} resolved {reference}.",
        details={"decision_id": str(decision.id), "status": status.value},
    )
    audit.record(
        db,
        AuditAction.DECISION_RECORDED,
        actor_user_id=actor.id,
        actor_label=actor.display_name,
        project_id=project.id,
        target_type="project_decision",
        target_id=decision.id,
        summary=f"Recorded {decision.reference}: {decision.title}.",
        details={"subject_id": str(subject_id), "reference": reference},
    )
    record_event(
        db,
        project_id=project.id,
        event_type=EventType.PROJECT_QUESTION_RESOLVED,
        actor_user_id=actor.id,
        payload={
            "question_id": str(subject_id),
            "reference": reference,
            "category": category.value,
            "status": status.value,
            "location": SemanticLocation.REQUIREMENTS_HALL.value,
            "question": question[:500],
            "why_it_matters": why_it_matters[:500],
            "is_mandatory": True,
            "is_blocking": False,
            "blocks_task_kinds": blocks_task_kinds,
            "answer_summary": decision.statement[:500],
            "decision_id": str(decision.id),
            "answered_by_id": str(actor.id),
        },
    )
    db.flush()
