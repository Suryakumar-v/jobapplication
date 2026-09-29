"""Rotating, redacting log configuration.

Files (under the configured log directory):
  application.log  general application events
  browser.log      Playwright / browser events (logger ``app.browser``)
  error.log        ERROR and above from any ``app`` logger
  audit.log        approval, submission and safety events (logger ``app.audit``)
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from app.utils.redaction import redact_text, redact_url, redact_value

MAX_BYTES = 5 * 1024 * 1024
BACKUP_COUNT = 5

STRUCTURED_FIELDS = (
    "workflow",
    "execution_id",
    "operation",
    "job_id",
    "application_id",
    "portal",
    "url",
    "result",
    "error_type",
)

_HANDLER_TAG = "_job_automation_handler"


class RedactingFilter(logging.Filter):
    """Redacts the message and structured fields of every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact_text(record.getMessage())
        record.args = None
        for field in STRUCTURED_FIELDS:
            value = getattr(record, field, None)
            if value is None:
                continue
            setattr(
                record, field, redact_url(str(value)) if field == "url" else redact_value(value)
            )
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for field in STRUCTURED_FIELDS:
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = value
        if record.exc_info and record.exc_info[0] is not None:
            payload["error_type"] = record.exc_info[0].__name__
        return json.dumps(payload, ensure_ascii=True)


class _AuditOnly(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return record.name == "app.audit"


class _BrowserOnly(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return record.name == "app.browser" or record.name.startswith("app.browser.")


class _ExcludeAudit(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return record.name != "app.audit"


def _make_handler(
    path: Path, level: int, extra_filter: logging.Filter | None
) -> RotatingFileHandler:
    handler = RotatingFileHandler(
        path, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8", delay=True
    )
    handler.setLevel(level)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RedactingFilter())
    if extra_filter is not None:
        handler.addFilter(extra_filter)
    setattr(handler, _HANDLER_TAG, True)
    return handler


def shutdown_logging() -> None:
    """Remove and close handlers installed by :func:`setup_logging`."""
    root = logging.getLogger("app")
    for handler in list(root.handlers):
        if getattr(handler, _HANDLER_TAG, False):
            root.removeHandler(handler)
            handler.close()


def setup_logging(log_dir: Path, level: int = logging.INFO) -> None:
    """Idempotently install the four rotating log files on the ``app`` logger."""
    log_dir.mkdir(parents=True, exist_ok=True)
    shutdown_logging()
    root = logging.getLogger("app")
    root.setLevel(level)
    root.propagate = False
    handlers = (
        _make_handler(log_dir / "application.log", level, _ExcludeAudit()),
        _make_handler(log_dir / "browser.log", level, _BrowserOnly()),
        _make_handler(log_dir / "error.log", logging.ERROR, None),
        _make_handler(log_dir / "audit.log", logging.INFO, _AuditOnly()),
    )
    for handler in handlers:
        root.addHandler(handler)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name if name.startswith("app") else f"app.{name}")


def audit(operation: str, result: str, **fields: Any) -> None:
    """Write a redacted audit event (approvals, submissions, safety decisions)."""
    logging.getLogger("app.audit").info(
        "%s", operation, extra={"operation": operation, "result": result, **fields}
    )
