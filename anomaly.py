"""
anomaly.py — statistical anomaly detection on live Glassbox event streams.

Per layer, per region, we track:
  - rolling event count
  - rolling severity distribution
  - z-score of current cycle vs trailing baseline

If current exceeds baseline by >2σ AND sample size is meaningful, flag as anomaly.

State is persistent across cycles via the Brain (namespace="glassbox_anomaly").

Phase 5a (2026-05-10): an `sklearn.ensemble.IsolationForest` scoring path
sits alongside the EWMA baseline. The trainer is at
`infra/ml/anomaly_isolation_forest.py`; it persists a joblib artifact to
the external mewr-models/ volume. `score_with_isolation_forest()` lazy-
loads that artifact and returns per-bucket outlier flags + raw scores.
The EWMA path is preserved as a deterministic fallback (and as the
default when no model is on disk yet — fresh installs work without the
training step).
"""

from __future__ import annotations

import logging
import math
import os
import statistics
import threading
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_log = logging.getLogger("anomaly")


# Global bucket boundaries — 18 world regions roughly matching OSINT mental model
_REGIONS = [
    # (name, lat_min, lat_max, lng_min, lng_max)
    ("north_america",   15, 75,  -170, -50),
    ("central_america", 5, 25,   -110, -75),
    ("south_america",   -60, 15, -90, -35),
    ("europe_west",     35, 72,  -15, 20),
    ("europe_east",     35, 72,  20, 45),
    ("russia",          45, 82,  45, 180),
    ("middle_east",     12, 45,  25, 65),
    ("africa_north",    15, 37,  -20, 55),
    ("africa_sub",      -40, 15, -20, 55),
    ("south_asia",      5, 40,   60, 95),
    ("japan_korea",     25, 48,  122, 150),   # checked before china so Japan wins
    ("china",           15, 55,  70, 122),
    ("se_asia",         -15, 25, 90, 145),
    ("oceania",         -50, 0,  110, 180),
    ("pacific",         -40, 40, 140, -120),  # wraps the dateline
    ("atlantic",        -40, 40, -65, -15),
    ("indian_ocean",    -40, 25, 40, 100),
    ("arctic",          60, 90,  -180, 180),
]


def region_for(lat: float, lng: float) -> str:
    for name, la_min, la_max, ln_min, ln_max in _REGIONS:
        if la_min <= lat <= la_max:
            if ln_min <= ln_max:
                if ln_min <= lng <= ln_max:
                    return name
            else:
                # wrapping (pacific)
                if lng >= ln_min or lng <= ln_max:
                    return name
    return "other"


def bucket_events(events: List[Dict[str, Any]]) -> Dict[Tuple[str, str], List[Dict[str, Any]]]:
    """Group events by (layer, region)."""
    out: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
    for e in events:
        try:
            layer = e.get("layer") or "unknown"
            lat = float(e.get("lat"))
            lng = float(e.get("lng"))
        except Exception:
            continue
        out[(layer, region_for(lat, lng))].append(e)
    return out


def compute_stats(events: List[Dict[str, Any]]) -> Dict[str, float]:
    n = len(events)
    if n == 0:
        return {"count": 0, "avg_severity": 0.0, "max_severity": 0, "sev_sum": 0}
    sevs = [int(e.get("severity") or 0) for e in events]
    return {
        "count": n,
        "avg_severity": round(statistics.mean(sevs), 2),
        "max_severity": max(sevs),
        "sev_sum": sum(sevs),
    }


def detect_anomalies(
    current_buckets: Dict[Tuple[str, str], List[Dict[str, Any]]],
    baselines: Dict[str, Dict[str, float]],
    min_sample: int = 5,
    z_threshold: float = 2.0,
) -> List[Dict[str, Any]]:
    """
    Compare each (layer, region) bucket to its rolling baseline.
    Returns a list of anomaly dicts with a severity score 1-10.

    baselines shape: {"layer:region": {"mean_count": X, "std_count": Y, "n_cycles": Z}}
    """
    anomalies: List[Dict[str, Any]] = []
    for (layer, region), events in current_buckets.items():
        stats = compute_stats(events)
        if stats["count"] < min_sample:
            continue
        key = f"{layer}:{region}"
        base = baselines.get(key) or {}
        mean_c = float(base.get("mean_count") or 0)
        std_c = max(1.0, float(base.get("std_count") or 1))
        n_cycles = int(base.get("n_cycles") or 0)
        if n_cycles < 3:
            # Not enough history to call anything anomalous yet
            continue
        z = (stats["count"] - mean_c) / std_c
        if abs(z) < z_threshold:
            continue

        # Severity ramps with z-score AND max_severity of events in bucket
        sev = min(10, max(3, int(round(abs(z) + stats["max_severity"] / 2))))
        anomalies.append({
            "layer": layer,
            "region": region,
            "count": stats["count"],
            "baseline_mean": round(mean_c, 2),
            "baseline_std": round(std_c, 2),
            "z_score": round(z, 2),
            "max_severity": stats["max_severity"],
            "avg_severity": stats["avg_severity"],
            "direction": "spike" if z > 0 else "dropout",
            "anomaly_severity": sev,
            "detected_at": datetime.now(timezone.utc).isoformat(),
            "sample": [
                {"external_id": ev.get("external_id"), "lat": ev.get("lat"),
                 "lng": ev.get("lng"), "severity": ev.get("severity"),
                 "payload": ev.get("payload", {})}
                for ev in events[:5]
            ],
        })
    anomalies.sort(key=lambda a: a["anomaly_severity"], reverse=True)
    return anomalies


