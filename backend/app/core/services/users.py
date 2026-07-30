"""User, role and session domain logic.

Kept out of the route handlers so authentication can be tested without HTTP and
so the worker and CLI scripts can reuse it.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.clock import utcnow
from app.core.config import Settings
from app.core.enums import AuditAction, RoleName
from app.core.errors import AuthenticationError, ConflictError, NotFoundError, ValidationError
from app.core.logging import get_logger
from app.models.user import RevokedToken, Role, User
from app.security import audit
from app.security.passwords import (
    PasswordPolicyError,
    hash_password,
    needs_rehash,
    verify_password,
    waste_time_like_a_real_verification,
)
from app.security.tokens import TokenClaims, create_access_token

logger = get_logger(__name__)

#: Descriptions seeded alongside the roles, shown in the admin UI.
ROLE_DESCRIPTIONS: dict[RoleName, str] = {
    RoleName.ENGINEER: "Creates projects, uploads files and starts reviews.",
    RoleName.REVIEWER: "Reviews findings and records engineering approvals.",
    RoleName.ADMINISTRATOR: "Manages users, permissions and model configuration.",
}


def ensure_roles(db: Session) -> dict[RoleName, Role]:
    """Create any missing role rows and return them all."""
    existing = {RoleName(role.name): role for role in db.execute(select(Role)).scalars()}
    for name, description in ROLE_DESCRIPTIONS.items():
        if name not in existing:
            role = Role(name=name, description=description)
            db.add(role)
            existing[name] = role
    db.flush()
    return existing


def get_user_by_email(db: Session, email: str) -> User | None:
    normalised = email.strip().lower()
    return db.execute(select(User).where(User.email == normalised)).scalar_one_or_none()


def get_user(db: Session, user_id: uuid.UUID) -> User:
    user = db.get(User, user_id)
    if user is None:
        raise NotFoundError("User", user_id)
    return user


def create_user(
    db: Session,
    *,
    email: str,
    display_name: str,
    password: str,
    roles: list[RoleName],
    actor_user_id: uuid.UUID | None = None,
    actor_label: str = "",
) -> User:
    """Create a user. Raises :class:`ConflictError` if the email is taken."""
    normalised = email.strip().lower()
    if not normalised:
        raise ValidationError("Email is required.")
    if get_user_by_email(db, normalised) is not None:
        raise ConflictError(
            "An account with that email address already exists.",
            details={"email": normalised},
        )

    try:
        password_hash = hash_password(password)
    except PasswordPolicyError as exc:
        raise ValidationError(str(exc)) from exc

    role_rows = ensure_roles(db)
    user = User(
        email=normalised,
        display_name=display_name.strip() or normalised,
        password_hash=password_hash,
        roles=[role_rows[name] for name in dict.fromkeys(roles or [RoleName.ENGINEER])],
    )
    db.add(user)
    db.flush()

    audit.record(
        db,
        AuditAction.USER_CREATED,
        actor_user_id=actor_user_id,
        actor_label=actor_label,
        target_type="user",
        target_id=user.id,
        summary=f"Created user {user.email}",
        details={"roles": sorted(role.value for role in user.role_names)},
    )
    return user


def set_user_roles(
    db: Session,
    user: User,
    roles: list[RoleName],
    *,
    actor_user_id: uuid.UUID | None = None,
) -> User:
    """Replace a user's global roles, recording the before/after in the audit log."""
    before = sorted(role.value for role in user.role_names)
    role_rows = ensure_roles(db)
    user.roles = [role_rows[name] for name in dict.fromkeys(roles)]
    db.flush()

    audit.record(
        db,
        AuditAction.PERMISSION_CHANGED,
        actor_user_id=actor_user_id,
        target_type="user",
        target_id=user.id,
        summary=f"Changed global roles for {user.email}",
        details={"before": before, "after": sorted(role.value for role in user.role_names)},
    )
    return user


