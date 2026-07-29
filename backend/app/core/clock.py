"""Time and identifier helpers.

All persisted timestamps are timezone-aware UTC. Centralising ``utcnow`` keeps
tests deterministic (they patch this one function rather than ``datetime``).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime


def utcnow() -> datetime:
    """Current time as a timezone-aware UTC datetime."""
    return datetime.now(UTC)


def ensure_utc(value: datetime) -> datetime:
    """Attach UTC to a naive datetime, or convert an aware one to UTC.

    SQLite round-trips ``DateTime`` columns without the tzinfo, so rows read back
    from a demo database arrive naive. Treating those as UTC is correct because
    that is the only thing we ever write.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def isoformat_z(value: datetime) -> str:
    """Serialise to RFC 3339 with a ``Z`` suffix (what the contract specifies)."""
    return ensure_utc(value).isoformat().replace("+00:00", "Z")


def new_id() -> uuid.UUID:
    """Generate a fresh random identifier for a domain entity."""
    return uuid.uuid4()
