"""
NOAA SWPC ingester — space-weather alerts (geomagnetic, radio, radiation).

Source: https://services.swpc.noaa.gov/products/alerts.json
License: US Government public domain (NOAA SWPC, 17 USC §105). Commercial-OK.
NO API key required.

Strategic context: solar storms hit infrastructure (GPS accuracy, HF radio,
power-grid stability, satellite operations) hours before mainstream news
catches up. The SWPC alerts feed surfaces them in real time:
  - K-index alerts (geomagnetic disturbance) — affects power grids + GPS
  - G-storm alerts (geomagnetic storm scale G1–G5) — same family, escalation
  - R-flare alerts (radio blackouts on the dayside) — HF/SAR comms
  - S-radiation alerts (solar particle events) — satellite + polar aviation

Each alert's `product_id` encodes type + level + alert kind. We parse it,
map to a Glassbox severity 0–10, and anchor each alert geographically near
its primary impact region:
  - K-index / G-storm → Arctic Circle (60°N) — auroral oval, high-lat impact
  - R-flare         → equator (0°)         — peak dayside ionosphere
  - S-radiation     → polar cap (85°N)     — particles enter via field lines

The geographic anchor is approximate (space weather is global) but lets the
viewport bbox query surface these without special-casing.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import aiohttp

from .base import GlassboxEvent, Ingester


# product_id format examples (verified against live data 2026-05-08):
#   "K04W"  = K-index 4 Warning            (geomagnetic, high-lat)
#   "K05A"  = K-index 5 Alert              (geomagnetic)
#   "GSTW"  = G-storm Watch                (geomagnetic, generic)
#   "R02A"  = R-flare 2 Alert              (radio blackout)
#   "S01A"  = S-radiation 1 Alert          (solar radiation)
#   "WARK04"/"ALTK05"/"SUMK05" — alternate prefix forms in `message`
_PRODUCT_RE = re.compile(r"^(?P<kind>[KGRS])(?P<level>\d{1,2})(?P<alert>[WASP])$")


# Anchor coordinates per alert family (lat, lng).
_ANCHORS: Dict[str, Tuple[float, float]] = {
    "K": (60.0, 0.0),    # Arctic Circle — auroral oval
    "G": (60.0, 0.0),    # geomagnetic-storm scale; same family as K
    "R": (0.0, 0.0),     # radio blackouts peak on equatorial dayside ionosphere
    "S": (85.0, 0.0),    # solar radiation events enter polar caps
}

_KIND_LABEL: Dict[str, str] = {
    "K": "geomagnetic_kindex",
    "G": "geomagnetic_storm",
    "R": "radio_blackout",
    "S": "solar_radiation",
}

_ALERT_LABEL: Dict[str, str] = {
    "W": "warning",
    "A": "alert",
    "S": "summary",
    "P": "predicted",
}


def _parse_product_id(product_id: str) -> Optional[Dict[str, Any]]:
    """Parse a SWPC product_id like 'K04W' into structured fields.
    Returns None for product_ids we don't categorize (forecasts, advisories
    that don't match the K/G/R/S family — fine to skip for v1.0)."""
    m = _PRODUCT_RE.match(product_id.strip())
    if not m:
        return None
    kind = m.group("kind")
    try:
        level = int(m.group("level"))
    except (TypeError, ValueError):
        return None
    alert = m.group("alert")
    return {
        "kind": kind,
        "level": level,
        "alert": alert,
        "kind_label": _KIND_LABEL.get(kind, "space_weather"),
        "alert_label": _ALERT_LABEL.get(alert, alert.lower()),
    }


def _severity_from_level(kind: str, level: int) -> int:
    """Map SWPC level 1–5 (or K-index 1–9) to Glassbox severity 0–10.
    Higher impact = higher number."""
    if kind == "K":
        # K-index 0–9 — only K4+ tends to alert, K7+ is severe.
        if level <= 3:
            return 1
        if level == 4:
            return 3
        if level == 5:
            return 5
        if level == 6:
            return 7
        if level == 7:
            return 8
        if level == 8:
            return 9
        return 10  # K9
    # G/R/S scales are 1–5
    return min(10, max(1, level * 2))


