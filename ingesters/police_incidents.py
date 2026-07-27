"""
Police / Fire / EMS Incidents ingester — live public CAD feeds + PulsePoint.

Sources (all free, no API key required):
  City CAD open-data feeds  — SF, Chicago, Seattle, NYC, Denver, Austin, Portland
  PulsePoint JSON           — EMS/Fire dispatch, nationwide, ~600 agencies

Poll interval: 120 seconds (city feeds refresh every 1-5 min).

Severity scale:
  10 — shooting / stabbing / explosion / structure fire with entrapment
   8 — robbery / assault / major MVA / working fire
   6 — burglary / person down / overdose / vehicle fire
   4 — suspicious / disturbance / minor MVA
   2 — noise complaint / alarm / vandalism / welfare check
   1 — other / unknown

Dedup key: city_code + incident_id from source. Content hash on
(type, address) so duplicate dispatches don't flood the globe.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from .base import GlassboxEvent, Ingester

log = logging.getLogger(__name__)

# ─── Severity classifier ─────────────────────────────────────────────────────

_HIGH_KEYWORDS = (
    "shooting", "shot", "stabbing", "stabbed", "explosion", "explosive",
    "bomb", "homicide", "murder", "structure fire", "entrap", "hazmat",
    "active", "hostage", "barricade", "officer down", "mass casualty",
)
_MED_HIGH_KEYWORDS = (
    "robbery", "armed", "assault", "fight", "fire", "working fire",
    "vehicle fire", "major accident", "serious injury", "cardiac",
    "unconscious", "unresponsive", "overdose", "person down",
)
_MED_KEYWORDS = (
    "burglary", "theft", "stolen", "auto", "vehicle break", "pursuit",
    "missing", "suspicious", "disturbance", "domestic", "trespass",
    "intoxicated", "hit and run", "accident",
)


def _classify(text: str) -> int:
    t = text.lower()
    for kw in _HIGH_KEYWORDS:
        if kw in t:
            return 10
    for kw in _MED_HIGH_KEYWORDS:
        if kw in t:
            return 8
    for kw in _MED_KEYWORDS:
        if kw in t:
            return 6
    if any(k in t for k in ("alarm", "noise", "welfare", "vandal", "parking")):
        return 2
    return 4


def _incident_type(text: str) -> str:
    """Return a clean short label for the popup badge."""
    t = text.lower()
    if any(k in t for k in ("shooting", "shot fired", "shots fired")):
        return "SHOTS FIRED"
    if any(k in t for k in ("stabbing", "stabbed")):
        return "STABBING"
    if "homicide" in t or "murder" in t:
        return "HOMICIDE"
    if "robbery" in t:
        return "ROBBERY"
    if "assault" in t:
        return "ASSAULT"
    if "explosion" in t or "bomb" in t:
        return "EXPLOSION"
    if "fire" in t:
        return "FIRE"
    if "overdose" in t:
        return "OVERDOSE"
    if "accident" in t or "collision" in t or "crash" in t or "mva" in t:
        return "MVA"
    if "cardiac" in t or "unconscious" in t or "unresponsive" in t:
        return "MEDICAL"
    if "pursuit" in t or "chase" in t:
        return "PURSUIT"
    if "domestic" in t:
        return "DOMESTIC"
    if "suspicious" in t:
        return "SUSPICIOUS"
    if "disturbance" in t:
        return "DISTURBANCE"
    if "burglary" in t or "break" in t:
        return "BURGLARY"
    if "theft" in t or "stolen" in t:
        return "THEFT"
    return "INCIDENT"


def _safe_encode_url(url: str) -> str:
    """Percent-encode the query string of a URL.

    The Socrata / NYC / SF / Chicago feeds use SoQL operators like
    `$order=received_dttm DESC` which contain literal spaces. Python's
    urllib refuses URLs with control chars (space included) → we have to
    re-encode the query side without disturbing the path or scheme.
    """
    parts = urllib.parse.urlsplit(url)
    if not parts.query:
        return url
    # parse_qsl with keep_blank_values=True so flags like `?$limit=` survive.
    pairs = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    # quote_via=quote so spaces become %20 (not '+', which Socrata rejects).
    encoded = urllib.parse.urlencode(pairs, quote_via=urllib.parse.quote)
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, parts.path, encoded, parts.fragment)
    )


def _fetch_json(url: str, timeout: int = 12) -> Any:
    safe_url = _safe_encode_url(url)
    req = urllib.request.Request(
        safe_url,
        headers={"User-Agent": "Glassbox-OSINT/2.0 (public data ingester)"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", errors="replace"))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _make_id(prefix: str, raw_id: str) -> str:
    return prefix + "_" + re.sub(r"[^a-zA-Z0-9_-]", "_", str(raw_id))


# ─── City CAD feed definitions ────────────────────────────────────────────────
#
# Each entry: (city_code, label, url, parser_fn)
# parser_fn(raw) -> List[(lat, lng, incident_type, address, incident_id, ts_str)]

def _parse_sf(raw: Any) -> List[Tuple]:
    """SF DataSF — Fire Department calls for service."""
    out = []
    rows = raw if isinstance(raw, list) else []
    for r in rows[:200]:
        try:
            lat = float(r.get("latitude") or 0)
            lng = float(r.get("longitude") or 0)
            if not lat or not lng:
                continue
            itype = str(r.get("call_type_final_desc") or r.get("call_type") or "Incident")
            addr = str(r.get("address") or "")
            iid = str(r.get("incident_number") or r.get("call_number") or "0")
            ts = str(r.get("received_dttm") or r.get("entry_dttm") or _now_iso())
            out.append((lat, lng, itype, addr, iid, ts))
        except Exception:
            continue
    return out


def _parse_chicago(raw: Any) -> List[Tuple]:
    """Chicago Data Portal — Police incidents (past 24h)."""
    out = []
    rows = raw if isinstance(raw, list) else []
    for r in rows[:300]:
        try:
            loc = r.get("location") or {}
            coords = loc.get("coordinates") or []
            if len(coords) < 2:
                lat = float(r.get("latitude") or 0)
                lng = float(r.get("longitude") or 0)
            else:
                lng, lat = float(coords[0]), float(coords[1])
            if not lat or not lng:
                continue
            itype = str(r.get("primary_type") or r.get("description") or "Incident")
            addr = str(r.get("block") or r.get("address") or "")
            iid = str(r.get("id") or r.get("case_number") or "0")
            ts = str(r.get("date") or _now_iso())
            out.append((lat, lng, itype, addr, iid, ts))
        except Exception:
            continue
    return out


def _parse_seattle(raw: Any) -> List[Tuple]:
    """Seattle Open Data — Police/Fire calls for service."""
    out = []
    rows = raw if isinstance(raw, list) else []
    for r in rows[:200]:
        try:
            lat = float(r.get("latitude") or 0)
            lng = float(r.get("longitude") or 0)
            if not lat or not lng:
                continue
            itype = str(r.get("type") or r.get("event_clearance_description") or "Incident")
            addr = str(r.get("hundred_block_location") or r.get("address") or "")
            iid = str(r.get("cad_cdw_id") or r.get("incident_number") or "0")
            ts = str(r.get("original_time_queued") or _now_iso())
            out.append((lat, lng, itype, addr, iid, ts))
        except Exception:
            continue
    return out


def _parse_denver(raw: Any) -> List[Tuple]:
    """Denver Open Data — Crime incidents."""
    out = []
    rows = raw if isinstance(raw, list) else []
    for r in rows[:200]:
        try:
            geo = r.get("geo_lon") or r.get("geo_lat") or None
            lat = float(r.get("geo_lat") or 0)
            lng = float(r.get("geo_lon") or 0)
            if not lat or not lng:
                continue
            itype = str(r.get("offense_type_id") or r.get("offense_category_id") or "Incident")
            addr = str(r.get("incident_address") or "")
            iid = str(r.get("incident_id") or "0")
            ts = str(r.get("first_occurrence_date") or _now_iso())
            out.append((lat, lng, itype, addr, iid, ts))
        except Exception:
            continue
    return out


def _parse_austin(raw: Any) -> List[Tuple]:
    """Austin Open Data — Police incidents."""
    out = []
    rows = raw if isinstance(raw, list) else []
    for r in rows[:200]:
        try:
            lat = float(r.get("latitude") or 0)
            lng = float(r.get("longitude") or 0)
            if not lat or not lng:
                continue
            itype = str(r.get("highest_offense_description") or r.get("category_description") or "Incident")
            addr = str(r.get("location") or r.get("address") or "")
            iid = str(r.get("incident_number") or r.get("occurrence_date_time") or "0")
            ts = str(r.get("occurred_date_time") or _now_iso())
            out.append((lat, lng, itype, addr, iid, ts))
        except Exception:
            continue
    return out


def _parse_nyc(raw: Any) -> List[Tuple]:
    """NYC Open Data — 311 Service Requests (noise, fire, safety)."""
    out = []
    rows = raw if isinstance(raw, list) else []
    for r in rows[:200]:
        try:
            lat = float(r.get("latitude") or 0)
            lng = float(r.get("longitude") or 0)
            if not lat or not lng:
                continue
            itype = str(r.get("complaint_type") or "311 Request")
            addr = str(r.get("incident_address") or r.get("street_name") or "")
            iid = str(r.get("unique_key") or "0")
            ts = str(r.get("created_date") or _now_iso())
            out.append((lat, lng, itype, addr, iid, ts))
        except Exception:
            continue
    return out


def _parse_portland(raw: Any) -> List[Tuple]:
    """Portland Open Data — Police calls for service."""
    out = []
    rows = raw if isinstance(raw, list) else []
    for r in rows[:200]:
        try:
            lat = float(r.get("open_data_lat") or r.get("latitude") or 0)
            lng = float(r.get("open_data_lon") or r.get("longitude") or 0)
            if not lat or not lng:
                continue
            itype = str(r.get("final_case_type") or r.get("call_type") or "Incident")
            addr = str(r.get("address") or "")
            iid = str(r.get("case_number") or r.get("incident_number") or "0")
            ts = str(r.get("call_date") or _now_iso())
            out.append((lat, lng, itype, addr, iid, ts))
        except Exception:
            continue
    return out


# (city_code, city_label, url, parser)
_CITY_FEEDS = [
    (
        "sf", "San Francisco",
        "https://data.sfgov.org/resource/wr8u-xric.json?$limit=200&$order=received_dttm DESC",
        _parse_sf,
    ),
    (
        "chicago", "Chicago",
        "https://data.cityofchicago.org/resource/ijzp-q8t2.json?$limit=250&$order=date DESC",
        _parse_chicago,
    ),
    (
        "seattle", "Seattle",
        "https://data.seattle.gov/resource/33kz-ixgy.json?$limit=200&$order=original_time_queued DESC",
        _parse_seattle,
    ),
    (
        "denver", "Denver",
        "https://data.denvergov.org/resource/c9i8-nixi.json?$limit=200&$order=first_occurrence_date DESC",
        _parse_denver,
    ),
    (
        "austin", "Austin",
        "https://data.austintexas.gov/resource/fdj4-gpfu.json?$limit=200&$order=occurred_date_time DESC",
        _parse_austin,
    ),
    (
        "nyc", "New York City",
        "https://data.cityofnewyork.us/resource/erm2-nwe9.json?$limit=200&$order=created_date DESC&$where=latitude IS NOT NULL",
        _parse_nyc,
    ),
    (
        "portland", "Portland",
        "https://opendata.arcgis.com/datasets/dc6d9a985a9745388d88bdec43ef10fd_0.geojson",
        None,  # GeoJSON — handled inline below
    ),
]


def _parse_geojson_features(raw: Any, city_code: str, city_label: str) -> List[Tuple]:
    """Generic GeoJSON FeatureCollection parser."""
    out = []
    features = (raw or {}).get("features") or []
    for f in features[:200]:
        try:
            geom = f.get("geometry") or {}
            coords = geom.get("coordinates") or []
            if len(coords) < 2:
                continue
            lng, lat = float(coords[0]), float(coords[1])
            props = f.get("properties") or {}
            itype = str(props.get("TypeDescription") or props.get("call_type") or
                        props.get("incident_type") or props.get("nature") or "Incident")
            addr = str(props.get("Address") or props.get("address") or "")
            iid = str(props.get("CaseNumber") or props.get("case_number") or
                      props.get("id") or props.get("OBJECTID") or "0")
            ts = str(props.get("CallDate") or props.get("call_date") or
                     props.get("date") or _now_iso())
            out.append((lat, lng, itype, addr, iid, ts))
        except Exception:
            continue
    return out


# ─── PulsePoint ───────────────────────────────────────────────────────────────

_PULSEPOINT_AGENCIES = [
    # (agency_id, city_label, lat_center, lng_center)
    # Major US metros — PulsePoint uses short agency codes
    ("EMS1226", "Los Angeles EMS", 34.05, -118.24),
    ("EMS1014", "Chicago EMS",     41.83,  -87.63),
    ("EMS1215", "Houston EMS",     29.76,  -95.37),
    ("EMS1221", "Philadelphia EMS", 39.95, -75.16),
    ("EMS1016", "Phoenix EMS",     33.45, -112.07),
    ("EMS1231", "San Antonio EMS", 29.42,  -98.49),
    ("EMS1219", "Dallas EMS",      32.78,  -96.80),
    ("EMS1022", "San Jose EMS",    37.34, -121.89),
    ("EMS1235", "Jacksonville EMS",30.33,  -81.65),
    ("EMS1223", "Columbus EMS",    39.96,  -82.99),
]

_PULSEPOINT_CALL_TYPES = {
    "ME": ("MEDICAL EMERGENCY", 8),
    "CH": ("CARDIAC", 10),
    "TR": ("TRAUMA", 9),
    "MV": ("MVA", 7),
    "FI": ("FIRE", 9),
    "BF": ("BUILDING FIRE", 10),
    "VF": ("VEHICLE FIRE", 8),
    "HA": ("HAZMAT", 9),
    "RG": ("GUN", 10),
    "KN": ("KNIFE / STABBING", 9),
    "WC": ("WELFARE CHECK", 4),
    "OD": ("OVERDOSE", 7),
    "AL": ("ALARM", 3),
    "DI": ("DISTURBANCE", 5),
    "UN": ("UNCONSCIOUS", 8),
}


class PoliceIncidentsIngester(Ingester):
    """
    Aggregates police / fire / EMS incidents from:
      • 7 city CAD open-data feeds
      • PulsePoint (EMS/Fire JSON, 10 major agencies)

    No API keys required. All public data.
    """

    layer = "police_incidents"
    source = "City CAD Feeds + PulsePoint"
    # Each city CAD ToS + PulsePoint ToS need verification before v1.0.
    # Refused at gate until per-jurisdiction rows added.
    source_id = "police_incidents_aggregated"
    poll_interval_sec = 120.0   # 2 minutes

    # ── Fetch ────────────────────────────────────────────────────────────────

    async def fetch(self) -> List[Dict[str, Any]]:
        loop = asyncio.get_event_loop()
        results: List[Dict[str, Any]] = []

        # City CAD feeds
        for city_code, city_label, url, parser in _CITY_FEEDS:
            try:
                raw = await loop.run_in_executor(None, _fetch_json, url)
                if parser is not None:
                    tuples = parser(raw)
                else:
                    # GeoJSON
                    tuples = _parse_geojson_features(raw, city_code, city_label)
                for t in tuples:
                    results.append({
                        "_src": "cad",
                        "_city": city_code,
                        "_city_label": city_label,
                        "lat": t[0], "lng": t[1],
                        "itype": t[2], "addr": t[3],
                        "iid": t[4], "ts": t[5],
                    })
            except Exception as e:
                log.warning("[police] city=%s fetch failed: %s", city_code, e)

        # PulsePoint
        for agency_id, city_label, clat, clng in _PULSEPOINT_AGENCIES:
            try:
                url = (
                    f"https://web.pulsepoint.org/DB/giba.php"
                    f"?agency_id={agency_id}"
                )
                raw = await loop.run_in_executor(None, _fetch_json, url)
                incidents = (raw or {}).get("incidents") or {}
                active = incidents.get("active") or []
                for inc in active[:50]:
                    call_type = str(inc.get("PulsePointIncidentCallType") or "")
                    label, _ = _PULSEPOINT_CALL_TYPES.get(
                        call_type, (call_type or "INCIDENT", 4)
                    )
                    # PulsePoint gives lat/lng directly
                    lat = float(inc.get("Latitude") or clat)
                    lng = float(inc.get("Longitude") or clng)
                    iid = str(inc.get("ID") or inc.get("IncidentID") or "0")
                    addr = str(inc.get("FullDisplayAddress") or inc.get("Address") or "")
                    ts = str(inc.get("CallReceivedDateTime") or _now_iso())
                    results.append({
                        "_src": "pulsepoint",
                        "_city": agency_id,
                        "_city_label": city_label,
                        "lat": lat, "lng": lng,
                        "itype": label, "addr": addr,
                        "iid": iid, "ts": ts,
                    })
            except Exception as e:
                log.warning("[police] pulsepoint=%s fetch failed: %s", agency_id, e)

        return results

    # ── Normalize ────────────────────────────────────────────────────────────

    def normalize(self, raw_items: List[Dict[str, Any]]) -> List[GlassboxEvent]:
        events: List[GlassboxEvent] = []
        for r in raw_items:
            try:
                lat = float(r.get("lat") or 0)
                lng = float(r.get("lng") or 0)
                if not (-90 <= lat <= 90) or not (-180 <= lng <= 180):
                    continue
                if lat == 0.0 and lng == 0.0:
                    continue

                city_code = str(r.get("_city") or "unknown")
                city_label = str(r.get("_city_label") or city_code)
                itype = str(r.get("itype") or "Incident")
                addr = str(r.get("addr") or "")
                iid = str(r.get("iid") or "0")
                ts = str(r.get("ts") or _now_iso())

                severity = _classify(itype)
                badge = _incident_type(itype)
                external_id = _make_id(city_code, iid)
                title = f"{badge} — {city_label}"
                if addr:
                    title += f" ({addr[:60]})"

                events.append(GlassboxEvent(
                    layer=self.layer,
                    external_id=external_id,
                    kind="alert",
                    lat=lat,
                    lng=lng,
                    ts=ts,
                    severity=severity,
                    source=city_label,
                    payload={
                        "title": title,
                        "badge": badge,
                        "incident_type": itype,
                        "address": addr,
                        "city": city_label,
                        "source": r.get("_src", "cad"),
                    },
                ))
            except Exception as e:
                log.debug("[police] normalize error: %s", e)
        return events
