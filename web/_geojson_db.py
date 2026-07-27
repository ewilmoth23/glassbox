"""
DB-first geojson helper — backs the 4 infrastructure routes that have
companion live ingesters writing to the `event` hypertable:

  /api/v1/infrastructure/cyber-kev            ← event_type='kev_disclosure'
  /api/v1/infrastructure/cyber-spamhaus-drop  ← event_type='spamhaus_block_entry'
  /api/v1/infrastructure/noaa-buoys           ← event_type='ndbc_observation'
  /api/v1/infrastructure/climate-forecast     ← event_type='climate_forecast'

Behavior:
  1. Query the event table for the matching event_type
  2. If rows present, build a FeatureCollection from the rows + the
     static seed's metadata block (preserves description, attribution,
     license text) and return as application/geo+json
  3. If rows absent (empty DB / pre-restart / DB outage), fall back
     to serving the static seed file unchanged

This lets the route ship live-data freshness once the ingesters start
running, without losing the cockpit-renders-something-on-day-one
guarantee the static seeds provide.

The DB-query path uses the read pool (10s timeout per [P1-A]). On
query failure the helper falls back to static silently rather than
500-ing — the route MUST stay reachable even when Postgres is down.

For layers with multiple events per entity (NDBC observations come in
every 10-30 min per buoy; climate_forecast updates daily per city),
pass `distinct_on_subtype=True` to get latest-per-subtype via
`SELECT DISTINCT ON (event_subtype) ... ORDER BY event_subtype, event_time DESC`.

For layers with one canonical event per entity (KEV catalog snapshot,
Spamhaus block list snapshot), pass `distinct_on_subtype=False` —
the latest N rows by event_time are exactly what you want.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi.responses import Response


_log = logging.getLogger("web._geojson_db")

# Module dir → parent.parent is `21_GLASSBOX_AI/`. Mirrors the
# _DATA_DIR computation in web/routes/infrastructure.py.
_DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def _load_seed_metadata(filename: str) -> Optional[Dict[str, Any]]:
    """Load the static seed file and return everything EXCEPT the
    features array. Returns None if the file is missing or malformed.

    The returned dict carries the route's stable shape (type, name,
    metadata { description, license, attribution, ... }) so the
    DB-derived response stays structurally consistent with the
    static-fallback response."""
    p = _DATA_DIR / filename
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        _log.warning(f"failed to load seed {filename}: {e}")
        return None
    if not isinstance(data, dict):
        return None
    return {k: v for k, v in data.items() if k != "features"}


def _serve_static_seed(filename: str) -> Response:
    """Fall-back path — serve the static seed file unchanged."""
    p = _DATA_DIR / filename
    if not p.exists():
        return Response(
            "{}",
            status_code=404,
            media_type="application/geo+json",
        )
    return Response(
        content=p.read_bytes(),
        media_type="application/geo+json",
        headers={"Cache-Control": "public, max-age=3600"},
    )


def _row_to_feature(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Convert one event-table row to a GeoJSON Feature.
    Returns None if the row is malformed (missing geom or properties).

    Property whitelisting: the row's `properties` jsonb is passed
    through as-is — the ingester already whitelisted at write time.
    A few row-level fields are injected for traceability:
      - _event_id (uuid) — the event row's id
      - _event_time (iso) — the event row's event_time
      - _event_subtype — the row's subtype, useful for ungrouping in
        the frontend if needed
    """
    lng = row.get("lng")
    lat = row.get("lat")
    if lng is None or lat is None:
        return None
    props = row.get("properties") or {}
    if isinstance(props, str):
        try:
            props = json.loads(props)
        except Exception:
            props = {}
    if not isinstance(props, dict):
        props = {}
    out = dict(props)
    out["_event_id"] = str(row.get("id"))
    if row.get("event_time") is not None:
        out["_event_time"] = row["event_time"].isoformat() if hasattr(row["event_time"], "isoformat") else str(row["event_time"])
    if row.get("event_subtype") is not None:
        out["_event_subtype"] = row["event_subtype"]
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [float(lng), float(lat)]},
        "properties": out,
    }


async def build_db_geojson_response(
    event_type: str,
    static_filename: str,
    *,
    distinct_on_subtype: bool = False,
    limit: int = 2000,
    source_note: str = "live",
) -> Response:
    """Try a DB query for the event_type; if rows present, return a
    FeatureCollection assembled from rows + the static seed's metadata
    block. If empty or query fails, fall back to the static seed.

    Args:
      event_type: the event-table value to filter on (e.g.
        'kev_disclosure').
      static_filename: the seed file in `data/` to use as
        metadata source + fallback (e.g. 'cyber_kev.geojson').
      distinct_on_subtype: when True, `SELECT DISTINCT ON (event_subtype)`
        + order-by-(subtype, event_time desc) returns the latest row
        per subtype. Use for layers where each entity emits multiple
        events over time (NDBC observations, climate forecasts).
        When False, plain `ORDER BY event_time DESC LIMIT N` returns
        the N most-recent rows. Use for snapshot-style layers (KEV
        catalog, Spamhaus block list).
      limit: max rows to return.
      source_note: short text injected into the response's metadata.source
        field so consumers can tell DB-derived from static-seed responses.
    """
    # Deferred import — keeps this module decoupled from db.py if it ever
    # gets imported in an environment without asyncpg.
    try:
        from db import fetch_read
    except Exception as e:
        _log.info(f"db unavailable for {event_type}: {e}")
        return _serve_static_seed(static_filename)

    if distinct_on_subtype:
        sql = """
            SELECT DISTINCT ON (event_subtype)
                id, event_time, event_subtype, properties,
                ST_X(geom::geometry) AS lng, ST_Y(geom::geometry) AS lat
            FROM event
            WHERE event_type = $1
            ORDER BY event_subtype, event_time DESC
            LIMIT $2
        """
    else:
        sql = """
            SELECT id, event_time, event_subtype, properties,
                ST_X(geom::geometry) AS lng, ST_Y(geom::geometry) AS lat
            FROM event
            WHERE event_type = $1
            ORDER BY event_time DESC
            LIMIT $2
        """

    try:
        rows = await fetch_read(sql, event_type, limit)
    except Exception as e:
        _log.info(f"db query failed for {event_type}: {e}; falling back to static")
        return _serve_static_seed(static_filename)

    if not rows:
        # DB has no rows for this event_type yet — fall back to static
        # seed so the cockpit still renders SOMETHING.
        return _serve_static_seed(static_filename)

    features: List[Dict[str, Any]] = []
    for row in rows:
        feat = _row_to_feature(dict(row))
        if feat is not None:
            features.append(feat)

    if not features:
        # All rows malformed somehow — fall back to static.
        return _serve_static_seed(static_filename)

    seed_meta = _load_seed_metadata(static_filename) or {
        "type": "FeatureCollection",
        "name": static_filename.replace(".geojson", ""),
    }
    if "metadata" in seed_meta and isinstance(seed_meta["metadata"], dict):
        seed_meta["metadata"] = dict(seed_meta["metadata"])
        seed_meta["metadata"]["count"] = len(features)
        seed_meta["metadata"]["source"] = source_note
    seed_meta["features"] = features

    return Response(
        content=json.dumps(seed_meta),
        media_type="application/geo+json",
        headers={"Cache-Control": "public, max-age=300"},  # 5min vs static's 1h
    )
