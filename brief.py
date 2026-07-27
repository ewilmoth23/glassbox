"""
Brief generator — turn the /api/v1/viewport JSON into a short factual summary.

Phase 1.5 of the V2 plan called for a 200-word LLM brief via Ollama. This
implementation is the deterministic-template version: every number in the
output traces directly to a count of rows in the input. No hallucination,
no fabrication (per Rule 2.3 in CLAUDE_CODE_GLASSBOX.md), <5ms generation,
zero external dependency. An LLM-narrative path can be added later behind
an opt-in flag without changing this default.

Output shape:
  - Leads with cross-domain proximity findings (the killer signal — that's
    where Glassbox earns its keep against single-domain trackers)
  - Then entity counts by type, with military / emergency callouts
  - Then events grouped by event_type
  - Concise, no marketing fluff. Reads like an analyst's situation note.

Cached for 5 minutes per (bbox, types, time_from-rounded-to-5min) so a
common dashboard refresh on the same region is a sub-millisecond hit.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple


# ─── Cache (process-local, thread-safe) ───────────────────────────────────


class _BriefCache:
    """In-memory cache with TTL. Process-local — fine for v1.0 single-server.

    Keyed on a normalized tuple of (bbox-rounded, types, time-window-bucket)
    so semantically-equivalent queries share a hit. Thread-safe so multiple
    request handlers can hit it concurrently."""

    def __init__(self, ttl_seconds: float = 300.0):
        self._ttl = ttl_seconds
        self._store: Dict[Tuple, Tuple[float, str]] = {}
        self._lock = threading.Lock()

    @staticmethod
    def make_key(viewport_response: dict) -> Tuple:
        meta = viewport_response.get("meta", {}) or {}
        bbox = meta.get("bbox") or [0, 0, 0, 0]
        # Round bbox to 0.1 degree so micro-changes in cursor position
        # still hit the same cache entry.
        bbox_r = tuple(round(float(b), 1) for b in bbox)
        types = tuple(sorted(meta.get("types") or []))
        # Round time_from to a 5-min bucket — a dashboard polling every minute
        # gets the same brief for ~5 min then a fresh one.
        tf = (meta.get("time_from") or "")[:16]   # 'YYYY-MM-DDTHH:MM' (drop subseconds)
        return (bbox_r, types, tf)

    def get_or_compute(self, key: Tuple, compute: Callable[[], str]) -> str:
        with self._lock:
            entry = self._store.get(key)
            now = time.time()
            if entry is not None:
                ts, value = entry
                if now - ts <= self._ttl:
                    return value
        # Compute outside lock so a slow producer doesn't block other readers
        value = compute()
        with self._lock:
            self._store[key] = (time.time(), value)
        return value


# Module-level singleton used by the FastAPI route. Tests construct their
# own _BriefCache instances to control TTL.
brief_cache = _BriefCache(ttl_seconds=300.0)


# ─── Brief generation ─────────────────────────────────────────────────────


_TYPE_LABELS = {
    "aircraft": "aircraft",
    "vessel": "vessels",
    "satellite": "satellites",
}

_EVENT_TYPE_LABELS = {
    "usgs_quake": "USGS earthquake",
    "emsc_quake": "EMSC earthquake",
    "noaa_alert": "NOAA weather alert",
    "gdelt_topical": "GDELT news event",
    "nasa_firms": "NASA active-fire detection",
    "nasa_eonet": "NASA natural event",
    "detected_proximity": "cross-domain proximity finding",
    "sanctioned_vessel_underway": "sanctioned-vessel-underway alert",
    "sanctioned_vessel_multijurisdictional": "multi-jurisdictional sanctioned-vessel alert",
    "shadow_fleet_cluster": "shadow-fleet cluster alert",
    "dark_vessel_detected": "dark-vessel detection",
}

# event_type values that we surface as TOP-LINE alerts (above proximity).
# These are the highest-leverage "before MSM" signals.
_TIER1_ALERT_EVENT_TYPES = (
    "sanctioned_vessel_underway",
    "dark_vessel_detected",
    "swpc_alert",
    "military_aircraft_underway",
    "loitering_detected",
    "rendezvous_detected",
    "aircraft_in_sanctioned_airspace",
    # Combined-signal CRITICAL tier — surface above all others.
    "shadow_fleet_cluster",
    "sanctioned_vessel_multijurisdictional",
    "sanctioned_vessel_went_dark",
    "sanctioned_vessel_rendezvous",
)


def _format_bbox(bbox: List[float]) -> str:
    if not bbox or len(bbox) != 4:
        return "the queried region"
    w, s, e, n = bbox
    return f"bbox [{w:.1f}, {s:.1f}, {e:.1f}, {n:.1f}]"


def _format_count(n: int, singular: str, plural: Optional[str] = None) -> str:
    if n == 0:
        return f"no {plural or singular + 's'}"
    if n == 1:
        return f"1 {singular}"
    return f"{n} {plural or singular + 's'}"


def _proximity_findings(events: List[dict]) -> List[dict]:
    return [e for e in events if e.get("event_type") == "detected_proximity"]


def _tier1_alert_events(events: List[dict]) -> List[dict]:
    """Tier-1 alerts: sanctioned vessels currently broadcasting AIS, and
    vessels that have gone dark while underway. The highest-leverage
    'before MSM' signals — surfaced ABOVE proximity findings."""
    return [e for e in events if e.get("event_type") in _TIER1_ALERT_EVENT_TYPES]


def _non_proximity_events(events: List[dict]) -> List[dict]:
    """Events not covered by the proximity / tier-1 paths above."""
    return [
        e for e in events
        if e.get("event_type") != "detected_proximity"
        and e.get("event_type") not in _TIER1_ALERT_EVENT_TYPES
    ]


def _sanction_callouts(events: List[dict]) -> str:
    """One-line callout for sanctioned vessels currently broadcasting AIS.
    Distinguishes IMO-precise matches (zero false positives) from name-fuzzy
    matches (lower confidence). Lists up to 4 representative names."""
    sanctions = [e for e in events if e.get("event_type") == "sanctioned_vessel_underway"]
    if not sanctions:
        return ""

    n_imo = sum(1 for e in sanctions if e.get("event_subtype") == "imo_match")
    n_name = sum(1 for e in sanctions if e.get("event_subtype") == "name_match")

    # Prefer IMO matches in the sample (higher confidence).
    sanctions_sorted = sorted(
        sanctions,
        key=lambda e: (0 if e.get("event_subtype") == "imo_match" else 1,
                       -(e.get("severity") or 0)),
    )
    sample_names: List[str] = []
    for e in sanctions_sorted[:4]:
        props = e.get("properties") or {}
        name = props.get("live_vessel_name") or e.get("title", "").replace(
            "Sanctioned vessel underway: ", ""
        ).replace(" [IMO match]", "")
        if name and name not in sample_names:
            sample_names.append(name)

    confidence_note = ""
    if n_imo and n_name:
        confidence_note = f" ({n_imo} IMO-confirmed, {n_name} name-fuzzy)"
    elif n_imo:
        confidence_note = f" (IMO-confirmed)"
    elif n_name:
        confidence_note = f" (name-fuzzy match — verify via IMO)"

    head = (
        f"ALERT — {len(sanctions)} OFAC-sanctioned vessel"
        f"{'s' if len(sanctions) != 1 else ''} currently broadcasting AIS"
        f"{confidence_note}"
    )
    if sample_names:
        head += ": " + ", ".join(sample_names[:3])
        if len(sanctions) > 3:
            head += f", and {len(sanctions) - len(sample_names[:3])} more"
    return head + "."


def _dark_ship_callouts(events: List[dict]) -> str:
    """One-line callout for vessels that went dark while underway.
    Sorted by hours_dark descending (longest-dark first)."""
    dark = [e for e in events if e.get("event_type") == "dark_vessel_detected"]
    if not dark:
        return ""

    # Sort by hours_dark desc — longest-silent vessels first
    def _hours(e: dict) -> float:
        try:
            return float((e.get("properties") or {}).get("hours_dark") or 0)
        except (TypeError, ValueError):
            return 0.0

    dark_sorted = sorted(dark, key=lambda e: -_hours(e))
    samples: List[str] = []
    for e in dark_sorted[:3]:
        props = e.get("properties") or {}
        # title format: "Vessel went dark: NAME"
        title = e.get("title") or ""
        name = title.replace("Vessel went dark: ", "")
        h = _hours(e)
        if name:
            if h >= 24:
                samples.append(f"{name} ({h / 24:.1f}d)")
            else:
                samples.append(f"{name} ({h:.0f}h)")

    head = (
        f"ALERT — {len(dark)} vessel{'s' if len(dark) != 1 else ''} "
        f"went dark while underway"
    )
    if samples:
        head += ": " + ", ".join(samples)
        if len(dark) > 3:
            head += f", +{len(dark) - 3} more"
    return head + "."


def _volcanic_callouts(events: List[dict]) -> str:
    """One-line callout for currently-elevated volcanoes (USGS VHP).
    Sorts by severity desc; groups by alert_level (WATCH/ADVISORY/etc.)
    and lists up to 3 names with their color codes."""
    vols = [e for e in events if e.get("event_type") == "volcanic_alert"]
    if not vols:
        return ""

    by_level: Dict[str, int] = {}
    for e in vols:
        lvl = ((e.get("properties") or {}).get("alert_level")
               or (e.get("event_subtype") or "?")).upper()
        by_level[lvl] = by_level.get(lvl, 0) + 1

    level_summary = ", ".join(
        f"{cnt} {lvl}" for lvl, cnt in
        sorted(by_level.items(), key=lambda kv: -kv[1])
    )

    sorted_vols = sorted(vols, key=lambda e: -(e.get("severity") or 0))
    sample = []
    for e in sorted_vols[:3]:
        p = e.get("properties") or {}
        nm = p.get("volcano_name") or "?"
        cc = (p.get("color_code") or "?").upper()
        sample.append(f"{nm} ({cc})")

    head = (
        f"ALERT — {len(vols)} elevated volcano"
        f"{'es' if len(vols) != 1 else ''} ({level_summary})"
    )
    if sample:
        head += ": " + ", ".join(sample)
    if len(vols) > 3:
        head += f", +{len(vols) - 3} more"
    return head + "."


def _gdacs_callouts(events: List[dict]) -> str:
    """One-line callout for GDACS global-disaster alerts. Groups by
    severity tier (Red/Orange/Green) and lists the top 2 by name."""
    gd = [e for e in events if e.get("event_type") == "gdacs_alert"]
    if not gd:
        return ""

    by_tier: Dict[str, int] = {}
    for e in gd:
        tier = (e.get("event_subtype") or "?").lower()
        by_tier[tier] = by_tier.get(tier, 0) + 1

    tier_summary = ", ".join(
        f"{cnt} {t}" for t, cnt in
        sorted(by_tier.items(), key=lambda kv: -kv[1])
    )

    sorted_gd = sorted(gd, key=lambda e: -(e.get("severity") or 0))
    sample = []
    for e in sorted_gd[:2]:
        p = e.get("properties") or {}
        nm = p.get("event_name") or e.get("title") or "?"
        sample.append(str(nm)[:60])

    head = (
        f"ALERT — {len(gd)} GDACS disaster alert"
        f"{'s' if len(gd) != 1 else ''} ({tier_summary})"
    )
    if sample:
        head += ": " + "; ".join(sample)
    return head + "."


def _swpc_callouts(events: List[dict]) -> str:
    """One-line callout for active NOAA SWPC space-weather alerts.
    Surfaces the highest-severity alert and groups by category
    (geomagnetic, radio blackout, solar radiation)."""
    swpc = [e for e in events if e.get("event_type") == "swpc_alert"]
    if not swpc:
        return ""

    # Group by event_subtype (category) and pick the highest-severity alert overall.
    by_category: Dict[str, int] = {}
    for e in swpc:
        cat = e.get("event_subtype") or "space_weather"
        by_category[cat] = by_category.get(cat, 0) + 1

    # Pick highest-severity alert for the headline sample
    sorted_swpc = sorted(swpc, key=lambda e: -(e.get("severity") or 0))
    top = sorted_swpc[0]
    top_props = top.get("properties") or {}
    top_headline = top_props.get("headline") or top.get("title") or ""
    # Trim "WARNING:" / "ALERT:" prefix for brevity in the brief
    for prefix in ("WARNING: ", "ALERT: ", "WATCH: ", "SUMMARY: ", "EXTENDED WARNING: "):
        if top_headline.startswith(prefix):
            top_headline = top_headline[len(prefix):]
            break

    category_summary = ", ".join(
        f"{cnt} {cat.replace('_', ' ')}"
        for cat, cnt in sorted(by_category.items(), key=lambda kv: -kv[1])
    )

    head = (
        f"ALERT — {len(swpc)} active NOAA SWPC space-weather alert"
        f"{'s' if len(swpc) != 1 else ''} ({category_summary})"
    )
    if top_headline:
        head += f"; top: {top_headline[:120]}"
    return head + "."


def _military_flight_callouts(events: List[dict]) -> str:
    """One-line callout for live military aircraft. Groups by callsign-prefix
    family (e.g. VIPR, GAF, SHWK) and lists the top families + sample names."""
    mil = [e for e in events if e.get("event_type") == "military_aircraft_underway"]
    if not mil:
        return ""

    # Group by event_subtype (callsign prefix family)
    by_family: Dict[str, int] = {}
    for e in mil:
        fam = e.get("event_subtype") or "unknown"
        by_family[fam] = by_family.get(fam, 0) + 1

    family_summary = ", ".join(
        f"{cnt} {fam}" for fam, cnt in
        sorted(by_family.items(), key=lambda kv: -kv[1])[:5]
    )

    # Sample callsigns: take first 4 distinct
    sample_names: List[str] = []
    for e in mil:
        props = e.get("properties") or {}
        cs = props.get("callsign")
        if cs and cs not in sample_names:
            sample_names.append(cs)
        if len(sample_names) >= 4:
            break

    head = (
        f"ALERT — {len(mil)} military aircraft broadcasting ADS-B"
        f" ({family_summary})"
    )
    if sample_names:
        head += ": " + ", ".join(sample_names[:3])
        if len(sample_names) >= 4:
            head += f", and {len(mil) - 3} more"
        elif len(mil) > 3:
            head += f", +{len(mil) - 3} more"
    return head + "."


def _loitering_callouts(events: List[dict]) -> str:
    """One-line callout for entities loitering (anchored-but-not-anchored).
    Lists vessel/aircraft splits and longest-loitering names."""
    loiter = [e for e in events if e.get("event_type") == "loitering_detected"]
    if not loiter:
        return ""

    vessel_count = sum(1 for e in loiter if e.get("event_subtype") == "vessel")
    aircraft_count = sum(1 for e in loiter if e.get("event_subtype") == "aircraft")

    # Sort by span_hours descending — longest-loitering first
    def _span_h(e: dict) -> float:
        try:
            return float((e.get("properties") or {}).get("span_hours") or 0)
        except (TypeError, ValueError):
            return 0.0

    loiter_sorted = sorted(loiter, key=lambda e: -_span_h(e))

    sample_names: List[str] = []
    for e in loiter_sorted[:3]:
        title = e.get("title") or ""
        name = title.replace("Loitering detected: ", "")
        h = _span_h(e)
        if name:
            sample_names.append(f"{name} ({h:.1f}h)")

    breakdown_parts = []
    if vessel_count:
        breakdown_parts.append(f"{vessel_count} vessel{'s' if vessel_count != 1 else ''}")
    if aircraft_count:
        breakdown_parts.append(f"{aircraft_count} aircraft")
    breakdown = ", ".join(breakdown_parts) or f"{len(loiter)} entity"

    head = f"ALERT — {breakdown} loitering (small-radius / long-duration)"
    if sample_names:
        head += ": " + ", ".join(sample_names)
        if len(loiter) > 3:
            head += f", +{len(loiter) - 3} more"
    return head + "."


def _rendezvous_callouts(events: List[dict]) -> str:
    """One-line callout for entity-pair rendezvous (ship-to-ship transfers,
    in-flight refueling, hover-and-board operations). Lists vessel-vessel
    count separately since it's the highest-signal sanctions-evasion case."""
    rdv = [e for e in events if e.get("event_type") == "rendezvous_detected"]
    if not rdv:
        return ""

    # Count by pair_kind. vessel_vessel is sanctions-evasion-shaped.
    by_kind: Dict[str, int] = {}
    for e in rdv:
        kind = e.get("event_subtype") or "unknown"
        # Normalize aircraft_vessel / vessel_aircraft to one bucket
        if "aircraft" in kind and "vessel" in kind:
            kind = "aircraft_vessel"
        by_kind[kind] = by_kind.get(kind, 0) + 1

    # Sample by highest severity (closest pairs first)
    rdv_sorted = sorted(rdv, key=lambda e: -(e.get("severity") or 0))
    samples: List[str] = []
    for e in rdv_sorted[:3]:
        title = e.get("title") or ""
        # Title format: "Rendezvous: NAME_A near NAME_B (Xm)"
        s = title.replace("Rendezvous: ", "").strip()
        if s:
            samples.append(s)

    parts = []
    if by_kind.get("vessel_vessel"):
        parts.append(f"{by_kind['vessel_vessel']} vessel-vessel")
    if by_kind.get("aircraft_vessel"):
        parts.append(f"{by_kind['aircraft_vessel']} aircraft-vessel")
    if by_kind.get("aircraft_aircraft"):
        parts.append(f"{by_kind['aircraft_aircraft']} aircraft-aircraft")
    breakdown = ", ".join(parts) or f"{len(rdv)} pair"

    head = f"ALERT — {breakdown} rendezvous (close-proximity, low-velocity)"
    if samples:
        head += ": " + "; ".join(samples)
        if len(rdv) > 3:
            head += f"; +{len(rdv) - 3} more"
    return head + "."


