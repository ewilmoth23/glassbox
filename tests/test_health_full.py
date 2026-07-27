"""
Phase 6 starter — `build_health_snapshot` helper for /api/v1/health/full.

Asserts the helper assembles the documented payload (status / db / pool /
ingesters / algorithms / findings) with the correct aggregate-status
grading. The helper is tested directly (no FastAPI HTTP layer) so the
asyncpg pool can stay on a single event loop; the HTTP wrapper just
JSON-encodes the dict.

Run:
    21_GLASSBOX_AI/.venv/bin/python -m pytest 21_GLASSBOX_AI/tests/test_health_full.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db import init_pool, close_pool, pool_stats  # noqa: E402
from web.routes.api_v1.health_metrics import build_health_snapshot  # noqa: E402


@pytest.fixture(autouse=True)
async def _pool():
    await init_pool()
    yield
    await close_pool()


class _FakeIngester:
    def __init__(self, *, layer="planes", health="ok",
                 cycles_failed=0, db_write_failures=0):
        self.layer = layer
        self._health = health
        self._cf = cycles_failed
        self._dwf = db_write_failures

    def status(self):
        return {
            "layer":               self.layer,
            "source":              "fake",
            "health":              self._health,
            "last_fetch_ts":       "2026-05-09T17:00:00+00:00",
            "last_fetch_count":    100,
            "last_emit_ts":        "2026-05-09T17:00:00+00:00",
            "last_cycle_ms":       250,
            "tracked_entities":    9999,
            "cycles_run":          12,
            "cycles_failed":       self._cf,
            "last_error":          None if self._health == "ok" else "boom",
            "db_write_enabled":    True,
            "last_db_write_count": 99,
            "db_write_failures":   self._dwf,
            "last_db_error":       None,
        }


# ─── Pool stats helper ───────────────────────────────────────────────────


def test_pool_stats_returns_live_snapshot():
    s = pool_stats()
    assert s["initialized"] is True
    for k in ("size", "free", "in_use", "min_size", "max_size"):
        assert k in s
    assert s["size"] >= s["min_size"]
    assert s["free"] >= 0
    assert s["in_use"] == s["size"] - s["free"]


# ─── build_health_snapshot ───────────────────────────────────────────────


async def test_snapshot_top_level_shape():
    snap = await build_health_snapshot([_FakeIngester()], None)
    for k in ("status", "ts", "db", "pool", "ingesters", "algorithms", "findings"):
        assert k in snap, f"missing top-level key {k!r}; got {list(snap.keys())}"
    assert snap["status"] in ("ok", "degraded", "down")
    assert snap["db"]["ok"] is True
    assert snap["db"]["latency_ms"] >= 0


async def test_snapshot_status_ok_when_all_ingesters_ok():
    snap = await build_health_snapshot([
        _FakeIngester(layer="planes", health="ok"),
        _FakeIngester(layer="ships",  health="ok"),
    ], None)
    assert snap["status"] == "ok"
    assert snap["ingesters"]["ok"] == 2
    assert snap["ingesters"]["degraded"] == 0
    assert snap["ingesters"]["down"] == 0


async def test_snapshot_status_degraded_when_one_fails():
    snap = await build_health_snapshot([
        _FakeIngester(layer="planes", health="ok"),
        _FakeIngester(layer="ships",  health="degraded", cycles_failed=2),
    ], None)
    assert snap["status"] == "degraded"
    assert snap["ingesters"]["degraded"] == 1


async def test_snapshot_status_down_when_all_ingesters_down():
    snap = await build_health_snapshot([
        _FakeIngester(layer="planes", health="down"),
        _FakeIngester(layer="ships",  health="down"),
    ], None)
    assert snap["status"] == "down"
    assert snap["ingesters"]["down"] == 2
    assert snap["ingesters"]["ok"] == 0


async def test_snapshot_handles_zero_ingesters_as_ok():
    snap = await build_health_snapshot([], None)
    assert snap["status"] == "ok"
    assert snap["ingesters"]["total"] == 0
    assert snap["ingesters"]["items"] == []


async def test_snapshot_findings_counts_are_integers():
    snap = await build_health_snapshot([_FakeIngester()], None)
    f = snap["findings"]
    if f.get("err"):
        pytest.skip("DB query failed; counts not asserted")
    assert isinstance(f["5m"], int)
    assert isinstance(f["60m"], int)
    assert f["60m"] >= f["5m"]   # 60-min window contains 5-min window


async def test_snapshot_pool_section_includes_in_use():
    snap = await build_health_snapshot([_FakeIngester()], None)
    p = snap["pool"]
    assert p["initialized"] is True
    assert "in_use" in p
    assert p["in_use"] >= 0


async def test_snapshot_recent_cycle_passes_through():
    fake_cycle = {"completed_at": "2026-05-09T17:00:00+00:00",
                  "duration_ms": 1234, "totals": {"x": 5}}
    snap = await build_health_snapshot([], fake_cycle)
    assert snap["algorithms"]["recent_cycle"] == fake_cycle


async def test_snapshot_splink_run_passes_through():
    """Optional `last_splink_run` arg is preserved under
    algorithms.last_splink_run for monitors to inspect."""
    fake_run = {
        "started_at":   "2026-05-09T17:00:00+00:00",
        "completed_at": "2026-05-09T17:00:01+00:00",
        "duration_ms":  1024,
        "predicted":    192,
        "persisted":    7,
        "error":        None,
    }
    snap = await build_health_snapshot([], None, last_splink_run=fake_run)
    assert snap["algorithms"]["last_splink_run"] == fake_run


async def test_snapshot_splink_run_defaults_to_none():
    snap = await build_health_snapshot([], None)
    assert snap["algorithms"]["last_splink_run"] is None


async def test_snapshot_ingester_with_status_exception_marked_down():
    """An ingester whose status() raises should be marked down with the
    error captured — not crash the whole snapshot."""
    class _Broken:
        layer = "broken"
        def status(self):
            raise RuntimeError("status() boom")
    snap = await build_health_snapshot([_Broken()], None)
    assert snap["ingesters"]["down"] == 1
    item = snap["ingesters"]["items"][0]
    assert item["health"] == "down"
    assert "boom" in (item.get("last_error") or "")


# ─── Phase 6 SLA monitor ─────────────────────────────────────────────────


from datetime import datetime, timezone, timedelta  # noqa: E402


class _IngesterWithFetch:
    """A fake ingester that reports a configurable last_fetch_ts +
    poll_interval_sec. Used to drive the SLA grading branch."""
    def __init__(self, *, layer, last_fetch_ts, poll_interval_sec, health="ok"):
        self.layer = layer
        self._last = last_fetch_ts
        self._poll = poll_interval_sec
        self._health = health

    def status(self):
        return {
            "layer":              self.layer,
            "source":             "fake",
            "health":             self._health,
            "poll_interval_sec":  self._poll,
            "last_fetch_ts":      self._last,
            "last_fetch_count":   1,
            "tracked_entities":   100,
            "cycles_run":         5,
            "cycles_failed":      0,
            "last_error":         None,
        }


def _iso(dt):
    return dt.isoformat()


async def test_sla_breach_marks_stale_ingester():
    """An ingester whose last_fetch_ts is older than 3*poll_interval_sec
    should be flagged sla_breach=True and promoted from ok->degraded."""
    now = datetime.now(timezone.utc)
    stale_when = now - timedelta(seconds=300)   # 5 min ago
    poll = 30                                   # ingester expects to poll every 30s
    # 5 min stale vs 30s poll * 3 = 90s threshold → breach
    snap = await build_health_snapshot([
        _IngesterWithFetch(layer="stale_one",
                            last_fetch_ts=_iso(stale_when),
                            poll_interval_sec=poll),
    ], None)
    assert snap["ingesters"]["sla_breach"] == 1
    assert snap["ingesters"]["degraded"] == 1
    assert snap["ingesters"]["ok"] == 0
    item = snap["ingesters"]["items"][0]
    assert item["sla_breach"] is True
    assert item["health"] == "degraded"
    assert item["secs_since_last_fetch"] >= 290  # ~300s ago


async def test_sla_breach_does_not_fire_within_threshold():
    now = datetime.now(timezone.utc)
    fresh = now - timedelta(seconds=10)
    snap = await build_health_snapshot([
        _IngesterWithFetch(layer="fresh_one",
                            last_fetch_ts=_iso(fresh),
                            poll_interval_sec=60),
    ], None)
    assert snap["ingesters"]["sla_breach"] == 0
    assert snap["ingesters"]["ok"] == 1
    item = snap["ingesters"]["items"][0]
    assert item["sla_breach"] is False


async def test_sla_breach_aggregates_status_to_degraded():
    """Even one SLA breach should pull aggregate status from 'ok' to
    'degraded'."""
    now = datetime.now(timezone.utc)
    stale = now - timedelta(seconds=600)
    snap = await build_health_snapshot([
        _IngesterWithFetch(layer="fresh", last_fetch_ts=_iso(now), poll_interval_sec=60),
        _IngesterWithFetch(layer="stale", last_fetch_ts=_iso(stale), poll_interval_sec=60),
    ], None)
    assert snap["status"] == "degraded"
    assert snap["ingesters"]["sla_breach"] == 1


async def test_sla_breach_minimum_60s_floor():
    """For very-fast ingesters (poll_interval_sec=1), the threshold
    floor is 60s so a 30s gap doesn't trigger breach."""
    now = datetime.now(timezone.utc)
    short_gap = now - timedelta(seconds=30)
    snap = await build_health_snapshot([
        _IngesterWithFetch(layer="fast", last_fetch_ts=_iso(short_gap),
                            poll_interval_sec=1),
    ], None)
    # 1s * 3 = 3s threshold but min 60s → 30s gap doesn't breach
    assert snap["ingesters"]["sla_breach"] == 0


