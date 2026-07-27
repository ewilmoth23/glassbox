"""Core entity + event surface — `/viewport`, `/entity/{id}`,
`/vessel/{mmsi}`, `/aircraft/{icao24}`, `/event/{id}`, `/events/similar`,
`/entities/{id}/aliases`, `/entities/{id}/cross_domain`
(extraction #7 of P3-H Phase 2 — biggest functional surface).

8 routes plus the two killer-query helpers `query_viewport` and
`query_entity_detail` that several test files reach into via api_v1.

Pure-async helpers (`query_viewport`, `query_entity_detail`) take their
deps as args so tests can drive them without HTTP plumbing. They moved
WITH this cluster because the routes are their only callers; api_v1.py
keeps a bottom-of-file re-export shim so:
    `from api_v1 import query_viewport` (test_viewport_endpoint.py)
    `from api_v1 import query_entity_detail` (test_entity_detail.py,
                                              test_brief.py)
continue to work unchanged.

`from api_v1 import _parse_bbox, _parse_iso, _parse_types` at module
top is safe under the bottom-shim pattern (api_v1's shim that loads
this module sits at the end of api_v1.py — those three helpers are
all module-level defs at api_v1 lines ~90-130, well before the shim).

`coerce_jsonb` imported directly from `web/_jsonb.py` (lifted in
commit `fa65217`) — no reason for a fresh module to inherit the
legacy underscore-prefix.

`test_cross_domain_endpoint.py:299` regex-scans for the
`entity_cross_domain` handler. After this extraction the handler lives
HERE, not in api_v1.py. The regex anchor in the test needs to be
updated alongside this commit; the test's specific assertion is that
the handler EXISTS at SOME importable location, so changing the regex
target to scan this module's source preserves the test's intent.

Mounted by `api_v1.build_router()` at BOTH `/api/v1/*` AND `/api/intel/*`.
"""

from __future__ import annotations

import json
import time as time_mod
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query

from brief import generate_brief_cached, generate_brief_llm_cached
from db import acquire_read, fetch_read, fetch_write
from web._jsonb import coerce_jsonb

# `_parse_bbox`, `_parse_iso`, `_parse_types` still live in api_v1.py
# (consumed by inline signals routes too). The bottom-of-file shim in
# api_v1.py that loads this module runs after those defs, so this
# module-top import resolves cleanly.
from api_v1 import _parse_bbox, _parse_iso, _parse_types


# ─── query_viewport — the killer query ────────────────────────────────────