def _sanctioned_airspace_callouts(events: List[dict]) -> str:
    """One-line callout for aircraft transiting sanctioned airspace
    (Iran FIR, North Korea, Crimea, etc.). Groups by zone."""
    flights = [e for e in events
               if e.get("event_type") == "aircraft_in_sanctioned_airspace"]
    if not flights:
        return ""

    by_zone: Dict[str, int] = {}
    for e in flights:
        zone = e.get("event_subtype") or "unknown"
        by_zone[zone] = by_zone.get(zone, 0) + 1

    # Highest-severity sample first (NK > crimea > iran > others)
    flights_sorted = sorted(flights, key=lambda e: -(e.get("severity") or 0))
    samples: List[str] = []
    for e in flights_sorted[:3]:
        props = e.get("properties") or {}
        cs = props.get("callsign") or props.get("icao24") or "unknown"
        zone = e.get("event_subtype") or "?"
        samples.append(f"{cs} ({zone})")

    zone_summary = ", ".join(
        f"{cnt} {zone.replace('_', ' ')}"
        for zone, cnt in sorted(by_zone.items(), key=lambda kv: -kv[1])[:5]
    )
    head = (
        f"ALERT — {len(flights)} aircraft in sanctioned airspace"
        f" ({zone_summary})"
    )
    if samples:
        head += ": " + ", ".join(samples)
        if len(flights) > 3:
            head += f", +{len(flights) - 3} more"
    return head + "."