def update_baselines(
    baselines: Dict[str, Dict[str, float]],
    current_buckets: Dict[Tuple[str, str], List[Dict[str, Any]]],
    alpha: float = 0.25,
) -> Dict[str, Dict[str, float]]:
    """
    EWMA update. New observation blends into the baseline with weight alpha.
    Preserved as a deterministic fallback path; the IsolationForest scorer
    below is the preferred upgrade once a trained artifact is on disk.
    """
    for (layer, region), events in current_buckets.items():
        key = f"{layer}:{region}"
        count = len(events)
        base = baselines.get(key) or {"mean_count": count, "std_count": 1.0, "n_cycles": 0}
        old_mean = float(base.get("mean_count") or 0)
        old_std = float(base.get("std_count") or 1)
        n = int(base.get("n_cycles") or 0) + 1
        new_mean = (1 - alpha) * old_mean + alpha * count
        # Track squared deviations in an EWMA fashion
        dev = count - new_mean
        new_var = (1 - alpha) * (old_std ** 2) + alpha * (dev ** 2)
        new_std = max(1.0, math.sqrt(new_var))
        baselines[key] = {"mean_count": new_mean, "std_count": new_std, "n_cycles": n}
    return baselines


# ─── Phase 5a: IsolationForest scoring path ────────────────────────────────
#
# The model is trained offline by `infra/ml/anomaly_isolation_forest.py` and
# saved as a joblib pickle on the external mewr-models/ volume. Live scoring
# loads the artifact lazily on first call (thread-safe) and reuses it for
# the lifetime of the process.
#
# This path is additive — `detect_anomalies()` above continues to work
# untouched. Callers can opt into the forest by calling
# `score_with_isolation_forest()` directly, or use `detect_anomalies_combined()`
# which runs both paths and merges the findings.

_DEFAULT_MODEL_PATH = Path(
    os.environ.get(
        "GLASSBOX_ANOMALY_MODEL",
        "/Volumes/Mac Mini Expanded Storage/ewilmoth/mewr-models/anomaly_isoforest.joblib",
    )
)

_iso_artifact = None
_iso_load_attempted = False
_iso_load_lock = threading.Lock()


def _load_iso_artifact(path: Path = _DEFAULT_MODEL_PATH):
    """Lazily load the joblib artifact. Returns None if absent / load fails."""
    global _iso_artifact, _iso_load_attempted
    if _iso_artifact is not None:
        return _iso_artifact
    if _iso_load_attempted and _iso_artifact is None:
        return None
    with _iso_load_lock:
        if _iso_artifact is not None:
            return _iso_artifact
        if _iso_load_attempted and _iso_artifact is None:
            return None
        _iso_load_attempted = True
        try:
            import joblib  # noqa: F401
        except Exception as e:
            _log.info(f"joblib import failed; isolation-forest path disabled: {e}")
            return None
        if not path.exists():
            _log.info(f"no IsolationForest artifact at {path}; falling back to EWMA")
            return None
        try:
            import joblib
            _iso_artifact = joblib.load(path)
            _log.info(
                f"loaded IsolationForest artifact: {getattr(_iso_artifact, 'n_buckets', '?')} "
                f"training buckets, trained_at={getattr(_iso_artifact, 'trained_at', '?')}"
            )
        except Exception as e:
            _log.warning(f"IsolationForest artifact load failed: {e}")
            _iso_artifact = None
    return _iso_artifact


def isolation_forest_available(path: Path = _DEFAULT_MODEL_PATH) -> bool:
    """True iff the joblib artifact exists and loads successfully."""
    return _load_iso_artifact(path) is not None


def _bucket_features(events: List[Dict[str, Any]], hour_of_day: int) -> List[float]:
    """Build the per-bucket feature row matching the trainer's FEATURE_NAMES order."""
    n = len(events)
    if n == 0:
        return [0.0, 0.0, 0.0, 0.0, 0.0, math.sin(2.0 * math.pi * hour_of_day / 24.0),
                math.cos(2.0 * math.pi * hour_of_day / 24.0)]
    sevs = [float(e.get("severity") or 0.0) for e in events]
    avg = sum(sevs) / n
    mx = max(sevs)
    s_sorted = sorted(sevs)
    p90 = s_sorted[int(0.9 * (n - 1))]
    angle = 2.0 * math.pi * hour_of_day / 24.0
    return [float(n), avg, mx, p90, sum(sevs), math.sin(angle), math.cos(angle)]


