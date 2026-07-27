"""
Phase 6 follow-up — /api/v1/metrics Prometheus exposition format.

Asserts:
  - _render_prometheus produces well-formed line-based output
  - HELP + TYPE comments precede each metric family
  - Labels are properly quoted + escaped
  - DB / pool / ingester / findings / splink families render
  - Body ends with a newline (Prometheus spec requires it)
  - Empty ingester list still renders without crashing

Run:
    21_GLASSBOX_AI/.venv/bin/python -m pytest 21_GLASSBOX_AI/tests/test_metrics_endpoint.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db import init_pool, close_pool  # noqa: E402
from web.routes.api_v1.health_metrics import (  # noqa: E402
    build_health_snapshot,
    _render_prometheus,
    _esc_label,
)


@pytest.fixture(autouse=True)
async def _pool():
    await init_pool()
    yield
    await close_pool()


class _FakeIngester:
    def __init__(self, *, layer="planes", health="ok", cycles_failed=0):
        self.layer = layer
        self._health = health
        self._cf = cycles_failed

    def status(self):
        return {
            "layer":               self.layer,
            "source":              "fake",
            "health":              self._health,
            "tracked_entities":    1000,
            "cycles_run":          50,
            "cycles_failed":       self._cf,
            "db_write_failures":   0,
        }


# ─── _esc_label ───────────────────────────────────────────────────────────


def test_esc_label_handles_special_chars():
    assert _esc_label("simple") == "simple"
    assert _esc_label('quote"inside') == 'quote\\"inside'
    assert _esc_label("back\\slash") == "back\\\\slash"
    assert _esc_label("line\nbreak") == "line\\nbreak"
    assert _esc_label(None) == ""
    assert _esc_label(42) == "42"


# ─── _render_prometheus ──────────────────────────────────────────────────


async def test_render_includes_db_metrics():
    snap = await build_health_snapshot([], None)
    text = _render_prometheus(snap)
    assert "glassbox_db_up" in text
    assert "# HELP glassbox_db_up" in text
    assert "# TYPE glassbox_db_up gauge" in text
    assert "glassbox_db_query_latency_ms" in text


async def test_render_includes_pool_metrics_when_initialized():
    snap = await build_health_snapshot([], None)
    text = _render_prometheus(snap)
    if snap["pool"].get("initialized"):
        assert "glassbox_pool_size" in text
        assert "glassbox_pool_in_use" in text


async def test_render_emits_one_line_per_status_per_ingester():
    """Each ingester gets 3 health lines (one per status), so the output
    is monotonic in number of ingesters. Lets monitors plot 'count of
    degraded ingesters' as a single sum() query."""
    snap = await build_health_snapshot([
        _FakeIngester(layer="planes", health="ok"),
        _FakeIngester(layer="ships",  health="degraded", cycles_failed=2),
    ], None)
    text = _render_prometheus(snap)
    # Each layer should appear in 3 health lines
    for layer in ("planes", "ships"):
        for status in ("ok", "degraded", "down"):
            line = (f'glassbox_ingester_health{{layer="{layer}",'
                    f'status="{status}"}}')
            assert line in text, f"missing {line}"
    # Spot check: planes should be ok=1 degraded=0 down=0
    assert 'glassbox_ingester_health{layer="planes",status="ok"} 1' in text
    assert 'glassbox_ingester_health{layer="planes",status="degraded"} 0' in text
    # ships should be degraded=1 ok=0 down=0
    assert 'glassbox_ingester_health{layer="ships",status="degraded"} 1' in text
    assert 'glassbox_ingester_health{layer="ships",status="ok"} 0' in text


async def test_render_emits_counters_per_ingester():
    snap = await build_health_snapshot([
        _FakeIngester(layer="planes", cycles_failed=2),
    ], None)
    text = _render_prometheus(snap)
    assert 'glassbox_ingester_cycles_total{layer="planes"} 50' in text
    assert 'glassbox_ingester_cycles_failed_total{layer="planes"} 2' in text
    assert 'glassbox_ingester_tracked_entities{layer="planes"} 1000' in text


async def test_render_emits_findings_when_present():
    snap = await build_health_snapshot([_FakeIngester()], None)
    text = _render_prometheus(snap)
    if snap["findings"].get("err"):
        # DB count failed; metric line absent — acceptable
        return
    assert "glassbox_findings_5m" in text
    assert "glassbox_findings_60m" in text


async def test_render_emits_splink_when_present():
    fake_run = {
        "started_at": "2026-05-09T17:00:00+00:00",
        "completed_at": "2026-05-09T17:00:01+00:00",
        "duration_ms": 1024, "predicted": 192, "persisted": 7, "error": None,
    }
    snap = await build_health_snapshot([], None, last_splink_run=fake_run)
    text = _render_prometheus(snap)
    assert 'glassbox_splink_predicted 192' in text
    assert 'glassbox_splink_persisted 7' in text
    assert 'glassbox_splink_duration_ms 1024' in text


async def test_render_handles_zero_ingesters():
    snap = await build_health_snapshot([], None)
    text = _render_prometheus(snap)
    # No ingester families — but DB / pool / findings still emit
    assert "glassbox_db_up" in text
    # No glassbox_ingester_health lines because no items
    assert "glassbox_ingester_health{" not in text


async def test_render_body_ends_with_newline():
    """Prometheus spec: response must end with a newline."""
    snap = await build_health_snapshot([_FakeIngester()], None)
    text = _render_prometheus(snap)
    assert text.endswith("\n")


async def test_render_label_special_chars_escaped():
    """An ingester layer with quotes in the name must produce a parseable
    label. Real ingesters don't do this but the formatter must defend."""
    snap = await build_health_snapshot([
        _FakeIngester(layer='evil"layer'),
    ], None)
    text = _render_prometheus(snap)
    # The escape should appear in the rendered label
    assert 'layer="evil\\"layer"' in text


async def test_render_emits_sla_breach_gauge_per_ingester():
    """Phase 6 SLA monitor adds glassbox_ingester_sla_breach + secs metrics."""
    from datetime import datetime, timezone, timedelta

    class _Stale:
        layer = "stale"
        def status(self):
            return {"layer": "stale", "health": "ok",
                    "poll_interval_sec": 30,
                    "last_fetch_ts": (datetime.now(timezone.utc) -
                                      timedelta(seconds=600)).isoformat(),
                    "tracked_entities": 100, "cycles_run": 5}

    snap = await build_health_snapshot([_Stale()], None)
    text = _render_prometheus(snap)
    assert 'glassbox_ingester_sla_breach{layer="stale"} 1' in text
    assert 'glassbox_ingester_secs_since_last_fetch{layer="stale"}' in text


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
