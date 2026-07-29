"""Authentication and user schemas."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from pydantic import EmailStr, Field, field_validator

from app.api.schemas.common import ApiModel
from app.core.enums import RoleName


class LoginRequest(ApiModel):
    email: EmailStr
    password: Annotated[str, Field(min_length=1, max_length=256)]


class UserSummary(ApiModel):
    id: uuid.UUID
    email: str
    display_name: str
    is_active: bool
    roles: list[RoleName] = Field(default_factory=list)

    @field_validator("roles", mode="before")
    @classmethod
    def _role_objects_to_names(cls, value: object) -> object:
        """Accept the ORM's ``list[Role]`` as well as a plain list of names."""
        if isinstance(value, (list, tuple, set)):
            return [getattr(item, "name", item) for item in value]
        return value


class LoginResponse(ApiModel):
    """Returned on success.

    The token is also set as an httpOnly cookie; it is echoed in the body so a
    desktop shell that cannot use cookies can send it as a bearer token.
    """

    access_token: str
    token_type: str = "bearer"  # noqa: S105 - the scheme name, not a credential
    expires_at: datetime
    user: UserSummary


class CurrentUserResponse(ApiModel):
    user: UserSummary
    #: Project ids the caller is a member of, with their role on each.
    project_roles: dict[str, str] = Field(default_factory=dict)


class CreateUserRequest(ApiModel):
    """Administrator-only user provisioning."""

    email: EmailStr
    display_name: Annotated[str, Field(min_length=1, max_length=120)]
    password: Annotated[str, Field(min_length=12, max_length=256)]
    roles: list[RoleName] = Field(default_factory=lambda: [RoleName.ENGINEER])
