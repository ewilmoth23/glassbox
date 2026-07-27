"""
Spamhaus DROP/EDROP ingester tests — P2-A Phase 1 MVP.

Sources:
  - https://www.spamhaus.org/drop/drop.txt   (DROP — hijacked/criminal /24+)
  - https://www.spamhaus.org/drop/edrop.txt  (EDROP — extended list)

License: free, ToS-redistributable (Spamhaus DROP/EDROP redistribution
allowed per their published terms).

Tests cover:
  - Plain-text parser (line shape, comment filtering, CIDR validation)
  - Severity scheme (DROP=8, EDROP=7)
  - normalize() emits cyber_spamhaus_drop events with proper payload
  - SBL ID extraction is the external_id key (idempotent across polls)

Run:
    21_GLASSBOX_AI/.venv/bin/python -m pytest \\
        21_GLASSBOX_AI/tests/test_spamhaus_drop_ingester.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ingesters.spamhaus_drop import (  # noqa: E402
    SpamhausDropIngester,
    _severity_for_list,
    _parse_drop_line,
)
from ingesters.base import GlassboxEvent  # noqa: E402
from db import init_pool, close_pool, fetch, execute  # noqa: E402
from writers import write_spamhaus_drop_events  # noqa: E402


TEST_SBL_PREFIX = "SBLTEST"


# ─── Severity helper ─────────────────────────────────────────────────────


def test_severity_drop_is_8():
    """DROP is hijacked/criminal — severity 8."""
    assert _severity_for_list("DROP") == 8


def test_severity_edrop_is_7():
    """EDROP is extended/lower-confidence — severity 7."""
    assert _severity_for_list("EDROP") == 7


def test_severity_unknown_list_defaults_low():
    """Defensive: unknown list name yields a low but non-zero score."""
    assert _severity_for_list("UNKNOWN") <= 5


# ─── Plain-text line parser ──────────────────────────────────────────────


def test_parse_drop_line_basic():
    """Canonical line shape: `<cidr> ; <sbl_ref>`."""
    cidr, sbl = _parse_drop_line("1.10.16.0/20 ; SBL257397")
    assert cidr == "1.10.16.0/20"
    assert sbl == "SBL257397"


def test_parse_drop_line_ipv6():
    """IPv6 blocks are formatted identically; parser must accept them."""
    cidr, sbl = _parse_drop_line("2001:db8::/32 ; SBL999999")
    assert cidr == "2001:db8::/32"
    assert sbl == "SBL999999"


def test_parse_drop_line_extra_whitespace():
    """Extra spacing must not break the parser."""
    cidr, sbl = _parse_drop_line("  1.2.3.0/24   ;   SBL12345  ")
    assert cidr == "1.2.3.0/24"
    assert sbl == "SBL12345"


def test_parse_drop_line_comment_returns_none():
    """Comment lines (leading semicolon) yield (None, None)."""
    assert _parse_drop_line("; Spamhaus DROP List header") == (None, None)
    assert _parse_drop_line("; Last-Modified: 2026-05-27T18:00:00Z") == (None, None)


def test_parse_drop_line_empty_returns_none():
    """Empty / whitespace-only lines yield (None, None)."""
    assert _parse_drop_line("") == (None, None)
    assert _parse_drop_line("   ") == (None, None)


def test_parse_drop_line_missing_sbl_returns_none():
    """A line without an SBL reference can't be deduped — drop it."""
    assert _parse_drop_line("1.2.3.0/24") == (None, None)
    assert _parse_drop_line("1.2.3.0/24 ; ") == (None, None)


def test_parse_drop_line_malformed_cidr_returns_none():
    """A line with garbage in the CIDR slot must be rejected."""
    assert _parse_drop_line("not-a-cidr ; SBL1234") == (None, None)


# ─── normalize() ─────────────────────────────────────────────────────────


def _sample_raw(list_name: str, entries):
    """Wrap entries in the per-list dict the fetch() returns."""
    return {"list_name": list_name, "entries": list(entries)}


def test_normalize_emits_event_per_block():
    ing = SpamhausDropIngester(broadcaster=lambda *_: None)
    raw = [_sample_raw("DROP", [
        ("1.10.16.0/20", "SBL257397"),
        ("1.34.96.0/19", "SBL354646"),
    ])]
    events = ing.normalize(raw)
    assert len(events) == 2
    ext_ids = {e.external_id for e in events}
    assert ext_ids == {"spamhaus:SBL257397", "spamhaus:SBL354646"}


def test_normalize_event_shape():
    ing = SpamhausDropIngester(broadcaster=lambda *_: None)
    raw = [_sample_raw("DROP", [("1.10.16.0/20", "SBL257397")])]
    events = ing.normalize(raw)
    assert len(events) == 1
    e = events[0]
    assert e.layer == "cyber_spamhaus_drop"
    assert e.kind == "spamhaus_block_entry"
    assert e.external_id == "spamhaus:SBL257397"
    assert e.lat == 0.0 and e.lng == 0.0
    assert e.geocode_quality == "not_geo"
    assert e.domain == "cyber"
    assert e.severity == 8                        # DROP severity
    assert e.payload["cidr"] == "1.10.16.0/20"
    assert e.payload["sbl_id"] == "SBL257397"
    assert e.payload["list_name"] == "DROP"
    assert "_attribution" in e.payload


