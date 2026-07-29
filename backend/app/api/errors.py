"""Exception handlers producing the single documented error envelope."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.errors import AppError
from app.core.logging import get_logger

logger = get_logger(__name__)

#: Maps the status codes Starlette raises internally onto our stable codes.
_STATUS_CODES: dict[int, str] = {
    400: "bad_request",
    401: "authentication_required",
    403: "permission_denied",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    413: "payload_too_large",
    415: "unsupported_content",
    422: "validation_error",
    429: "rate_limited",
    500: "internal_error",
    501: "capability_unavailable",
}


def _envelope(code: str, message: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"code": code, "message": message}
    if details:
        body["details"] = details
    return {"error": body}


async def app_error_handler(_request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, AppError)
    logger.info(
        "domain error",
        extra={"error_code": exc.code, "status": exc.status_code, "detail": exc.message},
    )
    return JSONResponse(status_code=exc.status_code, content=exc.to_payload())


async def http_exception_handler(_request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)
    code = _STATUS_CODES.get(exc.status_code, "http_error")
    detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
    headers = getattr(exc, "headers", None)
    return JSONResponse(
        status_code=exc.status_code,
        content=_envelope(code, detail),
        headers=headers,
    )


async def validation_exception_handler(_request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    # ``errors()`` can contain non-JSON-serialisable ``ctx`` values (exception
    # instances); stringify defensively so the handler itself cannot 500.
    problems = [
        {
            "location": list(err.get("loc", ())),
            "message": str(err.get("msg", "")),
            "type": str(err.get("type", "")),
        }
        for err in exc.errors()
    ]
    return JSONResponse(
        status_code=422,
        content=_envelope("validation_error", "Request validation failed", {"errors": problems}),
    )


async def unhandled_exception_handler(_request: Request, exc: Exception) -> JSONResponse:
    """Last resort. Logs the full traceback, returns nothing internal."""
    logger.exception("unhandled exception", extra={"error_type": type(exc).__name__})
    return JSONResponse(
        status_code=500,
        content=_envelope(
            "internal_error",
            "An unexpected internal error occurred. The request id in the "
            "X-Request-ID response header identifies it in the server log.",
        ),
    )


def install_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, app_error_handler)
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)
