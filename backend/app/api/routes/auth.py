"""Authentication endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy import select

from app.api.deps import (
    Administrator,
    AppSettings,
    CurrentUser,
    DbSession,
    Provenance,
    get_token_claims,
)
from app.api.schemas.auth import (
    CreateUserRequest,
    CurrentUserResponse,
    LoginRequest,
    LoginResponse,
    UserSummary,
)
from app.api.schemas.common import OkResponse
from app.core.errors import AuthenticationError
from app.core.services import users as user_service
from app.models.project import ProjectMember
from app.security.tokens import TokenClaims

router = APIRouter(prefix="/auth", tags=["authentication"])


def _set_session_cookie(
    response: Response, token: str, *, settings: AppSettings, max_age: int
) -> None:
    """Store the token in an httpOnly cookie.

    ``httponly`` keeps it out of reach of page scripts; ``samesite=lax`` is
    enough for a local app whose only cross-site exposure is top-level
    navigation.
    """
    response.set_cookie(
        key=settings.session_cookie_name,
        value=token,
        max_age=max_age,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
        path="/",
    )


@router.post(
    "/login",
    response_model=LoginResponse,
    summary="Exchange credentials for an access token",
)
def login(
    payload: LoginRequest,
    response: Response,
    db: DbSession,
    settings: AppSettings,
    provenance: Provenance,
) -> LoginResponse:
    """Authenticate locally.

    Both success and failure are audited. Every failure returns the same message
    regardless of cause, so the response cannot be used to enumerate accounts.
    """
    try:
        user, token, claims = user_service.authenticate(
            db,
            email=payload.email,
            password=payload.password,
            settings=settings,
            source_ip=provenance.source_ip,
            user_agent=provenance.user_agent,
        )
    except AuthenticationError:
        # ``authenticate`` added a USER_LOGIN_FAILED entry before raising, and
        # nothing else in this request will commit. Without this the entire
        # record of failed sign-in attempts would be discarded when the session
        # closes — which is exactly the record you want after an incident.
        db.commit()
        raise
    db.commit()

    _set_session_cookie(
        response, token, settings=settings, max_age=settings.access_token_ttl_seconds
    )
    return LoginResponse(
        access_token=token,
        expires_at=claims.expires_at,
        user=UserSummary.model_validate(user),
    )


@router.post(
    "/logout",
    response_model=OkResponse,
    summary="Revoke the current access token",
)
def logout(
    response: Response,
    db: DbSession,
    settings: AppSettings,
    provenance: Provenance,
    claims: TokenClaims = Depends(get_token_claims),
) -> OkResponse:
    """Deny-list the presented token and clear the cookie.

    The token is genuinely invalidated server-side; this is not a client-side
    gesture that leaves a stolen token usable until it expires.
    """
    user_service.revoke_token(
        db,
        claims,
        source_ip=provenance.source_ip,
        user_agent=provenance.user_agent,
    )
    db.commit()

    response.delete_cookie(settings.session_cookie_name, path="/")
    return OkResponse(message="Signed out.")


@router.get(
    "/me",
    response_model=CurrentUserResponse,
    summary="The authenticated user and their project roles",
)
def me(user: CurrentUser, db: DbSession) -> CurrentUserResponse:
    memberships = db.execute(
        select(ProjectMember).where(ProjectMember.user_id == user.id)
    ).scalars()
    return CurrentUserResponse(
        user=UserSummary.model_validate(user),
        project_roles={str(m.project_id): str(m.role) for m in memberships},
    )


@router.post(
    "/users",
    response_model=UserSummary,
    status_code=status.HTTP_201_CREATED,
    summary="Create a user (administrator only)",
)
def create_user(
    payload: CreateUserRequest,
    admin: Administrator,
    db: DbSession,
    provenance: Provenance,
) -> UserSummary:
    user = user_service.create_user(
        db,
        email=payload.email,
        display_name=payload.display_name,
        password=payload.password,
        roles=payload.roles,
        actor_user_id=admin.id,
    )
    db.commit()
    return UserSummary.model_validate(user)


@router.get(
    "/users",
    response_model=list[UserSummary],
    summary="List users (administrator only)",
)
def list_users(admin: Administrator, db: DbSession) -> list[UserSummary]:
    from app.models.user import User

    rows = db.execute(select(User).order_by(User.display_name)).scalars()
    return [UserSummary.model_validate(row) for row in rows]