async def query_viewport(
    *,
    bbox: Tuple[float, float, float, float],
    time_from: datetime,
    time_to: datetime,
    types: List[str],
    limit: int = 1000,
) -> Dict[str, Any]:
    """Return entities (with latest position in window) + events inside bbox+time.

    Bbox is (west, south, east, north). Times are tz-aware UTC.
    """
    west, south, east, north = bbox
    t0 = time_mod.time()

    # Entity-first query: filter against entity using the partial index
    # `entity_type_current_time_idx (entity_type, current_position_time DESC)`
    # and read kinematics from the denormalized current_velocity_ms /
    # current_heading_deg / current_altitude_m columns. This avoids any
    # join against the 103M-row position_track hypertable on the hot path.
    # Writers maintain these columns alongside current_geom + current_
    # position_time with the same out-of-order safety guard.
    entity_query = """
        SELECT
            e.id,
            e.entity_type,
            e.canonical_id,
            e.canonical_id_type,
            e.display_name,
            e.properties,
            e.last_seen,
            ST_Y(e.current_geom::geometry) AS lat,
            ST_X(e.current_geom::geometry) AS lng,
            e.current_altitude_m AS altitude_m,
            e.current_velocity_ms AS velocity_ms,
            e.current_heading_deg AS heading_deg,
            e.current_position_time AS position_time
        FROM entity e
        WHERE e.entity_type = ANY($7::text[])
          AND e.current_position_time IS NOT NULL
          AND e.current_position_time >= $1
          AND e.current_position_time <= $2
          AND e.current_geom IS NOT NULL
          AND ST_Intersects(
                e.current_geom::geometry,
                ST_MakeEnvelope($3, $4, $5, $6, 4326)
          )
        ORDER BY e.current_position_time DESC
        LIMIT $8
    """

    event_query = """
        SELECT
            id, event_type, event_subtype, event_time, severity, severity_for_market,
            title, description, properties, domain, decay_half_life_min, entity_id,
            ST_Y(geom::geometry) AS lat,
            ST_X(geom::geometry) AS lng
        FROM event
        WHERE event_time >= $1
          AND event_time <= $2
          AND geom IS NOT NULL
          AND (properties->>'withdrawn') IS NULL
          AND ST_Intersects(
                geom::geometry,
                ST_MakeEnvelope($3, $4, $5, $6, 4326)
          )
        ORDER BY event_time DESC
        LIMIT $7
    """

    # Tier-1 alerts get pulled in TWO separate queries so the high-volume
    # sanctioned-vessel-underway events (often hundreds at scale in busy
    # AIS regions) don't crowd out the rarer dark-vessel + SWPC events.
    # Without this split, a 500-row tier-1 cap was being saturated entirely
    # by the most-recent sanctioned events — defeating the whole point of
    # surfacing the rarer signals.
    sanctions_tier1_query = """
        SELECT
            id, event_type, event_subtype, event_time, severity, severity_for_market,
            title, description, properties, domain, decay_half_life_min, entity_id,
            ST_Y(geom::geometry) AS lat,
            ST_X(geom::geometry) AS lng
        FROM event
        WHERE event_time >= $1 AND event_time <= $2
          AND geom IS NOT NULL
          AND event_type = 'sanctioned_vessel_underway'
          AND (properties->>'withdrawn') IS NULL
          AND ST_Intersects(geom::geometry, ST_MakeEnvelope($3, $4, $5, $6, 4326))
        ORDER BY event_time DESC
        LIMIT 1000
    """
    rare_tier1_query = """
        SELECT
            id, event_type, event_subtype, event_time, severity, severity_for_market,
            title, description, properties, domain, decay_half_life_min, entity_id,
            ST_Y(geom::geometry) AS lat,
            ST_X(geom::geometry) AS lng
        FROM event
        WHERE event_time >= $1 AND event_time <= $2
          AND geom IS NOT NULL
          AND event_type IN ('dark_vessel_detected', 'swpc_alert',
                              'military_aircraft_underway',
                              'loitering_detected',
                              'rendezvous_detected',
                              'aircraft_in_sanctioned_airspace',
                              'sanctioned_vessel_went_dark',
                              'sanctioned_vessel_rendezvous',
                              'sanctioned_vessel_multijurisdictional',
                              'shadow_fleet_cluster',
                              'volcanic_alert')
          AND (properties->>'withdrawn') IS NULL
          AND ST_Intersects(geom::geometry, ST_MakeEnvelope($3, $4, $5, $6, 4326))
        ORDER BY event_time DESC
        LIMIT 500
    """

    async with acquire_read() as conn:
        entity_rows = await conn.fetch(
            entity_query, time_from, time_to, west, south, east, north, types, limit
        )
        event_rows = await conn.fetch(
            event_query, time_from, time_to, west, south, east, north, limit
        )
        sanctions_rows = await conn.fetch(
            sanctions_tier1_query, time_from, time_to, west, south, east, north,
        )
        rare_rows = await conn.fetch(
            rare_tier1_query, time_from, time_to, west, south, east, north,
        )
        tier1_rows = list(sanctions_rows) + list(rare_rows)

    entities = []
    for r in entity_rows:
        entities.append({
            "id": str(r["id"]),
            "entity_type": r["entity_type"],
            "canonical_id": r["canonical_id"],
            "canonical_id_type": r["canonical_id_type"],
            "display_name": r["display_name"],
            "properties": coerce_jsonb(r["properties"]),
            "last_seen": r["last_seen"].isoformat() if r["last_seen"] else None,
            "position": {
                "lat": float(r["lat"]),
                "lng": float(r["lng"]),
                "altitude_m": float(r["altitude_m"]) if r["altitude_m"] is not None else None,
                "velocity_ms": float(r["velocity_ms"]) if r["velocity_ms"] is not None else None,
                "heading_deg": float(r["heading_deg"]) if r["heading_deg"] is not None else None,
                "time": r["position_time"].isoformat() if r["position_time"] else None,
            },
        })

    def _row_to_event(r) -> dict:
        return {
            "id": str(r["id"]),
            "event_type": r["event_type"],
            "event_subtype": r["event_subtype"],
            "event_time": r["event_time"].isoformat() if r["event_time"] else None,
            "severity": float(r["severity"]) if r["severity"] is not None else None,
            "severity_for_market": float(r["severity_for_market"]) if r["severity_for_market"] is not None else None,
            "title": r["title"],
            "description": r["description"],
            "properties": coerce_jsonb(r["properties"]),
            "domain": r["domain"],
            "decay_half_life_min": r["decay_half_life_min"],
            "entity_id": str(r["entity_id"]) if r["entity_id"] else None,
            "lat": float(r["lat"]) if r["lat"] is not None else None,
            "lng": float(r["lng"]) if r["lng"] is not None else None,
        }

    events = [_row_to_event(r) for r in event_rows]
    # Merge in tier-1 events that didn't make the main limit. Dedup by id
    # so we don't double-count when both queries return the same row.
    seen_ids = {e["id"] for e in events}
    for r in tier1_rows:
        if str(r["id"]) in seen_ids:
            continue
        events.append(_row_to_event(r))

    query_ms = int((time_mod.time() - t0) * 1000)

    return {
        "meta": {
            "bbox": list(bbox),
            "time_from": time_from.isoformat(),
            "time_to": time_to.isoformat(),
            "types": types,
            "entity_count": len(entities),
            "event_count": len(events),
            "query_ms": query_ms,
            "now": datetime.now(timezone.utc).isoformat(),
        },
        "entities": entities,
        "events": events,
    }


