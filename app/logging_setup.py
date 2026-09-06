"""
Logging, with secrets scrubbed.

WHY THE REDACTION FILTER IS INSTALLED ON THE ROOT LOGGER
========================================================
A secret that reaches a log file has escaped. Logs get tailed into support
tickets, pasted into chat, and shipped to third-party aggregators.

Being careful at every call site is necessary but not sufficient -- eventually
somebody logs an exception whose message happens to contain a token, or a
library we do not control logs a request header. So the filter goes on the root
logger and on uvicorn's own loggers, which means it applies to application
logs, access logs and third-party output alike.

See :class:`app.security.crypto.RedactingFilter` for what it strips.
"""

from __future__ import annotations

import json
import logging
import logging.config
import sys
from datetime import UTC, datetime

from app.config import settings
from app.security.crypto import RedactingFilter, redact


class JsonFormatter(logging.Formatter):
    """
    One JSON object per line.

    Preferred on the server because ``grep``, ``jq`` and every log shipper
    handle it without a custom parser, and because a multi-line traceback stays
    inside one record instead of being split across lines.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": redact(record.getMessage()),
        }
        if record.exc_info:
            payload["exc"] = redact(self.formatException(record.exc_info))
        # Anything attached with logger.info(..., extra={...}).
        for key, value in record.__dict__.items():
            if key.startswith("ctx_"):
                payload[key[4:]] = value
        return json.dumps(payload, ensure_ascii=False, default=str)


class HumanFormatter(logging.Formatter):
    """Readable single-line output, for development."""

    def __init__(self) -> None:
        super().__init__(
            fmt="%(asctime)s %(levelname)-8s %(name)-34s %(message)s",
            datefmt="%H:%M:%S",
        )

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def configure() -> None:
    """
    Set up logging. Idempotent; safe to call more than once.

    Called before anything else in :mod:`app.main`, so that even a failure
    during startup is logged with secrets already scrubbed.
    """
    formatter = JsonFormatter() if settings.log_json else HumanFormatter()

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    handler.addFilter(RedactingFilter())

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(getattr(logging, settings.log_level.upper(), logging.INFO))

    # Uvicorn installs its own handlers, which would bypass the filter.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers = [handler]
        lg.propagate = False

    # Libraries that are chatty at INFO and tell us nothing useful.
    logging.getLogger("apscheduler.executors.default").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    # SQLAlchemy's echo is controlled by the engine, not here; this only stops
    # its own informational chatter.
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)

    logging.getLogger(__name__).info(
        "logging configured: level=%s format=%s",
        settings.log_level, "json" if settings.log_json else "text",
    )
