"""
NASA FIRMS ingester — active wildfire detections (MODIS + VIIRS satellites).

Source: https://firms.modaps.eosdis.nasa.gov/api/area/csv/{KEY}/{DATASET}/world/{DAYS}
License: US public domain (NASA), commercial use OK
Attribution: required ("Wildfires: NASA FIRMS (MODIS/VIIRS)")
KEY required: NASA_FIRMS_MAP_KEY env var.

Currently glassbox.html line ~7259 hits FIRMS directly with the key in
plain JS. This backend version takes load off the browser, makes the
key swappable via env var, and means clients consume a clean SSE stream.

Two FIRMS datasets we ingest:
  - VIIRS_SNPP_NRT  (375m resolution, near-real-time, 2-3h latency)
  - MODIS_NRT       (1km resolution, near-real-time, ~3h latency)

We pull WORLD/1 (last 24 hours globally) every 30 min. Each detection
has a brightness temperature (Kelvin) and confidence value. We map
to severity 4-9 based on brightness + confidence.

Rate limit: NASA FIRMS allows ~5,000 transactions/10min on Area API,
so 30 min cadence with ~2-3 datasets = trivially within budget.
"""

from __future__ import annotations

import csv
import io
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import aiohttp

from .base import GlassboxEvent, Ingester


# ─── Severity mapping ─────────────────────────────────────────────────────

def _severity_for_fire(brightness_k: Optional[float], confidence: Optional[str]) -> int:
    """Map FIRMS brightness + confidence to internal 0-10 severity."""
    base = 5
    if brightness_k is not None:
        if brightness_k >= 360:    # very hot — likely strong active fire
            base = 8
        elif brightness_k >= 340:
            base = 7
        elif brightness_k >= 320:
            base = 6
        elif brightness_k >= 300:
            base = 5
        else:
            base = 4

    # FIRMS VIIRS uses 'l'/'n'/'h' (low/nominal/high). MODIS uses 0-100.
    if confidence:
        c = str(confidence).lower()
        if c in ("h", "high") or (c.isdigit() and int(c) >= 80):
            return min(10, base + 1)
        if c in ("l", "low") or (c.isdigit() and int(c) < 30):
            return max(1, base - 2)
    return base


# ─── Ingester ─────────────────────────────────────────────────────────────