def _critical_combined_callouts(events: List[dict]) -> str:
    """The TOP-OF-BRIEF CRITICAL line: shadow-fleet clusters, multi-
    jurisdictional sanctions matches, sanctioned vessels going dark,
    and sanctioned vessels in rendezvous. These are the combined-signal
    findings that deserve the loudest siren — sanctions evasion in real
    time, pulled from the JOIN of the multi-authority sanctions index
    against live AIS feeds and dark-vessel / rendezvous events.
    """
    sf = [e for e in events if e.get("event_type") == "shadow_fleet_cluster"]
    mj = [e for e in events
          if e.get("event_type") == "sanctioned_vessel_multijurisdictional"]
    sd = [e for e in events if e.get("event_type") == "sanctioned_vessel_went_dark"]
    sr = [e for e in events if e.get("event_type") == "sanctioned_vessel_rendezvous"]
    if not sf and not mj and not sd and not sr:
        return ""

    parts: List[str] = []
    if sf:
        # Shadow-fleet clusters lead — strongest single operational signal.
        # Sort by cluster_size desc so the biggest fleet appears first.
        def _sz(e: dict) -> int:
            try:
                return int((e.get("properties") or {}).get("cluster_size") or 0)
            except (TypeError, ValueError):
                return 0
        sf_sorted = sorted(sf, key=lambda e: -_sz(e))
        n_large = sum(1 for e in sf if _sz(e) >= 6)
        n_fleet = sum(1 for e in sf if 4 <= _sz(e) <= 5)
        n_cluster = len(sf) - n_large - n_fleet
        breakdown = []
        if n_large:   breakdown.append(f"{n_large} large-fleet")
        if n_fleet:   breakdown.append(f"{n_fleet} fleet")
        if n_cluster: breakdown.append(f"{n_cluster} cluster")
        sample = []
        for e in sf_sorted[:2]:
            props = e.get("properties") or {}
            size = _sz(e)
            authorities = props.get("authorities") or []
            if isinstance(authorities, str):
                try:
                    import json as _json
                    authorities = _json.loads(authorities)
                except Exception:
                    authorities = []
            auth_short = []
            for a in authorities:
                if "OFAC" in a:        auth_short.append("OFAC")
                elif "OFSI" in a or a == "UK OFSI": auth_short.append("UK")
                elif "CFSP" in a or a == "EU CFSP": auth_short.append("EU")
                else: auth_short.append(a)
            sample.append(f"{size}-vessel [{'+'.join(auth_short) or '?'}]")
        sf_str = (
            f"{len(sf)} shadow-fleet cluster"
            f"{'s' if len(sf) != 1 else ''}"
            + ((" (" + ", ".join(breakdown) + ")") if breakdown else "")
            + ((": " + "; ".join(sample)) if sample else "")
            + (f"; +{len(sf) - 2} more" if len(sf) > 2 else "")
        )
        parts.append(sf_str)
    if mj:
        # Tri-listed entries lead — they're the strongest single signal in
        # the system (a vessel that 3 independent authorities have
        # converged on can't realistically be a coincidence).
        def _ac(e: dict) -> int:
            try:
                return int((e.get("properties") or {}).get("authority_count") or 0)
            except (TypeError, ValueError):
                return 0
        mj_sorted = sorted(mj, key=lambda e: -_ac(e))
        n_tri = sum(1 for e in mj if _ac(e) >= 3)
        n_dual = len(mj) - n_tri
        sample = []
        for e in mj_sorted[:3]:
            props = e.get("properties") or {}
            name = props.get("live_vessel_name") or props.get("mmsi") or "?"
            authorities = props.get("authorities") or []
            if isinstance(authorities, str):
                # JSONB sometimes round-trips as a JSON string — defensive
                try:
                    import json as _json
                    authorities = _json.loads(authorities)
                except Exception:
                    authorities = []
            # Compact authority labels for the brief line
            auth_short = []
            for a in authorities:
                if "OFAC" in a:
                    auth_short.append("OFAC")
                elif "OFSI" in a or a == "UK OFSI":
                    auth_short.append("UK")
                elif "CFSP" in a or a == "EU CFSP":
                    auth_short.append("EU")
                else:
                    auth_short.append(a)
            sample.append(f"{name} [{'+'.join(auth_short)}]")
        breakdown = []
        if n_tri:
            breakdown.append(f"{n_tri} tri-listed")
        if n_dual:
            breakdown.append(f"{n_dual} dual-listed")
        mj_str = (
            f"{len(mj)} multi-jurisdictional sanctioned vessel"
            f"{'s' if len(mj) != 1 else ''}"
            + ((" (" + ", ".join(breakdown) + ")") if breakdown else "")
            + ((": " + "; ".join(sample)) if sample else "")
            + (f"; +{len(mj) - 3} more" if len(mj) > 3 else "")
        )
        parts.append(mj_str)
    if sd:
        # Pull top samples (highest hours_dark first)
        def _hd(e: dict) -> float:
            try:
                return float((e.get("properties") or {}).get("hours_dark") or 0)
            except (TypeError, ValueError):
                return 0.0
        sd_sorted = sorted(sd, key=lambda e: -_hd(e))
        sample = []
        for e in sd_sorted[:3]:
            props = e.get("properties") or {}
            name = props.get("live_vessel_name") or props.get("mmsi") or "?"
            h = _hd(e)
            sample.append(f"{name} ({h:.0f}h)")
        sd_str = (
            f"{len(sd)} sanctioned vessel{'s' if len(sd) != 1 else ''} went dark"
            + ((": " + ", ".join(sample)) if sample else "")
            + (f", +{len(sd) - 3} more" if len(sd) > 3 else "")
        )
        parts.append(sd_str)
    if sr:
        # Highlight both-sanctioned cases first (highest severity)
        sr_sorted = sorted(sr, key=lambda e: -(e.get("severity") or 0))
        n_both = sum(1 for e in sr if e.get("event_subtype") == "both_sanctioned")
        n_one  = len(sr) - n_both
        sample = []
        for e in sr_sorted[:2]:
            props = e.get("properties") or {}
            a = props.get("a_name") or props.get("a_mmsi") or "?"
            b = props.get("b_name") or props.get("b_mmsi") or "?"
            d = props.get("distance_m")
            sample.append(f"{a} ↔ {b} ({d}m)" if d is not None else f"{a} ↔ {b}")
        breakdown = []
        if n_both:
            breakdown.append(f"{n_both} BOTH OFAC")
        if n_one:
            breakdown.append(f"{n_one} one OFAC")
        sr_str = (
            f"{len(sr)} sanctioned-vessel rendezvous"
            + ((" (" + ", ".join(breakdown) + ")") if breakdown else "")
            + ((": " + "; ".join(sample)) if sample else "")
            + (f"; +{len(sr) - 2} more" if len(sr) > 2 else "")
        )
        parts.append(sr_str)

    return "*** CRITICAL *** — " + "; ".join(parts) + "."