# ─── query_entity_detail — Phase 1.3 ──────────────────────────────────────


async def query_entity_detail(
    entity_id: str,
    *,
    track_window_hours: int = 24,
    related_events_window_hours: int = 24,
    related_events_radius_m: int = 50_000,
) -> Optional[Dict[str, Any]]:
    """Identity + recent track + nearby/related events + provenance."""
    try:
        eid = UUID(entity_id)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="entity id must be a UUID")

    now = datetime.now(timezone.utc)
    track_since = now - timedelta(hours=track_window_hours)
    related_since = now - timedelta(hours=related_events_window_hours)

    async with acquire_read() as conn:
        entity_row = await conn.fetchrow(
            """
            SELECT id, entity_type, canonical_id, canonical_id_type, display_name,
                   properties, first_seen, last_seen, confidence
            FROM entity WHERE id = $1
            """,
            eid,
        )
        if not entity_row:
            return None

        track_rows = await conn.fetch(
            """
            SELECT time,
                   ST_Y(geom::geometry) AS lat,
                   ST_X(geom::geometry) AS lng,
                   altitude_m, velocity_ms, heading_deg, properties
            FROM position_track
            WHERE entity_id = $1 AND time >= $2
            ORDER BY time DESC
            LIMIT 5000
            """,
            eid,
            track_since,
        )

        # Latest position to anchor the related-events query
        latest = track_rows[0] if track_rows else None
        related_event_rows = []
        if latest is not None:
            related_event_rows = await conn.fetch(
                """
                SELECT id, event_type, event_subtype, event_time, severity, title,
                       ST_Y(geom::geometry) AS lat,
                       ST_X(geom::geometry) AS lng,
                       ST_Distance(
                           geom,
                           ST_SetSRID(ST_MakePoint($3, $4), 4326)::geography
                       ) AS distance_m
                FROM event
                WHERE event_time >= $1
                  AND geom IS NOT NULL
                  AND ST_DWithin(
                          geom,
                          ST_SetSRID(ST_MakePoint($3, $4), 4326)::geography,
                          $2
                  )
                ORDER BY event_time DESC
                LIMIT 100
                """,
                related_since,
                related_events_radius_m,
                float(latest["lng"]),
                float(latest["lat"]),
            )

    return {
        "entity": {
            "id": str(entity_row["id"]),
            "entity_type": entity_row["entity_type"],
            "canonical_id": entity_row["canonical_id"],
            "canonical_id_type": entity_row["canonical_id_type"],
            "display_name": entity_row["display_name"],
            "properties": coerce_jsonb(entity_row["properties"]),
            "first_seen": entity_row["first_seen"].isoformat() if entity_row["first_seen"] else None,
            "last_seen": entity_row["last_seen"].isoformat() if entity_row["last_seen"] else None,
            "confidence": float(entity_row["confidence"]) if entity_row["confidence"] is not None else None,
        },
        "track": [
            {
                "time": r["time"].isoformat(),
                "lat": float(r["lat"]),
                "lng": float(r["lng"]),
                "altitude_m": float(r["altitude_m"]) if r["altitude_m"] is not None else None,
                "velocity_ms": float(r["velocity_ms"]) if r["velocity_ms"] is not None else None,
                "heading_deg": float(r["heading_deg"]) if r["heading_deg"] is not None else None,
                "properties": coerce_jsonb(r["properties"]),
            }
            for r in track_rows
        ],
        "related_events": [
            {
                "id": str(r["id"]),
                "event_type": r["event_type"],
                "event_subtype": r["event_subtype"],
                "event_time": r["event_time"].isoformat() if r["event_time"] else None,
                "severity": float(r["severity"]) if r["severity"] is not None else None,
                "title": r["title"],
                "lat": float(r["lat"]) if r["lat"] is not None else None,
                "lng": float(r["lng"]) if r["lng"] is not None else None,
                "distance_m": float(r["distance_m"]) if r["distance_m"] is not None else None,
            }
            for r in related_event_rows
        ],
        "meta": {
            "track_window_hours": track_window_hours,
            "track_count": len(track_rows),
            "related_events_window_hours": related_events_window_hours,
            "related_events_radius_m": related_events_radius_m,
            "related_events_count": len(related_event_rows),
        },
    }


