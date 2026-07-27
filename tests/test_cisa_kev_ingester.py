"""
CISA KEV ingester tests — P2-A Phase 1 MVP.

Source: https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json
License: CC0 (US Government public domain).

Asserts the ingester's pure transformation logic (normalize() and severity
helpers) against representative samples of the KEV JSON shape. No network
calls — sample data is hand-built off the published CISA schema:
  - top-level: { title, catalogVersion, dateReleased, count, vulnerabilities[] }
  - per-entry: cveID, vendorProject, product, vulnerabilityName, dateAdded,
               shortDescription, requiredAction, dueDate,
               knownRansomwareCampaignUse, notes, cwes[]

Severity scheme tested below: base 7 for any KEV entry (all are exploited
in the wild by definition), +2 if knownRansomwareCampaignUse=='Known',
+1 if added within the last 30 days, capped at 10.

Run:
    21_GLASSBOX_AI/.venv/bin/python -m pytest \\
        21_GLASSBOX_AI/tests/test_cisa_kev_ingester.py -v
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ingesters.cisa_kev import (  # noqa: E402
    CisaKevIngester,
    _severity_for_kev,
    _parse_kev_date,
)
from ingesters.base import GlassboxEvent  # noqa: E402
from db import init_pool, close_pool, fetch, execute  # noqa: E402
from writers import write_cisa_kev_events  # noqa: E402


TEST_CVE_PREFIX = "CVE-2099-99"   # synthetic future-dated CVEs for test isolation


# ─── Severity helper ─────────────────────────────────────────────────────


def test_severity_base_for_any_kev():
    """Every KEV entry baseline = 7 (exploited in the wild)."""
    assert _severity_for_kev(
        ransomware_use="Unknown",
        date_added=date.today() - timedelta(days=365),
    ) == 7


def test_severity_ransomware_bump():
    """knownRansomwareCampaignUse='Known' adds +2."""
    assert _severity_for_kev(
        ransomware_use="Known",
        date_added=date.today() - timedelta(days=365),
    ) == 9


def test_severity_recent_bump():
    """Entries added within the last 30 days bump +1."""
    assert _severity_for_kev(
        ransomware_use="Unknown",
        date_added=date.today() - timedelta(days=10),
    ) == 8


def test_severity_ransomware_plus_recent_capped_at_10():
    """Ransomware (+2) + recent (+1) on base 7 = 10 (capped)."""
    assert _severity_for_kev(
        ransomware_use="Known",
        date_added=date.today() - timedelta(days=2),
    ) == 10


def test_severity_unknown_ransomware_field_treated_as_no_bump():
    """Empty / None / weird ransomware-use values must NOT bump severity."""
    assert _severity_for_kev(
        ransomware_use=None,
        date_added=date.today() - timedelta(days=365),
    ) == 7
    assert _severity_for_kev(
        ransomware_use="",
        date_added=date.today() - timedelta(days=365),
    ) == 7


def test_severity_missing_date_added_no_recent_bump():
    """date_added=None means we can't tell if recent — no bump."""
    assert _severity_for_kev(
        ransomware_use="Unknown",
        date_added=None,
    ) == 7


# ─── Date parsing ────────────────────────────────────────────────────────


def test_parse_kev_date_yyyy_mm_dd():
    """CISA uses ISO 8601 date-only format (YYYY-MM-DD)."""
    assert _parse_kev_date("2025-05-15") == date(2025, 5, 15)


def test_parse_kev_date_invalid_returns_none():
    """Malformed dates safely return None — ingester must not raise."""
    assert _parse_kev_date("not-a-date") is None
    assert _parse_kev_date("") is None
    assert _parse_kev_date(None) is None


# ─── normalize() ─────────────────────────────────────────────────────────


def _sample_entry(**overrides):
    """One representative KEV vulnerability dict as published by CISA."""
    base = {
        "cveID": "CVE-2025-1234",
        "vendorProject": "Microsoft",
        "product": "Windows",
        "vulnerabilityName": "Microsoft Windows Privilege Escalation Vulnerability",
        "dateAdded": "2025-05-15",
        "shortDescription": "An attacker can escalate privileges via crafted IPC.",
        "requiredAction": "Apply mitigations per CISA guidance.",
        "dueDate": "2025-06-05",
        "knownRansomwareCampaignUse": "Unknown",
        "notes": "",
        "cwes": ["CWE-787"],
    }
    base.update(overrides)
    return base


def _sample_payload(*entries):
    """Wrap entries in the top-level KEV JSON shape."""
    return {
        "title": "CISA Catalog of Known Exploited Vulnerabilities",
        "catalogVersion": "2026.05.27",
        "dateReleased": "2026-05-27T18:00:00.000Z",
        "count": len(entries),
        "vulnerabilities": list(entries),
    }


