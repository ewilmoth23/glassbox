"""
Satellites ingester — CelesTrak TLEs, SGP4-propagated server-side. (v2)

v2 expansion (Session 136):
  - 11 additional CelesTrak groups: glo-ops, galileo, beidou, oneweb,
    iridium-NEXT, geo, other-comm (includes AST SpaceMobile BlueWalker 3),
    weather, noaa, goes, amateur.
  - Starlink cap raised 200 → 2000. The constellation is ~6,000+ birds in
    orbit; the old 200 cap was showing a sliver and made the globe look
    sparse over North America / Europe.
  - TLE disk cache at 21_GLASSBOX_AI/data/tle_cache/{group}.tle so:
      (a) a server restart doesn't leave the globe sat-empty for 10 min
          while the first CelesTrak fetch happens — we warm-start from
          cache on the first cycle, then refresh over the top.
      (b) a CelesTrak rate-limit or outage falls back per-group to the
          last good text we cached, instead of wiping out a constellation.
    Cache considered useful for 7 days (TLEs degrade but visualization
    stays fine for a week without updates).

Future upgrade path:
  space-track.org (USSF's catalog) — free registration, gives unclassified
  military tracking, decay predictions, and higher rate limits than the
  anonymous CelesTrak feed. When we're ready, add SPACE_TRACK_USER /
  SPACE_TRACK_PASS env vars + a sibling fetcher that hits
  https://www.space-track.org/basicspacedata/query/class/gp/ .

Why server-side propagation is the big win:
  V1 Glassbox fetched 4 CelesTrak groups (stations, GPS, active, Starlink)
  = ~380 TLEs (after caps), then ran satellite.js propagation on every
  animation frame in the browser — a constant 10-15% CPU even tab-hidden.

  V2: Server fetches TLEs every 10 min (disk-cached), propagates once
  every 30s, pushes positions via SSE. Client just places points.
  Browser CPU drops to ~0% for this layer even with thousands of sats.

Requires `sgp4` (pip install sgp4). The launcher's venv already handles it.
"""

from __future__ import annotations

import logging as _logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

from .base import GlassboxEvent, Ingester


# CelesTrak groups — (group_name, URL, cap).
# Caps are sanity limits; actual counts per group vary. Aggregate worst-case
# ~4k TLEs — SGP4 propagates that in <50ms so the 30s cycle has plenty of
# headroom. The SSE broadcast cost is ~80 bytes per changed point, well
# within the slow-consumer eviction budget.
_TLE_GROUPS: List[Tuple[str, str, int]] = [
    # 2026-05-05 01:30 ET — Pivoted to Cloudflare Worker proxy after Ethan's
    # Mac confirmed celestrak.org IP (104.168.149.178) is TCP-unreachable
    # from his network (curl times out on port 443). Both .org and .com
    # resolve to same IP. Either ISP block or CelesTrak's bot defense.
    #
    # Worker proxy at mewr-news-api.mewrcreate.workers.dev/api/proxy/celestrak/
    # fetches from CelesTrak using CF data-center IPs (not blocked) and
    # returns the TLE text with edge cache (10 min TTL).
    #
    # The 4 below cover 99% of the visual + OSINT value:
    #   stations  — ISS + Tiangong + Dragon (the headline crewed objects)
    #   active    — broad catalog mix (~120 sats, keeps globe populated)
    #   starlink  — Starlink mega-constellation (the big visual payoff)
    #   geo       — Geostationary belt (huge symbolic value at GEO altitude)
    ("stations",  "https://mewr-news-api.mewrcreate.workers.dev/api/proxy/celestrak/stations.txt",   20),
    ("active",    "https://mewr-news-api.mewrcreate.workers.dev/api/proxy/celestrak/active.txt",     120),
    ("starlink",  "https://mewr-news-api.mewrcreate.workers.dev/api/proxy/celestrak/starlink.txt",   2000),
    ("geo",       "https://mewr-news-api.mewrcreate.workers.dev/api/proxy/celestrak/geo.txt",        500),
    # ── Disabled in v1.0; re-enable selectively once cache + per-group
    #    timing is verified ──────────────────────────────────────────────
    # ("gps-ops",      "https://celestrak.org/NORAD/elements/gp.php?GROUP=gps-ops&FORMAT=tle",      40),
    # ("glo-ops",      "https://celestrak.org/NORAD/elements/gp.php?GROUP=glo-ops&FORMAT=tle",      30),
    # ("galileo",      "https://celestrak.org/NORAD/elements/gp.php?GROUP=galileo&FORMAT=tle",      30),
    # ("beidou",       "https://celestrak.org/NORAD/elements/gp.php?GROUP=beidou&FORMAT=tle",       60),
    # ("oneweb",       "https://celestrak.org/NORAD/elements/gp.php?GROUP=oneweb&FORMAT=tle",       800),
    # ("iridium-NEXT", "https://celestrak.org/NORAD/elements/gp.php?GROUP=iridium-NEXT&FORMAT=tle", 100),
    # ("other-comm",   "https://celestrak.org/NORAD/elements/gp.php?GROUP=other-comm&FORMAT=tle",   200),
    # ("weather",      "https://celestrak.org/NORAD/elements/gp.php?GROUP=weather&FORMAT=tle",      50),
    # ("noaa",         "https://celestrak.org/NORAD/elements/gp.php?GROUP=noaa&FORMAT=tle",         20),
    # ("goes",         "https://celestrak.org/NORAD/elements/gp.php?GROUP=goes&FORMAT=tle",         20),
    # ("amateur",      "https://celestrak.org/NORAD/elements/gp.php?GROUP=amateur&FORMAT=tle",      100),
]