# ─── Canonical-id lookup: /vessel/{mmsi} + /aircraft/{icao24} ─────────────
# Resolves a canonical_id to the UUID and dispatches to the entity-
# detail query. Lets the dashboard / external links share URLs by
# MMSI + ICAO24 instead of opaque UUIDs.


async def _by_canonical(entity_type: str, cid_type: str, canonical_id: str,
                         track_window_hours: int,
                         related_events_radius_m: int):
    async with acquire_read() as conn:
        row = await conn.fetchrow(
            """
            SELECT id FROM entity
            WHERE entity_type = $1
              AND canonical_id_type = $2
              AND canonical_id = $3
            LIMIT 1
            """,
            entity_type, cid_type, canonical_id,
        )
    if not row:
        raise HTTPException(
            status_code=404,
            detail=f"{entity_type} with {cid_type}={canonical_id} not found",
        )
    result = await query_entity_detail(
        str(row["id"]),
        track_window_hours=track_window_hours,
        related_events_radius_m=related_events_radius_m,
    )
    if result is None:
        raise HTTPException(status_code=404, detail="entity not found after id resolution")
    return result


# ─── Routes ───────────────────────────────────────────────────────────────


router = APIRouter()


@router.get("/viewport")
async def viewport(
    bbox: str = Query(..., description="west,south,east,north in degrees"),
    time_from: Optional[str] = Query(None, description="ISO-8601; defaults to 1h ago"),
    time_to: Optional[str] = Query(None, description="ISO-8601; defaults to now"),
    types: Optional[str] = Query("aircraft", description="comma-separated entity types"),
    limit: int = Query(1000, ge=1, le=15000),
    brief: bool = Query(False, description="If true, include a deterministic situation-note summary in meta.brief"),
    brief_llm: bool = Query(
        False,
        description="If true, run the LLM analyst-note layer on top of the "
                    "deterministic brief (~10-15s cold, <50ms cached). Falls "
                    "back to deterministic if Ollama is unreachable.",
    ),
):
    bbox_t = _parse_bbox(bbox)
    now = datetime.now(timezone.utc)
    tf = _parse_iso("time_from", time_from, now - timedelta(hours=1))
    tt = _parse_iso("time_to", time_to, now)
    if tf > tt:
        raise HTTPException(status_code=400, detail="time_from must be <= time_to")
    types_l = _parse_types(types)
    result = await query_viewport(
        bbox=bbox_t, time_from=tf, time_to=tt, types=types_l, limit=limit
    )
    if brief_llm:
        # LLM-augmented brief supersedes the deterministic one (it
        # already contains the deterministic text as a prefix).
        result["meta"]["brief"] = await generate_brief_llm_cached(result)
    elif brief:
        result["meta"]["brief"] = generate_brief_cached(result)
    return result


