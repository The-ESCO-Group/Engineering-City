"""Domain errors and their HTTP representation.

Route handlers raise these instead of ``HTTPException`` so that domain logic
stays framework-agnostic and every error reaches the client in one shape:

.. code-block:: json

    {"error": {"code": "file_rejected", "message": "...", "details": {...}}}
"""

from __future__ import annotations

from typing import Any


class AppError(Exception):
    """Base class for expected, client-reportable failures.

    Unexpected exceptions are deliberately *not* subclasses: they surface as a
    500 with an opaque message so internals are never leaked.
    """

    status_code: int = 400
    code: str = "app_error"

    def __init__(
        self,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        code: str | None = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}
        if code is not None:
            self.code = code
        if status_code is not None:
            self.status_code = status_code

    def to_payload(self) -> dict[str, Any]:
        body: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details:
            body["details"] = self.details
        return {"error": body}


class ValidationError(AppError):
    status_code = 422
    code = "validation_error"


class AuthenticationError(AppError):
    status_code = 401
    code = "authentication_required"


class PermissionDeniedError(AppError):
    status_code = 403
    code = "permission_denied"


class NotFoundError(AppError):
    status_code = 404
    code = "not_found"

    def __init__(self, entity: str, entity_id: object | None = None, **kwargs: Any) -> None:
        message = f"{entity} not found" if entity_id is None else f"{entity} {entity_id} not found"
        super().__init__(message, **kwargs)


class ConflictError(AppError):
    """The request is valid but conflicts with current state."""

    status_code = 409
    code = "conflict"


class InvalidStateTransitionError(ConflictError):
    code = "invalid_state_transition"

    def __init__(self, entity: str, current: object, requested: object) -> None:
        super().__init__(
            f"cannot move {entity} from {current} to {requested}",
            details={"entity": entity, "current": str(current), "requested": str(requested)},
        )


class PayloadTooLargeError(AppError):
    status_code = 413
    code = "payload_too_large"


class UnsupportedContentError(AppError):
    """The file type is understood but deliberately not processed.

    Distinct from a parse failure: this is a documented capability boundary, and
    it is surfaced to the user rather than silently ignored.
    """

    status_code = 415
    code = "unsupported_content"


class FileRejectedError(AppError):
    """Intake refused the file on safety grounds (type, size, archive shape)."""

    status_code = 400
    code = "file_rejected"


class ParserError(AppError):
    """A parser could not produce a usable artifact from a file."""

    status_code = 422
    code = "parser_error"


class ProviderError(AppError):
    """A model provider call failed."""

    status_code = 502
    code = "provider_error"


class ProviderNotEnabledError(ProviderError):
    status_code = 400
    code = "provider_not_enabled"


class ProviderRateLimitedError(ProviderError):
    status_code = 429
    code = "provider_rate_limited"

    def __init__(self, message: str, *, retry_after_seconds: float | None = None) -> None:
        super().__init__(
            message,
            details={} if retry_after_seconds is None else {"retry_after": retry_after_seconds},
        )
        self.retry_after_seconds = retry_after_seconds


class BudgetExceededError(AppError):
    """A token or cost budget would be exceeded by continuing."""

    status_code = 409
    code = "budget_exceeded"


class AgentActionRejectedError(AppError):
    """The backend refused to execute a model-proposed action.

    Carries the machine-readable reason so it can be persisted on the agent run
    and shown to the engineer — rejections are part of the audit trail, not
    something to swallow.
    """

    status_code = 422
    code = "agent_action_rejected"

    def __init__(self, reason: str, *, action_type: str | None = None, **details: Any) -> None:
        super().__init__(reason, details={"action_type": action_type, **details})
        self.reason = reason
        self.action_type = action_type


class CapabilityUnavailableError(AppError):
    """An optional plugin (delivery agent, report provider) is not installed.

    Returned instead of crashing so the core product still starts and can report
    precisely which capability is missing.
    """

    status_code = 501
    code = "capability_unavailable"
