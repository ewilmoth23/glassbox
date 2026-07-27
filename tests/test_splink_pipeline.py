"""
Phase 4b — Splink probabilistic ER pipeline (vessel↔sanctioned matching).

Asserts:
  - Pipeline imports + runs end-to-end without errors
  - Default-settings predict returns at least one match against a
    seeded (live, sanctioned) IMO-equal pair
  - Persist writes one entity_relation row per match; re-running is a no-op
  - Idempotency via the existing UNIQUE (from, to, relation_type, valid_from)
  - fetch_aliases_for_vessel returns the persisted rows in confidence-desc order
  - min_confidence filter on the read path works
  - The pipeline tolerates an empty input (no vessels OR no sanctioned)

Run:
    21_GLASSBOX_AI/.venv/bin/python -m pytest 21_GLASSBOX_AI/tests/test_splink_pipeline.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path
from datetime import datetime, timezone
from uuid import uuid4

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# infra/er sits one level up — add the empire root to the path
_EMPIRE_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_EMPIRE_ROOT) not in sys.path:
    sys.path.insert(0, str(_EMPIRE_ROOT))

from db import init_pool, close_pool, execute, fetchval, fetch  # noqa: E402
from infra.er.splink_pipeline import (  # noqa: E402
    predict_with_default_settings,
    persist_matches,
    fetch_aliases_for_vessel,
    VesselERResult,
    PIPELINE_VERSION,
)


TEST_PREFIX = "splink_test"


@pytest.fixture(autouse=True)
async def _pool():
    await init_pool()
    yield
    await close_pool()


@pytest.fixture
async def _clean_test_entities():
    async def _cleanup():
        # Delete entity_relation rows pointing at test entities
        await execute(
            "DELETE FROM entity_relation WHERE properties->>'pipeline' = $1 "
            "  AND (from_entity_id IN (SELECT id FROM entity WHERE canonical_id LIKE $2) "
            "    OR to_entity_id   IN (SELECT id FROM entity WHERE canonical_id LIKE $2))",
            PIPELINE_VERSION, f"{TEST_PREFIX}%",
        )
        await execute(
            "DELETE FROM entity WHERE canonical_id LIKE $1",
            f"{TEST_PREFIX}%",
        )
    await _cleanup()
    yield
    await _cleanup()


async def _seed_test_pair(*, name: str, imo: str, suffix: str = "1"):
    """Seed one (live vessel, sanctioned vessel) pair sharing name + IMO."""
    import json as _json
    v_id = await fetchval(
        """
        INSERT INTO entity (entity_type, canonical_id_type, canonical_id,
                            display_name, properties, last_seen)
        VALUES ('vessel', 'mmsi', $1, $2, $3::jsonb, NOW())
        RETURNING id
        """,
        f"{TEST_PREFIX}_v_{suffix}",
        name,
        _json.dumps({"imo": imo}),
    )
    s_id = await fetchval(
        """
        INSERT INTO entity (entity_type, canonical_id_type, canonical_id,
                            display_name, properties, last_seen)
        VALUES ('sanctioned_vessel', 'ofac_sdn_id', $1, $2, $3::jsonb, NOW())
        RETURNING id
        """,
        f"{TEST_PREFIX}_s_{suffix}",
        name,
        _json.dumps({
            "imo": imo,
            "regime": "TEST",
            "sanctioning_authority": "Test Authority",
        }),
    )
    return str(v_id), str(s_id)


# ─── Pipeline + persistence ───────────────────────────────────────────────


async def test_predict_finds_seeded_imo_pair(_clean_test_entities):
    """Seed one matching pair → predict returns at least that one match."""
    v_id, s_id = await _seed_test_pair(
        name="ZZSPLINKTEST VESSEL ALPHA",
        imo="9999991",
        suffix="alpha",
    )
    matches = await predict_with_default_settings(threshold_match_probability=0.5)

    test_match = [m for m in matches if m.vessel_id == v_id and m.sanctioned_id == s_id]
    assert len(test_match) == 1, (
        f"expected predict to surface the seeded ({v_id} <-> {s_id}) pair; "
        f"saw {len(matches)} matches total but none for this pair"
    )
    m = test_match[0]
    assert m.match_probability >= 0.5
    assert m.vessel_imo == "9999991"
    assert m.sanctioned_imo == "9999991"


async def test_persist_writes_relation_row(_clean_test_entities):
    """Persisting one match → one new entity_relation row of relation_type splink_alias."""
    v_id, s_id = await _seed_test_pair(
        name="ZZSPLINKTEST VESSEL BETA",
        imo="9999992",
        suffix="beta",
    )
    fake = VesselERResult(
        vessel_id=v_id, sanctioned_id=s_id,
        match_probability=0.97,
        vessel_name="ZZSPLINKTEST VESSEL BETA",
        sanctioned_name="ZZSPLINKTEST VESSEL BETA",
        vessel_imo="9999992", sanctioned_imo="9999992",
        feature_breakdown={"gamma_imo": 1.0},
    )
    n = await persist_matches([fake])
    assert n == 1

    rows = await fetch(
        "SELECT confidence, relation_type FROM entity_relation "
        "WHERE from_entity_id = $1::uuid AND to_entity_id = $2::uuid",
        v_id, s_id,
    )
    assert len(rows) == 1
    assert rows[0]["relation_type"] == "splink_alias"
    assert rows[0]["confidence"] == pytest.approx(0.97)


async def test_persist_is_idempotent(_clean_test_entities):
    """Re-persisting the same match → 0 new rows (UNIQUE constraint)."""
    v_id, s_id = await _seed_test_pair(
        name="ZZSPLINKTEST VESSEL GAMMA",
        imo="9999993",
        suffix="gamma",
    )
    fake = VesselERResult(
        vessel_id=v_id, sanctioned_id=s_id, match_probability=0.99,
        vessel_name="x", sanctioned_name="x",
        vessel_imo=None, sanctioned_imo=None,
        feature_breakdown={},
    )
    assert await persist_matches([fake]) == 1
    assert await persist_matches([fake]) == 0   # second call is a no-op

    n = await fetchval(
        "SELECT count(*) FROM entity_relation "
        "WHERE from_entity_id = $1::uuid AND to_entity_id = $2::uuid "
        "  AND relation_type = 'splink_alias'",
        v_id, s_id,
    )
    assert n == 1


async def test_persist_handles_empty_input():
    assert await persist_matches([]) == 0


# ─── Read API ─────────────────────────────────────────────────────────────


async def test_fetch_aliases_returns_persisted_rows_desc(_clean_test_entities):
    """One vessel matched to two sanctioned entries → 2 aliases, ordered by confidence."""
    v_id, s_id_1 = await _seed_test_pair(
        name="ZZSPLINKTEST VESSEL DELTA", imo="9999994", suffix="d1",
    )
    # Second sanctioned with same name (think: name reused under two regimes)
    import json as _json
    s_id_2 = await fetchval(
        """
        INSERT INTO entity (entity_type, canonical_id_type, canonical_id,
                            display_name, properties, last_seen)
        VALUES ('sanctioned_vessel', 'ofac_sdn_id', $1, $2, $3::jsonb, NOW())
        RETURNING id
        """,
        f"{TEST_PREFIX}_s_d2",
        "ZZSPLINKTEST VESSEL DELTA",
        _json.dumps({"imo": "9999994", "regime": "OTHER",
                     "sanctioning_authority": "Test 2"}),
    )

    await persist_matches([
        VesselERResult(
            vessel_id=v_id, sanctioned_id=str(s_id_1), match_probability=0.99,
            vessel_name="d", sanctioned_name="d",
            vessel_imo=None, sanctioned_imo=None, feature_breakdown={},
        ),
        VesselERResult(
            vessel_id=v_id, sanctioned_id=str(s_id_2), match_probability=0.85,
            vessel_name="d", sanctioned_name="d",
            vessel_imo=None, sanctioned_imo=None, feature_breakdown={},
        ),
    ])

    aliases = await fetch_aliases_for_vessel(v_id)
    assert len(aliases) == 2
    # Confidence DESC
    assert aliases[0]["confidence"] >= aliases[1]["confidence"]
    assert aliases[0]["confidence"] == pytest.approx(0.99)
    assert aliases[1]["confidence"] == pytest.approx(0.85)


async def test_fetch_aliases_min_confidence_filter(_clean_test_entities):
    """min_confidence drops rows below the cutoff."""
    v_id, s_id = await _seed_test_pair(
        name="ZZSPLINKTEST VESSEL EPSILON", imo="9999995", suffix="eps",
    )
    await persist_matches([VesselERResult(
        vessel_id=v_id, sanctioned_id=s_id, match_probability=0.65,
        vessel_name="e", sanctioned_name="e",
        vessel_imo=None, sanctioned_imo=None, feature_breakdown={},
    )])
    high = await fetch_aliases_for_vessel(v_id, min_confidence=0.9)
    assert len(high) == 0
    low = await fetch_aliases_for_vessel(v_id, min_confidence=0.5)
    assert len(low) == 1


async def test_fetch_aliases_empty_for_unknown_vessel():
    """Unknown vessel id → empty list (not an error)."""
    fake_id = str(uuid4())
    aliases = await fetch_aliases_for_vessel(fake_id)
    assert aliases == []


# ─── Phase 4c — alt-name expansion ─────────────────────────────────────────


async def test_alt_name_match_resolves_to_canonical_sanctioned_id(_clean_test_entities):
    """Sanctioned vessel has primary name 'UNRELATED PRIMARY' and an AKA
    'ZZSPLINKAKA TEST ALPHA'. A live vessel broadcasting under the AKA
    must match the canonical sanctioned entity.id even though the primary
    name differs.

    Both sides intentionally carry NULL IMO so the test isolates the
    alt-name expansion behavior from Splink's IMO-comparison default
    (which heavily penalizes one-sided NULL — proper handling is Phase
    4d follow-up)."""
    import json as _json
    s_id = await fetchval(
        """
        INSERT INTO entity (entity_type, canonical_id_type, canonical_id,
                            display_name, properties, last_seen)
        VALUES ('sanctioned_vessel', 'ofac_sdn_id', $1, $2, $3::jsonb, NOW())
        RETURNING id
        """,
        f"{TEST_PREFIX}_aka_s",
        "UNRELATED PRIMARY NAME OF SHIP",  # primary deliberately doesn't match
        _json.dumps({
            "regime": "TEST",
            "sanctioning_authority": "Test Authority",
            "alt_names": ["ZZSPLINKAKA TEST ALPHA", "ANOTHER OLD NAME"],
        }),
    )
    v_id = await fetchval(
        """
        INSERT INTO entity (entity_type, canonical_id_type, canonical_id,
                            display_name, properties, last_seen)
        VALUES ('vessel', 'mmsi', $1, $2, $3::jsonb, NOW())
        RETURNING id
        """,
        f"{TEST_PREFIX}_aka_v",
        "ZZSPLINKAKA TEST ALPHA",          # broadcasting under the AKA
        _json.dumps({}),
    )

    # Threshold 0.05 reflects Splink-untrained behavior: name-only matches
    # without IMO score around 0.09 by default because the prior
    # probability_two_random_records_match=0.0001 is conservative for our
    # corpus. Real-world recall lift requires EM training on the labeled
    # positives the foundation pipeline produces (Phase 4d). The test
    # asserts the alt-name expansion MECHANISM works regardless of the
    # default-untrained probability.
    matches = await predict_with_default_settings(threshold_match_probability=0.05)
    aka_match = [m for m in matches
                 if m.vessel_id == str(v_id) and m.sanctioned_id == str(s_id)]
    assert len(aka_match) == 1, (
        f"live vessel broadcasting an AKA must match the canonical "
        f"sanctioned entity even though the primary name differs; "
        f"saw {len(aka_match)} matches for this pair out of {len(matches)} total"
    )
    assert aka_match[0].sanctioned_candidate_kind == "alt", (
        "match should be flagged as having matched via the alt-name path"
    )
    assert aka_match[0].sanctioned_name == "ZZSPLINKAKA TEST ALPHA", (
        "sanctioned_name should reflect the specific alias that matched"
    )


async def test_train_and_save_model_writes_to_disk(_clean_test_entities, tmp_path):
    """Phase 4d-3: train_and_save_model() runs EM and writes a JSON
    model file to the path the caller specifies. Returns metadata dict."""
    from infra.er.splink_pipeline import train_and_save_model
    target = tmp_path / "splink_test_model.json"

    # Seed a couple of pairs so EM has labeled positives to learn from
    await _seed_test_pair(name="ZZSPLINKEM ALPHA", imo="8888881", suffix="em_a")
    await _seed_test_pair(name="ZZSPLINKEM BETA",  imo="8888882", suffix="em_b")
    await _seed_test_pair(name="ZZSPLINKEM GAMMA", imo="8888883", suffix="em_c")

    res = await train_and_save_model(save_path=str(target))
    assert res["error"] is None or res["stages_completed"] >= 1, (
        f"training should complete >=1 stage; got {res}"
    )
    assert res["saved_to"] == str(target)
    assert target.exists()
    # Sanity: file is non-trivial JSON
    import json as _json
    body = _json.loads(target.read_text())
    assert isinstance(body, dict)
    assert "comparisons" in body or "blocking_rules_to_generate_predictions" in body


async def test_predict_with_trained_model_path_uses_it(_clean_test_entities, tmp_path):
    """Loading a trained model file through trained_model_path= should
    produce predict results without erroring; missing file path falls
    back to defaults silently."""
    from infra.er.splink_pipeline import (
        train_and_save_model, predict_with_default_settings,
    )
    target = tmp_path / "splink_test_model.json"

    await _seed_test_pair(name="ZZSPLINKLOAD ALPHA", imo="7777771", suffix="load_a")

    # Train + load path
    await train_and_save_model(save_path=str(target))
    matches = await predict_with_default_settings(
        threshold_match_probability=0.5,
        trained_model_path=str(target),
    )
    assert isinstance(matches, list)

    # Missing-path fallback
    matches_fallback = await predict_with_default_settings(
        threshold_match_probability=0.5,
        trained_model_path=str(tmp_path / "nonexistent.json"),
    )
    assert isinstance(matches_fallback, list)


async def test_alt_name_persistence_dedups_to_one_edge_per_canonical_pair(_clean_test_entities):
    """Even if Splink finds the same (vessel, sanctioned) pair via both
    primary AND an alt name, only ONE entity_relation edge should land —
    persist_matches dedups via existing currently-valid edge check."""
    import json as _json
    s_id = await fetchval(
        """
        INSERT INTO entity (entity_type, canonical_id_type, canonical_id,
                            display_name, properties, last_seen)
        VALUES ('sanctioned_vessel', 'ofac_sdn_id', $1, $2, $3::jsonb, NOW())
        RETURNING id
        """,
        f"{TEST_PREFIX}_dedup_s",
        "ZZSPLINKDEDUP CANONICAL",
        _json.dumps({
            "imo": "1234568",
            "regime": "TEST",
            "sanctioning_authority": "Test",
            "alt_names": ["ZZSPLINKDEDUP CANONICAL"],   # alt = same as primary
        }),
    )
    v_id = await fetchval(
        """
        INSERT INTO entity (entity_type, canonical_id_type, canonical_id,
                            display_name, properties, last_seen)
        VALUES ('vessel', 'mmsi', $1, $2, $3::jsonb, NOW())
        RETURNING id
        """,
        f"{TEST_PREFIX}_dedup_v",
        "ZZSPLINKDEDUP CANONICAL",
        _json.dumps({"imo": "1234568"}),
    )

    matches = await predict_with_default_settings(threshold_match_probability=0.5)
    # Both primary + alt rows would produce two predict rows; persist must
    # collapse them into one edge.
    n_inserted = await persist_matches([
        m for m in matches
        if m.vessel_id == str(v_id) and m.sanctioned_id == str(s_id)
    ])
    assert n_inserted == 1, (
        f"expected exactly 1 inserted edge per canonical pair; got {n_inserted}"
    )

    edges = await fetchval(
        "SELECT count(*) FROM entity_relation WHERE from_entity_id = $1::uuid "
        "  AND to_entity_id = $2::uuid AND relation_type = 'splink_alias' "
        "  AND valid_to IS NULL",
        v_id, s_id,
    )
    assert edges == 1


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
