"""
CISA KEV ingester — Known Exploited Vulnerabilities Catalog.

Source: https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json
License: CC0 — US Government public domain (Title 17 USC § 105).
Attribution rendered in cockpit footer when the cyber_kev layer is active.

CISA publishes a daily-updated catalog of CVEs known to be exploited in
the wild. Each entry includes:
  - cveID, vendorProject, product, vulnerabilityName
  - dateAdded (YYYY-MM-DD)
  - shortDescription, requiredAction, dueDate
  - knownRansomwareCampaignUse ('Known' | 'Unknown')
  - cwes[]

All entries are by definition exploited-in-the-wild, so the BASE severity
is high (7). Ransomware-flagged entries get +2 (cap 10). Entries added in
the last 30 days get +1 (signal: this is news, not historical context).

KEV is not geographically positioned — vendor HQs span every continent
and choosing one HQ per vendor would be misleading. We emit sentinel
(0, 0) and tag geocode_quality='not_geo'. The cockpit renders KEV via a
side-panel list view, not a globe overlay (per P2A_CYBER_LAYERS_SCOPING).

Polled every 24h — KEV updates at most once per day.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import aiohttp

from .base import GlassboxEvent, Ingester


# ─── Severity helpers ─────────────────────────────────────────────────────


_KEV_BASE_SEVERITY = 7          # every KEV entry is exploited-in-the-wild → high
_KEV_RANSOMWARE_BUMP = 2        # 'Known' ransomware-campaign use → +2
_KEV_RECENT_BUMP = 1            # added within last 30 days → +1
_KEV_RECENT_WINDOW_DAYS = 30


def _severity_for_kev(
    ransomware_use: Optional[str],
    date_added: Optional[date],
) -> int:
    """Score a KEV entry. Base 7; +2 if ransomware-flagged; +1 if recent.
    Cap at 10."""
    score = _KEV_BASE_SEVERITY
    if ransomware_use == "Known":
        score += _KEV_RANSOMWARE_BUMP
    if date_added is not None:
        age = (date.today() - date_added).days
        if 0 <= age <= _KEV_RECENT_WINDOW_DAYS:
            score += _KEV_RECENT_BUMP
    return min(score, 10)


def _parse_kev_date(s: Optional[str]) -> Optional[date]:
    """Parse a CISA `dateAdded` / `dueDate` string (always YYYY-MM-DD).
    Returns None on missing or malformed input — ingester must never raise
    on a single bad entry."""
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


# ─── Ingester ─────────────────────────────────────────────────────────────


class CisaKevIngester(Ingester):
    layer = "cyber_kev"
    source = "CISA KEV Catalog (CC0)"
    source_id = "cisa_kev"               # gates against infra/sources.yaml
    poll_interval_sec = 86400.0          # 24h — KEV updates daily

    URL = (
        "https://www.cisa.gov/sites/default/files/feeds/"
        "known_exploited_vulnerabilities.json"
    )
    UA = "FulcrumGlassbox/2.0 (mewrcreate.com/glassbox)"

    async def fetch(self) -> List[Dict[str, Any]]:
        """Pull the full KEV catalog. Returns a single-element list
        wrapping the JSON payload so normalize() iterates it via the
        standard `raw_items` contract.

        Smoke mode caps to the first 25 vulnerabilities so smoke runs
        don't iterate the full ~1100-entry catalog.
        """
        timeout = aiohttp.ClientTimeout(total=60)
        headers = {"User-Agent": self.UA, "Accept": "application/json"}
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as s:
            async with s.get(self.URL) as r:
                r.raise_for_status()
                payload = await r.json()

        if self.smoke_mode and isinstance(payload, dict):
            vulns = payload.get("vulnerabilities") or []
            payload = dict(payload)
            payload["vulnerabilities"] = vulns[:25]
            payload["count"] = len(payload["vulnerabilities"])
        return [payload]

    def normalize(self, raw_items: List[Dict[str, Any]]) -> List[GlassboxEvent]:
        out: List[GlassboxEvent] = []
        for payload in raw_items:
            if not isinstance(payload, dict):
                continue
            vulns = payload.get("vulnerabilities")
            if not isinstance(vulns, list):
                continue
            for v in vulns:
                if not isinstance(v, dict):
                    continue
                ev = self._normalize_one(v)
                if ev is not None:
                    out.append(ev)
        return out

    def _normalize_one(self, v: Dict[str, Any]) -> Optional[GlassboxEvent]:
        cve_id = (v.get("cveID") or "").strip()
        if not cve_id:
            return None

        date_added_raw = v.get("dateAdded")
        date_added = _parse_kev_date(date_added_raw)
        # Anchor event_time to dateAdded (midnight UTC) when present so
        # downstream time-window queries surface the disclosure correctly.
        # Fall back to now() if missing — preserves the live-data invariant.
        if date_added is not None:
            ts = datetime(
                date_added.year, date_added.month, date_added.day,
                tzinfo=timezone.utc,
            ).isoformat()
        else:
            ts = datetime.now(timezone.utc).isoformat()

        ransomware = v.get("knownRansomwareCampaignUse")
        severity = _severity_for_kev(ransomware, date_added)

        vendor = (v.get("vendorProject") or "") or None
        product = (v.get("product") or "") or None
        vuln_name = (v.get("vulnerabilityName") or "").strip() or None
        cwes_raw = v.get("cwes")
        cwes = list(cwes_raw) if isinstance(cwes_raw, list) else []

        title = vuln_name or f"KEV: {cve_id}"
        description = (v.get("shortDescription") or "").strip() or None

        # Strong market signal — exploited-in-the-wild vulnerabilities in
        # major vendor stacks move security-incident probability markets.
        mtag = "cyber:kev_ransomware" if ransomware == "Known" else "cyber:kev"
        sev_market = 6 if ransomware == "Known" else 3

        return GlassboxEvent(
            layer=self.layer,
            external_id=f"kev:{cve_id}",
            kind="kev_disclosure",
            lat=0.0,
            lng=0.0,
            ts=ts,
            severity=severity,
            source=self.source,
            payload={
                "cve_id": cve_id,
                "vendor_project": vendor,
                "product": product,
                "vulnerability_name": vuln_name,
                "short_description": description,
                "required_action": (v.get("requiredAction") or "") or None,
                "date_added": date_added_raw,
                "due_date": v.get("dueDate"),
                "known_ransomware_campaign_use": ransomware,
                "notes": (v.get("notes") or "") or None,
                "cwes": cwes,
                "title": title,
                "link": (
                    f"https://nvd.nist.gov/vuln/detail/{cve_id}"
                    if cve_id.startswith("CVE-")
                    else None
                ),
                "_attribution": "Known-exploited vulnerabilities: CISA KEV",
            },
            domain="cyber",
            geocode_quality="not_geo",
            decay_half_life_min=43200,        # 30 days — KEVs stay relevant a long time
            market_tags=[mtag],
            severity_for_market=sev_market,
        )
