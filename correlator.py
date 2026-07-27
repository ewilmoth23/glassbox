"""
correlator.py — cross-layer event correlation via geo + time proximity.

Given a set of events from multiple layers (planes, ships, earthquakes,
news, social, ...), find clusters of events close enough in space + time
to suggest a real-world causal or suspicious relationship.

Simple approach (proven in Session 71 Cognitive Fusion): grid the world
into 5° × 5° cells, group events by cell, find cells with events from
≥2 different layers in the same time window.

Then apply 20 cascade rules (earthquake+nuclear=critical, conflict+cyber=
hybrid warfare, disease+humanitarian=pandemic strain, etc.) to flag the
high-severity combos.

Phase 5c (2026-05-10) — `audit_firing_rates()` runs a per-rule binomial
test (`scipy.stats.binomtest`) comparing today's firing rate against a
historical baseline. Rules firing more than `elevation_threshold` × their
baseline rate AND with p-value below threshold are tagged 'elevated' in
the output. The intent: surface when "today's earthquake+nuclear cascades
are 3× normal — investigate" without manually reviewing the 20-rule grid.
"""

from __future__ import annotations

import logging
import math
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

_log = logging.getLogger("correlator")


# Cascade rules: (layer_a, layer_b) → (severity 0-10, narrative)
# Based on Session 71 Cognitive Fusion rules, refined.
CASCADE_RULES: List[Tuple[str, str, int, str]] = [
    ("earthquakes", "nuclear",      10, "Nuclear facility seismic risk"),
    ("earthquakes", "tsunamis",     10, "Tsunami generation risk"),
    ("conflict",    "nuclear",      10, "Armed conflict near nuclear infrastructure"),
    ("conflict",    "humanitarian",  9, "Humanitarian crisis escalation"),
    ("conflict",    "cyber",         9, "Hybrid warfare indicators"),
    ("conflict",    "planes",        7, "Military airspace activity"),
    ("disease",     "humanitarian",  8, "Pandemic response strain"),
    ("conflict",    "refugees",      8, "Displacement surge"),
    ("protests",    "conflict",      8, "Civil unrest escalation"),
    ("cyber",       "finance",       9, "Financial system cyber attack"),
    ("fires",       "airQuality",    6, "Air quality emergency"),
    ("earthquakes", "fires",         7, "Secondary fire outbreak"),
    ("ships",       "conflict",      6, "Maritime traffic in conflict zone"),
    ("planes",      "conflict",      7, "Civilian aircraft near conflict"),
    ("sanctions",   "finance",       7, "Sanctions enforcement impact"),
    ("social",      "protests",      5, "Social unrest signals"),
    ("satellites",  "conflict",      8, "Satellite monitoring of conflict"),
    ("weather",     "planes",        4, "Aviation weather disruption"),
    ("weather",     "ships",         5, "Maritime weather risk"),
    ("elections",   "protests",      8, "Electoral tension"),
]

# Quick lookup
_RULE_INDEX = {frozenset((a, b)): (sev, label) for a, b, sev, label in CASCADE_RULES}


def _grid_cell(lng: float, lat: float, size_deg: float = 5.0) -> Tuple[int, int]:
    return (int(math.floor(lng / size_deg)), int(math.floor(lat / size_deg)))