def _tier1_summary(events: List[dict]) -> List[str]:
    """Top-line alerts. CRITICAL combined-signals lead, then per-signal."""
    out: List[str] = []
    crit = _critical_combined_callouts(events)
    if crit:
        out.append(crit)
    s = _sanction_callouts(events)
    if s:
        out.append(s)
    d = _dark_ship_callouts(events)
    if d:
        out.append(d)
    m = _military_flight_callouts(events)
    if m:
        out.append(m)
    sa = _sanctioned_airspace_callouts(events)
    if sa:
        out.append(sa)
    rd = _rendezvous_callouts(events)
    if rd:
        out.append(rd)
    lo = _loitering_callouts(events)
    if lo:
        out.append(lo)
    sw = _swpc_callouts(events)
    if sw:
        out.append(sw)
    vo = _volcanic_callouts(events)
    if vo:
        out.append(vo)
    gd = _gdacs_callouts(events)
    if gd:
        out.append(gd)
    return out


def _aircraft_callouts(entities: List[dict]) -> List[str]:
    """Detect mil + emergency aircraft. Returns headline strings, or [] if none."""
    aircraft = [e for e in entities if e.get("entity_type") == "aircraft"]
    out: List[str] = []

    emergency = [
        e for e in aircraft
        if (e.get("properties") or {}).get("emergency")
    ]
    if emergency:
        names = [e["display_name"] or e["canonical_id"] for e in emergency[:3]]
        out.append(
            f"{len(emergency)} emergency-squawk aircraft: {', '.join(names)}"
            + ("…" if len(emergency) > 3 else "")
        )

    military = [
        e for e in aircraft
        if (e.get("properties") or {}).get("military")
    ]
    if military:
        names = [e["display_name"] or e["canonical_id"] for e in military[:3]]
        out.append(
            f"{len(military)} military aircraft: {', '.join(names)}"
            + ("…" if len(military) > 3 else "")
        )
    return out


