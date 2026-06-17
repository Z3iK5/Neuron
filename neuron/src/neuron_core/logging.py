"""Structured logging for Neuron services.

Why structured logs? When services run in containers, logs are usually shipped
to a central system (e.g. Loki, CloudWatch). Machine-readable **JSON** lines are
far easier to search and filter than free-form text. For local development a
human-friendly ``console`` format is nicer to read.

Usage in a service::

    from neuron_core import configure_logging, get_logger

    configure_logging(level="INFO", fmt="json")
    log = get_logger(__name__)
    log.info("starting up", extra={"service": "neuron-console"})

This intentionally uses only the Python standard library (no extra dependency)
so the logging path is easy to understand and debug.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

# Standard LogRecord attributes we do NOT want to duplicate when collecting the
# caller's extra fields. Anything not in this set (and not private) is treated as
# a custom structured field added via ``logger.info(..., extra={...})``.
_RESERVED_LOGRECORD_FIELDS = {
    "args", "asctime", "created", "exc_info", "exc_text", "filename", "funcName",
    "levelname", "levelno", "lineno", "module", "msecs", "message", "msg", "name",
    "pathname", "process", "processName", "relativeCreated", "stack_info",
    "thread", "threadName", "taskName",
}


class JsonFormatter(logging.Formatter):
    """Format each log record as a single JSON object on one line."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        # Include any custom fields passed via `extra={...}`.
        for key, value in record.__dict__.items():
            if key not in _RESERVED_LOGRECORD_FIELDS and not key.startswith("_"):
                payload[key] = value
        # Include exception details if present.
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO", fmt: str = "json") -> None:
    """Configure the root logger once, at service startup.

    :param level: a Python log level name, e.g. "DEBUG", "INFO", "WARNING".
    :param fmt: "json" for machine-readable logs, "console" for human-readable.
    """
    handler = logging.StreamHandler(stream=sys.stdout)
    if fmt == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s")
        )

    root = logging.getLogger()
    root.handlers.clear()  # avoid duplicate handlers if called more than once
    root.addHandler(handler)
    root.setLevel(level.upper())


def get_logger(name: str) -> logging.Logger:
    """Return a named logger. Use ``__name__`` as the name in each module."""
    return logging.getLogger(name)
