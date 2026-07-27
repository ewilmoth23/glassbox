"""
Earthquakes ingester — USGS real-time feeds.

Source: https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/
We poll the "all_hour" feed every 60 seconds. Anything magnitude ≥ 2.5 is
worth emitting; ≥ 5.0 is severity 7; ≥ 6.0 is severity 9; ≥ 7.0 severity 10.

Dedup key is the USGS event id (stable across updates). If USGS revises a
magnitude (happens for large quakes), the content_hash changes and the
cycle re-broadcasts — client just updates the pulse.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List

from .base import GlassboxEvent, Ingester


_USGS_FEEDS = {
    "hour": "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/all_hour.geojson",
    "day":  "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/2.5_day.geojson",
    "week": "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/significant_week.geojson",
}


def _severity(mag: float) -> int:
    if mag >= 7.5: return 10
    if mag >= 7.0: return 9
    if mag >= 6.0: return 8
    if mag >= 5.5: return 7
    if mag >= 5.0: return 6
    if mag >= 4.5: return 5
    if mag >= 4.0: return 4
    if mag >= 3.0: return 2
    return 1


class EarthquakesIngester(Ingester):
    layer = "earthquakes"
    source = "USGS Earthquake Hazards Program"
    source_id = "usgs_earthquakes"      # gates against sources.yaml
    poll_interval_sec = 60.0
    MIN_MAGNITUDE = 2.5

    async def fetch(self) -> List[Dict[str, Any]]:
        import aiohttp  # lazy
        timeout = aiohttp.ClientTimeout(total=15)
        all_rows: List[Dict[str, Any]] = []
        seen: set = set()
        # Two feeds — hour (high freshness) + day (wider coverage for mid-mag)
        for feed_key in ("hour", "day"):
            url = _USGS_FEEDS[feed_key]
            try:
                async with aiohttp.ClientSession(timeout=timeout) as s:
                    async with s.get(url, headers={"User-Agent": "FulcrumGlassbox/2.0"}) as r:
                        r.raise_for_status()
                        data = await r.json()
            except Exception as e:
                self.log.info(f"USGS {feed_key} feed failed: {e}")
                continue
            for feat in (data.get("features") or []):
                try:
                    eq_id = feat.get("id")
                    if not eq_id or eq_id in seen:
                        continue
                    seen.add(eq_id)
                    geom = feat.get("geometry") or {}
                    coords = geom.get("coordinates") or []
                    if len(coords) < 2:
                        continue
                    lng, lat = float(coords[0]), float(coords[1])
                    depth_km = float(coords[2]) if len(coords) >= 3 else 0.0
                    props = feat.get("properties") or {}
                    mag = float(props.get("mag") or 0)
                    if mag < self.MIN_MAGNITUDE:
                        continue
                    all_rows.append({
                        "id": eq_id, "mag": mag, "lat": lat, "lng": lng,
                        "depth_km": depth_km,
                        "place": props.get("place") or "",
                        "time_ms": int(props.get("time") or 0),
                        "url": props.get("url") or "",
                        "title": props.get("title") or "",
                        "tsunami": int(props.get("tsunami") or 0),
                        "alert": props.get("alert"),
                    })
                except Exception as e:
                    self.log.debug(f"skip quake: {e}")
        return all_rows

    def normalize(self, raw_items: List[Dict[str, Any]]) -> List[GlassboxEvent]:
        now = datetime.now(timezone.utc).isoformat()
        out: List[GlassboxEvent] = []
        for r in raw_items:
            mag = float(r.get("mag") or 0)
            # Severity boosts: tsunami adds +1, USGS alert yellow/orange/red ramps
            sev = _severity(mag)
            if r.get("tsunami"):
                sev = min(10, sev + 1)
            alert = (r.get("alert") or "").lower()
            if alert == "yellow":  sev = max(sev, 7)
            elif alert == "orange": sev = max(sev, 8)
            elif alert == "red":    sev = max(sev, 10)

            ev_ts = now
            if r.get("time_ms"):
                try:
                    ev_ts = datetime.fromtimestamp(r["time_ms"] / 1000, tz=timezone.utc).isoformat()
                except Exception:
                    pass

            # ─── Pre-classification (Loop Step 3) ─────────────────
            # Earthquakes are unambiguously geo + city-precision (USGS bins to
            # ~few km). For high-magnitude or tsunami-flagged events, tag
            # likely prediction-market relevance. The classifier respects
            # whatever we set here (heuristic path, no Ollama call wasted).
            market_tags: List[str] = []
            sev_market = 0
            if mag >= 6.0 or r.get("tsunami") or alert in ("orange", "red"):
                # Major event — tag generic disaster markets. The Loop will
                # cross-reference against actual Kalshi/Polymarket listings.
                market_tags.append(f"earthquake:M{mag:.1f}")
                if r.get("tsunami"):
                    market_tags.append("kalshi:tsunami")
                # severity_for_market mirrors our internal severity for major events
                sev_market = sev

            out.append(GlassboxEvent(
                layer=self.layer,
                external_id=str(r["id"]),
                kind="alert",
                lat=float(r["lat"]),
                lng=float(r["lng"]),
                ts=ev_ts,
                severity=sev,
                altitude_m=-1000.0 * float(r.get("depth_km") or 0),  # negative = depth
                source=self.source,
                payload={
                    "mag": mag,
                    "place": r.get("place"),
                    "title": r.get("title"),
                    "depth_km": r.get("depth_km"),
                    "url": r.get("url"),
                    "tsunami": bool(r.get("tsunami")),
                    "alert": r.get("alert"),
                },
                # Loop classification — ingester pre-fills what it knows
                domain="geo",
                geocode_quality="city",
                decay_half_life_min=60,
                market_tags=market_tags,
                severity_for_market=sev_market,
            ))
        return out