def _entity_summary(entities: List[dict]) -> str:
    """One sentence per entity_type with non-zero count."""
    by_type: Dict[str, int] = {}
    for e in entities:
        t = e.get("entity_type") or "entity"
        by_type[t] = by_type.get(t, 0) + 1

    parts: List[str] = []
    for entity_type, n in sorted(by_type.items(), key=lambda kv: -kv[1]):
        label = _TYPE_LABELS.get(entity_type, entity_type)
        parts.append(_format_count(n, label.rstrip("s"), label))

    if not parts:
        return "No entities currently in this region."
    return ", ".join(parts) + " active."


def _event_summary(events: List[dict]) -> str:
    """Group events by event_type and event_subtype, surface the highest-severity sample."""
    non_prox = _non_proximity_events(events)
    if not non_prox:
        return ""
    by_type: Dict[str, List[dict]] = {}
    for e in non_prox:
        by_type.setdefault(e.get("event_type") or "other", []).append(e)

    parts: List[str] = []
    for event_type, group in sorted(by_type.items(), key=lambda kv: -len(kv[1])):
        label = _EVENT_TYPE_LABELS.get(event_type, event_type)
        n = len(group)
        # Highest-severity sample
        sorted_g = sorted(
            group,
            key=lambda e: -(e.get("severity") or 0),
        )
        sample_title = sorted_g[0].get("title") or ""
        sample_title_short = sample_title[:80] + ("…" if len(sample_title) > 80 else "")
        if sample_title_short:
            parts.append(f"{n} {label}{'s' if n != 1 else ''} (sample: {sample_title_short})")
        else:
            parts.append(f"{n} {label}{'s' if n != 1 else ''}")

    return "; ".join(parts) + "."


