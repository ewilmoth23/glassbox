"""Public-signals taxonomy — category metadata + derived index.

Lifted from `api_v1.py` 2026-05-27 as the P3-H Phase 2 #6 prep step.
Dashboard (`/dashboard/summary`) needs these for its critical-events
roll-up; the signals cluster (`/signals/today`, `/signals/timeline`,
`/signals.json`, `/signals.rss`, `/signals/snapshot.csv`) is the
heaviest consumer. Both clusters extract into separate files; the
constants live here so neither has to take a cross-cluster module
dependency.

Adding a new category? Add a row to `SIGNALS_CATEGORY_ORDER` AND
add a fact-extractor in `_signals_facts_for()` (still inline in
`api_v1.py` until the signals extraction lands).

`_SEVERITY_RANK` is intentionally NOT lifted here — only the signals
routes consume it (RSS feed's min_severity filter); it will move with
the signals cluster when that extracts.

Public names (no underscore prefix because cross-module). `api_v1.py`
keeps underscore-prefixed re-export aliases so
`tests/test_signals_page_live_wiring.py` continues to do
`from api_v1 import _SIGNALS_CATEGORY_ORDER` unchanged.
"""

from __future__ import annotations

from typing import Any, Dict, List


# Category metadata — id, human label, icon, severity, ordered top-down for
# how it should render on the public page.
SIGNALS_CATEGORY_ORDER: List[Dict[str, Any]] = [
    {
        "id": "sanctioned_dark",
        "event_type": "sanctioned_vessel_went_dark",
        "label": "Sanctioned vessels gone dark",
        "icon": "⚫",
        "severity": "critical",
    },
    {
        "id": "sanctioned_rendezvous",
        "event_type": "sanctioned_vessel_rendezvous",
        "label": "Sanctioned-vessel rendezvous",
        "icon": "🚨",
        "severity": "critical",
    },
    {
        "id": "shadow_fleet",
        "event_type": "shadow_fleet_cluster",
        "label": "Shadow-fleet clusters",
        "icon": "🛢️",
        "severity": "critical",
    },
    {
        "id": "sanctioned_underway",
        "event_type": "sanctioned_vessel_underway",
        "label": "Sanctioned vessels broadcasting AIS",
        "icon": "📡",
        "severity": "high",
    },
    {
        "id": "sanctioned_port",
        "event_type": "sanctioned_port_arrival",
        "label": "Sanctioned-vessel port arrivals",
        "icon": "⚓",
        "severity": "high",
    },
    {
        "id": "sanctioned_airspace",
        "event_type": "aircraft_in_sanctioned_airspace",
        "label": "Aircraft in sanctioned airspace",
        "icon": "✈️",
        "severity": "high",
    },
    {
        "id": "military_air",
        "event_type": "military_aircraft_underway",
        "label": "Military aircraft on ADS-B",
        "icon": "🪖",
        "severity": "medium",
    },
    {
        "id": "dark_vessel",
        "event_type": "dark_vessel_detected",
        "label": "Vessels gone dark while underway",
        "icon": "🌑",
        "severity": "medium",
    },
    {
        "id": "loitering",
        "event_type": "loitering_detected",
        "label": "Loitering tracks (small radius / long duration)",
        "icon": "🔄",
        "severity": "low",
    },
    {
        "id": "wildfires",
        "event_type": "nasa_firms",
        "label": "Active wildfires (NASA FIRMS)",
        "icon": "🔥",
        "severity": "low",
    },
    {
        "id": "quakes",
        "event_type": "usgs_quake",
        "label": "Earthquakes (USGS, M4+)",
        "icon": "🌐",
        "severity": "low",
    },
]


# Reverse index for O(1) per-event lookup by event_type.
SIGNALS_CATEGORIES_BY_TYPE: Dict[str, Dict[str, Any]] = {
    c["event_type"]: c for c in SIGNALS_CATEGORY_ORDER
}
