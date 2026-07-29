"""Shared response envelopes and query models."""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field


class ApiModel(BaseModel):
    """Base for every API schema.

    ``from_attributes`` lets routes return ORM instances directly;
    ``use_enum_values=False`` keeps enums as enums so FastAPI documents them.
    """

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)


class ErrorDetail(ApiModel):
    code: str = Field(description="Stable, machine-readable error code.")
    message: str = Field(description="Human-readable explanation.")
    details: dict[str, Any] = Field(default_factory=dict)


class ErrorResponse(ApiModel):
    """The single error shape returned by every failing endpoint."""

    error: ErrorDetail


class Page[T](ApiModel):
    """Offset-paginated collection."""

    items: list[T]
    total: int = Field(description="Total matching rows, ignoring limit/offset.")
    limit: int
    offset: int


class PaginationParams(BaseModel):
    limit: Annotated[int, Field(ge=1, le=500)] = 50
    offset: Annotated[int, Field(ge=0)] = 0


class OkResponse(ApiModel):
    """Acknowledgement for operations with no meaningful body."""

    ok: bool = True
    message: str = ""


class CapabilityStatus(ApiModel):
    """Whether an optional, plugin-provided capability is installed.

    Reported rather than crashed on: a missing delivery plugin degrades the
    product, it does not break it.
    """

    name: str
    available: bool
    provider: str = ""
    version: str = ""
    detail: str = Field(
        default="",
        description="Why the capability is unavailable, when it is.",
    )


class HealthResponse(ApiModel):
    status: str = Field(description="``ok`` or ``degraded``.")
    app_name: str
    version: str
    contract_version: str
    environment: str
    database: str = Field(description="``ok`` or an error summary.")
    #: Providers an administrator has explicitly enabled.
    enabled_providers: list[str] = Field(default_factory=list)
    capabilities: list[CapabilityStatus] = Field(default_factory=list)