def _proximity_summary(findings: List[dict]) -> str:
    """Lead-line for cross-domain proximity. The killer signal — comes first."""
    if not findings:
        return ""
    n = len(findings)
    # Group by event_subtype (e.g. 'aircraft_usgs_quake', 'satellite_nasa_eonet')
    by_subtype: Dict[str, int] = {}
    for f in findings:
        st = f.get("event_subtype") or "unspecified"
        by_subtype[st] = by_subtype.get(st, 0) + 1

    # Highest-priority sample: pick first finding's title
    sample = findings[0]
    sample_title = sample.get("title") or ""
    sample_title_short = sample_title[:120] + ("…" if len(sample_title) > 120 else "")

    subtype_summary = ", ".join(
        f"{cnt} {st}" for st, cnt in sorted(by_subtype.items(), key=lambda kv: -kv[1])[:3]
    )
    parts = [f"{n} cross-domain proximity finding{'s' if n != 1 else ''} ({subtype_summary})"]
    if sample_title_short:
        parts.append(f"e.g. {sample_title_short}")
    return ". ".join(parts) + "."


def generate_brief(viewport_response: dict) -> str:
    """Produce a short factual situation note from a /api/v1/viewport result.

    Output is deterministic, ~50–300 words, no LLM. Reads as:

        [BBOX, time window]: [N entities active]. [N proximity findings:
        sample]. [N events by type: sample]. [Mil/emergency callouts].
    """
    meta = viewport_response.get("meta") or {}
    entities = viewport_response.get("entities") or []
    events = viewport_response.get("events") or []

    bbox_str = _format_bbox(meta.get("bbox") or [])
    types = meta.get("types") or []

    if not entities and not events:
        return f"No entities or events in {bbox_str} for the queried time window. Types requested: {', '.join(types) or 'none'}."

    sentences: List[str] = []

    # Lead with TIER-1 alerts: sanctioned vessels currently broadcasting,
    # and vessels that went dark while underway. These are the highest-
    # leverage "before MSM" signals — they go above everything else.
    sentences.extend(_tier1_summary(events))

    # Then cross-domain proximity (the killer signal for tactical work)
    proximity = _proximity_findings(events)
    if proximity:
        sentences.append(_proximity_summary(proximity))

    # Entity overview
    if entities:
        sentences.append(_entity_summary(entities))

    # Aircraft callouts (emergency first, then military)
    callouts = _aircraft_callouts(entities)
    sentences.extend(callouts)

    # Non-proximity / non-tier-1 events
    event_text = _event_summary(events)
    if event_text:
        sentences.append(event_text)

    head = f"In {bbox_str} for the past " + _format_window(meta) + ":"
    body = " ".join(sentences)
    return f"{head} {body}"


