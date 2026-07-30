"""Authorisation rules.

Two layers, both required:

* a **global role** (``engineer`` / ``reviewer`` / ``administrator``) says what
  kind of operations a user may perform at all;
* **project membership** says which projects they may perform them on.

Holding the global ``engineer`` role does not grant access to someone else's
project. Administrators are the single exception and are treated as members of
every project, because someone has to be able to recover an orphaned one.

Every rule is a pure function of (user, project role) so it can be unit-tested
without a request.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.enums import ProjectRole, RoleName
from app.core.errors import PermissionDeniedError
from app.models.project import ProjectMember
from app.models.user import User

#: Project roles that may change project content (upload, start a run).
WRITE_ROLES: frozenset[ProjectRole] = frozenset({ProjectRole.OWNER, ProjectRole.ENGINEER})

#: Project roles that may decide an approval or set a finding's disposition.
#: An engineer may not sign off their own project's safety findings; that
#: separation is the point of the reviewer role.
REVIEW_ROLES: frozenset[ProjectRole] = frozenset({ProjectRole.OWNER, ProjectRole.REVIEWER})

#: Project roles that may read.
READ_ROLES: frozenset[ProjectRole] = frozenset(ProjectRole)


def project_role_for(db: Session, user: User, project_id: uuid.UUID) -> ProjectRole | None:
    """The user's role on a project, or ``None`` if they are not a member.

    Administrators are reported as ``OWNER`` everywhere.
    """
    if user.is_administrator:
        return ProjectRole.OWNER

    membership = db.execute(
        select(ProjectMember).where(
            ProjectMember.project_id == project_id,
            ProjectMember.user_id == user.id,
        )
    ).scalar_one_or_none()
    return ProjectRole(membership.role) if membership else None


def require_global_role(user: User, *allowed: RoleName) -> None:
    """Assert the user holds at least one of the given global roles."""
    if user.is_administrator:
        return
    if not user.role_names & set(allowed):
        raise PermissionDeniedError(
            "Your account does not have the role required for this operation.",
            details={"required_any_of": sorted(role.value for role in allowed)},
        )


def require_project_access(
    role: ProjectRole | None,
    allowed: frozenset[ProjectRole],
    *,
    operation: str,
) -> ProjectRole:
    """Assert a project role is present and sufficient.

    ``None`` (not a member) and an insufficient role both raise 403 with the
    same message. Distinguishing them would let a caller enumerate which
    project ids exist.
    """
    if role is None or role not in allowed:
        raise PermissionDeniedError(
            f"You do not have permission to {operation} on this project.",
            details={"required_any_of": sorted(r.value for r in allowed)},
        )
    return role


def can_read_project(role: ProjectRole | None) -> bool:
    return role is not None


def can_write_project(role: ProjectRole | None) -> bool:
    return role in WRITE_ROLES


def can_review_project(role: ProjectRole | None) -> bool:
    return role in REVIEW_ROLES


def can_decide_approval(user: User, role: ProjectRole | None) -> bool:
    """Whether this user may record an approval decision.

    Requires both the global ``reviewer`` (or administrator) role and a
    reviewing role on the project. An approval is an engineering sign-off, so it
    takes more than project membership.
    """
    if user.is_administrator:
        return True
    return role in REVIEW_ROLES and user.has_role(RoleName.REVIEWER)


def can_manage_users(user: User) -> bool:
    return user.is_administrator


def can_delete_file(user: User, role: ProjectRole | None) -> bool:
    return user.is_administrator or role in WRITE_ROLES
