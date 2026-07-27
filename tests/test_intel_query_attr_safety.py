"""
Regression tests locking the c3f4f02 fix and the broader contract that
glassbox_server.py must NOT access GlassboxEvent attributes that aren't
declared on the dataclass.

Context: c3f4f02 fixed an AttributeError at glassbox_server.py:3165
  -    "title": (ev.title or "")[:80],
  +    "title": (getattr(ev, "title", "") or "")[:80],
The buggy line had been on the floor since 2026-05-07 (commit 160cac99)
and only fired the first time someone hit /api/intel/query with cached
events of sev≥3 whose layer didn't surface a `.title` field.

These tests enforce two things:

1. The GlassboxEvent dataclass has NO `title` attribute. Any future code
   that does `event.title` will fail this test on day-one, before it
   ships to production.
2. The exact defensive-access pattern from line 3165 produces a sane
   empty string instead of crashing.

Plus: a quick re-cap audit assertion that none of the four other
`for ev in ...` loops in glassbox_server.py read an attribute that
isn't on the dataclass — done via static text scan, not import,
because importing glassbox_server.py spins up the full server
side-effects (ingester construction, etc.).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ingesters.base import GlassboxEvent  # noqa: E402


# Set of attributes declared on the GlassboxEvent dataclass (or as
# @property). Anything outside this set is NOT guaranteed on an instance,
# so reading it directly is unsafe.
SAFE_GLASSBOXEVENT_ATTRS = {
    # Required positional fields
    "layer", "external_id", "kind", "lat", "lng", "ts",
    # Defaulted fields (always present, may be falsy)
    "severity", "altitude_m", "heading_deg", "velocity_ms",
    "source", "payload",
    "market_tags", "severity_for_market", "geocode_quality",
    "domain", "decay_half_life_min",
    # Methods + properties
    "id", "to_dict",
}


# ─── Contract: GlassboxEvent has no `title` attribute ────────────────────

def test_glassbox_event_dataclass_has_no_title():
    """Day-one canary: if someone adds a `title` field to GlassboxEvent
    later, this test fails and prompts a re-audit of every getattr(ev, "title")
    callsite — those would all start returning the *new* attribute instead
    of the empty-string default. If `title` becomes a real field, drop the
    defensive getattr and use direct access; this test then becomes obsolete."""
    e = GlassboxEvent(
        layer="aircraft",
        external_id="ABC123",
        kind="position",
        lat=37.7,
        lng=-122.4,
        ts="2026-05-21T00:00:00Z",
    )
    assert not hasattr(e, "title")


def test_glassbox_event_has_required_fields():
    """Sanity: the fields glassbox_server.py loops over directly (lat, lng,
    severity, layer, external_id, ts, source, payload) MUST exist. If any
    of these go missing, the broadcast pipeline + intel_query both break."""
    e = GlassboxEvent(
        layer="aircraft",
        external_id="ABC123",
        kind="position",
        lat=37.7,
        lng=-122.4,
        ts="2026-05-21T00:00:00Z",
    )
    for attr in ("lat", "lng", "severity", "layer", "external_id", "ts", "source", "payload"):
        assert hasattr(e, attr), f"GlassboxEvent missing required field: {attr}"


# ─── Regression: the c3f4f02 defensive pattern works ─────────────────────

def test_intel_query_top_severity_pattern_handles_missing_title():
    """Lock the exact pattern from glassbox_server.py:3160-3169 against
    a GlassboxEvent that lacks .title (which is every GlassboxEvent as
    of 2026-05-21). The endpoint must build a top_severity entry
    without crashing."""
    e = GlassboxEvent(
        layer="aircraft",
        external_id="ABC123",
        kind="position",
        lat=37.7,
        lng=-122.4,
        ts="2026-05-21T00:00:00Z",
        severity=5,
    )
    # Verbatim from the fixed code at glassbox_server.py:3165-3169
    sev = getattr(e, "severity", 0) or 0
    entry = {
        "layer": "aircraft",
        "title": (getattr(e, "title", "") or "")[:80],
        "severity": sev,
        "lat": round(getattr(e, "lat", 0) or 0, 2),
        "lng": round(getattr(e, "lng", 0) or 0, 2),
    }
    assert entry["title"] == ""        # safely empty, no crash
    assert entry["severity"] == 5
    assert entry["lat"] == 37.7
    assert entry["lng"] == -122.4


def test_intel_query_pattern_uses_explicit_empty_when_title_is_none():
    """An ingester *could* set `.title = None` directly on the dataclass
    instance (Python doesn't prevent dynamic attribute assignment). The
    `or ""` keeps the slice safe in that case."""
    e = GlassboxEvent(
        layer="news",
        external_id="news-1",
        kind="alert",
        lat=0.0,
        lng=0.0,
        ts="2026-05-21T00:00:00Z",
        severity=5,
    )
    e.title = None  # type: ignore[attr-defined]
    title = (getattr(e, "title", "") or "")[:80]
    assert title == ""


def test_intel_query_pattern_truncates_long_title():
    """If a future GlassboxEvent subclass or dynamic assignment DOES set
    title, the [:80] truncation must still apply. Locks the existing
    output contract."""
    e = GlassboxEvent(
        layer="news",
        external_id="news-2",
        kind="alert",
        lat=0.0,
        lng=0.0,
        ts="2026-05-21T00:00:00Z",
        severity=5,
    )
    e.title = "x" * 200  # type: ignore[attr-defined]
    title = (getattr(e, "title", "") or "")[:80]
    assert len(title) == 80
    assert title == "x" * 80


# ─── Audit: no other unsafe ev.<attr> access in glassbox_server.py ───────

GLASSBOX_SERVER_PATH = ROOT / "glassbox_server.py"


def test_no_unguarded_ev_attribute_access_in_glassbox_server():
    """Scan glassbox_server.py for `ev.<word>` patterns where <word> is
    NOT a known GlassboxEvent attribute. Catches the next AttributeError
    before it ships.

    Scoped to `ev.` (not `event.`) because `event.` only appears inside
    docstrings + comments in this file (a pre-audit grep confirmed); scoping
    narrowly avoids needing a docstring/comment parser.

    The pattern `ev.payload.<key>` is already covered because `payload` is
    in SAFE_GLASSBOXEVENT_ATTRS, so the regex stops at `ev.payload` and the
    chained `.get(...)` is fine.

    False-positive surface: if `ev` ever rebinds to a non-GlassboxEvent
    object in this file, the test would still flag legitimate access. In
    practice every loop variable named `ev` here iterates over a
    GlassboxEvent list — verified by reading every `for ev in ...` site."""
    src = GLASSBOX_SERVER_PATH.read_text()

    unsafe = []
    for m in re.finditer(r'\bev\.([a-z_][a-z_0-9]*)', src):
        attr = m.group(1)
        if attr in SAFE_GLASSBOXEVENT_ATTRS:
            continue
        line_no = src[:m.start()].count('\n') + 1
        lines = src.splitlines()
        line = lines[line_no - 1] if line_no <= len(lines) else ""

        # Skip if `.` directly precedes — this is a chained `.X.Y` access,
        # not a fresh `ev.Y`
        char_before = src[m.start() - 1] if m.start() > 0 else ''
        if char_before == '.':
            continue

        # Skip pure-comment lines
        if line.lstrip().startswith('#'):
            continue

        unsafe.append((line_no, attr, line.strip()[:100]))

    assert not unsafe, (
        "Unsafe ev.<attr> access detected (not in SAFE_GLASSBOXEVENT_ATTRS "
        "and not guarded by getattr). Add getattr(ev, name, default) or "
        "extend SAFE_GLASSBOXEVENT_ATTRS if the field has been added to "
        "the dataclass:\n" + "\n".join(
            f"  glassbox_server.py:{ln} [ev.{a}]: {snip}"
            for ln, a, snip in unsafe)
    )