def _format_window(meta: dict) -> str:
    """Best-effort '~Nh' / '~Nm' string from time_from/time_to. Falls back to
    a literal range if parsing fails."""
    try:
        from datetime import datetime
        tf = meta.get("time_from") or ""
        tt = meta.get("time_to") or ""
        if not tf or not tt:
            return "queried window"
        # asyncpg / FastAPI gives ISO8601 with timezone; strip the 'Z' fallback case
        if tf.endswith("Z"):
            tf = tf[:-1] + "+00:00"
        if tt.endswith("Z"):
            tt = tt[:-1] + "+00:00"
        d_from = datetime.fromisoformat(tf)
        d_to = datetime.fromisoformat(tt)
        delta_min = max(0, int((d_to - d_from).total_seconds() / 60))
        if delta_min < 60:
            return f"{delta_min} min"
        if delta_min < 60 * 24:
            return f"{delta_min // 60}h"
        return f"{delta_min // (60 * 24)}d"
    except Exception:
        return "queried window"


def generate_brief_cached(viewport_response: dict) -> str:
    """Same as generate_brief but with the module-singleton cache applied."""
    key = brief_cache.make_key(viewport_response)
    return brief_cache.get_or_compute(key, lambda: generate_brief(viewport_response))


# ─── Optional LLM analyst-note layer ──────────────────────────────────────
#
# The LLM does NOT rewrite the deterministic brief — that would lose specifics
# (verified empirically: qwen2.5:14b at 37s/call dropped entity names + event
# magnitudes; the deterministic template is more faithful). Instead the LLM
# adds ONE sentence of analyst commentary on top, identifying the highest-
# priority signal in the data.
#
# Configurable via env:
#   GLASSBOX_BRIEF_LLM_MODEL       (default 'llama3.1:latest' — ~11s on Mac Mini)
#   GLASSBOX_BRIEF_LLM_TIMEOUT_SEC (default 15)
#   GLASSBOX_BRIEF_LLM_OLLAMA_URL  (default 'http://127.0.0.1:11434')
#
# On any error / timeout: returns the deterministic brief unchanged. The
# LLM is purely additive — never load-bearing.

import logging
import os

_brief_llm_log = logging.getLogger("brief.llm")

_DEFAULT_LLM_MODEL = "llama3.1:latest"
_DEFAULT_LLM_TIMEOUT_SEC = 15.0
_DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"

# Model selection benchmark (Mac Mini M4 Pro 24GB, 2026-05-08, /tmp/llm_brief_benchmark.py)
# Same prompt, same brief, scored on latency + specificity + entity-type correctness:
#
#   llama3.1:latest      0.8s warm   ✓ HA5231 ✓ M2.8 ✓ SUMBA ✓ correct  → DEFAULT
#   phi4:latest          17.2s       ✓ HA5231 ✓ M2.8 ✓ SUMBA ✓ correct  → too slow
#   qwen3.5:9b           22.3s       empty response                       → broken
#   qwen2.5:14b          37.0s       drops callsigns + magnitudes        → too slow + lossy
#   deepseek-r1:14b      29.8s       empty response (eval_count=200)     → broken
#   command-r:latest     >60s        timeout                             → too slow
#
# llama3.1's cold-call latency on this Mac is 5-15s (model load); once warm it's
# sub-second. The 5-min in-memory cache keeps the working set warm during a
# session. Re-run the benchmark if you add a new model: /tmp/llm_brief_benchmark.py


