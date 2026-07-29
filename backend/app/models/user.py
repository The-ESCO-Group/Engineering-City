"""Users and global roles."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, Column, ForeignKey, String, Table, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import RoleName
from app.database.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.database.types import GUID, UTCDateTime

if TYPE_CHECKING:
    from app.models.project import ProjectMember

#: Users may hold more than one global role (e.g. a reviewer who is also an
#: administrator), so this is a many-to-many rather than a column on ``users``.
user_roles = Table(
    "user_roles",
    Base.metadata,
    Column("user_id", GUID, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    Column("role_id", GUID, ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True),
)


class Role(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A named global capability set (``engineer``, ``reviewer``, ``administrator``)."""

    __tablename__ = "roles"
    __table_args__ = (UniqueConstraint("name", name="uq_roles_name"),)

    name: Mapped[RoleName] = mapped_column(String(32), nullable=False)
    description: Mapped[str] = mapped_column(String(255), default="", nullable=False)

    users: Mapped[list[User]] = relationship(
        secondary=user_roles, back_populates="roles", lazy="selectin"
    )


class User(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """An ESCO engineer, reviewer or administrator.

    Authentication is local only: there is no external identity provider and no
    outbound call at login time.
    """

    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("email", name="uq_users_email"),)

    email: Mapped[str] = mapped_column(String(320), nullable=False, index=True)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    #: Argon2id hash. The plaintext password is never stored or logged.
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    roles: Mapped[list[Role]] = relationship(
        secondary=user_roles, back_populates="users", lazy="selectin"
    )
    memberships: Mapped[list[ProjectMember]] = relationship(
        back_populates="user", cascade="all, delete-orphan", lazy="selectin"
    )

    @property
    def role_names(self) -> set[RoleName]:
        return {RoleName(role.name) for role in self.roles}

    def has_role(self, role: RoleName) -> bool:
        return role in self.role_names

    @property
    def is_administrator(self) -> bool:
        return self.has_role(RoleName.ADMINISTRATOR)


class RevokedToken(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Denylist of access tokens invalidated by an explicit logout.

    JWTs are otherwise valid until expiry; persisting the ``jti`` is what makes
    ``POST /api/auth/logout`` a real operation rather than a client-side no-op.
    """

    __tablename__ = "revoked_tokens"
    __table_args__ = (UniqueConstraint("jti", name="uq_revoked_tokens_jti"),)

    jti: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="CASCADE"), nullable=True
    )
    #: Rows older than this can be pruned; the token is expired anyway.
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, index=True)
