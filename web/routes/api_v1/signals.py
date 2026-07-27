"""Public-signals surface — `/signals/*` + `/signals.json` + `/signals.rss`
(extraction #8 of P3-H Phase 2 — FINAL).

Eight routes plus the entire helper graph that powered them:

  GET  /signals/today           — citizen-facing roll-up (cached 30s)
  POST /signals/subscribe       — email subscription (rate-limited)
  GET  /signals/verify          — double-opt-in confirm
  GET  /signals/unsubscribe     — opt-out
  GET  /signals/timeline        — bucketed counts powering the
                                   /monitor + /globe time-scrubber
  GET  /signals/snapshot.csv    — RFC-4180 CSV for offline analysis
  GET  /signals.json            — JSON Feed v1.1
  GET  /signals.rss             — RSS 2.0

Helpers + constants (all moved here from api_v1.py — they had no other
consumers):
  _SEVERITY_RANK        — severity ordinal for min_severity filtering
  _EMAIL_RE             — lightweight email check (subscribe)
  _looks_like_email     — wrapper
  _read_subscribe_body  — accepts JSON or form-urlencoded
  _signals_facts_for    — per-event-type structured-fact extractor
  _signals_authority_for — authority citation for an event row
  _CSV_COLUMNS          — RFC-4180 column order (test_signals_csv_endpoint
                          imports this from api_v1 via the re-export shim)
  _csv_escape, _signals_csv_row, _signals_to_csv — CSV renderers
  _RFC822_MONTHS, _RFC822_WEEKDAYS, _rfc822, _rfc822_now, _clip
                          — RSS date + count formatters
  _signals_rss_item     — single <item> renderer

Imports from already-lifted shared modules:
  request_rate_limit          (web/_rate_limit.py — used by /subscribe)
  SIGNALS_CATEGORY_ORDER      (web/_signals_categories.py — render order)
  SIGNALS_CATEGORIES_BY_TYPE  (web/_signals_categories.py — reverse index)

No `from api_v1 import` — this is the last cluster, and every prior
extraction either lifted helpers to dedicated modules or kept them in
api_v1.py for cross-cluster consumers (`_parse_bbox`, `_parse_iso`,
`_parse_types`), none of which signals consumes. So signals takes the
**no-shim-needed** pattern; api_v1.py keeps a TOP-of-file re-export
shim for `_CSV_COLUMNS` (tests/test_signals_csv_endpoint.py imports it).

Mounted by `api_v1.build_router()` at BOTH `/api/v1/*` AND `/api/intel/*`.
"""

from __future__ import annotations

import json
import re
import secrets
import time as time_mod
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from xml.sax.saxutils import escape as xml_escape

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response

from db import fetch_read, fetchval_read
from web._rate_limit import request_rate_limit
from web._signals_categories import (
    SIGNALS_CATEGORIES_BY_TYPE,
    SIGNALS_CATEGORY_ORDER,
)


# ─── Severity + email helpers ────────────────────────────────────────────


# Severity ordering for the RSS feed's `min_severity` filter. Lower number
# means more severe; the floor passes anything <= the floor's rank.
_SEVERITY_RANK: Dict[str, int] = {
    "critical": 0,
    "high":     1,
    "medium":   2,
    "low":      3,
}


# Lightweight email check — not a full RFC 5322 implementation, but
# rejects the common form-fill mistakes (no @, trailing spaces, no
# domain). Real verification is the email-confirmation step.
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def _looks_like_email(s: str) -> bool:
    return bool(s) and bool(_EMAIL_RE.match(s)) and len(s) <= 254


async def _read_subscribe_body(request: Request) -> Dict[str, Any]:
    """Accept either application/json or application/x-www-form-urlencoded
    (the common <form> POST). We parse the form body manually rather
    than via Starlette's request.form() — that pulls in python-multipart,
    a dep we don't have. URL-encoded forms are trivial to parse anyway."""
    from urllib.parse import parse_qsl
    ct = (request.headers.get("content-type") or "").lower()
    if ct.startswith("application/json"):
        try:
            data = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="malformed JSON body")
        if not isinstance(data, dict):
            raise HTTPException(status_code=400,
                                detail="JSON body must be an object")
        return data
    if ct.startswith("application/x-www-form-urlencoded") or not ct:
        raw = await request.body()
        try:
            text = raw.decode("utf-8", errors="replace")
        except Exception:
            raise HTTPException(status_code=400, detail="undecodable body")
        # parse_qsl returns [(k, v), ...] preserving duplicates; we
        # collapse to last-wins which matches Starlette's form() behavior.
        pairs = parse_qsl(text, keep_blank_values=True)
        out: Dict[str, Any] = {}
        for k, v in pairs:
            out[k] = v
        return out
    raise HTTPException(status_code=415,
                        detail=f"unsupported content-type: {ct!r}")


# ─── Per-event-type fact + authority extractors ──────────────────────────


