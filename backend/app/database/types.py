"""Portable column types.

The same ORM models must run on PostgreSQL (internal multi-user deployment) and
SQLite (single-file demonstration mode), so UUID, JSON and timestamp columns go
through these decorators rather than dialect-specific types.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CHAR, DateTime, Dialect, Text, TypeDecorator
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID

from app.core.clock import ensure_utc


class GUID(TypeDecorator[uuid.UUID]):
    """UUID stored natively on PostgreSQL, as a 36-char string on SQLite."""

    impl = CHAR
    cache_ok = True

    def load_dialect_impl(self, dialect: Dialect) -> Any:
        if dialect.name == "postgresql":
            return dialect.type_descriptor(PGUUID(as_uuid=True))
        return dialect.type_descriptor(CHAR(36))

    def process_bind_param(self, value: Any, dialect: Dialect) -> Any:
        if value is None:
            return None
        if not isinstance(value, uuid.UUID):
            value = uuid.UUID(str(value))
        return value if dialect.name == "postgresql" else str(value)

    def process_result_value(self, value: Any, dialect: Dialect) -> uuid.UUID | None:
        if value is None:
            return None
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


class JSONDict(TypeDecorator[dict[str, Any]]):
    """``JSONB`` on PostgreSQL, JSON-encoded ``TEXT`` on SQLite.

    Always round-trips to a ``dict``; ``None`` becomes ``{}`` so callers never
    have to null-check structured payloads.
    """

    impl = Text
    cache_ok = True

    def load_dialect_impl(self, dialect: Dialect) -> Any:
        if dialect.name == "postgresql":
            return dialect.type_descriptor(JSONB())
        return dialect.type_descriptor(Text())

    def process_bind_param(self, value: Any, dialect: Dialect) -> Any:
        if value is None:
            value = {}
        if dialect.name == "postgresql":
            return value
        return json.dumps(value, default=str, ensure_ascii=False, sort_keys=True)

    def process_result_value(self, value: Any, dialect: Dialect) -> dict[str, Any]:
        if value is None:
            return {}
        if isinstance(value, dict):
            return value
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return decoded if isinstance(decoded, dict) else {}


class JSONList(TypeDecorator[list[Any]]):
    """List-valued counterpart to :class:`JSONDict`."""

    impl = Text
    cache_ok = True

    def load_dialect_impl(self, dialect: Dialect) -> Any:
        if dialect.name == "postgresql":
            return dialect.type_descriptor(JSONB())
        return dialect.type_descriptor(Text())

    def process_bind_param(self, value: Any, dialect: Dialect) -> Any:
        if value is None:
            value = []
        if dialect.name == "postgresql":
            return value
        return json.dumps(value, default=str, ensure_ascii=False)

    def process_result_value(self, value: Any, dialect: Dialect) -> list[Any]:
        if value is None:
            return []
        if isinstance(value, list):
            return value
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError):
            return []
        return decoded if isinstance(decoded, list) else []


class UTCDateTime(TypeDecorator[datetime]):
    """Timezone-aware UTC timestamps on every backend.

    SQLite drops tzinfo on the way out; this normalises reads so application
    code can always assume an aware UTC datetime.
    """

    impl = DateTime
    cache_ok = True

    def load_dialect_impl(self, dialect: Dialect) -> Any:
        return dialect.type_descriptor(DateTime(timezone=True))

    def process_bind_param(self, value: Any, dialect: Dialect) -> Any:
        if value is None:
            return None
        if not isinstance(value, datetime):
            raise TypeError(f"expected datetime, got {type(value).__name__}")
        return ensure_utc(value)

    def process_result_value(self, value: Any, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        return ensure_utc(value)