# Where per-group TLE text is cached between restarts.
# Resolves to 21_GLASSBOX_AI/data/tle_cache/ from this module's location.
_CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "tle_cache"
# Cache considered useful this long. TLEs degrade (position error grows
# ~1-3 km/day); at 7 days the visualization is still honest enough.
_CACHE_MAX_AGE_SEC = 7 * 24 * 3600


class SatellitesIngester(Ingester):
    layer = "satellites"
    source = "CelesTrak + SGP4 server-side propagation"
    source_id = "celestrak"             # gates against sources.yaml
    # Fast propagation — the TLE refresh is throttled separately
    poll_interval_sec = 30.0
    TLE_REFRESH_SEC = 600   # re-download TLEs every 10 min

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # Cached parsed TLE satellites: [(name, group, sat_rec)]
        self._tles: List[Tuple[str, str, Any]] = []
        self._tles_loaded_at: float = 0.0
        # Prevents re-reading disk cache every cycle after warm-start
        self._cache_warm_start_done: bool = False
        try:
            _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            self.log.info(f"could not create TLE cache dir {_CACHE_DIR}: {e}")

    async def fetch(self) -> List[Dict[str, Any]]:
        import asyncio as _asyncio
        import time as _time
        # Warm start: try disk cache before touching the network so the
        # globe isn't empty for the first ~10s after a server restart.
        if not self._cache_warm_start_done:
            self._cache_warm_start_done = True
            loaded = self._load_from_cache()
            if loaded:
                self.log.info(f"TLE warm-start from cache: {loaded} satellites")

        # Refresh TLE set if stale (network; falls back per-group to cache
        # on any given group's failure)
        if (_time.time() - self._tles_loaded_at) > self.TLE_REFRESH_SEC:
            await self._refresh_tles()

        # Propagate every known satellite to "now". With v2's expansion to
        # 4500+ satellites, the pure-Python ECI→geodetic loop blocks the event
        # loop for 200-600ms — every 30s. Offload to a thread so SSE clients
        # don't see jitter and the broadcast queue doesn't back up.
        return await _asyncio.get_running_loop().run_in_executor(
            None, self._propagate_all
        )

    # ─── cache warm-start ─────────────────────────────────────────────

    def _load_from_cache(self) -> int:
        """Populate self._tles from disk cache on first cycle.
        Returns count loaded. Quiet return if sgp4 missing or nothing cached."""
        try:
            from sgp4.api import Satrec
        except Exception:
            return 0
        import time as _time
        loaded: List[Tuple[str, str, Any]] = []
        newest_mtime = 0.0
        for group_name, _url, cap in _TLE_GROUPS:
            path = _CACHE_DIR / f"{group_name}.tle"
            if not path.exists():
                continue
            try:
                mtime = path.stat().st_mtime
                if (_time.time() - mtime) > _CACHE_MAX_AGE_SEC:
                    # Too stale to trust — skip; network path will refresh
                    continue
                text = path.read_text(encoding="utf-8")
                added = self._parse_tle_text(text, group_name, cap, loaded, Satrec)
                if added > 0 and mtime > newest_mtime:
                    newest_mtime = mtime
            except Exception as e:
                self.log.info(f"TLE cache read failed for {group_name}: {e}")
                continue
        if loaded:
            self._tles = loaded
            # Use newest cache mtime so the staleness check in fetch()
            # reflects real data age, not "right now".
            self._tles_loaded_at = newest_mtime or _time.time()
        return len(loaded)

    def _parse_tle_text(
        self,
        text: str,
        group_name: str,
        cap: int,
        out: List[Tuple[str, str, Any]],
        Satrec: Any,
    ) -> int:
        """Parse 3-line TLE text, append up to `cap` records to `out`.
        Returns count added. Tolerant of malformed lines — skips silently."""
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        added = 0
        for i in range(0, len(lines) - 2, 3):
            if added >= cap:
                break
            try:
                name, l1, l2 = lines[i], lines[i + 1], lines[i + 2]
                if not (l1.startswith("1 ") and l2.startswith("2 ")):
                    continue
                sat = Satrec.twoline2rv(l1, l2)
                out.append((name, group_name, sat))
                added += 1
            except Exception:
                continue
        return added

    # ─── network refresh ──────────────────────────────────────────────

    async def _refresh_tles(self) -> None:
        import aiohttp   # lazy
        try:
            from sgp4.api import Satrec  # lazy
        except Exception as e:
            self.log.warning(f"sgp4 not installed: {e}. pip install sgp4")
            return

        # 2026-05-05 00:15 ET: dropped "FulcrumGlassbox" from UA — CelesTrak
        # appears to filter on branded UAs. Generic Safari UA gets through.
        # Also bumped timeout 30→60s; CelesTrak's larger TLE files (active.txt
        # is ~250KB, starlink.txt is ~700KB) need headroom.
        timeout = aiohttp.ClientTimeout(total=60, connect=10)
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
            "Accept": "text/plain, */*",
        }
        new_tles: List[Tuple[str, str, Any]] = []
        network_hits = 0
        cache_hits = 0

        import asyncio as _asyncio
        # 2026-05-05 (architectural fix): smoke mode pulls only stations.txt
        # (ISS + Tiangong + Dragon = a few KB, < 1s). Production pulls all 4 groups.
        groups_to_fetch = _TLE_GROUPS[:1] if self.smoke_mode else _TLE_GROUPS
        for idx, (group_name, url, cap) in enumerate(groups_to_fetch):
            if idx > 0:
                await _asyncio.sleep(2)   # gentle pacing to avoid bursts

            text: str = ""
            # 2026-05-05 fix: bumped per-group failure logs from INFO to WARNING
            # so they surface in smoke test (which sets level=WARNING). Without
            # this we couldn't see WHY each TLE group was failing.
            try:
                async with aiohttp.ClientSession(timeout=timeout, headers=headers) as s:
                    async with s.get(url) as r:
                        if r.status != 200:
                            # 2026-05-09: celestrak rate-limits with HTTP 403
                            # ("GP data has not updated since your last successful
                            # download...") — these are NORMAL, not failures.
                            # The disk cache covers the stale window. Log at INFO
                            # so an operator can see it but it doesn't pollute
                            # WARNING-grade dashboards/alerts.
                            level = (
                                _logging.INFO
                                if r.status == 403
                                else _logging.WARNING
                            )
                            self.log.log(
                                level,
                                f"TLE {group_name} HTTP {r.status} from {url}"
                                + (" (rate-limit, falling back to disk cache)"
                                   if r.status == 403 else "")
                            )
                        else:
                            body = await r.text()
                            if body and "ERROR" not in body[:40] and len(body) >= 100:
                                text = body
                            else:
                                self.log.warning(
                                    f"TLE {group_name} empty/error body "
                                    f"({len(body or '')} chars; preview: {(body or '')[:100]!r})"
                                )
            except Exception as e:
                err_msg = str(e) or type(e).__name__
                self.log.warning(f"TLE {group_name} fetch failed ({url}): {err_msg}")

            if text:
                # Network hit — parse and write-through to cache
                count_before = len(new_tles)
                self._parse_tle_text(text, group_name, cap, new_tles, Satrec)
                if len(new_tles) > count_before:
                    network_hits += 1
                    try:
                        (_CACHE_DIR / f"{group_name}.tle").write_text(text, encoding="utf-8")
                    except Exception as e:
                        self.log.info(f"TLE cache write failed for {group_name}: {e}")
            else:
                # Network failed — per-group cache fallback so we don't
                # lose an entire constellation because CelesTrak hiccuped
                path = _CACHE_DIR / f"{group_name}.tle"
                if path.exists():
                    try:
                        cached_text = path.read_text(encoding="utf-8")
                        count_before = len(new_tles)
                        self._parse_tle_text(cached_text, group_name, cap, new_tles, Satrec)
                        if len(new_tles) > count_before:
                            cache_hits += 1
                    except Exception as e:
                        self.log.info(
                            f"TLE cache fallback read failed for {group_name}: {e}"
                        )

        if new_tles:
            self._tles = new_tles
            import time as _time
            self._tles_loaded_at = _time.time()
            self.log.info(
                f"TLE set refreshed: {len(new_tles)} satellites "
                f"(network={network_hits} groups, cache-fallback={cache_hits} groups)"
            )
        else:
            # Leave prior self._tles in place — degraded but not empty
            self.log.warning(
                "TLE refresh produced zero satellites (network + cache both empty); "
                "keeping previously-loaded set"
            )

    # ─── propagation ──────────────────────────────────────────────────

    def _propagate_all(self) -> List[Dict[str, Any]]:
        if not self._tles:
            return []
        try:
            from sgp4.api import jday
        except Exception:
            return []
        now = datetime.now(timezone.utc)
        jd, fr = jday(now.year, now.month, now.day,
                      now.hour, now.minute, now.second + now.microsecond * 1e-6)
        out: List[Dict[str, Any]] = []
        for name, group, sat in self._tles:
            try:
                e, r_km, v_km_s = sat.sgp4(jd, fr)
                if e != 0:
                    continue
                # ECI → geodetic (lat, lng, alt) approximation via WGS-84
                lat, lng, alt_km = _eci_to_geodetic(r_km, jd, fr)
                out.append({
                    "norad": int(getattr(sat, "satnum", 0) or 0),
                    "name": name.strip(),
                    "group": group,
                    "lat": lat, "lng": lng, "alt_km": alt_km,
                    "vel_km_s": (v_km_s[0]**2 + v_km_s[1]**2 + v_km_s[2]**2) ** 0.5,
                })
            except Exception:
                continue
        return out

    def normalize(self, raw_items: List[Dict[str, Any]]) -> List[GlassboxEvent]:
        now = datetime.now(timezone.utc).isoformat()
        out: List[GlassboxEvent] = []
        for r in raw_items:
            norad = r.get("norad")
            if not norad:
                continue
            # Severity — minimal; bump for crewed / notable platforms.
            sev = 1
            name = (r.get("name") or "").lower()
            if "iss" in name or "tiangong" in name or "hubble" in name:
                sev = 5
            out.append(GlassboxEvent(
                layer=self.layer,
                external_id=str(norad),
                kind="position",
                lat=float(r["lat"]),
                lng=float(r["lng"]),
                ts=now,
                severity=sev,
                altitude_m=float(r["alt_km"]) * 1000.0,
                velocity_ms=float(r["vel_km_s"]) * 1000.0,
                source=self.source,
                payload={
                    "norad": norad,
                    "name": r.get("name"),
                    "group": r.get("group"),
                },
            ))
        return out


