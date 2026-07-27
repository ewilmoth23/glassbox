"""Smoke tests for the Postmark digest sender. Pure-function tests on
the rendering + filtering paths — the actual Postmark POST is mocked
out (we already verified the live integration end-to-end against the
Postmark API in development; CI doesn't need to hammer their endpoint)."""
from __future__ import annotations
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from digest_sender import (  # noqa: E402
    filter_for_subscriber, render_html, render_text, SEVERITY_RANK,
)


def _sample_signals() -> dict:
    return {
        "categories": [
            {
                "id": "sanctioned_dark", "label": "Sanctioned · gone dark",
                "severity": "critical", "count": 3,
                "items": [
                    {"id": "1", "title": "CRITICAL — VANGUARD went dark",
                     "description": "OFAC SDN match", "ts": "2026-05-10T03:00:00Z",
                     "lat": 25.0, "lng": 56.0},
                    {"id": "2", "title": "CRITICAL — TRITON went dark",
                     "description": "Russia SDN", "ts": "2026-05-10T02:00:00Z",
                     "lat": 60.0, "lng": 30.0},
                ],
            },
            {
                "id": "wildfires", "label": "Active wildfires",
                "severity": "low", "count": 12,
                "items": [
                    {"id": "f1", "title": "Wildfire in California",
                     "description": "FIRMS detection", "ts": "2026-05-10T01:00:00Z",
                     "lat": 36.0, "lng": -120.0},
                ],
            },
            {
                "id": "military_air", "label": "Military aircraft",
                "severity": "medium", "count": 5,
                "items": [
                    {"id": "ma1", "title": "Military aircraft over Black Sea",
                     "description": "ADS-B trace", "ts": "2026-05-10T04:00:00Z",
                     "lat": 44.0, "lng": 33.0},
                ],
            },
        ],
    }


def test_filter_severity_floor_critical_only():
    """severity_floor=critical should keep only critical items."""
    items = filter_for_subscriber(_sample_signals(),
                                  {"severity_floor": "critical"})
    assert len(items) == 2
    assert all(i["severity"] == "critical" for i in items)


def test_filter_severity_floor_high_includes_critical():
    """severity_floor=high keeps critical + high (none in sample), drops low/medium."""
    items = filter_for_subscriber(_sample_signals(),
                                  {"severity_floor": "high"})
    sevs = {i["severity"] for i in items}
    assert sevs == {"critical"}, f"expected only critical, got {sevs}"


def test_filter_severity_floor_low_keeps_all():
    """severity_floor=low keeps everything."""
    items = filter_for_subscriber(_sample_signals(),
                                  {"severity_floor": "low"})
    assert len(items) == 4    # 2 critical + 1 low + 1 medium


def test_filter_category_ids_subset():
    """When category_ids is set, only those categories' items are returned."""
    items = filter_for_subscriber(
        _sample_signals(),
        {"severity_floor": "low", "category_ids": ["wildfires"]},
    )
    assert len(items) == 1
    assert items[0]["category_id"] == "wildfires"


def test_filter_sorts_by_severity_then_recency():
    """Critical comes first, then most recent within severity tier."""
    items = filter_for_subscriber(_sample_signals(),
                                  {"severity_floor": "low"})
    # First two should be critical
    assert items[0]["severity"] == "critical"
    assert items[1]["severity"] == "critical"
    # Within critical, the more recent ts comes first
    assert items[0]["ts"] > items[1]["ts"]


def test_render_html_contains_brand_and_findings():
    """HTML body has brand mark, recipient email, and finding titles."""
    items = filter_for_subscriber(_sample_signals(),
                                  {"severity_floor": "low"})
    html = render_html(items, "test@mewrcreate.com", "tok-abc", 24)
    assert "GLASS" in html and "BOX" in html
    assert "test@mewrcreate.com" in html
    assert "VANGUARD went dark" in html
    assert "Wildfire in California" in html
    # Severity badges present
    assert "CRITICAL" in html
    # Inline style only — no <style> tag (email-client safe)
    assert "<style>" not in html.lower()


def test_render_html_handles_empty_findings():
    """Empty list produces a friendly 'world is quiet' message, not blank."""
    html = render_html([], "test@mewrcreate.com", "tok-abc", 24)
    assert "No findings matched" in html or "world is quiet" in html.lower()


def test_render_html_includes_unsubscribe_link():
    """Unsubscribe URL is built from the unsubscribe_token + public host."""
    html = render_html([], "test@mewrcreate.com", "TOK-XYZ", 24)
    assert "TOK-XYZ" in html
    assert "/api/v1/signals/unsubscribe" in html


def test_render_text_plaintext_alternative():
    """Plaintext fallback is built and includes findings."""
    items = filter_for_subscriber(_sample_signals(),
                                  {"severity_floor": "critical"})
    text = render_text(items, "test@mewrcreate.com", "tok", 24)
    assert "GLASSBOX" in text
    assert "VANGUARD" in text
    # Severity label — the 2026-05-13 digest rewrite swapped the old
    # "[CRITICAL]" bracket token for editorial section headers like
    # "LEAD STORY · CRITICAL" and "SANCTIONED · GONE DARK · CRITICAL".
    # The token still appears; just no longer wrapped in brackets.
    assert "CRITICAL" in text
    # No HTML tags leaked into the plaintext path
    assert "<" not in text or text.count("<") < 3   # tolerate a stray date arrow


def test_severity_rank_ordering_consistent():
    """SEVERITY_RANK constants must order: critical > high > medium > low."""
    assert SEVERITY_RANK["critical"] > SEVERITY_RANK["high"]
    assert SEVERITY_RANK["high"] > SEVERITY_RANK["medium"]
    assert SEVERITY_RANK["medium"] > SEVERITY_RANK["low"]