class NasaFirmsIngester(Ingester):
    layer = "wildfires"
    source = "NASA FIRMS (MODIS + VIIRS active fire detections)"
    source_id = "nasa_firms"             # gates against infra/sources.yaml
    poll_interval_sec = 1800.0           # 30 min

    # Datasets to pull (VIIRS first — newer + 375m resolution)
    DATASETS = ("VIIRS_SNPP_NRT", "MODIS_NRT")

    # ─── Build URL with the env-injected key ──────────────────────────
    BASE_URL = "https://firms.modaps.eosdis.nasa.gov/api/area/csv"
    UA = "FulcrumGlassbox/2.0 (mewrcreate.com/glassbox)"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # Look for env var; fallback to the known-good key if it's set in
        # a config block (for early v1.0 testing). In production this
        # MUST come from env or .env.glassbox file.
        # 2026-05-04 23:58 ET: prior key (77b5ef05...) was INVALID per smoke
        # test ("Invalid MAP_KEY" 400). Ethan re-registered + provided new key.
        self._key = (
            os.environ.get("NASA_FIRMS_MAP_KEY")
            or "9fb328cf4e0336cf6d58a66af420e9f3"   # registered key, see INFRASTRUCTURE.md
        )
        if not self._key:
            self.log.warning(
                "[nasa_firms] NASA_FIRMS_MAP_KEY not set — ingester will start "
                "but every fetch will return []. Register at "
                "https://firms.modaps.eosdis.nasa.gov/api/area/"
            )

    async def fetch(self) -> List[Dict[str, Any]]:
        if not self._key:
            return []
        results: List[Dict[str, Any]] = []
        timeout = aiohttp.ClientTimeout(total=60)
        headers = {"User-Agent": self.UA}

        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as s:
            for dataset in self.DATASETS:
                url = f"{self.BASE_URL}/{self._key}/{dataset}/world/1"
                try:
                    async with s.get(url) as r:
                        if r.status != 200:
                            self.log.warning(f"[nasa_firms] {dataset} HTTP {r.status} from {url}")
                            continue
                        text = await r.text()
                except Exception as e:
                    self.log.warning(f"[nasa_firms] {dataset} fetch failed: {e}")
                    continue

                # 2026-05-04 fix: surface what FIRMS actually returned. If
                # the body is short (header only) OR contains an error string,
                # log it explicitly so the operator can diagnose.
                if not text or len(text) < 100:
                    self.log.warning(
                        f"[nasa_firms] {dataset} returned {len(text or '')} bytes "
                        f"(header-only or empty); preview: {(text or '')[:200]!r}"
                    )
                    continue
                if text.lstrip().startswith(("<", "{", "[")):
                    # HTML/JSON instead of CSV = error response
                    self.log.warning(
                        f"[nasa_firms] {dataset} returned non-CSV content "
                        f"(probably error). Preview: {text[:200]!r}"
                    )
                    continue

                # FIRMS returns CSV with a header row. The world/1 dataset
                # for VIIRS_SNPP_NRT typically has 200-2000 rows in 24h.
                reader = csv.DictReader(io.StringIO(text))
                row_count = 0
                for row in reader:
                    row["_dataset"] = dataset
                    results.append(row)
                    row_count += 1
                self.log.info(f"[nasa_firms] {dataset}: parsed {row_count} fire detections")
        return results

    def normalize(self, raw_items: List[Dict[str, Any]]) -> List[GlassboxEvent]:
        now = datetime.now(timezone.utc).isoformat()
        out: List[GlassboxEvent] = []
        seen: set = set()

        for r in raw_items:
            try:
                lat = float(r.get("latitude", "") or 0)
                lng = float(r.get("longitude", "") or 0)
            except (TypeError, ValueError):
                continue
            if lat == 0 and lng == 0:
                continue

            # Build a stable external_id: dataset + lat + lng + acq_date + acq_time
            acq_date = r.get("acq_date", "")
            acq_time = r.get("acq_time", "")
            ext_id = f"{r.get('_dataset','firms')}:{lat:.4f}:{lng:.4f}:{acq_date}:{acq_time}"
            if ext_id in seen:
                continue
            seen.add(ext_id)

            try:
                brightness = float(r.get("bright_ti4") or r.get("brightness") or 0) or None
            except (TypeError, ValueError):
                brightness = None
            confidence = r.get("confidence")
            severity = _severity_for_fire(brightness, confidence)

            mtags: List[str] = []
            sev_market = 0
            if severity >= 7:
                mtags.append("weather:wildfire")
                sev_market = 5

            ts_iso = now
            if acq_date and acq_time:
                # FIRMS acq_time is HHMM (e.g., '1432'). Build ISO8601.
                try:
                    h = int(acq_time[:-2]) if len(acq_time) >= 3 else 0
                    m = int(acq_time[-2:]) if len(acq_time) >= 2 else 0
                    ts_iso = f"{acq_date}T{h:02d}:{m:02d}:00Z"
                except (ValueError, TypeError):
                    pass

            out.append(GlassboxEvent(
                layer=self.layer,
                external_id=ext_id,
                kind="event",
                lat=lat,
                lng=lng,
                ts=ts_iso,
                severity=severity,
                source=self.source,
                payload={
                    "dataset":      r.get("_dataset"),
                    "brightness_k": brightness,
                    "confidence":   confidence,
                    "frp_mw":       _safe_float(r.get("frp")),     # fire radiative power
                    "satellite":    r.get("satellite"),
                    "instrument":   r.get("instrument"),
                    "daynight":     r.get("daynight"),
                    "_attribution": "Wildfires: NASA FIRMS (MODIS/VIIRS)",
                },
                domain="geo",
                geocode_quality="exact",       # satellite-derived GPS position
                decay_half_life_min=120,       # 2h — FIRMS detections age out fast
                market_tags=mtags,
                severity_for_market=sev_market,
            ))

        return out


def _safe_float(v: Any) -> Optional[float]:
    try:
        if v is None or v == "":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None
