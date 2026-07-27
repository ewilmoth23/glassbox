"""
Phase 6 follow-up — JsonFormatter for structured logging.

Asserts:
  - JsonFormatter produces parseable JSON with required keys
  - exc_info populates exc_type / exc_msg / stack_trace
  - extra={...} fields are folded into the top-level object
  - Reserved keys in extra are silently dropped (don't shadow standard ones)
  - Non-JSON-serializable extras fall back to repr() rather than crashing
  - configure_logging() is idempotent + honors GLASSBOX_LOG_FORMAT
  - Default 'text' format produces non-JSON output (legacy preserved)

Run:
    21_GLASSBOX_AI/.venv/bin/python -m pytest 21_GLASSBOX_AI/tests/test_log_format.py -v
"""

from __future__ import annotations

import io
import json
import logging
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from log_format import JsonFormatter, configure_logging  # noqa: E402


def _build_record(*, msg="hello world", level=logging.INFO,
                  name="ingester.test", extra=None,
                  exc_info=None) -> logging.LogRecord:
    record = logging.LogRecord(
        name=name, level=level, pathname=__file__, lineno=1,
        msg=msg, args=(), exc_info=exc_info,
    )
    if extra:
        for k, v in extra.items():
            setattr(record, k, v)
    return record


# ─── JsonFormatter ────────────────────────────────────────────────────────


def test_emits_required_top_level_keys():
    out = JsonFormatter().format(_build_record())
    parsed = json.loads(out)
    for k in ("ts", "level", "logger", "message", "thread", "pid"):
        assert k in parsed, f"missing required key {k!r}"
    assert parsed["level"] == "INFO"
    assert parsed["logger"] == "ingester.test"
    assert parsed["message"] == "hello world"


def test_ts_is_iso8601_utc():
    out = JsonFormatter().format(_build_record())
    parsed = json.loads(out)
    # Will raise if not ISO-8601
    from datetime import datetime
    datetime.fromisoformat(parsed["ts"].replace("Z", "+00:00"))
    assert "+" in parsed["ts"] or parsed["ts"].endswith("Z")


def test_extra_fields_fold_into_payload():
    out = JsonFormatter().format(_build_record(extra={
        "layer":  "planes",
        "count":  142,
        "duration_ms": 250,
    }))
    parsed = json.loads(out)
    assert parsed["layer"] == "planes"
    assert parsed["count"] == 142
    assert parsed["duration_ms"] == 250


def test_reserved_keys_in_extra_are_dropped():
    """A caller passing extra={'level': 'fake'} must NOT clobber the real level."""
    out = JsonFormatter().format(_build_record(level=logging.WARNING, extra={
        "level":   "fake",
        "logger":  "fake-logger",
        "message": "fake-msg",
    }))
    parsed = json.loads(out)
    assert parsed["level"] == "WARNING"
    assert parsed["logger"] == "ingester.test"
    assert parsed["message"] == "hello world"


def test_non_json_extra_falls_back_to_repr():
    class _Weird:
        def __repr__(self):
            return "<weird-obj>"
    out = JsonFormatter().format(_build_record(extra={"weird": _Weird()}))
    parsed = json.loads(out)
    assert parsed["weird"] == "<weird-obj>"


def test_exc_info_populates_exception_block():
    try:
        raise ValueError("boom")
    except ValueError:
        exc = sys.exc_info()
    out = JsonFormatter().format(_build_record(
        msg="ingester crashed",
        level=logging.ERROR,
        exc_info=exc,
    ))
    parsed = json.loads(out)
    assert parsed["level"] == "ERROR"
    assert parsed["exc_type"] == "ValueError"
    assert parsed["exc_msg"] == "boom"
    assert "Traceback" in parsed["stack_trace"]


# ─── configure_logging ───────────────────────────────────────────────────


def _capture_handler_output() -> tuple[logging.Handler, io.StringIO]:
    buf = io.StringIO()
    h = logging.StreamHandler(buf)
    return h, buf


def test_configure_logging_json_mode_produces_parseable_json():
    """Lines written to the configured handler must each be valid JSON
    when format='json'."""
    configure_logging(level=logging.INFO, log_format="json")
    # Replace the just-installed handler's stream with our capture buffer
    root = logging.getLogger()
    handler = root.handlers[0]
    buf = io.StringIO()
    handler.stream = buf

    logging.getLogger("ingester.test").info(
        "scan complete", extra={"layer": "planes", "count": 142},
    )
    handler.flush()
    line = buf.getvalue().strip().splitlines()[-1]
    parsed = json.loads(line)
    assert parsed["message"] == "scan complete"
    assert parsed["layer"] == "planes"
    assert parsed["count"] == 142


def test_configure_logging_text_mode_is_not_json():
    configure_logging(level=logging.INFO, log_format="text")
    root = logging.getLogger()
    handler = root.handlers[0]
    buf = io.StringIO()
    handler.stream = buf

    logging.getLogger("test").info("legacy line")
    handler.flush()
    line = buf.getvalue().strip().splitlines()[-1]
    # Legacy text format starts with [timestamp]; JSON would start with {
    assert line.startswith("[")
    with pytest.raises(json.JSONDecodeError):
        json.loads(line)


def test_configure_logging_idempotent_replaces_handlers():
    """Calling twice must replace handlers, not duplicate them."""
    configure_logging(level=logging.INFO, log_format="text")
    n1 = len(logging.getLogger().handlers)
    configure_logging(level=logging.INFO, log_format="text")
    n2 = len(logging.getLogger().handlers)
    assert n1 == n2 == 1


def test_configure_logging_reads_env_var():
    os.environ["GLASSBOX_LOG_FORMAT"] = "json"
    try:
        configure_logging(level=logging.INFO)   # no explicit log_format kwarg
        handler = logging.getLogger().handlers[0]
        assert isinstance(handler.formatter, JsonFormatter)
    finally:
        del os.environ["GLASSBOX_LOG_FORMAT"]
        # Restore text mode for the rest of the test session
        configure_logging(level=logging.INFO, log_format="text")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