def test_normalize_emits_one_event_per_vulnerability():
    ing = CisaKevIngester(broadcaster=lambda *_: None)
    raw = [_sample_payload(_sample_entry(cveID="CVE-2025-1234"),
                            _sample_entry(cveID="CVE-2025-5678"))]
    events = ing.normalize(raw)
    assert len(events) == 2
    assert {e.external_id for e in events} == {
        "kev:CVE-2025-1234",
        "kev:CVE-2025-5678",
    }


def test_normalize_event_shape():
    """Every emitted event has the canonical KEV shape."""
    ing = CisaKevIngester(broadcaster=lambda *_: None)
    raw = [_sample_payload(_sample_entry())]
    events = ing.normalize(raw)
    assert len(events) == 1
    e = events[0]
    assert e.layer == "cyber_kev"
    assert e.kind == "kev_disclosure"
    assert e.external_id == "kev:CVE-2025-1234"
    # Sentinel geo — KEV entries aren't geographically positioned
    assert e.lat == 0.0
    assert e.lng == 0.0
    assert e.geocode_quality == "not_geo"
    assert e.domain == "cyber"
    # Payload carries the source-of-truth fields downstream consumers need
    assert e.payload["cve_id"] == "CVE-2025-1234"
    assert e.payload["vendor_project"] == "Microsoft"
    assert e.payload["product"] == "Windows"
    assert "Privilege Escalation" in e.payload["vulnerability_name"]
    assert e.payload["date_added"] == "2025-05-15"
    assert e.payload["due_date"] == "2025-06-05"
    assert e.payload["required_action"].startswith("Apply mitigations")
    assert e.payload["known_ransomware_campaign_use"] == "Unknown"
    assert e.payload["cwes"] == ["CWE-787"]
    assert "_attribution" in e.payload


def test_normalize_drops_entries_missing_cve_id():
    """An entry without cveID is unidentifiable — drop it cleanly."""
    ing = CisaKevIngester(broadcaster=lambda *_: None)
    raw = [_sample_payload(
        _sample_entry(cveID="CVE-2025-OK"),
        _sample_entry(cveID=""),       # empty
        _sample_entry(cveID=None),     # explicit null
        {"vendorProject": "X"},         # missing entirely
    )]
    events = ing.normalize(raw)
    assert len(events) == 1
    assert events[0].external_id == "kev:CVE-2025-OK"


def test_normalize_handles_missing_optional_fields():
    """vendorProject / product / dueDate / cwes can all be absent.
    Ingester must emit a usable event with None / empty defaults."""
    ing = CisaKevIngester(broadcaster=lambda *_: None)
    raw = [_sample_payload({
        "cveID": "CVE-2025-MIN",
        "vulnerabilityName": "Test minimal entry",
        "dateAdded": "2025-05-01",
    })]
    events = ing.normalize(raw)
    assert len(events) == 1
    p = events[0].payload
    assert p["vendor_project"] is None or p["vendor_project"] == ""
    assert p["product"] is None or p["product"] == ""
    assert p["cwes"] == []


def test_normalize_severity_known_ransomware_higher():
    """A ransomware-flagged entry must score higher than a non-flagged one
    added on the same day."""
    same_date = (date.today() - timedelta(days=180)).isoformat()
    ing = CisaKevIngester(broadcaster=lambda *_: None)
    raw = [_sample_payload(
        _sample_entry(cveID="CVE-A", knownRansomwareCampaignUse="Unknown",
                       dateAdded=same_date),
        _sample_entry(cveID="CVE-B", knownRansomwareCampaignUse="Known",
                       dateAdded=same_date),
    )]
    events = {e.external_id: e for e in ing.normalize(raw)}
    assert events["kev:CVE-A"].severity < events["kev:CVE-B"].severity


def test_normalize_ts_is_iso_8601_utc():
    """Event.ts must be ISO 8601 with timezone info (per Ingester contract)."""
    ing = CisaKevIngester(broadcaster=lambda *_: None)
    raw = [_sample_payload(_sample_entry(dateAdded="2025-05-15"))]
    events = ing.normalize(raw)
    parsed = datetime.fromisoformat(events[0].ts)
    assert parsed.tzinfo is not None
    # The KEV entry's dateAdded becomes the event timestamp (midnight UTC)
    assert parsed.date() == date(2025, 5, 15)


def test_normalize_empty_payload_returns_empty_list():
    """Empty vulnerabilities array means no events."""
    ing = CisaKevIngester(broadcaster=lambda *_: None)
    raw = [_sample_payload()]
    assert ing.normalize(raw) == []


