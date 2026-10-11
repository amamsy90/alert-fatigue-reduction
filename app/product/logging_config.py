"""Optional JSON logs when LOG_FORMAT=json (stdout and the audit file)."""
from __future__ import annotations

import json
import logging
import os


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        # log.exception() must keep its traceback, or a production error is undebuggable.
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)
        return json.dumps(payload, default=str)


def json_logs_enabled() -> bool:
    return os.environ.get("LOG_FORMAT", "").strip().lower() == "json"


def configure_logging() -> None:
    """Replace root and audit formatters with JSON when LOG_FORMAT=json."""
    if not json_logs_enabled():
        return
    formatter = JsonFormatter()
    root = logging.getLogger()
    if not root.handlers:
        stream = logging.StreamHandler()
        stream.setFormatter(formatter)
        root.addHandler(stream)
        root.setLevel(logging.INFO)
    else:
        for existing in root.handlers:
            existing.setFormatter(formatter)
    audit = logging.getLogger("oncall.audit")
    for existing in audit.handlers:
        existing.setFormatter(formatter)