def test_normalize_edrop_is_severity_7():
    ing = SpamhausDropIngester(broadcaster=lambda *_: None)
    raw = [_sample_raw("EDROP", [("5.6.7.0/24", "SBL999")])]
    events = ing.normalize(raw)
    assert len(events) == 1
    assert events[0].severity == 7


def test_normalize_concatenates_multiple_lists():
    """fetch() returns one entry per list (DROP + EDROP); normalize() flattens."""
    ing = SpamhausDropIngester(broadcaster=lambda *_: None)
    raw = [
        _sample_raw("DROP", [("1.0.0.0/24", "SBL1")]),
        _sample_raw("EDROP", [("2.0.0.0/24", "SBL2")]),
    ]
    events = ing.normalize(raw)
    assert len(events) == 2
    by_list = {e.payload["list_name"]: e for e in events}
    assert by_list["DROP"].severity == 8
    assert by_list["EDROP"].severity == 7


def test_normalize_empty_returns_empty():
    ing = SpamhausDropIngester(broadcaster=lambda *_: None)
    assert ing.normalize([]) == []
    assert ing.normalize([_sample_raw("DROP", [])]) == []


def test_normalize_skips_malformed_entries():
    """Defensive: non-tuple / non-(str,str) entries are silently skipped."""
    ing = SpamhausDropIngester(broadcaster=lambda *_: None)
    raw = [_sample_raw("DROP", [
        ("1.0.0.0/24", "SBL1"),
        (None, "SBL2"),
        ("3.0.0.0/24", None),
        "garbage",
    ])]
    events = ing.normalize(raw)
    assert len(events) == 1
    assert events[0].external_id == "spamhaus:SBL1"


# ─── Ingester identity / source-yaml gate ────────────────────────────────


def test_ingester_layer_and_source_id():
    ing = SpamhausDropIngester(broadcaster=lambda *_: None)
    assert ing.layer == "cyber_spamhaus_drop"
    assert ing.source_id == "spamhaus_drop"
    assert ing.poll_interval_sec == 3600.0       # 1h — Spamhaus operational guidance


# ─── Writer (real Postgres) ──────────────────────────────────────────────


@pytest.fixture(autouse=False)
async def _pool():
    await init_pool()
    yield
    await close_pool()


@pytest.fixture
async def _clean_spam(_pool):
    async def _cleanup():
        await execute(
            "DELETE FROM event WHERE event_type='spamhaus_block_entry' "
            "AND properties->>'sbl_id' LIKE $1",
            f"{TEST_SBL_PREFIX}%",
        )
    await _cleanup()
    yield
    await _cleanup()


def _sample_event(sbl_id: str, **overrides) -> GlassboxEvent:
    return GlassboxEvent(
        layer="cyber_spamhaus_drop",
        external_id=f"spamhaus:{sbl_id}",
        kind="spamhaus_block_entry",
        lat=0.0,
        lng=0.0,
        ts="2026-05-27T00:00:00+00:00",
        severity=8,
        source="Spamhaus DROP/EDROP",
        payload={
            "cidr": "192.0.2.0/24",
            "sbl_id": sbl_id,
            "list_name": "DROP",
            "title": f"DROP block 192.0.2.0/24 ({sbl_id})",
            "link": f"https://www.spamhaus.org/sbl/query/{sbl_id}",
            "_attribution": "Block lists: Spamhaus",
        },
        domain="cyber",
        decay_half_life_min=43200,
        **overrides,
    )


async def test_writer_persists_block_row(_clean_spam):
    sbl = f"{TEST_SBL_PREFIX}9001"
    n = await write_spamhaus_drop_events([_sample_event(sbl)])
    assert n == 1
    rows = await fetch(
        "SELECT event_type, event_subtype, severity, title, properties "
        "FROM event WHERE properties->>'sbl_id' = $1",
        sbl,
    )
    assert len(rows) == 1
    row = rows[0]
    assert row["event_type"] == "spamhaus_block_entry"
    assert row["event_subtype"] == "DROP"
    assert int(row["severity"]) == 8
    import json as _json
    props = _json.loads(row["properties"]) if isinstance(row["properties"], str) else row["properties"]
    assert props["sbl_id"] == sbl
    assert props["cidr"] == "192.0.2.0/24"
    assert props["list_name"] == "DROP"


async def test_writer_idempotent_per_sbl(_clean_spam):
    sbl = f"{TEST_SBL_PREFIX}9002"
    ev = _sample_event(sbl)
    assert await write_spamhaus_drop_events([ev]) == 1
    assert await write_spamhaus_drop_events([ev]) == 0


async def test_writer_skips_wrong_layer(_pool):
    ev = _sample_event(f"{TEST_SBL_PREFIX}9003")
    ev.layer = "hacker_news"
    assert await write_spamhaus_drop_events([ev]) == 0


async def test_writer_zero_events_is_noop():
    assert await write_spamhaus_drop_events([]) == 0


async def test_writer_skips_missing_external_id(_pool):
    ev = _sample_event(f"{TEST_SBL_PREFIX}9004")
    ev.external_id = ""
    assert await write_spamhaus_drop_events([ev]) == 0


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
