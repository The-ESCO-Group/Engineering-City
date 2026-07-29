"""Declarative base and shared column mixins."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, ClassVar

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.core.clock import new_id, utcnow
from app.database.types import GUID, JSONDict, UTCDateTime

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
    #: written with plain ``Mapped[uuid.UUID]`` / ``Mapped[datetime]``.
    type_annotation_map: ClassVar[dict[Any, Any]] = {
        uuid.UUID: GUID,
        datetime: UTCDateTime,
        dict[str, Any]: JSONDict,
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
