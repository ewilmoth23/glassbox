"""
FRED ingester — US economic data from the St. Louis Fed.

Source: https://fred.stlouisfed.org/docs/api/fred/
API:    https://api.stlouisfed.org/fred/series/observations

Why this matters for predictions:
  - Polymarket / Kalshi run weekly Fed-action markets (rate cut/hold/raise)
  - Unemployment claims, CPI, PCE all move "next FOMC decision" markets
  - GDP / PMI / yield curve = recession-prediction markets
  - Initial jobless claims (weekly) = leading indicator that retail trades emotionally

Auth: free API key, register at https://fred.stlouisfed.org/docs/api/api_key.html.
Set env var FRED_API_KEY. Without it the ingester stays dormant.

Series we pull (daily / weekly / monthly):
  - UNRATE       Unemployment rate (monthly)
  - CPIAUCSL     CPI All Urban (monthly) — inflation marker
  - DFF          Fed Funds rate (daily) — current policy stance
  - DGS10        10-year treasury (daily) — recession watch
  - DGS2         2-year treasury (daily) — yield curve component
  - ICSA         Initial jobless claims (weekly) — Fed-decision input
  - PAYEMS       Nonfarm payrolls (monthly) — Fed-decision input
  - GDP          GDP (quarterly)

Each polled series produces ONE GlassboxEvent per new observation: when the
latest value differs from what we last saw, a "macro_release" event fires
into the Loop bus. Markets fan out via market_tags.

Author: 2026-04-27 — task #169
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from .base import GlassboxEvent, Ingester


_FRED_BASE = "https://api.stlouisfed.org/fred/series/observations"

SERIES_REGISTRY: Dict[str, Dict[str, Any]] = {
    "UNRATE":   {"label": "Unemployment rate (US)",          "freq": "monthly",   "market_severity": 7,
                 "tags": ["fred:UNRATE", "fed:decision_input", "macro:labor"]},
    "CPIAUCSL": {"label": "CPI All Urban Consumers",         "freq": "monthly",   "market_severity": 8,
                 "tags": ["fred:CPI", "fed:decision_input", "macro:inflation"]},
    "DFF":      {"label": "Effective Fed Funds rate",        "freq": "daily",     "market_severity": 6,
                 "tags": ["fred:FED_FUNDS", "fed:current_rate"]},
    "DGS10":    {"label": "10-year Treasury constant maturity", "freq": "daily",  "market_severity": 5,
                 "tags": ["fred:10Y", "macro:yield_curve"]},
    "DGS2":     {"label": "2-year Treasury constant maturity",  "freq": "daily",  "market_severity": 5,
                 "tags": ["fred:2Y", "macro:yield_curve"]},
    "ICSA":     {"label": "Initial jobless claims (weekly)",   "freq": "weekly",  "market_severity": 7,
                 "tags": ["fred:ICSA", "macro:labor", "fed:decision_input"]},
    "PAYEMS":   {"label": "Nonfarm payrolls (monthly)",        "freq": "monthly", "market_severity": 8,
                 "tags": ["fred:NFP", "macro:labor", "fed:decision_input"]},
    "GDP":      {"label": "GDP (quarterly)",                   "freq": "quarterly", "market_severity": 7,
                 "tags": ["fred:GDP", "macro:growth"]},
}


class FREDMacroIngester(Ingester):
    """Polls FRED for the configured macro series and emits release events."""

    layer = "macro"
    source = "FRED — Federal Reserve Bank of St. Louis"
    poll_interval_sec = 60 * 60     # hourly is plenty — most series update daily/weekly

    def __init__(self, *, api_key: Optional[str] = None, **kwargs):
        super().__init__(**kwargs)
        self.api_key = (api_key or os.environ.get("FRED_API_KEY", "")).strip()
        # Map series_id -> last (date, value) we've seen, so we only emit on change
        self._last_seen: Dict[str, Tuple[str, str]] = {}

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    async def fetch(self) -> List[Dict[str, Any]]:
        if not self.enabled:
            self.log.info("[fred] FRED_API_KEY not set; skipping (set to activate)")
            return []
        try:
            import aiohttp
        except ImportError:
            return []

        out: List[Dict[str, Any]] = []
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
            for sid, meta in SERIES_REGISTRY.items():
                try:
                    obs = await self._fetch_series(session, sid)
                    if not obs:
                        continue
                    latest = obs[-1]
                    prior = self._last_seen.get(sid)
                    self._last_seen[sid] = (latest["date"], latest["value"])
                    # Emit on first-seen OR when latest observation date is new
                    if prior is None or prior[0] != latest["date"]:
                        out.append({
                            "series_id": sid,
                            "label": meta["label"],
                            "freq": meta["freq"],
                            "market_severity": meta["market_severity"],
                            "tags": meta["tags"],
                            "date": latest["date"],
                            "value": latest["value"],
                            "prior_value": prior[1] if prior else None,
                        })
                except Exception as e:
                    self.log.info(f"[fred/{sid}] {type(e).__name__}: {e}")
        self.log.info(f"[fred] {len(out)} new releases this poll")
        return out

    async def _fetch_series(self, session, series_id: str) -> List[Dict[str, str]]:
        params = {
            "series_id": series_id,
            "api_key": self.api_key,
            "file_type": "json",
            "sort_order": "asc",
            "limit": "100",      # plenty for getting "latest"
        }
        async with session.get(_FRED_BASE, params=params) as resp:
            if resp.status != 200:
                self.log.info(f"[fred/{series_id}] HTTP {resp.status}")
                return []
            data = await resp.json(content_type=None)
        return data.get("observations", []) or []

    def normalize(self, raw_items: List[Dict[str, Any]]) -> List[GlassboxEvent]:
        out: List[GlassboxEvent] = []
        for r in raw_items:
            sid = r["series_id"]
            value = r["value"]
            prior = r.get("prior_value")
            try:
                v_num = float(value) if value not in (".", "", None) else None
                p_num = float(prior) if prior not in (".", "", None) else None
            except ValueError:
                v_num, p_num = None, None
            change_str = ""
            if v_num is not None and p_num is not None:
                delta = v_num - p_num
                pct = (delta / p_num * 100) if p_num else 0.0
                change_str = f"{delta:+.2f} ({pct:+.2f}%)"

            ext_id = f"{sid}:{r['date']}"
            ts = datetime.now(timezone.utc).isoformat()

            out.append(GlassboxEvent(
                layer=self.layer,
                external_id=ext_id,
                kind="macro_release",
                lat=38.6270, lng=-90.1994,    # St Louis Fed HQ as nominal anchor
                ts=ts,
                severity=r["market_severity"],
                source=self.source,
                payload={
                    "series_id": sid,
                    "series_label": r["label"],
                    "frequency": r["freq"],
                    "observation_date": r["date"],
                    "value": value,
                    "prior_value": prior,
                    "change": change_str,
                },
                domain="macro",
                geocode_quality="country",
                severity_for_market=r["market_severity"],
                decay_half_life_min=60 * 24 * 3,    # 3 days — releases echo for days
                market_tags=r["tags"],
            ))
        return out
