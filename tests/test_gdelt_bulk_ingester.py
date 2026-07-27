# Copyright (c) 2026 Glassbox contributors
# SPDX-License-Identifier: MIT
"""
GdeltBulkIngester end-to-end test, with the network mocked out via
monkeypatch on the downloader. Exercises the full daemon-cycle path:
fetch → CAMEO + prefilter → normalize → GlassboxEvent.

State persistence (last_processed_url) is covered too — a second cycle
with the same URL must short-circuit.
"""

from __future__ import annotations

import asyncio
import io
import sys
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


# Use a current UTC timestamp (default) so the prefilter's recency rule
# (max_age_hours=6) sees the mock events as fresh. Tests historically
# used a hardcoded "20260510120000" string, which started failing on
# 2026-05-19 when the date aged past the 6-hour window.
def _now_dateadded() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")


def _now_sqldate() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d")


def _gdelt_url_for_now(quarter: int = 0) -> str:
    # Floor to the nearest past 15-min boundary; `quarter` lets a test
    # construct a "newer" URL than the previous one (used in the
    # already-processed / new-url tests).
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    floor = now.replace(minute=(now.minute // 15) * 15) + timedelta(minutes=15 * quarter)
    return f"http://data.gdeltproject.org/gdeltv2/{floor.strftime('%Y%m%d%H%M%S')}.export.CSV.zip"

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ingesters.base import GlassboxEvent  # noqa: E402
from ingesters.gdelt_bulk import downloader as downloader_mod  # noqa: E402
from ingesters.gdelt_bulk import ingester as ingester_mod  # noqa: E402
from ingesters.gdelt_bulk.downloader import LastUpdate, LastUpdateEntry  # noqa: E402
from ingesters.gdelt_bulk.ingester import GdeltBulkIngester  # noqa: E402


def _row(**overrides) -> str:
    """Mirror of the helper in test_gdelt_bulk_parser — same shape, kept
    local so the ingester test stays self-contained."""
    cols = [""] * 61
    cols[0]  = overrides.get("event_id", "1000000001")
    cols[1]  = _now_sqldate()
    cols[6]  = overrides.get("actor1_name", "MILITARY")
    cols[7]  = "USA"
    cols[16] = overrides.get("actor2_name", "CIVILIAN")
    cols[17] = "UKR"
    cols[26] = overrides.get("event_code", "195")
    cols[28] = "19"
    cols[30] = "-10.0"
    cols[34] = "-7.5"
    cols[51] = overrides.get("actiongeo_type", "4")
    cols[52] = overrides.get("actiongeo_name", "Mariupol, Donetska, Ukraine")
    cols[53] = "UP"
    cols[56] = overrides.get("lat", "47.0971")
    cols[57] = overrides.get("lng", "37.5434")
    cols[59] = overrides.get("dateadded", _now_dateadded())
    cols[60] = overrides.get("source_url",
                             "https://www.reuters.com/world/2026-05-10-mariupol")
    return "\t".join(cols)


def _csv_payload() -> str:
    return "\n".join([
        _row(event_id="EV-PASS-1"),
        # Drops by category (diplomatic.statement not in default allowlist):
        _row(event_id="EV-DROP-1", event_code="010", event_root="01",
             goldstein="0.0",
             source_url="https://infowars.com/x"),
        _row(event_id="EV-PASS-2", event_code="163", event_root="16",
             goldstein="-5.6",
             source_url="https://www.bloomberg.com/x",
             actor1_name="EU", actor2_name="RUSSIA"),
    ])


@pytest.fixture
def patched_downloader(monkeypatch, tmp_path):
    """Replace the network entry points so the ingester runs entirely
    in-process. Caller controls the URL returned via ``set_url``."""

    state = {
        "url": _gdelt_url_for_now(),
        "csv": _csv_payload(),
        "fetch_calls": 0,
        "download_calls": 0,
    }

    async def _fake_fetch_lastupdate(session):
        state["fetch_calls"] += 1
        return LastUpdate(
            export=LastUpdateEntry(
                size_bytes=999, md5="aa", url=state["url"]
            ),
            mentions=None,
            gkg=None,
        )

    async def _fake_download_export_csv(session, entry):
        state["download_calls"] += 1
        return state["csv"]

    monkeypatch.setattr(ingester_mod, "fetch_lastupdate", _fake_fetch_lastupdate)
    monkeypatch.setattr(ingester_mod, "download_export_csv", _fake_download_export_csv)
    return state


def _make_ingester(tmp_path) -> GdeltBulkIngester:
    return GdeltBulkIngester(
        broadcaster=None,
        classifier=None,
        db_writer=None,
        cache_dir=tmp_path,
    )


def test_ingester_fetch_normalize_round_trip(patched_downloader, tmp_path):
    ing = _make_ingester(tmp_path)
    raw = asyncio.run(ing.fetch())
    # Two events should pass the default prefilter chain (the
    # diplomatic.statement on infowars is dropped at category)
    assert len(raw) == 2

    events = ing.normalize(raw)
    assert len(events) == 2
    assert all(isinstance(ev, GlassboxEvent) for ev in events)
    assert {ev.external_id for ev in events} == {"EV-PASS-1", "EV-PASS-2"}

    # Per-event payload sanity
    pass_one = next(ev for ev in events if ev.external_id == "EV-PASS-1")
    assert pass_one.layer == "news"
    assert pass_one.kind == "news"
    assert pass_one.lat == 47.0971
    assert pass_one.lng == 37.5434
    assert pass_one.payload["cameo_subcategory"] == "armed_conflict.airstrike"
    assert pass_one.payload["url"].startswith("https://www.reuters.com/")
    assert 0.0 <= pass_one.payload["prefilter_priority"] <= 1.0
    # Severity is encoded as int 0..10 on GlassboxEvent
    assert 0 <= pass_one.severity <= 10

    # Per-cycle counters surface via status()
    s = ing.status()
    assert s["last_parsed_count"] == 3
    assert s["last_filtered_count"] == 2
    assert s["last_processed_url"] == patched_downloader["url"]


def test_ingester_skips_when_url_already_processed(patched_downloader, tmp_path):
    ing = _make_ingester(tmp_path)
    first = asyncio.run(ing.fetch())
    assert len(first) == 2
    assert patched_downloader["download_calls"] == 1

    second = asyncio.run(ing.fetch())
    # Same URL -> no new snapshot -> empty list, no second download
    assert second == []
    assert patched_downloader["download_calls"] == 1
    assert patched_downloader["fetch_calls"] == 2  # lastupdate still polled


def test_ingester_processes_new_url_after_first(patched_downloader, tmp_path):
    ing = _make_ingester(tmp_path)
    asyncio.run(ing.fetch())          # cycle 1 — original URL
    patched_downloader["url"] = _gdelt_url_for_now(quarter=1)  # 15 min later
    # Cycle 2 must have distinct event IDs + headlines — otherwise the
    # prefilter's DedupRule (correctly) drops them as duplicates of
    # cycle 1's entries, and we'd be measuring dedup behavior rather
    # than URL-state behavior.
    patched_downloader["csv"] = "\n".join([
        _row(event_id="EV-PASS-3",
             actiongeo_name="Aleppo, Syria"),
        _row(event_id="EV-PASS-4", event_code="163", event_root="16",
             goldstein="-5.6",
             actiongeo_name="Brussels, Belgium",
             source_url="https://www.bloomberg.com/y",
             actor1_name="EU", actor2_name="BELARUS",
             lat="50.85", lng="4.35"),
    ])
    raw = asyncio.run(ing.fetch())     # cycle 2 — new URL + distinct events
    assert len(raw) == 2
    assert patched_downloader["download_calls"] == 2
    assert {fe.event.event_id for fe in raw} == {"EV-PASS-3", "EV-PASS-4"}


def test_ingester_state_persists_across_instances(patched_downloader, tmp_path):
    """A new ingester instance pointed at the same cache dir must NOT
    re-process the URL the prior instance committed."""
    ing1 = _make_ingester(tmp_path)
    asyncio.run(ing1.fetch())

    ing2 = _make_ingester(tmp_path)
    raw = asyncio.run(ing2.fetch())
    assert raw == []   # new instance reads the persisted last_processed_url
    # The new instance shares state via the file system.
    assert ing2.last_processed_url == patched_downloader["url"]


def test_ingester_handles_empty_lastupdate(monkeypatch, tmp_path):
    """A lastupdate.txt with no .export entry must not crash; cycle
    returns 0 events."""
    async def _empty_lastupdate(session):
        return LastUpdate(export=None, mentions=None, gkg=None)
    monkeypatch.setattr(ingester_mod, "fetch_lastupdate", _empty_lastupdate)

    ing = _make_ingester(tmp_path)
    raw = asyncio.run(ing.fetch())
    assert raw == []


def test_ingester_status_includes_prefilter_health(patched_downloader, tmp_path):
    ing = _make_ingester(tmp_path)
    asyncio.run(ing.fetch())
    s = ing.status()
    assert "prefilter_health" in s
    pf = s["prefilter_health"]
    assert pf["pass_count"] == 2
    assert pf["drop_count"] >= 1
    assert "rules_in_chain" in pf


# ─── A/B shadow wiring ─────────────────────────────────────────────────


def test_ingester_no_shadow_by_default(tmp_path):
    """Bare construction (no env var, no kwarg) must not wire a shadow.
    Production default — zero overhead."""
    ing = _make_ingester(tmp_path)
    assert ing.engine.shadow_engine is None
    h = ing.engine.health()
    assert "shadow" not in h


def test_ingester_wires_shadow_from_kwarg(tmp_path):
    """Explicit ``shadow_config_path`` kwarg constructs a shadow engine
    against the given config file."""
    shadow_yaml = (Path(__file__).resolve().parent.parent
                   / "ingesters" / "gdelt_bulk" / "prefilter" / "config"
                   / "prefilter_shadow_example.yaml")
    assert shadow_yaml.exists(), (
        "the shipped example shadow config moved — fix this test")
    ing = GdeltBulkIngester(
        broadcaster=None, classifier=None, db_writer=None,
        cache_dir=tmp_path,
        shadow_config_path=shadow_yaml,
    )
    assert ing.engine.shadow_engine is not None
    # The shadow's config has a distinct version stamp so we confirm
    # the right file was loaded.
    assert ing.engine.shadow_engine._cfg.version == "1.0-shadow-example"


def test_ingester_wires_shadow_from_env_var(tmp_path, monkeypatch):
    """``GLASSBOX_PREFILTER_SHADOW_CONFIG`` env var path is the
    operator-facing knob. Test that path-from-env reaches the engine."""
    shadow_yaml = (Path(__file__).resolve().parent.parent
                   / "ingesters" / "gdelt_bulk" / "prefilter" / "config"
                   / "prefilter_shadow_example.yaml")
    monkeypatch.setenv("GLASSBOX_PREFILTER_SHADOW_CONFIG", str(shadow_yaml))
    ing = _make_ingester(tmp_path)
    assert ing.engine.shadow_engine is not None
    assert ing.engine.shadow_engine._cfg.version == "1.0-shadow-example"


def test_ingester_shadow_load_failure_is_non_fatal(tmp_path, monkeypatch):
    """A typo / missing shadow config must NOT keep the ingester from
    starting. Log + continue without shadow is the right failure
    mode for an experimental knob."""
    monkeypatch.setenv("GLASSBOX_PREFILTER_SHADOW_CONFIG",
                       str(tmp_path / "does-not-exist.yaml"))
    ing = _make_ingester(tmp_path)
    # Ingester constructed cleanly; shadow stays None.
    assert ing.engine.shadow_engine is None


def test_ingester_kwarg_takes_precedence_over_env_var(tmp_path, monkeypatch):
    """Test path: explicit kwarg beats env var. Lets a test target a
    specific shadow config without leaking the env-var value into other
    tests in the same session."""
    real_yaml = (Path(__file__).resolve().parent.parent
                 / "ingesters" / "gdelt_bulk" / "prefilter" / "config"
                 / "prefilter_shadow_example.yaml")
    monkeypatch.setenv("GLASSBOX_PREFILTER_SHADOW_CONFIG",
                       str(tmp_path / "ignored.yaml"))
    ing = GdeltBulkIngester(
        broadcaster=None, classifier=None, db_writer=None,
        cache_dir=tmp_path,
        shadow_config_path=real_yaml,
    )
    # Kwarg won → shadow loaded successfully despite bad env-var path.
    assert ing.engine.shadow_engine is not None
    assert ing.engine.shadow_engine._cfg.version == "1.0-shadow-example"


def test_ingester_shadow_records_outcomes_after_fetch(
    patched_downloader, tmp_path,
):
    """End-to-end: fetch a cycle with a shadow wired, then confirm
    the confusion-matrix counters on engine.health()['shadow'] add up
    to the total events seen by the primary engine."""
    shadow_yaml = (Path(__file__).resolve().parent.parent
                   / "ingesters" / "gdelt_bulk" / "prefilter" / "config"
                   / "prefilter_shadow_example.yaml")
    ing = GdeltBulkIngester(
        broadcaster=None, classifier=None, db_writer=None,
        cache_dir=tmp_path,
        shadow_config_path=shadow_yaml,
    )
    asyncio.run(ing.fetch())
    h = ing.engine.health()
    assert "shadow" in h
    sb = h["shadow"]
    seen_by_shadow = (sb["agree_pass"] + sb["agree_drop"]
                      + sb["primary_pass_only"] + sb["primary_drop_only"])
    seen_by_primary = h["pass_count"] + h["drop_count"]
    assert seen_by_shadow == seen_by_primary, (
        f"shadow saw {seen_by_shadow} events but primary saw "
        f"{seen_by_primary} — shadow path missed some events"
    )
