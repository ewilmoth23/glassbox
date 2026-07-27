"""
SEC EDGAR ingester — recent securities filings (8-K, 10-K, 10-Q, S-1).

Source: https://www.sec.gov/cgi-bin/browse-edgar (RSS) +
        https://efts.sec.gov/LATEST/search-index (full-text search)
License: US public domain (US Government — securities filings are public record)
Attribution: not legally required but rendered as good citizenship.
NO KEY required.

CRITICAL — SEC EDGAR User-Agent rules:
  Per https://www.sec.gov/os/accessing-edgar-data:
  EDGAR REQUIRES a User-Agent that identifies your company + email.
  Failing to set this returns 403 from sec.gov. We use a clear UA below.
  If we ever start getting 403s, this is the first thing to check.

Filing types we ingest (most market-relevant):
  - 8-K   — material events (earnings, M&A, mgmt changes, restatements)
  - 10-K  — annual report
  - 10-Q  — quarterly report
  - S-1   — IPO registration
  - 13F   — institutional holdings (45 days lagged)

For v1.0 we focus on 8-K + S-1 — these are the highest-signal events
for prediction markets.

Rate limits: SEC asks for max 10 req/sec. We poll the recent-filings
RSS every 5 min, well within budget.

Geocoding:
  Filings don't have lat/lng directly. We use the company's HQ from
  EDGAR's address index when present; otherwise sentinel (0,0) and
  kind='watch' so frontend renders as panel item.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import aiohttp

from .base import GlassboxEvent, Ingester


_FORM_SEVERITY = {
    "8-K":  6,    # material events — high signal
    "S-1":  7,    # IPO — major event
    "10-K": 4,    # scheduled annual report
    "10-Q": 3,    # scheduled quarterly
    "13F":  2,    # holdings disclosure (lagged)
    "424B": 5,    # prospectus supplement (commonly precedes IPO/secondary)
}


_FORM_MARKET_TAG = {
    "8-K":  "securities:material_event",
    "S-1":  "securities:ipo",
    "10-K": "securities:annual",
    "10-Q": "securities:quarterly",
}


# ─── Ingester ─────────────────────────────────────────────────────────────


class SecEdgarIngester(Ingester):
    layer = "securities_filings"
    source = "SEC EDGAR (US public domain)"
    source_id = "sec_edgar"               # gates against infra/sources.yaml
    poll_interval_sec = 300.0             # 5 min — RSS updates intra-hour

    URL = "https://www.sec.gov/cgi-bin/browse-edgar"
    # SEC requires a UA with company name + contact email
    UA = "FulcrumGlassbox/2.0 (MEWR Creative Enterprises LLC; hello@mewrcreate.com)"

    # Form types we ingest, in priority order
    FORM_TYPES = ("8-K", "S-1", "424B")

    async def fetch(self) -> List[Dict[str, Any]]:
        timeout = aiohttp.ClientTimeout(total=30)
        headers = {"User-Agent": self.UA, "Accept": "application/atom+xml"}

        all_filings: List[Dict[str, Any]] = []
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as s:
            for form in self.FORM_TYPES:
                params = {
                    "action":    "getcurrent",
                    "type":      form,
                    "company":   "",
                    "dateb":     "",
                    "owner":     "include",
                    "count":     "40",
                    "output":    "atom",
                }
                try:
                    async with s.get(self.URL, params=params) as r:
                        r.raise_for_status()
                        text = await r.text()
                except Exception as e:
                    self.log.info(f"[sec_edgar] {form} fetch failed: {e}")
                    continue

                # Parse Atom feed
                try:
                    root = ET.fromstring(text)
                except ET.ParseError as e:
                    self.log.info(f"[sec_edgar] {form} XML parse failed: {e}")
                    continue

                # Atom namespace
                ns = {"atom": "http://www.w3.org/2005/Atom"}
                for entry in root.findall("atom:entry", ns):
                    title_el = entry.find("atom:title", ns)
                    upd_el   = entry.find("atom:updated", ns)
                    link_el  = entry.find("atom:link", ns)
                    id_el    = entry.find("atom:id", ns)
                    all_filings.append({
                        "form":    form,
                        "title":   (title_el.text or "").strip() if title_el is not None else "",
                        "updated": (upd_el.text or "").strip() if upd_el is not None else "",
                        "link":    link_el.attrib.get("href", "") if link_el is not None else "",
                        "id":      (id_el.text or "").strip() if id_el is not None else "",
                    })
        return all_filings

    def normalize(self, raw_items: List[Dict[str, Any]]) -> List[GlassboxEvent]:
        now = datetime.now(timezone.utc).isoformat()
        out: List[GlassboxEvent] = []

        for f in raw_items:
            ext_id = f.get("id") or ""
            if not ext_id:
                continue

            form = f.get("form") or ""
            severity = _FORM_SEVERITY.get(form, 3)

            mtag = _FORM_MARKET_TAG.get(form)
            mtags = [mtag] if mtag else []
            sev_market = 4 if form in ("8-K", "S-1") else 0

            # No lat/lng for filings — render as panel item
            out.append(GlassboxEvent(
                layer=self.layer,
                external_id=ext_id,
                kind="watch",
                lat=0.0,
                lng=0.0,
                ts=f.get("updated") or now,
                severity=severity,
                source=self.source,
                payload={
                    "form":    form,
                    "title":   f.get("title"),
                    "link":    f.get("link"),
                    "_attribution": "Securities filings: SEC EDGAR (US public domain)",
                },
                domain="entity",
                geocode_quality="not_geo",
                decay_half_life_min=720,    # 12h — filings are fresh for the trading day
                market_tags=mtags,
                severity_for_market=sev_market,
            ))

        return out