@router.get("/entity/{entity_id}")
async def entity_detail(
    entity_id: str,
    track_window_hours: int = Query(24, ge=1, le=720),
    related_events_radius_m: int = Query(50_000, ge=100, le=500_000),
    format: str = Query(
        "internal",
        pattern="^(internal|ftm)$",
        description=(
            "Response shape. 'internal' = full Glassbox detail "
            "(entity + track + nearby events). 'ftm' = "
            "FollowTheMoney JSON for OCCRP/OpenSanctions ecosystem "
            "interop — entity only, no track or related events."
        ),
    ),
):
    if format == "ftm":
        from ftm import entity_to_ftm
        try:
            eid = UUID(entity_id)
        except (ValueError, TypeError):
            raise HTTPException(status_code=400,
                                detail="entity id must be a UUID")
        async with acquire_read() as conn:
            row = await conn.fetchrow(
                "SELECT entity_type, canonical_id, display_name, properties "
                "FROM entity WHERE id = $1",
                eid,
            )
        if not row:
            raise HTTPException(status_code=404,
                                detail=f"entity {entity_id} not found")
        props = row["properties"]
        if isinstance(props, str):
            # asyncpg may return JSONB as string in some configs;
            # normalize so entity_to_ftm sees a dict either way.
            try:
                props = json.loads(props)
            except (TypeError, ValueError):
                props = {}
        props = dict(props or {})
        # Merge the entity table's top-level display_name into the
        # property bag — it lives in its own column so the translator
        # (which only sees the bag) wouldn't otherwise find it.
        # Property-bag wins on collision so an ingester that already
        # set its own display_name keeps authority.
        if row["display_name"] and "display_name" not in props:
            props["display_name"] = row["display_name"]
        ftm_doc = entity_to_ftm({
            "entity_type":  row["entity_type"],
            "canonical_id": row["canonical_id"],
            "properties":   props,
        })
        if ftm_doc is None:
            raise HTTPException(
                status_code=415,
                detail=(f"entity_type {row['entity_type']!r} has no "
                        f"FtM mapping; supported types: "
                        f"{sorted(__import__('ftm').supported_entity_types())}"),
            )
        return ftm_doc

    result = await query_entity_detail(
        entity_id,
        track_window_hours=track_window_hours,
        related_events_radius_m=related_events_radius_m,
    )
    if result is None:
        raise HTTPException(status_code=404, detail=f"entity {entity_id} not found")
    return result


@router.get("/vessel/{mmsi}")
async def vessel_by_mmsi(
    mmsi: str,
    track_window_hours: int = Query(24, ge=1, le=720),
    related_events_radius_m: int = Query(50_000, ge=100, le=500_000),
):
    # Strip any prefix the caller may have added; MMSIs are 9-digit
    # strings. We accept the raw digits, no further validation —
    # the canonical_id lookup will return 404 if it doesn't exist.
    cid = mmsi.strip()
    return await _by_canonical(
        "vessel", "mmsi", cid,
        track_window_hours, related_events_radius_m,
    )


@router.get("/aircraft/{icao24}")
async def aircraft_by_icao24(
    icao24: str,
    track_window_hours: int = Query(24, ge=1, le=720),
    related_events_radius_m: int = Query(50_000, ge=100, le=500_000),
):
    # ICAO24 is a 6-hex-digit code; we accept upper or lower case
    # and normalize to lowercase since that's how planes.py stores it.
    cid = icao24.strip().lower()
    return await _by_canonical(
        "aircraft", "icao24", cid,
        track_window_hours, related_events_radius_m,
    )


