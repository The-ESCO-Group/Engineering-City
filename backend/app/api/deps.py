"""FastAPI dependencies: authentication, authorisation, request provenance.

Route handlers declare what access they need and get a validated user (and
project role) or a 401/403. No handler reads a token or checks a role itself.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Header, Path, Request
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.enums import ProjectRole, RoleName
from app.core.errors import AuthenticationError, PermissionDeniedError
from app.core.logging import set_request_context
from app.core.services import projects as project_service
from app.core.services import users as user_service
from app.database.session import get_db
from app.models.project import Project
from app.models.user import User
from app.security import permissions
from app.security.tokens import TokenClaims, decode_access_token


@dataclass(frozen=True, slots=True)
class RequestContext:
    """Provenance recorded on audit entries."""

    source_ip: str = ""
    user_agent: str = ""


def get_request_context(
    request: Request,
    user_agent: Annotated[str | None, Header(alias="User-Agent")] = None,
) -> RequestContext:
    client = request.client
    return RequestContext(
        source_ip=client.host if client else "",
        user_agent=(user_agent or "")[:255],
    )


def _extract_token(request: Request, settings: Settings) -> str:
    """Read the token from the Authorization header, else the session cookie.

    Both are supported because the browser frontend uses an httpOnly cookie
    while the desktop shell cannot, and neither should have to work around the
    other's transport.
    """
    header = request.headers.get("Authorization", "")
    if header:
        scheme, _, value = header.partition(" ")
        if scheme.lower() != "bearer" or not value.strip():
            raise AuthenticationError("Authorization header must be 'Bearer <token>'.")
        return value.strip()

    cookie = request.cookies.get(settings.session_cookie_name)
    if cookie:
        return cookie

    raise AuthenticationError("Authentication required.")


def get_token_claims(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> TokenClaims:
    """Validate the presented token, including the logout deny-list."""
    claims = decode_access_token(_extract_token(request, settings), settings)
    if user_service.is_token_revoked(db, claims.jti):
        raise AuthenticationError("Session has been signed out. Sign in again.")
    return claims


def get_current_user(
    claims: Annotated[TokenClaims, Depends(get_token_claims)],
    db: Annotated[Session, Depends(get_db)],
) -> User:
    """The authenticated user.

    A token for a deleted or disabled account is rejected here, so deactivating
    someone takes effect immediately rather than at token expiry.
    """
    user = db.get(User, claims.user_id)
    if user is None:
        raise AuthenticationError("Account no longer exists.")
    if not user.is_active:
        raise AuthenticationError("Account is disabled.")

    set_request_context(user_id=str(user.id))
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]
DbSession = Annotated[Session, Depends(get_db)]
AppSettings = Annotated[Settings, Depends(get_settings)]
Provenance = Annotated[RequestContext, Depends(get_request_context)]


def require_administrator(user: CurrentUser) -> User:
    if not user.is_administrator:
        raise PermissionDeniedError("This operation requires the administrator role.")
    return user


Administrator = Annotated[User, Depends(require_administrator)]


def require_engineer(user: CurrentUser) -> User:
    permissions.require_global_role(user, RoleName.ENGINEER)
    return user


@dataclass(frozen=True, slots=True)
class ProjectAccess:
    """A project the caller is authorised for, with their role on it."""

    project: Project
    role: ProjectRole
    user: User

    @property
    def can_write(self) -> bool:
        return permissions.can_write_project(self.role)

    @property
    def can_review(self) -> bool:
        return permissions.can_review_project(self.role)


def _load_project_access(
    db: Session,
    user: User,
    project_id: uuid.UUID,
    allowed: frozenset[ProjectRole],
    operation: str,
) -> ProjectAccess:
    project = project_service.get_project(db, project_id)
    role = permissions.project_role_for(db, user, project_id)
    permissions.require_project_access(role, allowed, operation=operation)
    set_request_context(project_id=str(project_id))
    assert role is not None  # require_project_access raises otherwise
    return ProjectAccess(project=project, role=role, user=user)


def project_read_access(
    project_id: Annotated[uuid.UUID, Path()],
    db: DbSession,
    user: CurrentUser,
) -> ProjectAccess:
    return _load_project_access(db, user, project_id, permissions.READ_ROLES, "view")


def project_write_access(
    project_id: Annotated[uuid.UUID, Path()],
    db: DbSession,
    user: CurrentUser,
) -> ProjectAccess:
    return _load_project_access(db, user, project_id, permissions.WRITE_ROLES, "make changes")


def project_review_access(
    project_id: Annotated[uuid.UUID, Path()],
    db: DbSession,
    user: CurrentUser,
) -> ProjectAccess:
    return _load_project_access(db, user, project_id, permissions.REVIEW_ROLES, "review findings")


ProjectRead = Annotated[ProjectAccess, Depends(project_read_access)]
ProjectWrite = Annotated[ProjectAccess, Depends(project_write_access)]
ProjectReview = Annotated[ProjectAccess, Depends(project_review_access)]


def authorise_project(
    db: Session,
    user: User,
    project_id: uuid.UUID,
    allowed: frozenset[ProjectRole],
    operation: str,
) -> ProjectAccess:
    """Authorise access to a project reached indirectly.

    Used by routes keyed on a child id (``/api/files/{file_id}``), where the
    project is only known after the child row is loaded.
    """
    return _load_project_access(db, user, project_id, allowed, operation)