def authenticate(
    db: Session,
    *,
    email: str,
    password: str,
    settings: Settings,
    source_ip: str = "",
    user_agent: str = "",
) -> tuple[User, str, TokenClaims]:
    """Verify credentials and mint an access token.

    A failed attempt is audited and always reports the same message, whether the
    account is unknown, disabled or the password was wrong. For an unknown
    account the work of a real hash verification is still performed, so response
    timing does not reveal which emails exist.
    """
    user = get_user_by_email(db, email)

    if user is None:
        waste_time_like_a_real_verification()
        audit.record(
            db,
            AuditAction.USER_LOGIN_FAILED,
            actor_label="anonymous",
            summary="Login failed: unknown account",
            details={"email": email.strip().lower(), "reason": "unknown_account"},
            source_ip=source_ip,
            user_agent=user_agent,
        )
        raise AuthenticationError("Incorrect email address or password.")

    if not verify_password(password, user.password_hash):
        audit.record(
            db,
            AuditAction.USER_LOGIN_FAILED,
            actor_user_id=user.id,
            summary=f"Login failed for {user.email}",
            details={"reason": "bad_password"},
            source_ip=source_ip,
            user_agent=user_agent,
        )
        raise AuthenticationError("Incorrect email address or password.")

    if not user.is_active:
        audit.record(
            db,
            AuditAction.USER_LOGIN_FAILED,
            actor_user_id=user.id,
            summary=f"Login refused for disabled account {user.email}",
            details={"reason": "account_disabled"},
            source_ip=source_ip,
            user_agent=user_agent,
        )
        raise AuthenticationError("Incorrect email address or password.")

    if needs_rehash(user.password_hash):
        # Upgrade the stored hash opportunistically so KDF parameters can be
        # raised over time without forcing a password reset.
        user.password_hash = hash_password(password)

    user.last_login_at = utcnow()
    token, claims = create_access_token(user_id=user.id, email=user.email, settings=settings)

    audit.record(
        db,
        AuditAction.USER_LOGIN_SUCCEEDED,
        actor_user_id=user.id,
        summary=f"{user.email} signed in",
        details={"jti": claims.jti},
        source_ip=source_ip,
        user_agent=user_agent,
    )
    return user, token, claims


def revoke_token(
    db: Session,
    claims: TokenClaims,
    *,
    source_ip: str = "",
    user_agent: str = "",
) -> None:
    """Deny-list a token so logout actually invalidates it."""
    already = db.execute(
        select(RevokedToken).where(RevokedToken.jti == claims.jti)
    ).scalar_one_or_none()
    if already is None:
        db.add(
            RevokedToken(
                jti=claims.jti,
                user_id=claims.user_id,
                expires_at=claims.expires_at,
            )
        )
    audit.record(
        db,
        AuditAction.USER_LOGOUT,
        actor_user_id=claims.user_id,
        summary="Signed out",
        details={"jti": claims.jti},
        source_ip=source_ip,
        user_agent=user_agent,
    )


def is_token_revoked(db: Session, jti: str) -> bool:
    return db.execute(select(RevokedToken.id).where(RevokedToken.jti == jti)).scalar() is not None


def prune_revoked_tokens(db: Session, *, now: datetime | None = None) -> int:
    """Delete deny-list rows for tokens that have expired anyway."""
    cutoff = now or utcnow()
    result = db.execute(delete(RevokedToken).where(RevokedToken.expires_at < cutoff))
    return int(getattr(result, "rowcount", 0) or 0)


#: Demo accounts seeded so the offline demonstration has one user per role.
#: Passwords are intentionally simple and obviously non-production.
DEMO_USERS: tuple[tuple[str, str, RoleName, str], ...] = (
    ("engineer@esco.internal", "ESCO Engineer", RoleName.ENGINEER, "esco-demo-engineer"),
    ("reviewer@esco.internal", "ESCO Reviewer", RoleName.REVIEWER, "esco-demo-reviewer"),
)


def bootstrap(db: Session, settings: Settings) -> User:
    """Seed roles and the administrator on first boot; idempotent.

    Without this, a fresh install has no way in. Demo users are only seeded when
    ``bootstrap_demo_users`` is on, which production configuration turns off.
    """
    ensure_roles(db)

    admin = get_user_by_email(db, settings.bootstrap_admin_email)
    if admin is None:
        admin = create_user(
            db,
            email=settings.bootstrap_admin_email,
            display_name="ESCO Administrator",
            password=settings.bootstrap_admin_password.get_secret_value(),
            roles=[RoleName.ADMINISTRATOR, RoleName.ENGINEER, RoleName.REVIEWER],
            actor_label="bootstrap",
        )
        logger.info("seeded administrator account", extra={"email": admin.email})

    if settings.bootstrap_demo_users:
        for email, display_name, role, password in DEMO_USERS:
            if get_user_by_email(db, email) is None:
                create_user(
                    db,
                    email=email,
                    display_name=display_name,
                    password=password,
                    roles=[role],
                    actor_label="bootstrap",
                )
                logger.info("seeded demo account", extra={"email": email, "role": role.value})

    return admin
