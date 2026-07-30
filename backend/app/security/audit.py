"""Audit trail writer.

The audit log answers "who caused this, and when" for every consequential
operation. It is append-only: there is no update or delete path, by design.

Audit entries are written in the *same transaction* as the change they describe.
An operation that succeeds without its audit row, or an audit row for an
operation that rolled back, would both be worse than useless.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.orm import Session

from app.core.enums import AuditAction
from app.core.logging import get_logger, get_request_id
from app.models.audit import AuditEntry

logger = get_logger(__name__)

#: Keys whose values are redacted if a caller ever passes them in ``details``.
_REDACTED_KEYS = frozenset(
    {"password", "password_hash", "token", "access_token", "secret", "api_key", "jwt"}
)


def _scrub(details: dict[str, Any] | None) -> dict[str, Any]:
    """Drop anything credential-shaped before it reaches durable storage."""
    if not details:
        return {}
    return {
        key: ("[redacted]" if key.lower() in _REDACTED_KEYS else value)
        for key, value in details.items()
    }


def record(
    db: Session,
    action: AuditAction,
    *,
    actor_user_id: uuid.UUID | None = None,
    actor_agent_kind: str = "",
    actor_label: str = "",
    project_id: uuid.UUID | None = None,
    target_type: str = "",
    target_id: uuid.UUID | None = None,
    summary: str = "",
    details: dict[str, Any] | None = None,
    source_ip: str = "",
    user_agent: str = "",
) -> AuditEntry:
    """Append one audit entry to the caller's transaction.

    Deliberately does not commit: the caller owns the transaction boundary, so
    the entry lands if and only if the operation it describes lands.
    """
    entry = AuditEntry(
        action=action,
        actor_user_id=actor_user_id,
        actor_agent_kind=actor_agent_kind,
        actor_label=actor_label,
        project_id=project_id,
        target_type=target_type,
        target_id=target_id,
        summary=summary,
        details=_scrub(details),
        request_id=get_request_id() or "",
        source_ip=source_ip[:64],
        user_agent=user_agent[:255],
    )
    db.add(entry)

    logger.info(
        "audit",
        extra={
            "audit_action": action.value,
            "actor_user_id": str(actor_user_id) if actor_user_id else "",
            "actor_agent_kind": actor_agent_kind,
            "project_id": str(project_id) if project_id else "",
            "target_type": target_type,
            "target_id": str(target_id) if target_id else "",
        },
    )
    return entry