@router.get("/entities/{entity_id}/aliases")
async def entity_aliases(
    entity_id: str,
    min_confidence: float = Query(0.0, ge=0.0, le=1.0),
):
    """Phase 4b — return Splink ER alias edges for a vessel.

    Looks up entity_relation rows where relation_type='splink_alias'
    and from_entity_id = the given UUID. Returns the linked
    sanctioned-vessel records sorted by confidence DESC. Use this
    to ask "is this live vessel actually a sanctioned one under a
    different identifier?"
    """
    try:
        UUID(entity_id)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="entity_id must be a UUID")

    # Local import keeps the api module light when ER isn't being used.
    # `infra/er/splink_pipeline.py` lives at the EMPIRE ROOT (not
    # 21_GLASSBOX_AI), so resolve five levels up from this file:
    #   __file__ = 21_GLASSBOX_AI/web/routes/api_v1/core.py
    #   .parent ×5 = empire root
    import sys as _sys
    from pathlib import Path as _Path
    _ROOT = _Path(__file__).resolve().parent.parent.parent.parent.parent
    if str(_ROOT) not in _sys.path:
        _sys.path.insert(0, str(_ROOT))
    from infra.er.splink_pipeline import fetch_aliases_for_vessel

    aliases = await fetch_aliases_for_vessel(
        entity_id, min_confidence=min_confidence,
    )
    return {
        "entity_id":      entity_id,
        "min_confidence": min_confidence,
        "alias_count":    len(aliases),
        "aliases":        aliases,
    }


@router.get("/events/similar")
async def events_similar(
    id: Optional[str] = Query(None, description="Event UUID; uses its embedding."),
    q: Optional[str] = Query(None, description="Free-text query; embedded on the fly."),
    limit: int = Query(20, ge=1, le=100),
    within_days: int = Query(30, ge=1, le=365,
        description="Restrict to events with event_time within last N days."),
):
    """Phase 4a — semantic similarity search over event.embedding (HNSW
    cosine). Provide EITHER `id=<uuid>` to find neighbors of an existing
    event, OR `q=<text>` to embed a query string on the fly.

    Returns up to `limit` events ordered by cosine distance ascending
    (smallest distance = most similar). Excludes the seed event itself.
    Restricted to event types in TEXT_EVENT_TYPES.
    """
    from embeddings import embed_text, to_pgvector_literal, TEXT_EVENT_TYPES

    if not id and not q:
        raise HTTPException(status_code=400, detail="must supply id= or q=")
    if id and q:
        raise HTTPException(status_code=400, detail="supply only one of id/q")

    seed_uuid: Optional[str] = None
    seed_lit: Optional[str] = None

    if id:
        try:
            seed_uuid = str(UUID(id))
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="id must be a UUID")
        row = await fetch_read(
            "SELECT embedding::text AS emb_txt FROM event WHERE id = $1::uuid LIMIT 1",
            seed_uuid,
        )
        if not row or not row[0]["emb_txt"]:
            raise HTTPException(status_code=404,
                                detail="event not found or has no embedding")
        seed_lit = row[0]["emb_txt"]
    else:
        vec = embed_text(q or "")
        if vec is None:
            raise HTTPException(status_code=503,
                                detail="embedding unavailable")
        seed_lit = to_pgvector_literal(vec)

    rows = await fetch_read(
        """
        SELECT id, event_type, event_subtype, event_time, severity,
               title, description, properties,
               ST_Y(geom::geometry) AS lat,
               ST_X(geom::geometry) AS lng,
               embedding <=> $1::vector AS distance
        FROM event
        WHERE embedding IS NOT NULL
          AND event_type = ANY($2::text[])
          AND event_time >= NOW() - ($3 || ' days')::interval
          AND ($4::uuid IS NULL OR id <> $4::uuid)
        ORDER BY embedding <=> $1::vector
        LIMIT $5
        """,
        seed_lit, list(TEXT_EVENT_TYPES), str(within_days),
        seed_uuid, limit,
    )

    return {
        "seed": {"id": seed_uuid, "q": q},
        "limit": limit,
        "within_days": within_days,
        "results": [
            {
                "id": str(r["id"]),
                "event_type": r["event_type"],
                "event_subtype": r["event_subtype"],
                "event_time": r["event_time"].isoformat() if r["event_time"] else None,
                "severity": float(r["severity"]) if r["severity"] is not None else None,
                "title": r["title"],
                "description": r["description"],
                "properties": coerce_jsonb(r["properties"]),
                "lat": float(r["lat"]) if r["lat"] is not None else None,
                "lng": float(r["lng"]) if r["lng"] is not None else None,
                "distance": float(r["distance"]),
            }
            for r in rows
        ],
    }


