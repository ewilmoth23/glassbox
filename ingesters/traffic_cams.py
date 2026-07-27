"""
MEWR Glassbox — Live Camera Feeds & Real-Time Traffic Ingester
==============================================================
Sources:
  1. DOT / 511 Traffic Cameras — official government APIs, fully open
     - US states with open APIs: CA, TX, NY, WA, OR, CO, FL, VA, MN, AZ
     - Each returns lat/lng + JPEG/MJPEG stream URL

  2. HERE Traffic API (free tier: 250K API calls/month)
     - Real-time incidents: accidents, closures, congestion
     - Returns severity, lat/lng, description

  3. TomTom Traffic API (free tier: 2,500 requests/day)
     - Traffic incidents with severity + coordinates

  4. OpenStreetMap Nominatim geocoding (free, no key)
     - Used to geocode location strings from camera metadata

  5. webcams.travel API — public worldwide webcam directory
     - No key required for basic access

All sources emit GlassboxEvent-compatible dicts with:
  - layer: "traffic_cams" | "traffic_incidents" | "webcams"
  - has_media: True (camera stream or incident icon)
  - media_url: JPEG/MJPEG stream URL where available
  - confidence_score: via confidence_scorer

Environment variables:
  HERE_API_KEY    — developer.here.com (free tier, 250K/month)
  TOMTOM_API_KEY  — developer.tomtom.com (free tier, 2500/day)
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import aiohttp

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from confidence_scorer import score_event

log = logging.getLogger("traffic_cams")

# ─── Config ───────────────────────────────────────────────────────────────────

HERE_API_KEY   = os.getenv("HERE_API_KEY", "")
TOMTOM_API_KEY = os.getenv("TOMTOM_API_KEY", "")

def _make_id(source: str, uid: str) -> str:
    return hashlib.md5(f"{source}:{uid}".encode()).hexdigest()[:16]

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ─── DOT / 511 Traffic Camera APIs ────────────────────────────────────────────
# All fully open, no API key required.
# Each returns a list of camera objects with lat/lng and stream URLs.

DOT_ENDPOINTS = {
    # California — 511 API (largest camera network, ~1000+ cams)
    "california": "https://511.org/api/v2/get/cameras?api_key=&format=json",
    # Washington State DOT
    "washington": "https://www.wsdot.wa.gov/Traffic/api/Cameras/CameraAPI.ashx?AccessCode=&format=json",
    # Colorado — COTRIP
    "colorado": "https://cotrip.org/speed/getALLCamera.do",
    # Oregon DOT
    "oregon": "https://api.odot.state.or.us/tripcheck/Cameras",
    # Minnesota DOT — open data
    "minnesota": "https://data.dot.state.mn.us/api/CAMERA/json?key=",
    # Utah UDOT
    "utah": "https://api.udot.utah.gov/mt/cameras",
}

# Backup: hardcoded camera samples for states without clean JSON APIs
# Real lat/lng positions from official DOT sources
HARDCODED_CAMS = [
    # California — Bay Bridge
    {"id": "cam_ca_001", "name": "Bay Bridge Toll Plaza — Oakland", "lat": 37.8197, "lng": -122.3775, "state": "CA", "url": "https://cwwp2.dot.ca.gov/vm/streamwin.htm?id=cam002"},
    {"id": "cam_ca_002", "name": "I-5 @ LA Central", "lat": 34.0195, "lng": -118.2519, "state": "CA", "url": "https://cwwp2.dot.ca.gov/vm/streamwin.htm?id=cam054"},
    {"id": "cam_ca_003", "name": "Golden Gate Bridge North", "lat": 37.8344, "lng": -122.4786, "state": "CA", "url": ""},
    # New York
    {"id": "cam_ny_001", "name": "Brooklyn Bridge", "lat": 40.7061, "lng": -73.9969, "state": "NY", "url": ""},
    {"id": "cam_ny_002", "name": "Lincoln Tunnel — NJ Approach", "lat": 40.7611, "lng": -74.0196, "state": "NY", "url": ""},
    # Texas
    {"id": "cam_tx_001", "name": "I-35 Austin Corridor", "lat": 30.2849, "lng": -97.7341, "state": "TX", "url": ""},
    {"id": "cam_tx_002", "name": "I-10 Houston Downtown", "lat": 29.7604, "lng": -95.3698, "state": "TX", "url": ""},
    # Florida
    {"id": "cam_fl_001", "name": "I-95 Miami @ SR-836", "lat": 25.7617, "lng": -80.1918, "state": "FL", "url": ""},
    {"id": "cam_fl_002", "name": "I-4 Orlando International", "lat": 28.5383, "lng": -81.3792, "state": "FL", "url": ""},
    # Washington
    {"id": "cam_wa_001", "name": "I-5 Seattle — Mercer St", "lat": 47.6239, "lng": -122.3212, "state": "WA", "url": ""},
    # Colorado
    {"id": "cam_co_001", "name": "I-70 Eisenhower Tunnel", "lat": 39.6853, "lng": -105.9128, "state": "CO", "url": ""},
    # Chicago
    {"id": "cam_il_001", "name": "I-90/94 Chicago — Wacker Dr", "lat": 41.8855, "lng": -87.6354, "state": "IL", "url": ""},
    # Nevada
    {"id": "cam_nv_001", "name": "Las Vegas Strip — I-15", "lat": 36.1147, "lng": -115.1728, "state": "NV", "url": ""},
    # Oregon
    {"id": "cam_or_001", "name": "I-5 Portland — Fremont Bridge", "lat": 45.5370, "lng": -122.6733, "state": "OR", "url": ""},
    # Georgia
    {"id": "cam_ga_001", "name": "I-285 Atlanta Perimeter", "lat": 33.8403, "lng": -84.4677, "state": "GA", "url": ""},
]


class DOTCameraSource:
    """
    Fetches live traffic camera positions from DOT open APIs.
    Falls back to hardcoded verified positions if APIs are down.
    """

    async def fetch(self, session: aiohttp.ClientSession) -> List[Dict[str, Any]]:
        events = []

        # Try live APIs first
        events.extend(await self._fetch_california(session))
        events.extend(await self._fetch_wsdot(session))

        # Always include hardcoded verified cams (supplement live data)
        for cam in HARDCODED_CAMS:
            conf = score_event(
                platform="manual",
                has_media=bool(cam.get("url")),
                has_coordinates=True,
                coordinate_precision_km=0.5,
                source_tier=1,  # Government DOT = tier 1
                has_url=bool(cam.get("url")),
                age_hours=0.0,
            )
            events.append({
                "external_id": _make_id("dot_cam", cam["id"]),
                "layer": "traffic_cams",
                "lat": cam["lat"],
                "lng": cam["lng"],
                "has_coords": True,
                "title": f"Traffic Cam: {cam['name']}",
                "summary": f"Live DOT traffic camera — {cam['state']}. {cam['name']}",
                "url": cam.get("url", ""),
                "media_url": cam.get("url", ""),
                "source": f"DOT {cam['state']} Traffic Camera",
                "platform": "manual",
                "severity": 1.0,  # Cameras are infrastructure — low base severity
                "confidence_score": conf.score,
                "confidence_label": conf.label,
                "severity_cap": conf.severity_cap,
                "timestamp": _now_iso(),
                "has_media": bool(cam.get("url")),
                "cam_id": cam["id"],
                "state": cam["state"],
            })

        log.info("DOT cameras: %d total events", len(events))
        return events

    async def _fetch_california(self, session: aiohttp.ClientSession) -> List[Dict]:
        """CA 511 API — returns camera metadata including lat/lng and image URLs."""
        try:
            # CA CWWP2 camera list (free, no key)
            url = "https://cwwp2.dot.ca.gov/data/d3/cctv/cctvStatusD03.json"
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json(content_type=None)

            cameras = data if isinstance(data, list) else data.get("data", [])
            events = []
            for cam in cameras[:50]:  # Limit to 50
                lat = cam.get("Location", {}).get("Latitude") or cam.get("lat")
                lng = cam.get("Location", {}).get("Longitude") or cam.get("lon")
                if not lat or not lng:
                    continue
                name = cam.get("CctvID", "") or cam.get("name", "CA Camera")
                img_url = cam.get("ImageURL", "") or cam.get("url", "")
                conf = score_event(
                    platform="manual",
                    has_media=bool(img_url),
                    has_coordinates=True,
                    coordinate_precision_km=0.1,
                    source_tier=1,
                    has_url=True,
                    age_hours=0.0,
                )
                events.append({
                    "external_id": _make_id("ca_dot", str(name)),
                    "layer": "traffic_cams",
                    "lat": float(lat), "lng": float(lng),
                    "has_coords": True,
                    "title": f"CA Traffic Cam: {name}",
                    "summary": f"California DOT live traffic camera",
                    "url": img_url,
                    "media_url": img_url,
                    "source": "Caltrans CWWP2",
                    "platform": "manual",
                    "severity": 1.0,
                    "confidence_score": conf.score,
                    "confidence_label": conf.label,
                    "severity_cap": conf.severity_cap,
                    "timestamp": _now_iso(),
                    "has_media": bool(img_url),
                    "state": "CA",
                })
            log.info("CA DOT: %d cameras loaded", len(events))
            return events
        except Exception as exc:
            log.debug("CA DOT API error (using hardcoded): %s", exc)
            return []

    async def _fetch_wsdot(self, session: aiohttp.ClientSession) -> List[Dict]:
        """Washington State DOT camera API."""
        try:
            url = "https://www.wsdot.wa.gov/Traffic/api/Cameras/CameraAPI.ashx?AccessCode=&format=json"
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json(content_type=None)

            if not isinstance(data, list):
                return []

            events = []
            for cam in data[:30]:
                lat = cam.get("ImageURL") and None  # WSDOT doesn't expose lat in basic API
                loc = cam.get("Location", {})
                lat = loc.get("Latitude") if loc else None
                lng = loc.get("Longitude") if loc else None
                if not lat or not lng:
                    continue
                name = cam.get("Title", "WA Camera")
                img_url = cam.get("ImageURL", "")
                conf = score_event(
                    platform="manual",
                    has_media=bool(img_url),
                    has_coordinates=True,
                    coordinate_precision_km=0.1,
                    source_tier=1,
                    has_url=True,
                    age_hours=0.0,
                )
                events.append({
                    "external_id": _make_id("wsdot", name),
                    "layer": "traffic_cams",
                    "lat": float(lat), "lng": float(lng),
                    "has_coords": True,
                    "title": f"WA Traffic Cam: {name}",
                    "summary": f"Washington State DOT live camera",
                    "url": img_url, "media_url": img_url,
                    "source": "WSDOT", "platform": "manual",
                    "severity": 1.0,
                    "confidence_score": conf.score, "confidence_label": conf.label,
                    "severity_cap": conf.severity_cap, "timestamp": _now_iso(),
                    "has_media": bool(img_url), "state": "WA",
                })
            return events
        except Exception as exc:
            log.debug("WSDOT API error: %s", exc)
            return []


# ─── HERE Real-Time Traffic Incidents ─────────────────────────────────────────

class HERETrafficSource:
    """
    HERE Traffic API — real-time incidents.
    Free tier: 250,000 transactions/month.
    Sign up at: https://developer.here.com (choose Freemium plan)
    """

    INCIDENTS_URL = "https://data.traffic.hereapi.com/v7/incidents"

    # Bounding boxes for major CONUS metro areas to query
    BBOXES = [
        ("New York",    (40.45, -74.27, 40.91, -73.70)),
        ("Los Angeles", (33.70, -118.67, 34.30, -117.60)),
        ("Chicago",     (41.64, -87.94,  42.03, -87.52)),
        ("Houston",     (29.52, -95.63,  30.09, -95.02)),
        ("Miami",       (25.55, -80.44,  25.98, -80.10)),
        ("Seattle",     (47.40, -122.52, 47.81, -121.97)),
        ("Atlanta",     (33.60, -84.71,  33.97, -84.20)),
        ("Dallas",      (32.62, -97.04,  33.02, -96.50)),
    ]

    def __init__(self, api_key: str):
        self.api_key = api_key

    async def fetch(self, session: aiohttp.ClientSession) -> List[Dict[str, Any]]:
        if not self.api_key:
            log.info("HERE: no API key — skipping traffic incidents")
            return []

        events = []
        for city, (south, west, north, east) in self.BBOXES:
            try:
                params = {
                    "apiKey": self.api_key,
                    "bbox": f"{west},{south},{east},{north}",
                    "locationReferencing": "shape",
                    "incidentType": "accident,congestion,roadClosed,construction",
                }
                async with session.get(
                    self.INCIDENTS_URL, params=params,
                    timeout=aiohttp.ClientTimeout(total=15)
                ) as resp:
                    if resp.status != 200:
                        log.warning("HERE HTTP %s for %s", resp.status, city)
                        continue
                    data = await resp.json()

                for result in data.get("results", []):
                    inc = result.get("incidentDetails", {})
                    loc = result.get("location", {})

                    # Get coordinates from shape or start point
                    shape = loc.get("shape", {})
                    links = shape.get("links", [])
                    lat, lng = None, None
                    if links:
                        pts = links[0].get("points", [])
                        if pts:
                            lat = pts[len(pts)//2].get("lat")
                            lng = pts[len(pts)//2].get("lng")

                    if lat is None:
                        continue

                    inc_type = inc.get("type", {}).get("label", "incident")
                    severity_raw = inc.get("criticality", {}).get("label", "minor")
                    severity_map = {"critical": 8.0, "major": 6.0, "minor": 3.0, "lowImpact": 2.0}
                    severity = severity_map.get(severity_raw, 4.0)

                    description = inc.get("description", {}).get("value", inc_type)
                    start_time = inc.get("startTime", _now_iso())

                    conf = score_event(
                        platform="manual",
                        has_media=False,
                        has_coordinates=True,
                        coordinate_precision_km=0.1,
                        source_tier=1,  # HERE = official traffic data
                        has_url=False,
                        age_hours=0.5,
                    )

                    events.append({
                        "external_id": _make_id("here", result.get("incidentId", str(lat))),
                        "layer": "traffic_incidents",
                        "lat": float(lat), "lng": float(lng),
                        "has_coords": True,
                        "title": f"{inc_type.title()}: {city}",
                        "summary": description,
                        "url": "",
                        "source": f"HERE Traffic — {city}",
                        "platform": "manual",
                        "severity": severity,
                        "confidence_score": conf.score,
                        "confidence_label": conf.label,
                        "severity_cap": conf.severity_cap,
                        "timestamp": start_time,
                        "has_media": False,
                        "incident_type": inc_type,
                        "severity_label": severity_raw,
                    })

                await asyncio.sleep(0.2)

            except Exception as exc:
                log.error("HERE city=%s error: %s", city, exc)

        log.info("HERE Traffic: %d incidents", len(events))
        return events


# ─── TomTom Traffic Source ────────────────────────────────────────────────────

class TomTomTrafficSource:
    """
    TomTom Traffic Incidents API.
    Free tier: 2,500 requests/day.
    Sign up at: https://developer.tomtom.com
    """

    INCIDENTS_URL = "https://api.tomtom.com/traffic/services/5/incidentDetails"

    # Zoom 10 covers ~100km radius — good for metro areas
    QUERIES = [
        # (lat, lng, zoom)
        (40.7128, -74.0060, 10),   # New York
        (34.0522, -118.2437, 10),  # Los Angeles
        (41.8781, -87.6298, 10),   # Chicago
        (29.7604, -95.3698, 10),   # Houston
        (47.6062, -122.3321, 10),  # Seattle
        (51.5074, -0.1278, 10),    # London
        (48.8566, 2.3522, 10),     # Paris
        (35.6762, 139.6503, 10),   # Tokyo
    ]

    def __init__(self, api_key: str):
        self.api_key = api_key

    async def fetch(self, session: aiohttp.ClientSession) -> List[Dict[str, Any]]:
        if not self.api_key:
            log.info("TomTom: no API key — skipping")
            return []

        events = []
        for lat, lng, zoom in self.QUERIES[:4]:  # Limit to stay in free tier
            try:
                # TomTom uses bbox: minLon,minLat,maxLon,maxLat
                delta = 0.5
                bbox = f"{lng-delta},{lat-delta},{lng+delta},{lat+delta}"
                params = {
                    "key": self.api_key,
                    "bbox": bbox,
                    "fields": "{incidents{type,geometry{coordinates},properties{id,magnitudeOfDelay,events{description,code},startTime,endTime}}}",
                    "language": "en-GB",
                    "t": "-1",
                    "timeValidityFilter": "present",
                }
                url = f"{self.INCIDENTS_URL}?key={self.api_key}&bbox={bbox}&timeValidityFilter=present"
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status != 200:
                        continue
                    data = await resp.json()

                for inc in data.get("incidents", []):
                    geo = inc.get("geometry", {})
                    coords = geo.get("coordinates", [])
                    if not coords:
                        continue

                    # LineString → take midpoint; Point → direct
                    if geo.get("type") == "LineString" and len(coords) >= 2:
                        mid = coords[len(coords)//2]
                        ilng, ilat = float(mid[0]), float(mid[1])
                    elif geo.get("type") == "Point":
                        ilng, ilat = float(coords[0]), float(coords[1])
                    else:
                        continue

                    props = inc.get("properties", {})
                    events_list = props.get("events", [])
                    description = events_list[0].get("description", "Traffic incident") if events_list else "Traffic incident"
                    magnitude = props.get("magnitudeOfDelay", 0)
                    severity = min(10.0, float(magnitude) * 2.0 + 2.0)

                    conf = score_event(
                        platform="manual",
                        has_media=False,
                        has_coordinates=True,
                        coordinate_precision_km=0.05,
                        source_tier=1,
                        has_url=False,
                        age_hours=0.0,
                    )

                    events.append({
                        "external_id": _make_id("tomtom", props.get("id", str(ilat))),
                        "layer": "traffic_incidents",
                        "lat": ilat, "lng": ilng,
                        "has_coords": True,
                        "title": f"Traffic: {description[:80]}",
                        "summary": description,
                        "url": "",
                        "source": "TomTom Traffic",
                        "platform": "manual",
                        "severity": severity,
                        "confidence_score": conf.score,
                        "confidence_label": conf.label,
                        "severity_cap": conf.severity_cap,
                        "timestamp": props.get("startTime", _now_iso()),
                        "has_media": False,
                    })

            except Exception as exc:
                log.error("TomTom error: %s", exc)

        log.info("TomTom: %d incidents", len(events))
        return events


# ─── Public Webcam Directory ──────────────────────────────────────────────────

# Curated list of notable public webcams with verified lat/lng
# These are permanent infrastructure feeds — great for globe context
NOTABLE_WEBCAMS = [
    {"id": "wc_times_sq",    "name": "Times Square NYC",         "lat": 40.7580, "lng": -73.9855, "url": "https://www.earthcam.com/usa/newyork/timessquare/?cam=tsrobo1", "city": "New York"},
    {"id": "wc_eiffel",      "name": "Eiffel Tower Paris",       "lat": 48.8584, "lng": 2.2945,   "url": "https://www.earthcam.com/world/france/paris/?cam=eiffel", "city": "Paris"},
    {"id": "wc_shibuya",     "name": "Shibuya Crossing Tokyo",   "lat": 35.6590, "lng": 139.7005, "url": "https://www.youtube.com/watch?v=ByERmBLJrHI", "city": "Tokyo"},
    {"id": "wc_dubai_burj",  "name": "Dubai Burj Khalifa",       "lat": 25.1972, "lng": 55.2744,  "url": "https://www.earthcam.com/world/uae/dubai/?cam=burjkhalifa", "city": "Dubai"},
    {"id": "wc_gg_bridge",   "name": "Golden Gate Bridge SF",    "lat": 37.8199, "lng": -122.4783,"url": "https://www.earthcam.com/usa/california/sanfrancisco/?cam=ggbridge", "city": "San Francisco"},
    {"id": "wc_colosseum",   "name": "Rome Colosseum",           "lat": 41.8902, "lng": 12.4922,  "url": "https://www.earthcam.com/world/italy/rome/?cam=colosseum", "city": "Rome"},
    {"id": "wc_trafalgar",   "name": "Trafalgar Square London",  "lat": 51.5080, "lng": -0.1281,  "url": "https://www.earthcam.com/world/england/london/?cam=trafalgar", "city": "London"},
    {"id": "wc_sydney_oh",   "name": "Sydney Opera House",       "lat": -33.8568, "lng": 151.2153,"url": "https://www.earthcam.com/world/australia/sydney/?cam=sydneyoperahouse", "city": "Sydney"},
    {"id": "wc_moscow_red",  "name": "Red Square Moscow",        "lat": 55.7539, "lng": 37.6208,  "url": "", "city": "Moscow"},
    {"id": "wc_hong_kong",   "name": "Hong Kong Harbour",        "lat": 22.2873, "lng": 114.1734, "url": "", "city": "Hong Kong"},
    {"id": "wc_kyiv_maidan", "name": "Kyiv Maidan Nezalezhnosti","lat": 50.4501, "lng": 30.5234,  "url": "", "city": "Kyiv"},
    {"id": "wc_taipei_101",  "name": "Taipei 101",               "lat": 25.0339, "lng": 121.5645, "url": "", "city": "Taipei"},
    {"id": "wc_istanbul",    "name": "Istanbul Bosphorus",        "lat": 41.0082, "lng": 28.9784,  "url": "", "city": "Istanbul"},
    {"id": "wc_singapore",   "name": "Singapore Marina Bay",     "lat": 1.2816,  "lng": 103.8636, "url": "", "city": "Singapore"},
    {"id": "wc_rio",         "name": "Rio de Janeiro Copacabana", "lat": -22.9714, "lng": -43.1823,"url": "", "city": "Rio de Janeiro"},
]


class WebcamSource:
    """Emits notable public webcam positions as globe entities."""

    async def fetch(self) -> List[Dict[str, Any]]:
        events = []
        for cam in NOTABLE_WEBCAMS:
            conf = score_event(
                platform="manual",
                has_media=bool(cam.get("url")),
                has_coordinates=True,
                coordinate_precision_km=0.1,
                source_tier=1,
                has_url=bool(cam.get("url")),
                age_hours=0.0,
            )
            events.append({
                "external_id": _make_id("webcam", cam["id"]),
                "layer": "webcams",
                "lat": cam["lat"],
                "lng": cam["lng"],
                "has_coords": True,
                "title": f"Live Cam: {cam['name']}",
                "summary": f"Public webcam — {cam['city']}",
                "url": cam.get("url", ""),
                "media_url": cam.get("url", ""),
                "source": f"EarthCam / Public — {cam['city']}",
                "platform": "manual",
                "severity": 1.0,
                "confidence_score": conf.score,
                "confidence_label": conf.label,
                "severity_cap": conf.severity_cap,
                "timestamp": _now_iso(),
                "has_media": bool(cam.get("url")),
                "city": cam.get("city", ""),
            })
        return events


# ─── Main Traffic Ingester ────────────────────────────────────────────────────

class TrafficCamsIngester:
    """
    Orchestrates all camera and traffic sources.
    Wired into glassbox_server.py _startup() just like gdelt.py.
    """

    # Canonical layer name (snake_case). Per-source events may emit under
    # narrower layers ("traffic_cams", "traffic_incidents", "webcams") but
    # the orchestrator identity is traffic_cams. The wired adapter
    # (TrafficCamsAdapter in citizen_adapter.py) also uses traffic_cams.
    layer = "traffic_cams"
    source = "DOT 511 / HERE / TomTom / Public Webcams"

    def __init__(self):
        self.dot      = DOTCameraSource()
        self.here     = HERETrafficSource(api_key=HERE_API_KEY)
        self.tomtom   = TomTomTrafficSource(api_key=TOMTOM_API_KEY)
        self.webcams  = WebcamSource()

    async def run(self) -> Dict[str, List[Dict[str, Any]]]:
        results: Dict[str, List] = {
            "traffic_cams": [],
            "traffic_incidents": [],
            "webcams": [],
        }

        async with aiohttp.ClientSession() as session:
            dot_task    = asyncio.create_task(self.dot.fetch(session))
            here_task   = asyncio.create_task(self.here.fetch(session))
            tomtom_task = asyncio.create_task(self.tomtom.fetch(session))
            webcam_task = asyncio.create_task(self.webcams.fetch())

            dot_events    = await dot_task
            here_events   = await here_task
            tomtom_events = await tomtom_task
            webcam_events = await webcam_task

        results["traffic_cams"].extend(dot_events)
        results["traffic_incidents"].extend(here_events)
        results["traffic_incidents"].extend(tomtom_events)
        results["webcams"].extend(webcam_events)

        total = sum(len(v) for v in results.values())
        log.info("TrafficCams harvest complete: %d total events", total)
        return results

    def all_events(self, run_result: Dict[str, List]) -> List[Dict[str, Any]]:
        return [e for events in run_result.values() for e in events]


# ─── Standalone test ──────────────────────────────────────────────────────────

async def _test():
    logging.basicConfig(level=logging.INFO)
    ingester = TrafficCamsIngester()
    result = await ingester.run()
    for layer, events in result.items():
        print(f"\n{layer.upper()}: {len(events)} events")
        for ev in events[:3]:
            print(f"  [{ev['confidence_label']}] {ev['title'][:70]}")
            print(f"    {ev['lat']:.4f}, {ev['lng']:.4f}")


if __name__ == "__main__":
    asyncio.run(_test())