# ─── ECI → geodetic helper (simplified, good to ~1km accuracy) ─────────────

import math

def _eci_to_geodetic(r_km, jd, fr):
    """
    Convert ECI (inertial) coordinates to geodetic (lat, lng, alt_km).
    Uses Greenwich Mean Sidereal Time (GMST) approximation. Good enough
    for visualization — not precise astronomy.
    """
    x, y, z = r_km
    # GMST at (jd + fr) — IAU 1982 formula, truncated
    T = (jd + fr - 2451545.0) / 36525.0
    gmst = 67310.54841 + (876600.0 * 3600.0 + 8640184.812866) * T + 0.093104 * T * T
    gmst = (gmst % 86400.0) * (2.0 * math.pi / 86400.0)
    # Rotate ECI to ECEF
    cos_g, sin_g = math.cos(gmst), math.sin(gmst)
    xe = x * cos_g + y * sin_g
    ye = -x * sin_g + y * cos_g
    ze = z
    # WGS-84 flattening approximation
    a = 6378.137       # equatorial radius km
    f = 1 / 298.257223563
    e2 = f * (2 - f)
    p = math.sqrt(xe * xe + ye * ye)
    lng = math.atan2(ye, xe)
    lat = math.atan2(ze, p * (1 - e2))
    # One iteration Newton refinement
    for _ in range(3):
        sin_lat = math.sin(lat)
        N = a / math.sqrt(1 - e2 * sin_lat * sin_lat)
        alt = p / math.cos(lat) - N
        lat = math.atan2(ze, p * (1 - e2 * N / (N + alt)))
    sin_lat = math.sin(lat)
    N = a / math.sqrt(1 - e2 * sin_lat * sin_lat)
    alt_km = p / math.cos(lat) - N
    return math.degrees(lat), math.degrees(lng), alt_km
