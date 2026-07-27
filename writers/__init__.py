"""
Persistence writers — convert canonical GlassboxEvent objects into rows in
Postgres. One writer per entity domain.

Two shapes of writer:

  ENTITY+POSITION (entity that moves over time)
    `write_aircraft_events`  — ICAO24 in entity, position_track snapshots
    Phase 2A: `write_vessel_events`  — MMSI in entity, position_track snapshots
    (also satellites in Phase 2B, same shape)

  EVENT-INTO-EVENT-TABLE (point-in-time happening)
    `write_seismic_events`   — USGS quakes → event hypertable
    Phase 2D: weather alerts, GDELT topical, ACLED — same shape

The writer's contract for ALL kinds:
  - Input: a list of GlassboxEvent objects (ALREADY post-dedup, post-classify)
  - Side effect: UPSERT/INSERT into the appropriate table(s) per shape
  - Output: integer count of NEW rows persisted (re-runs of the same input
    return 0 if dedup catches them — useful for "did anything change" semantics)

Errors are logged but do not raise — DB downtime must NEVER break the SSE
broadcast pipeline. The cycle's broadcast already happened before this is
called; if Postgres is down, we drop the durable archive for that cycle but
keep serving live data. (Recovery from gaps is a Phase 2.5/Phase 6 concern.)

Why type-specific writers (not one generic):
  - Mapping ingester payload fields → properties is type-specific. Aircraft
    cares about icao24/callsign/squawk/military. Vessels care about
    MMSI/IMO/flag_state/nav_status. Quakes care about magnitude/depth/tsunami.
    A generic writer would bury the type contract under conditionals.
  - Each writer's surface area is small (~50 lines). Easier to read, test,
    audit for legal/license posture, and keep up to date with schema changes.
"""

from __future__ import annotations

# Cross-cutting helpers (P3-H Phase 3, 2026-05-27): lifted to
# writers/_shared.py so per-cluster modules can import them directly.
# Re-exported here for test_writers_confidence.py which does
# `from writers import _with_confidence, _LAYER_TO_PLATFORM`.
from writers._shared import (  # noqa: F401
    _EVENT_UUID_NAMESPACE,
    _LAYER_TO_PLATFORM,
    _maybe_embed,
    _parse_ts,
    _sort_batch_for_upsert,
    _with_confidence,
)


# Per-cluster extractions (P3-H Phase 3 — each cluster lives in writers/<name>.py)
from writers.aqi import write_aqi_events    # noqa: F401, E402  (#1)
from writers.metar import write_metar_events  # noqa: F401, E402  (#2)
from writers.neo import write_neo_events    # noqa: F401, E402  (#3)
from writers.donki import write_donki_events  # noqa: F401, E402  (#4)
from writers.sec import write_sec_filing_events  # noqa: F401, E402  (#5)
from writers.gdacs import write_gdacs_events  # noqa: F401, E402  (#6)
from writers.volcanic import write_volcanic_events  # noqa: F401, E402  (#7)
from writers.fema import write_fema_events  # noqa: F401, E402  (#8)
from writers.wildfire import write_wildfire_events  # noqa: F401, E402  (#9)
from writers.eonet import write_natural_event_events  # noqa: F401, E402  (#10)
from writers.seismic import write_seismic_events  # noqa: F401, E402  (#11)
from writers.emsc import write_emsc_quake_events  # noqa: F401, E402  (#12)
from writers.weather_alert import write_weather_alert_events  # noqa: F401, E402  (#13)
from writers.space_weather import write_space_weather_events  # noqa: F401, E402  (#14)
from writers.tropical_storm import write_tropical_storm_events  # noqa: F401, E402  (#15)
from writers.hn import write_hn_events  # noqa: F401, E402  (#16)
from writers.newsdata import write_newsdata_events  # noqa: F401, E402  (#17)
from writers.social import write_social_events  # noqa: F401, E402  (#18)
from writers.news import write_news_events  # noqa: F401, E402  (#19)
from writers.gdelt_bulk import write_gdelt_bulk_events  # noqa: F401, E402  (#20)
from writers.aircraft import write_aircraft_events  # noqa: F401, E402  (#21)
from writers.vessel import write_vessel_events  # noqa: F401, E402  (#22)
from writers.satellite import write_satellite_events  # noqa: F401, E402  (#23)
from writers.sanctions import write_sanction_entities  # noqa: F401, E402  (#24)

# P2-A Phase 1 MVP (2026-05-27) — cyber-attack data layers.
# These add net-new public writers post-trilogy; they are NOT extractions
# from the original writers.py.
from writers.cisa_kev import write_cisa_kev_events  # noqa: F401, E402  (P2-A #1)
from writers.spamhaus_drop import write_spamhaus_drop_events  # noqa: F401, E402  (P2-A #2)
from writers.open_meteo_forecast import write_open_meteo_forecast_events  # noqa: F401, E402  (P2-B Phase 1.5 — live ingester upgrade for climate_forecast layer)
from writers.noaa_ndbc import write_noaa_ndbc_events  # noqa: F401, E402  (P2-B Phase 1.5 — live ingester upgrade for noaa_buoys layer)


