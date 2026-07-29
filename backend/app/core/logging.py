"""Structured JSON logging.

One JSON object per line so local log files stay greppable and can be shipped
to an internal collector without a parsing sidecar. A request-scoped correlation
id is carried in a :class:`contextvars.ContextVar` and attached to every record
emitted while handling that request.
"""

from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar
from typing import Any

from app.core.clock import isoformat_z, utcnow

_request_id: ContextVar[str | None] = ContextVar("esco_request_id", default=None)
_user_id: ContextVar[str | None] = ContextVar("esco_user_id", default=None)
_project_id: ContextVar[str | None] = ContextVar("esco_project_id", default=None)

#: Attributes present on every ``LogRecord``; anything else the caller attached
#: via ``extra=`` is treated as structured context and merged into the output.
_RESERVED = frozenset(
    vars(logging.LogRecord("", 0, "", 0, "", None, None)).keys()
    | {"message", "asctime", "taskName"}
)


def set_request_context(
    *,
    request_id: str | None = None,
    user_id: str | None = None,
    project_id: str | None = None,
) -> None:
    """Bind identifiers onto the current context for subsequent log records."""
    if request_id is not None:
        _request_id.set(request_id)
    if user_id is not None:
        _user_id.set(user_id)
    if project_id is not None:
        _project_id.set(project_id)


def clear_request_context() -> None:
    _request_id.set(None)
    _user_id.set(None)
    _project_id.set(None)


def get_request_id() -> str | None:
    return _request_id.get()


class JsonFormatter(logging.Formatter):
    """Render a ``LogRecord`` as a single-line JSON object."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": isoformat_z(utcnow()),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        for key in ("request_id", "user_id", "project_id"):
            value = {"request_id": _request_id, "user_id": _user_id, "project_id": _project_id}[
                key
            ].get()
            if value:
                payload[key] = value

        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = _coerce(value)

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)

        return json.dumps(payload, default=str, ensure_ascii=False)


class TextFormatter(logging.Formatter):
    """Human-friendly formatter for interactive local development."""

    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)-7s %(name)s :: %(message)s")

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        extras = {
            key: _coerce(value)
            for key, value in record.__dict__.items()
            if key not in _RESERVED and not key.startswith("_")
        }
        rid = _request_id.get()
        if rid:
            extras["request_id"] = rid
        return f"{base} {extras}" if extras else base


def _coerce(value: Any) -> Any:
    """Make a value JSON-safe without losing information."""
    if isinstance(value, (str, int, float, bool, type(None))):
        return value
    if isinstance(value, (list, tuple)):
        return [_coerce(item) for item in value]
    if isinstance(value, dict):
        return {str(k): _coerce(v) for k, v in value.items()}
    return str(value)


def configure_logging(*, level: str = "INFO", json_output: bool = True) -> None:
    """Install the root handler. Safe to call more than once."""
    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(JsonFormatter() if json_output else TextFormatter())

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level)

    # uvicorn installs its own colourised handlers; route them through ours so
    # the log stream stays one format.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers = []
        logger.propagate = True

    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
