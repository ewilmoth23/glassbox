# Copyright (c) 2026 Glassbox contributors
# SPDX-License-Identifier: MIT
"""
GDELT Events V2 CSV parser tests + end-to-end smoke against the
HANDOFF_02 CAMEO lookup and HANDOFF_03 prefilter chain.

The synthetic fixture below mirrors the real GDELT V2 schema (61
tab-separated columns, no header row); column indices match the codebook
at http://data.gdeltproject.org/documentation/GDELT-Event_Codebook-V2.0.pdf.
Every test asserts on visible behavior, not internal counters, so the
parser is free to evolve as long as its contract holds.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from glassbox_taxonomy import CAMEOLookup  # noqa: E402
from ingesters.gdelt_bulk.parser import (  # noqa: E402
    actiongeo_type_to_quality,
    parse_events_csv,
)
from ingesters.gdelt_bulk.prefilter import (  # noqa: E402
    PreFilterConfig,
    PreFilterEngine,
)
from ingesters.gdelt_bulk.prefilter.config import GDELTEventForPrefilter  # noqa: E402


_PREFILTER_PKG = ROOT / "ingesters" / "gdelt_bulk" / "prefilter"
_CONFIG_PATH = _PREFILTER_PKG / "config" / "prefilter.yaml"


def _now_dateadded() -> str:
    # Use current UTC so the prefilter's recency rule (max_age_hours=6)
    # sees the synthetic row as fresh. Previously the default was a
    # hardcoded "20260510120000" which aged past the window on 2026-05-19.
    return datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")


def _now_sqldate() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d")


def _row(**overrides) -> str:
    """Build a single 61-column tab-separated GDELT V2 row.

    Defaults represent a high-signal airstrike-on-Mariupol-style event
    sourced from Reuters. Overrides target specific columns by name to
    exercise edge cases without each test repeating the whole row.
    """
    cols = [""] * 61
    cols[0]  = overrides.get("event_id", "1000000001")
    cols[1]  = overrides.get("sqldate", _now_sqldate())
    cols[6]  = overrides.get("actor1_name", "MILITARY")
    cols[7]  = overrides.get("actor1_country", "USA")
    cols[16] = overrides.get("actor2_name", "CIVILIAN")
    cols[17] = overrides.get("actor2_country", "UKR")
    cols[26] = overrides.get("event_code", "195")    # Employ aerial weapons
    cols[28] = overrides.get("event_root", "19")
    cols[30] = overrides.get("goldstein", "-10.0")
    cols[34] = overrides.get("avg_tone", "-7.5")
    cols[51] = overrides.get("actiongeo_type", "4")  # WORLDCITY
    cols[52] = overrides.get("actiongeo_name", "Mariupol, Donetska, Ukraine")
    cols[53] = overrides.get("actiongeo_country", "UP")
    cols[54] = overrides.get("actiongeo_adm1", "UP14")
    cols[55] = overrides.get("actiongeo_adm2", "")
    cols[56] = overrides.get("lat", "47.0971")
    cols[57] = overrides.get("lng", "37.5434")
    cols[59] = overrides.get("dateadded", _now_dateadded())
    cols[60] = overrides.get("source_url", "https://www.reuters.com/world/2026-05-10-mariupol")
    return "\t".join(cols)


# ─── ActionGeo type mapping ──────────────────────────────────────────────


def test_actiongeo_type_mapping():
    assert actiongeo_type_to_quality("1") == "country"
    assert actiongeo_type_to_quality("2") == "region"
    assert actiongeo_type_to_quality("3") == "city"
    assert actiongeo_type_to_quality("4") == "city"
    assert actiongeo_type_to_quality("5") == "region"
    assert actiongeo_type_to_quality("") == "unknown"
    assert actiongeo_type_to_quality("99") == "unknown"


# ─── Single-row parse ────────────────────────────────────────────────────


def test_parser_single_row_round_trip():
    """The default high-signal row should parse cleanly with all the
    CAMEO lookup fields populated correctly."""
    cameo = CAMEOLookup()
    # Pin dateadded to a known value so the timestamp assertion below is
    # deterministic — _row()'s default uses NOW (for the recency filter)
    # which would defeat this round-trip check.
    csv = _row(sqldate="20260510", dateadded="20260510120000")
    events = list(parse_events_csv(csv, cameo=cameo))
    assert len(events) == 1
    ev = events[0]
    assert isinstance(ev, GDELTEventForPrefilter)
    assert ev.event_id == "1000000001"
    assert ev.code == "195"
    assert ev.subcategory == "armed_conflict.airstrike"
    assert ev.category == "armed_conflict"
    assert ev.severity > 0.8
    assert ev.lat == 47.0971
    assert ev.lng == 37.5434
    assert ev.geocode_quality == "city"
    assert ev.iso_country == "UP"
    assert ev.source_url.startswith("https://www.reuters.com/")
    assert ev.timestamp == datetime(2026, 5, 10, 12, 0, 0, tzinfo=timezone.utc)
    assert "military" in ev.flags


def test_parser_skips_row_without_lat_or_lng():
    cameo = CAMEOLookup()
    csv = "\n".join([
        _row(lat="", lng="37.5434"),       # missing lat
        _row(lat="47.0971", lng=""),       # missing lng
        _row(event_id="GOOD"),             # valid
    ])
    events = list(parse_events_csv(csv, cameo=cameo))
    assert len(events) == 1
    assert events[0].event_id == "GOOD"


def test_parser_skips_row_without_source_url():
    cameo = CAMEOLookup()
    csv = "\n".join([
        _row(source_url=""),
        _row(event_id="GOOD"),
    ])
    events = list(parse_events_csv(csv, cameo=cameo))
    assert len(events) == 1
    assert events[0].event_id == "GOOD"


def test_parser_skips_row_with_unparseable_dateadded():
    cameo = CAMEOLookup()
    csv = "\n".join([
        _row(dateadded="abcdefghij1234"),  # not digits
        _row(dateadded="2026"),            # too short
        _row(event_id="GOOD"),
    ])
    events = list(parse_events_csv(csv, cameo=cameo))
    assert len(events) == 1
    assert events[0].event_id == "GOOD"


def test_parser_skips_short_rows():
    """A row with fewer than the expected column count is silently
    skipped — production CSV occasionally has truncated lines."""
    cameo = CAMEOLookup()
    short = "\t".join([""] * 30)         # half-length junk row
    csv = "\n".join([short, _row(event_id="GOOD")])
    events = list(parse_events_csv(csv, cameo=cameo))
    assert len(events) == 1
    assert events[0].event_id == "GOOD"


def test_parser_unknown_cameo_code_falls_back_to_999():
    """A code GDELT might emit but the CAMEO lookup doesn't know
    must fall through parent-fallback to 999 (unknown), not crash."""
    cameo = CAMEOLookup()
    csv = _row(event_code="9876")        # entirely outside CAMEO space
    events = list(parse_events_csv(csv, cameo=cameo))
    assert len(events) == 1
    assert events[0].subcategory == "unknown.unknown"


def test_parser_max_rows_caps_output():
    cameo = CAMEOLookup()
    csv = "\n".join(_row(event_id=f"EV-{i}") for i in range(10))
    events = list(parse_events_csv(csv, cameo=cameo, max_rows=4))
    assert len(events) == 4
    assert [e.event_id for e in events] == [f"EV-{i}" for i in range(4)]


# ─── End-to-end with the prefilter ───────────────────────────────────────


def test_end_to_end_default_config_passes_high_signal_drops_noise():
    """Synthetic mini-batch: an airstrike on Reuters (should pass the
    default chain) interleaved with a low-grade diplomatic statement
    on infowars (should be dropped — at least by category, possibly
    earlier). Verifies the parser → CAMEO → prefilter wiring all the
    way down to the queue."""
    cameo = CAMEOLookup()
    cfg = PreFilterConfig.load_yaml(_CONFIG_PATH)
    eng = PreFilterEngine(cfg, _PREFILTER_PKG)

    csv_lines = [
        # PASSES: airstrike on Mariupol, Reuters
        _row(event_id="EV-PASS-1"),
        # DROPS: diplomatic statement on infowars (category not allowed
        # AND source quality below floor — category fires first)
        _row(
            event_id="EV-DROP-1",
            event_code="010",
            event_root="01",
            goldstein="0.0",
            actiongeo_type="1",
            source_url="https://infowars.com/post/123",
        ),
        # PASSES: economic sanctions on Bloomberg
        _row(
            event_id="EV-PASS-2",
            event_code="163",
            event_root="16",
            goldstein="-5.6",
            source_url="https://www.bloomberg.com/x",
            actor1_name="EU",
            actor2_name="RUSSIA",
            lat="50.45",
            lng="30.52",
            dateadded=_now_dateadded(),
            actiongeo_name="EU statement on Russia",
        ),
        # DROPS: high-quality source but unmapped event code falls into
        # unknown.unknown which is not in the default category allowlist
        _row(
            event_id="EV-DROP-2",
            event_code="9999",
            event_root="99",
            goldstein="0.0",
        ),
    ]
    csv = "\n".join(csv_lines)
    events = list(parse_events_csv(csv, cameo=cameo))
    assert len(events) == 4

    passes = []
    drops = []
    for ev in events:
        out = eng.process(ev)
        if out is not None:
            passes.append(out)
        else:
            drops.append(ev.event_id)

    assert sorted(p.event.event_id for p in passes) == ["EV-PASS-1", "EV-PASS-2"]
    assert sorted(drops) == ["EV-DROP-1", "EV-DROP-2"]
    # Both passes should have landed in the queue
    assert eng.queue.depth() == 2
    # And the per-event-id duplicate_of pointer is None for first-occurrence events
    assert all(p.duplicate_of is None for p in passes)


def test_end_to_end_two_near_identical_events_dedup():
    """Same incident reported twice from different sources within the
    dedup window should yield one queued event + one dedup drop.

    The synthetic events have fixed timestamps (12:00 + 12:15 UTC on
    2026-05-10) — the test pins both the recency rule's clock AND the
    dedup rule's clock to ``_FIXED_NOW`` (12:30 UTC same day) so the
    test is deterministic regardless of when on the wall clock it
    runs. Without that pin, the dedup rule's default
    ``datetime.now(timezone.utc)`` would expire the first event from
    its 60-min sliding window the moment the wall clock crossed
    13:00 UTC, causing the second event to find an empty cache and
    pass through.
    """
    from ingesters.gdelt_bulk.prefilter.rules import (
        DedupRule, RecencyRule,
    )

    cameo = CAMEOLookup()
    cfg = PreFilterConfig.load_yaml(_CONFIG_PATH)
    # Loosen sim threshold for the synthetic fixture (real titles are
    # usually richer; the parser uses ActionGeo_FullName as the title and
    # those are only 3-4 tokens).
    cfg.rules.dedup_filter.similarity_threshold = 0.5
    eng = PreFilterEngine(cfg, _PREFILTER_PKG)

    # Pin both stateful rules to a deterministic clock so the test
    # passes regardless of when in the day it runs.
    fixed_now = datetime(2026, 5, 10, 12, 30, 0, tzinfo=timezone.utc)
    eng._rules = [
        RecencyRule(cfg.rules.recency_filter, now_fn=lambda: fixed_now)
            if r.name == "recency" else
        DedupRule(cfg.rules.dedup_filter, now_fn=lambda: fixed_now)
            if r.name == "dedup" else
        r
        for r in eng._rules
    ]

    csv = "\n".join([
        _row(event_id="ORIG",
             actiongeo_name="Mariupol, Donetska, Ukraine"),
        _row(event_id="DUP",
             actiongeo_name="Mariupol, Donetska, Ukraine",
             dateadded="20260510121500",
             source_url="https://www.bbc.co.uk/news/x"),
    ])
    events = list(parse_events_csv(csv, cameo=cameo))
    assert len(events) == 2

    out_first = eng.process(events[0])
    out_dup = eng.process(events[1])

    assert out_first is not None and out_first.event.event_id == "ORIG"
    assert out_dup is None
    assert eng.stats.drops_by_rule.get("dedup", 0) == 1
    assert eng.stats.last_duplicate_of == "ORIG"
    assert eng.queue.depth() == 1
