"""
Phase 5a — IsolationForest anomaly detection tests.

Pure-fn tests:
  - feature_vector composes the 7-feature row in the documented order
  - bucket_key groups by (event_type, region, hour_floor)
  - aggregate_buckets and assemble_matrix line up
  - train() on synthetic well-separated data produces a model that flags
    the obvious outliers via decision_function
  - score_with_isolation_forest() returns no findings when no artifact is
    on disk (graceful fallback)
  - detect_anomalies_combined() falls through to EWMA when the artifact
    is absent

Artifact-roundtrip:
  - save_artifact + load_artifact preserves the trained model + metadata
  - score_with_isolation_forest() reads the artifact correctly and flags
    obvious outliers when given live event-shape buckets

Run:
    21_GLASSBOX_AI/.venv/bin/python -m pytest 21_GLASSBOX_AI/tests/test_anomaly_isoforest.py -v
"""

from __future__ import annotations

import math
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
EMPIRE_ROOT = ROOT.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(EMPIRE_ROOT) not in sys.path:
    sys.path.insert(0, str(EMPIRE_ROOT))

from anomaly import (  # noqa: E402
    detect_anomalies_combined,
    isolation_forest_available,
    score_with_isolation_forest,
    update_baselines,
)
from infra.ml.anomaly_isolation_forest import (  # noqa: E402
    FEATURE_NAMES,
    aggregate_buckets,
    assemble_matrix,
    bucket_key,
    feature_vector,
    load_artifact,
    save_artifact,
    train,
)


# ─── Pure-fn tests ────────────────────────────────────────────────────────


def test_feature_names_order_is_stable():
    assert FEATURE_NAMES == [
        "count", "avg_severity", "max_severity", "p90_severity",
        "sev_sum", "hour_sin", "hour_cos",
    ]


def test_feature_vector_zero_severities():
    fv = feature_vector([], hour_of_day=0)
    assert len(fv) == len(FEATURE_NAMES)
    assert fv[0] == 0.0  # count
    assert fv[1] == 0.0  # avg_severity
    assert fv[5] == pytest.approx(0.0)            # sin(0) = 0
    assert fv[6] == pytest.approx(1.0)            # cos(0) = 1


def test_feature_vector_known_severities():
    fv = feature_vector([1.0, 2.0, 3.0, 8.0, 10.0], hour_of_day=12)
    assert fv[0] == 5.0
    assert fv[1] == pytest.approx(4.8)            # avg
    assert fv[2] == 10.0                          # max
    # p90 of 5 sorted [1,2,3,8,10] -> int(0.9*(5-1))=3 -> sorted[3]=8.0
    assert fv[3] == 8.0
    assert fv[4] == pytest.approx(24.0)           # sum
    # hour=12: angle = pi -> sin=0, cos=-1
    assert fv[5] == pytest.approx(0.0, abs=1e-9)
    assert fv[6] == pytest.approx(-1.0, abs=1e-9)


def test_bucket_key_groups_by_hour_floor():
    t1 = datetime(2026, 5, 9, 14, 7, tzinfo=timezone.utc)
    t2 = datetime(2026, 5, 9, 14, 59, tzinfo=timezone.utc)
    t3 = datetime(2026, 5, 9, 15, 1, tzinfo=timezone.utc)
    k1 = bucket_key("usgs_quake", 30.0, -90.0, t1)
    k2 = bucket_key("usgs_quake", 30.0, -90.0, t2)
    k3 = bucket_key("usgs_quake", 30.0, -90.0, t3)
    assert k1 == k2  # same hour
    assert k1 != k3  # different hour
    # event_type and region should be stable
    assert k1[0] == "usgs_quake"
    assert k1[1] == k2[1]


def test_bucket_key_2h_bucket_groups_pairs():
    t14 = datetime(2026, 5, 9, 14, 30, tzinfo=timezone.utc)
    t15 = datetime(2026, 5, 9, 15, 30, tzinfo=timezone.utc)
    t16 = datetime(2026, 5, 9, 16, 30, tzinfo=timezone.utc)
    k14 = bucket_key("X", 30.0, -90.0, t14, hour_bucket=2)
    k15 = bucket_key("X", 30.0, -90.0, t15, hour_bucket=2)
    k16 = bucket_key("X", 30.0, -90.0, t16, hour_bucket=2)
    assert k14 == k15  # 14h bucket
    assert k15 != k16  # 16h bucket


# ─── Trainer + scorer round-trip ──────────────────────────────────────────


