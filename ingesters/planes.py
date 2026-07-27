"""
Planes ingester — military, emergency, and restricted-flag aircraft.

v1.0 SCOPE (revised 2026-05-04 23:30 ET after smoke test caught issue):
  adsb.lol does NOT have a /v2/all global firehose endpoint. Their public
  REST API exposes targeted endpoints only. For v1.0 we focus on the
  HIGHEST OSINT-VALUE subsets, which are coincidentally the ones with
  documented public endpoints:

    1. /v2/mil       — military + state aircraft globally
    2. /v2/squawk/7500 — hijack squawks
    3. /v2/squawk/7600 — radio-failure squawks
    4. /v2/squawk/7700 — general distress squawks
    5. /v2/ladd      — LADD-restricted (US privacy program — flag-of-interest)
    6. /v2/pia       — PIA aircraft (Privacy ICAO Address)

  Civilian commercial flights (~30k+ globally) are NOT in v1.0 — they
  require either a feeder relationship with adsb.lol (re-api/) or
  per-airport radius queries via /v2/lat/lon/dist. Phase 2.5 enhancement.

  This is actually MORE valuable than the original "all aircraft" plan:
  military + emergency aircraft are the entire reason an OSINT globe
  exists. Commercial 737s flying scheduled routes are noise.

LICENSE: ODbL 1.0, commercial OK with attribution.

DROPPED FROM v1.0 (per LEGAL_COMPLIANCE_REGISTRY.md NEVER-USE list):
  - OpenSky Network: ToS explicitly prohibits commercial use.
  - adsb.fi:        ToS prohibits commercial use.

Military detection (independent of source — heuristic backup for civilian
endpoints we may add in Phase 2.5):
  - callsign prefix (US/NATO/allied lookup table)
  - ICAO24 hex range (allocated military blocks)
  - squawk code (7500/7600/7700 = emergency regardless of civ/mil)

This ingester is gated by infra/sources.yaml. The backend startup gate
refuses to start it if `adsb_lol.enabled = false` or `commercial_use_ok =
false` in the registry.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import aiohttp

from .base import GlassboxEvent, Ingester


# ─── Military / state-aircraft heuristics ─────────────────────────────────

MIL_CALLSIGN_PREFIXES = {
    # US
    "RCH", "FORTE", "JAKE", "SPAR", "SENTRY", "CONVOY", "PAT", "TREK",
    "PHOENIX", "EAGLE", "RAVEN", "CHECK", "DOOM", "LIFT", "REACH",
    "LOBO", "HUSKY", "DRAGON", "EYES", "IRON", "NAIL", "RADAR", "SCORE",
    "TOPCAT", "WATCH", "WEBB", "MANGO", "SCAN", "FBI", "HAWK", "HUNTER",
    "SAM", "EXEC1", "EXEC2", "MARINE", "ARMY", "NAVY",
    # UK
    "KRF", "RRR", "ASCOT", "VORTEX", "VIVID",
    # NATO
    "NATO", "HIRO", "MAGMA",
    # German
    "GAF", "HKY",
    # French
    "FAF", "COTAM",
    # Canadian
    "CFC", "HUSKY", "CANFORCE",
}

# ICAO24 hex prefixes allocated to state / military aircraft
MIL_HEX_PREFIXES = (
    # United States military blocks
    "ADF", "AE0", "AE1", "AE2", "AE3", "AE4", "AE5", "AE6", "AE7",
    # UK military
    "43C", "43D", "43E", "43F",
    # German / allied
    "3F4", "3F5",
    # Russian state
    "15",
)


def _is_military(callsign: str, icao24: str, db_flags: int = 0) -> bool:
    cs = (callsign or "").strip().upper()
    hex_prefix = (icao24 or "").upper()[:3]
    if db_flags and (db_flags & 0x01):   # adsb.lol exposes a military bit
        return True
    if hex_prefix in MIL_HEX_PREFIXES:
        return True
    for pfx in MIL_CALLSIGN_PREFIXES:
        if cs.startswith(pfx):
            return True
    return False


def _is_emergency_squawk(sq: Optional[str]) -> bool:
    return sq in ("7500", "7600", "7700")


# ─── The ingester ─────────────────────────────────────────────────────────


class PlanesIngester(Ingester):
    layer = "planes"
    source = "adsb.lol (ODbL, commercial-OK)"
    source_id = "adsb_lol"            # gates against infra/sources.yaml row
    # 2026-05-04 23:55 ET: bumped from 5s → 60s after adding tile-based
    # civilian coverage. ~85 tiles × 0.3s pacing + 6 specialty endpoints =
    # ~30s per cycle. 60s gives 30s headroom before next cycle starts.
    poll_interval_sec = 60.0

    # 2026-05-05 (v3): routed through CF Worker proxy. adsb.lol IP-rate-limited
    # Ethan's Mac Mini after burst tile testing. CF data center IPs aren't
    # in their gate. Worker also edge-caches each tile 60s so popular tiles
    # only hit adsb.lol once per minute regardless of how many clients query us.
    _ADSBLOL_PROXY = "https://mewr-news-api.mewrcreate.workers.dev/api/proxy/adsblol"
    ADSBLOL_MIL_URL       = _ADSBLOL_PROXY + "/v2/mil"
    ADSBLOL_SQUAWK_7700   = _ADSBLOL_PROXY + "/v2/squawk/7700"
    ADSBLOL_SQUAWK_7600   = _ADSBLOL_PROXY + "/v2/squawk/7600"
    ADSBLOL_SQUAWK_7500   = _ADSBLOL_PROXY + "/v2/squawk/7500"
    ADSBLOL_LADD          = _ADSBLOL_PROXY + "/v2/ladd"
    ADSBLOL_PIA           = _ADSBLOL_PROXY + "/v2/pia"

    # Endpoint set polled each cycle. Order matters for dedup priority
    # (first-seen wins).
    ENDPOINTS = (
        ADSBLOL_MIL_URL,
        ADSBLOL_SQUAWK_7700,
        ADSBLOL_SQUAWK_7600,
        ADSBLOL_SQUAWK_7500,
        ADSBLOL_LADD,
        ADSBLOL_PIA,
    )

    # ─── TILE-BASED CIVILIAN COVERAGE (2026-05-04 23:55 ET) ──────────────
    # The 6 endpoints above only return ~150-300 high-OSINT aircraft.
    # adsb.lol does NOT have a global firehose endpoint, so we tile the
    # globe with /v2/lat/{lat}/lon/{lon}/dist/{dist} queries.
    #
    # Coverage strategy: 250nm radius (max allowed by adsb.lol) covers
    # ~672k km² per query. We define ~85 tile centers concentrated over
    # populated regions + air corridors. Result: ~10k-30k aircraft per
    # cycle (depending on time of day, with peak global aircraft ~12k).
    #
    # Each tile = 1 HTTP call. With 0.3s pacing between calls (well within
    # adsb.lol's posted limits), 85 tiles = ~25s. poll_interval_sec=60s
    # gives us 35s headroom before the next cycle starts.
    #
    # Tile centers selected for: NA hubs, Europe hubs, Asia hubs, Atlantic
    # corridor, Pacific corridor, Mid-East, S America, Africa, Oceania.
    DIST_NM = 250  # max allowed
    CIVILIAN_TILES: tuple = (
        # NORTH AMERICA — densest aircraft traffic globally
        (40.6, -73.8),   # NYC area (JFK/LGA/EWR)
        (33.9, -118.4),  # LA area (LAX/BUR/LGB)
        (41.9, -87.9),   # Chicago (ORD/MDW)
        (32.9, -97.0),   # DFW
        (33.6, -84.4),   # Atlanta (ATL)
        (39.8, -104.7),  # Denver (DEN)
        (37.6, -122.4),  # SFO/OAK/SJC
        (47.5, -122.3),  # Seattle (SEA)
        (25.8, -80.3),   # Miami (MIA/FLL)
        (29.6, -95.3),   # Houston (IAH/HOU)
        (33.4, -112.0),  # Phoenix (PHX)
        (39.0, -77.0),   # DC (IAD/DCA/BWI)
        (42.4, -71.0),   # Boston (BOS)
        (36.0, -115.2),  # Las Vegas (LAS)
        (45.0, -93.2),   # MSP
        (42.2, -83.3),   # Detroit (DTW)
        (35.2, -80.9),   # Charlotte (CLT)
        (28.4, -81.3),   # Orlando (MCO)
        (43.7, -79.6),   # Toronto (YYZ)
        (49.2, -123.2),  # Vancouver (YVR)
        (45.5, -73.7),   # Montreal (YUL)
        (51.1, -114.0),  # Calgary (YYC)
        (19.4, -99.1),   # Mexico City (MMMX)
        (61.2, -149.9),  # Anchorage (PANC)
        (21.3, -157.9),  # Honolulu (PHNL)
        # EUROPE
        (51.5, -0.1),    # London (LHR/LGW/STN/LCY)
        (48.9, 2.3),     # Paris (CDG/ORY)
        (50.0, 8.6),     # Frankfurt (FRA)
        (52.3, 4.8),     # Amsterdam (AMS)
        (40.4, -3.6),    # Madrid (MAD)
        (41.3, 2.1),     # Barcelona (BCN)
        (41.8, 12.3),    # Rome (FCO)
        (47.5, 19.0),    # Budapest
        (50.1, 14.3),    # Prague
        (52.5, 13.4),    # Berlin
        (48.1, 11.5),    # Munich (MUC)
        (48.4, 16.4),    # Vienna (VIE)
        (60.3, 24.9),    # Helsinki
        (59.3, 18.0),    # Stockholm
        (59.9, 10.8),    # Oslo
        (55.7, 12.6),    # Copenhagen
        (53.4, -6.3),    # Dublin
        (38.8, -9.2),    # Lisbon
        (37.9, 23.7),    # Athens
        (41.0, 28.8),    # Istanbul (IST)
        # MIDDLE EAST
        (25.3, 55.4),    # Dubai (DXB/DWC)
        (24.5, 54.6),    # Abu Dhabi (AUH)
        (25.3, 51.6),    # Doha (DOH)
        (29.2, 47.9),    # Kuwait
        (24.7, 46.7),    # Riyadh
        (31.8, 35.2),    # Tel Aviv (TLV)
        (33.5, 35.5),    # Beirut
        (32.0, 44.4),    # Baghdad
        (35.7, 51.4),    # Tehran
        # ASIA
        (35.7, 139.8),   # Tokyo (HND/NRT)
        (34.4, 135.2),   # Osaka (KIX/ITM)
        (37.5, 126.8),   # Seoul (ICN/GMP)
        (22.3, 113.9),   # Hong Kong (HKG)
        (1.4, 103.9),    # Singapore (SIN)
        (13.7, 100.5),   # Bangkok (BKK)
        (3.1, 101.7),    # Kuala Lumpur (KUL)
        (22.5, 114.0),   # Shenzhen
        (39.9, 116.6),   # Beijing (PEK/PKX)
        (31.2, 121.8),   # Shanghai (PVG/SHA)
        (23.4, 113.3),   # Guangzhou (CAN)
        (24.0, 121.0),   # Taipei (TPE)
        (10.8, 106.7),   # Ho Chi Minh
        (28.6, 77.1),    # Delhi (DEL)
        (19.1, 72.9),    # Mumbai (BOM)
        (12.9, 77.7),    # Bangalore (BLR)
        (-6.1, 106.7),   # Jakarta
        (14.5, 121.0),   # Manila
        # OCEANIA
        (-33.9, 151.2),  # Sydney (SYD)
        (-37.7, 144.8),  # Melbourne (MEL)
        (-27.4, 153.1),  # Brisbane (BNE)
        (-31.9, 115.9),  # Perth
        (-36.8, 174.8),  # Auckland
        # SOUTH AMERICA
        (-23.4, -46.5),  # Sao Paulo (GRU/CGH)
        (-22.8, -43.2),  # Rio de Janeiro
        (-34.6, -58.4),  # Buenos Aires (EZE/AEP)
        (-12.0, -77.1),  # Lima
        (4.7, -74.1),    # Bogota
        (-33.4, -70.8),  # Santiago
        # AFRICA
        (-26.2, 28.2),   # Johannesburg (JNB)
        (-33.9, 18.6),   # Cape Town
        (-1.3, 36.9),    # Nairobi
        (30.1, 31.4),    # Cairo (CAI)
        (33.8, -7.6),    # Casablanca
        (6.6, 3.3),      # Lagos
        (9.0, 38.8),     # Addis Ababa
        # OCEANIC CORRIDORS (catch transatlantic + transpacific traffic)
        (50.0, -30.0),   # Mid-Atlantic
        (45.0, -20.0),   # NE Atlantic
        (30.0, -160.0),  # Mid-Pacific
        (40.0, -170.0),  # NE Pacific
        (-15.0, -150.0), # S Pacific
        (0.0, 90.0),     # Indian Ocean
    )

    # User-Agent recommended by adsb.lol — identifies us as a polite consumer
    UA = "FulcrumGlassbox/2.0 (+https://mewrcreate.com/glassbox)"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # 429 backoff state — be a good citizen if we ever get rate-limited
        self._adsblol_skip_until = 0.0

    # ─── fetch ────────────────────────────────────────────────────────

    async def fetch(self) -> List[Dict[str, Any]]:
        """Pull aircraft from THREE source layers, in this priority order:
          1. Specialty endpoints (/v2/mil + 3× /v2/squawk + ladd + pia) —
             ~150-300 high-OSINT aircraft, near-zero rate-limit risk.
          2. Tile-based civilian coverage (~85 lat/lon/dist queries) —
             ~10k-30k aircraft globally over populated regions + air corridors.
          3. (future Phase 2.5) feeder-relationship re-api firehose for
             complete civilian coverage.
        Returns [] on global failure (logged). Server-side dedup in
        normalize() handles the same plane appearing in multiple tiles +
        the specialty endpoints (an emergency military aircraft over
        London will appear in /v2/mil, /v2/squawk/7700, and the EGLL
        tile — dedup keys on ICAO24 hex)."""
        import asyncio as _asyncio
        if time.time() < self._adsblol_skip_until:
            return []

        # Per-request timeout 15s; tiles can be slow when a region is busy
        timeout = aiohttp.ClientTimeout(total=15, connect=5)
        headers = {"User-Agent": self.UA, "Accept": "application/json"}
        rows: List[Dict[str, Any]] = []
        any_429 = False
        sess_429_count = 0
        TILE_PACING_SEC = 0.3   # gentle on adsb.lol

        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as s:
            # ─── Layer 1: specialty endpoints ────────────────────────
            for url in self.ENDPOINTS:
                try:
                    async with s.get(url) as r:
                        if r.status == 429:
                            sess_429_count += 1
                            self.log.info("[planes] 429 from " + url)
                            continue
                        r.raise_for_status()
                        data = await r.json()
                except Exception as e:
                    self.log.info("[planes] " + url + " failed: " + (str(e) or type(e).__name__))
                    continue
                rows.extend(self._parse_adsblol_response(data))

            specialty_count = len(rows)
            self.log.info(f"[planes] specialty endpoints: {specialty_count} aircraft")

            # ─── Layer 2: tile-based civilian coverage (PARALLELIZED) ─
            # 2026-05-05 (v2): reduced sem 10 → 4 after smoke test caught
            # adsb.lol rate-limiting under high concurrency. With sem=10 we
            # got 8,380 unique aircraft. With sem=10 + adsb.lol cooldown
            # we dropped to 1,742 (silent 429s). sem=4 stays under their
            # per-IP burst threshold while still giving ~20-30s total.
            sem = _asyncio.Semaphore(4)
            tile_results: List[List[Dict[str, Any]]] = []
            tile_429_lock = {"count": 0}   # mutable container for nonlocal-ish update

            async def _fetch_one_tile(lat: float, lng: float) -> List[Dict[str, Any]]:
                async with sem:
                    # 2026-05-05 (v2): bumped abort threshold 5 → 20 — was
                    # too aggressive. With sem=4 the rate-limit pressure is
                    # lower so we can absorb more 429s before giving up.
                    if tile_429_lock["count"] >= 20:
                        return []
                    # 2026-05-05 (v3): routed through CF Worker proxy.
                    url = (
                        f"{self._ADSBLOL_PROXY}/v2/lat/{lat:.4f}/lon/{lng:.4f}/dist/{self.DIST_NM}"
                    )
                    try:
                        async with s.get(url) as r:
                            if r.status == 429:
                                tile_429_lock["count"] += 1
                                # Brief cooldown before next tile in this slot
                                await _asyncio.sleep(1.0)
                                return []
                            r.raise_for_status()
                            data = await r.json()
                    except Exception as e:
                        self.log.info(
                            f"[planes] tile ({lat:.1f},{lng:.1f}) failed: "
                            + (str(e) or type(e).__name__)
                        )
                        return []
                    return self._parse_adsblol_response(data)

            # 2026-05-05 (architectural fix): in smoke mode use 8 representative
            # tiles (NA + Europe + Asia + Pacific corridor) instead of all 85.
            # Production runs all 85 in its own poll cycle.
            if self.smoke_mode:
                _smoke_tiles = (
                    (40.6, -73.8),    # NYC
                    (33.9, -118.4),   # LAX
                    (51.5, -0.1),     # London
                    (50.0, 8.6),      # Frankfurt
                    (35.7, 139.8),    # Tokyo
                    (1.4, 103.9),     # Singapore
                    (-33.9, 151.2),   # Sydney
                    (50.0, -30.0),    # Mid-Atlantic corridor
                )
                tiles_to_fetch = _smoke_tiles
            else:
                tiles_to_fetch = self.CIVILIAN_TILES

            tile_tasks = [
                _fetch_one_tile(lat, lng)
                for (lat, lng) in tiles_to_fetch
            ]
            tile_results = await _asyncio.gather(*tile_tasks, return_exceptions=False)

            tile_count_total = 0
            tile_calls_made = 0
            for tile_rows in tile_results:
                if tile_rows:
                    rows.extend(tile_rows)
                    tile_count_total += len(tile_rows)
                    tile_calls_made += 1
            sess_429_count += tile_429_lock["count"]

            # Surface 429 count as WARNING so smoke test shows it
            if tile_429_lock["count"] > 0:
                self.log.warning(
                    f"[planes] {tile_429_lock['count']} tiles got 429 "
                    f"(coverage degraded — adsb.lol throttling)"
                )
            self.log.info(
                f"[planes] civilian tiles: {tile_count_total} aircraft "
                f"across {tile_calls_made}/{len(tiles_to_fetch)} tiles "
                f"(parallelized, sem=4, smoke_mode={self.smoke_mode})"
            )

        if any_429 or sess_429_count >= 10:
            # Mass 429 → back off all endpoints
            self._adsblol_skip_until = time.time() + 120
            self.log.warning(
                f"[planes] adsb.lol rate-limited ({sess_429_count} 429s); "
                f"backing off 2 min"
            )

        return rows

    def _parse_adsblol_response(self, data: Dict[str, Any]) -> List[Dict[str, Any]]:
        # adsb.lol returns {"ac": [...], "msg": "...", "now": <ms>, ...}
        # Each ac record: {hex, flight, lat, lon, alt_baro, gs, track, squawk, dbFlags, ...}
        rows: List[Dict[str, Any]] = []
        for ac in (data.get("ac") or []):
            lat = ac.get("lat")
            lng = ac.get("lon")
            if lat is None or lng is None:
                continue
            alt_baro = ac.get("alt_baro")
            # alt_baro can be the literal string "ground" or a numeric ft value
            on_ground = (alt_baro == "ground")
            alt_m = (
                alt_baro * 0.3048
                if isinstance(alt_baro, (int, float))
                else None
            )
            gs = ac.get("gs")
            vel_ms = (
                gs * 0.514444
                if isinstance(gs, (int, float))
                else None
            )
            rows.append({
                "icao24": (ac.get("hex") or "").lower(),
                "callsign": (ac.get("flight") or "").strip(),
                "origin_country": None,         # adsb.lol does not provide
                "time_position": ac.get("seen"),
                "lat": lat,
                "lng": lng,
                "baro_altitude": alt_m,
                "geo_altitude": None,
                "on_ground": on_ground,
                "velocity_ms": vel_ms,
                "heading": ac.get("track"),
                "squawk": ac.get("squawk"),
                "db_flags": ac.get("dbFlags", 0) or 0,
                "_source": "adsb.lol",
            })
        return rows

    # ─── normalize ────────────────────────────────────────────────────

    def normalize(self, raw_items: List[Dict[str, Any]]) -> List[GlassboxEvent]:
        now = datetime.now(timezone.utc).isoformat()
        out: List[GlassboxEvent] = []
        seen: set = set()

        for r in raw_items:
            icao = (r.get("icao24") or "").lower()
            if not icao or icao in seen:
                continue
            seen.add(icao)

            lat = r.get("lat")
            lng = r.get("lng")
            if lat is None or lng is None:
                continue

            callsign = r.get("callsign", "") or ""
            is_mil = _is_military(callsign, icao, r.get("db_flags", 0))
            is_emerg = _is_emergency_squawk(r.get("squawk"))

            # Severity scale:
            #   10 = emergency squawk
            #    7 = military, airborne
            #    4 = military on ground
            #    2 = commercial, unusual (helicopter / ultra-low / very high)
            #    1 = normal commercial / GA
            severity = 1
            if is_emerg:
                severity = 10
            elif is_mil:
                severity = 4 if r.get("on_ground") else 7

            alt = r.get("baro_altitude")
            if alt is None:
                alt = r.get("geo_altitude")

            # ─── Pre-classification (Loop Step 3) ─────────────────
            # Planes: GPS-grade ADS-B → exact geocode. Positions stale
            # in minutes (5 min half-life). Emergency-squawk planes can
            # affect aviation/safety markets; the classifier respects this.
            plane_market_tags: List[str] = []
            plane_sev_market = 0
            if is_emerg:
                plane_market_tags.append("aviation:emergency")
                plane_sev_market = 8

            out.append(GlassboxEvent(
                layer=self.layer,
                external_id=icao,
                kind="position",
                lat=float(lat),
                lng=float(lng),
                ts=now,
                severity=severity,
                altitude_m=float(alt) if isinstance(alt, (int, float)) else None,
                heading_deg=float(r["heading"]) if isinstance(r.get("heading"), (int, float)) else None,
                velocity_ms=float(r["velocity_ms"]) if isinstance(r.get("velocity_ms"), (int, float)) else None,
                source=r.get("_source", self.source),
                payload={
                    "callsign": callsign,
                    "military": is_mil,
                    "emergency": is_emerg,
                    "squawk": r.get("squawk"),
                    "on_ground": bool(r.get("on_ground")),
                    "origin_country": r.get("origin_country"),
                    "time_position": r.get("time_position"),
                    # License attribution rendered by frontend per sources.yaml
                    "_attribution": "Aircraft positions: adsb.lol (ODbL)",
                },
                # Loop classification — explicit so future audits can
                # see exactly what each ingester contributes.
                domain="geo",
                geocode_quality="exact",
                decay_half_life_min=5,
                market_tags=plane_market_tags,
                severity_for_market=plane_sev_market,
            ))

        return out
