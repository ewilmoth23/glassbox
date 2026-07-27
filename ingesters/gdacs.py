"""
GDACS Global Disaster Alert and Coordination System ingester.

Source: https://www.gdacs.org/xml/rss.xml
License: European Union, CC BY 4.0 — commercial use OK with attribution.
NO API key required.

Strategic value: GDACS aggregates global natural disaster alerts with
QUANTITATIVE alert levels (Green/Orange/Red) and impact estimates
(affected population by intensity zone). Other ingesters in this stack
(USGS, EONET, NWS) provide raw event detection but not the
"how-bad-is-it" classification GDACS provides.

Coverage: Earthquakes (EQ), Volcanoes (VO), Floods (FL), Tropical Cyclones
(TC), Droughts (DR), Wildfires (WF). Global, near-real-time
(~15min lag from primary sources).

Lead time: Variable — earthquakes are simultaneous with USGS, but TC and
flood alerts often lead local-government advisories by hours-to-days.

Attribution required (commercial use): "Sourced from GDACS, European
Commission Joint Research Centre, CC BY 4.0".
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Dict, List, Optional

import aiohttp

from .base import GlassboxEvent, Ingester


# GDACS event type → human label
_EVENT_TYPE_LABEL = {
    "EQ": "earthquake",
    "VO": "volcano",
    "FL": "flood",
    "TC": "tropical_cyclone",
    "DR": "drought",
    "WF": "wildfire",
}


# GDACS alert level → Glassbox severity (0-10 scale)
_ALERT_TO_SEVERITY = {
    "green":  4,
    "orange": 7,
    "red":    9,
}


def _strip_ns(tag: str) -> str:
    return tag.split("}", 1)[1] if "}" in tag else tag


def _parse_rfc822(s: str) -> Optional[datetime]:
    if not s:
        return None
    try:
        dt = parsedate_to_datetime(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (TypeError, ValueError):
        return None


def _gdacs_field(item: ET.Element, local_name: str) -> Optional[str]:
    """Find a child by local-name (ignoring namespace). Returns text or None."""
    for child in item.iter():
        if _strip_ns(child.tag) == local_name:
            text = (child.text or "").strip()
            return text or None
    return None


def _gdacs_attr_field(
    item: ET.Element, local_name: str, attr: str,
) -> Optional[str]:
    """Find a child by local-name and return one of its attribute values."""
    for child in item.iter():
        if _strip_ns(child.tag) == local_name:
            val = child.attrib.get(attr)
            if val is not None and val != "":
                return val
    return None


class GdacsIngester(Ingester):
    layer = "gdacs"
    source = "GDACS Global Disaster Alert (CC BY 4.0)"
    source_id = "gdacs"
    poll_interval_sec = 600.0   # 10 min — GDACS updates ~15 min cycle

    URL = "https://www.gdacs.org/xml/rss.xml"
    UA = "FulcrumGlassbox/2.0 (mewrcreate.com/glassbox; hello@mewrcreate.com)"

    async def fetch(self) -> List[Dict[str, Any]]:
        timeout = aiohttp.ClientTimeout(total=30)
        headers = {"User-Agent": self.UA, "Accept": "application/xml"}
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as s:
            async with s.get(self.URL) as r:
                r.raise_for_status()
                xml_bytes = await r.read()

        try:
            root = ET.fromstring(xml_bytes)
        except ET.ParseError as e:
            self.log.warning(f"[gdacs] XML parse failed: {e}")
            return []

        out: List[Dict[str, Any]] = []
        # Walk <channel><item>...</item></channel>
        for item in root.iter():
            if _strip_ns(item.tag) != "item":
                continue

            event_type = _gdacs_field(item, "eventtype")
            event_id   = _gdacs_field(item, "eventid")
            if not event_type or not event_id:
                continue

            alert_level = (_gdacs_field(item, "alertlevel") or "").lower()
            try:
                alert_score = float(_gdacs_field(item, "alertscore") or 0)
            except ValueError:
                alert_score = 0

            # Coordinates: prefer <geo:Point><geo:lat/long>; fall back to
            # <georss:point>"lat lon" if present.
            lat = lng = None
            for child in item.iter():
                tag = _strip_ns(child.tag)
                if tag == "lat":
                    try:
                        lat = float((child.text or "").strip())
                    except ValueError:
                        pass
                elif tag == "long":
                    try:
                        lng = float((child.text or "").strip())
                    except ValueError:
                        pass
                if lat is not None and lng is not None:
                    break

            if lat is None or lng is None:
                continue

            from_date = _gdacs_field(item, "fromdate")
            to_date   = _gdacs_field(item, "todate")
            country   = _gdacs_field(item, "country")
            iso3      = _gdacs_field(item, "iso3")
            episode   = _gdacs_field(item, "episodeid")
            severity_raw    = _gdacs_attr_field(item, "severity", "value")
            severity_unit   = _gdacs_attr_field(item, "severity", "unit")
            population_raw  = _gdacs_attr_field(item, "population", "value")
            population_unit = _gdacs_attr_field(item, "population", "unit")
            title       = _gdacs_field(item, "title") or ""
            description = _gdacs_field(item, "description") or ""

            out.append({
                "event_id":       event_id,
                "episode_id":     episode,
                "event_type":     event_type,
                "alert_level":    alert_level,
                "alert_score":    alert_score,
                "lat":            lat,
                "lng":            lng,
                "from_date":      from_date,
                "to_date":        to_date,
                "country":        country,
                "iso3":           iso3,
                "severity_raw":   severity_raw,
                "severity_unit":  severity_unit,
                "population_raw": population_raw,
                "population_unit": population_unit,
                "title":          title,
                "description":    description,
            })
        return out

    def normalize(self, raw_items: List[Dict[str, Any]]) -> List[GlassboxEvent]:
        out: List[GlassboxEvent] = []
        for r in raw_items:
            event_type = r.get("event_type") or ""
            event_id   = r.get("event_id") or ""
            if not event_id:
                continue

            alert = (r.get("alert_level") or "").lower()
            severity = _ALERT_TO_SEVERITY.get(alert, 4)
            label = _EVENT_TYPE_LABEL.get(event_type, event_type.lower())

            # Pull a stable timestamp: prefer fromdate (event start) over now.
            ts_dt = _parse_rfc822(r.get("from_date") or "")
            ts_iso = (ts_dt or datetime.now(timezone.utc)).isoformat()

            payload: Dict[str, Any] = {
                "gdacs_event_id":  event_id,
                "gdacs_episode_id": r.get("episode_id"),
                "gdacs_event_type": event_type,
                "alert_level":     alert,
                "alert_score":     r.get("alert_score"),
                "country":         r.get("country"),
                "iso3":            r.get("iso3"),
                "from_date":       r.get("from_date"),
                "to_date":         r.get("to_date"),
                "title":           r.get("title"),
                "description":     r.get("description"),
                "_attribution":    "Sourced from GDACS, European Commission Joint Research Centre, CC BY 4.0",
            }
            if r.get("severity_raw"):
                payload["raw_severity_value"] = r["severity_raw"]
                payload["raw_severity_unit"]  = r.get("severity_unit")
            if r.get("population_raw"):
                payload["affected_population_value"] = r["population_raw"]
                payload["affected_population_unit"]  = r.get("population_unit")

            out.append(GlassboxEvent(
                layer=self.layer,
                external_id=f"gdacs:{event_type}:{event_id}",
                kind="gdacs_alert",
                lat=float(r["lat"]),
                lng=float(r["lng"]),
                ts=ts_iso,
                severity=severity,
                source=self.source,
                payload=payload,
                domain=("maritime" if event_type == "TC" else "geo"),
                geocode_quality="point",
                # Decay: most GDACS items stay relevant for the duration of the
                # event. 24h is a reasonable per-event window for v1.0.
                decay_half_life_min=1440,
                market_tags=[],
                severity_for_market=0,
            ))
        return out