def test_normalize_skips_non_dict_top_level():
    """Defensive: if fetch() returns garbage (network glitch), don't crash."""
    ing = CisaKevIngester(broadcaster=lambda *_: None)
    assert ing.normalize([{"unexpected": "shape"}]) == []
    assert ing.normalize([]) == []


# ─── Ingester identity / source-yaml gate ────────────────────────────────


def test_ingester_layer_and_source_id():
    """The ingester's layer + source_id must match what infra/sources.yaml
    declares and what writers expect."""
    ing = CisaKevIngester(broadcaster=lambda *_: None)
    assert ing.layer == "cyber_kev"
    assert ing.source_id == "cisa_kev"
    # 24h poll cadence — KEV catalog updates at most once daily
    assert ing.poll_interval_sec == 86400.0


# ─── Writer (real Postgres) ──────────────────────────────────────────────


@pytest.fixture(autouse=False)
async def _pool():
    await init_pool()
    yield
    await close_pool()


@pytest.fixture
async def _clean_kev(_pool):
    async def _cleanup():
        await execute(
            "DELETE FROM event WHERE event_type='kev_disclosure' "
            "AND properties->>'cve_id' LIKE $1",
            f"{TEST_CVE_PREFIX}%",
        )
    await _cleanup()
    yield
    await _cleanup()


def _sample_event(cve_id: str, **overrides) -> GlassboxEvent:
    payload = {
        "cve_id": cve_id,
        "vendor_project": "Microsoft",
        "product": "Windows",
        "vulnerability_name": "Privilege Escalation in Windows IPC",
        "short_description": "An attacker can elevate privileges via crafted IPC.",
        "required_action": "Apply mitigations per CISA guidance.",
        "date_added": "2025-05-15",
        "due_date": "2025-06-05",
        "known_ransomware_campaign_use": "Known",
        "notes": "",
        "cwes": ["CWE-787"],
        "title": "Microsoft Windows Privilege Escalation",
        "link": f"https://nvd.nist.gov/vuln/detail/{cve_id}",
        "_attribution": "Known-exploited vulnerabilities: CISA KEV",
    }
    payload.update(overrides.pop("payload_overrides", {}))
    return GlassboxEvent(
        layer="cyber_kev",
        external_id=f"kev:{cve_id}",
        kind="kev_disclosure",
        lat=0.0,
        lng=0.0,
        ts="2025-05-15T00:00:00+00:00",
        severity=9,
        source="CISA KEV Catalog (CC0)",
        payload=payload,
        domain="cyber",
        decay_half_life_min=43200,
        **overrides,
    )


async def test_writer_persists_kev_row(_clean_kev):
    cve = f"{TEST_CVE_PREFIX}9001"
    ev = _sample_event(cve)
    n = await write_cisa_kev_events([ev])
    assert n == 1

    rows = await fetch(
        "SELECT event_type, event_subtype, severity, title, description, "
        "properties FROM event WHERE properties->>'cve_id' = $1",
        cve,
    )
    assert len(rows) == 1
    row = rows[0]
    assert row["event_type"] == "kev_disclosure"
    assert row["event_subtype"] == "Microsoft"   # vendor → subtype for cluster grouping
    assert int(row["severity"]) == 9
    assert "Privilege Escalation" in row["title"]
    # Properties whitelisted fields are present
    import json as _json
    props = _json.loads(row["properties"]) if isinstance(row["properties"], str) else row["properties"]
    assert props["cve_id"] == cve
    assert props["vendor_project"] == "Microsoft"
    assert props["product"] == "Windows"
    assert props["known_ransomware_campaign_use"] == "Known"
    assert props["cwes"] == ["CWE-787"]


async def test_writer_idempotent_per_cve(_clean_kev):
    """Re-running the same KEV entry must not double-write — UUID5 of the
    cve_id collides with the prior row's id and the (id, event_time)
    unique constraint catches it."""
    cve = f"{TEST_CVE_PREFIX}9002"
    ev = _sample_event(cve)
    assert await write_cisa_kev_events([ev]) == 1
    assert await write_cisa_kev_events([ev]) == 0


async def test_writer_skips_wrong_layer(_pool):
    """A non-cyber_kev event must be skipped without error or write."""
    ev = _sample_event(f"{TEST_CVE_PREFIX}9003")
    ev.layer = "hacker_news"   # wrong layer
    assert await write_cisa_kev_events([ev]) == 0


async def test_writer_zero_events_is_noop():
    """The universal `if not events: return 0` contract — no DB needed."""
    assert await write_cisa_kev_events([]) == 0


async def test_writer_skips_missing_external_id(_pool):
    """Events without external_id can't be deduped — drop cleanly."""
    ev = _sample_event(f"{TEST_CVE_PREFIX}9004")
    ev.external_id = ""
    assert await write_cisa_kev_events([ev]) == 0


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