# ─── First-cycle grace period for stream ingesters ──────────────────────


class _IngesterFreshlyBorn:
    """A stream-style ingester that's brand-new (no fetch completed yet)
    + has a sla_breach_threshold_sec override (the streaming-vs-polling
    signal). Created `created_secs_ago` seconds ago."""
    def __init__(self, *, layer, created_secs_ago, sla_override,
                 poll_interval_sec=30, health="ok"):
        self.layer = layer
        self._created_at = (
            datetime.now(timezone.utc) - timedelta(seconds=created_secs_ago)
        ).isoformat()
        self._sla_override = sla_override
        self._poll = poll_interval_sec
        self._health = health

    def status(self):
        return {
            "layer":              self.layer,
            "source":             "fake-stream",
            "health":             self._health,
            "poll_interval_sec":  self._poll,
            "sla_breach_threshold_sec": self._sla_override,
            "created_at":         self._created_at,
            "last_fetch_ts":      None,  # critical: no fetch yet
            "last_fetch_count":   0,
            "tracked_entities":   0,
            "cycles_run":         1,
            "cycles_failed":      0,
            "last_error":         None,
        }


async def test_sla_grace_period_does_not_fire_within_override_window():
    """A stream ingester (sla_override set) created 30s ago with no
    fetch yet should NOT breach — the 600s override is its grace."""
    snap = await build_health_snapshot([
        _IngesterFreshlyBorn(layer="aisstream",
                             created_secs_ago=30,
                             sla_override=600),
    ], None)
    assert snap["ingesters"]["sla_breach"] == 0
    assert snap["ingesters"]["ok"] == 1
    item = snap["ingesters"]["items"][0]
    assert item["sla_breach"] is False
    assert item["health"] == "ok"