def score_with_isolation_forest(
    current_buckets: Dict[Tuple[str, str], List[Dict[str, Any]]],
    *,
    min_sample: int = 5,
    now: Optional[datetime] = None,
    model_path: Path = _DEFAULT_MODEL_PATH,
) -> List[Dict[str, Any]]:
    """
    Score each (layer, region) bucket with a trained IsolationForest.

    Returns a list of anomaly dicts (same shape as detect_anomalies for
    downstream consumers) including:
        outlier (bool)         -- True iff sklearn predicts -1
        iso_score (float)      -- raw decision_function score (lower = more anomalous)
        anomaly_severity (1-10)

    If no trained artifact is on disk, returns []. Caller should fall back
    to the EWMA path or to detect_anomalies_combined().
    """
    art = _load_iso_artifact(model_path)
    if art is None:
        return []
    model = getattr(art, "model", None)
    if model is None:
        return []

    now = now or datetime.now(timezone.utc)
    hour_of_day = now.hour

    keys: List[Tuple[str, str]] = []
    rows: List[List[float]] = []
    for (layer, region), events in current_buckets.items():
        if len(events) < min_sample:
            continue
        keys.append((layer, region))
        rows.append(_bucket_features(events, hour_of_day))

    if not rows:
        return []

    try:
        preds = model.predict(rows)             # +1 normal, -1 outlier
        scores = model.decision_function(rows)  # higher = more normal
    except Exception as e:
        _log.warning(f"IsolationForest prediction failed: {e}")
        return []

    out: List[Dict[str, Any]] = []
    for (layer, region), feats, pred, raw in zip(keys, rows, preds, scores):
        if int(pred) != -1:
            continue  # not flagged
        events = current_buckets[(layer, region)]
        sevs = [int(e.get("severity") or 0) for e in events]
        max_sev = max(sevs) if sevs else 0
        # Map decision_function score to severity 3-10. The trainer's
        # contamination=0.05 means decision_function is roughly ~0 at the
        # boundary, with outliers <0. Clamp + scale.
        # The more negative the score, the more anomalous → higher severity.
        sev = min(10, max(3, int(round(3 + abs(min(0.0, float(raw))) * 30 + max_sev / 2))))
        out.append({
            "layer": layer,
            "region": region,
            "count": len(events),
            "iso_score": round(float(raw), 4),
            "outlier": True,
            "max_severity": max_sev,
            "avg_severity": round(sum(sevs) / max(1, len(sevs)), 2),
            "anomaly_severity": sev,
            "method": "isolation_forest",
            "detected_at": now.isoformat(),
            "sample": [
                {"external_id": ev.get("external_id"), "lat": ev.get("lat"),
                 "lng": ev.get("lng"), "severity": ev.get("severity"),
                 "payload": ev.get("payload", {})}
                for ev in events[:5]
            ],
        })
    out.sort(key=lambda a: a["iso_score"])  # most-anomalous first (most-negative score)
    return out


def detect_anomalies_combined(
    current_buckets: Dict[Tuple[str, str], List[Dict[str, Any]]],
    baselines: Dict[str, Dict[str, float]],
    *,
    min_sample: int = 5,
    z_threshold: float = 2.0,
    model_path: Path = _DEFAULT_MODEL_PATH,
) -> List[Dict[str, Any]]:
    """
    Run both the EWMA z-score path and the IsolationForest path; merge
    findings. A bucket flagged by both paths gets `method='ewma+iso'`. If
    the IsolationForest artifact is unavailable, this is identical to
    `detect_anomalies()`.
    """
    ewma = detect_anomalies(current_buckets, baselines,
                            min_sample=min_sample, z_threshold=z_threshold)
    for a in ewma:
        a.setdefault("method", "ewma")
    iso = score_with_isolation_forest(current_buckets, min_sample=min_sample,
                                      model_path=model_path)
    if not iso:
        return ewma

    # Index ewma findings by (layer, region) and merge.
    by_key: Dict[Tuple[str, str], Dict[str, Any]] = {(a["layer"], a["region"]): a for a in ewma}
    merged: List[Dict[str, Any]] = list(ewma)
    for ia in iso:
        k = (ia["layer"], ia["region"])
        if k in by_key:
            existing = by_key[k]
            existing["iso_score"] = ia["iso_score"]
            existing["method"] = "ewma+iso"
            # Boost severity slightly when both methods agree.
            existing["anomaly_severity"] = min(10, max(existing["anomaly_severity"],
                                                       ia["anomaly_severity"]) + 1)
        else:
            merged.append(ia)
    merged.sort(key=lambda a: a["anomaly_severity"], reverse=True)
    return merged