def _synthetic_rows():
    """
    Synthetic event rows: 100 normal buckets with low count + low severity,
    plus 5 obvious outlier buckets with very high count and severity. The
    forest should flag the outliers.
    """
    rows = []
    base_t = datetime(2026, 5, 1, 0, 0, tzinfo=timezone.utc)
    # Normal: 100 buckets, ~3-5 events/bucket, severity 1-3
    import random
    rng = random.Random(0)
    for h in range(100):
        t = base_t.replace(hour=h % 24, day=1 + (h // 24))
        for _ in range(rng.randint(3, 5)):
            rows.append({
                "event_type": "normal_evt",
                "event_time": t,
                "lat": 30.0 + rng.uniform(-1, 1),  # north_america
                "lng": -90.0 + rng.uniform(-1, 1),
                "severity": rng.uniform(1.0, 3.0),
            })
    # Outliers: 5 buckets, 50+ events each, severity 8-10
    for o in range(5):
        t = base_t.replace(hour=o, day=15)
        for _ in range(60):
            rows.append({
                "event_type": "spike_evt",
                "event_time": t,
                "lat": 30.0 + rng.uniform(-0.1, 0.1),
                "lng": -90.0 + rng.uniform(-0.1, 0.1),
                "severity": rng.uniform(8.0, 10.0),
            })
    return rows


def test_train_on_synthetic_separates_outliers():
    rows = _synthetic_rows()
    artifact = train(rows, contamination=0.05)
    assert artifact.n_buckets >= 100
    # Predict on the original feature matrix; the outlier buckets should
    # appear among the most-anomalous (lowest decision_function scores).
    buckets = aggregate_buckets(rows)
    X, keys = assemble_matrix(buckets)
    scores = artifact.model.decision_function(X)
    # Pair (key, score) and find most-anomalous; expect spike_evt to dominate
    paired = sorted(zip(keys, scores), key=lambda kv: kv[1])  # ascending = most anomalous first
    top5 = [k[0] for k, _ in paired[:5]]
    assert "spike_evt" in top5, f"outliers not detected; top5 event types = {top5}"


def test_save_and_load_artifact_roundtrip(tmp_path):
    rows = _synthetic_rows()
    artifact = train(rows, contamination=0.05)
    out = tmp_path / "anomaly_isoforest.joblib"
    saved = save_artifact(artifact, out)
    assert saved.exists()
    reloaded = load_artifact(out)
    assert reloaded is not None
    assert reloaded.feature_names == FEATURE_NAMES
    assert reloaded.n_buckets == artifact.n_buckets
    assert reloaded.model is not None
    # Reloaded model should produce same scores on the same input.
    buckets = aggregate_buckets(rows)
    X, _ = assemble_matrix(buckets)
    s1 = artifact.model.decision_function(X)
    s2 = reloaded.model.decision_function(X)
    for a, b in zip(s1, s2):
        assert a == pytest.approx(b)


def test_score_with_isolation_forest_no_artifact_returns_empty(tmp_path):
    """No model on disk -> graceful empty-list fallback."""
    bogus = tmp_path / "does_not_exist.joblib"
    assert not bogus.exists()
    assert isolation_forest_available(bogus) is False
    out = score_with_isolation_forest(
        {("planes", "north_america"): [{"severity": 5} for _ in range(20)]},
        model_path=bogus,
    )
    assert out == []


def test_score_with_isolation_forest_flags_outlier_with_artifact(tmp_path):
    """Train + save artifact, then run live-bucket scoring against it."""
    rows = _synthetic_rows()
    artifact = train(rows, contamination=0.05)
    out_path = tmp_path / "anomaly_isoforest.joblib"
    save_artifact(artifact, out_path)

    # Reset the global cache so this test gets a fresh load.
    import anomaly as anomaly_mod
    anomaly_mod._iso_artifact = None
    anomaly_mod._iso_load_attempted = False

    # Live-style buckets: one obvious spike, one normal
    spike_events = [{"severity": 9, "external_id": f"x{i}"} for i in range(60)]
    normal_events = [{"severity": 2, "external_id": f"n{i}"} for i in range(5)]
    findings = score_with_isolation_forest(
        {
            ("spike_evt", "north_america"): spike_events,
            ("normal_evt", "north_america"): normal_events,
        },
        model_path=out_path,
    )
    layers = [f["layer"] for f in findings]
    assert "spike_evt" in layers, f"spike not flagged; got {layers}"
    spike = [f for f in findings if f["layer"] == "spike_evt"][0]
    assert spike["outlier"] is True
    assert spike["method"] == "isolation_forest"
    assert spike["count"] == 60
    assert spike["anomaly_severity"] >= 5
    assert "iso_score" in spike

    # Cleanup the global cache so it doesn't leak into other tests.
    anomaly_mod._iso_artifact = None
    anomaly_mod._iso_load_attempted = False


def test_detect_anomalies_combined_falls_back_to_ewma_without_artifact(tmp_path):
    """When no artifact on disk, behavior should match plain detect_anomalies()."""
    bogus = tmp_path / "missing.joblib"
    assert not bogus.exists()

    # Reset global cache so previous tests don't leak.
    import anomaly as anomaly_mod
    anomaly_mod._iso_artifact = None
    anomaly_mod._iso_load_attempted = False

    # Build a baseline that EWMA would consider "spike-worthy" — high z.
    baselines = {
        "planes:north_america": {"mean_count": 10.0, "std_count": 2.0, "n_cycles": 5},
    }
    spike_bucket = {
        ("planes", "north_america"): [
            {"severity": 5, "external_id": f"a{i}", "lat": 30.0, "lng": -90.0}
            for i in range(40)
        ]
    }
    findings = detect_anomalies_combined(
        spike_bucket, baselines, model_path=bogus,
    )
    assert any(f["layer"] == "planes" for f in findings)
    # method is 'ewma' since no IsoForest artifact
    methods = {f.get("method") for f in findings}
    assert methods == {"ewma"}