async def test_sla_grace_period_fires_after_override_elapses():
    """Same stream ingester, but created 700s ago and STILL no fetch.
    Now it's a real breach — the websocket isn't talking."""
    snap = await build_health_snapshot([
        _IngesterFreshlyBorn(layer="aisstream",
                             created_secs_ago=700,
                             sla_override=600),
    ], None)
    assert snap["ingesters"]["sla_breach"] == 1
    item = snap["ingesters"]["items"][0]
    assert item["sla_breach"] is True
    assert item["health"] == "degraded"


async def test_sla_grace_period_does_not_apply_to_poll_ingesters():
    """An ingester with NO sla_override but no fetch yet is a poll
    ingester that claims to fetch every <poll>s. No grace — it's a
    real breach (something's wrong with its first fetch)."""
    snap = await build_health_snapshot([
        _IngesterFreshlyBorn(layer="some_poll_ingester",
                             created_secs_ago=10,
                             sla_override=None),
    ], None)
    assert snap["ingesters"]["sla_breach"] == 1


async def test_sla_grace_period_handles_missing_created_at():
    """Defensive: if status() is missing created_at (older code path
    where the field hadn't been added yet), fall back to the original
    'never fetched = breach' behavior. Don't silently let an ingester
    misrepresent itself as ok forever."""

    class _StreamWithoutCreatedAt:
        layer = "ancient"
        def status(self):
            return {
                "layer":              "ancient",
                "source":             "fake",
                "health":             "ok",
                "poll_interval_sec":  30,
                "sla_breach_threshold_sec": 600,
                # NO created_at on purpose
                "last_fetch_ts":      None,
                "last_fetch_count":   0,
                "cycles_run":         0,
                "cycles_failed":      0,
            }

    snap = await build_health_snapshot([_StreamWithoutCreatedAt()], None)
    # Without created_at we can't compute the grace window — fall back
    # to flagging breach so we don't lose visibility.
    assert snap["ingesters"]["sla_breach"] == 1


