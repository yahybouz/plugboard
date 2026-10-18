"""Structured JSON logging with per-request context.

Logs are emitted to stdout in JSON format — compatible with
Docker log drivers, Loki, Elasticsearch, and CloudWatch.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import sys
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path

_request_id_var: ContextVar[str] = ContextVar("request_id", default="-")

_APPLICATION = os.environ.get("APP_NAME", "plugboard")
_SERVICE = os.environ.get("SERVICE_NAME", "plugboard")
_PID = os.getpid()

# Optional file sink — when PLUGBOARD_LOG_FILE is set (e.g. "logs/app.log"),
# we also write to disk with rotation. Rotation defaults: 20MB per file,
# keep 5 backups => 100MB ceiling per service.
_LOG_FILE = os.environ.get("PLUGBOARD_LOG_FILE", "").strip()
_LOG_MAX_BYTES = int(os.environ.get("PLUGBOARD_LOG_MAX_BYTES", str(20 * 1024 * 1024)))
_LOG_BACKUP_COUNT = int(os.environ.get("PLUGBOARD_LOG_BACKUPS", "5"))


def _source_service(logger_name: str) -> str:
    """Return a compact service name inferred from logger name."""
    if not logger_name:
        return "-"
    if logger_name.startswith("plugboard."):
        # plugboard.router -> router
        return logger_name.split(".", 1)[1]
    if logger_name == "plugboard":
        return "core"
    return logger_name


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict = {
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
            + "Z",
            "level": record.levelname,
            "application": _APPLICATION,
            "logger": record.name,
            "service": _SERVICE,
            "source_service": _source_service(record.name),
            "pid": _PID,
            "request_id": _request_id_var.get(),
            "msg": record.getMessage(),
        }
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False)


def setup_logging(level: int = logging.INFO) -> None:
    from plugboard.log_redact import RedactingFilter

    redact_filter = RedactingFilter()
    formatter = _JsonFormatter()
    root = logging.getLogger()
    root.handlers.clear()

    # Console sink — always on (containers, devs).
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    stream_handler.addFilter(redact_filter)
    root.addHandler(stream_handler)

    # Rotating file sink — only when PLUGBOARD_LOG_FILE is set so tests
    # and ephemeral contexts don't accidentally spill logs on disk.
    if _LOG_FILE:
        try:
            log_path = Path(_LOG_FILE)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            file_handler = logging.handlers.RotatingFileHandler(
                log_path,
                maxBytes=_LOG_MAX_BYTES,
                backupCount=_LOG_BACKUP_COUNT,
                encoding="utf-8",
            )
            file_handler.setFormatter(formatter)
            file_handler.addFilter(redact_filter)
            root.addHandler(file_handler)
        except Exception as exc:  # pragma: no cover — best-effort
            # Don't let a bad log path kill the app; stream sink still works.
            sys.stderr.write(f"warning: could not open log file {_LOG_FILE!r}: {exc}\n")

    root.setLevel(level)