@router.get("/entities/{entity_id}/cross_domain")
async def entity_cross_domain(
    entity_id: str,
    within_hours: int = Query(168, ge=1, le=2160,
        description="Window size in hours back from now. Default 168 = 7 days."),
    event_types: Optional[str] = Query(
        None,
        description=(
            "Optional comma-separated event_type whitelist (e.g. "
            "'rendezvous_detected,sanctioned_vessel_rendezvous,"
            "shadow_fleet_cluster'). When omitted, all multi-"
            "entity event_types are included."
        ),
    ),
    limit: int = Query(50, ge=1, le=500),
):
    """Multi-entity findings — algorithm-derived events where the
    given entity appears alongside one or more partners.

    Glassbox algorithms that detect pair / cluster patterns
    (rendezvous, proximity, sanctioned_vessel_*, shadow_fleet_cluster,
    etc.) record `properties.entity_ids` as a JSON array of all
    participating entity UUIDs. This endpoint surfaces those
    events for a single entity, with each partner's
    display_name + canonical_id resolved so an investigator
    doesn't need a second round-trip to /entity/{id}.

    Returns each event once with `partners: [{entity_id, canonical_id,
    display_name, entity_type}, ...]` containing every OTHER
    entity in the same finding (the queried entity is excluded).
    """
    try:
        UUID(entity_id)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400,
                            detail="entity_id must be a UUID")

    types_list: Optional[List[str]] = None
    if event_types:
        types_list = [t.strip() for t in event_types.split(",") if t.strip()]
        if not types_list:
            types_list = None

    # 2026-05-21: switched the entity-ids filter from
    #   `e.properties->'entity_ids' ? $1::text`  (key-existence on extracted value)
    # to
    #   `e.properties @> jsonb_build_object('entity_ids',
    #                                       jsonb_build_array($1::text))`
    # because the rewritten form lets the existing `event_props_gin`
    # (GIN on properties with jsonb_ops) drive an index scan.
    # `entity_ids` is always a jsonb array (verified across 499,593 active
    # rows on 2026-05-21 — `jsonb_typeof(properties->'entity_ids') = 'array'`
    # for every row), so the two predicates are semantically equivalent.
    # Performance impact: previously the planner picked
    # `event_event_time_idx` for the time-range and post-filtered by
    # jsonb, scanning 13.7M rows in a single chunk to return 8 — observed
    # 219.6s wall on entity 351d0fd8-... Now the planner uses
    # `event_props_gin` for the containment filter — observed 45–175ms
    # on the same entities. Routed back to api_pool (fetch_read) since
    # the query now fits well inside the 10s budget. Original
    # P1-A regression note + the spawned task (covering-index proposal,
    # rendered moot by this rewrite) live in the session handoff.
    rows = await fetch_read(
        """
        WITH multi_entity_events AS (
            SELECT
                e.id,
                e.event_type,
                e.event_subtype,
                e.event_time,
                e.severity,
                e.title,
                e.description,
                e.properties,
                e.domain,
                ST_Y(e.geom::geometry) AS lat,
                ST_X(e.geom::geometry) AS lng,
                -- Pull the partner entity_ids out of the JSON array,
                -- excluding the queried entity itself. The casts go
                -- text -> uuid via PostgreSQL because jsonb_array_elements_text
                -- returns text and we need a uuid[] for the join below.
                (
                    SELECT array_agg(other_id::uuid)
                    FROM jsonb_array_elements_text(
                             e.properties->'entity_ids'
                         ) AS other_id
                    WHERE other_id::uuid <> $1::uuid
                ) AS partner_entity_ids
            FROM event e
            WHERE e.event_time >= NOW() - ($2::int || ' hours')::interval
              AND e.properties ? 'entity_ids'
              AND e.properties @> jsonb_build_object(
                      'entity_ids', jsonb_build_array($1::text))
              AND ($3::text[] IS NULL OR e.event_type = ANY($3::text[]))
            ORDER BY e.event_time DESC
            LIMIT $4
        )
        SELECT
            m.id,
            m.event_type, m.event_subtype, m.event_time, m.severity,
            m.title, m.description, m.properties, m.domain,
            m.lat, m.lng,
            COALESCE(
                (
                    SELECT jsonb_agg(jsonb_build_object(
                        'entity_id',     pe.id::text,
                        'canonical_id',  pe.canonical_id,
                        'canonical_id_type', pe.canonical_id_type,
                        'display_name',  pe.display_name,
                        'entity_type',   pe.entity_type
                    ) ORDER BY pe.display_name NULLS LAST)
                    FROM unnest(m.partner_entity_ids) AS pid
                    LEFT JOIN entity pe ON pe.id = pid
                ),
                '[]'::jsonb
            ) AS partners
        FROM multi_entity_events m
        """,
        entity_id, within_hours, types_list, limit,
    )

    items = []
    for r in rows:
        props = r["properties"]
        if isinstance(props, str):
            try:
                props = json.loads(props)
            except (TypeError, ValueError):
                props = {}
        partners = r["partners"]
        if isinstance(partners, str):
            try:
                partners = json.loads(partners)
            except (TypeError, ValueError):
                partners = []
        items.append({
            "id":            str(r["id"]),
            "event_type":    r["event_type"],
            "event_subtype": r["event_subtype"],
            "event_time":    r["event_time"].isoformat() if r["event_time"] else None,
            "severity":      r["severity"],
            "title":         r["title"],
            "description":   r["description"],
            "properties":    props or {},
            "domain":        r["domain"],
            "lat":           r["lat"],
            "lng":           r["lng"],
            "partners":      partners or [],
        })
    return {
        "entity_id":    entity_id,
        "within_hours": within_hours,
        "event_types":  types_list,
        "result_count": len(items),
        "events":       items,
    }