def _parse_swpc_ts(ts_str: str) -> str:
    """SWPC issue_datetime format: '2026-05-07 16:37:40.453' (no tz, UTC implied)."""
    try:
        ts_str = ts_str.replace(" ", "T", 1)
        if "." in ts_str:
            head, frac = ts_str.split(".", 1)
            frac = (frac + "000000")[:6]
            ts_str = f"{head}.{frac}"
        dt = datetime.fromisoformat(ts_str).replace(tzinfo=timezone.utc)
        return dt.isoformat()
    except Exception:
        return datetime.now(timezone.utc).isoformat()


def _short_headline(message: str, max_len: int = 140) -> str:
    """Pull the first meaningful line from the SWPC message body."""
    if not message:
        return ""
    # Skip leading boilerplate ("Space Weather Message Code:", "Serial Number:",
    # "Issue Time:") and find the first WARNING/ALERT/SUMMARY line.
    for raw_line in message.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith(("WARNING:", "ALERT:", "SUMMARY:", "WATCH:", "PREDICTED:")):
            return line[:max_len]
    # Fallback: first non-empty line
    for raw_line in message.splitlines():
        line = raw_line.strip()
        if line and not line.startswith((
            "Space Weather Message Code", "Serial Number", "Issue Time",
        )):
            return line[:max_len]
    return ""


# ─── Ingester ─────────────────────────────────────────────────────────────


class NoaaSwpcIngester(Ingester):
    layer = "space_weather"
    source = "NOAA Space Weather Prediction Center"
    source_id = "noaa_swpc"
    poll_interval_sec = 300.0   # 5 min — SWPC posts new alerts as conditions change

    URL = "https://services.swpc.noaa.gov/products/alerts.json"
    UA = "FulcrumGlassbox/2.0 (mewrcreate.com/glassbox; hello@mewrcreate.com)"

    async def fetch(self) -> List[Dict[str, Any]]:
        timeout = aiohttp.ClientTimeout(total=20)
        headers = {"User-Agent": self.UA, "Accept": "application/json"}
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as s:
            async with s.get(self.URL) as r:
                r.raise_for_status()
                data = await r.json()
        if not isinstance(data, list):
            self.log.warning(
                f"[noaa_swpc] expected JSON list, got {type(data).__name__}"
            )
            return []
        return data

    def normalize(self, raw_items: List[Dict[str, Any]]) -> List[GlassboxEvent]:
        out: List[GlassboxEvent] = []

        for r in raw_items:
            product_id = (r.get("product_id") or "").strip()
            if not product_id:
                continue
            parsed = _parse_product_id(product_id)
            if not parsed:
                # Skip forecast / outlook / non-K/G/R/S alerts for v1.0.
                continue

            kind = parsed["kind"]
            level = parsed["level"]
            severity = _severity_from_level(kind, level)
            anchor = _ANCHORS.get(kind, (0.0, 0.0))
            ts_iso = _parse_swpc_ts(r.get("issue_datetime") or "")
            message = r.get("message") or ""
            headline = _short_headline(message)

            # External ID stable across polls: product_id is unique per alert
            # event (NOAA doesn't reuse codes within a 12h window). Belt-and-
            # suspenders: append issue_datetime so re-issued same-code alerts
            # are distinct event rows.
            ext_id = f"swpc:{product_id}:{r.get('issue_datetime', '')}"

            out.append(GlassboxEvent(
                layer=self.layer,
                external_id=ext_id,
                kind="swpc_alert",
                lat=float(anchor[0]),
                lng=float(anchor[1]),
                ts=ts_iso,
                severity=severity,
                source=self.source,
                payload={
                    "product_id":   product_id,
                    "kind":         parsed["kind_label"],
                    "alert_kind":   parsed["alert_label"],
                    "level":        level,
                    "headline":     headline,
                    "message":      message[:2000],   # cap to keep payload sane
                    "_attribution": "NOAA Space Weather Prediction Center",
                },
                domain="atmospheric",
                geocode_quality="anchor_only",   # space weather is global; anchor is approx
                # Decay: K-index/G-storm alerts last 6–24h; R-flares minutes; S
                # events hours-to-days. Use 12h as a sensible default that the
                # proximity scan can use.
                decay_half_life_min=720,
                market_tags=[],
                severity_for_market=0,
            ))

        return out
