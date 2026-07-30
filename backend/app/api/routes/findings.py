"""Findings and the human disposition of them."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Path, Query, status
from sqlalchemy import func, select

from app.api.deps import (
    CurrentUser,
    DbSession,
    ProjectRead,
    Provenance,
    authorise_project,
)
from app.api.schemas.common import Page
from app.api.schemas.file import EvidenceAnchorRead
from app.api.schemas.finding import (
    CreateFindingCommentRequest,
    FindingCommentRead,
    FindingDetail,
    FindingEvidenceRead,
    FindingRead,
    RecommendationRead,
    UpdateFindingRequest,
)
from app.core.clock import utcnow
from app.core.enums import AuditAction, EventType, FindingSeverity, FindingStatus, ProjectRole
from app.core.errors import ConflictError, NotFoundError, PermissionDeniedError
from app.models.finding import Finding, FindingComment, Recommendation
from app.models.user import User
from app.orchestration.events import record_event
from app.security import audit, permissions

router = APIRouter(tags=["findings"])


def _read(finding: Finding) -> FindingRead:
    payload = FindingRead.model_validate(finding)
    payload.evidence_count = len(finding.evidence_links)
    return payload


def _load(
    db: DbSession,
    user: User,
    finding_id: uuid.UUID,
    operation: str,
    roles: frozenset[ProjectRole],
) -> Finding:
    finding = db.get(Finding, finding_id)
    if finding is None:
        raise NotFoundError("Finding", finding_id)
    authorise_project(db, user, finding.project_id, roles, operation)
    return finding


@router.get(
    "/projects/{project_id}/findings",
    response_model=Page[FindingRead],
    summary="List a project's findings",
)
def list_findings(
    access: ProjectRead,
    db: DbSession,
    finding_status: Annotated[FindingStatus | None, Query(alias="status")] = None,
    severity: Annotated[FindingSeverity | None, Query()] = None,
    reviewed_only: Annotated[bool, Query()] = False,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[FindingRead]:
    statement = select(Finding).where(Finding.project_id == access.project.id)
    if finding_status is not None:
        statement = statement.where(Finding.status == finding_status)
    if severity is not None:
        statement = statement.where(Finding.severity == severity)
    if reviewed_only:
        statement = statement.where(Finding.is_reviewed.is_(True))

    total = int(db.execute(select(func.count()).select_from(statement.subquery())).scalar() or 0)
    rows = db.execute(statement.order_by(Finding.created_at).limit(limit).offset(offset)).scalars()
    return Page[FindingRead](
        items=[_read(row) for row in rows], total=total, limit=limit, offset=offset
    )


@router.get(
    "/findings/{finding_id}",
    response_model=FindingDetail,
    summary="Finding detail with every citation",
)
def get_finding(
    finding_id: Annotated[uuid.UUID, Path()], db: DbSession, user: CurrentUser
) -> FindingDetail:
    finding = _load(db, user, finding_id, "view", permissions.READ_ROLES)

    detail = FindingDetail.model_validate(finding)
    detail.evidence_count = len(finding.evidence_links)
    detail.evidence = [
        FindingEvidenceRead(
            id=link.id,
            anchor_id=link.anchor_id,
            evidence_kind=link.evidence_kind,
            role=link.role,
            note=link.note,
            anchor=(EvidenceAnchorRead.model_validate(link.anchor) if link.anchor else None),
        )
        for link in finding.evidence_links
    ]
    detail.recommendations = [
        RecommendationRead.model_validate(item)
        for item in db.execute(
            select(Recommendation).where(Recommendation.finding_id == finding.id)
        ).scalars()
    ]
    return detail


@router.patch(
    "/findings/{finding_id}",
    response_model=FindingDetail,
    summary="Record a human decision on a finding",
)
def update_finding(
    finding_id: Annotated[uuid.UUID, Path()],
    payload: UpdateFindingRequest,
    db: DbSession,
    user: CurrentUser,
    provenance: Provenance,
) -> FindingDetail:
    """Accept, reject or resolve a finding.

    Only the disposition is editable. The agent-produced content — fact,
    inference, evidence — is immutable, so the record of what the platform
    actually said cannot be rewritten after the fact.

    Requires the reviewer role: accepting a finding is an engineering judgement,
    not a bookkeeping action.
    """
    finding = _load(db, user, finding_id, "review findings", permissions.REVIEW_ROLES)

    role = permissions.project_role_for(db, user, finding.project_id)
    if not permissions.can_decide_approval(user, role):
        raise PermissionDeniedError("Recording a decision on a finding requires the reviewer role.")
    if not finding.is_reviewed:
        raise ConflictError(
            "This finding has not been through senior review yet, so it is not "
            "ready for a decision."
        )

    previous = finding.status
    finding.status = payload.status
    finding.decision_notes = payload.decision_notes
    finding.decided_by_id = user.id
    finding.decided_at = utcnow()
    db.flush()

    audit.record(
        db,
        AuditAction.FINDING_UPDATED,
        actor_user_id=user.id,
        project_id=finding.project_id,
        target_type="finding",
        target_id=finding.id,
        summary=f"Set finding '{finding.title}' to {payload.status.value}",
        details={"previous": previous.value, "notes": payload.decision_notes},
        source_ip=provenance.source_ip,
        user_agent=provenance.user_agent,
    )
    record_event(
        db,
        project_id=finding.project_id,
        event_type=EventType.FINDING_UPDATED,
        actor_user_id=user.id,
        payload={
            "finding_id": str(finding.id),
            "title": finding.title,
            "category": finding.category.value,
            "severity": finding.severity.value,
            "status": finding.status.value,
            "confidence": finding.confidence,
            "requires_approval": finding.requires_approval,
            "evidence_count": len(finding.evidence_links),
        },
    )
    db.commit()
    return get_finding(finding_id, db, user)


@router.post(
    "/findings/{finding_id}/comment",
    response_model=FindingCommentRead,
    status_code=status.HTTP_201_CREATED,
    summary="Add a reviewer note to a finding",
)
def comment_on_finding(
    finding_id: Annotated[uuid.UUID, Path()],
    payload: CreateFindingCommentRequest,
    db: DbSession,
    user: CurrentUser,
) -> FindingCommentRead:
    finding = _load(db, user, finding_id, "view", permissions.READ_ROLES)

    comment = FindingComment(finding_id=finding.id, author_id=user.id, body=payload.body)
    db.add(comment)
    db.commit()

    result = FindingCommentRead.model_validate(comment)
    result.author_display_name = user.display_name
    return result


@router.get(
    "/findings/{finding_id}/comments",
    response_model=list[FindingCommentRead],
    summary="Reviewer notes on a finding",
)
def list_comments(
    finding_id: Annotated[uuid.UUID, Path()], db: DbSession, user: CurrentUser
) -> list[FindingCommentRead]:
    finding = _load(db, user, finding_id, "view", permissions.READ_ROLES)

    rows = db.execute(
        select(FindingComment, User)
        .outerjoin(User, User.id == FindingComment.author_id)
        .where(FindingComment.finding_id == finding.id)
        .order_by(FindingComment.created_at)
    ).all()

    results: list[FindingCommentRead] = []
    for comment, author in rows:
        item = FindingCommentRead.model_validate(comment)
        item.author_display_name = author.display_name if author else ""
        results.append(item)
    return results


@router.get(
    "/projects/{project_id}/recommendations",
    response_model=list[RecommendationRead],
    summary="Proposed engineering changes",
)
def list_recommendations(access: ProjectRead, db: DbSession) -> list[RecommendationRead]:
    """Proposals for a human engineer.

    The platform never applies one; an approved recommendation is a recorded
    decision, not an executed change.
    """
    rows = db.execute(
        select(Recommendation)
        .where(Recommendation.project_id == access.project.id)
        .order_by(Recommendation.display_order, Recommendation.created_at)
    ).scalars()
    return [RecommendationRead.model_validate(row) for row in rows]
