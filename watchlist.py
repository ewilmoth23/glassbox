"""
watchlist.py — user-defined geofence + severity alerts.

A watchlist is:
    {
      id: "wl_<hash>",
      email: "alice@example.com",
      label: "Japan earthquakes M5+",
      layers: ["earthquakes"],
      center_lat: 35.0, center_lng: 139.0,
      radius_km: 1000,
      min_severity: 6,
      slack_webhook: "https://hooks.slack.com/..." (optional),
      enabled: True,
      created_at: "...",
      last_fired_at: null,
      fire_count: 0,
    }

Storage: one JSON blob per watchlist in the Brain (namespace="watchlist").
`list_watchlists()` loads all; `evaluate()` checks each against the latest
intel-loop cycle output and fires notifications for matches.

Firing rules:
  - At most one fire per watchlist per cycle (5 min)
  - A watchlist that keeps matching the SAME event id (same earthquake over
    multiple cycles) is de-duplicated via `last_fired_event_ids` on the
    watchlist record. Only NEW event IDs trigger.

Notification channels (MVP):
  - Slack webhook (per watchlist OR global SLACK_WEBHOOK_URL env)
  - Brain event log (always)
  - Email via Beehiiv/SMTP (deferred to Phase C.2)
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import sys
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT / "20_HOLDING_BRAIN" / "memory") not in sys.path:
    sys.path.insert(0, str(_ROOT / "20_HOLDING_BRAIN" / "memory"))

try:
    from brain import Brain  # type: ignore
    _BRAIN_OK = True
except Exception:
    _BRAIN_OK = False

log = logging.getLogger("watchlist")

NAMESPACE = "watchlist"
MAX_PER_USER = 25


# ─── Data model ────────────────────────────────────────────────────────────

@dataclass
class Watchlist:
    id: str
    email: str
    label: str
    layers: List[str]
    center_lat: float
    center_lng: float
    radius_km: float
    min_severity: int = 5
    slack_webhook: Optional[str] = None
    enabled: bool = True
    created_at: str = ""
    last_fired_at: Optional[str] = None
    fire_count: int = 0
    last_fired_event_ids: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _new_id(email: str, label: str, lat: float, lng: float) -> str:
    h = hashlib.sha256(f"{email}|{label}|{lat}|{lng}|{datetime.utcnow().timestamp()}".encode()).hexdigest()
    return "wl_" + h[:12]


# ─── Storage (Brain-backed) ────────────────────────────────────────────────

def list_watchlists(email: Optional[str] = None) -> List[Watchlist]:
    if not _BRAIN_OK:
        return []
    try:
        brain = Brain()
        # Use a wide recall query then filter — our Brain doesn't have
        # structured SQL access from here, but predicate="config" + the
        # namespace already narrows nicely.
        import sqlite3
        con = sqlite3.connect(str(brain.db_path), timeout=10.0)
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT object FROM facts WHERE namespace=? AND predicate='config' "
            "ORDER BY created_at DESC LIMIT 500",
            (NAMESPACE,),
        ).fetchall()
        con.close()
    except Exception as e:
        log.warning(f"list_watchlists failed: {e}")
        return []

    out: List[Watchlist] = []
    for r in rows:
        try:
            d = json.loads(r["object"])
            if not isinstance(d, dict):
                continue
            if email and d.get("email") != email:
                continue
            out.append(Watchlist(**d))
        except Exception:
            continue
    return out


def save_watchlist(wl: Watchlist) -> bool:
    if not _BRAIN_OK:
        return False
    try:
        brain = Brain()
        brain.remember(
            namespace=NAMESPACE,
            predicate="config",
            subject=wl.id,
            object=json.dumps(wl.to_dict(), default=str),
            source="watchlist.py",
            tags=f"watchlist,{wl.email},{','.join(wl.layers)}",
        )
        return True
    except Exception as e:
        log.warning(f"save_watchlist failed: {e}")
        return False


def delete_watchlist(wl_id: str) -> bool:
    if not _BRAIN_OK:
        return False
    try:
        import sqlite3
        brain = Brain()
        con = sqlite3.connect(str(brain.db_path), timeout=10.0)
        con.execute(
            "DELETE FROM facts WHERE namespace=? AND predicate='config' AND subject=?",
            (NAMESPACE, wl_id),
        )
        con.commit()
        con.close()
        return True
    except Exception as e:
        log.warning(f"delete_watchlist failed: {e}")
        return False


def create_watchlist(
    email: str, label: str, layers: List[str],
    center_lat: float, center_lng: float, radius_km: float,
    min_severity: int = 5, slack_webhook: Optional[str] = None,
) -> Optional[Watchlist]:
    # Rate-limit per email
    existing = [w for w in list_watchlists(email=email) if w.enabled]
    if len(existing) >= MAX_PER_USER:
        log.info(f"watchlist cap reached for {email}")
        return None
    wl = Watchlist(
        id=_new_id(email, label, center_lat, center_lng),
        email=email.strip().lower(),
        label=label.strip()[:120],
        layers=[l.strip() for l in layers if l.strip()][:6],
        center_lat=float(center_lat),
        center_lng=float(center_lng),
        radius_km=max(1.0, min(20000.0, float(radius_km))),
        min_severity=max(0, min(10, int(min_severity))),
        slack_webhook=slack_webhook,
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    if save_watchlist(wl):
        return wl
    return None


# ─── Geo math ──────────────────────────────────────────────────────────────

def _haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    R = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlmb / 2) ** 2
    return 2 * R * math.asin(math.sqrt(max(0, a)))


# ─── Evaluation ────────────────────────────────────────────────────────────

def evaluate(
    watchlists: List[Watchlist],
    events: List[Dict[str, Any]],
    anomalies: List[Dict[str, Any]],
    correlations: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    Return a list of fired-alert records. One record per (watchlist, match).
    Caller is responsible for dispatching notifications + persisting updates.
    """
    fired: List[Dict[str, Any]] = []
    for wl in watchlists:
        if not wl.enabled:
            continue
        matches = _find_matches(wl, events, anomalies, correlations)
        if not matches:
            continue
        # Dedupe against recently-fired event ids
        prev = set(wl.last_fired_event_ids or [])
        new_matches = [m for m in matches if m.get("event_id") not in prev]
        if not new_matches:
            continue
        fired.append({
            "watchlist": wl.to_dict(),
            "matches": new_matches[:5],
            "fired_at": datetime.now(timezone.utc).isoformat(),
        })
    return fired