@router.get("/event/{event_id}")
async def event_detail(event_id: str):
    """Single-event detail row by UUID. Mirrors entity_detail's role
    for the event table — agents drilling from a search/similar/
    in_bbox result into the full row.

    Returns the full event record including geom (lat/lng), severity,
    properties (JSON bag), domain, decay window, and the related
    entity_id (when populated). 404 when no row matches the UUID.
    Embedding column is excluded from the response (binary blob,
    agents can call /events/similar?id= to use it instead).
    """
    try:
        UUID(event_id)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400,
                            detail="event_id must be a UUID")

    rows = await fetch_read(
        """
        SELECT
            id, event_type, event_subtype, event_time,
            severity, severity_for_market,
            title, description, properties,
            domain, decay_half_life_min, entity_id,
            ST_Y(geom::geometry) AS lat,
            ST_X(geom::geometry) AS lng
        FROM event
        WHERE id = $1::uuid
        LIMIT 1
        """,
        event_id,
    )
    if not rows:
        raise HTTPException(status_code=404,
                            detail=f"event {event_id} not found")
    r = rows[0]
    # Normalize asyncpg's JSONB-as-text in some configs.
    props = r["properties"]
    if isinstance(props, str):
        try:
            props = json.loads(props)
        except (TypeError, ValueError):
            props = {}
    return {
        "id":                  str(r["id"]),
        "event_type":          r["event_type"],
        "event_subtype":       r["event_subtype"],
        "event_time":          r["event_time"].isoformat() if r["event_time"] else None,
        "severity":            r["severity"],
        "severity_for_market": r["severity_for_market"],
        "title":               r["title"],
        "description":         r["description"],
        "properties":          props or {},
        "domain":              r["domain"],
        "decay_half_life_min": r["decay_half_life_min"],
        "entity_id":           str(r["entity_id"]) if r["entity_id"] else None,
        "lat":                 r["lat"],
        "lng":                 r["lng"],
    }