def _signals_facts_for(event_type: str, props: Dict[str, Any]) -> Dict[str, Any]:
    """Extract the user-visible structured facts from a row's properties bag.
    Each event_type has its own interesting fields — keep this map literal
    rather than introspecting because the JSON keys differ subtly per
    algorithm (a_imo vs imo, hours_dark vs duration_h, etc.)."""
    if event_type == "sanctioned_vessel_went_dark":
        return {
            "vessel":       props.get("live_vessel_name"),
            "mmsi":         props.get("mmsi"),
            "imo":          props.get("live_imo") or props.get("sanctioned_imo"),
            "hours_dark":   props.get("hours_dark"),
            "match_kind":   props.get("match_kind"),
            "last_seen":    props.get("last_seen_ais"),
        }
    if event_type == "sanctioned_vessel_rendezvous":
        return {
            "a_name":       props.get("a_name"),
            "b_name":       props.get("b_name"),
            "a_mmsi":       props.get("a_mmsi"),
            "b_mmsi":       props.get("b_mmsi"),
            "distance_m":   props.get("distance_m"),
            "a_sanctioned": bool(props.get("a_sanctioned")),
            "b_sanctioned": bool(props.get("b_sanctioned")),
        }
    if event_type == "shadow_fleet_cluster":
        return {
            "vessels":      props.get("vessel_count") or props.get("size"),
            "members":      (props.get("member_names") or [])[:5],
        }
    if event_type == "sanctioned_vessel_underway":
        return {
            "vessel":       props.get("live_vessel_name"),
            "mmsi":         props.get("mmsi"),
            "imo":          props.get("live_imo") or props.get("sanctioned_imo"),
            "match_kind":   props.get("match_kind"),
        }
    if event_type == "sanctioned_port_arrival":
        return {
            "vessel":       props.get("vessel_name") or props.get("live_vessel_name"),
            "port":         props.get("port_name") or props.get("port"),
            "mmsi":         props.get("mmsi"),
        }
    if event_type == "aircraft_in_sanctioned_airspace":
        return {
            "callsign":     props.get("callsign"),
            "icao24":       props.get("icao24"),
            "country":      props.get("country") or props.get("airspace"),
        }
    if event_type == "military_aircraft_underway":
        return {
            "callsign":     props.get("callsign"),
            "icao24":       props.get("icao24"),
            "operator":     props.get("operator") or props.get("country"),
        }
    if event_type == "dark_vessel_detected":
        return {
            "vessel":       props.get("vessel_name") or props.get("live_vessel_name"),
            "mmsi":         props.get("mmsi"),
            "hours_dark":   props.get("hours_dark") or props.get("duration_h"),
        }
    if event_type == "loitering_detected":
        return {
            "entity_name":  props.get("entity_name") or props.get("vessel_name"),
            "mmsi":         props.get("mmsi"),
            "icao24":       props.get("icao24"),
            "duration_h":   props.get("duration_h"),
            "radius_m":     props.get("radius_m"),
        }
    if event_type == "nasa_firms":
        return {
            "satellite":    props.get("satellite") or props.get("instrument"),
            "brightness":   props.get("brightness") or props.get("bright_ti4"),
            "confidence":   props.get("confidence"),
        }
    if event_type == "usgs_quake":
        return {
            "magnitude":    props.get("mag") or props.get("magnitude"),
            "place":        props.get("place"),
            "depth_km":     props.get("depth_km") or props.get("depth"),
        }
    return {}