def _find_matches(
    wl: Watchlist,
    events: List[Dict[str, Any]],
    anomalies: List[Dict[str, Any]],
    correlations: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    matches: List[Dict[str, Any]] = []

    # Direct event matches
    for ev in events:
        if wl.layers and ev.get("layer") not in wl.layers:
            continue
        try:
            lat = float(ev.get("lat"))
            lng = float(ev.get("lng"))
        except Exception:
            continue
        sev = int(ev.get("severity") or 0)
        if sev < wl.min_severity:
            continue
        d = _haversine_km(wl.center_lat, wl.center_lng, lat, lng)
        if d > wl.radius_km:
            continue
        matches.append({
            "kind": "event",
            "event_id": f"{ev.get('layer')}:{ev.get('external_id')}",
            "layer": ev.get("layer"),
            "lat": lat, "lng": lng,
            "severity": sev,
            "distance_km": round(d, 1),
            "summary": (ev.get("payload") or {}).get("title")
                or (ev.get("payload") or {}).get("name")
                or (ev.get("payload") or {}).get("place")
                or ev.get("external_id"),
        })

    # Anomaly matches — compared by region center approximation
    for a in anomalies:
        if wl.layers and a.get("layer") not in wl.layers:
            continue
        sev = int(a.get("anomaly_severity") or 0)
        if sev < wl.min_severity:
            continue
        # Use first sample point as a stand-in for region center
        sample = (a.get("sample") or [{}])[0]
        try:
            lat = float(sample.get("lat"))
            lng = float(sample.get("lng"))
        except Exception:
            continue
        d = _haversine_km(wl.center_lat, wl.center_lng, lat, lng)
        if d > wl.radius_km:
            continue
        matches.append({
            "kind": "anomaly",
            "event_id": f"anomaly:{a.get('layer')}:{a.get('region')}:{sev}",
            "layer": a.get("layer"),
            "region": a.get("region"),
            "severity": sev,
            "z_score": a.get("z_score"),
            "distance_km": round(d, 1),
            "summary": f"{a.get('direction')} in {a.get('layer')} · {a.get('region')} · z={a.get('z_score')}",
        })

    # Correlation clusters — any cluster whose center is within radius
    for c in correlations:
        try:
            lat = float(c.get("center_lat"))
            lng = float(c.get("center_lng"))
        except Exception:
            continue
        sev = int(c.get("severity") or 0)
        if sev < wl.min_severity:
            continue
        layers_present = set(c.get("layers_present") or [])
        if wl.layers and not (set(wl.layers) & layers_present):
            continue
        d = _haversine_km(wl.center_lat, wl.center_lng, lat, lng)
        if d > wl.radius_km:
            continue
        rules = c.get("cascade_rules_fired") or []
        matches.append({
            "kind": "correlation",
            "event_id": "correlation:" + "+".join(sorted(layers_present)) + f"@{round(lat,1)},{round(lng,1)}",
            "layers": sorted(layers_present),
            "severity": sev,
            "distance_km": round(d, 1),
            "cascade_rule": rules[0].get("rule") if rules else None,
            "summary": f"Cross-layer cluster: {' + '.join(sorted(layers_present))}"
                       + (f" · {rules[0].get('rule')}" if rules else ""),
        })

    return matches


# ─── Notification dispatch ─────────────────────────────────────────────────

def _post_slack(webhook_url: str, text: str) -> bool:
    try:
        body = json.dumps({"text": text}).encode("utf-8")
        req = urllib.request.Request(
            webhook_url, data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            r.read()
        return True
    except Exception as e:
        log.info(f"slack post failed: {e}")
        return False


def dispatch_alerts(fired: List[Dict[str, Any]]) -> int:
    """
    Post Slack alerts, log Brain events, update last_fired bookkeeping.
    Returns count of successfully dispatched alerts.
    """
    if not fired:
        return 0
    global_webhook = os.environ.get("SLACK_WEBHOOK_URL", "")
    sent = 0
    brain = None
    if _BRAIN_OK:
        try: brain = Brain()
        except Exception: brain = None

    for f in fired:
        wl_dict = f["watchlist"]
        matches = f["matches"]
        label = wl_dict.get("label", "Unnamed watchlist")
        lines = [f":rotating_light: *Glassbox alert · {label}*"]
        for m in matches[:3]:
            lines.append(
                f"• [{m.get('kind', '').upper()}] *{m.get('layer', '')}*"
                f" · sev `{m.get('severity')}` · {m.get('distance_km')}km"
                f" · {m.get('summary', '')[:140]}"
            )
        lines.append(f"<https://mewrcreate.com/glassbox|Open Glassbox>")
        text = "\n".join(lines)

        webhook = wl_dict.get("slack_webhook") or global_webhook
        if webhook:
            if _post_slack(webhook, text):
                sent += 1

        # Update last_fired on the watchlist
        if brain:
            try:
                updated = Watchlist(**wl_dict)
                updated.last_fired_at = f["fired_at"]
                updated.fire_count = int(updated.fire_count or 0) + 1
                existing_ids = set(updated.last_fired_event_ids or [])
                for m in matches:
                    if m.get("event_id"):
                        existing_ids.add(m["event_id"])
                # Keep last 200
                updated.last_fired_event_ids = list(existing_ids)[-200:]
                save_watchlist(updated)
                brain.log_event(
                    namespace="glassbox", kind="watchlist_fired",
                    summary=f"Watchlist '{label}' fired · {len(matches)} match(es)",
                    detail={"watchlist_id": updated.id, "email": updated.email,
                            "matches": matches[:5]},
                    severity="warn", source="watchlist.py",
                )
            except Exception as e:
                log.warning(f"brain bookkeeping failed: {e}")
    return sent
