"""
log_format.py — Phase 6 starter: JSON log formatter.

Turn it on by setting GLASSBOX_LOG_FORMAT=json at server startup.
Default ("text") preserves the legacy human-readable output that
operators following `tail -f` rely on; the JSON path is for log
aggregators (Loki, Vector, Promtail, Datadog Agent, etc.) that index
fields rather than parse free text.

Each JSON record carries:
  ts        ISO-8601 UTC, microsecond precision
  level     "INFO" / "WARNING" / "ERROR" / ...
  logger    `logging.LogRecord.name` (e.g. "ingester.planes")
  message   the formatted message
  thread    thread id (`%(thread)d`)
  pid       process id (`%(process)d`)

If the record carries an exception, `exc_type` / `exc_msg` /
`stack_trace` (the formatted traceback) are added.

Extra fields passed via `logger.info(..., extra={...})` are folded into
the top-level JSON object — handy for structured event tagging
(`logger.info("scan complete", extra={"layer": "planes", "count": 142})`).

Reserved keys in `extra` that would shadow standard ones are skipped
defensively so a careless caller can't break the schema.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict


_RESERVED = {
    "ts", "level", "logger", "message", "thread", "pid",
    "exc_type", "exc_msg", "stack_trace",
    # logging.LogRecord internals — never want these surfacing as fields
    "args", "asctime", "created", "exc_info", "exc_text", "filename",
    "funcName", "levelname", "levelno", "lineno", "module", "msecs",
    "msg", "name", "pathname", "process", "processName", "relativeCreated",
    "stack_info", "thread", "threadName", "taskName",
}


class JsonFormatter(logging.Formatter):
    """logging.Formatter that emits one JSON object per record."""

    def format(self, record: logging.LogRecord) -> str:
        payload: Dict[str, Any] = {
            "ts":       datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level":    record.levelname,
            "logger":   record.name,
            "message":  record.getMessage(),
            "thread":   record.thread,
            "pid":      record.process,
        }
        # Exception block, when present
        if record.exc_info:
            etype, eobj, _tb = record.exc_info
            payload["exc_type"] = etype.__name__ if etype else None
            payload["exc_msg"]  = str(eobj) if eobj else None
            payload["stack_trace"] = self.formatException(record.exc_info)
        # Caller-supplied extras (logger.info(..., extra={...}))
        for k, v in record.__dict__.items():
            if k in _RESERVED:
                continue
            try:
                json.dumps(v)
                payload[k] = v
            except (TypeError, ValueError):
                payload[k] = repr(v)
        try:
            return json.dumps(payload, default=str)
        except (TypeError, ValueError):
            # Last-ditch fallback so a serialization bug never silences logs.
            return json.dumps({
                "ts": payload["ts"],
                "level": payload["level"],
                "logger": payload["logger"],
                "message": str(payload["message"]),
                "format_error": "json.dumps failed",
            })


def configure_logging(*, level: int = logging.INFO,
                      log_format: str | None = None) -> None:
    """Configure the root logger.

    `log_format` may be 'json' or 'text' (default). When omitted, reads
    GLASSBOX_LOG_FORMAT from the environment and falls back to 'text'.
    Existing handlers on the root logger are removed first so this is
    idempotent across server restarts inside the same process.
    """
    if log_format is None:
        log_format = os.environ.get("GLASSBOX_LOG_FORMAT", "text").lower()

    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)

    handler = logging.StreamHandler()
    if log_format == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter(
            "[%(asctime)s] %(name)s %(levelname)s: %(message)s"
        ))
    root.addHandler(handler)
    root.setLevel(level)
