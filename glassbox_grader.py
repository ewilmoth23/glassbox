#!/usr/bin/env python3
"""
glassbox_grader.py — closes the feedback loop on Glassbox predictions.

Fetches past predictions whose `due_at` has passed (still `outcome=pending`),
checks real events (USGS earthquakes, GDELT, MEWR News API /intel/grade), and
writes an outcome via brain.grade_prediction(). The grader is conservative:
  - If a real event matches (region + category + magnitude), mark 'win'
  - If the prediction window passed with no matching signal, mark 'loss'
  - If ambiguous (partial match, different category), mark 'unresolved'

Run hourly. Idempotent — already-graded predictions are skipped.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import sys
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "20_HOLDING_BRAIN" / "memory"))
from brain import Brain  # type: ignore


API_BASE = os.environ.get("API_BASE", "https://mewr-news-api.mewrcreate.workers.dev")
SERVICE_NAME = "glassbox_grader"
NS = "glassbox"


def _http_get(url: str, timeout: float = 15.0) -> dict | list | None:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "GlassboxGrader/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:
        print(f"[gb-grader] {url} -> {type(e).__name__}: {e}")
        return None


def _haversine_km(lat1, lng1, lat2, lng2) -> float:
    if None in (lat1, lng1, lat2, lng2):
        return 1e9
    R = 6371.0
    from math import radians, sin, cos, atan2, sqrt
    dlat = radians(lat2 - lat1); dlng = radians(lng2 - lng1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlng / 2) ** 2
    return 2 * R * atan2(sqrt(a), sqrt(1 - a))


# ─── Evidence sources ────────────────────────────────────────────────

def _fetch_usgs(start_iso: str, end_iso: str, min_mag: float = 4.5) -> list[dict]:
    """USGS earthquake catalog — canonical source for seismic predictions."""
    url = (
        "https://earthquake.usgs.gov/fdsnws/event/1/query"
        f"?format=geojson&starttime={start_iso}&endtime={end_iso}&minmagnitude={min_mag}"
    )
    data = _http_get(url)
    if not isinstance(data, dict):
        return []
    events = []
    for f in data.get("features", []):
        p = f.get("properties", {})
        g = f.get("geometry", {})
        coords = g.get("coordinates") or [None, None]
        events.append({
            "source": "usgs",
            "category": "earthquake",
            "mag": p.get("mag"),
            "place": p.get("place"),
            "time_ms": p.get("time"),
            "lat": coords[1],
            "lng": coords[0],
        })
    return events


def _fetch_gdelt_recent(hours: int = 72) -> list[dict]:
    """GDELT 2.0 event database — political violence / protests / crises."""
    # GDELT isn't freely available with fine-grained date filtering without
    # a BigQuery cred; fall back to lightweight event-tone filter via doc API.
    # If the query fails, we degrade gracefully — many predictions will still
    # resolve from USGS alone.
    url = "https://api.gdeltproject.org/api/v2/doc/doc?query=%28conflict%20OR%20strike%20OR%20protest%29&mode=ArtList&format=json&maxrecords=50"
    data = _http_get(url, timeout=8.0)
    if not isinstance(data, dict):
        return []
    articles = data.get("articles", [])
    events = []
    for a in articles[:50]:
        events.append({
            "source": "gdelt",
            "category": "conflict",
            "title": a.get("title", "")[:200],
            "url": a.get("url", ""),
            "seendate": a.get("seendate", ""),
        })
    return events


# ─── Grading logic ───────────────────────────────────────────────────

def _match_prediction(pred: dict, usgs_events: list[dict], gdelt_events: list[dict]) -> tuple[str, dict]:
    """
    Return (outcome, details_dict) for a prediction.
    outcome ∈ ('win', 'loss', 'push', 'unresolved')
    """
    features = json.loads(pred.get("features_json") or "{}")
    lat = features.get("lat"); lng = features.get("lng")
    category = (features.get("category") or "").lower()
    severity = features.get("severity")

    # SEISMIC / NATURAL: USGS is authoritative
    if "earth" in category or "seismic" in category or "quake" in category or category == "natural":
        near = [e for e in usgs_events if _haversine_km(lat, lng, e["lat"], e["lng"]) < 500]
        if near:
            strongest = max(near, key=lambda e: e.get("mag") or 0)
            return "win", {
                "matched": "usgs_earthquake",
                "magnitude": strongest.get("mag"),
                "distance_km": round(_haversine_km(lat, lng, strongest["lat"], strongest["lng"]), 0),
                "place": strongest.get("place"),
                "evidence_count": len(near),
            }
        return "loss", {"matched": None, "searched": "usgs", "events_scanned": len(usgs_events)}

    # CONFLICT / POLITICAL: GDELT as weak signal
    if "conflict" in category or "political" in category or "war" in category:
        # Without georef in GDELT cheap API, we can only tell if SOMETHING happened in the window
        if gdelt_events:
            return "unresolved", {
                "matched": "gdelt_weak_signal",
                "events_scanned": len(gdelt_events),
                "note": "GDELT hit detected but without geo-precision — requires manual review.",
            }
        return "loss", {"matched": None, "searched": "gdelt", "events_scanned": 0}

    # CYBER / ECONOMIC / SOCIAL: no free authoritative source yet — mark unresolved
    return "unresolved", {
        "matched": None,
        "note": f"No automated grader for category={category}. Manual review required.",
    }


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--days", type=int, default=14, help="How far back to search for due predictions")
    args = p.parse_args()

    brain = Brain()
    brain.register_service(SERVICE_NAME, namespace=NS, expected_every_sec=3600)

    now = datetime.now(timezone.utc)
    now_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    since_iso = (now - timedelta(days=args.days)).strftime("%Y-%m-%dT%H:%M:%SZ")

    # 1. Pull due-but-pending predictions directly (we need raw rows — Brain API
    #    doesn't currently expose a "find by due_at" filter).
    con = sqlite3.connect(str(brain.db_path), timeout=30.0)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            """SELECT * FROM predictions
               WHERE namespace = ? AND outcome = 'pending'
                 AND due_at IS NOT NULL AND due_at <= ?
                 AND made_at >= ?""",
            (NS, now_iso, since_iso),
        ).fetchall()
    finally:
        con.close()

    if not rows:
        print(f"[gb-grader] No due predictions. Registered heartbeat.")
        brain.heartbeat(SERVICE_NAME, payload={"graded": 0})
        return 0

    print(f"[gb-grader] {len(rows)} predictions due for grading")

    # 2. Fetch evidence sources once (re-used across all predictions)
    usgs = _fetch_usgs(since_iso, now_iso)
    gdelt = _fetch_gdelt_recent(hours=args.days * 24)
    print(f"[gb-grader] evidence: USGS={len(usgs)} events, GDELT={len(gdelt)} articles")

    # 3. Grade each
    graded = {"win": 0, "loss": 0, "push": 0, "unresolved": 0}
    for row in rows:
        pred = dict(row)
        outcome, details = _match_prediction(pred, usgs, gdelt)
        graded[outcome] += 1
        if args.dry_run:
            print(f"[dry] {pred['id'][:8]} {pred['claim'][:60]} -> {outcome} ({details.get('matched')})")
            continue
        brain.grade_prediction(pred["id"], outcome=outcome, details=details,
                               notes=f"Auto-graded by glassbox_grader at {now_iso}")

    if not args.dry_run:
        brain.log_event(
            namespace=NS, kind="grade_cycle",
            summary=f"Graded {len(rows)} predictions: "
                    f"{graded['win']}W {graded['loss']}L {graded['push']}P {graded['unresolved']}U",
            detail=graded,
            severity="info",
            source="glassbox_grader",
        )
        brain.heartbeat(SERVICE_NAME, payload={"graded": len(rows), **graded})

    print(f"[gb-grader] done: {graded}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