_ANALYST_NOTE_PROMPT_TEMPLATE = (
    "Below is a structured situational brief built from telemetry. In ONE "
    "sentence (max 25 words), tell an analyst what to investigate FIRST. "
    "Be specific — reference entity names, event titles, or magnitudes "
    "directly when relevant. Do NOT add numbers or names not in the data. "
    "Do NOT pad with adjectives. If nothing is notable, say 'No priority "
    "items.'\n\n"
    "DATA:\n{deterministic_brief}\n\n"
    "ANALYST NOTE:"
)


# LLM cache is separate from the deterministic cache — different output.
brief_llm_cache = _BriefCache(ttl_seconds=300.0)


async def generate_brief_llm(
    viewport_response: dict,
    *,
    model: Optional[str] = None,
    timeout_sec: Optional[float] = None,
    ollama_url: Optional[str] = None,
) -> str:
    """Return: deterministic brief + ' Analyst note: <LLM sentence>'.

    On any failure (timeout, HTTP error, empty response), returns just the
    deterministic brief — the LLM layer is opt-in and additive."""
    deterministic = generate_brief(viewport_response)

    model = model or os.environ.get("GLASSBOX_BRIEF_LLM_MODEL", _DEFAULT_LLM_MODEL)
    timeout_sec = timeout_sec or float(
        os.environ.get("GLASSBOX_BRIEF_LLM_TIMEOUT_SEC", _DEFAULT_LLM_TIMEOUT_SEC)
    )
    ollama_url = ollama_url or os.environ.get("GLASSBOX_BRIEF_LLM_OLLAMA_URL", _DEFAULT_OLLAMA_URL)

    note = await _ollama_analyst_note(
        deterministic_brief=deterministic,
        model=model,
        ollama_url=ollama_url,
        timeout_sec=timeout_sec,
    )
    if not note:
        return deterministic
    # Explicit (LLM) marker so consumers can distinguish the deterministic
    # facts (truthful by construction) from the LLM commentary (which can
    # hallucinate — verified empirically: llama3.1 swaps entity types
    # like saying "satellite HA5231" when HA5231 is an aircraft, even with
    # strict entity-type-preservation prompts).
    return f"{deterministic} Analyst note (LLM, may misclassify): {note}"


async def _ollama_analyst_note(
    *,
    deterministic_brief: str,
    model: str,
    ollama_url: str,
    timeout_sec: float,
) -> str:
    """Free-text analyst note via Ollama. Returns the trimmed sentence,
    or '' on any error (caller falls back to the deterministic brief).

    Routes through llm_ollama.generate_text which picks /api/generate
    or /v1/chat/completions based on GLASSBOX_OLLAMA_USE_CHAT_API.
    The fail-soft contract is preserved: on ANY error we return '' so
    a slow/down Ollama never blocks a viewport response. ``ollama_url``
    is now ignored — the helper reads OLLAMA_URL from env. Kept in the
    signature for backward-compat with one external caller; will
    delete on the next pass.
    """
    from llm_ollama import generate_text
    import os as _os
    prompt = _ANALYST_NOTE_PROMPT_TEMPLATE.format(deterministic_brief=deterministic_brief)
    # Honor caller's ollama_url override if it disagrees with the env
    # var — this is the legacy contract one upstream call site still
    # depends on.
    prior_env = _os.environ.get("OLLAMA_URL")
    override = ollama_url and ollama_url != prior_env
    if override:
        _os.environ["OLLAMA_URL"] = ollama_url
    try:
        text = await generate_text(
            prompt=prompt,
            model=model,
            task="brief_llm",
            temperature=0.3,
            max_tokens=80,
            timeout_total=timeout_sec,
        )
    except Exception as e:  # noqa: BLE001 — see comment in legacy code
        _brief_llm_log.warning(
            f"Ollama call failed ({type(e).__name__}: {e}); "
            f"falling back to deterministic"
        )
        return ""
    finally:
        if override:
            if prior_env is None:
                _os.environ.pop("OLLAMA_URL", None)
            else:
                _os.environ["OLLAMA_URL"] = prior_env

    text = text.strip()
    # Strip trailing model-prompt-echo if any
    text = text.split("\n")[0].strip()
    if not text:
        return ""
    return text


async def generate_brief_llm_cached(viewport_response: dict, **kwargs) -> str:
    """Cached LLM-augmented brief. Cache window is 5 min on the same key
    as the deterministic cache, but stored separately."""
    key = brief_llm_cache.make_key(viewport_response)
    # Async cache: the existing _BriefCache is sync; use a small async shim.
    cached = None
    with brief_llm_cache._lock:  # type: ignore[attr-defined]
        entry = brief_llm_cache._store.get(key)  # type: ignore[attr-defined]
        if entry is not None:
            ts, value = entry
            if time.time() - ts <= brief_llm_cache._ttl:  # type: ignore[attr-defined]
                cached = value
    if cached is not None:
        return cached
    value = await generate_brief_llm(viewport_response, **kwargs)
    with brief_llm_cache._lock:  # type: ignore[attr-defined]
        brief_llm_cache._store[key] = (time.time(), value)  # type: ignore[attr-defined]
    return value
