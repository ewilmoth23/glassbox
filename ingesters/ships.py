"""
Ships ingester — AIS vessel positions, multi-source (v121).

Sources (no auth, generous rate limits):
  1. Digitraffic Finland  — https://meri.digitraffic.fi/api/ais/v1/locations
                            Baltic + Gulf of Finland. ~2000–4000 vessels.
                            Static info (name, IMO, callsign, ship_type) fetched
                            from /v1/vessels and merged into each position
                            record via the static-info cache (Phase static-info,
                            2026-05-08).
  2. BarentsWatch (Norway)— https://www.barentswatch.no/bwapi/v2/geodata/ais
                            Nordic + Arctic routes, military often suppressed.
                            ~1000–3000 vessels.
  3. Danish Maritime (DK) — https://dmiapi.govcloud.dk/v1/ais/
                            Kattegat + North Sea + Danish straits.
                            ~1500–2500 vessels.

v121 change: removed the 48-waypoint "_global_fallback()" generator that used
to run when all 3 upstreams returned empty. Those ghost markers had sog=0,
never moved, were indistinguishable from real AIS in the client, and their
content hash never changed so dedup held them in the client forever. Empty
layer + log warning is honest; see `fetch()` below.

De-dup by MMSI across all sources (the first source wins its record).

Dark-ship heuristics (see `_suspicious`):
  - MMSI out of normal range (leading zeros, all 0s, all 9s)
  - AIS type = 0 or empty when vessel is clearly moving
  - Speed > 3 knots but no nav status or name
  - Identical MMSI reported in two wildly different locations in same cycle
    → flagged via out["_dark"] = True (renderer can tint red)
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .base import GlassboxEvent, Ingester


_TYPE_SEVERITY = {
    # AIS type codes → severity ranking
    30: 1,   # fishing
    35: 6,   # military ops
    36: 5,   # sailing
    52: 4,   # tug
    70: 2, 71: 2, 72: 2, 73: 2, 74: 2, 75: 2, 76: 2, 77: 2, 78: 2, 79: 2,  # cargo
    80: 3, 81: 3, 82: 3, 83: 3, 84: 3, 85: 3, 86: 3, 87: 3, 88: 3, 89: 3,  # tanker
}


def _severity(ship_type: int) -> int:
    return _TYPE_SEVERITY.get(int(ship_type or 0), 0)


def _suspicious(r: Dict[str, Any]) -> bool:
    """Heuristic dark-ship flag. Not perfect — but visually useful.
    Defensive coercions: upstream feeds return ints where we expect strings
    (and vice versa) — belt-and-suspenders with str()."""
    mmsi = str(r.get("mmsi") or "").strip()
    try:
        ship_type = int(r.get("ship_type") or 0)
    except (TypeError, ValueError):
        ship_type = 0
    sog = r.get("sog") or 0
    name_raw = r.get("name")
    name = str(name_raw).strip() if name_raw is not None else ""
    if not mmsi or mmsi in ("0", "000000000") or mmsi.startswith("000"):
        return True
    if ship_type == 0 and isinstance(sog, (int, float)) and sog > 3 and not name:
        return True
    return False


class ShipsIngester(Ingester):
    layer = "ships"
    source = "AIS multi-source (Digitraffic + BarentsWatch + DMA)"
    source_id = "digitraffic_finland"   # primary; gates against sources.yaml
    additional_source_ids = ("barentswatch_ais", "dma_denmark_ais")
    poll_interval_sec = 60.0

    DIGITRAFFIC_URL = "https://meri.digitraffic.fi/api/ais/v1/locations"
    DIGITRAFFIC_VESSELS_URL = "https://meri.digitraffic.fi/api/ais/v1/vessels"
    BARENTSWATCH_URL = "https://www.barentswatch.no/bwapi/v2/geodata/ais"
    # Danish Maritime Authority publishes open AIS via their data hub.
    # Their public JSON endpoint 1-hour snapshot.
    DMA_URL = "https://dmiapi.govcloud.dk/v1/ais/locations"

    # Static-info cache refresh cadence (seconds). The vessel registry changes
    # slowly — names + IMOs are stable for years. 1h is plenty.
    STATIC_INFO_REFRESH_SEC = 3600.0

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Static-info cache: keyed by stringified MMSI, value is the vessel
        # registry record from /v1/vessels (name, imo, callSign, shipType, etc.)
        self._digitraffic_static: Dict[str, Dict[str, Any]] = {}
        # epoch seconds of last successful static-info fetch; 0 = never
        self._digitraffic_static_last_refresh: float = 0.0

    # ─── static-info cache ─────────────────────────────────────────────

    async def _refresh_digitraffic_static_info(self) -> int:
        """Fetch /v1/vessels (the AIS Type-5 / static-info firehose) and
        update the in-memory cache. Returns count of vessels loaded.

        The /v1/locations endpoint we hit every 60s is the position firehose
        (AIS Types 1/2/3) — those broadcasts do NOT carry vessel name, IMO,
        or callsign. Static info lives on /v1/vessels (one row per known
        MMSI). This method refreshes the cache hourly; vessels' static info
        is ~stable for years.
        """
        import aiohttp
        timeout = aiohttp.ClientTimeout(total=30)
        # Digitraffic returns HTTP 406 unless we explicitly request gzip.
        # aiohttp normally negotiates gzip automatically, but the server
        # is stricter than typical — set it explicitly.
        headers = {
            "User-Agent":       "FulcrumGlassbox/2.0",
            "Accept":           "application/json",
            "Accept-Encoding":  "gzip",
        }
        try:
            async with aiohttp.ClientSession(timeout=timeout, headers=headers) as s:
                async with s.get(self.DIGITRAFFIC_VESSELS_URL) as r:
                    r.raise_for_status()
                    data = await r.json()
        except Exception as e:
            self.log.info(f"[ships.static] /v1/vessels fetch failed: {e}")
            return 0

        if not isinstance(data, list):
            self.log.warning(
                f"[ships.static] /v1/vessels returned non-list ({type(data).__name__})"
            )
            return 0

        new_cache: Dict[str, Dict[str, Any]] = {}
        for v in data:
            mmsi = v.get("mmsi")
            if mmsi is None:
                continue
            new_cache[str(mmsi)] = v

        self._digitraffic_static = new_cache
        self._digitraffic_static_last_refresh = time.time()
        self.log.info(
            f"[ships.static] refreshed Digitraffic static-info cache: "
            f"{len(new_cache):,} vessels (name + IMO + callsign + ship_type)"
        )
        return len(new_cache)

    def _merge_static_info(self, position_row: Dict[str, Any]) -> None:
        """Augment a position record with cached name/IMO/callsign/shipType.
        Mutates position_row in place. No-op if MMSI not in cache."""
        mmsi = str(position_row.get("mmsi") or "")
        if not mmsi:
            return
        static = self._digitraffic_static.get(mmsi)
        if not static:
            return
        # Only fill in fields that are missing from the position record
        # (defensive: the position source might already carry name in rare cases)
        if not position_row.get("name"):
            n = static.get("name")
            if isinstance(n, str) and n.strip():
                position_row["name"] = n.strip()
        if not position_row.get("imo"):
            imo = static.get("imo")
            if isinstance(imo, int) and imo > 0:
                position_row["imo"] = imo
        if not position_row.get("callsign"):
            cs = static.get("callSign")  # camelCase from upstream
            if isinstance(cs, str) and cs.strip():
                position_row["callsign"] = cs.strip()
        if not position_row.get("ship_type"):
            st = static.get("shipType")
            if isinstance(st, int):
                position_row["ship_type"] = st
        # Optional but useful: declared destination + draught (per-voyage)
        if not position_row.get("destination"):
            d = static.get("destination")
            if isinstance(d, str) and d.strip() and d.strip().upper() != "UNKNOWN":
                position_row["destination"] = d.strip()
        if not position_row.get("draught"):
            dr = static.get("draught")
            if isinstance(dr, int) and dr > 0:
                # Digitraffic encodes draught in decimeters (e.g. 118 = 11.8m)
                position_row["draught"] = dr / 10.0

    # ─── multi-source fetch ────────────────────────────────────────────

    async def fetch(self) -> List[Dict[str, Any]]:
        """Union of all configured upstream feeds, deduped by MMSI."""
        import asyncio
        rows: List[Dict[str, Any]] = []
        seen: set = set()

        # Run the three upstream fetches in parallel — bounded by the slowest.
        tasks = [self._fetch_digitraffic(), self._fetch_barentswatch(), self._fetch_dma()]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for res in results:
            if isinstance(res, Exception):
                self.log.info(f"ship feed failed: {res}")
                continue
            for r in (res or []):
                mmsi = str(r.get("mmsi") or "")
                if not mmsi or mmsi in seen:
                    continue
                seen.add(mmsi)
                # Annotate dark-ship heuristic once (renderer reads _dark).
                if _suspicious(r):
                    r["_dark"] = True
                rows.append(r)

        if not rows:
            # No synthetic waypoints — v121 removed _global_fallback() because
            # the 48 static ghosts polluted the client dedup cache indefinitely
            # and looked real to users. Empty is more honest than fake.
            self.log.warning(
                "all 3 AIS upstream feeds returned empty — "
                "ships layer will be bare this cycle"
            )
        return rows

    # ─── per-source fetchers ───────────────────────────────────────────

    async def _fetch_digitraffic(self) -> List[Dict[str, Any]]:
        import aiohttp
        out: List[Dict[str, Any]] = []
        # Refresh the static-info cache if it's stale (or never populated).
        # First call (cache empty) blocks ~1-2s for ~18K vessel records;
        # subsequent calls within the refresh window are cheap.
        if (
            not self._digitraffic_static
            or (time.time() - self._digitraffic_static_last_refresh)
                > self.STATIC_INFO_REFRESH_SEC
        ):
            await self._refresh_digitraffic_static_info()

        timeout = aiohttp.ClientTimeout(total=15)
        # Same gzip requirement as /v1/vessels — see _refresh_digitraffic_static_info.
        headers = {
            "User-Agent":      "FulcrumGlassbox/2.0",
            "Accept":          "application/json",
            "Accept-Encoding": "gzip",
        }
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as s:
            async with s.get(self.DIGITRAFFIC_URL) as r:
                r.raise_for_status()
                data = await r.json()
        features = (data.get("features") or []) if isinstance(data, dict) else []
        for f in features:
            props = f.get("properties") or {}
            geom = f.get("geometry") or {}
            coords = geom.get("coordinates") or []
            if len(coords) < 2:
                continue
            mmsi = props.get("mmsi") or f.get("mmsi")
            if not mmsi:
                continue
            # Digitraffic /v1/locations is the position firehose (AIS Types 1/2/3)
            # which does NOT carry vessel name/IMO/callsign — those live on AIS
            # Type-5 broadcasts surfaced via /v1/vessels. We merge from the
            # static-info cache (refreshed every STATIC_INFO_REFRESH_SEC).
            row = {
                "mmsi": str(mmsi),
                "lat": float(coords[1]),
                "lng": float(coords[0]),
                "sog": props.get("sog"),
                "cog": props.get("cog"),
                "heading": props.get("heading"),
                "ship_type": props.get("shipType") or props.get("type"),
                "name": None,        # filled by static-info merge if cached
                "_source": "digitraffic",
            }
            self._merge_static_info(row)
            out.append(row)
        return out

    async def _fetch_barentswatch(self) -> List[Dict[str, Any]]:
        import aiohttp
        out: List[Dict[str, Any]] = []
        timeout = aiohttp.ClientTimeout(total=15)
        headers = {"User-Agent": "FulcrumGlassbox/2.0", "Accept": "application/json"}
        try:
            async with aiohttp.ClientSession(timeout=timeout, headers=headers) as s:
                async with s.get(self.BARENTSWATCH_URL) as r:
                    if r.status != 200:
                        return out
                    data = await r.json()
        except Exception:
            return out
        # BarentsWatch returns a list of ship dicts OR a GeoJSON feature collection
        items = data if isinstance(data, list) else (data.get("features") or [])
        for item in items:
            if isinstance(item, dict) and "geometry" in item:
                coords = (item.get("geometry") or {}).get("coordinates") or []
                props = item.get("properties") or {}
                if len(coords) < 2:
                    continue
                lng, lat = float(coords[0]), float(coords[1])
                mmsi = props.get("mmsi")
                name = props.get("name") or props.get("shipName")
                ship_type = props.get("shipType") or props.get("aisShipType")
                sog = props.get("speedOverGround") or props.get("sog")
                cog = props.get("courseOverGround") or props.get("cog")
            else:
                lat = item.get("latitude") or item.get("lat")
                lng = item.get("longitude") or item.get("lon") or item.get("lng")
                mmsi = item.get("mmsi")
                name = item.get("name") or item.get("shipName")
                ship_type = item.get("shipType") or item.get("aisShipType")
                sog = item.get("speedOverGround") or item.get("sog")
                cog = item.get("courseOverGround") or item.get("cog")
            if not mmsi or lat is None or lng is None:
                continue
            out.append({
                "mmsi": str(mmsi), "lat": float(lat), "lng": float(lng),
                "sog": sog, "cog": cog, "heading": cog,
                "ship_type": ship_type, "name": name,
                "_source": "barentswatch",
            })
        return out

    async def _fetch_dma(self) -> List[Dict[str, Any]]:
        import aiohttp
        out: List[Dict[str, Any]] = []
        timeout = aiohttp.ClientTimeout(total=15)
        headers = {"User-Agent": "FulcrumGlassbox/2.0"}
        try:
            async with aiohttp.ClientSession(timeout=timeout, headers=headers) as s:
                async with s.get(self.DMA_URL) as r:
                    if r.status != 200:
                        return out
                    data = await r.json()
        except Exception:
            return out
        features = (data.get("features") or []) if isinstance(data, dict) else []
        for f in features:
            props = f.get("properties") or {}
            geom = f.get("geometry") or {}
            coords = geom.get("coordinates") or []
            if len(coords) < 2:
                continue
            mmsi = props.get("mmsi")
            if not mmsi:
                continue
            out.append({
                "mmsi": str(mmsi), "lat": float(coords[1]), "lng": float(coords[0]),
                "sog": props.get("sog"), "cog": props.get("cog"),
                "heading": props.get("heading"),
                "ship_type": props.get("shipType") or props.get("type"),
                "name": props.get("name"),
                "_source": "dma",
            })
        return out

    # ─── normalize ─────────────────────────────────────────────────────

    def normalize(self, raw_items: List[Dict[str, Any]]) -> List[GlassboxEvent]:
        now = datetime.now(timezone.utc).isoformat()
        out: List[GlassboxEvent] = []
        for r in raw_items:
            mmsi = r.get("mmsi")
            if not mmsi:
                continue
            sev = _severity(r.get("ship_type") or 0)
            if r.get("_dark"):
                sev = max(sev, 7)    # dark-ship bump
            payload: Dict[str, Any] = {
                "mmsi":       mmsi,
                "name":       r.get("name"),
                "ship_type":  r.get("ship_type"),
                "cog":        r.get("cog"),
                "dark":       bool(r.get("_dark")),
            }
            # Optional fields populated by the static-info merge (Digitraffic
            # /v1/vessels). Cross-domain matching against OFAC SDN keys off
            # name + IMO; these are what unblock that signal.
            for key in ("imo", "callsign", "destination", "draught"):
                if r.get(key) is not None:
                    payload[key] = r[key]
            out.append(GlassboxEvent(
                layer=self.layer,
                external_id=str(mmsi),
                kind="position",
                lat=float(r["lat"]),
                lng=float(r["lng"]),
                ts=now,
                severity=sev,
                heading_deg=float(r["heading"]) if isinstance(r.get("heading"), (int, float)) else None,
                velocity_ms=(float(r["sog"]) * 0.514444) if isinstance(r.get("sog"), (int, float)) else None,
                source=r.get("_source", self.source),
                payload=payload,
            ))
        return out