async def test_sla_breach_when_never_fetched():
    """An ingester that never fetched once (last_fetch_ts=None +
    poll_interval_sec set) is in breach by default."""
    class _NoFetch:
        layer = "never_fetched"
        def status(self):
            return {"layer": "never_fetched", "health": "ok",
                    "poll_interval_sec": 60, "last_fetch_ts": None}
    snap = await build_health_snapshot([_NoFetch()], None)
    assert snap["ingesters"]["sla_breach"] == 1


async def test_sla_breach_custom_multiplier_is_more_lenient():
    """sla_multiplier=10 should let a 5-min stale fetch pass for a
    60s-poll ingester (60*10 = 600s threshold)."""
    now = datetime.now(timezone.utc)
    stale = now - timedelta(seconds=300)
    snap = await build_health_snapshot([
        _IngesterWithFetch(layer="lenient", last_fetch_ts=_iso(stale),
                            poll_interval_sec=60),
    ], None, sla_multiplier=10.0)
    assert snap["ingesters"]["sla_breach"] == 0


# ─── Per-ingester sla_breach_threshold_sec override (streaming fix) ──────


class _IngesterWithOverride:
    """Streaming-style fake ingester. Same shape as _IngesterWithFetch
    but advertises a per-instance sla_breach_threshold_sec override —
    mirrors what AISStream + Bluesky now set."""
    def __init__(self, *, layer, last_fetch_ts, poll_interval_sec,
                 sla_threshold_override, health="ok"):
        self.layer = layer
        self._last = last_fetch_ts
        self._poll = poll_interval_sec
        self._override = sla_threshold_override
        self._health = health

    def status(self):
        return {
            "layer":                     self.layer,
            "source":                    "fake_stream",
            "health":                    self._health,
            "poll_interval_sec":         self._poll,
            "last_fetch_ts":             self._last,
            "last_fetch_count":          1,
            "tracked_entities":          100,
            "cycles_run":                5,
            "cycles_failed":             0,
            "last_error":                None,
            "sla_breach_threshold_sec":  self._override,
        }


async def test_override_lets_streaming_ingester_pass_within_window():
    """5-min-old fetch on a streaming ingester (poll=30s, override=600s)
    must NOT trip — the formula's 90s floor would have flagged it."""
    now = datetime.now(timezone.utc)
    five_min_old = now - timedelta(seconds=300)
    snap = await build_health_snapshot([
        _IngesterWithOverride(layer="ais",
                              last_fetch_ts=_iso(five_min_old),
                              poll_interval_sec=30,
                              sla_threshold_override=600.0),
    ], None)
    assert snap["ingesters"]["sla_breach"] == 0
    assert snap["ingesters"]["ok"] == 1


async def test_override_still_breaches_beyond_its_own_threshold():
    """A streaming ingester whose last fetch is older than its override
    SHOULD breach — the override is a per-class budget, not a free pass."""
    now = datetime.now(timezone.utc)
    twelve_min_old = now - timedelta(seconds=720)  # > 600s override
    snap = await build_health_snapshot([
        _IngesterWithOverride(layer="ais_overdue",
                              last_fetch_ts=_iso(twelve_min_old),
                              poll_interval_sec=30,
                              sla_threshold_override=600.0),
    ], None)
    assert snap["ingesters"]["sla_breach"] == 1
    assert snap["ingesters"]["degraded"] == 1
    assert snap["ingesters"]["items"][0]["health"] == "degraded"


async def test_override_none_falls_back_to_formula():
    """An ingester that explicitly reports sla_breach_threshold_sec=None
    must use the standard formula. Same input as
    test_sla_breach_marks_stale_ingester — must produce the same breach."""
    now = datetime.now(timezone.utc)
    five_min_old = now - timedelta(seconds=300)
    snap = await build_health_snapshot([
        _IngesterWithOverride(layer="poller",
                              last_fetch_ts=_iso(five_min_old),
                              poll_interval_sec=30,
                              sla_threshold_override=None),
    ], None)
    # 30 * 3 = 90s threshold; 300s gap > 90 → breach via formula
    assert snap["ingesters"]["sla_breach"] == 1


async def test_override_zero_or_negative_falls_back_to_formula():
    """Defensive: a misconfigured override of 0 / negative shouldn't
    silently disable the SLA monitor for that ingester."""
    now = datetime.now(timezone.utc)
    five_min_old = now - timedelta(seconds=300)
    snap = await build_health_snapshot([
        _IngesterWithOverride(layer="bad_override",
                              last_fetch_ts=_iso(five_min_old),
                              poll_interval_sec=30,
                              sla_threshold_override=0.0),
    ], None)
    # Falls back to formula → 90s threshold → breach
    assert snap["ingesters"]["sla_breach"] == 1


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