def find_correlations(
    events: List[Dict[str, Any]],
    grid_size_deg: float = 5.0,
    min_layers: int = 2,
) -> List[Dict[str, Any]]:
    """
    Return a list of correlation clusters sorted by severity.
    Each cluster has: center_lat/lng, layers_present, events, cascade_rules_fired, severity.
    """
    # Group events by cell
    cells: Dict[Tuple[int, int], List[Dict[str, Any]]] = defaultdict(list)
    for ev in events:
        try:
            lat = float(ev.get("lat"))
            lng = float(ev.get("lng"))
        except Exception:
            continue
        cells[_grid_cell(lng, lat, grid_size_deg)].append(ev)

    clusters: List[Dict[str, Any]] = []
    for (cx, cy), bucket in cells.items():
        layers = set(ev.get("layer") for ev in bucket if ev.get("layer"))
        if len(layers) < min_layers:
            continue

        # Check cascade rules
        fired: List[Dict[str, Any]] = []
        for a, b, sev, label in CASCADE_RULES:
            if a in layers and b in layers:
                fired.append({"layers": [a, b], "severity": sev, "rule": label})

        # Overall severity = max of fired rules, else count+max-event-severity blend
        if fired:
            severity = max(r["severity"] for r in fired)
        else:
            max_ev_sev = max([int(ev.get("severity") or 0) for ev in bucket] + [0])
            severity = min(8, 2 + len(layers) + max_ev_sev // 3)

        lats = [float(ev.get("lat")) for ev in bucket if ev.get("lat") is not None]
        lngs = [float(ev.get("lng")) for ev in bucket if ev.get("lng") is not None]
        clusters.append({
            "center_lat": round(sum(lats) / len(lats), 3) if lats else None,
            "center_lng": round(sum(lngs) / len(lngs), 3) if lngs else None,
            "layers_present": sorted(layers),
            "event_count": len(bucket),
            "cascade_rules_fired": fired,
            "severity": severity,
            "sample_events": [
                {"layer": ev.get("layer"), "external_id": ev.get("external_id"),
                 "severity": ev.get("severity"),
                 "summary": (ev.get("payload") or {}).get("callsign")
                            or (ev.get("payload") or {}).get("name")
                            or (ev.get("payload") or {}).get("title")
                            or (ev.get("payload") or {}).get("place")}
                for ev in bucket[:6]
            ],
            "detected_at": datetime.now(timezone.utc).isoformat(),
        })

    clusters.sort(key=lambda c: c["severity"], reverse=True)
    return clusters


# ─── Phase 5c: cascade-rule firing-rate audit ─────────────────────────────


def _count_rule_firings(clusters: List[Dict[str, Any]]) -> Counter:
    """Count how many clusters fired each cascade rule (by label)."""
    c = Counter()
    for cl in clusters:
        for r in cl.get("cascade_rules_fired") or []:
            label = r.get("rule")
            if label:
                c[label] += 1
    return c


def audit_firing_rates(
    today_clusters: List[Dict[str, Any]],
    history_clusters: List[Dict[str, Any]],
    *,
    elevation_threshold: float = 3.0,
    p_value_threshold: float = 0.05,
    min_today_firings: int = 2,
) -> List[Dict[str, Any]]:
    """
    Compare today's cascade-rule firing rates to a historical baseline.

    For each cascade rule:
      today_rate    = today_firings / max(1, today_total_clusters)
      baseline_rate = history_firings / max(1, history_total_clusters)
      elevation     = today_rate / max(epsilon, baseline_rate)
      p_value       = scipy.stats.binomtest(today_firings, today_total,
                                            baseline_rate, alternative='greater').pvalue

    A rule is tagged 'elevated' iff:
      today_firings >= min_today_firings    AND
      elevation     >  elevation_threshold  AND
      p_value       <  p_value_threshold

    Returns the per-rule audit list, sorted by elevation descending.
    Each entry has keys: rule, today_firings, today_total, today_rate,
    history_firings, history_total, baseline_rate, elevation, p_value,
    elevated.

    Rules that have never fired in either window are omitted (no signal).
    """
    today_n = max(0, len(today_clusters))
    hist_n = max(0, len(history_clusters))
    today_counts = _count_rule_firings(today_clusters)
    hist_counts = _count_rule_firings(history_clusters)

    try:
        from scipy.stats import binomtest  # noqa: E402
    except Exception as e:
        _log.info(f"scipy.stats.binomtest unavailable; firing-rate audit disabled: {e}")
        binomtest = None  # type: ignore

    out: List[Dict[str, Any]] = []
    rules_seen = set(today_counts.keys()) | set(hist_counts.keys())
    for label in rules_seen:
        today_k = int(today_counts.get(label, 0))
        hist_k = int(hist_counts.get(label, 0))
        # Skip rules with no signal in either window.
        if today_k == 0 and hist_k == 0:
            continue

        today_rate = today_k / today_n if today_n > 0 else 0.0
        baseline_rate = hist_k / hist_n if hist_n > 0 else 0.0

        # Elevation: today vs baseline. If baseline is 0, we use a small
        # epsilon to avoid division-by-zero — interpretation is "today is
        # a strict positive rate vs an exactly-zero baseline." A finite
        # large number captures the magnitude reasonably.
        epsilon = 1e-6
        elevation = (today_rate / max(epsilon, baseline_rate)) if today_rate > 0 else 0.0

        if binomtest is not None and today_n > 0 and 0.0 <= baseline_rate <= 1.0:
            try:
                test = binomtest(today_k, today_n, baseline_rate, alternative="greater")
                p = float(test.pvalue)
            except Exception as e:
                _log.info(f"binomtest failed for rule '{label}': {e}")
                p = 1.0
        else:
            p = 1.0

        elevated = (
            today_k >= min_today_firings
            and elevation > elevation_threshold
            and p < p_value_threshold
        )

        out.append({
            "rule": label,
            "today_firings": today_k,
            "today_total": today_n,
            "today_rate": round(today_rate, 6),
            "history_firings": hist_k,
            "history_total": hist_n,
            "baseline_rate": round(baseline_rate, 6),
            "elevation": round(elevation, 3) if math.isfinite(elevation) else None,
            "p_value": round(p, 6),
            "elevated": bool(elevated),
        })

    out.sort(
        key=lambda r: (
            0 if r["elevated"] else 1,                            # elevated first
            -(r["elevation"] if r["elevation"] is not None else 0),
            r["p_value"],
        ),
    )
    return out