def _signals_authority_for(event_type: str, props: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Return the authoritative source for this finding when the row carries
    one (sanctions matches → OFAC / EU CFSP / UK OFSI). Returns None when
    the source is purely derived (algorithm + AIS/ADS-B observation) — the
    UI then displays the algorithm name + raw-source label instead."""
    auth = props.get("sanctioning_authority")
    canonical = (
        props.get("sanctioned_canonical_id")
        or props.get("a_sanctioned_canonical_id")
        or props.get("b_sanctioned_canonical_id")
    )
    if auth or canonical:
        return {
            "name":         auth,
            "canonical_id": canonical,
        }
    if event_type == "nasa_firms":
        return {"name": "NASA FIRMS", "canonical_id": None}
    if event_type == "usgs_quake":
        return {"name": "USGS Earthquake Hazards Program", "canonical_id": None}
    return None


# ─── CSV renderers ───────────────────────────────────────────────────────


_CSV_COLUMNS: List[str] = [
    "event_time_utc", "severity", "category", "event_type",
    "title", "description",
    "vessel_name", "mmsi", "imo", "icao24", "callsign",
    "hours_dark", "distance_m", "partner",
    "lat", "lng",
    "authority", "authority_canonical_id",
    "entity_id", "entity_url", "event_url",
]


def _csv_escape(v: Any) -> str:
    """RFC-4180 CSV cell. Empty cells stay literally empty (Excel
    treats them as blank, which is what we want for 'no value')."""
    if v is None:
        return ""
    s = str(v)
    if not s:
        return ""
    needs_quote = any(c in s for c in ',"\r\n')
    if needs_quote:
        s = s.replace('"', '""')
        return f'"{s}"'
    return s


def _signals_csv_row(*, event_id: str, event_type: str, event_time,
                      severity: Any, title: Any, description: Any,
                      props: Dict[str, Any], lat: Any, lng: Any,
                      entity_id: Optional[str], base: str) -> List[str]:
    """Project one DB row into the CSV column order."""
    facts = _signals_facts_for(event_type, props)
    authority = _signals_authority_for(event_type, props)
    cat = SIGNALS_CATEGORIES_BY_TYPE.get(event_type, {})

    # Per-type extraction — pull the most-useful fields for an analyst
    # opening this in Excel. Falls back gracefully when a field
    # doesn't apply to this event_type (the cell stays empty).
    vessel_name = (facts.get("vessel") or facts.get("entity_name")
                    or facts.get("a_name") or "")
    partner = facts.get("b_name") or ""
    if event_type == "shadow_fleet_cluster":
        members = facts.get("members") or []
        vessel_name = members[0] if members else ""
        partner = ", ".join(members[1:5]) if len(members) > 1 else ""

    return [
        _csv_escape(event_time.isoformat() if event_time else ""),
        _csv_escape(cat.get("severity", "")),
        _csv_escape(cat.get("label", "")),
        _csv_escape(event_type),
        _csv_escape(title or ""),
        _csv_escape(description or ""),
        _csv_escape(vessel_name),
        _csv_escape(facts.get("mmsi") or facts.get("a_mmsi") or ""),
        _csv_escape(facts.get("imo") or ""),
        _csv_escape(facts.get("icao24") or ""),
        _csv_escape(facts.get("callsign") or ""),
        _csv_escape(facts.get("hours_dark") or facts.get("duration_h") or ""),
        _csv_escape(facts.get("distance_m") or facts.get("radius_m") or ""),
        _csv_escape(partner),
        _csv_escape(lat if lat is not None else ""),
        _csv_escape(lng if lng is not None else ""),
        _csv_escape(authority.get("name") if authority else ""),
        _csv_escape(authority.get("canonical_id") if authority else ""),
        _csv_escape(entity_id or ""),
        _csv_escape(f"{base}/entity/{entity_id}" if entity_id else ""),
        _csv_escape(f"{base}/api/v1/event/{event_id}"),
    ]


def _signals_to_csv(rows, *, base: str) -> str:
    """Render the complete CSV body (header + rows). Joined with \\r\\n
    line endings per RFC-4180."""
    lines = [",".join(_CSV_COLUMNS)]
    for r in rows:
        props = r["properties"]
        if isinstance(props, str):
            try:
                props = json.loads(props)
            except (TypeError, ValueError):
                props = {}
        ent_id = str(r["entity_id"]) if r["entity_id"] else None
        cells = _signals_csv_row(
            event_id=str(r["id"]),
            event_type=r["event_type"],
            event_time=r["event_time"],
            severity=r["severity"],
            title=r["title"],
            description=r["description"],
            props=props or {},
            lat=r["lat"],
            lng=r["lng"],
            entity_id=ent_id,
            base=base,
        )
        lines.append(",".join(cells))
    return "\r\n".join(lines) + "\r\n"


# ─── RSS date + item renderers ───────────────────────────────────────────


_RFC822_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
                  "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_RFC822_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def _rfc822(dt: datetime) -> str:
    """Format a UTC datetime as RFC-822 — required by RSS readers.
    Locale-independent (we don't trust the daemon's LC_TIME)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    return (f"{_RFC822_WEEKDAYS[dt.weekday()]}, "
            f"{dt.day:02d} {_RFC822_MONTHS[dt.month - 1]} "
            f"{dt.year} {dt.hour:02d}:{dt.minute:02d}:{dt.second:02d} +0000")


def _rfc822_now() -> str:
    return _rfc822(datetime.now(timezone.utc))


def _clip(n: int) -> int:
    return n


def _signals_rss_item(*, base: str, row: Any,
                       facts: Dict[str, Any],
                       authority: Optional[Dict[str, Any]],
                       category_label: str,
                       severity_label: str) -> str:
    """Render one <item> for the RSS feed. All user-supplied text is
    XML-escaped — title and description are CDATA-wrapped so embedded
    formatting (the description bag often has parentheses/colons) ships
    cleanly to feed readers."""
    eid = str(row["id"])
    pubdate = _rfc822(row["event_time"]) if row["event_time"] else _rfc822_now()
    link = f"{base}/api/v1/event/{eid}"
    title = row["title"] or f"{category_label}: {eid[:8]}"

    # Build a reader-friendly description. Use HTML for richer rendering
    # in feed readers that support it (most do), but keep it minimal
    # so plain-text readers don't choke.
    parts = []
    if row["description"]:
        parts.append(f"<p>{xml_escape(row['description'])}</p>")
    fact_lines = [(k, v) for k, v in (facts or {}).items()
                   if v is not None and v != "" and v is not False]
    if fact_lines:
        parts.append("<p><strong>Details:</strong> "
                     + ", ".join(f"{xml_escape(k)}={xml_escape(str(v))}"
                                  for k, v in fact_lines)
                     + "</p>")
    if authority and authority.get("name"):
        cid = authority.get("canonical_id")
        cid_str = f" ({xml_escape(cid)})" if cid else ""
        parts.append(f"<p><strong>Authority:</strong> "
                     f"{xml_escape(authority['name'])}{cid_str}</p>")
    parts.append(f"<p><em>Severity:</em> {xml_escape(severity_label)} "
                 f"&nbsp;·&nbsp; <em>Category:</em> "
                 f"{xml_escape(category_label)}</p>")
    description_html = "".join(parts)

    return (
        '    <item>\n'
        f'      <title><![CDATA[{title}]]></title>\n'
        f'      <link>{xml_escape(link)}</link>\n'
        f'      <guid isPermaLink="false">{xml_escape(eid)}</guid>\n'
        f'      <pubDate>{pubdate}</pubDate>\n'
        f'      <category>{xml_escape(category_label)}</category>\n'
        f'      <description><![CDATA[{description_html}]]></description>\n'
        '    </item>\n'
    )


# ─── /signals/today in-memory cache ──────────────────────────────────────


# The endpoint's underlying query has ~3-4 seconds of TimescaleDB chunk-
# pruning at PLANNING time (with 60+ chunks on the event hypertable) —
# execution is fast but planning is not. Without caching, the cockpit +
# bots + SSE clients + digest all stack concurrent calls, saturating the
# asyncpg pool. 30s TTL: stale enough that the live dashboard sees
# near-real-time, fresh enough that the digest + 1-min auto-refreshes
# don't re-plan. Key includes (window_hours, per_category) so the
# cockpit's 24h cap doesn't poison cache for a custom widget asking
# for 168h.
_SIGNALS_TODAY_CACHE: Dict[Any, Dict[str, Any]] = {}
_SIGNALS_TODAY_TTL_SEC = 30


# ─── Routes ──────────────────────────────────────────────────────────────


router = APIRouter()


@router.get("/signals/today")
async def signals_today(
    window_hours: int = Query(24, ge=1, le=168,
        description="Look-back window in hours. Default 24."),
    per_category: int = Query(8, ge=1, le=500,
        description="Sample items per category. Default 8, max 500."),
):
    _cache_key = (window_hours, per_category)
    _now = time_mod.monotonic()
    _hit = _SIGNALS_TODAY_CACHE.get(_cache_key)
    if _hit and (_now - _hit["at"]) < _SIGNALS_TODAY_TTL_SEC:
        return _hit["body"]
    """Public-facing 'today's signals' summary — the algorithm-derived
    findings worth surfacing to a customer, grouped into categories
    and severity-ranked.

    Powers the /signals page. Each category returns a count + a
    truncated sample of the most recent items, with the source
    authority surfaced per item where applicable (OFAC / EU CFSP /
    UK OFSI for sanctions; the responsible algorithm name for
    purely-derived findings). Designed to render top-to-bottom on a
    single screen with no JavaScript framework — see
    21_GLASSBOX_AI/signals/index.html for the consumer.

    Latency target: <300ms when warm. Categories are queried in a
    single CTE so the endpoint stays one round-trip.
    """
    # LATERAL-join: for each event_type in the category list, pull
    # the top `per_category` rows by event_time using the existing
    # (event_type, event_time DESC) index. Returns ~per_category *
    # n_types rows total (~132 at default per_category=12) instead
    # of every match in the window (~58k at v1.0 scale). Dropping
    # 99.8% of the row transfer + Python-side grouping cost.
    # Separately, COUNT(*) per type via a single GROUP BY for the
    # category totals shown in the UI. Both queries use the index
    # cleanly; combined runtime well under the prior 4s planning
    # cost even on cold cache.
    all_types = list(SIGNALS_CATEGORIES_BY_TYPE.keys())
    rows = await fetch_read(
        """
        WITH types(t) AS (SELECT unnest($2::text[]))
        SELECT
            e.id,
            e.event_type,
            e.event_time,
            e.severity,
            e.title,
            e.description,
            e.properties,
            e.entity_id,
            ST_Y(e.geom::geometry) AS lat,
            ST_X(e.geom::geometry) AS lng
        FROM types
        JOIN LATERAL (
            SELECT id, event_type, event_time, severity, title,
                   description, properties, entity_id, geom
            FROM event
            WHERE event_time >= NOW() - ($1::int || ' hours')::interval
              AND event_type = types.t
              -- Withdrawn events (algorithm corrections) excluded from
              -- the public surface. The row is preserved for audit;
              -- consumers query `properties->>'withdrawn'` to inspect
              -- corrections.
              AND (properties->>'withdrawn') IS NULL
            ORDER BY event_time DESC
            LIMIT $3::int
        ) e ON TRUE
        ORDER BY e.event_type, e.event_time DESC
        """,
        window_hours,
        all_types,
        per_category,
    )

    # Total per-type counts: cheap single-pass aggregation using the
    # same index. Returns one row per type that has any matches.
    count_rows = await fetch_read(
        """
        SELECT event_type, COUNT(*)::int AS cnt
        FROM event
        WHERE event_time >= NOW() - ($1::int || ' hours')::interval
          AND event_type = ANY($2::text[])
          AND (properties->>'withdrawn') IS NULL
        GROUP BY event_type
        """,
        window_hours,
        all_types,
    )
    type_counts = {r["event_type"]: r["cnt"] for r in count_rows}

    # Group sample rows by event_type for the rendering loop.
    by_type: Dict[str, List[Any]] = {}
    for r in rows:
        by_type.setdefault(r["event_type"], []).append(r)

    categories = []
    total_findings = 0
    critical_count = 0
    for cat in SIGNALS_CATEGORY_ORDER:
        type_rows = by_type.get(cat["event_type"], [])
        # True total per-type count from the GROUP BY query (the
        # LATERAL only returned the top `per_category` samples, so
        # len(type_rows) is capped at per_category — not the real
        # total). Falls back to len when type isn't in count_rows
        # (happens when 0 rows match).
        count = type_counts.get(cat["event_type"], len(type_rows))
        total_findings += count
        if cat["severity"] == "critical":
            critical_count += count
        sample = []
        for r in type_rows[:per_category]:
            props = r["properties"]
            if isinstance(props, str):
                try:
                    props = json.loads(props)
                except (TypeError, ValueError):
                    props = {}
            ent_id = str(r["entity_id"]) if r["entity_id"] else None
            # P3-N step 2 (2026-05-20): surface confidence_score +
            # confidence_label from event properties. These fields are
            # populated at writer-time via `_with_confidence()` in
            # writers.py and pass through unchanged. Will be None on
            # events from before the P3-N daemon restart and on event
            # types where the layer has no PLATFORM_BASELINE mapping —
            # consumers should treat both as "no signal" rather than
            # error.
            sample.append({
                "id":               str(r["id"]),
                "ts":               r["event_time"].isoformat() if r["event_time"] else None,
                "title":            r["title"],
                "description":      r["description"],
                "severity":         r["severity"],
                "lat":              r["lat"],
                "lng":              r["lng"],
                "entity_id":        ent_id,
                "facts":            _signals_facts_for(cat["event_type"], props or {}),
                "authority":        _signals_authority_for(cat["event_type"], props or {}),
                "confidence_score": (props or {}).get("confidence_score"),
                "confidence_label": (props or {}).get("confidence_label"),
                "links":            {
                    "event":  f"/api/v1/event/{r['id']}",
                    "entity": (f"/entity/{ent_id}" if ent_id else None),
                },
            })
        categories.append({
            "id":       cat["id"],
            "label":    cat["label"],
            "severity": cat["severity"],
            "icon":     cat["icon"],
            "count":    count,
            "items":    sample,
        })

    _body = {
        "generated_at":  datetime.now(timezone.utc).isoformat(),
        "window_hours":  window_hours,
        "summary": {
            "total_findings":   total_findings,
            "critical_count":   critical_count,
            "categories_active": sum(1 for c in categories if c["count"] > 0),
        },
        "categories": categories,
    }
    _SIGNALS_TODAY_CACHE[_cache_key] = {"at": time_mod.monotonic(), "body": _body}
    return _body


@router.post("/signals/subscribe")
@request_rate_limit(max_per_window=5, window_sec=300, scope="subscribe")
async def signals_subscribe(request: Request):
    """Public email subscription. Idempotent on email — repeated
    signups update the filter prefs instead of erroring.

    POST body (application/x-www-form-urlencoded OR application/json):
        email: required, must look like an email
        severity_floor: critical | high | medium | low (default 'high')
        category_ids: comma-separated list (default empty = all)
        source: optional string for attribution

    Returns: {ok: true, status: 'created'|'updated', verify_required: true}
    Always returns the same shape on success — does NOT echo the
    verify_token (that goes via email in v1.1+).
    """
    body = await _read_subscribe_body(request)
    email = (body.get("email") or "").strip().lower()
    if not _looks_like_email(email):
        raise HTTPException(status_code=400,
                            detail="email must be a valid address")
    sev = (body.get("severity_floor") or "high").strip().lower()
    if sev not in _SEVERITY_RANK:
        raise HTTPException(status_code=400,
                            detail="severity_floor must be one of "
                                   "critical|high|medium|low")
    raw_cats = body.get("category_ids") or ""
    if isinstance(raw_cats, list):
        cats = [str(c).strip() for c in raw_cats if str(c).strip()]
    else:
        cats = [c.strip() for c in str(raw_cats).split(",") if c.strip()]
    valid_cat_ids = {c["id"] for c in SIGNALS_CATEGORY_ORDER}
    bad = [c for c in cats if c not in valid_cat_ids]
    if bad:
        raise HTTPException(
            status_code=400,
            detail=f"unknown category_ids: {bad}. valid: {sorted(valid_cat_ids)}",
        )
    source = (body.get("source") or "unknown")[:64]

    verify_token      = secrets.token_urlsafe(32)
    unsubscribe_token = secrets.token_urlsafe(32)
    client_ip = request.client.host if request.client else None
    user_agent = request.headers.get("user-agent", "")[:500]

    filters = {"severity_floor": sev, "category_ids": cats}

    # Idempotent insert. ON CONFLICT updates the prefs but keeps the
    # original tokens (so an old verify-link still works) and the
    # original verified flag (re-subscribing doesn't undo verification).
    rows = await fetch_read(
        """
        INSERT INTO signals_subscription
            (email, filters, source,
             verify_token, unsubscribe_token,
             created_ip, user_agent)
        VALUES ($1, $2::jsonb, $3, $4, $5, $6, $7)
        ON CONFLICT (email) DO UPDATE
            SET filters    = EXCLUDED.filters,
                source     = EXCLUDED.source
        RETURNING (xmax = 0) AS was_inserted, verified
        """,
        email, json.dumps(filters), source,
        verify_token, unsubscribe_token,
        client_ip, user_agent,
    )
    was_inserted = bool(rows[0]["was_inserted"])
    verified = bool(rows[0]["verified"])
    return {
        "ok": True,
        "status": "created" if was_inserted else "updated",
        "verify_required": not verified,
    }


@router.get("/signals/verify")
async def signals_verify(t: str = Query(..., min_length=10, max_length=128)):
    """Confirm an email signup. Sets verified=true if the token
    matches an unverified row. Idempotent — calling twice returns
    ok=true the second time too."""
    rows = await fetch_read(
        """
        UPDATE signals_subscription
           SET verified=true, verified_at = now()
         WHERE verify_token = $1
           AND verified = false
        RETURNING email
        """,
        t,
    )
    if not rows:
        # Either the token doesn't exist OR the row is already
        # verified. Distinguish for the user — both states are
        # success-ish, but the message differs.
        already = await fetchval_read(
            "SELECT verified FROM signals_subscription WHERE verify_token=$1",
            t,
        )
        if already is None:
            raise HTTPException(status_code=404,
                                detail="verification token not found")
        return {"ok": True, "status": "already_verified"}
    return {"ok": True, "status": "verified", "email": rows[0]["email"]}


@router.get("/signals/unsubscribe")
async def signals_unsubscribe(
    t: str = Query(..., min_length=10, max_length=128),
):
    """Soft-delete a subscription. Idempotent."""
    rows = await fetch_read(
        """
        UPDATE signals_subscription
           SET unsubscribed_at = COALESCE(unsubscribed_at, now())
         WHERE unsubscribe_token = $1
        RETURNING email, unsubscribed_at
        """,
        t,
    )
    if not rows:
        raise HTTPException(status_code=404,
                            detail="unsubscribe token not found")
    return {"ok": True, "status": "unsubscribed",
            "email": rows[0]["email"]}


@router.get("/signals/timeline")
async def signals_timeline(
    window_hours: int = Query(168, ge=1, le=720,
        description="Look-back window in hours. Default 168 (7 days)."),
    bucket_min: int = Query(60, ge=5, le=1440,
        description="Bucket size in minutes. Default 60 = hourly."),
):
    """Time-bucketed counts of algorithm-derived findings — powers
    the time-scrubber UI on /monitor + /globe. A slider drags
    through these buckets, and at each tick the page re-queries
    /signals/today with `as_of=<bucket_ts>` to render the world
    as it looked at that moment.

    Returns:
      generated_at, window_hours, bucket_min,
      buckets: [
        { ts, total,
          by_category: {sanctioned_dark: 3, ...},
          by_severity: {critical: 5, high: 2, ...} }
      ]
    """
    rows = await fetch_read(
        """
        WITH bucketed AS (
            SELECT
                date_trunc('minute',
                  to_timestamp(
                    (extract(epoch FROM event_time)::bigint /
                     ($2::int * 60)) * ($2::int * 60)
                  )
                ) AS bucket_ts,
                event_type,
                count(*) AS n
            FROM event
            WHERE event_time >= NOW() - ($1::int || ' hours')::interval
              AND event_type = ANY($3::text[])
            GROUP BY bucket_ts, event_type
        )
        SELECT
            bucket_ts,
            event_type,
            n
        FROM bucketed
        ORDER BY bucket_ts ASC, event_type ASC
        """,
        window_hours,
        bucket_min,
        list(SIGNALS_CATEGORIES_BY_TYPE.keys()),
    )

    # Group rows by bucket
    buckets_map: Dict[Any, Dict[str, Any]] = {}
    for r in rows:
        ts = r["bucket_ts"]
        slot = buckets_map.setdefault(ts, {
            "ts": ts.isoformat() if ts else None,
            "total": 0,
            "by_category": {},
            "by_severity": {"critical": 0, "high": 0, "medium": 0, "low": 0},
        })
        cat = SIGNALS_CATEGORIES_BY_TYPE.get(r["event_type"], {})
        cid = cat.get("id", r["event_type"])
        sev = cat.get("severity", "low")
        n = int(r["n"])
        slot["by_category"][cid] = slot["by_category"].get(cid, 0) + n
        slot["by_severity"][sev] = slot["by_severity"].get(sev, 0) + n
        slot["total"] += n

    buckets = sorted(buckets_map.values(), key=lambda s: s["ts"] or "")
    return {
        "generated_at":  datetime.now(timezone.utc).isoformat(),
        "window_hours":  window_hours,
        "bucket_min":    bucket_min,
        "bucket_count":  len(buckets),
        "buckets":       buckets,
    }


@router.get("/signals/snapshot.csv", response_class=Response)
async def signals_snapshot_csv(
    request: Request,
    window_hours: int = Query(24, ge=1, le=168),
    min_severity: str = Query(
        "high",
        pattern="^(critical|high|medium|low)$",
        description="Floor severity. Same semantics as /signals.rss.",
    ),
    limit: int = Query(500, ge=1, le=5000,
        description=("CSV is for offline analysis — higher cap than "
                     "the feed endpoints (500 default vs 50)."),
    ),
):
    """RFC-4180 CSV snapshot of recent algorithm-derived findings —
    for spreadsheet analysis (Excel, Google Sheets, pandas) and
    ad-hoc data work without writing an API client.

    Filter semantics mirror /signals.rss + /signals.json. Columns
    chosen to be self-describing for an analyst opening the CSV
    cold (no need to cross-reference docs):

      event_time_utc, severity, category, event_type,
      title, description,
      vessel_name, mmsi, imo, icao24, callsign,
      hours_dark, distance_m, partner,
      lat, lng,
      authority, authority_canonical_id,
      entity_id, entity_url, event_url

    Empty cells are EMPTY, not 'null' — Excel sees them as blank,
    which is the right semantic for 'this finding doesn't have
    that field'.
    """
    sev_floor = _SEVERITY_RANK[min_severity]
    wanted_types = [
        cat["event_type"] for cat in SIGNALS_CATEGORY_ORDER
        if _SEVERITY_RANK[cat["severity"]] <= sev_floor
    ]
    if not wanted_types:
        wanted_types = [c["event_type"] for c in SIGNALS_CATEGORY_ORDER]

    rows = await fetch_read(
        """
        SELECT
            id, event_type, event_time, severity,
            title, description, properties, entity_id,
            ST_Y(geom::geometry) AS lat,
            ST_X(geom::geometry) AS lng
        FROM event
        WHERE event_time >= NOW() - ($1::int || ' hours')::interval
          AND event_type = ANY($2::text[])
        ORDER BY event_time DESC
        LIMIT $3
        """,
        window_hours, wanted_types, limit,
    )

    scheme = request.url.scheme if request else "http"
    netloc = request.url.netloc if request else "127.0.0.1:8790"
    base = f"{scheme}://{netloc}"

    csv_text = _signals_to_csv(rows, base=base)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%SZ")
    return Response(
        content=csv_text,
        media_type="text/csv; charset=utf-8",
        headers={
            "Cache-Control": "public, max-age=60",
            "Content-Disposition":
                f'attachment; filename="glassbox_signals_{ts}.csv"',
        },
    )


@router.get("/signals.json", response_class=Response)
async def signals_jsonfeed(
    request: Request,
    window_hours: int = Query(24, ge=1, le=168),
    min_severity: str = Query(
        "high",
        pattern="^(critical|high|medium|low)$",
        description="Floor severity. Same semantics as /signals.rss.",
    ),
    limit: int = Query(50, ge=1, le=200),
):
    """JSON Feed v1.1 (jsonfeed.org/version/1.1) of recent
    algorithm-derived findings — the modern alternative to RSS,
    consumed natively by Zapier, Pipedream, Slack's JSON-feed
    connector, and most modern feed readers.

    Same filter semantics as /signals.rss (window_hours +
    min_severity floor + limit). Item schema:
      - id            event UUID (stable for de-dup)
      - url           same-origin /api/v1/event/{id}
      - title         finding title
      - content_html  same body as RSS description
      - date_published ISO-8601 (event_time)
      - tags          [category_label]
      - _glassbox     extension object: {severity, authority{name,canonical_id},
                      facts{...}, lat, lng}

    The `_glassbox` extension follows the JSON Feed convention
    for vendor-specific fields (underscore-prefixed) so a
    consumer that wants the raw structured facts (vessel name,
    MMSI, distance_m, etc.) doesn't have to re-parse content_html.
    """
    sev_floor = _SEVERITY_RANK[min_severity]
    wanted_types = [
        cat["event_type"] for cat in SIGNALS_CATEGORY_ORDER
        if _SEVERITY_RANK[cat["severity"]] <= sev_floor
    ]
    if not wanted_types:
        wanted_types = [c["event_type"] for c in SIGNALS_CATEGORY_ORDER]

    rows = await fetch_read(
        """
        SELECT
            id, event_type, event_time, severity,
            title, description, properties, entity_id,
            ST_Y(geom::geometry) AS lat,
            ST_X(geom::geometry) AS lng
        FROM event
        WHERE event_time >= NOW() - ($1::int || ' hours')::interval
          AND event_type = ANY($2::text[])
        ORDER BY event_time DESC
        LIMIT $3
        """,
        window_hours, wanted_types, limit,
    )

    scheme = request.url.scheme if request else "http"
    netloc = request.url.netloc if request else "127.0.0.1:8790"
    base = f"{scheme}://{netloc}"

    items = []
    for r in rows:
        props = r["properties"]
        if isinstance(props, str):
            try:
                props = json.loads(props)
            except (TypeError, ValueError):
                props = {}
        facts = _signals_facts_for(r["event_type"], props or {})
        authority = _signals_authority_for(r["event_type"], props or {})
        cat = SIGNALS_CATEGORIES_BY_TYPE.get(r["event_type"], {})
        cat_label = cat.get("label", r["event_type"])
        sev_label = cat.get("severity", "low")
        ent_id = str(r["entity_id"]) if r["entity_id"] else None

        # content_html mirrors the RSS description so consumers
        # that bridge between the two see consistent text.
        content_parts = []
        if r["description"]:
            content_parts.append(f"<p>{xml_escape(r['description'])}</p>")
        fact_lines = [(k, v) for k, v in (facts or {}).items()
                       if v is not None and v != "" and v is not False]
        if fact_lines:
            content_parts.append(
                "<p><strong>Details:</strong> "
                + ", ".join(f"{xml_escape(k)}={xml_escape(str(v))}"
                             for k, v in fact_lines)
                + "</p>"
            )
        if authority and authority.get("name"):
            cid = authority.get("canonical_id")
            cid_str = f" ({xml_escape(cid)})" if cid else ""
            content_parts.append(
                f"<p><strong>Authority:</strong> "
                f"{xml_escape(authority['name'])}{cid_str}</p>"
            )

        item = {
            "id":             str(r["id"]),
            "url":            f"{base}/api/v1/event/{r['id']}",
            "title":          r["title"] or f"{cat_label}: {str(r['id'])[:8]}",
            "content_html":   "".join(content_parts) or "(no detail)",
            "date_published": (r["event_time"].isoformat()
                                if r["event_time"] else None),
            "tags":           [cat_label],
            "_glassbox": {
                "severity":     sev_label,
                "category_id":  cat.get("id"),
                "event_type":   r["event_type"],
                "lat":          r["lat"],
                "lng":          r["lng"],
                "facts":        facts,
                "authority":    authority,
                "entity_id":    ent_id,
                "entity_url":   (f"{base}/entity/{ent_id}" if ent_id else None),
            },
        }
        items.append(item)

    feed = {
        "version":      "https://jsonfeed.org/version/1.1",
        "title":        "Glassbox — today’s signals",
        "home_page_url": f"{base}/signals",
        "feed_url":     f"{base}/api/v1/signals.json",
        "description":  ("Algorithm-derived findings from Glassbox: "
                         "sanctioned-vessel events, restricted-airspace "
                         "incursions, shadow-fleet clusters."),
        "items":        items,
    }
    return Response(
        content=json.dumps(feed, default=str, ensure_ascii=False),
        media_type="application/feed+json; charset=utf-8",
        headers={"Cache-Control": "public, max-age=60"},
    )


@router.get("/signals.rss", response_class=Response)
async def signals_rss(
    request: Request,
    window_hours: int = Query(24, ge=1, le=168),
    min_severity: str = Query(
        "high",
        pattern="^(critical|high|medium|low)$",
        description=("Floor severity. 'critical' = only the top tier; "
                     "'low' = everything. Default 'high' covers the "
                     "first six categories."),
    ),
    limit: int = Query(50, ge=1, le=200),
):
    """RSS 2.0 feed of recent algorithm-derived findings — designed
    for plugging Glassbox into existing intel workflows (Slack RSS,
    Feedly, IFTTT, Zapier, etc.) without writing a custom integration.

    Each item carries the finding's title, a structured description
    (key facts + authority citation), pubDate, and a stable GUID
    (the event UUID). Items are sorted newest first across all
    categories at or above `min_severity`.

    Cache-friendly: emits a 60s `Cache-Control` so feed readers don't
    hammer the DB on every poll. Same-origin URLs in the feed link
    back to /api/v1/event/{id} for full evidence.
    """
    sev_floor = _SEVERITY_RANK[min_severity]
    wanted_types = [
        cat["event_type"] for cat in SIGNALS_CATEGORY_ORDER
        if _SEVERITY_RANK[cat["severity"]] <= sev_floor
    ]
    if not wanted_types:
        wanted_types = [c["event_type"] for c in SIGNALS_CATEGORY_ORDER]

    rows = await fetch_read(
        """
        SELECT
            id, event_type, event_time, severity,
            title, description, properties,
            ST_Y(geom::geometry) AS lat,
            ST_X(geom::geometry) AS lng
        FROM event
        WHERE event_time >= NOW() - ($1::int || ' hours')::interval
          AND event_type = ANY($2::text[])
        ORDER BY event_time DESC
        LIMIT $3
        """,
        window_hours, wanted_types, limit,
    )

    # Best-effort scheme/host for absolute links — RSS readers prefer
    # them. Falls back to relative URLs if the request object isn't
    # populated (which only happens in unit tests; harmless).
    scheme = request.url.scheme if request else "http"
    netloc = request.url.netloc if request else "127.0.0.1:8790"
    base = f"{scheme}://{netloc}"

    items_xml = []
    for r in rows:
        props = r["properties"]
        if isinstance(props, str):
            try:
                props = json.loads(props)
            except (TypeError, ValueError):
                props = {}
        facts = _signals_facts_for(r["event_type"], props or {})
        authority = _signals_authority_for(r["event_type"], props or {})
        cat = SIGNALS_CATEGORIES_BY_TYPE.get(r["event_type"], {})
        items_xml.append(_signals_rss_item(
            base=base, row=r, facts=facts, authority=authority,
            category_label=cat.get("label", r["event_type"]),
            severity_label=cat.get("severity", "low"),
        ))

    feed = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">\n'
        '  <channel>\n'
        f'    <title>Glassbox — today’s signals</title>\n'
        f'    <link>{xml_escape(base)}/signals</link>\n'
        f'    <atom:link href="{xml_escape(base)}/api/v1/signals.rss" '
        f'rel="self" type="application/rss+xml"/>\n'
        '    <description>Algorithm-derived findings from Glassbox: '
        'sanctioned-vessel events, restricted-airspace incursions, '
        'shadow-fleet clusters. Updated continuously from public '
        'OSINT sources.</description>\n'
        '    <language>en-us</language>\n'
        '    <ttl>5</ttl>\n'
        f'    <lastBuildDate>{_rfc822_now()}</lastBuildDate>\n'
        f'    <generator>glassbox-server v2 ({_clip(len(rows))} '
        f'items, window={window_hours}h)</generator>\n'
        + "".join(items_xml)
        + '  </channel>\n'
        '</rss>\n'
    )
    return Response(
        content=feed,
        media_type="application/rss+xml; charset=utf-8",
        headers={"Cache-Control": "public, max-age=60"},
    )
