"""Structured operational events with a deliberately tiny data vocabulary."""
from __future__ import annotations

import logging
import re
from typing import Any

import structlog

_EVENT_NAME = re.compile(r"^[a-z][a-z0-9_.-]{0,79}$")
_SAFE_FIELDS = {
    "attempt",
    "component",
    "count",
    "elapsed_ms",
    "object_type",
    "operation",
    "outcome",
    "provider",
    "status_code",
}

structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.JSONRenderer(sort_keys=True),
    ],
    logger_factory=structlog.stdlib.LoggerFactory(),
    wrapper_class=structlog.stdlib.BoundLogger,
    cache_logger_on_first_use=True,
)


class SafeLogger:
    """Reject arbitrary keys and free-form event text before it reaches logs."""

    def __init__(self, component: str):
        self._component = component if _EVENT_NAME.fullmatch(component) else "application"
        self._logger = structlog.get_logger("shimline")

    def _emit(self, level: str, event: str, **fields: Any) -> None:
        safe_event = event if _EVENT_NAME.fullmatch(event) else "invalid_event_name"
        safe = {key: value for key, value in fields.items() if key in _SAFE_FIELDS}
        safe["component"] = self._component
        getattr(self._logger, level)(safe_event, **safe)

    def info(self, event: str, **fields: Any) -> None:
        self._emit("info", event, **fields)

    def warning(self, event: str, **fields: Any) -> None:
        self._emit("warning", event, **fields)

    def error(self, event: str, **fields: Any) -> None:
        self._emit("error", event, **fields)


def get_logger(component: str) -> SafeLogger:
    logging.getLogger("shimline").setLevel(logging.INFO)
    return SafeLogger(component)
