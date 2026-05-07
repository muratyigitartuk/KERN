"""Structured logging configuration for KERN.

Call ``setup_logging()`` once at process startup â€” before any other import
that touches ``logging.getLogger``.  The function reads two env-vars
(already surfaced in ``app.config.Settings``):

* ``KERN_LOG_LEVEL``  â€“ DEBUG / INFO / WARNING / ERROR (default: INFO)
* ``KERN_LOG_FORMAT`` â€“ ``json`` or ``text`` (default: ``text``)
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone

_CONFIGURED = False


class _JSONFormatter(logging.Formatter):
    """Single-line JSON log formatter for production deployments."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info and record.exc_info[0] is not None:
            payload["error"] = self.formatException(record.exc_info)
        request_id = getattr(record, "request_id", None)
        if request_id:
            payload["request_id"] = request_id
        return json.dumps(payload, ensure_ascii=False)


_TEXT_FORMAT = "%(asctime)s  %(levelname)-8s  %(name)s  [%(request_id)s]  %(message)s"


class _RequestIdSafeFormatter(logging.Formatter):
    """Text formatter that supplies a default ``request_id`` when absent."""

    def format(self, record: logging.LogRecord) -> str:
        if not hasattr(record, "request_id") or record.request_id is None:  # type: ignore[attr-defined]
            record.request_id = "-"  # type: ignore[attr-defined]
        return super().format(record)


def setup_logging() -> None:
    """Configure the root logger once.  Safe to call multiple times."""
    global _CONFIGURED
    if _CONFIGURED:
        return
    _CONFIGURED = True

    level_name = os.getenv("KERN_LOG_LEVEL", "INFO").strip().upper()
    level = getattr(logging, level_name, logging.INFO)

    log_format = os.getenv("KERN_LOG_FORMAT", "text").strip().lower()

    handler = logging.StreamHandler(sys.stderr)
    if log_format == "json":
        handler.setFormatter(_JSONFormatter())
    else:
        handler.setFormatter(_RequestIdSafeFormatter(_TEXT_FORMAT))

    root = logging.getLogger()
    root.setLevel(level)
    # Remove any pre-existing handlers to avoid duplicate output.
    root.handlers.clear()
    root.addHandler(handler)

    # Suppress noisy third-party loggers in production.
    for noisy in ("httpx", "httpcore", "watchdog", "urllib3", "asyncio"):
        logging.getLogger(noisy).setLevel(max(level, logging.WARNING))
