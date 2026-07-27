"""
Spamhaus DROP/EDROP ingester — hijacked + criminal IP block lists.

Sources:
  - DROP:  https://www.spamhaus.org/drop/drop.txt   (hijacked/criminal /24+)
  - EDROP: https://www.spamhaus.org/drop/edrop.txt  (extended list)

License: free with attribution; Spamhaus DROP/EDROP are explicitly
designed for redistribution within block-list use.

Plain-text format — one CIDR + SBL reference per line, semicolon-prefix
comments:

    ; Spamhaus DROP List
    ; Last-Modified: 2026-05-27T18:00:00Z
    1.10.16.0/20 ; SBL257397
    1.34.96.0/19 ; SBL354646

We pull both DROP + EDROP every hour (Spamhaus's operational guidance:
hourly is fine; faster is rude). Each block becomes a single event
keyed by SBL ID. Blocks removed from a future poll won't have their
row deleted — instead the row's decay_half_life_min (30 days) means
downstream queries naturally weight it down. A row's still-active
status is verified by whether a fresh poll re-emits it; the writer's
INSERT...ON CONFLICT updates the same id deterministically.

SBL_ID is the unique key. We anchor event_time to a stable polling
time (now) so the same SBL re-emitted next cycle is dedup'd via the
deterministic UUID5 derivation.
"""

from __future__ import annotations

import ipaddress
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import aiohttp

from .base import GlassboxEvent, Ingester


# ─── Severity helper ─────────────────────────────────────────────────────


_LIST_SEVERITY = {
    "DROP":  8,   # hijacked / criminal control of /24+ blocks
    "EDROP": 7,   # extended list — lower-confidence but still actively malicious
}


def _severity_for_list(list_name: str) -> int:
    return _LIST_SEVERITY.get((list_name or "").upper(), 4)


# ─── Plain-text line parser ──────────────────────────────────────────────


_LINE_RE = re.compile(
    r"^\s*"
    r"(?P<cidr>[0-9a-fA-F:.]+/[0-9]+)"
    r"\s*;\s*"
    r"(?P<sbl>SBL[0-9A-Z]+)"
    r"\s*$"
)


def _parse_drop_line(line: str) -> Tuple[Optional[str], Optional[str]]:
    """Parse one DROP/EDROP plain-text line. Returns (cidr, sbl_id) or
    (None, None) for comments / blanks / malformed lines."""
    if not line:
        return (None, None)
    s = line.strip()
    if not s or s.startswith(";"):
        return (None, None)
    m = _LINE_RE.match(s)
    if not m:
        return (None, None)
    cidr = m.group("cidr")
    sbl = m.group("sbl")
    # Validate the CIDR — guards against future Spamhaus format changes.
    try:
        ipaddress.ip_network(cidr, strict=False)
    except ValueError:
        return (None, None)
    if not sbl:
        return (None, None)
    return (cidr, sbl)


# ─── Ingester ─────────────────────────────────────────────────────────────


class SpamhausDropIngester(Ingester):
    layer = "cyber_spamhaus_drop"
    source = "Spamhaus DROP/EDROP"
    source_id = "spamhaus_drop"           # gates against infra/sources.yaml
    poll_interval_sec = 3600.0            # 1h — Spamhaus operational guidance

    DROP_URL = "https://www.spamhaus.org/drop/drop.txt"
    EDROP_URL = "https://www.spamhaus.org/drop/edrop.txt"
    UA = "FulcrumGlassbox/2.0 (mewrcreate.com/glassbox)"

    async def fetch(self) -> List[Dict[str, Any]]:
        """Pull both DROP + EDROP feeds. Returns a list of two dicts,
        one per list; normalize() flattens them into events.

        Smoke mode caps each list to 25 entries to keep smoke runs fast.
        """
        out: List[Dict[str, Any]] = []
        timeout = aiohttp.ClientTimeout(total=60)
        headers = {"User-Agent": self.UA, "Accept": "text/plain"}
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as s:
            for list_name, url in (("DROP", self.DROP_URL), ("EDROP", self.EDROP_URL)):
                try:
                    async with s.get(url) as r:
                        r.raise_for_status()
                        text = await r.text()
                except Exception as e:
                    self.log.info(f"[spamhaus_drop] {list_name} fetch failed: {e}")
                    continue

                entries: List[Tuple[str, str]] = []
                for raw_line in text.splitlines():
                    cidr, sbl = _parse_drop_line(raw_line)
                    if cidr and sbl:
                        entries.append((cidr, sbl))
                    if self.smoke_mode and len(entries) >= 25:
                        break
                out.append({"list_name": list_name, "entries": entries})
        return out

    def normalize(self, raw_items: List[Dict[str, Any]]) -> List[GlassboxEvent]:
        out: List[GlassboxEvent] = []
        now_iso = datetime.now(timezone.utc).isoformat()
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            list_name = (item.get("list_name") or "").upper()
            entries = item.get("entries") or []
            severity = _severity_for_list(list_name)
            for entry in entries:
                # Defensive: only accept (str, str) tuples
                if (
                    not isinstance(entry, (tuple, list))
                    or len(entry) != 2
                    or not isinstance(entry[0], str)
                    or not isinstance(entry[1], str)
                ):
                    continue
                cidr, sbl_id = entry
                if not cidr or not sbl_id:
                    continue
                out.append(GlassboxEvent(
                    layer=self.layer,
                    external_id=f"spamhaus:{sbl_id}",
                    kind="spamhaus_block_entry",
                    lat=0.0,
                    lng=0.0,
                    ts=now_iso,
                    severity=severity,
                    source=self.source,
                    payload={
                        "cidr": cidr,
                        "sbl_id": sbl_id,
                        "list_name": list_name,
                        "title": f"{list_name} block {cidr} ({sbl_id})",
                        "link": f"https://www.spamhaus.org/sbl/query/{sbl_id}",
                        "_attribution": "Block lists: Spamhaus",
                    },
                    domain="cyber",
                    geocode_quality="not_geo",
                    decay_half_life_min=43200,    # 30 days
                    market_tags=["cyber:spamhaus_block"],
                    severity_for_market=2,
                ))
        return out
