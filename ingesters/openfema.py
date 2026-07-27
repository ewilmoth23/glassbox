"""
OpenFEMA Disaster Declarations Summaries ingester.

Source: https://www.fema.gov/api/open/v2/DisasterDeclarationsSummaries
License: US Government public domain (FEMA, 17 USC §105). Commercial-OK.
NO API key required.

Strategic context: a Presidential disaster declaration is the
authoritative confirmation that a hazard has crossed thresholds for
federal aid. By the time FEMA declares, GDACS / NHC / NWS have usually
already flagged the event — but the declaration adds a strong
"this is serious enough for federal action" signal that drives
insurance, supply-chain, and political coverage. Useful as a
correlation overlay on top of the upstream weather/seismic feeds.

What we ingest: declarations from the last 60 days. The endpoint
returns one row per (disaster, designated county/area), so a single
hurricane affecting 30 counties yields 30 rows — we group by
disasterNumber and emit ONE event per disaster (representative county).

Coordinates: FEMA's API doesn't expose lat/lng. We use state-centroid
lookup (population-weighted center for major states, geographic center
for sparser ones). Granularity: state-level. Adequate for the map
overlay since the alert is "an event happened in this state" rather
than precise coordinates.

Refresh cadence: declarations issue at most a few per day during
active episodes; usually weekly cadence in quiet periods. Poll every
30 min — small payload, plenty of headroom.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import aiohttp

from .base import GlassboxEvent, Ingester


# State centroids (population-weighted where helpful, geographic center otherwise).
# Sources: US Census 2020 mean population center for the populous states;
# official state geographic centers for low-population states + territories.
_STATE_CENTROIDS: Dict[str, Tuple[float, float]] = {
    "AL": (32.7794, -86.8287),
    "AK": (64.0685, -152.2782),
    "AZ": (34.2744, -111.6602),
    "AR": (34.8938, -92.4426),
    "CA": (37.1841, -119.4696),
    "CO": (38.9972, -105.5478),
    "CT": (41.6219, -72.7273),
    "DE": (38.9896, -75.5050),
    "DC": (38.9101, -77.0147),
    "FL": (28.6305, -82.4497),
    "GA": (32.6415, -83.4426),
    "HI": (20.2927, -156.3737),
    "ID": (44.3509, -114.6130),
    "IL": (40.0417, -89.1965),
    "IN": (39.8942, -86.2816),
    "IA": (42.0751, -93.4960),
    "KS": (38.4937, -98.3804),
    "KY": (37.5347, -85.3021),
    "LA": (31.0689, -91.9968),
    "ME": (45.3695, -69.2428),
    "MD": (39.0550, -76.7909),
    "MA": (42.2596, -71.8083),
    "MI": (44.3467, -85.4102),
    "MN": (46.2807, -94.3053),
    "MS": (32.7364, -89.6678),
    "MO": (38.3566, -92.4580),
    "MT": (47.0527, -109.6333),
    "NE": (41.5378, -99.7951),
    "NV": (39.3289, -116.6312),
    "NH": (43.6805, -71.5811),
    "NJ": (40.1907, -74.6728),
    "NM": (34.4071, -106.1126),
    "NY": (42.9538, -75.5268),
    "NC": (35.5557, -79.3877),
    "ND": (47.4501, -100.4659),
    "OH": (40.2862, -82.7937),
    "OK": (35.5889, -97.4943),
    "OR": (43.9336, -120.5583),
    "PA": (40.8781, -77.7996),
    "RI": (41.6772, -71.5101),
    "SC": (33.9169, -80.8964),
    "SD": (44.4443, -100.2263),
    "TN": (35.8580, -86.3505),
    "TX": (31.4757, -99.3312),
    "UT": (39.3055, -111.6703),
    "VT": (44.0687, -72.6658),
    "VA": (37.5215, -78.8537),
    "WA": (47.3826, -120.4472),
    "WV": (38.6409, -80.6227),
    "WI": (44.6243, -89.9941),
    "WY": (42.9957, -107.5512),
    # Territories
    "PR": (18.2208, -66.5901),
    "VI": (18.0001, -64.8000),    # US Virgin Islands
    "GU": (13.4443, 144.7937),    # Guam
    "AS": (-14.2710, -170.1322),  # American Samoa
    "MP": (15.0979, 145.6739),    # Northern Mariana Islands
}


def _state_coords(state_abbr: Optional[str]) -> Tuple[float, float]:
    """Return state-centroid (lat, lng) or sentinel (0, 0) if unknown."""
    if not state_abbr:
        return (0.0, 0.0)
    return _STATE_CENTROIDS.get(state_abbr.upper(), (0.0, 0.0))


# Severity ladder by incidentType.
_INCIDENT_BASE_SEVERITY = {
    "Hurricane":           9,
    "Tropical Storm":      7,
    "Coastal Storm":       6,
    "Severe Storm":        6,
    "Severe Storm(s)":     6,
    "Severe Ice Storm":    7,
    "Tornado":             7,
    "Flood":               7,
    "Earthquake":          9,
    "Tsunami":             9,
    "Volcano":             9,
    "Volcanic Eruption":   9,
    "Wildfire":            7,
    "Fire":                6,
    "Snowstorm":           5,
    "Winter Storm":        5,
    "Mud/Landslide":       6,
    "Drought":             5,
    "Other":               4,
    "Pandemic":            8,
    "Biological":          7,
    "Chemical":            7,
    "Toxic Substances":    6,
    "Dam/Levee Break":     8,
    "Terrorist":           9,
    "Human Cause":         5,
}

# DR-type modifiers.
#   DR = Major Disaster Declaration (highest)
#   EM = Emergency Declaration (lower threshold — president can act faster)
#   FM = Fire Management Assistance (most common, very specific scope)
_DECL_TYPE_BOOST = {
    "DR": 1,
    "EM": 0,
    "FM": -1,
}


def _severity(incident_type: Optional[str], decl_type: Optional[str]) -> int:
    base = _INCIDENT_BASE_SEVERITY.get((incident_type or "").strip(), 5)
    boost = _DECL_TYPE_BOOST.get((decl_type or "").upper(), 0)
    return max(1, min(10, base + boost))


# ─── Ingester ─────────────────────────────────────────────────────────────


class OpenFemaIngester(Ingester):
    layer = "fema_declarations"
    source = "FEMA Disaster Declarations"
    source_id = "openfema"
    poll_interval_sec = 1800.0   # 30 min

    BASE_URL = "https://www.fema.gov/api/open/v2/DisasterDeclarationsSummaries"
    # 60-day window covers ongoing events; older declarations don't change
    # operationally.
    WINDOW_DAYS = 60
    # Cap rows pulled per cycle. With $top + the date filter we typically
    # see 50–500 rows (one per county-area per disaster). Group → ~5-30
    # disasters.
    MAX_ROWS = 1000
    UA = "FulcrumGlassbox/2.0 (mewrcreate.com/glassbox; hello@mewrcreate.com)"

    def _url(self) -> str:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=self.WINDOW_DAYS)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        return (
            f"{self.BASE_URL}"
            f"?$filter=declarationDate ge '{cutoff}'"
            f"&$top={self.MAX_ROWS}"
            f"&$orderby=declarationDate desc"
        )

    async def fetch(self) -> List[Dict[str, Any]]:
        timeout = aiohttp.ClientTimeout(total=60)
        headers = {"User-Agent": self.UA, "Accept": "application/json"}
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as s:
            async with s.get(self._url()) as r:
                r.raise_for_status()
                data = await r.json()
        if not isinstance(data, dict):
            self.log.warning(f"[openfema] expected dict, got {type(data).__name__}")
            return []
        return data.get("DisasterDeclarationsSummaries") or []

    def normalize(self, raw_items: List[Dict[str, Any]]) -> List[GlassboxEvent]:
        # Group raw rows by disasterNumber. The endpoint returns one row per
        # (disaster, county-area) — we collapse to one event per disaster.
        by_disaster: Dict[int, Dict[str, Any]] = {}
        for it in raw_items:
            dn = it.get("disasterNumber")
            if dn is None:
                continue
            try:
                dn_int = int(dn)
            except (TypeError, ValueError):
                continue
            slot = by_disaster.get(dn_int)
            if slot is None:
                # First-seen row carries the canonical fields; subsequent
                # rows just contribute to the area count.
                by_disaster[dn_int] = {
                    "disaster": it,
                    "area_count": 1,
                }
            else:
                slot["area_count"] += 1

        out: List[GlassboxEvent] = []
        sentinel_count = 0

        for dn_int, slot in by_disaster.items():
            it = slot["disaster"]
            state = (it.get("state") or "").strip()
            decl_type = (it.get("declarationType") or "").strip().upper()
            incident_type = (it.get("incidentType") or "").strip()
            decl_title = (it.get("declarationTitle") or "").strip()
            decl_date = it.get("declarationDate") or ""
            fema_id = it.get("femaDeclarationString") or f"DR-{dn_int}-{state}"

            severity = _severity(incident_type, decl_type)
            lat, lng = _state_coords(state)
            if lat == 0.0 and lng == 0.0:
                sentinel_count += 1
                geocode_quality = "needs_match"
            else:
                geocode_quality = "approximate"   # state-level centroid

            payload: Dict[str, Any] = {
                "disaster_number":     dn_int,
                "fema_declaration":    fema_id,
                "state":               state or None,
                "declaration_type":    decl_type or None,
                "incident_type":       incident_type or None,
                "declaration_title":   decl_title or None,
                "declaration_date":    decl_date or None,
                "incident_begin_date": it.get("incidentBeginDate"),
                "incident_end_date":   it.get("incidentEndDate"),
                "designated_area_count": slot["area_count"],
                "ih_program":          bool(it.get("ihProgramDeclared")),
                "ia_program":          bool(it.get("iaProgramDeclared")),
                "pa_program":          bool(it.get("paProgramDeclared")),
                "hm_program":          bool(it.get("hmProgramDeclared")),
                "fema_region":         it.get("region"),
                "_attribution":        "Disaster declarations: FEMA",
            }

            title = (
                f"{incident_type or 'Disaster'} — "
                f"{decl_title or fema_id}"
                + (f" ({state})" if state else "")
            )

            mtags: List[str] = []
            if severity >= 8:
                mtags.append("hazard:major_disaster")
            sev_market = max(0, severity - 4) if mtags else 0

            out.append(GlassboxEvent(
                layer=self.layer,
                external_id=f"openfema:{fema_id}",
                kind="fema_declaration",
                lat=float(lat),
                lng=float(lng),
                ts=decl_date or datetime.now(timezone.utc).isoformat(),
                severity=severity,
                source=self.source,
                payload=payload,
                domain="atmospheric",
                geocode_quality=geocode_quality,
                # Disasters stay relevant for weeks. 7d half-life keeps the
                # ranking signal high during the active window.
                decay_half_life_min=10080,
                market_tags=mtags,
                severity_for_market=sev_market,
            ))

        if sentinel_count:
            self.log.info(
                f"[openfema] {sentinel_count} declarations emitted at sentinel "
                f"(0,0) — state not in _STATE_CENTROIDS"
            )
        return out
